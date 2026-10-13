# -*- coding: utf-8 -*-
"""Build a brownfield (existing-infrastructure) ZIP from a completed design.

Why this exists
---------------
The brownfield the engine used to feed itself was the project's own previous
run, whole: every trench, every spare duct way -- hundreds of line features
whose footprint is the design, so the "Existing infrastructure" layer on the
portal was a copy of the design rather than the handful of assets that actually
exist in the ground, and it carried no points at all (0 chambers, 0 poles,
0 PDPs).

The planner supplies the brownfield. Until they do, this builds a stand-in with
the shape a real handover has.

The REGION rule
---------------
Scattering one trench here and one chamber there over a whole ward is not what
a handover looks like, and on the map it reads as noise spread across the
project rather than an existing network. So the archive is drawn from **one
region of the design**: the distribution region (``Polygons`` ``POLYGON_ID``)
that carries the exchange -- the MFG -- plus its nearest neighbouring regions,
up to five. That is a compact, contiguous area a few hundred metres across,
and it is the smallest area that can hold 3 to 5 PDPs, because a region hosts
exactly one PDP.

What it writes
--------------
One GeoJSON per brownfield parameter, named with the token the engine's
``_match_brownfield_param`` resolves, so the archive needs no manifest:

    bf_feeder_trench.geojson      -> BF_FEEDER_TRENCH
    bf_distribution_trench.geojson-> BF_DIST_TRENCH
    bf_ducts.geojson              -> BF_DUCTS
    bf_fibre.geojson              -> BF_FIBRE          (the cables)
    bf_chambers.geojson           -> BF_CHAMBERS
    bf_poles.geojson              -> BF_POLES
    bf_existing_pdp.geojson       -> BF_EXISTING_PDP
    bf_existing_mfg.geojson       -> BF_EXISTING_MFG

``--count`` (default 5, i.e. "3 to 5 of each") caps each file. Assets inside
the region are preferred and spread evenly through it (not the first N, which
would pile every asset into the first polygon walked); when the region holds
fewer than three of something -- a trunk corridor often clips only one or two
regions -- the nearest assets outside it top the file up to three, so the
archive always carries a usable network.

Poles are the one asset the design cannot supply
------------------------------------------------
A completed design plans poles only on the aerial legs it found, and on a ward
with none to find the layer is empty -- yet a handover almost always includes
the existing pole line. When the design has fewer than ``--count`` poles in the
region (the usual case: zero), this places the pole line **along the region's
own distribution corridor**, spaced ``--pole-spacing`` (45 m, plus a 1.5 m
offset so the pole stands beside the trench, not in it) and marked
``VERIFY_STATUS=Survey Required``: a stand-in the planner can walk and correct,
never a claim that a pole was surveyed.

Fields
------
Every row keeps its own attributes and gains the ones the brownfield loader
keys on: ``ASSET_TYPE`` / ``INFRA_STATUS`` / ``VERIFY_STATUS`` /
``USE_MODE=brownfield`` (preferred, weight 0.1x -- the router may still pick a
better path) plus the auto-detected capacity pair ``capacity_total`` /
``capacity_used``. Duct and cable capacity come from ``occupancy.duct_rows`` /
``occupancy.cable_rows`` -- the SAME derivation the engine stores in
``gis.duct_occupancy`` / ``gis.cable_occupancy`` -- so this brownfield and that
registry can never disagree about how many ways a duct has left.

Only rows with spare capacity are taken (a full duct or a cable with no free
fibre is unusable and the loader skips it anyway). A chamber whose own
``CAPACITY_TOTAL`` is unrecorded is brought in as 1-way with 0 used: a 0/0 row
would make ``has_capacity`` false and the asset silently unreusable, which is
the opposite of what "existing infrastructure" means.

Usage (from HLD_Planning_01/web/backend, any python):

    python ../tools/make_brownfield_layers.py [--project-id <id>]
                                              [--count 5]
                                              [--regions 5]
                                              [--out ../brownfield_layers.zip]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BACKEND = Path(__file__).resolve().parents[1] / "web" / "backend"
sys.path.insert(0, str(BACKEND))

import occupancy  # noqa: E402

DEFAULT_PROJECT = "24cd3573f4884a578c1b98a80bbd4040"
OUTPUTS = BACKEND / "outputs"
HLD_ROOT = Path(__file__).resolve().parents[1]

# "3 to 5 of each" is the shape of a handover: fewer than three assets is not a
# network, more than five is a design.
MIN_PER_LAYER = 3
POLE_OFFSET_M = 1.5


# ── helpers ─────────────────────────────────────────────────────────────────

def _load(output_dir: Path, name: str) -> List[Dict[str, Any]]:
    """The features of one design output, or ``[]`` when it is not there."""
    path = output_dir / f"{name}.geojson"
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    return [f for f in (doc.get("features") or []) if f.get("geometry")]


def _sample(features: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """``count`` features spread evenly through the list (all of them if fewer).

    Even spacing, not the first N: the sample has to cover the region. Taking
    the head of the layer would pile every brownfield asset into the first
    corridor the designer happened to walk.
    """
    if count <= 0 or len(features) <= count:
        return list(features)
    if count == 1:
        return [features[len(features) // 2]]
    step = (len(features) - 1) / float(count - 1)
    return [features[int(round(i * step))] for i in range(count)]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _stamp(props: Dict[str, Any], kind: str, *, src_id: Any = None,
           capacity_total: int = 1, capacity_used: int = 0,
           verify: str = "Verified") -> Dict[str, Any]:
    """The brownfield fields every asset gets, whatever its type."""
    out = dict(props)
    stamped = {
        "STAGE": "brownfield",
        "ASSET_TYPE": kind,
        "SRC_ID": props.get("SRC_ID") or src_id,
        "INFRA_STATUS": "Existing",
        "VERIFY_STATUS": props.get("VERIFY_STATUS") or verify,
        "capacity_total": int(capacity_total),
        "capacity_used": max(0, int(capacity_used)),
        "USE_MODE": "brownfield",
    }
    # A GeoPackage is case-INsensitive about field names, so a source row that
    # already carries the upper-case twin of a field stamped here (the design's
    # CAPACITY_TOTAL / CAPACITY_USED next to this builder's lower-case pair)
    # makes the archive unloadable: OGR refuses the second field
    #     "Cannot create field capacity_total. A field with the same name
    #      already exists."
    # and the whole run dies in the trench stage. The stamped value wins; the
    # twin is dropped.
    for name in stamped:
        for twin in [k for k in out if k != name and k.lower() == name.lower()]:
            del out[twin]
    out.update(stamped)
    return out


def _feature(geom: Dict[str, Any], props: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "Feature", "geometry": geom, "properties": props}


def _write(dest: Path, features: Iterable[Dict[str, Any]]) -> Optional[Path]:
    features = list(features)
    if not features:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=1),
        encoding="utf-8",
    )
    return dest


# ── flat planar geometry (WGS84 lon/lat, no GDAL needed) ────────────────────

def _coords(geom: Dict[str, Any]) -> List[List[float]]:
    """Every vertex of a (Multi)Point/Line/Polygon as ``[x, y]``."""
    out: List[List[float]] = []

    def walk(node: Any) -> None:
        if not node:
            return
        if isinstance(node[0], (int, float)):
            out.append([float(node[0]), float(node[1])])
            return
        for child in node:
            walk(child)

    walk(geom.get("coordinates"))
    return out


def _rings(geom: Dict[str, Any]) -> List[List[List[float]]]:
    """The exterior rings of a (Multi)Polygon."""
    if not geom:
        return []
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _ring_contains(ring: Sequence[Sequence[float]], x: float, y: float) -> bool:
    """Ray casting; the ring is treated as closed."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _centroid(geom: Dict[str, Any]) -> Tuple[float, float]:
    pts = _coords(geom)
    if not pts:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def _metres(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Planar distance in metres at this latitude (small-area approximation)."""
    lat = math.radians((a[1] + b[1]) / 2.0)
    dx = (b[0] - a[0]) * 111320.0 * math.cos(lat)
    dy = (b[1] - a[1]) * 110540.0
    return math.hypot(dx, dy)


def _representative(geom: Dict[str, Any]) -> Tuple[float, float]:
    """A point that decides whether an asset is inside the region.

    A line is judged by its MIDPOINT: a trench that runs out of the region to
    the exchange still belongs to the region it was planned for. A point asset
    is judged by the point itself.
    """
    pts = _coords(geom)
    if not pts:
        return (0.0, 0.0)
    if len(pts) == 1:
        return (pts[0][0], pts[0][1])
    return ((pts[0][0] + pts[-1][0]) / 2.0, (pts[0][1] + pts[-1][1]) / 2.0)


def _line_length_m(geom: Dict[str, Any]) -> float:
    pts = _coords(geom)
    return sum(_metres((a[0], a[1]), (b[0], b[1])) for a, b in zip(pts, pts[1:]))


# ── the region ──────────────────────────────────────────────────────────────

class Region:
    """The cluster of design regions the brownfield is drawn from."""

    def __init__(self, ids: Sequence[str], rings: List[List[List[float]]],
                 centre: Tuple[float, float]) -> None:
        self.ids = list(ids)
        self.rings = rings
        self.centre = centre

    def contains(self, geom: Optional[Dict[str, Any]]) -> bool:
        if not geom:
            return False
        x, y = _representative(geom)
        return any(_ring_contains(r, x, y) for r in self.rings)

    def metres_to(self, geom: Optional[Dict[str, Any]]) -> float:
        if not geom:
            return float("inf")
        return _metres(self.centre, _representative(geom))

    def describe(self, output_dir: Path) -> str:
        polys = {str((f.get("properties") or {}).get("POLYGON_ID")): f
                 for f in _load(output_dir, "Polygons")}
        areas = sum(_polygon_area_m2(polys[i]["geometry"])
                    for i in self.ids if i in polys)
        return ("%s (%.1f ha, %.0f m across)"
                % (", ".join(self.ids), areas / 10000.0, self._width_m()))

    def _width_m(self) -> float:
        pts = [p for ring in self.rings for p in ring]
        if not pts:
            return 0.0
        xs = [_metres((min(p[0] for p in pts), self.centre[1]), (max(p[0] for p in pts), self.centre[1]))]
        ys = [_metres((self.centre[0], min(p[1] for p in pts)), (self.centre[0], max(p[1] for p in pts)))]
        return max(xs[0], ys[0])


def _polygon_area_m2(geom: Dict[str, Any]) -> float:
    total = 0.0
    for ring in _rings(geom):
        pts = [(p[0] * 111320.0 * math.cos(math.radians(p[1])), p[1] * 110540.0)
               for p in ring]
        acc = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            acc += x1 * y2 - x2 * y1
        total += abs(acc) / 2.0
    return total


def select_region(output_dir: Path, want: int) -> Region:
    """The region cluster: the exchange's region plus its nearest neighbours.

    The MFG is the head of the network, so the region that carries it is the
    one whose corridor already runs through the area -- seeding there keeps the
    feeder trench, the feeder duct and the feeder cable inside the cluster
    instead of leaving a handover with distribution assets only.
    """
    polys = _load(output_dir, "Polygons")
    if not polys:
        raise SystemExit("[fatal] the design has no Polygons layer to define a region")
    entries = []
    for feat in polys:
        props = feat.get("properties") or {}
        pid = str(props.get("POLYGON_ID") or props.get("SRC_ID") or "").strip()
        if not pid:
            continue
        entries.append((pid, feat["geometry"], _centroid(feat["geometry"])))
    if not entries:
        raise SystemExit("[fatal] every Polygon row is missing POLYGON_ID")

    mfg = _load(output_dir, "MFG")
    seed_pt = _centroid(mfg[0]["geometry"]) if mfg else entries[0][2]
    chosen = [min(entries, key=lambda e: _metres(seed_pt, e[2]))]
    while len(chosen) < min(max(1, want), len(entries)):
        rest = [e for e in entries if e not in chosen]
        chosen.append(min(rest, key=lambda e: min(_metres(e[2], c[2]) for c in chosen)))

    rings: List[List[List[float]]] = []
    for _pid, geom, _c in chosen:
        rings.extend(_rings(geom))
    centre = (sum(c[2][0] for c in chosen) / len(chosen),
              sum(c[2][1] for c in chosen) / len(chosen))
    return Region([c[0] for c in chosen], rings, centre)


def _in_region(region: Region, prop_polygon: Any, geom: Dict[str, Any]) -> bool:
    """Inside the region: by the row's own POLYGON_ID, or by its geometry.

    Both signals are used because the design stamps POLYGON_ID on the assets it
    attributed to a region, while the trunk rows (feeder trench, duct, cable)
    carry no region at all -- their geometry is the only thing that can place
    them.
    """
    pid = str(prop_polygon or "").strip()
    if pid and pid in region.ids:
        return True
    return region.contains(geom)


def _choose(candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]],
            region: Region, count: int) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Up to ``count`` (feature, props) rows: inside the region first.

    A region is small -- half a hectare -- so a trunk corridor often clips only
    one or two of them. When the region cannot fill three of something, the
    nearest rows to the region centre top the file up, so the archive still
    describes a network rather than a stub.
    """
    def inside(item):
        feat, props = item
        return _in_region(region, props.get("POLYGON_ID"), feat["geometry"])

    in_region = [c for c in candidates if inside(c)]
    if len(in_region) >= MIN_PER_LAYER:
        return _sample(in_region, count)
    outside = sorted((c for c in candidates if c not in in_region),
                     key=lambda c: region.metres_to(c[0]["geometry"]))
    return in_region + outside[:max(0, count - len(in_region))]


# ── per-asset builders ──────────────────────────────────────────────────────

def trench_files(output_dir: Path, region: Region, staging: Path,
                 count: int) -> List[Path]:
    """Existing trenches, one file per tier (feeder / distribution).

    Mirrors ``occupancy.trench_brownfield_features``: a trench the last design
    planned is excavation that exists on paper, so it is fed as existing
    infrastructure with one reusable way (a trench carries a duct or a cable)
    and read by the router exactly as the duct read-back is.
    """
    features = _load(output_dir, "Final_Trenches")
    by_tier: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for feat in features:
        props = feat.get("properties") or {}
        tier = str(props.get("TRENCH_TIER") or "").strip().lower()
        if not tier:
            continue
        by_tier.setdefault(tier, []).append((feat, props))

    written: List[Path] = []
    for tier, name in (("feeder", "bf_feeder_trench.geojson"),
                       ("distribution", "bf_distribution_trench.geojson")):
        rows = []
        for feat, props in _choose(by_tier.get(tier, []), region, count):
            clean = {k: v for k, v in props.items() if k != "id"}
            clean["length_m"] = round(_line_length_m(feat["geometry"]), 1)
            rows.append(_feature(feat["geometry"], _stamp(
                clean, "trench",
                src_id=props.get("TRENCH_ID"),
                capacity_total=1, capacity_used=0, verify="Designed",
            )))
        path = _write(staging / name, rows)
        if path:
            written.append(path)
    return written


def duct_features(output_dir: Path, region: Region, count: int) -> List[Dict[str, Any]]:
    """Existing ducts with spare ways, from the same rows the registry stores."""
    rows = [r for r in occupancy.duct_rows(output_dir)
            if _int((r.get("properties") or {}).get("SPARE_WAYS")) > 0]
    out = []
    for row, props in _choose([(r, r.get("properties") or {}) for r in rows],
                              region, count):
        props = dict(props)
        total = _int(props.get("WAYS_TOTAL"), 1) or 1
        used = _int(props.get("WAYS_USED"))
        out.append(_feature(row["geometry"], _stamp(
            props, "duct", src_id=props.get("DUCT_ID"),
            capacity_total=total, capacity_used=used, verify="Designed",
        )))
    return out


def cable_features(output_dir: Path, region: Region, count: int) -> List[Dict[str, Any]]:
    """Existing cables with free fibre, as brownfield FIBRE lines.

    ``BF_FIBRE`` is not decoration: ``BrownfieldRegistry.iter_graph_edges``
    yields every line asset, so a cable handed in here is a preferred edge in
    the routing graph exactly like a duct or a trench.
    """
    rows = [r for r in occupancy.cable_rows(output_dir)
            if _int((r.get("properties") or {}).get("AVAILABLE_FIBRES")) > 0]
    out = []
    for row, props in _choose([(r, r.get("properties") or {}) for r in rows],
                              region, count):
        props = dict(props)
        total = _int(props.get("FIBER_COUNT"), 1) or 1
        used = _int(props.get("FIBRES_USED"))
        out.append(_feature(row["geometry"], _stamp(
            props, "fibre", src_id=props.get("CABLE_ID"),
            capacity_total=total, capacity_used=used, verify="Designed",
        )))
    return out


def chamber_features(output_dir: Path, region: Region, count: int) -> List[Dict[str, Any]]:
    """Existing chambers (manholes / handholes), reusable structures."""
    out = []
    for feat in _sample_in_region(output_dir, "Chambers", region, count):
        props = feat.get("properties") or {}
        recorded = _int(props.get("CAPACITY_TOTAL"))
        total = recorded if recorded > 0 else 1
        out.append(_feature(feat["geometry"], _stamp(
            props, "chamber", src_id=props.get("STRUCT_ID"),
            capacity_total=total, capacity_used=_int(props.get("CAPACITY_USED")),
        )))
    return out


def _sample_in_region(output_dir: Path, layer: str, region: Region,
                      count: int) -> List[Dict[str, Any]]:
    """``count`` features of a point layer, region first (see ``_choose``)."""
    feats = _load(output_dir, layer)
    chosen = _choose([(f, f.get("properties") or {}) for f in feats], region, count)
    return [f for f, _p in chosen]


def pole_features(output_dir: Path, region: Region, spine: List[Dict[str, Any]],
                  count: int) -> List[Dict[str, Any]]:
    """Existing poles: the design's own where it has them, else the region's spine.

    A design plans poles only on the aerial legs it found, so a ward with none
    to find publishes an empty layer -- but a handover normally carries the
    existing pole line. The stand-ins are placed ALONG the region's distribution
    corridor (the pipe a pole line follows) and marked ``Survey Required``, so
    they read as "to be walked", not as surveyed plant.
    """
    out = []
    poles = _sample_in_region(output_dir, "Poles", region, count)
    for feat in poles:
        props = feat.get("properties") or {}
        # cable slots on the pole
        total = _int(props.get("CAPACITY_TOTAL")) or 2
        out.append(_feature(feat["geometry"], _stamp(
            props, "pole", src_id=props.get("POLE_ID"),
            capacity_total=total, capacity_used=_int(props.get("CAPACITY_USED")),
            verify="Verified",
        )))
    if len(out) >= MIN_PER_LAYER:
        return out
    for index, geom in enumerate(_pole_line(spine, count - len(out)), start=1):
        out.append(_feature(geom, _stamp(
            {"POLE_ID": "BF_POLE_%03d" % index, "POLE_TYPE": "Telecom",
             "EQUIPMENT": "", "MATERIAL": "Wood", "HEIGHT_M": 9.0},
            "pole", src_id="BF_POLE_%03d" % index,
            capacity_total=2, capacity_used=0, verify="Survey Required",
        )))
    return out


def _pole_line(spine: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """``count`` pole positions spaced along the longest corridor of the region.

    Poles follow the duct/trench, so the stand-in line is the longest line
    asset the region already has; nothing is invented away from the corridor.
    """
    if count <= 0 or not spine:
        return []
    longest = max(spine, key=lambda f: _line_length_m(f["geometry"]))
    pts = _coords(longest["geometry"])
    if len(pts) < 2:
        return []
    total = _line_length_m(longest["geometry"])
    if total <= 0:
        return []
    spacing = total / (count + 1)
    wanted = [spacing * (i + 1) for i in range(count)]

    out: List[Dict[str, Any]] = []
    run = 0.0
    idx = 0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = _metres((a[0], a[1]), (b[0], b[1]))
        while idx < len(wanted) and run + seg >= wanted[idx]:
            t = (wanted[idx] - run) / seg if seg > 0 else 0.0
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            # Stand beside the corridor, not in it: 1.5 m to the left of travel.
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            norm = math.hypot(dx, dy) or 1.0
            off_x = POLE_OFFSET_M / (111320.0 * math.cos(math.radians(y)))
            off_y = POLE_OFFSET_M / 110540.0
            out.append({"type": "Point",
                        "coordinates": [round(x - dy / norm * off_x, 7),
                                        round(y + dx / norm * off_y, 7)]})
            idx += 1
        run += seg
    return out


def pdp_features(output_dir: Path, region: Region, count: int) -> List[Dict[str, Any]]:
    """Existing PDPs: the network stage snaps a new PDP onto one within 10 m."""
    out = []
    for feat in _sample_in_region(output_dir, "PDPs", region, count):
        props = feat.get("properties") or {}
        out.append(_feature(feat["geometry"], _stamp(
            props, "pdp", src_id=props.get("PDP_ID"),
            capacity_total=1, capacity_used=0,
        )))
    return out


def mfg_features(output_dir: Path, region: Region, count: int) -> List[Dict[str, Any]]:
    """Existing MFG(s) -- at most one per design, so ``count`` rarely bites."""
    out = []
    for feat in _sample_in_region(output_dir, "MFG", region, count):
        props = feat.get("properties") or {}
        out.append(_feature(feat["geometry"], _stamp(
            props, "mfg", src_id=props.get("MFG_ID"),
            capacity_total=1, capacity_used=0,
        )))
    return out


# ── entry point ─────────────────────────────────────────────────────────────

def build(project_id: str, count: int, out_zip: Path,
          regions: int = 5) -> Tuple[Dict[str, int], str]:
    output_dir = OUTPUTS / project_id
    if not output_dir.is_dir():
        raise SystemExit(f"[fatal] no design outputs for {project_id}: {output_dir}")

    region = select_region(output_dir, regions)
    staging = HLD_ROOT / "brownfield_layers"
    if staging.is_dir():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    written: List[Path] = list(trench_files(output_dir, region, staging, count))
    # The pole stand-ins follow the region's own corridor, so they are placed
    # on the trenches that were just written rather than on the whole design.
    trenches = [f for path in written
                for f in (json.loads(path.read_text(encoding="utf-8")).get("features") or [])]
    for name, features in (
        ("bf_ducts.geojson", duct_features(output_dir, region, count)),
        ("bf_fibre.geojson", cable_features(output_dir, region, count)),
        ("bf_chambers.geojson", chamber_features(output_dir, region, count)),
        ("bf_poles.geojson", pole_features(output_dir, region, trenches, count)),
        ("bf_existing_pdp.geojson", pdp_features(output_dir, region, count)),
        ("bf_existing_mfg.geojson", mfg_features(output_dir, region, count)),
    ):
        path = _write(staging / name, features)
        if path:
            written.append(path)

    # The region travels beside the archive (never inside it: the zip is fed
    # straight to the pipeline, and a stray JSON in it would be sniffed as a
    # brownfield layer). It is what a reviewer checks the sample against.
    (staging / "_region.json").write_text(json.dumps({
        "project_id": project_id,
        "polygon_ids": region.ids,
        "centre": list(region.centre),
        "rings": region.rings,
        "description": region.describe(output_dir),
    }, indent=1), encoding="utf-8")

    _write_readme(staging, output_dir, project_id, region, out_zip)

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(written):
            zf.write(path, path.name)
            payload = json.loads(path.read_text(encoding="utf-8"))
            counts[path.name] = len(payload.get("features") or [])
    return counts, region.describe(output_dir)


def _write_readme(staging: Path, output_dir: Path, project_id: str,
                  region: Region, out_zip: Path) -> None:
    """Say what this archive is and how to rebuild it, beside the files.

    The folder is regenerated on every build, so the note is written by the
    builder rather than left behind by hand -- a stale note describing an
    earlier sample is worse than none.
    """
    lines = [
        "# Brownfield archive - %s" % project_id,
        "",
        "Existing-infrastructure inputs for the brownfield HLD run, drawn from the",
        "design's own network in ONE region:",
        "",
        "    %s" % region.describe(output_dir),
        "",
        "| File | Parameter | Features |",
        "|---|---|---|",
    ]
    for path in sorted(staging.glob("bf_*.geojson")):
        count = len((json.loads(path.read_text(encoding="utf-8")) or {}).get("features") or [])
        lines.append("| `%s` | `%s` | %d |" % (path.name,
                                               _param_of(path.name), count))
    lines += [
        "",
        "Zip the `bf_*.geojson` files (not this note, not `_region.json`) and upload",
        "the zip as the project's brownfield archive:",
        "",
        "    %s" % out_zip,
        "",
        "Rebuild with:",
        "",
        "```bash",
        "python HLD_Planning_01/tools/make_brownfield_layers.py \\",
        "    --project-id %s --count 5 --regions 5" % project_id,
        "```",
        "",
        "Poles are the one stand-in: a design plans poles only on the aerial legs",
        "it found, so where it has none the pole line is drawn along the region's",
        "distribution corridor and marked `Survey Required`.",
    ]
    (staging / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _param_of(filename: str) -> str:
    """The BF_* parameter a filename resolves to, by the engine's own tokens."""
    stem = filename.lower()
    if "pdp" in stem:
        return "BF_EXISTING_PDP"
    if "mfg" in stem:
        return "BF_EXISTING_MFG"
    if "chamber" in stem:
        return "BF_CHAMBERS"
    if "pole" in stem:
        return "BF_POLES"
    if "fibre" in stem or "fiber" in stem:
        return "BF_FIBRE"
    if "trench" in stem and "feeder" in stem:
        return "BF_FEEDER_TRENCH"
    if "trench" in stem and ("dist" in stem or "distribution" in stem):
        return "BF_DIST_TRENCH"
    if "trench" in stem:
        return "BF_TRENCHES"
    if "duct" in stem:
        return "BF_DUCTS"
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-id", default=DEFAULT_PROJECT)
    ap.add_argument("--count", type=int, default=5,
                    help="features per layer (default 5: '3 to 5 of each')")
    ap.add_argument("--regions", type=int, default=5,
                    help="distribution regions to draw the region from (default 5)")
    ap.add_argument("--out", default=str(HLD_ROOT / "brownfield_layers.zip"))
    args = ap.parse_args()

    counts, region = build(args.project_id, args.count, Path(args.out), args.regions)
    print(f"[brownfield] region: {region}")
    print(f"[brownfield] {len(counts)} layer file(s) -> {args.out}")
    for name, n in sorted(counts.items()):
        print(f"  {name:32s} {n:3d} feature(s)")
    if not counts:
        print("[brownfield] nothing written - is this project's design on disk?")
        return 1
    # The exchange is one per design, so it is never expected to reach three.
    thin = {n: c for n, c in counts.items()
            if c < MIN_PER_LAYER and n != "bf_existing_mfg.geojson"}
    if thin:
        print(f"[brownfield] WARNING: fewer than {MIN_PER_LAYER} of: {thin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
