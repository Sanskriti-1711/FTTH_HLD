# -*- coding: utf-8 -*-
"""Civil Trench Designer — standalone sub-process (Phase A).

Designs the FTTH **trench network** from the plan instead of deriving it from
road sidewalks, so that the published trenches are the civil corridor that
chambers, ducts and cables are built on (design the trench first, then lay the
duct bank inside it).

Pipeline position (target): after Polygons / MFG / PDPs / Objects and before
the chamber + duct layers. Runs as an independent process over GeoPackages, so
it can be exercised on its own and embedded in the engine pipeline later.

Inputs
------
* ``--mfg``       MFG point(s)          — feeder origin
* ``--pdps``      PDP points            — splitters (feeder destination)
* ``--objects``   house/premise points  — garden drop legs
* ``--polygons``  service areas         — group houses to their PDP
* ``--roads``     OSM road lines        — walkable carrier + vehicular crossings
* ``--aerial``    (optional) aerial zones — legs inside are not trenched

Outputs (in ``--out``)
----------------------
* ``Final_Trenches.gpkg`` + ``.geojson``        — node-to-node spans (§4.8)
* ``Feeder_Trench.gpkg`` / ``Distribution_Trench.gpkg`` / ``Garden_Trench.gpkg``
* ``Trench_Nodes.gpkg``                          — structural nodes for chambers
* ``Tangent_Crossings.gpkg``                     — HDD drills
* ``trench_design_report.json``                  — counts, lengths, checks

Design rules: see ``docs/subprojects/ftth-engine/TRENCH_DESIGN.md``.

Dependencies: GDAL/OGR + networkx only (no QGIS, no shapely) so the process
runs in the same environment as the other post-processing utilities.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import networkx as nx
from osgeo import ogr, osr

# ─────────────────────────────────────────────────────────────────────────────
# Parameters
# ─────────────────────────────────────────────────────────────────────────────

WALKABLE_CLASSES = (
    "footway", "path", "pedestrian", "living_street", "service",
    "cycleway", "steps", "track", "residential", "unclassified",
    "bridleway", "sidewalk",
)
# Walkable classes that are *not* carriageways — these are never "crossed".
PURE_FOOTWAY_CLASSES = ("footway", "path", "pedestrian", "cycleway", "steps",
                        "bridleway", "sidewalk")

# Classes a trench is carried by. These are the carriers the existing HLD
# engine routes on: footway / path / pedestrian / sidewalk / service /
# cycleway ("Build network from OSM footways/paths/service; cross vehicular
# roads with perpendicular drills").
PREFERRED_CARRIER_CLASSES = (
    "footway", "path", "pedestrian", "sidewalk", "service", "cycleway",
)
# Carriageway classes. A trench CAN run along one (Berlin has streets whose
# footway is unmapped), but only when no preferred carrier gets there — see
# CLASS_FACTOR. `track` is a field/dirt track: trenchable only as a last
# resort.
FALLBACK_CARRIER_CLASSES = (
    "residential", "unclassified", "living_street", "track",
)

# Carriageway width by OSM class — used as the drill (HDD) length base.
VEHICULAR_WIDTH_M = {
    "motorway": 14.0, "motorway_link": 6.0,
    "trunk": 12.0, "trunk_link": 6.0,
    "primary": 11.0, "primary_link": 5.0,
    "secondary": 9.0, "secondary_link": 4.0,
    "tertiary": 7.5, "tertiary_link": 4.0,
    "residential": 6.5, "unclassified": 6.0,
    "service": 5.0, "living_street": 5.5, "track": 4.0,
}
# Classes a trench may not sit along (they are crossed, not followed).
NON_CARRIER_CLASSES = tuple(
    c for c in ("motorway", "motorway_link", "trunk", "trunk_link",
                "primary", "primary_link", "secondary", "secondary_link",
                "tertiary", "tertiary_link")
)

# Edge weight multipliers: routing prefers the sidewalk corridor.
#
# The preferred carriers are cheap, carriageways are expensive — a router will
# walk a footway detour many times longer before it runs a trench down a
# carriageway. This is the "avoid the streets" rule: the old engine refused to
# route on carriageways at all, but refusing outright breaks areas with
# unmapped footways, so they stay reachable at a heavy cost.
CARRIAGE_FACTOR = 12.0
CLASS_FACTOR = {
    "footway": 1.0, "path": 1.0, "pedestrian": 1.0, "sidewalk": 1.0,
    "cycleway": 1.05, "service": 1.05, "steps": 1.6, "bridleway": 1.3,
}

# The street-avoidance ladder. A trench along a carriageway needs a road
# opening permit and traffic management, so the cost is ordered by how big the
# road is — a district road is far worse to dig than a residential street:
#
#     residential < tertiary < secondary < primary/motorway
#
# Every class stays *routable* (a graph that refuses outright disconnects), but
# at these weights the router will accept a kilometres-long footway detour
# before it enters a street, and when it must use one it picks the smallest
# class available. `track` is a field haul road: cheap to cross, poor to dig.
STREET_CLASS_FACTOR = {
    "track": CARRIAGE_FACTOR * 0.85,      # 10.2 — dirt/field track
    "residential": CARRIAGE_FACTOR,       # 12.0 — base street cost
    "unclassified": CARRIAGE_FACTOR,
    "living_street": CARRIAGE_FACTOR,
    "tertiary_link": CARRIAGE_FACTOR * 2.5,
    "tertiary": CARRIAGE_FACTOR * 3.0,    # 36.0 — district road
    "secondary_link": CARRIAGE_FACTOR * 5.0,
    "secondary": CARRIAGE_FACTOR * 6.0,   # 72.0 — state road
    "primary_link": CARRIAGE_FACTOR * 8.0,
    "primary": CARRIAGE_FACTOR * 9.0,     # 108.0 — federal road
    "trunk": CARRIAGE_FACTOR * 12.0,
    "motorway": CARRIAGE_FACTOR * 15.0,
}
NON_CARRIER_FACTOR = CARRIAGE_FACTOR  # any carriageway not in the ladder


@dataclass
class Params:
    """Designer knobs (see TRENCH_DESIGN.md §4)."""

    target_epsg: int = 25833
    simplify_tol_m: float = 2.5       # Douglas–Peucker straightening
    min_vertex_gap_m: float = 5.0     # thin vertices closer than this
    bend_deg: float = 8.0             # drop bends shallower than this
    max_garden_m: float = 60.0        # longer than this ⇒ Open Cut, not Garden
    drill_extra_m: float = 2.0        # drill = road width + this
    crossing_merge_m: float = 45.0    # a junction this close shares the crossing
    crossing_dedupe_m: float = 25.0   # suppress a drill this close to another
    hdd_pit_keepout_m: float = 15.0   # keep-out around HDD entry/exit pits
    min_node_sep_m: float = 10.0      # floor between any two structures
    pull_backbone_m: float = 250.0    # intermediate pull chamber interval
    pull_dist_m: float = 100.0
    junction_deg: int = 3             # degree that counts as a branch
    pdp_search_m: float = 60.0        # PDP → graph snap limit
    house_search_m: float = 120.0     # house → network search limit
    road_bbox_buffer_m: float = 400.0  # AOI buffer for reading OSM roads
    crossing_angle_deg: float = 20.0  # below this the trench runs along the road
    # Street avoidance: multiplier on the whole carriageway cost ladder
    # (STREET_CLASS_FACTOR). 1.0 = the tuned default; raise to push the router
    # further onto footways, lower to let it use streets sooner.
    street_avoid_scale: float = 1.0
    # A designed line that ends in mid-air (no anchor, no junction) is a spur:
    # prune it. ``prune_anchor_m`` is the tolerance for "this end is an anchor".
    prune_dangling: bool = True
    prune_anchor_m: float = 3.0      # how close an end must be to "reach" an anchor
    prune_join_m: float = 3.0        # how close an end must be to join another span
    # Diagnostic only: a span whose midpoint is further than this from every
    # premise/anchor is reported (never silently removed).
    far_premise_m: float = 80.0
    # Aerial: a drop leg inside an aerial zone (or longer than this, when set)
    # is built aerial — drawn as an aerial drop, never excavated. 0 = length
    # rule off (zone layer only).
    aerial_max_leg_m: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Small geometry helpers (OGR only)
# ─────────────────────────────────────────────────────────────────────────────

def _ascii(msg: str) -> str:
    """Console-safe text (Windows consoles default to cp1252)."""
    for a, b in (("\u2192", "->"), ("\u00d7", "x"), ("\u2014", "-"),
                 ("\u21d2", "=>"), ("\u00b7", "-"), ("\u2264", "<="),
                 ("\u2265", ">="), ("\u00b1", "+/-")):
        msg = msg.replace(a, b)
    return msg.encode("ascii", "replace").decode("ascii")


def _xy(pt) -> Tuple[float, float]:
    return (float(pt[0]), float(pt[1]))


def _part_coords(geom) -> List[List[Tuple[float, float]]]:
    """Every line part of a (multi)line as a coord list."""
    out: List[List[Tuple[float, float]]] = []
    if geom is None or geom.IsEmpty():
        return out
    name = geom.GetGeometryName()
    if name in ("LINESTRING", "LINEARRING"):
        pts = [_xy(p) for p in geom.GetPoints()]
        if len(pts) >= 2:
            out.append(pts)
    elif name.startswith("MULTI") or name == "GEOMETRYCOLLECTION":
        for i in range(geom.GetGeometryCount()):
            out.extend(_part_coords(geom.GetGeometryRef(i)))
    elif name in ("POLYGON", "MULTIPOLYGON"):
        for i in range(geom.GetGeometryCount() or 1):
            g = geom.GetGeometryRef(i) if geom.GetGeometryCount() else geom
            if g is None:
                continue
            for j in range(g.GetGeometryCount()):
                ring = g.GetGeometryRef(j)
                pts = [_xy(p) for p in ring.GetPoints()]
                if len(pts) >= 2:
                    out.append(pts)
    return out


def _make_line(coords: Sequence[Tuple[float, float]]) -> ogr.Geometry:
    ls = ogr.Geometry(ogr.wkbLineString)
    for x, y in coords:
        ls.AddPoint_2D(float(x), float(y))
    return ls


def _make_multiline(parts: Iterable[Sequence[Tuple[float, float]]]) -> ogr.Geometry:
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    for coords in parts:
        if len(coords) >= 2:
            ml.AddGeometry(_make_line(coords))
    return ml


def _coords_len(coords: Sequence[Tuple[float, float]]) -> float:
    return sum(math.hypot(coords[i + 1][0] - coords[i][0],
                          coords[i + 1][1] - coords[i][1])
               for i in range(len(coords) - 1))


def _cum(coords: Sequence[Tuple[float, float]]) -> List[float]:
    out = [0.0]
    for i in range(len(coords) - 1):
        out.append(out[-1] + math.hypot(coords[i + 1][0] - coords[i][0],
                                        coords[i + 1][1] - coords[i][1]))
    return out


def _project(coords: Sequence[Tuple[float, float]], x: float, y: float
             ) -> Tuple[float, float, Tuple[float, float]]:
    """(distance, arc-length position, projected point) of (x, y) on a line."""
    cum = _cum(coords)
    best = (float("inf"), 0.0, (coords[0][0], coords[0][1]))
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 0:
            t = 0.0
        else:
            t = ((x - ax) * dx + (y - ay) * dy) / seg2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(x - qx, y - qy)
        if d < best[0]:
            best = (d, cum[i] + t * math.sqrt(seg2), (qx, qy))
    return best


def _substring(coords: Sequence[Tuple[float, float]], a0: float, a1: float
               ) -> List[Tuple[float, float]]:
    """The piece of a line between two arc-length positions."""
    if a1 < a0:
        a0, a1 = a1, a0
    if len(coords) < 2:
        return []
    cum = _cum(coords)
    total = cum[-1]
    a0 = max(0.0, min(total, a0))
    a1 = max(0.0, min(total, a1))

    def _at(a: float) -> Tuple[float, float]:
        for i in range(len(coords) - 1):
            if cum[i] <= a <= cum[i + 1]:
                seg = cum[i + 1] - cum[i]
                t = 0.0 if seg <= 0 else (a - cum[i]) / seg
                return (coords[i][0] + t * (coords[i + 1][0] - coords[i][0]),
                        coords[i][1] + t * (coords[i + 1][1] - coords[i][1]))
        return coords[-1]

    out = [_at(a0)]
    for i in range(1, len(coords) - 1):
        if a0 + 1e-9 < cum[i] < a1 - 1e-9:
            out.append(coords[i])
    out.append(_at(a1))
    return out


def _straighten(coords: Sequence[Tuple[float, float]], p: Params
                ) -> List[Tuple[float, float]]:
    """Sidewalk wobble → a few straight chords (TRENCH_DESIGN.md §4.6)."""
    if len(coords) <= 2:
        return list(coords)
    g = _make_line(coords)
    simplified = g.Simplify(p.simplify_tol_m)
    pts = [_xy(q) for q in simplified.GetPoints()] if simplified else list(coords)
    if len(pts) < 2:
        pts = list(coords)

    # drop shallow bends
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = out[-1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        v1 = (bx - ax, by - ay)
        v2 = (cx - bx, cy - by)
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
        if math.degrees(math.acos(cosang)) < p.bend_deg:
            continue
        out.append(pts[i])
    out.append(pts[-1])

    # thin vertices that sit closer than the minimum gap
    thin = [out[0]]
    for q in out[1:-1]:
        if math.hypot(q[0] - thin[-1][0], q[1] - thin[-1][1]) >= p.min_vertex_gap_m:
            thin.append(q)
    if math.hypot(out[-1][0] - thin[-1][0], out[-1][1] - thin[-1][1]) > 1e-9:
        thin.append(out[-1])
    return thin if len(thin) >= 2 else list(coords)


class GridIndex:
    """Uniform grid index over points (nearest-anchor / drill dedupe lookups)."""

    def __init__(self, cell: float = 100.0):
        self.cell = cell
        self.cells: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        self.items: List[Tuple[float, float]] = []

    def add(self, x: float, y: float, idx) -> None:
        self.items.append((x, y))
        self.cells[self._key(x, y)].append(idx)

    def _key(self, x: float, y: float) -> Tuple[int, int]:
        return (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))

    def near(self, x: float, y: float, radius: float) -> List[int]:
        c = self.cell
        r = int(math.ceil(max(radius, 1e-6) / c))
        kx, ky = self._key(x, y)
        out: List[int] = []
        for i in range(kx - r, kx + r + 1):
            for j in range(ky - r, ky + r + 1):
                out.extend(self.cells.get((i, j), ()))
        return out

    def any_within(self, x: float, y: float, radius: float) -> bool:
        """True when an indexed point lies within ``radius`` of (x, y)."""
        for i in self.near(x, y, radius):
            px, py = self.items[i]
            if math.hypot(px - x, py - y) <= radius:
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Layer IO
# ─────────────────────────────────────────────────────────────────────────────

def _srs(epsg: int) -> osr.SpatialReference:
    s = osr.SpatialReference()
    s.ImportFromEPSG(int(epsg))
    try:
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return s


def _value(feat, name: str):
    try:
        v = feat.GetField(name)
    except Exception:
        return None
    return v


def _read_points(path: str, target_epsg: int, bbox=None) -> List[dict]:
    ds = ogr.Open(path)
    if ds is None:
        raise SystemExit(f"cannot open {path}")
    out: List[dict] = []
    for li in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(li)
        if bbox is not None:
            minx, miny, maxx, maxy = bbox
            lyr.SetSpatialFilterRect(minx, miny, maxx, maxy)
        src = lyr.GetSpatialRef()
        tr = _transform(src, target_epsg) if src else None
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            p = g.Clone()
            if tr is not None:
                p.Transform(tr)
            if p.GetGeometryName() not in ("POINT", "MULTIPOINT"):
                p = p.Centroid() if not p.IsEmpty() else p
                if p is None or p.IsEmpty():
                    continue
            out.append({
                "x": float(p.GetX()), "y": float(p.GetY()),
                "fid": f.GetFID(),
                "PDP_ID": _value(f, "PDP_ID"),
                "POLYGON_ID": _value(f, "POLYGON_ID"),
                "ADDR_ID": _value(f, "ADDR_ID") or _value(f, "SRC_ID"),
                "HH": _value(f, "HH"),
                "fclass": _value(f, "fclass"),
            })
    ds = None
    return out


def _rect_in_layer_crs(bbox, src_epsg: int, layer_srs
                       ) -> Optional[Tuple[float, float, float, float]]:
    """bbox (in ``src_epsg``) expressed in the layer's own CRS.

    A spatial filter is applied in the *layer's* CRS, so a project-CRS box must
    be transformed first — otherwise a WGS84 road extract filters to nothing.
    """
    if layer_srs is None:
        return None
    dst = layer_srs.Clone()
    try:
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    src = _srs(src_epsg)
    try:
        if dst.IsSame(src):
            return tuple(bbox)
    except Exception:
        pass
    tr = osr.CoordinateTransformation(src, dst)
    minx, miny, maxx, maxy = bbox
    xs, ys = [], []
    for px, py in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(px, py)
        p.Transform(tr)
        xs.append(p.GetX())
        ys.append(p.GetY())
    return (min(xs), min(ys), max(xs), max(ys))


def _transform(src, target_epsg: int) -> Optional[osr.CoordinateTransformation]:
    if src is None:
        return None
    dst = _srs(target_epsg)
    try:
        if src.IsSame(dst):
            return None
    except Exception:
        pass
    src = src.Clone()
    try:
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    tr = osr.CoordinateTransformation(src, dst)
    return tr


def _read_polygons_geom(path: str, target_epsg: int) -> Optional[ogr.Geometry]:
    """All polygons of a layer merged into one geometry (mask tests)."""
    ds = ogr.Open(path)
    if ds is None:
        return None
    parts: List[ogr.Geometry] = []
    for li in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(li)
        tr = _transform(lyr.GetSpatialRef(), target_epsg)
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            g = g.Clone()
            if tr is not None:
                g.Transform(tr)
            parts.append(g)
    ds = None
    if not parts:
        return None
    col = ogr.Geometry(ogr.wkbGeometryCollection)
    for g in parts:
        col.AddGeometry(g)
    return col


def _read_road_parts(path: str, target_epsg: int, bbox=None
                     ) -> Tuple[List[Tuple[List[Tuple[float, float]], str]],
                                List[Tuple[List[Tuple[float, float]], str]]]:
    """(walkable, vehicular) road parts, each with its fclass."""
    ds = ogr.Open(path)
    if ds is None:
        raise SystemExit(f"cannot open {path}")
    walk: List[Tuple[List[Tuple[float, float]], str]] = []
    veh: List[Tuple[List[Tuple[float, float]], str]] = []
    for li in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(li)
        lyr_srs = lyr.GetSpatialRef()
        if bbox is not None:
            rect = _rect_in_layer_crs(bbox, target_epsg, lyr_srs)
            if rect is not None:
                lyr.SetSpatialFilterRect(*rect)
        tr = _transform(lyr_srs, target_epsg)
        defn = lyr.GetLayerDefn()
        i_cls = defn.GetFieldIndex("fclass")
        i_bridge = defn.GetFieldIndex("bridge")
        i_tunnel = defn.GetFieldIndex("tunnel")

        def _is_true(idx, feat) -> bool:
            if idx < 0:
                return False
            val = feat.GetField(idx)
            return str(val).strip().upper() in ("T", "TRUE", "1", "YES")

        for f in lyr:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            g = g.Clone()
            if tr is not None:
                g.Transform(tr)
            cls = str(f.GetField(i_cls) or "") if i_cls >= 0 else ""
            # A trench cannot be dug on a bridge deck or through a tunnel, so
            # those segments are never carriers (they stay crossable).
            deck = _is_true(i_bridge, f) or _is_true(i_tunnel, f)
            for coords in _part_coords(g):
                if deck:
                    veh.append((coords, cls))
                    continue
                if cls in WALKABLE_CLASSES:
                    walk.append((coords, cls))
                    # Streets that are walkable *and* drivable (residential,
                    # service, living_street, track, unclassified) are both a
                    # carrier and a road to be crossed — the crossing test's
                    # angle filter keeps the parallel overlap from becoming a
                    # drill, while genuine crossings are picked up.
                    if cls not in PURE_FOOTWAY_CLASSES:
                        veh.append((coords, cls))
                else:
                    veh.append((coords, cls))
    ds = None
    return walk, veh


def _write_lines(path: str, layer: str, rows: Sequence[dict], fields: Sequence[Tuple[str, int]],
                 epsg: int, geom_type=None) -> None:
    _write_layer(path, layer, geom_type or ogr.wkbLineString, rows, fields, epsg)


def _write_points(path: str, layer: str, rows: Sequence[dict],
                  fields: Sequence[Tuple[str, int]], epsg: int) -> None:
    _write_layer(path, layer, ogr.wkbPoint, rows, fields, epsg)


def _write_layer(path: str, layer: str, geom_type, rows: Sequence[dict],
                 fields: Sequence[Tuple[str, int]], epsg: int) -> None:
    for p in (path, path.replace(".gpkg", ".geojson")):
        drv_name = "GPKG" if p.lower().endswith(".gpkg") else "GeoJSON"
        drv = ogr.GetDriverByName(drv_name)
        if drv is None:
            continue
        if os.path.exists(p):
            drv.DeleteDataSource(p)
        ds = drv.CreateDataSource(p)
        lyr = ds.CreateLayer(layer, _srs(epsg), geom_type)
        for name, typ in fields:
            fld = ogr.FieldDefn(name, typ)
            if typ == ogr.OFTString:
                fld.SetWidth(48)
            lyr.CreateField(fld)
        defn = lyr.GetLayerDefn()
        lyr.StartTransaction()
        for row in rows:
            ft = ogr.Feature(defn)
            for name, _t in fields:
                ft.SetField(name, row.get(name))
            ft.SetGeometry(row["geom"].Clone())
            lyr.CreateFeature(ft)
        lyr.CommitTransaction()
        ds = None


# ─────────────────────────────────────────────────────────────────────────────
# Street graph
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StreetGraph:
    G: nx.Graph
    edge_coords: Dict[Tuple[str, str], List[Tuple[float, float]]]
    node_xy: Dict[str, Tuple[float, float]]
    index: GridIndex
    node_keys: List[str] = field(default_factory=list)

    def nearest_node(self, x: float, y: float, radius: float) -> Optional[str]:
        best, best_d = None, radius
        for i in self.index.near(x, y, radius):
            key = self.node_keys[i]
            nx_, ny_ = self.node_xy[key]
            d = math.hypot(nx_ - x, ny_ - y)
            if d <= best_d:
                best_d, best = d, key
        return best


def build_street_graph(walkable: Sequence[Tuple[List[Tuple[float, float]], str]],
                       params: Params) -> StreetGraph:
    """Node the walkable road network into a routable graph.

    OSM lines share exact vertices at intersections, so rounding coordinates is
    enough to connect the network; consecutive vertices become edges carrying
    their own geometry, so a route can be re-assembled without losing shape.
    """
    key_of: Dict[Tuple[int, int], str] = {}
    node_xy: Dict[str, Tuple[float, float]] = {}
    edge_coords: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    G = nx.Graph()
    tol = 0.25

    def key(x: float, y: float) -> str:
        k = (int(round(x / tol)), int(round(y / tol)))
        s = key_of.get(k)
        if s is None:
            s = "n%d" % len(key_of)
            key_of[k] = s
            node_xy[s] = (x, y)
            G.add_node(s)
        return s

    def factor(cls: str) -> float:
        return _class_factor(cls, params.street_avoid_scale)

    n_edges = 0
    for coords, cls in walkable:
        # density: no vertex spacing above 40 m so routes can bend realistically
        dense: List[Tuple[float, float]] = [coords[0]]
        for q in coords[1:]:
            prev = dense[-1]
            seg = math.hypot(q[0] - prev[0], q[1] - prev[1])
            if seg > 40.0:
                n = int(seg // 40.0)
                for i in range(1, n + 1):
                    t = i / (n + 1.0)
                    dense.append((prev[0] + t * (q[0] - prev[0]),
                                  prev[1] + t * (q[1] - prev[1])))
            dense.append(q)
        prev_key = None
        prev_pt = None
        f = factor(cls)
        for q in dense:
            k = key(q[0], q[1])
            if prev_key is not None and prev_key != k:
                w = math.hypot(q[0] - prev_pt[0], q[1] - prev_pt[1]) * f
                if w > 0:
                    ek = (prev_key, k) if prev_key < k else (k, prev_key)
                    if ek not in edge_coords:
                        G.add_edge(prev_key, k, weight=w, cls=cls)
                        edge_coords[ek] = [prev_pt, q]
                        n_edges += 1
            prev_key, prev_pt = k, q

    node_keys = list(node_xy.keys())
    idx = GridIndex(cell=50.0)
    for i, k in enumerate(node_keys):
        x, y = node_xy[k]
        idx.add(x, y, i)
    return StreetGraph(G=G, edge_coords=edge_coords, node_xy=node_xy,
                       index=idx, node_keys=node_keys)


def _class_factor(cls: str, scale: float = 1.0) -> float:
    """Routing weight multiplier for a road class.

    Preferred carriers (footway / path / service / cycleway) are ~1.0; every
    carriageway comes from :data:`STREET_CLASS_FACTOR`, ordered by road size.
    ``scale`` (``Params.street_avoid_scale``) dials the whole street penalty
    up or down without changing the order.
    """
    if cls in STREET_CLASS_FACTOR:
        return max(1.0, STREET_CLASS_FACTOR[cls] * max(0.0, scale))
    if cls in NON_CARRIER_CLASSES:
        return max(1.0, NON_CARRIER_FACTOR * max(0.0, scale))
    return CLASS_FACTOR.get(cls, 1.4)


def _path_edges(G: nx.Graph, path: Sequence[str]) -> List[Tuple[str, str]]:
    return [(path[i], path[i + 1]) if path[i] < path[i + 1] else (path[i + 1], path[i])
            for i in range(len(path) - 1)]


def _route(G: nx.Graph, src: str, dst: str) -> Optional[List[str]]:
    try:
        return nx.shortest_path(G, src, dst, weight="weight")
    except Exception:
        return None


def _edge_class_lengths(sg: StreetGraph, edge_keys) -> Dict[str, float]:
    """Metres per road class over a set of routed street edges.

    Reported after routing so the carrier mix (how much of the design runs on
    a footway vs down a carriageway) is visible in the run log — the point of
    the class factors is that the carriageway share stays small.
    """
    out: Dict[str, float] = defaultdict(float)
    for ek in edge_keys:
        if not sg.G.has_edge(*ek):
            continue
        cls = str(sg.G.edges[ek].get("cls") or "?")
        coords = sg.edge_coords.get(ek) or []
        out[cls] += _coords_len(coords) if len(coords) >= 2 else 0.0
    return dict(out)


def _orient(seg: Sequence[Tuple[float, float]], ref: Tuple[float, float]
            ) -> List[Tuple[float, float]]:
    """Return the segment pointing away from ``ref`` (its nearest endpoint)."""
    d0 = math.hypot(seg[0][0] - ref[0], seg[0][1] - ref[1])
    d1 = math.hypot(seg[-1][0] - ref[0], seg[-1][1] - ref[1])
    return list(seg) if d0 <= d1 else list(reversed(seg))


def runs_from_edges(edge_keys: Iterable[Tuple[str, str]],
                    sg: StreetGraph) -> Tuple[List[List[Tuple[float, float]]], List[str]]:
    """Assemble continuous runs; return (run coord lists, break node ids)."""
    sub = nx.Graph()
    for ek in edge_keys:
        a, b = ek
        if ek in sg.edge_coords:
            sub.add_edge(a, b, coords=sg.edge_coords[ek])

    runs: List[List[Tuple[float, float]]] = []
    used: Set[Tuple[str, str]] = set()
    breaks = [n for n in sub.nodes if sub.degree(n) != 2]
    if not breaks:
        breaks = list(sub.nodes)[:1]

    def ekey(u: str, v: str) -> Tuple[str, str]:
        return (u, v) if u < v else (v, u)

    def walk(start: str, first: str) -> None:
        coords = _orient(sub.edges[start, first]["coords"], sg.node_xy[start])
        used.add(ekey(start, first))
        prev, cur = start, first
        while True:
            if sub.degree(cur) != 2:
                break
            nxt = [w for w in sub.neighbors(cur) if ekey(cur, w) not in used]
            if len(nxt) != 1:
                break
            seg = _orient(sub.edges[cur, nxt[0]]["coords"], coords[-1])
            coords.extend(seg[1:])
            used.add(ekey(cur, nxt[0]))
            prev, cur = cur, nxt[0]
        if len(coords) >= 2:
            runs.append(coords)

    for b in breaks:
        for nbr in list(sub.neighbors(b)):
            if ekey(b, nbr) in used or not sub.has_edge(b, nbr):
                continue
            walk(b, nbr)
    for u, v in list(sub.edges):
        if ekey(u, v) in used:
            continue
        walk(u, v)
    return runs, breaks


# ─────────────────────────────────────────────────────────────────────────────
# Designer stages
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Run:
    coords: List[Tuple[float, float]]
    tier: str                      # Feeder | Distribution | Garden
    pdp: Optional[str] = None
    polygon: Optional[str] = None
    src: str = "design"
    infra: str = "New"


def _snap_anchor(sg: StreetGraph, x: float, y: float, tol: float
                 ) -> Optional[str]:
    return sg.nearest_node(x, y, tol)


def design_backbone(sg: StreetGraph, mfg: dict, pdps: Sequence[dict],
                    params: Params, log) -> Set[Tuple[str, str]]:
    edges: Set[Tuple[str, str]] = set()
    src = _snap_anchor(sg, mfg["x"], mfg["y"], params.pdp_search_m)
    if src is None:
        log("backbone: MFG could not be snapped to the street graph")
        return edges
    reached = 0
    for p in pdps:
        dst = _snap_anchor(sg, p["x"], p["y"], params.pdp_search_m)
        if dst is None:
            continue
        path = _route(sg.G, src, dst)
        if not path:
            continue
        edges.update(_path_edges(sg.G, path))
        reached += 1
    log(f"backbone: MFG → {reached}/{len(pdps)} PDP(s), {len(edges)} street edge(s)")
    return edges


def design_spine(sg: StreetGraph, pdps: Sequence[dict], houses: Sequence[dict],
                 params: Params, log) -> Dict[str, Set[Tuple[str, str]]]:
    """Per-PDP shortest-path tree to its houses (shared corridors merge)."""
    by_pdp: Dict[str, List[dict]] = defaultdict(list)
    for h in houses:
        pid = h.get("PDP_ID")
        if pid:
            by_pdp[str(pid)].append(h)

    per_pdp: Dict[str, Set[Tuple[str, str]]] = {}
    for p in pdps:
        pid = str(p.get("PDP_ID") or "")
        src = _snap_anchor(sg, p["x"], p["y"], params.pdp_search_m)
        if src is None:
            continue
        targets = []
        for h in by_pdp.get(pid, []):
            k = _snap_anchor(sg, h["x"], h["y"], params.house_search_m)
            if k is not None:
                targets.append(k)
        if not targets:
            continue
        try:
            _pred, dist = nx.dijkstra_predecessor_and_distance(sg.G, src, weight="weight")
        except Exception:
            continue
        edges: Set[Tuple[str, str]] = set()
        for t in targets:
            if t not in dist:
                continue
            node = t
            guard = 0
            while node != src and guard < 10000:
                guard += 1
                preds = _pred.get(node) or []
                if not preds:
                    break
                pnode = preds[0]
                edges.add((node, pnode) if node < pnode else (pnode, node))
                node = pnode
        per_pdp[pid] = edges
    paths = {pid: len(e) for pid, e in per_pdp.items()}
    log(f"spine: {sum(paths.values())} street edge(s) across {len(paths)} PDP(s)")
    return per_pdp


def design_garden_legs(network_parts: Sequence[List[Tuple[float, float]]],
                       houses: Sequence[dict], params: Params,
                       log) -> List[dict]:
    """Straight drop leg from every house to the nearest trench point."""
    legs: List[dict] = []
    skipped = 0
    for h in houses:
        best = (float("inf"), None, None)
        for coords in network_parts:
            d, a, q = _project(coords, h["x"], h["y"])
            if d < best[0]:
                best = (d, q, (coords, a))
        if best[1] is None or best[0] > params.house_search_m:
            skipped += 1
            continue
        q = best[1]
        if math.hypot(q[0] - h["x"], q[1] - h["y"]) < 0.5:
            continue
        length = math.hypot(q[0] - h["x"], q[1] - h["y"])
        legs.append({
            "coords": [q, (h["x"], h["y"])],
            "length": length,
            "type": "Garden" if length <= params.max_garden_m else "Open Cut",
            "house": h,
        })
    log(f"garden legs: {len(legs)} leg(s), {skipped} house(s) out of reach")
    return legs


def detect_drills(vehicular: Sequence[Tuple[List[Tuple[float, float]], str]],
                  network: List[Run], params: Params, log) -> List[dict]:
    """Crossings of a designed line over a carriageway → perpendicular drills."""
    veh_geoms = [( _make_line(c), c, cls) for c, cls in vehicular if len(c) >= 2]
    drills: List[dict] = []
    angle_lim = math.cos(math.radians(params.crossing_angle_deg))
    for run in network:
        # coarse pre-filter: only crossings near this run
        line = _make_line(run.coords)
        # envelopes are (minX, maxX, minY, maxY)
        rminx, rmaxx, rminy, rmaxy = line.GetEnvelope()
        for vg, vcoords, cls in veh_geoms:
            vminx, vmaxx, vminy, vmaxy = vg.GetEnvelope()
            if (vmaxx < rminx or vminx > rmaxx
                    or vmaxy < rminy or vminy > rmaxy):
                continue
            inter = line.Intersection(vg)
            if inter is None or inter.IsEmpty():
                continue
            for pt in _part_points(inter):
                d, arc, q = _project(run.coords, pt[0], pt[1])
                if d > 1.0:
                    continue
                # angle between the run and the road at that point
                if _is_parallel(run.coords, arc, vcoords, q, angle_lim):
                    continue
                # The bore CONTINUES the trench alignment under the road (it is
                # not bored along the road). So the drill axis is the trench
                # tangent at the crossing and its length is the road width
                # measured along that tangent — width / sin(angle). This keeps
                # both pits exactly on the trench, which is what makes the
                # crossing a real node-to-node span downstream.
                t = _lerp_dir(run.coords, arc)
                rdir = _lerp_dir(vcoords, _project(vcoords, q[0], q[1])[1])
                sin_t = abs(t[0] * rdir[1] - t[1] * rdir[0])
                sin_t = max(sin_t, 0.35)          # never longer than ~3x width
                road_w = VEHICULAR_WIDTH_M.get(cls, 6.0)
                bore = road_w / sin_t + params.drill_extra_m
                half = bore / 2.0
                e1 = (q[0] - t[0] * half, q[1] - t[1] * half)
                e2 = (q[0] + t[0] * half, q[1] + t[1] * half)
                drills.append({
                    "coords": [e1, e2], "cls": cls, "width": bore,
                    "road_width": road_w, "arc": arc, "run": id(run),
                })
    # consolidation: nearest-first, merge close junctions, suppress duplicates
    drills.sort(key=lambda d: (d["arc"], d["cls"]))
    kept: List[dict] = []
    merged = deduped = 0
    for d in drills:
        cx = (d["coords"][0][0] + d["coords"][1][0]) / 2.0
        cy = (d["coords"][0][1] + d["coords"][1][1]) / 2.0
        clash = None
        for k in kept:
            if k["run"] != d["run"]:
                continue
            kx = (k["coords"][0][0] + k["coords"][1][0]) / 2.0
            ky = (k["coords"][0][1] + k["coords"][1][1]) / 2.0
            dist = math.hypot(kx - cx, ky - cy)
            if dist <= params.crossing_dedupe_m:
                clash = ("dedupe", dist)
                break
            if dist <= params.crossing_merge_m:
                clash = ("merge", dist)
        if clash and clash[0] == "dedupe":
            deduped += 1
            continue
        if clash and clash[0] == "merge":
            merged += 1
        kept.append(d)
    log(f"drills: {len(kept)} kept from {len(drills)} crossing(s) "
        f"({merged} served by a nearby crossing, {deduped} duplicate(s) suppressed)")
    return kept


def _part_points(geom) -> List[Tuple[float, float]]:
    name = geom.GetGeometryName()
    if name in ("POINT", "POINT25D"):
        return [_xy(geom.GetPoint(0))]
    if name == "MULTIPOINT" or name == "GEOMETRYCOLLECTION":
        out = []
        for i in range(geom.GetGeometryCount()):
            out.extend(_part_points(geom.GetGeometryRef(i)))
        return out
    if "LINE" in name:
        return [_xy(p) for p in geom.GetPoints()]
    return []


def _lerp_dir(coords: Sequence[Tuple[float, float]], arc: float) -> Tuple[float, float]:
    cum = _cum(coords)
    for i in range(len(coords) - 1):
        if cum[i] <= arc <= cum[i + 1]:
            dx = coords[i + 1][0] - coords[i][0]
            dy = coords[i + 1][1] - coords[i][1]
            l = math.hypot(dx, dy) or 1.0
            return (dx / l, dy / l)
    return (1.0, 0.0)


def _is_parallel(run_coords, arc, road_coords, q, angle_lim) -> bool:
    a = _lerp_dir(run_coords, arc)
    d, rarc, _ = _project(road_coords, q[0], q[1])
    b = _lerp_dir(road_coords, rarc)
    return abs(a[0] * b[0] + a[1] * b[1]) >= angle_lim


def place_nodes(network: List[Run], drills: Sequence[dict],
                pdps: Sequence[dict], params: Params,
                junction_points: Sequence[Tuple[float, float]] = (),
                log=None) -> List[dict]:
    """Structural nodes in priority order (TRENCH_DESIGN.md §4.7)."""
    nodes: List[dict] = []

    def add(x, y, ntype, priority, run=None, arc=None, ref=None):
        for n in nodes:
            # HDD pits reserve the widest keep-out (they are the drill openings),
            # every other structure keeps the global minimum separation.
            sep = (params.hdd_pit_keepout_m if n["NODE_TYPE"] == "HDD_PIT"
                   else params.min_node_sep_m)
            if math.hypot(n["x"] - x, n["y"] - y) < sep:
                return n
        n = {"x": x, "y": y, "NODE_TYPE": ntype, "PRIORITY": priority,
             "run": id(run) if run is not None else None, "arc": arc, "ref": ref}
        nodes.append(n)
        return n

    # 1 — HDD entry / exit pits, widest keep-out (placed first). Both ends of
    #     one drill are one PAIR: they are ~road-width apart (below the global
    #     separation), so they are inserted without deduping each other.
    for di, d in enumerate(drills):
        for pt in d["coords"]:
            n = None
            for other in nodes:
                # Only pits of a DIFFERENT drill may absorb each other: the two
                # ends of one bore are ~road-width apart, which is below the
                # dedupe radius on narrow roads (service 7 m, track 6 m) —
                # collapsing the pair would delete the crossing span.
                if (other["NODE_TYPE"] == "HDD_PIT"
                        and other.get("drill") != di
                        and math.hypot(other["x"] - pt[0],
                                       other["y"] - pt[1]) <= params.crossing_dedupe_m / 3.0):
                    n = other
                    break
            if n is None:
                n = {"x": pt[0], "y": pt[1], "NODE_TYPE": "HDD_PIT",
                     "PRIORITY": 1, "run": d.get("run"), "arc": d.get("arc"),
                     "ref": None, "drill": di}
                nodes.append(n)


    # 2 — branches
    for pt in junction_points:
        add(pt[0], pt[1], "JUNCTION", 2)

    # 3 — splitters
    for p in pdps:
        add(p["x"], p["y"], "PDP", 3, ref=str(p.get("PDP_ID") or ""))

    # 4 — sharp bends
    for run in network:
        for i in range(1, len(run.coords) - 1):
            ax, ay = run.coords[i - 1]
            bx, by = run.coords[i]
            cx, cy = run.coords[i + 1]
            v1 = (bx - ax, by - ay)
            v2 = (cx - bx, cy - by)
            l1 = math.hypot(*v1) or 1.0
            l2 = math.hypot(*v2) or 1.0
            cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
            if math.degrees(math.acos(cosang)) > 45.0:
                add(bx, by, "BEND", 4, run)

    # 5 — interval pull chambers (only across real gaps)
    for run in network:
        gap = (params.pull_backbone_m if run.tier == "Feeder"
               else params.pull_dist_m)
        cum = _cum(run.coords)
        pos = gap
        while pos < cum[-1] - params.min_node_sep_m:
            gap_ok = True
            for n in nodes:
                d, a, q = _project(run.coords, n["x"], n["y"])
                if d <= 2.0 and abs(a - pos) < gap:
                    gap_ok = False
                    break
            if gap_ok:
                for i in range(len(cum) - 1):
                    if cum[i] <= pos <= cum[i + 1]:
                        seg = cum[i + 1] - cum[i]
                        t = 0.0 if seg <= 0 else (pos - cum[i]) / seg
                        add(run.coords[i][0] + t * (run.coords[i + 1][0] - run.coords[i][0]),
                            run.coords[i][1] + t * (run.coords[i + 1][1] - run.coords[i][1]),
                            "PULL", 5, run)
                        break
            pos += gap

    if log:
        counts: Dict[str, int] = defaultdict(int)
        for n in nodes:
            counts[n["NODE_TYPE"]] += 1
        log("nodes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return nodes


def split_spans(run: Run, nodes: Sequence[dict], params: Params,
                type_map: Optional[Sequence[Tuple[float, float, str]]] = None
                ) -> List[dict]:
    """Cut a run at its structural nodes (one chamber-to-chamber sequence each)."""
    cum = _cum(run.coords)
    total = cum[-1]
    anchors: List[Tuple[float, Optional[dict]]] = []
    for n in nodes:
        d, a, q = _project(run.coords, n["x"], n["y"])
        if d <= 3.0:
            anchors.append((a, n))
    if type_map:
        # Every bore boundary is an anchor in its own right: a pit can be
        # absorbed by a neighbouring structure (or by another drill's pit) and
        # drop out of the node list, but the crossing itself must still be its
        # own span, otherwise the HDD length is attributed to open cut.
        for ta, tb, _tt in type_map:
            anchors.append((ta, None))
            anchors.append((tb, None))
    # nodes win ties against bare arc boundaries at the same position
    anchors.sort(key=lambda t: (t[0], t[1] is None))
    kept: List[Tuple[float, Optional[dict]]] = []
    for a, n in anchors:
        # A node (or bore end) sitting on the run's end IS that end.
        if a <= params.min_node_sep_m / 4.0:
            a = 0.0
        elif total - a <= params.min_node_sep_m / 4.0:
            a = total
        if a <= 0.0 or a >= total:
            continue
        # Dedupe only against REAL anchors: the synthetic run ends must not
        # swallow a crossing that starts a few metres into the run.
        if kept and abs(kept[-1][0] - a) < params.min_node_sep_m / 2.0:
            if n is not None and kept[-1][1] is None:
                kept[-1] = (kept[-1][0], n)
            continue
        kept.append((a, n))
    seq: List[Tuple[float, Optional[dict]]] = ([(0.0, None)] + kept
                                               + [(total, None)])

    spans: List[dict] = []
    for i in range(len(seq) - 1):
        a0, n0 = seq[i]
        a1, n1 = seq[i + 1]
        coords = _substring(run.coords, a0, a1)
        if len(coords) < 2 or _coords_len(coords) < 0.5:
            continue
        ttype = run.tier_type if hasattr(run, "tier_type") else "Open Cut"
        if type_map:
            # A span is HDD when its drilled arc covers it — matched by
            # OVERLAP, not by an exact boundary fit: node projection can shift
            # a boundary by a metre on an oblique crossing.
            for ta, tb, tt in type_map:
                if a0 >= ta - 1e-6 and a1 <= tb + 1e-6:
                    ttype = tt
                    break
                covered = min(a1, tb) - max(a0, ta)
                # ``max`` (not ``min``): a span only becomes HDD when it *is*
                # the bore. Using the smaller of the two lengths marked a
                # 200 m open-cut span as HDD merely because the 9 m bore sat
                # inside it.
                if covered > 0 and covered >= 0.5 * max(a1 - a0, tb - ta):
                    ttype = tt
                    break
        spans.append({
            "coords": coords, "start": n0, "end": n1, "arc0": a0, "arc1": a1,
            "length": _coords_len(coords), "type": ttype, "tier": run.tier,
            "pdp": run.pdp, "polygon": run.polygon, "src": run.src,
            "infra": run.infra,
            "run_id": getattr(run, "run_id", "RUN-00000"),
        })
    return spans


# ─────────────────────────────────────────────────────────────────────────────
# Main design
# ─────────────────────────────────────────────────────────────────────────────

FIELD_LINE = (
    ("TRENCH_ID", ogr.OFTString), ("RUN_ID", ogr.OFTString),
    ("SPAN_ID", ogr.OFTString),
    ("START_NODE", ogr.OFTString), ("END_NODE", ogr.OFTString),
    ("SPAN_INDEX", ogr.OFTInteger), ("SPAN_COUNT", ogr.OFTInteger),
    ("length_m", ogr.OFTReal), ("TRENCH_TYPE", ogr.OFTString),
    ("TRENCH_TIER", ogr.OFTString), ("INFRA_STATUS", ogr.OFTString),
    ("VERIFY_STATUS", ogr.OFTString), ("SURFACE", ogr.OFTString),
    ("REINSTATE", ogr.OFTString), ("PDP_ID", ogr.OFTString),
    ("POLYGON_ID", ogr.OFTString), ("SRC", ogr.OFTString),
    # AERIAL: the span runs through an aerial zone (restricted land). The span
    # stays an excavated trench span here — the flag tells the planner/BOQ the
    # corridor is restricted, so a re-route or an aerial span is expected.
    ("AERIAL", ogr.OFTInteger), ("AERIAL_REASON", ogr.OFTString),
)
FIELD_AERIAL = (
    ("DROP_ID", ogr.OFTString), ("POLYGON_ID", ogr.OFTString),
    ("TRENCH_TIER", ogr.OFTString), ("TRENCH_TYPE", ogr.OFTString),
    ("length_m", ogr.OFTReal), ("AERIAL_REASON", ogr.OFTString),
    ("INFRA_STATUS", ogr.OFTString),
)
FIELD_NODE = (
    ("NODE_ID", ogr.OFTString), ("NODE_TYPE", ogr.OFTString),
    ("PRIORITY", ogr.OFTInteger), ("PDP_ID", ogr.OFTString),
    ("X", ogr.OFTReal), ("Y", ogr.OFTReal),
)
FIELD_DRILL = (
    ("DRILL_ID", ogr.OFTString), ("ROAD_CLASS", ogr.OFTString),
    ("WIDTH_M", ogr.OFTReal), ("TRENCH_TYPE", ogr.OFTString),
    ("INFRA_STATUS", ogr.OFTString),
)


def _surface_for(tier: str, ttype: str) -> Tuple[str, str]:
    if ttype == "HDD":
        return ("Asphalt", "Full")
    if ttype == "Garden":
        return ("Garden", "Seed")
    return ("Footway", "Pavement")


def _zone_polygons(zone_geom) -> List[Tuple[ogr.Geometry, Tuple[float, float, float, float]]]:
    """Flatten a zone geometry into (polygon, envelope) pairs.

    Zone tests must go against single polygons: ``Contains`` on a
    GeometryCollection / MultiPolygon is unreliable (it reports points *outside*
    the parts as contained), which would flag an entire design as "aerial".
    Invalid polygons are repaired where GDAL can.
    """
    out: List[Tuple[ogr.Geometry, Tuple[float, float, float, float]]] = []

    def walk(g) -> None:
        if g is None or g.IsEmpty():
            return
        if g.GetGeometryName() == "POLYGON":
            if not g.IsValid():
                try:
                    g = g.MakeValid()
                except Exception:
                    try:
                        g = g.Buffer(0)
                    except Exception:
                        return
                if g is None or g.IsEmpty():
                    return
                for i in range(g.GetGeometryCount() or 1):
                    walk(g.GetGeometryRef(i) if g.GetGeometryCount() else g)
                return
            if g.GetArea() > 0:
                out.append((g.Clone(), g.GetEnvelope()))
            return
        for i in range(g.GetGeometryCount()):
            walk(g.GetGeometryRef(i))

    walk(zone_geom)
    return out


def _in_zone(zone_polys, x: float, y: float) -> bool:
    """Point-in-zone test with a per-polygon envelope rejection."""
    for g, env in zone_polys or ():
        if x < env[0] or x > env[1] or y < env[2] or y > env[3]:
            continue
        if g.Contains(ogr.CreateGeometryFromWkt(f"POINT({x} {y})")):
            return True
    return False


def _leg_in_zone(zone_polys, leg: dict, samples: int = 6) -> bool:
    """True when any sampled point of the leg falls inside an aerial zone.

    Sampling the whole leg (not just its midpoint) matters: an aerial zone is
    often a park the leg crosses, so a midpoint-only test misses it.
    """
    if not zone_polys:
        return False
    (ax, ay), (bx, by) = leg["coords"][0], leg["coords"][-1]
    for i in range(samples + 1):
        t = i / float(samples)
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        if _in_zone(zone_polys, x, y):
            return True
    return False


def _split_drop_legs(legs: Sequence[dict], zone_polys, params: Params, log
                     ) -> Tuple[List[dict], List[dict]]:
    """Split house drop legs into (trenched, aerial).

    Aerial rules, in order:

    1. ``zone``   — the leg lies inside an aerial zone: underground is not
       permitted there, so the drop is built aerial.
    2. ``length`` — the leg is longer than ``aerial_max_leg_m`` (when set):
       the spur can neither be a garden trench nor economically open-cut.

    An aerial leg is **not** a trench: it is published on the ``Aerial_Drops``
    layer and excluded from every excavated trench output and length.
    """
    trenched: List[dict] = []
    aerial: List[dict] = []
    for leg in legs:
        item = dict(leg)
        if _leg_in_zone(zone_polys, item):
            item["type"] = "Aerial"
            item["aerial_reason"] = "zone"
            aerial.append(item)
        elif params.aerial_max_leg_m and item["length"] > params.aerial_max_leg_m:
            item["type"] = "Aerial"
            item["aerial_reason"] = "length"
            aerial.append(item)
        else:
            trenched.append(item)
    return trenched, aerial


def _aerial_flag(zone_polys, coords: Sequence[Tuple[float, float]]) -> str:
    """``"zone"`` when the span's own line samples inside an aerial zone."""
    if not zone_polys or len(coords) < 2:
        return ""
    step = max(1, len(coords) // 8)
    for pt in list(coords)[::step] + [coords[-1]]:
        if _in_zone(zone_polys, pt[0], pt[1]):
            return "zone"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Anchored-network checks: "is every trench going somewhere?"
# ─────────────────────────────────────────────────────────────────────────────

class _SegGrid:
    """Uniform grid over line segments — nearest-segment queries in O(1)."""

    def __init__(self, cell: float = 25.0) -> None:
        self.cell = cell
        self.cells: Dict[Tuple[int, int], List[Tuple[int, Tuple[float, float],
                                                           Tuple[float, float]]]] = defaultdict(list)

    def add(self, idx: int, a: Tuple[float, float], b: Tuple[float, float]) -> None:
        c = self.cell
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for cx in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for cy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                self.cells[(cx, cy)].append((idx, a, b))

    def nearest(self, x: float, y: float, max_d: float,
                exclude: Optional[int] = None) -> float:
        """Distance to the closest indexed segment (``max_d`` when none is near).

        ``exclude`` skips one span's own segments — the question "does this end
        touch *another* span" is meaningless if the span counts itself (its own
        endpoint is always 0 m from its own geometry).
        """
        c = self.cell
        best = max_d
        cx, cy = int(math.floor(x / c)), int(math.floor(y / c))
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for i, a, b in self.cells.get((gx, gy), ()):
                    if exclude is not None and i == exclude:
                        continue
                    d = _point_seg_dist(x, y, a, b)
                    if d < best:
                        best = d
        return best

    def nearest_index(self, x: float, y: float, max_d: float,
                      exclude: Optional[int] = None) -> Optional[int]:
        """Index of the span whose geometry passes closest to (x, y)."""
        c = self.cell
        best, best_i = max_d, None
        cx, cy = int(math.floor(x / c)), int(math.floor(y / c))
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for i, a, b in self.cells.get((gx, gy), ()):
                    if exclude is not None and i == exclude:
                        continue
                    d = _point_seg_dist(x, y, a, b)
                    if d < best:
                        best, best_i = d, i
        return best_i


def _point_seg_dist(x: float, y: float, a: Tuple[float, float],
                    b: Tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return math.hypot(x - a[0], y - a[1])
    t = ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (a[0] + t * dx), y - (a[1] + t * dy))


def _span_coords(sp: dict) -> List[Tuple[float, float]]:
    """Coordinate list of a span row.

    Span rows carry their geometry as a MULTILINESTRING ``geom`` (what the
    trench layers publish), not a bare coord list.
    """
    for part in _part_coords(sp.get("geom")):
        if len(part) >= 2:
            return list(part)
    return []


def _anchor_points(pdps, houses, mfgs) -> List[Tuple[float, float]]:
    """Every point a trench is allowed to end at (premise, PDP, MFG)."""
    out: List[Tuple[float, float]] = []
    for group in (houses, pdps, mfgs):
        out.extend((float(p["x"]), float(p["y"])) for p in group)
    return out


def prune_unanchored_spans(span_rows: List[dict], anchors_pts: Sequence[Tuple[float, float]],
                           params: Params, log) -> Tuple[List[dict], List[dict]]:
    """Drop span groups that reach no anchor — a trench to nowhere.

    A designed sub-network is only worth digging when it reaches something: a
    house/premise, a PDP or a MFG, or joins a group that does. A group of spans
    that touches none of them is a stray assembly over a disconnected street
    fragment (the classic "trench extending where it isn't needed").

    Connectivity is *geometry*, not shared endpoints: a service leg that starts
    mid-span on another run joins that run's group, and a house drop anchors
    every span it hangs off. (Getting this wrong — endpoint keys only — prunes
    distribution runs that are in fact feeding houses: measured 18 spans over
    11 live drops on the Berlin project.)

    Only *whole* groups are dropped: a single span is never cut out of a live
    chain, so an anchored network cannot be damaged. The removals are written
    to ``Pruned_Trenches`` so every drop stays auditable.
    """
    if not params.prune_dangling or not span_rows:
        return list(span_rows), []
    tol = 1.0
    join_tol = max(1.0, params.prune_join_m)

    def key(pt) -> Tuple[int, int]:
        return (int(round(pt[0] / tol)), int(round(pt[1] / tol)))

    parent: Dict[int, int] = {i: i for i in range(len(span_rows))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. spans sharing an endpoint are one line
    ends: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, sp in enumerate(span_rows):
        c = _span_coords(sp)
        if len(c) < 2:
            continue
        ends[key(c[0])].append(i)
        ends[key(c[-1])].append(i)
    for _k, idxs in ends.items():
        for other in idxs[1:]:
            union(idxs[0], other)

    # 2. a span whose end lands on another span's geometry joins it (T-join)
    grid = _SegGrid(cell=25.0)
    for i, sp in enumerate(span_rows):
        c = _span_coords(sp)
        for j in range(len(c) - 1):
            grid.add(i, c[j], c[j + 1])
    for i, sp in enumerate(span_rows):
        c = _span_coords(sp)
        if len(c) < 2:
            continue
        for pt in (c[0], c[-1]):
            j = grid.nearest_index(pt[0], pt[1], join_tol, exclude=i)
            if j is not None:
                union(i, j)

    anchor_grid = GridIndex(cell=max(20.0, 2 * params.prune_anchor_m))
    for i, (ax, ay) in enumerate(anchors_pts):
        anchor_grid.add(ax, ay, i)

    # A span whose end lands on an anchor, or whose SRC is a house drop, anchors
    # its whole group.
    anchored: Set[int] = set()
    for i, sp in enumerate(span_rows):
        if sp.get("SRC") == "house-drop":
            anchored.add(find(i))
            continue
        c = _span_coords(sp)
        if len(c) < 2:
            continue
        for pt in (c[0], c[-1]):
            if anchor_grid.any_within(pt[0], pt[1], params.prune_anchor_m):
                anchored.add(find(i))
                break

    keep, pruned = [], []
    pruned_groups: Set[int] = set()
    for i, sp in enumerate(span_rows):
        root = find(i)
        if root in anchored:
            keep.append(sp)
        else:
            pruned.append(sp)
            pruned_groups.add(root)
    if pruned:
        log("pruned %d span(s) (%.1f m) in %d unanchored group(s) - trench to nowhere"
            % (len(pruned), sum(s["length_m"] for s in pruned), len(pruned_groups)))
    return keep, pruned


def dangling_ends(span_rows: Sequence[dict], anchors_pts: Sequence[Tuple[float, float]],
                  params: Params, tol: float = 3.0) -> List[dict]:
    """Designed ends that are neither an anchor nor a junction (diagnostic).

    Reported, never deleted: a single end in mid-air is often the last house of
    a service leg, while a *chain* of them is a real spur. Counting them per
    run is what tells us whether the design is reaching where it should.
    """
    if not span_rows:
        return []
    anchor_grid = GridIndex(cell=max(20.0, 2 * params.prune_anchor_m))
    for i, (ax, ay) in enumerate(anchors_pts):
        anchor_grid.add(ax, ay, i)
    grid = _SegGrid(cell=25.0)
    for i, sp in enumerate(span_rows):
        c = _span_coords(sp)
        for j in range(len(c) - 1):
            grid.add(i, c[j], c[j + 1])
    out: List[dict] = []
    for i, sp in enumerate(span_rows):
        # a house drop is anchored by definition (its far end is the premise)
        if sp.get("SRC") == "house-drop":
            continue
        c = _span_coords(sp)
        if len(c) < 2:
            continue
        for pt, which in ((c[0], "start"), (c[-1], "end")):
            if anchor_grid.any_within(pt[0], pt[1], params.prune_anchor_m):
                continue
            # a T-join onto *another* span counts as connected
            if grid.nearest(pt[0], pt[1], tol, exclude=i) < tol:
                continue
            out.append({"TRENCH_ID": sp["TRENCH_ID"], "RUN_ID": sp["RUN_ID"],
                        "which": which, "x": round(pt[0], 1), "y": round(pt[1], 1)})
    return out


def design(cfg: dict) -> dict:
    """Run the designer. ``cfg`` keys mirror the CLI arguments."""
    t0 = time.time()
    params = Params(**{k: v for k, v in cfg.items() if k in Params.__annotations__})
    out_dir = cfg["out"]
    os.makedirs(out_dir, exist_ok=True)
    messages: List[str] = []

    def log(msg: str) -> None:
        messages.append(msg)
        print("  [design] " + _ascii(msg), flush=True)

    # ── read the plan ────────────────────────────────────────────────────
    pdps = _read_points(cfg["pdps"], params.target_epsg)
    houses = _read_points(cfg["objects"], params.target_epsg)
    mfgs = _read_points(cfg["mfg"], params.target_epsg)
    polygons = _read_points(cfg["polygons"], params.target_epsg)
    if not mfgs:
        raise SystemExit("no MFG point found")
    if not pdps:
        raise SystemExit("no PDP found")
    log(f"plan: {len(mfgs)} MFG, {len(pdps)} PDP, {len(houses)} house/premise, "
        f"{len(polygons)} polygon")

    xs = [p["x"] for p in pdps] + [h["x"] for h in houses] + [m["x"] for m in mfgs]
    ys = [p["y"] for p in pdps] + [h["y"] for h in houses] + [m["y"] for m in mfgs]
    bbox = (min(xs) - params.road_bbox_buffer_m, min(ys) - params.road_bbox_buffer_m,
            max(xs) + params.road_bbox_buffer_m, max(ys) + params.road_bbox_buffer_m)
    log("area: %.0f × %.0f m" % (bbox[2] - bbox[0], bbox[3] - bbox[1]))

    walkable, vehicular = _read_road_parts(cfg["roads"], params.target_epsg, bbox)
    log(f"roads: {len(walkable)} walkable part(s), {len(vehicular)} carriageway part(s)")

    # ── street graph + routing ───────────────────────────────────────────
    sg = build_street_graph(walkable, params)
    log(f"street graph: {sg.G.number_of_nodes()} node(s), {sg.G.number_of_edges()} edge(s)")

    backbone_edges = design_backbone(sg, mfgs[0], pdps, params, log)
    spine_edges = design_spine(sg, pdps, houses, params, log)

    # ── assemble runs (Feeder wins over Distribution where they overlap) ──
    feeder_keys = set(backbone_edges)
    dist_keys = set().union(*spine_edges.values()) if spine_edges else set()
    dist_only = dist_keys - feeder_keys

    runs: List[Run] = []
    pdp_by_key = {}
    for p in pdps:
        k = _snap_anchor(sg, p["x"], p["y"], params.pdp_search_m)
        if k:
            pdp_by_key[k] = p
    for keys, tier, pid in ((feeder_keys, "Feeder", None), (dist_only, "Distribution", None)):
        if not keys:
            continue
        raw_runs, breaks = runs_from_edges(keys, sg)
        for coords in raw_runs:
            straight = _straighten(coords, params)
            runs.append(Run(coords=straight, tier=tier, pdp=pid,
                            polygon=None, src="street-graph"))
    carrier_mix = _edge_class_lengths(sg, feeder_keys | dist_keys)
    log("carriers: " + ", ".join(
        "%s %.0f m" % (k, v)
        for k, v in sorted(carrier_mix.items(), key=lambda kv: -kv[1])[:6]))
    carriage_m = sum(v for k, v in carrier_mix.items()
                     if k in FALLBACK_CARRIER_CLASSES)
    if carrier_mix:
        log("carriageway carrier: %.0f m of %.0f m (%.1f%%) - footway/service first"
            % (carriage_m, sum(carrier_mix.values()),
               100.0 * carriage_m / max(1.0, sum(carrier_mix.values()))))
    log(f"runs: {len(runs)} assembled "
        f"({sum(1 for r in runs if r.tier == 'Feeder')} feeder, "
        f"{sum(1 for r in runs if r.tier == 'Distribution')} distribution)")

    # attach the pre-straighten network for garden-leg snapping
    network_parts = [r.coords for r in runs]

    # ── garden legs + aerial classification ──────────────────────────────
    aerial_polys: List[Tuple[ogr.Geometry, Tuple[float, float, float, float]]] = []
    if cfg.get("aerial"):
        aerial_zone = _read_polygons_geom(cfg["aerial"], params.target_epsg)
        aerial_polys = _zone_polygons(aerial_zone)
        if aerial_polys:
            log("aerial zones: %d polygon(s), %.1f ha - no excavation inside"
                % (len(aerial_polys),
                   sum(g.GetArea() for g, _e in aerial_polys) / 10000.0))
    legs = design_garden_legs(network_parts, houses, params, log)
    legs, aerial_legs = _split_drop_legs(legs, aerial_polys, params, log)
    if aerial_legs:
        by_reason: Dict[str, int] = defaultdict(int)
        for leg in aerial_legs:
            by_reason[leg["aerial_reason"]] += 1
        log("aerial drops: %d leg(s) (%.1f m) built aerial - %s"
            % (len(aerial_legs), sum(leg["length"] for leg in aerial_legs),
               ", ".join(f"{k}: {v}" for k, v in sorted(by_reason.items()))))
    for leg in legs:
        r = Run(coords=leg["coords"], tier="Garden", pdp=None,
                polygon=(leg["house"].get("POLYGON_ID") or None),
                src="house-drop")
        r.tier_type = leg["type"]          # type decided by leg length
        runs.append(r)
        # NOTE: legs are NOT added to ``network_parts`` — a later house must
        # snap to the designed mains, never to another house's drop leg.

    # ── drills over carriageways ─────────────────────────────────────────
    drills = detect_drills(vehicular, runs, params, log)

    # ── per-run HDD arcs (the drill replaces the crossing length) ─────────
    drill_arcs: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for d in drills:
        arc = d.get("arc")
        if arc is None:
            continue
        half = d["width"] / 2.0
        drill_arcs[d["run"]].append((arc - half, arc + half))

    for r in runs:
        r.tier_type = getattr(r, "tier_type", "Open Cut")
        spans_for_type: List[Tuple[float, float, str]] = []
        for a0, a1 in drill_arcs.get(id(r), []):
            spans_for_type.append((max(0.0, a0), min(_coords_len(r.coords), a1), "HDD"))
        r.type_map = spans_for_type

    # ── structural nodes + spans ─────────────────────────────────────────
    # Stable, readable run ids (an object address is not reproducible between
    # runs, and two addresses can differ by a multiple of the modulo below).
    for i, r in enumerate(runs):
        r.run_id = "RUN-%05d" % (i + 1)
    junction_points = _junction_points(runs, params.junction_deg)
    nodes = place_nodes(runs, drills, pdps, params, junction_points, log)

    spans: List[dict] = []
    for r in runs:
        for sp in split_spans(r, nodes, params, getattr(r, "type_map", None)):
            spans.append(sp)

    # node ids
    for i, n in enumerate(nodes):
        n["NODE_ID"] = "TN-%05d" % (i + 1)
    span_rows: List[dict] = []
    for i, sp in enumerate(spans):
        sn = sp["start"]["NODE_ID"] if sp["start"] else ""
        en = sp["end"]["NODE_ID"] if sp["end"] else ""
        surf, reinstate = _surface_for(sp["tier"], sp["type"])
        aerial_reason = _aerial_flag(aerial_polys, sp["coords"])
        span_rows.append({
            "TRENCH_ID": "TR-%06d" % (i + 1),
            "RUN_ID": sp["run_id"],
            "SPAN_ID": "%s-%s" % (sn or "START", en or "END"),
            "START_NODE": sn, "END_NODE": en,
            "SPAN_INDEX": 1, "SPAN_COUNT": 1,
            "length_m": round(sp["length"], 2),
            "TRENCH_TYPE": sp["type"], "TRENCH_TIER": sp["tier"],
            "INFRA_STATUS": sp["infra"], "VERIFY_STATUS": "Designed",
            "SURFACE": surf, "REINSTATE": reinstate,
            "PDP_ID": sp["pdp"], "POLYGON_ID": sp["polygon"],
            "SRC": sp["src"],
            "AERIAL": 1 if aerial_reason else 0,
            "AERIAL_REASON": aerial_reason or None,
            # The published trench layers are MULTILINESTRING (what the map,
            # LLD and BOQ readers expect) — one part per span.
            "geom": _make_multiline([sp["coords"]]),
        })
    # ── "is every trench going somewhere?" ───────────────────────────────
    # Anchors: a trench may only end at a premise, a PDP or the MFG (or join
    # another span). Groups of spans that reach none of them are dropped, and
    # the end-of-line diagnostic is reported so a partly dangling design shows
    # up in the run log instead of on the map.
    anchors_pts = _anchor_points(pdps, houses, mfgs)
    span_rows, pruned_rows = prune_unanchored_spans(span_rows, anchors_pts, params, log)
    if pruned_rows:
        _write_lines(os.path.join(out_dir, "Pruned_Trenches.gpkg"), "Pruned_Trenches",
                     pruned_rows, FIELD_LINE, params.target_epsg,
                     geom_type=ogr.wkbMultiLineString)
    dangling = dangling_ends(span_rows, anchors_pts, params)
    if dangling:
        by_run: Dict[str, int] = defaultdict(int)
        for d in dangling:
            by_run[d["RUN_ID"]] += 1
        log("loose ends: %d end(s) not at an anchor/junction across %d run(s) %s"
            % (len(dangling), len(by_run),
               ", ".join(sorted(by_run)[:6])))

    # ── spans reaching no premise at all (diagnostic, never removed) ──────
    grid_houses = GridIndex(cell=50.0)
    for i, h in enumerate(houses):
        grid_houses.add(h["x"], h["y"], i)
    far_spans = []
    for sp in span_rows:
        c = _span_coords(sp)
        if len(c) < 2:
            continue
        mid = c[len(c) // 2]
        if not grid_houses.any_within(mid[0], mid[1], params.far_premise_m):
            far_spans.append({"TRENCH_ID": sp["TRENCH_ID"],
                              "TRENCH_TIER": sp["TRENCH_TIER"],
                              "length_m": sp["length_m"]})
    if far_spans:
        log("far from any premise: %d span(s) (%.0f m) - backbone connectors unless "
            "the ends are loose" % (len(far_spans),
                                    sum(s["length_m"] for s in far_spans)))

    # per-run span indexing
    _index_spans(span_rows)

    # ── outputs ──────────────────────────────────────────────────────────
    _write_lines(os.path.join(out_dir, "Final_Trenches.gpkg"), "Final_Trenches",
                 span_rows, FIELD_LINE, params.target_epsg,
                 geom_type=ogr.wkbMultiLineString)
    tier_files = {"Feeder": "Feeder_Trench.gpkg",
                  "Distribution": "Distribution_Trench.gpkg",
                  "Garden": "Garden_Trench.gpkg"}
    for tier, fname in tier_files.items():
        rows = [r for r in span_rows if r["TRENCH_TIER"] == tier]
        _write_lines(os.path.join(out_dir, fname), tier, rows,
                     FIELD_LINE, params.target_epsg,
                     geom_type=ogr.wkbMultiLineString)

    node_rows = []
    for n in nodes:
        node_rows.append({
            "NODE_ID": n["NODE_ID"], "NODE_TYPE": n["NODE_TYPE"],
            "PRIORITY": n["PRIORITY"], "PDP_ID": n.get("ref") or None,
            "X": round(n["x"], 2), "Y": round(n["y"], 2),
            "geom": ogr.CreateGeometryFromWkt(f"POINT({n['x']} {n['y']})"),
        })
    _write_points(os.path.join(out_dir, "Trench_Nodes.gpkg"), "Trench_Nodes",
                  node_rows, FIELD_NODE, params.target_epsg)

    drill_rows = []
    for i, d in enumerate(drills):
        drill_rows.append({
            "DRILL_ID": "HDD-%05d" % (i + 1), "ROAD_CLASS": d["cls"],
            "WIDTH_M": round(d["width"], 2), "TRENCH_TYPE": "HDD",
            "INFRA_STATUS": "New", "geom": _make_line(d["coords"]),
        })
    _write_lines(os.path.join(out_dir, "Tangent_Crossings.gpkg"), "Tangent_Crossings",
                 drill_rows, FIELD_DRILL, params.target_epsg)

    # ── aerial drops (house legs built aerial, never excavated) ───────────
    aerial_rows = []
    for i, leg in enumerate(aerial_legs):
        aerial_rows.append({
            "DROP_ID": "AD-%05d" % (i + 1),
            "POLYGON_ID": leg["house"].get("POLYGON_ID") or None,
            "TRENCH_TIER": "Garden", "TRENCH_TYPE": "Aerial",
            "length_m": round(leg["length"], 2),
            "AERIAL_REASON": leg["aerial_reason"],
            "INFRA_STATUS": "New",
            "geom": _make_multiline([leg["coords"]]),
        })
    _write_lines(os.path.join(out_dir, "Aerial_Drops.gpkg"), "Aerial_Drops",
                 aerial_rows, FIELD_AERIAL, params.target_epsg,
                 geom_type=ogr.wkbMultiLineString)

    # ── report ───────────────────────────────────────────────────────────
    by_type: Dict[str, float] = defaultdict(float)
    by_tier: Dict[str, float] = defaultdict(float)
    for r in span_rows:
        by_type[r["TRENCH_TYPE"]] += r["length_m"]
        by_tier[r["TRENCH_TIER"]] += r["length_m"]
    vertices = [r["geom"].GetPointCount() for r in span_rows if r["geom"]]
    total_len = sum(r["length_m"] for r in span_rows)
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "params": asdict(params),
        "anchors": {"mfg": len(mfgs), "pdps": len(pdps), "houses": len(houses),
                    "polygons": len(polygons)},
        "street_graph": {"nodes": sg.G.number_of_nodes(),
                         "edges": sg.G.number_of_edges()},
        "runs": len(runs),
        "spans": len(span_rows),
        "spans_per_run": round(len(span_rows) / max(1, len(runs)), 2),
        "total_length_m": round(total_len, 1),
        "avg_span_m": round(total_len / max(1, len(span_rows)), 1),
        "max_span_m": round(max([r["length_m"] for r in span_rows] or [0]), 1),
        "avg_vertices_per_span": round(sum(vertices) / max(1, len(vertices)), 2),
        "length_by_type_m": {k: round(v, 1) for k, v in sorted(by_type.items())},
        "length_by_tier_m": {k: round(v, 1) for k, v in sorted(by_tier.items())},
        "nodes": {k: v for k, v in _count_by(nodes, "NODE_TYPE").items()},
        "carrier_length_by_class_m": {k: round(v, 1)
                                     for k, v in sorted(carrier_mix.items())},
        "carriageway_carrier_m": round(carriage_m, 1),
        "drills": len(drills),
        "garden_legs": len(legs),
        "aerial_legs": len(aerial_rows),
        "aerial_length_m": round(sum(r["length_m"] for r in aerial_rows), 1),
        "aerial_by_reason": _count_by(
            [{"r": r["AERIAL_REASON"]} for r in aerial_rows], "r"),
        "trench_spans_in_aerial_zone": sum(1 for r in span_rows if r["AERIAL"]),
        "pruned_spans": len(pruned_rows),
        "pruned_length_m": round(sum(s["length_m"] for s in pruned_rows), 1),
        "loose_ends": len(dangling),
        "spans_far_from_premise": len(far_spans),
        "elapsed_s": round(time.time() - t0, 1),
        "log": messages,
    }
    with open(os.path.join(out_dir, "trench_design_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "log"}, indent=2))
    return report


def _count_by(items: Sequence[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for it in items:
        out[str(it.get(key))] += 1
    return dict(sorted(out.items()))


def _index_spans(span_rows: List[dict]) -> None:
    per_run: Dict[str, List[dict]] = defaultdict(list)
    for r in span_rows:
        per_run[r["RUN_ID"]].append(r)
    for _run, rows in per_run.items():
        rows.sort(key=lambda r: r["TRENCH_ID"])
        for i, r in enumerate(rows):
            r["SPAN_INDEX"] = i + 1
            r["SPAN_COUNT"] = len(rows)


def _junction_points(runs: Sequence[Run], min_degree: int = 3,
                     tol_m: float = 5.0) -> List[Tuple[float, float]]:
    """Points where ``min_degree`` or more runs / network ends meet."""
    buckets: Dict[Tuple[int, int], int] = defaultdict(int)
    for r in runs:
        for pt in (r.coords[0], r.coords[-1]):
            buckets[(int(round(pt[0] / tol_m)), int(round(pt[1] / tol_m)))] += 1
    return [(k[0] * tol_m, k[1] * tol_m)
            for k, v in buckets.items() if v >= max(3, min_degree)]


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Civil trench designer (standalone)")
    ap.add_argument("--mfg", required=True)
    ap.add_argument("--pdps", required=True)
    ap.add_argument("--objects", required=True)
    ap.add_argument("--polygons", required=True)
    ap.add_argument("--roads", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--aerial", default=None,
                    help="optional aerial-zone polygons (never trenched)")
    ap.add_argument("--aerial-max-leg", type=float, default=0.0,
                    help="drop legs longer than this are built aerial (0 = off)")
    ap.add_argument("--target-epsg", type=int, default=25833)
    ap.add_argument("--simplify-tol", type=float, default=2.5)
    ap.add_argument("--max-garden", type=float, default=60.0)
    ap.add_argument("--crossing-merge", type=float, default=45.0)
    ap.add_argument("--crossing-dedupe", type=float, default=25.0)
    ap.add_argument("--pull-backbone", type=float, default=250.0)
    ap.add_argument("--pull-dist", type=float, default=100.0)
    ap.add_argument("--street-avoid-scale", type=float, default=1.0,
                    help="multiplier on the carriageway cost ladder (1.0 default; "
                         "raise to avoid streets harder)")
    ap.add_argument("--far-premise-m", type=float, default=80.0,
                    help="report spans whose midpoint is this far from any premise")
    ap.add_argument("--keep-orphans", action="store_true",
                    help="keep span groups that reach no anchor (default: prune)")
    args = ap.parse_args(argv)

    cfg = {
        "mfg": args.mfg, "pdps": args.pdps, "objects": args.objects,
        "polygons": args.polygons, "roads": args.roads, "out": args.out,
        "aerial": args.aerial,
        "aerial_max_leg_m": args.aerial_max_leg,
        "target_epsg": args.target_epsg, "simplify_tol_m": args.simplify_tol,
        "max_garden_m": args.max_garden,
        "crossing_merge_m": args.crossing_merge,
        "crossing_dedupe_m": args.crossing_dedupe,
        "pull_backbone_m": args.pull_backbone, "pull_dist_m": args.pull_dist,
        "street_avoid_scale": args.street_avoid_scale,
        "far_premise_m": args.far_premise_m,
        "prune_dangling": not args.keep_orphans,
    }
    design(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
