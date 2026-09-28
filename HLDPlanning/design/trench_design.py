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
import bisect
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

try:
    from . import surface_cross_section as sx
except ImportError:  # standalone script execution (python design/trench_design.py)
    import surface_cross_section as sx

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
# Classes a trench is NEVER dug along — not routed on at any weight, and never
# Open Cut. A motorway is not a street a trench can follow: it is only ever
# CROSSED, and a crossing is a drill (HDD), so a motorway span can never be
# open-cut. This is the operator rule, stated once and enforced by
# :func:`_class_factor` (an infinite weight) rather than left to the routing
# ladder, where a large finite cost is still a path the router will take when
# the only alternative is long enough.
NEVER_CARRIER_CLASSES = ("motorway", "motorway_link")

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

# Surface attribution is routing evidence + construction position, not a
# lookup on the highway class alone: a kerb-class span's geometry is published
# on the KERB BAND (kerb_offset_for(cls) = carriageway_half + 1.25 m — the
# middle of the footway by design), so its surface is whatever band that offset
# lands in, resolved through surface_cross_section. Only when the kerb band is
# disabled is a carriageway-carried trench actually IN the road (Asphalt).

# The street-avoidance ladder. A trench along a carriageway needs a road
# opening permit and traffic management, so the cost is ordered by how big the
# road is — a district road is far worse to dig than a residential street:
#
#     residential < tertiary < secondary < primary/motorway
#
# Every class stays *routable* EXCEPT the motorway set — see
# NEVER_CARRIER_CLASSES, which is excluded outright. At these weights the router
# will accept a kilometres-long footway detour before it enters a street, and
# when it must use one it picks the smallest class available. `track` is a field
# haul road: cheap to cross, poor to dig.
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
    # No "motorway" entry: it is in NEVER_CARRIER_CLASSES and gets no weight at
    # all (see _class_factor). A ladder entry would have implied it was a slow
    # carrier, i.e. still a carrier.
}
NON_CARRIER_FACTOR = CARRIAGE_FACTOR  # any carriageway not in the ladder

# ── the kerb band: never a trench down the middle of a carriageway ──────────
# Where a street has no mapped pavement the router has only the carriageway
# CENTRELINE to follow, so the published trench — and the duct and the cable
# riding it — was drawn down the middle of the road. That geometry is laid at
# the pavement band instead: KERB_OFFSET_M out from the centreline, ramping
# back to the exact OSM vertex at every end and at every junction, so no shared
# vertex, no snap and no route moves (see _kerb_band / _kerb_side_sign).
#
# The band is PER CLASS — half the carriageway plus a footway inset — so a
# trench lands in the footway of a narrow street AND of a wide one. A flat 3 m
# was measured wrong on the ground: on a 6.5 m residential road it is still ON
# the carriageway, which is what "the trenches are not really placed on the
# sidewalks" was. The same rule runs the derived pavement
# (osm_source.pavement_offset_for) and the network stage's cabinet band
# (trench_layer.SIDEWALK_OFFSET_M / network_layer.DEFAULT_SIDEWALK): a splitter
# is a street cabinet, so the cabinet sits BESIDE its own trench. Tests pin
# them equal.
KERB_FOOTWAY_INSET_M = 1.25  # kerb -> trench band (the middle of the footway)


def kerb_offset_for(cls: Optional[str]) -> float:
    """Centreline -> trench band for a street class (metres).

    Half the carriageway (``VEHICULAR_WIDTH_M``) plus the footway inset.
    Unknown classes get the default 6 m street width.
    """
    width = VEHICULAR_WIDTH_M.get(str(cls or "").strip().lower(), 6.0)
    return width / 2.0 + KERB_FOOTWAY_INSET_M


KERB_OFFSET_M = kerb_offset_for("residential")  # the base street band (4.5 m)
KERB_RAMP_M = 6.0            # over how many metres the offset flares in and out
KERB_SIDE_SEARCH_M = 2.0     # radius that counts as "the pavement is here"
KERB_ANCHOR_SEARCH_M = 6.0   # radius that counts as "the cabinet is here"
# Which classes the band applies to: the CARRIAGEWAY ladder, i.e. the classes the
# design already treats as roads to be crossed (see STREET_CLASS_FACTOR), PLUS
# ``service``. A service way is a driveway or parking aisle, and it is the single
# biggest source of centreline-carried trench on the reference project (206 m of
# 252 m) — "strictly never down the middle of the road" has to include it. It is
# banded with the same taper, so its junctions and its access corridor are still
# met exactly; a real pavement, where one exists, always wins the side.
KERB_CLASSES = tuple(sorted(set(STREET_CLASS_FACTOR) | {"service"}))
# How long a break in the PAVEMENT the graph may bridge. OSM carries the
# pavement per block/crossing/driveway, so a real sidewalk is a chain of loose
# ends: measured on the reference project, 49 090 footway endpoint pairs sit
# within 15 m of each other. Crossing one of those pairs is a driveway apron or a
# road crossing; NOT crossing it is what sends the router onto the carriageway
# for the length of the block (the defect this closes). Crossings the link does
# span are still drills — detect_drills sees the run cross a carriageway.
SIDEWALK_LINK_M = 15.0


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
    # Every anchor (MFG, PDP) must END ON the network, not merely near it: a
    # trench that stops 1-2 m short of the splitter looks unconnected on the map
    # and, because the duct/cable stages club geometry within 0.5 m, the service
    # inherits the gap. This is the physical tolerance for "touching" — the same
    # one the downstream stages use — so the anchor-termination pass closes
    # anything above it with a connector instead of deferring to the 10 m
    # chamber-separation heuristic (params.min_node_sep_m).
    anchor_touch_m: float = 0.05
    # Diagnostic only: a span whose midpoint is further than this from every
    # premise/anchor is reported (never silently removed).
    far_premise_m: float = 80.0
    # A bore end closer to its run's end than this is a bore continuing onto
    # the neighbouring run, not a pit (see detect_drills).
    pit_edge_m: float = 0.3
    # Cut mains runs back to the part something is connected to (see
    # trim_unserved_tails). Legs, MFG/PDP ends and junctions are supports.
    trim_tails: bool = True
    # Aerial: a drop leg inside an aerial zone (or longer than this, when set)
    # is built aerial — drawn as an aerial drop, never excavated. 0 = length
    # rule off (zone layer only).
    aerial_max_leg_m: float = 0.0
    # The kerb band (see KERB_OFFSET_M / kerb_offset_for): geometry along a
    # carriageway is drawn at the pavement band instead of on the centreline.
    # 0 disables the rule. With kerb_offset_per_class the value only gates the
    # rule (it must be > 0) and each class gets its own band from the width
    # table; set kerb_offset_per_class=False to lay every class at this flat
    # distance instead.
    kerb_offset_m: float = KERB_OFFSET_M
    kerb_offset_per_class: bool = True
    kerb_ramp_m: float = KERB_RAMP_M
    # When enabled, longitudinal trench routing is restricted to actual OSM
    # footway/sidewalk classes. Carriageway classes remain in the separate
    # vehicular input solely for HDD crossing detection; they cannot become
    # trench carriers.
    sidewalk_only: bool = True
    # A pavement mapped in pieces (every OSM block, crossing and driveway break
    # leaves a pair of loose ends) is joined across gaps up to this long, so the
    # router can stay on the pavement instead of stepping onto the carriageway
    # to get around the break.
    #
    # DEFAULT 0.0 = OFF, and that is a measurement, not an oversight. On the
    # reference project it bridges 718 breaks / 6 374 m and merges 195 graph
    # components to 167 — and changes the published trench by 0 m, because a
    # break's loose end is a DEAD END: the route never needed to pass through it
    # unless an anchor snapped there, and at the stretch that was actually
    # reported there is no mapped pavement at all, so there is no break to join.
    # With it on, the duct and cable layers read WORSE (distribution ducts
    # 110.7 -> 147.9 m within 1 m of a carriageway centreline). It is kept,
    # tested and off; turn it on with --sidewalk_link_m when a project's pavement
    # really is a chain of loose ends.
    sidewalk_link_m: float = 0.0


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


def _kerb_cell(x: float, y: float, size: float) -> Tuple[int, int]:
    """Spatial bucket for the kerb-side search indexes."""
    return (int(math.floor(x / size)), int(math.floor(y / size)))


def _left_normal(coords: Sequence[Tuple[float, float]], i: int
                 ) -> Tuple[float, float]:
    """Unit normal to the LEFT of the travel direction at vertex ``i``."""
    a = coords[i - 1] if i > 0 else coords[i]
    b = coords[i + 1] if i + 1 < len(coords) else coords[i]
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg = math.hypot(dx, dy)
    if seg <= 1e-9:
        return (0.0, 0.0)
    return (-dy / seg, dx / seg)


def _kerb_band(coords: Sequence[Tuple[float, float]], anchors: Set[int],
               kerb_m: float, ramp_m: float, sign: int
               ) -> List[Tuple[float, float]]:
    """A carriageway part's geometry, laid at the kerb instead of the centreline.

    The offset is ZERO at every anchor vertex — the part's two ends and every
    vertex OSM shares with another way — and ramps to ``kerb_m`` between them.
    Those shared vertices are what joins two ways at a junction, and a run's
    chambers, ducts and cables are all built on this geometry afterwards, so a
    moved anchor drags everything attached to it: that is exactly how the three
    earlier post-routing attempts at this offset failed. Ramps keep every
    junction, and every row that used to coincide, precisely where OSM put it.
    """
    n = len(coords)
    if n < 2 or kerb_m <= 0.0:
        return [(x, y) for x, y in coords]
    cum = _cum(coords)
    a_chain = sorted(cum[i] for i in anchors if 0 <= i < n)
    out: List[Tuple[float, float]] = []
    for i in range(n):
        x, y = coords[i]
        if i in anchors:
            out.append((x, y))
            continue
        near = float("inf")
        if a_chain:
            pos = bisect.bisect_left(a_chain, cum[i])
            for j in (pos - 1, pos):
                if 0 <= j < len(a_chain):
                    near = min(near, abs(cum[i] - a_chain[j]))
        t = 1.0 if (ramp_m <= 0.0 or near == float("inf")) else min(1.0, near / ramp_m)
        if t <= 0.0:
            out.append((x, y))
            continue
        nx_, ny_ = _left_normal(coords, i)
        out.append((x + sign * kerb_m * t * nx_, y + sign * kerb_m * t * ny_))
    return out


def _nearest_in(index: Dict[Tuple[int, int], List[Tuple[float, float]]],
                size: float, x: float, y: float, radius: float
                ) -> Optional[float]:
    """Distance to the nearest indexed point within ``radius`` (None if none).

    ``size`` is the index's cell size and must be >= ``radius``, so the 3x3 cell
    neighbourhood covers the whole search circle.
    """
    best: Optional[float] = None
    cx, cy = _kerb_cell(x, y, size)
    for gx in (cx - 1, cx, cx + 1):
        for gy in (cy - 1, cy, cy + 1):
            for px, py in index.get((gx, gy), ()):
                d = math.hypot(px - x, py - y)
                if d <= radius and (best is None or d < best):
                    best = d
    return best


def _count_in(index: Dict[Tuple[int, int], List[Tuple[float, float]]],
              size: float, x: float, y: float, radius: float) -> int:
    """How many indexed points lie within ``radius`` of (x, y)."""
    n = 0
    cx, cy = _kerb_cell(x, y, size)
    for gx in (cx - 1, cx, cx + 1):
        for gy in (cy - 1, cy, cy + 1):
            for px, py in index.get((gx, gy), ()):
                if math.hypot(px - x, py - y) <= radius:
                    n += 1
    return n


def _kerb_side_sign(coords: Sequence[Tuple[float, float]],
                    foot_index: Dict[Tuple[int, int], List[Tuple[float, float]]],
                    anchor_index: Dict[Tuple[int, int], List[Tuple[float, float]]],
                    offset_m: float = KERB_OFFSET_M
                    ) -> int:
    """Which side of a carriageway its kerb band is laid on (+1 = left of travel).

    Chosen from GEOMETRY, never from the way's direction: two OSM ways along one
    street are routinely digitised in opposite directions, so a per-way
    left/right rule puts them on OPPOSITE kerbs and rows that used to coincide
    separate — the recorded failure of the earlier attempt. In order:

    1. the side a real pavement covers — where the pavement is, the trench goes;
    2. then the side the network's anchors are on, measured to the nearest one: a
       splitter cabinet stands on the pavement it serves, so laying the kerb
       band on that side leaves the cabinet beside its own trench;
    3. then the higher-northing side (ties: the higher-easting one), which is a
       function of geometry alone and so is stable for either digitisation.
    """
    mid = len(coords) // 2
    x, y = coords[mid]
    nx_, ny_ = _left_normal(coords, mid)
    if nx_ == 0.0 and ny_ == 0.0:
        return 1
    sides: Dict[int, Tuple[float, float, int, Optional[float]]] = {}
    for sign in (1, -1):
        px = x + sign * nx_ * offset_m
        py = y + sign * ny_ * offset_m
        foot = _count_in(foot_index, KERB_SIDE_SEARCH_M, px, py,
                         KERB_SIDE_SEARCH_M)
        anchor = _nearest_in(anchor_index, KERB_ANCHOR_SEARCH_M, px, py,
                             KERB_ANCHOR_SEARCH_M)
        sides[sign] = (px, py, foot, anchor)
    lx, ly, lfoot, lanchor = sides[1]
    rx, ry, rfoot, ranchor = sides[-1]
    if lfoot != rfoot:
        return 1 if lfoot > rfoot else -1
    if (lanchor is None) != (ranchor is None):
        return 1 if lanchor is not None else -1
    if lanchor is not None and ranchor is not None and abs(lanchor - ranchor) > 1e-6:
        return 1 if lanchor < ranchor else -1
    if abs(ly - ry) > 1e-9:
        return 1 if ly > ry else -1
    return 1 if lx >= rx else -1


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
            name = p.GetGeometryName()
            if name == "MULTIPOINT":
                # Reused brownfield PDPs are written as MultiPoint (single
                # part) into a POINT layer — QGIS warns but allows it.  OGR's
                # GetX() on a MultiPoint is "Incompatible geometry" when
                # exceptions are enabled, so unwrap to the first point.
                if p.GetGeometryCount() == 0:
                    continue
                sub = p.GetGeometryRef(0)
                if sub is None or sub.IsEmpty():
                    continue
                p = sub.Clone()
            elif name not in ("POINT",):
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
                "MFG_ID": _value(f, "MFG_ID"),
                "fclass": _value(f, "fclass"),
            })
    ds = None
    return out


def _addr_of(pt: dict) -> Optional[str]:
    """The premise id carried onto a trench span (``None`` when unknown)."""
    v = pt.get("ADDR_ID")
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.upper() != "NULL" else None


def _hh_of(pt: dict) -> float:
    """Household count of a premise (1.0 when the plan does not state one)."""
    try:
        hh = float(pt.get("HH"))
    except (TypeError, ValueError):
        return 1.0
    return hh if hh > 0 else 1.0


def houses_on_edges(edge_keys: Iterable[Tuple[str, str]],
                    edge_houses: Dict[Tuple[str, str], Set[int]],
                    houses: Sequence[dict]) -> Tuple[Optional[str], Optional[float]]:
    """``(addr, hh)`` for every house whose spine path rides ``edge_keys``.

    Returns ``(None, None)`` for a span no house is routed along (a pure
    backbone span, or a PDP spur), so the published attribute states honestly
    that the span serves no premise instead of carrying a misleading ``1``.
    """
    idxs: Set[int] = set()
    for ek in edge_keys:
        idxs.update(edge_houses.get(ek, ()))
    if not idxs:
        return None, None
    seen: Set[str] = set()
    addrs: List[str] = []
    total = 0.0
    for i in sorted(idxs):
        if i < 0 or i >= len(houses):
            continue
        h = houses[i]
        a = _addr_of(h)
        if a and a not in seen:
            seen.add(a)
            addrs.append(a)
        total += _hh_of(h)
    return (",".join(addrs) if addrs else None), total


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
                     ) -> Tuple[List[Tuple[List[Tuple[float, float]], str, dict]],
                                List[Tuple[List[Tuple[float, float]], str, dict]]]:
    """(walkable, vehicular) road parts, each ``(coords, fclass, tags)``.

    ``tags`` carries the optional surface-relevant OSM fields (``sidewalk``,
    ``surface``, ``width``, ``lanes``) when the input layer has them; it is
    empty otherwise. They drive surface attribution only — never routing.
    """
    ds = ogr.Open(path)
    if ds is None:
        raise SystemExit(f"cannot open {path}")
    walk: List[Tuple[List[Tuple[float, float]], str, dict]] = []
    veh: List[Tuple[List[Tuple[float, float]], str, dict]] = []
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
        tag_idx = {name: defn.GetFieldIndex(name)
                   for name in ("sidewalk", "surface", "width", "lanes")}

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
            tags = {name: str(f.GetField(idx)) for name, idx in tag_idx.items()
                    if idx >= 0 and f.GetField(idx) not in (None, "")}
            # A trench cannot be dug on a bridge deck or through a tunnel, so
            # those segments are never carriers (they stay crossable).
            deck = _is_true(i_bridge, f) or _is_true(i_tunnel, f)
            for coords in _part_coords(g):
                if deck:
                    veh.append((coords, cls, tags))
                    continue
                if cls in WALKABLE_CLASSES:
                    walk.append((coords, cls, tags))
                    # Streets that are walkable *and* drivable (residential,
                    # service, living_street, track, unclassified) are both a
                    # carrier and a road to be crossed — the crossing test's
                    # angle filter keeps the parallel overlap from becoming a
                    # drill, while genuine crossings are picked up.
                    if cls not in PURE_FOOTWAY_CLASSES:
                        veh.append((coords, cls, tags))
                else:
                    veh.append((coords, cls, tags))
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
    # the largest connected piece of the walkable network: everything routable
    # belongs to it, and OSM always carries a few stray footway fragments
    main_component: Set[str] = field(default_factory=set)
    # How much published geometry was moved off a carriageway centreline onto its
    # kerb band (the "never run a trench down the middle of the road" rule), and
    # the largest offset actually applied.
    kerb_parts: int = 0
    kerb_max_offset_m: float = 0.0
    # Pavement breaks bridged (see ``Params.sidewalk_link_m``) and the total gap
    # length they closed.
    sidewalk_links: int = 0
    sidewalk_link_m: float = 0.0

    def nearest_node(self, x: float, y: float, radius: float) -> Optional[str]:
        best, best_d = None, radius
        for i in self.index.near(x, y, radius):
            key = self.node_keys[i]
            nx_, ny_ = self.node_xy[key]
            d = math.hypot(nx_ - x, ny_ - y)
            if d <= best_d:
                best_d, best = d, key
        return best

    def nearest_main_node(self, x: float, y: float, radius: float) -> Optional[str]:
        """Nearest node that is actually connected to the rest of the network."""
        if not self.main_component:
            return self.nearest_node(x, y, radius)
        best, best_d = None, radius
        for i in self.index.near(x, y, radius):
            key = self.node_keys[i]
            if key not in self.main_component:
                continue
            nx_, ny_ = self.node_xy[key]
            d = math.hypot(nx_ - x, ny_ - y)
            if d <= best_d:
                best_d, best = d, key
        return best


def _part_unpack(part) -> Tuple[List[Tuple[float, float]], str, dict]:
    """``(coords, cls, tags)`` from a road part of either tuple shape.

    ``_read_road_parts`` emits 3-tuples; tests and legacy callers still pass
    2-tuples. Both are accepted so the tag channel never breaks a caller.
    """
    coords, cls = part[0], part[1]
    tags = part[2] if len(part) > 2 else None
    return coords, cls, (tags or {})


def build_street_graph(walkable: Sequence[Tuple[List[Tuple[float, float]], str]],
                       params: Params,
                       anchors: Sequence[Tuple[float, float]] = ()) -> StreetGraph:
    """Node the walkable road network into a routable graph.

    OSM lines share exact vertices at intersections, so rounding coordinates is
    enough to connect the network; consecutive vertices become edges carrying
    their own geometry, so a route can be re-assembled without losing shape.

    NOTE — where OSM has no footway, a run IS routed on the carriageway
    centreline, so the published trench (and the duct and cable riding it) is
    drawn down the middle of that street: 8.1 % of the carrier length on the
    reference project, measured as 6 feeder-cable runs / 208.6 m hugging a
    carriageway centreline at 0.00 m with no footway within 2 m, worst 64.7 m.

    THREE attempts to shift that geometry afterwards were made and all REVERTED.
    They are recorded with their numbers so none is retried blind, because the
    lesson is the same every time and it is not about the shift itself:

    * Offset at the node-keying step — fatal, because the keys are what join two
      OSM ways at a junction: 6 components, 13 PDPs behind 392 m spurs, MFG
      reached 18/31.
    * Offset the STORED geometry only (keys untouched, tapered at junctions).
      26/31, 2 connected parts, 130/304 couplers off — the chamber/duct chain is
      built chamber-to-chamber on the trench, and the shift side is per OSM WAY,
      so two ways along one street digitised in opposite directions move to
      opposite kerbs and rows that used to coincide separate.
    * Snap each road stretch back onto the PARALLEL SIDEWAY (the option chosen on
      review, keyed on geometry not on way direction, sampled at 8 m, with the
      stretch's joins kept exact). It moved 362 m over 10 stretches and still
      cost the chain: 29/31 first, 26/31 with the join anchors kept, plus a
      loose run end. Anything that moves the routed geometry afterwards pulls
      the chambers and the ducts that attach to it.

    Linking the sidewalk across its own break (an edge between nearby sidewalk
    endpoints) was tried as the next lever and does NOT cover this: on the
    reference project 49 090 footway endpoint pairs sit within 15 m of each
    other, and at the reported stretch the graph carries a single 33 m
    ``residential`` edge with NO footway edge at either end, so there is no
    sidewalk to stay on and no gap to close.

    WHAT DOES FIX IT is the BASIS itself, before any run exists. Carriageway-
    carried geometry on the CARRIAGEWAY LADDER (``KERB_CLASSES``, which includes
    `service`: a driveway aisle is still a carriageway) is published on the
    street's KERB BAND — offset
    KERB_OFFSET_M toward the kerb, ramping back to the exact OSM vertex at every
    end and junction (see :func:`_kerb_band`, :func:`_kerb_side_sign`). The node
    keys, the node positions and every edge WEIGHT stay on the original OSM
    line, so the route chosen, the topology, the ranking and the connectivity are
    all exactly as before; only the coordinates a run is drawn on move. A trench
    can therefore not be drawn down the middle of a carriageway — where it has to
    cross one, that crossing is still a drill (HDD).
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

    def rkey(x: float, y: float) -> Tuple[int, int]:
        """The rounded key ``key()`` builds nodes from — used to spot junctions."""
        return (int(round(x / tol)), int(round(y / tol)))

    # ── pass 1: densify, and count how many parts share each vertex ───────────
    # A vertex used by two parts is a JUNCTION. The keys come from these OSM
    # coordinates and never from the kerb geometry below, so node identity — and
    # with it every junction, snap and route — is exactly as it was.
    dense_parts: List[Tuple[List[Tuple[float, float]], str, dict]] = []
    uses: Dict[Tuple[int, int], int] = {}
    for part in walkable:
        coords, cls, tags = _part_unpack(part)
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
        dense_parts.append((dense, cls, tags))
        for q in dense:
            rk = rkey(q[0], q[1])
            uses[rk] = uses.get(rk, 0) + 1

    # What the kerb side is chosen from: real pavements, and the anchors the
    # network exists to serve (the caller passes its PDP/MFG/premise points).
    kerb_on = params.kerb_offset_m > 0.0
    foot_index: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    anchor_index: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    if kerb_on:
        for dense, cls, _tags in dense_parts:
            if cls in PURE_FOOTWAY_CLASSES:
                for x, y in dense:
                    foot_index.setdefault(_kerb_cell(x, y, KERB_SIDE_SEARCH_M),
                                          []).append((x, y))
        for x, y in anchors:
            anchor_index.setdefault(_kerb_cell(x, y, KERB_ANCHOR_SEARCH_M),
                                    []).append((x, y))

    n_edges = 0
    n_kerb = 0
    kerb_max = 0.0
    n_links = 0
    links_m = 0.0
    for dense, cls, tags in dense_parts:
        # The geometry a RUN is drawn on. Where the only line along a street is
        # its carriageway centreline, that geometry moves onto the kerb band;
        # the keys and weights below still use the OSM coordinates, so which
        # edges the router picks does not change.
        geom: List[Tuple[float, float]] = dense
        if kerb_on and cls in KERB_CLASSES:
            anchors_i = {0, len(dense) - 1}
            for i, q in enumerate(dense):
                if uses.get(rkey(q[0], q[1]), 0) > 1:
                    anchors_i.add(i)
            band_m = (kerb_offset_for(cls) if params.kerb_offset_per_class
                      else params.kerb_offset_m)
            geom = _kerb_band(dense, anchors_i, band_m,
                              params.kerb_ramp_m,
                              _kerb_side_sign(dense, foot_index, anchor_index,
                                              band_m))
            moved = max(math.hypot(gx - qx, gy - qy)
                        for (qx, qy), (gx, gy) in zip(dense, geom))
            if moved > 0.01:
                n_kerb += 1
                kerb_max = max(kerb_max, moved)
        prev_key = None
        prev_pt = None
        prev_gq = None
        f = factor(cls)
        for q, gq in zip(dense, geom):
            k = key(q[0], q[1])
            if prev_key is not None and prev_key != k:
                w = math.hypot(q[0] - prev_pt[0], q[1] - prev_pt[1]) * f
                if w > 0:
                    ek = (prev_key, k) if prev_key < k else (k, prev_key)
                    if ek not in edge_coords:
                        G.add_edge(prev_key, k, weight=w, cls=cls, tags=tags)
                        edge_coords[ek] = [prev_gq, gq]
                        n_edges += 1
            prev_key, prev_pt, prev_gq = k, q, gq

    # ── pavement continuity: bridge the breaks in the sidewalk ───────────────
    # OSM carries a pavement per block / crossing / driveway, so a real sidewalk
    # is a chain of loose ends 1-15 m apart. The router cannot cross one of those
    # breaks, and the carriageway is then the only continuous line along the
    # block, which is what makes it step off the pavement. Joining the loose ends
    # fixes that at the source: the route stays on the pavement and NOTHING is
    # moved after routing, so no chamber, duct or cable can be disturbed.
    # Both ends must already exist as nodes (they do — pass 1 keyed every part),
    # so a link only ever joins two real OSM vertices.
    if params.sidewalk_link_m > 0.0:
        cap = params.sidewalk_link_m
        ends: List[Tuple[float, float, str, int]] = []
        for pi, (dense, cls, _tags) in enumerate(dense_parts):
            if cls in PURE_FOOTWAY_CLASSES and len(dense) >= 2:
                for p in (dense[0], dense[-1]):
                    ends.append((p[0], p[1], key(p[0], p[1]), pi))
        cells: Dict[Tuple[int, int], List[int]] = {}
        for i, e in enumerate(ends):
            cells.setdefault(_kerb_cell(e[0], e[1], cap), []).append(i)
        cands = []
        for i, (xa, ya, ka, pa) in enumerate(ends):
            cx, cy = _kerb_cell(xa, ya, cap)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in cells.get((gx, gy), ()):
                        if j <= i:
                            continue
                        xb, yb, kb, pb = ends[j]
                        if ka == kb or pa == pb:
                            continue
                        d = math.hypot(xb - xa, yb - ya)
                        if d <= tol or d > cap:
                            continue
                        # Only a loose end is a break. A vertex that already has
                        # two ways through it is mid-network: linking it would
                        # cut a corner across the pavement instead.
                        if G.degree(ka) > 1 and G.degree(kb) > 1:
                            continue
                        ek = (ka, kb) if ka < kb else (kb, ka)
                        if ek in edge_coords:
                            continue
                        cands.append((round(d, 3), ka, kb, xa, ya, xb, yb, ek))
        # Nearest first, and deterministic: the edge set may not depend on the
        # order the parts happened to come out of the reader.
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        used: Set[str] = set()
        f_link = factor("footway")
        for d, ka, kb, xa, ya, xb, yb, ek in cands:
            if ka in used or kb in used:
                continue
            G.add_edge(ka, kb, weight=max(d, 1e-6) * f_link, cls="sidewalk_link")
            edge_coords[ek] = [(xa, ya), (xb, yb)]
            used.add(ka)
            used.add(kb)
            n_links += 1
            links_m += d

    node_keys = list(node_xy.keys())
    idx = GridIndex(cell=50.0)
    for i, k in enumerate(node_keys):
        x, y = node_xy[k]
        idx.add(x, y, i)
    # The walkable extract is never fully connected: OSM carries stray footway
    # fragments. Snapping to one of those is what made PDP00004 unreachable —
    # it sits 8 m from a 4-node island while the real network is 100 m away.
    # Deterministic pick: the LARGEST component, ties broken by its smallest
    # node id. ``max(comps, key=len)`` alone would let the winner depend on the
    # order the components happened to come out of the graph.
    main: Set[str] = set()
    try:
        comps = sorted((sorted(c) for c in nx.connected_components(G)),
                       key=lambda c: (-len(c), c))
        main = set(comps[0]) if comps else set()
    except Exception:
        main = set(node_keys)
    return StreetGraph(G=G, edge_coords=edge_coords, node_xy=node_xy,
                       index=idx, node_keys=node_keys, main_component=main,
                       kerb_parts=n_kerb, kerb_max_offset_m=kerb_max,
                       sidewalk_links=n_links, sidewalk_link_m=links_m)


def _class_factor(cls: str, scale: float = 1.0) -> float:
    """Routing weight multiplier for a road class.

    Preferred carriers (footway / path / service / cycleway) are ~1.0; every
    carriageway comes from :data:`STREET_CLASS_FACTOR`, ordered by road size.
    ``scale`` (``Params.street_avoid_scale``) dials the whole street penalty
    up or down without changing the order.

    :data:`NEVER_CARRIER_CLASSES` (motorway) returns ``inf``: no path may be
    routed along it at any ``scale``. A finite weight is not enough — the router
    takes a big penalty whenever the detour is bigger — and "never open-cut on a
    motorway" has to hold for every input, not just the ones where a footway
    happens to exist. A motorway edge therefore can only be CROSSED, and every
    crossing is a drill (HDD).
    """
    if cls in NEVER_CARRIER_CLASSES:
        return math.inf
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


def _run_class_intervals(run: Run, sg: StreetGraph
                         ) -> List[Tuple[float, float, str, dict]]:
    """Return (arc_start, arc_end, cls, tags) intervals along a run's edges."""
    intervals: List[Tuple[float, float, str, dict]] = []
    pos = 0.0
    for ek in run.edge_keys or []:
        if not sg.G.has_edge(*ek):
            continue
        cls = str(sg.G.edges[ek].get("cls") or "")
        tags = sg.G.edges[ek].get("tags") or {}
        coords = sg.edge_coords.get(ek) or []
        length = _coords_len(coords) if len(coords) >= 2 else 0.0
        intervals.append((pos, pos + length, cls, tags))
        pos += length
    return intervals


def _span_surface_context(span: dict,
                          class_intervals: Sequence[Tuple[float, float, str, dict]]
                          ) -> Tuple[str, dict]:
    """Dominant OSM highway class + its tags along a span's arc range."""
    a0, a1 = span["arc0"], span["arc1"]
    if a1 <= a0 or not class_intervals:
        return "", {}
    totals: Dict[str, float] = defaultdict(float)
    tags_by_cls: Dict[str, dict] = {}
    for iv in class_intervals:
        s, e, cls = iv[0], iv[1], iv[2]
        tags = iv[3] if len(iv) > 3 else {}
        overlap = max(0.0, min(a1, e) - max(a0, s))
        if overlap > 0:
            totals[cls] += overlap
            tags_by_cls.setdefault(cls, tags)
    if not totals:
        return "", {}
    dom = max(totals.items(), key=lambda kv: kv[1])[0]
    return dom, tags_by_cls.get(dom, {})


def _span_surface_class(span: dict,
                        class_intervals: Sequence[Tuple[float, float, str, dict]]
                        ) -> str:
    """Dominant OSM highway class along a span's arc range."""
    return _span_surface_context(span, class_intervals)[0]


def _orient(seg: Sequence[Tuple[float, float]], ref: Tuple[float, float]
            ) -> List[Tuple[float, float]]:
    """Return the segment pointing away from ``ref`` (its nearest endpoint)."""
    d0 = math.hypot(seg[0][0] - ref[0], seg[0][1] - ref[1])
    d1 = math.hypot(seg[-1][0] - ref[0], seg[-1][1] - ref[1])
    return list(seg) if d0 <= d1 else list(reversed(seg))


def runs_from_edges(edge_keys: Iterable[Tuple[str, str]],
                    sg: StreetGraph
                    ) -> Tuple[List[List[Tuple[float, float]]], List[str],
                               List[List[Tuple[str, str]]]]:
    """Assemble continuous runs.

    Returns ``(run coord lists, break node ids, run edge keys)`` — the third
    list is parallel to the first and names the street edges each run walked,
    which is what attributes the houses routed along those edges to the run.
    """
    # ── DETERMINISM ──────────────────────────────────────────────────────
    # ``edge_keys`` is a SET of (str, str) tuples, and Python randomises string
    # hashing per process, so iterating it directly made the whole design
    # irreproducible: the insertion order into ``sub`` decided the node order,
    # which decided ``breaks``, which decided the walk order — and therefore
    # the runs, the spans, the drills and the nodes. Measured: two runs of the
    # SAME code on the same project gave 497 vs 495 spans and 21 differing drill
    # geometries. Sorting the input, the break list and the edge list is what
    # makes a run reproducible (verified: byte-identical output across runs).
    sub = nx.Graph()
    for ek in sorted(edge_keys):
        a, b = ek
        if ek in sg.edge_coords:
            sub.add_edge(a, b, coords=sg.edge_coords[ek])

    runs: List[List[Tuple[float, float]]] = []
    run_edges: List[List[Tuple[str, str]]] = []
    used: Set[Tuple[str, str]] = set()
    breaks = sorted(n for n in sub.nodes if sub.degree(n) != 2)
    if not breaks:
        breaks = sorted(sub.nodes)[:1]

    def ekey(u: str, v: str) -> Tuple[str, str]:
        return (u, v) if u < v else (v, u)

    def walk(start: str, first: str) -> None:
        coords = _orient(sub.edges[start, first]["coords"], sg.node_xy[start])
        used.add(ekey(start, first))
        walked: List[Tuple[str, str]] = [ekey(start, first)]
        prev, cur = start, first
        while True:
            if sub.degree(cur) != 2:
                break
            nxt = [w for w in sub.neighbors(cur) if ekey(cur, w) not in used]
            if len(nxt) != 1:
                break
            seg = _orient(sub.edges[cur, nxt[0]]["coords"], coords[-1])
            coords.extend(seg[1:])
            ek = ekey(cur, nxt[0])
            used.add(ek)
            walked.append(ek)
            prev, cur = cur, nxt[0]
        if len(coords) >= 2:
            runs.append(coords)
            run_edges.append(walked)

    for b in breaks:
        for nbr in list(sub.neighbors(b)):
            if ekey(b, nbr) in used or not sub.has_edge(b, nbr):
                continue
            walk(b, nbr)
    for u, v in sorted(sub.edges):
        if ekey(u, v) in used:
            continue
        walk(u, v)
    return runs, breaks, run_edges


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
    # ── routing evidence ─────────────────────────────────────────────────
    # Ordered street-graph edge keys this run was assembled from, so surface
    # attribution can look up the OSM highway class of each walked segment.
    edge_keys: Optional[List[Tuple[str, str]]] = None
    # ── premise attribution ──────────────────────────────────────────────
    # WHICH HOUSES THIS TRENCH EXISTS FOR. A Garden leg serves exactly one
    # house, but a distribution/feeder spine span is SHARED: one span on a
    # street carries the drops of every house routed along it. Without this
    # link a trench span is just a line — nothing records that it was dug for
    # specific premises, and the cabling stage (which indexes its distribution
    # input BY ADDRESS) has no way to connect the two.
    #
    # ``addr`` is the comma-joined ``ADDR_ID`` of every house served (the
    # single value when only one house rides the span); ``hh`` is their summed
    # household count. Both are attributes only — they never affect geometry,
    # routing or node placement.
    addr: Optional[str] = None
    hh: Optional[float] = None
    # The origin this network belongs to. ``cable_layer`` reads MFG_ID on the
    # feeder and garden layers (it plans the shared feeder from the MFG down),
    # so every span carries it rather than only the backbone.
    mfg: Optional[str] = None


def _snap_anchor(sg: StreetGraph, x: float, y: float, tol: float,
                 fallback: float = 250.0) -> Optional[str]:
    """Snap an anchor (MFG / PDP / house) onto a node that can be routed from.

    The nearest node is not necessarily usable: a stray footway island is often
    closer than the real street, and routing from it reaches nothing (PDP00004
    on the Berlin project: 8 m to a 4-node island, 100 m to the real network,
    so its whole polygon was designed as a fragment that touched no corridor).
    Prefer the nearest node of the main component within ``tol``, then widen the
    search to ``fallback`` before giving up.
    """
    key = sg.nearest_node(x, y, tol)
    if key is not None and (not sg.main_component or key in sg.main_component):
        return key
    wider = max(tol, fallback)
    key = sg.nearest_main_node(x, y, wider)
    if key is not None:
        return key
    return sg.nearest_node(x, y, wider)


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
                 params: Params, log
                 ) -> Tuple[Dict[str, Set[Tuple[str, str]]],
                            Dict[Tuple[str, str], Set[int]]]:
    """Per-PDP shortest-path tree to its houses (shared corridors merge).

    Returns ``(per_pdp_edge_sets, edge_houses)``. The second map records, for
    every street edge, the indices of the houses whose PDP→house path walks
    that edge — so a run assembled from those edges can state exactly which
    premises it was dug for (``Run.addr`` / ``Run.hh``). Several houses usually
    share one edge, which is precisely the case a shared trunk exists for.
    """
    by_pdp: Dict[str, List[dict]] = defaultdict(list)
    for h in houses:
        pid = h.get("PDP_ID")
        if pid:
            by_pdp[str(pid)].append(h)

    hidx = {id(h): i for i, h in enumerate(houses)}
    edge_houses: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
    per_pdp: Dict[str, Set[Tuple[str, str]]] = {}
    served = 0
    for p in pdps:
        pid = str(p.get("PDP_ID") or "")
        src = _snap_anchor(sg, p["x"], p["y"], params.pdp_search_m)
        if src is None:
            continue
        targets = []
        for h in by_pdp.get(pid, []):
            k = _snap_anchor(sg, h["x"], h["y"], params.house_search_m)
            if k is not None:
                targets.append((h, k))
        if not targets:
            continue
        try:
            _pred, dist = nx.dijkstra_predecessor_and_distance(sg.G, src, weight="weight")
        except Exception:
            continue
        edges: Set[Tuple[str, str]] = set()
        for h, t in targets:
            if t not in dist:
                continue
            hi = hidx.get(id(h))
            node = t
            guard = 0
            while node != src and guard < 10000:
                guard += 1
                preds = _pred.get(node) or []
                if not preds:
                    break
                pnode = preds[0]
                ek = (node, pnode) if node < pnode else (pnode, node)
                edges.add(ek)
                if hi is not None:
                    edge_houses[ek].add(hi)
                node = pnode
            served += 1
        per_pdp[pid] = edges
    paths = {pid: len(e) for pid, e in per_pdp.items()}
    log(f"spine: {sum(paths.values())} street edge(s) across {len(paths)} PDP(s), "
        f"{served} house(s) routed (edge→house attribution kept)")
    return per_pdp, dict(edge_houses)


def design_garden_legs(network_parts: Sequence[List[Tuple[float, float]]],
                       houses: Sequence[dict], params: Params,
                       log) -> List[dict]:
    """Straight drop leg from every house to the nearest trench point.

    Houses are attached nearest-the-mains first, and each new leg may join an
    **already designed leg**, not only the mains. Two houses 70 m up the same
    street then share one drop trunk (the second branches off the first) instead
    of running two parallel legs: measured on Berlin, drawing each leg straight
    from the mains left **200 m of duplicated drop trench**, e.g. an 85.6 m leg
    lying within 0.6 m of a 66.9 m one for 67 m of its length.

    ``shared_joins`` counts the legs that branched off another leg. Each leg
    records the leg it branched off (``parent``, ``-1`` when it meets the
    mains), because that chain decides the aerial rule in ``_split_drop_legs``.
    """
    legs: List[dict] = []
    parts: List[List[Tuple[float, float]]] = [list(c) for c in network_parts]

    def nearest(x: float, y: float) -> Tuple[float, Optional[Tuple[float, float]], int]:
        best = (float("inf"), None, -1)
        for i, coords in enumerate(parts):
            d, _a, q = _project(coords, x, y)
            if d < best[0]:
                best = (d, q, i)
        return best

    n_mains_parts = len(network_parts)
    order = sorted(houses, key=lambda h: nearest(h["x"], h["y"])[0])
    skipped = shared = 0
    for h in order:
        d, q, part_i = nearest(h["x"], h["y"])
        if q is None:
            skipped += 1
            continue
        # Keep an explicit candidate even when it is outside the normal
        # underground search radius.  The aerial stage needs the house-to-road
        # leg in order to evaluate an unreachable premise; silently dropping it
        # made "unreachable" indistinguishable from "not in the plan".
        unreachable = d > params.house_search_m
        if math.hypot(q[0] - h["x"], q[1] - h["y"]) < 0.5:
            continue
        parent = -1
        if part_i >= n_mains_parts:
            shared += 1                      # it joined a leg, not just the mains
            parent = part_i - n_mains_parts
        length = math.hypot(q[0] - h["x"], q[1] - h["y"])
        coords = [q, (h["x"], h["y"])]
        legs.append({
            "coords": coords,
            "length": length,
            "type": "Garden" if length <= params.max_garden_m else "Open Cut",
            "house": h,
            "parent": parent,
            "unreachable": unreachable,
        })
        parts.append(coords)                 # later houses may share this trunk
    log(f"garden legs: {len(legs)} leg(s), {skipped} house(s) without any network "
        f"point, {sum(1 for leg in legs if leg.get('unreachable'))} beyond the "
        f"underground search radius, {shared} joined another drop instead of the mains")
    return legs


def detect_drills(vehicular: Sequence[Tuple[List[Tuple[float, float]], str]],
                  network: List[Run], params: Params, log) -> List[dict]:
    """Crossings of a designed line over a carriageway → perpendicular drills."""
    veh_geoms = [(_make_line(c), c, cls) for c, cls, _tags in
                 (_part_unpack(p) for p in vehicular) if len(c) >= 2]
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
                # Clamp the bore to this run. When the crossing sits on a run
                # boundary — which happens whenever a junction (and therefore a
                # run break) falls on the carriageway, measured on 37 of 76
                # bores — the bore carries on into the neighbouring run, which
                # detects the same crossing at its own end. So this run
                # publishes only the part of the bore that lies within it, and
                # places a pit only at an end that is really in open trench
                # inside this run: a pit at a shared end would sit in the middle
                # of the carriageway, and a bore drawn past the run end left the
                # pit hanging in mid-air ~3.5 m off the trench.
                total_run = _coords_len(run.coords)
                arc0 = max(0.0, arc - half)
                arc1 = min(total_run, arc + half)
                if arc1 - arc0 < 0.2:
                    continue
                e1 = _point_at_arc(run.coords, arc0)
                e2 = _point_at_arc(run.coords, arc1)
                # the BORE is the straight hole: its length is the chord, not the
                # arc the trench happens to follow between the two pits
                bore_len = math.hypot(e2[0] - e1[0], e2[1] - e1[1])
                if bore_len < 0.2:
                    continue
                drills.append({
                    "coords": [e1, e2], "cls": cls, "width": bore_len,
                    "road_width": road_w, "arc": arc, "arc0": arc0,
                    "arc1": arc1,
                    "pit_start": arc0 > params.pit_edge_m,
                    "pit_end": arc1 < total_run - params.pit_edge_m,
                    "run": id(run),
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


def _point_at_arc(coords: Sequence[Tuple[float, float]],
                  arc: float) -> Tuple[float, float]:
    """The point sitting at an arc-length position along a line."""
    cum = _cum(coords)
    if arc <= 0.0:
        return (coords[0][0], coords[0][1])
    if arc >= cum[-1]:
        return (coords[-1][0], coords[-1][1])
    for i in range(len(cum) - 1):
        if cum[i] <= arc <= cum[i + 1]:
            seg = cum[i + 1] - cum[i]
            t = 0.0 if seg <= 0 else (arc - cum[i]) / seg
            return (coords[i][0] + t * (coords[i + 1][0] - coords[i][0]),
                    coords[i][1] + t * (coords[i + 1][1] - coords[i][1]))
    return (coords[-1][0], coords[-1][1])


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
        ends = ((d["coords"][0], d.get("pit_start", True)),
                (d["coords"][1], d.get("pit_end", True)))
        for pt, has_pit in ends:
            if not has_pit:
                # The bore continues onto the neighbouring run: this end is not
                # a pit, it is open trench meeting a bore mid-carriageway.
                continue
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

    # 3 — splitters. A splitter location OWNS its position: the cabinet must
    #     stand on the chamber where the feeder and the distribution meet. When
    #     the location falls inside another structure's keep-out — an HDD pit
    #     reserves the widest one (hdd_pit_keepout_m, 15 m), so a splitter beside
    #     a drill entry is the common case — that structure MOVES onto the
    #     splitter instead of the splitter being absorbed. The generic dedupe
    #     above used to drop the PDP node here, and the chamber stage's merge
    #     rule then folded the splitter INTO the pit: Berlin PDP00019 ended up
    #     4.50 m from the single chamber built for it, with no duct entering the
    #     cabinet. This is the splitter's civil reality either way — the drill
    #     keeps its bore geometry (the crossing is anchored by the drill spans,
    #     not by the node), only the structure at the bore end nudges along the
    #     trench. The structure keeps its own NODE_TYPE, so the BORE / junction
    #     evidence and the chamber sub-category survive, and records the splitter
    #     it now serves in `ref`.
    #
    #     Two splitters are never merged into each other: a splitter always gets
    #     a node of its own and the chamber rules separate them. Measured on
    #     Berlin: 31 splitters, 31 nodes — 16 placed as their own node, 15
    #     taking over a structure (10 junctions, 5 HDD pits), and no pair of
    #     splitters closer than min_node_sep_m.
    pdp_takeovers = 0
    for p in pdps:
        px, py = p["x"], p["y"]
        pid = str(p.get("PDP_ID") or "")
        host = None
        host_d = None
        for n in nodes:
            if n["NODE_TYPE"] == "PDP":
                continue                      # never merge two splitters
            sep = (params.hdd_pit_keepout_m if n["NODE_TYPE"] == "HDD_PIT"
                   else params.min_node_sep_m)
            d = math.hypot(n["x"] - px, n["y"] - py)
            if d < sep and (host_d is None or d < host_d):
                host, host_d = n, d
        if host is not None:
            host["x"], host["y"] = px, py
            if pid:
                host["ref"] = pid
            pdp_takeovers += 1
            continue
        add(px, py, "PDP", 3, ref=pid)

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

    # 5 — interval pull chambers. The rule is "no chamber-to-chamber gap on a
    #     run may exceed the interval", not "place one at exactly the interval
    #     mark". The old mark-and-test rule skipped a pull whenever any chamber
    #     sat within one interval behind the mark, so a run with a junction at
    #     100 m published **no pull at all**: Berlin had a **329.8 m feeder span
    #     and 0 PULL nodes** with a 250 m interval. Now the chambers already on
    #     the run are sorted and each gap between them is filled.
    for run in network:
        gap = (params.pull_backbone_m if run.tier == "Feeder"
               else params.pull_dist_m)
        cum = _cum(run.coords)
        total = cum[-1]
        if total <= gap:
            continue
        on_run = [0.0, total]
        for n in nodes:
            if n.get("run") != id(run):
                continue
            d, a, _q = _project(run.coords, n["x"], n["y"])
            if d <= 2.0:
                on_run.append(a)
        on_run.sort()
        for a0, a1 in list(zip(on_run, on_run[1:])):
            pos = a0 + gap
            while pos <= a1 - params.min_node_sep_m:
                q = _point_at_arc(run.coords, pos)
                add(q[0], q[1], "PULL", 5, run, arc=pos)
                pos += gap

    if log:
        counts: Dict[str, int] = defaultdict(int)
        for n in nodes:
            counts[n["NODE_TYPE"]] += 1
        log("nodes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        n_pdp = sum(1 for n in nodes if n["NODE_TYPE"] == "PDP")
        if pdp_takeovers or n_pdp != len(pdps):
            log("splitter nodes: %d for %d splitter(s) "
                "(%d took over a nearby structure, %d placed as their own "
                "node)" % (pdp_takeovers + n_pdp, len(pdps), pdp_takeovers,
                           n_pdp))
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
    # A drill interval is one complete civil HDD segment. Do not allow a
    # nearby bend/pull/junction anchor inside that interval to create a short
    # Open Cut "chamber span" before or after the bore. HDD entry/exit
    # boundaries remain authoritative; the chamber layer then places the HDD
    # pits on those exact trench points.
    if type_map:
        protected = []
        for a, n in anchors:
            inside_bore = any(ta + 0.01 < a < tb - 0.01
                              for ta, tb, _tt in type_map)
            if inside_bore and n is not None:
                continue
            protected.append((a, n))
        anchors = protected

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
            # HDD boundaries are real construction boundaries, not ordinary
            # structural nodes. Never swallow a synthetic bore boundary into a
            # nearby bend/junction: doing so leaves a short open-cut "chamber
            # span" before the HDD starts. Keep the boundary unless it is
            # genuinely coincident (drafting noise below 1 cm).
            previous_is_bore_boundary = kept[-1][1] is None
            current_is_bore_boundary = n is None
            if previous_is_bore_boundary != current_is_bore_boundary:
                if abs(kept[-1][0] - a) < 0.01:
                    if n is not None and kept[-1][1] is None:
                        kept[-1] = (kept[-1][0], n)
                    continue
                kept.append((a, n))
                continue
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
        length = _coords_len(coords)
        if ttype == "HDD":
            # A bore is drilled STRAIGHT. Where the run jogs inside the drilled
            # arc (small street-graph vertices, not real bends) the arc is
            # longer than the hole, so an HDD span reports the chord — the
            # drilled length — and the crossing layer and the trench layer then
            # agree exactly (measured: 6 bores drifted 0.5–2.2 m before this).
            length = math.hypot(coords[-1][0] - coords[0][0],
                                coords[-1][1] - coords[0][1])
        spans.append({
            "coords": coords, "start": n0, "end": n1, "arc0": a0, "arc1": a1,
            "length": length, "type": ttype, "tier": run.tier,
            "pdp": run.pdp, "polygon": run.polygon, "src": run.src,
            "infra": run.infra,
            "addr": getattr(run, "addr", None),
            "hh": getattr(run, "hh", None),
            "mfg": getattr(run, "mfg", None),
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
    # Premise attribution (see Run.addr): the address(es) the span serves and
    # their household count. "" when the span serves no premise (pure
    # backbone), so a blank value means "not attributed", not "one house".
    ("ADDR_ID", ogr.OFTString), ("HH", ogr.OFTReal),
    # Origin of the network: cable_layer plans the shared feeder from it.
    ("MFG_ID", ogr.OFTString),
    # AERIAL_ZONE: the span runs through an aerial zone (restricted land where
    # underground is not permitted). This is a CORRIDOR RESTRICTION, not a
    # construction class: per docs/aerial planning.docx feeder and distribution
    # are UG-only (allow_aerial_feeder/distribution = false) and aerial is a
    # DROP-stage decision, so a mains span stays an excavated trench span (its
    # TRENCH_TYPE keeps the real class) and the zone is recorded as evidence for
    # the permit/reroute review. It is deliberately NOT called AERIAL: that name
    # belongs to the construction class on the Aerial_Drops layer, and stamping
    # a feeder span with it is what made an aerial item read as an open-cut
    # feeder trench.
    ("AERIAL_ZONE", ogr.OFTInteger), ("AERIAL_ZONE_REASON", ogr.OFTString),
)
FIELD_AERIAL = (
    ("DROP_ID", ogr.OFTString), ("POLYGON_ID", ogr.OFTString),
    ("ADDR_ID", ogr.OFTString), ("HH", ogr.OFTReal),
    ("TRENCH_TIER", ogr.OFTString), ("TRENCH_TYPE", ogr.OFTString),
    # Aerial is a METHOD, not just a type string: ``CONSTRUCTION_METHOD =
    # "Overhead"`` and ``EXCAVATION = 0`` are what the BOQ, the platform and
    # the LLD read instead of inferring "not dug" from the label.
    ("CONSTRUCTION_METHOD", ogr.OFTString), ("EXCAVATION", ogr.OFTInteger),
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


def _surface_for(tier: str, ttype: str,
                 dominant_class: Optional[str] = None,
                 kerb_offset_m: float = 0.0,
                 edge_tags: Optional[dict] = None
                 ) -> Tuple[str, str]:
    """(SURFACE, REINSTATE) for a span, from construction class + routing
    evidence + construction position.

    ``kerb_offset_m`` is the offset the span's geometry was actually published
    at (``kerb_offset_for(cls)`` per class, or the flat ``params.kerb_offset_m``;
    0 when the kerb band is disabled). A kerb-class span's trench sits in
    whatever cross-section band that offset lands in — by design the middle of
    the footway — so "walked a residential street" only means Asphalt when the
    band was off or the street has no sidewalk to sit on.
    """
    if ttype == "HDD":
        return ("Asphalt", "Full")
    if ttype == "Garden":
        return ("Garden", "Seed")
    cls = (dominant_class or "").strip().lower()
    if cls in PURE_FOOTWAY_CLASSES:
        return ("Footway", "Pavement")
    if cls in KERB_CLASSES:
        if kerb_offset_m <= 0.0:
            # Kerb band disabled: the trench really is in the carriageway.
            return ("Asphalt", "Full")
        tags = edge_tags or {}
        sidewalk = str(tags.get("sidewalk") or "").strip().lower()
        if not sidewalk:
            # Untagged: the kerb band IS the footway band by construction
            # (kerb_offset_for = carr_half + footway inset), so the design
            # intent — and the physical strip the trench is dug in — is the
            # pavement beside the carriageway.
            return ("Footway", "Pavement")
        road = sx.RoadTags.from_osm(cls, tags)
        bounds = sx.cross_section_bounds(road)
        surf = sx.surface_at_offset(bounds, kerb_offset_m, road.surface)
        if surf == sx.SURFACE_FOOTWAY:
            return ("Footway", "Pavement")
        if surf == sx.SURFACE_VERGE:
            return ("Grass", "Seed")
        # Carriageway (flat kerb offset landing inside a wide road), or beyond
        # the bands on a street explicitly tagged sidewalk=no: road
        # restoration is the honest conservative answer.
        return ("Asphalt", "Full")
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
    3. ``chain``  — the leg **branches off an aerial leg**: the drop already
       went aerial further out, and a trench cannot start in mid-air. Legs are
       walked in creation order (nearest the mains first), so a parent is
       always classified before its children.

    An aerial leg is **not** a trench: it is published on the ``Aerial_Drops``
    layer and excluded from every excavated trench output and length.

    Rule 3 exists because rule 1 alone left a hole: on the Berlin project the
    sharing rule rooted a **Garden** leg on an aerial drop, so 18.6 m of open
    trench began 22 m off the mains and its house sat **40.6 m from any trench**
    while still being billed as connected.
    """
    trenched: List[dict] = []
    aerial: List[dict] = []
    aerial_idx: Set[int] = set()
    for i, leg in enumerate(legs):
        item = dict(leg)
        parent = item.get("parent", -1)
        if item.get("unreachable"):
            reason = "unreachable"
        elif _leg_in_zone(zone_polys, item):
            reason = "zone"
        elif params.aerial_max_leg_m and item["length"] > params.aerial_max_leg_m:
            reason = "length"
        elif parent >= 0 and parent in aerial_idx:
            reason = "chain"
        else:
            reason = ""
        if reason:
            item["type"] = "Aerial"
            item["aerial_reason"] = reason
            aerial.append(item)
            aerial_idx.add(i)
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


# ─────────────────────────────────────────────────────────────────────────────
# Trimming: a mains run may only exist where something is connected to it
# ─────────────────────────────────────────────────────────────────────────────

def _leg_attach_points(legs: Sequence[Run],
                       mains: Sequence[Run]) -> List[Tuple[float, float]]:
    """Where every drop **chain** meets the mains (the point service starts at).

    A leg may branch off another leg rather than off the mains (see
    ``design_garden_legs``), so the point that has to stay in the network is the
    one at the root of its chain, not its own free end.
    """
    segs = [(r.coords[i], r.coords[i + 1])
            for r in mains for i in range(len(r.coords) - 1)]
    tol = 2.0

    def mains_dist(p) -> float:
        return min((_point_seg_dist(p[0], p[1], x, y) for x, y in segs), default=1e9)

    # which leg does this point sit on?
    def parent_of(p) -> Optional[int]:
        for i, r in enumerate(legs):
            if any(_point_seg_dist(p[0], p[1], r.coords[j], r.coords[j + 1]) <= tol
                   for j in range(len(r.coords) - 1)):
                return i
        return None

    def resolve(i: int, seen: Set[int]) -> Tuple[float, float]:
        r = legs[i]
        a, b = r.coords[0], r.coords[-1]
        da, db = mains_dist(a), mains_dist(b)
        near_end = a if da <= db else b
        if min(da, db) <= tol:
            return near_end
        # neither end is on the mains: walk up to the leg this one joins
        if i in seen:
            return near_end
        seen.add(i)
        j = parent_of(near_end)
        if j is None or j == i:
            j = parent_of(b if near_end is a else a)
        if j is None or j == i:
            return near_end
        return resolve(j, seen)

    return [resolve(i, set()) for i in range(len(legs))]


def keep_mfg_component(runs: List[Run], mfg: dict, pdps: Sequence[dict],
                       params: Params, log) -> Tuple[List[Run], Dict[str, float]]:
    """Keep only the mains runs that are transitively joined to the MFG.

    Runs are assembled from independent edge sets — the backbone to the PDPs and
    the per-PDP distribution trees — and nothing checks that the result forms one
    network. Measured on the Berlin project: **4 components (93 / 3 / 1 / 1),
    735 m of trench outside the MFG component, with 20 drop legs hanging off
    fragments that lead nowhere.** Those fragments can never carry service (there
    is no route from them to the MFG or a PDP), so they are dropped *before* the
    drop legs are designed — the affected houses then attach to the connected
    network instead of to a dead fragment.

    A PDP that ends up on a dropped fragment is reported: it is unreachable, and
    the backbone did not get there.
    """
    if not runs:
        return [], {"components": 0, "dropped_runs": 0, "dropped_m": 0.0,
                    "unreachable_pdps": 0}
    tol_end = 1.0        # two runs sharing an end
    tol_touch = 1.5      # one run's end landing on another run
    parent = list(range(len(runs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # ends at the same point
    ends: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, r in enumerate(runs):
        for p in (r.coords[0], r.coords[-1]):
            ends[(int(round(p[0] / tol_end)), int(round(p[1] / tol_end)))].append(i)
    for _k, idxs in ends.items():
        for other in idxs[1:]:
            union(idxs[0], other)

    # an end landing on another run (T-join)
    grid = _SegGrid(cell=25.0)
    for i, r in enumerate(runs):
        for j in range(len(r.coords) - 1):
            grid.add(i, r.coords[j], r.coords[j + 1])
    for i, r in enumerate(runs):
        for p in (r.coords[0], r.coords[-1]):
            j = grid.nearest_index(p[0], p[1], tol_touch, exclude=i)
            if j is not None:
                union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(runs)):
        groups[find(i)].append(i)

    # the component the MFG sits on (fall back to the one richest in PDPs)
    def on_run(i: int, x: float, y: float, tol: float) -> bool:
        r = runs[i]
        return any(_point_seg_dist(x, y, r.coords[j], r.coords[j + 1]) <= tol
                   for j in range(len(r.coords) - 1))

    root = None
    for i in range(len(runs)):
        if on_run(i, mfg["x"], mfg["y"], params.pdp_search_m):
            root = find(i)
            break
    if root is None:
        # Deterministic fallback: most PDPs served, ties broken by the group's
        # smallest run index — never by dict iteration order.
        best = (-1, None, 0)
        for r_root, members in groups.items():
            n = sum(1 for p in pdps if any(on_run(i, p["x"], p["y"], params.pdp_search_m)
                                          for i in members))
            low = min(members) if members else 0
            if n > best[0] or (n == best[0] and low < best[2]):
                best = (n, r_root, low)
        root = best[1]

    kept = [runs[i] for i in groups.get(root, [])]
    dropped_idx = [i for i in range(len(runs)) if find(i) != root]
    dropped_m = sum(_coords_len(runs[i].coords) for i in dropped_idx)
    unreachable = sum(1 for p in pdps
                      if not any(on_run(i, p["x"], p["y"], params.pdp_search_m)
                                 for i in groups.get(root, [])))
    if dropped_idx:
        log("network: kept %d run(s) joined to the MFG of %d component(s); dropped "
            "%d run(s) / %.0f m that reach neither the MFG nor a PDP%s"
            % (len(kept), len(groups), len(dropped_idx), dropped_m,
               " - %d PDP unreachable" % unreachable if unreachable else ""))
    return kept, {"components": len(groups), "dropped_runs": len(dropped_idx),
                  "dropped_m": round(dropped_m, 1),
                  "unreachable_pdps": unreachable}


def connect_unreached_pdps(runs: List[Run], pdps: Sequence[dict],
                           params: Params, log,
                           max_gap_m: Optional[float] = None
                           ) -> Tuple[List[Run], Dict[str, float]]:
    """Reach every PDP: a splitter that no trench touches cannot be cabled.

    The backbone routes over the walkable street graph, so a PDP whose nearest
    *routable* node is a distance away ends up beside the network rather than on
    it. A straight spur from the nearest trench point to the splitter closes
    that gap (tier Feeder, ``SRC = pdp-spur``), so the F2D chamber ends up on a
    trench like every other structure.

    ``max_gap_m`` is the largest gap left alone (default ``min_node_sep_m``,
    the distance at which a separate chamber would be silly). The post-trim
    pass passes a tight tolerance instead, because there the question is no
    longer "is another chamber worth it" but "is this cabinet on a trench".
    """
    gap_m = params.min_node_sep_m if max_gap_m is None else max_gap_m
    if not runs or not pdps:
        return list(runs), {"pdp_spurs": 0, "max_spur_m": 0.0}
    segs = [(r.coords[j], r.coords[j + 1])
            for r in runs for j in range(len(r.coords) - 1)]
    out = list(runs)
    spurs, longest = 0, 0.0
    for p in pdps:
        best = (float("inf"), None, None)
        for a, b in segs:
            d = _point_seg_dist(p["x"], p["y"], a, b)
            if d < best[0]:
                dx, dy = b[0] - a[0], b[1] - a[1]
                if dx == 0.0 and dy == 0.0:
                    q = a
                else:
                    t = max(0.0, min(1.0, ((p["x"] - a[0]) * dx + (p["y"] - a[1]) * dy)
                                          / (dx * dx + dy * dy)))
                    q = (a[0] + t * dx, a[1] + t * dy)
                best = (d, q, (a, b))
        d, q, _seg = best
        if q is None or d <= gap_m:
            continue
        out.append(Run(coords=[q, (p["x"], p["y"])], tier="Feeder",
                       pdp=str(p.get("PDP_ID") or ""), polygon=None,
                       src="pdp-spur",
                       mfg=(runs[0].mfg if runs else None)))
        segs.append(((q[0], q[1]), (p["x"], p["y"])))
        spurs += 1
        longest = max(longest, d)
    if spurs:
        log("pdp spurs: %d splitter(s) within %.1f m of a trench were given a spur "
            "(longest %.0f m)" % (spurs, gap_m, longest))
    return out, {"pdp_spurs": spurs, "max_spur_m": round(longest, 1),
                 "gap_m": round(gap_m, 1)}


def connect_unreached_mfg(runs: List[Run], mfg: dict, params: Params,
                          log) -> Tuple[List[Run], Dict[str, float]]:
    """Reach the MFG: the feeder network starts at the cabinet.

    Same defect ``connect_unreached_pdps`` exists for, on the other end of the
    feeder: the router snaps the MFG onto the nearest routable street node, so
    the designed run can end beside the cabinet rather than on it. Measured on
    Berlin: the nearest trench was **1.91 m** from the MFG, and the feeder cable
    and duct inherited that gap. A connector from the nearest network point to
    the MFG closes it, so the feeder trunk physically starts at the cabinet.

    Returns the runs plus ``{"mfg_connected": 0|1, "mfg_gap_m": d}``.
    """
    if not runs or not mfg:
        return list(runs), {"mfg_connected": 0, "mfg_gap_m": 0.0}
    x, y = mfg["x"], mfg["y"]
    segs = [(r.coords[j], r.coords[j + 1])
            for r in runs for j in range(len(r.coords) - 1)]
    best = (float("inf"), None)
    for a, b in segs:
        d = _point_seg_dist(x, y, a, b)
        if d < best[0]:
            dx, dy = b[0] - a[0], b[1] - a[1]
            if dx == 0.0 and dy == 0.0:
                q = a
            else:
                t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy)
                                      / (dx * dx + dy * dy)))
                q = (a[0] + t * dx, a[1] + t * dy)
            best = (d, q)
    d, q = best
    if q is None or d <= params.anchor_touch_m:
        return list(runs), {"mfg_connected": 1 if q is not None else 0,
                            "mfg_gap_m": round(d, 2) if q is not None else 0.0}
    out = list(runs)
    out.append(Run(coords=[q, (x, y)], tier="Feeder",
                   pdp="", polygon=None, src="mfg-connector",
                   mfg=(runs[0].mfg if runs else None)))
    log("mfg connector: MFG was %.2f m off the network - connected "
        "(tolerance %.2f m)" % (d, params.anchor_touch_m))
    return out, {"mfg_connected": 1, "mfg_gap_m": round(d, 2)}


def trim_unserved_tails(mains: List[Run], legs: Sequence[Run],
                        anchors_pts: Sequence[Tuple[float, float]],
                        params: Params, log) -> Tuple[List[Run], Dict[str, float]]:
    """Cut each mains run back to the part something is actually connected to.

    A position on a run is *supported* when a trench is needed there: the MFG or
    a PDP sits on it, a drop leg attaches to it, or another run joins it. Any
    part outside the outermost support is a tail nothing is connected to.

    Why it happens: the distribution spine is routed to the street node nearest
    each house, but the drop leg is then drawn to the nearest point on the whole
    network — often a corridor that already passes the house. The branch that
    was routed for it is left behind, unserved. Measured on the Berlin project:
    **1 015 m of 7 949 m (12.8 %) across 44 of 98 runs**, and it is what the
    "loose ends" report was pointing at (a trench stopping 11–28 m short of the
    nearest premise with nothing attached).

    Supports are read once, from the run set as it stands: because every keeps
    point (a junction, an anchor, a leg attachment) survives in its own run,
    shortening one run can never pull the ground out from under another — and a
    single pass is what keeps that true. (A cascading version was measured to
    orphan **101 drop legs** on the Berlin project, so the invariant is now
    checked explicitly at the end: if any drop leg would lose the mains, the runs
    that carried it are restored and the override is reported.)

    Callers must drop anything not joined to the MFG first
    (``keep_mfg_component``): a fragment a leg attaches to must have been removed
    for the leg to attach to the connected network.
    """
    if not params.trim_tails or not mains:
        return list(mains), {"trimmed_runs": 0, "dropped_runs": 0, "removed_m": 0.0,
                             "restored_runs": 0, "detached_legs": 0}
    keep_anchor_m = 5.0     # MFG / PDP sits on the run
    keep_join_m = 2.0       # another run meets this one
    keep_leg_m = 2.0        # a drop leg attaches here
    leg_pts = _leg_attach_points(legs, mains)
    all_segs = [(r.coords[j], r.coords[j + 1])
                for r in mains for j in range(len(r.coords) - 1)]

    def on_network(p, segs) -> bool:
        return any(_point_seg_dist(p[0], p[1], a, b) <= keep_leg_m for a, b in segs)

    def keeps_for(r: Run, others: Sequence[Run]) -> List[float]:
        pos: List[float] = []
        for p in anchors_pts:
            d, a, _q = _project(r.coords, p[0], p[1])
            if d <= keep_anchor_m:
                pos.append(a)
        for p in leg_pts:
            d, a, _q = _project(r.coords, p[0], p[1])
            if d <= keep_leg_m:
                pos.append(a)
        for o in others:
            for p in (o.coords[0], o.coords[-1]):
                d, a, _q = _project(r.coords, p[0], p[1])
                if d <= keep_join_m:
                    pos.append(a)
        for end_pt, arc_end in ((r.coords[0], 0.0),
                                (r.coords[-1], _coords_len(r.coords))):
            for o in others:
                if any(_point_seg_dist(end_pt[0], end_pt[1], o.coords[k],
                                       o.coords[k + 1]) <= keep_join_m
                       for k in range(len(o.coords) - 1)):
                    pos.append(arc_end)
                    break
        return pos

    keep: List[Run] = []
    trimmed = dropped = restored = 0
    removed_len = 0.0
    for i, r in enumerate(mains):
        others = [o for j, o in enumerate(mains) if j != i]
        total = _coords_len(r.coords)
        pos = keeps_for(r, others)
        lo, hi = (min(pos), max(pos)) if pos else (0.0, 0.0)
        if pos and hi - lo >= total - 0.05:
            keep.append(r)                       # nothing to trim
            continue
        if pos and hi - lo >= 1.0:
            piece = _substring(r.coords, lo, hi)
            if len(piece) >= 2:
                removed_len += total - _coords_len(piece)
                trimmed += 1
                keep.append(Run(coords=piece, tier=r.tier, pdp=r.pdp,
                                polygon=r.polygon, src=r.src,
                                addr=r.addr, hh=r.hh, mfg=r.mfg))
                continue
        # no support, or supported at a single point: the whole run is unused
        if any(on_network(p, [(r.coords[j], r.coords[j + 1])
                              for j in range(len(r.coords) - 1)]) for p in leg_pts):
            keep.append(r)                       # a leg hangs off it: never drop
            restored += 1
            continue
        dropped += 1
        removed_len += total

    # ── stabilise: the supports above were read from the run set as it stood
    # *before* any of it was trimmed, so one run can end up holding a junction
    # whose partner this same pass cut away — a stub with nothing on it
    # (measured: 1 on Berlin, a 6 m feeder tail). Re-trim against the surviving
    # supports until nothing moves; dropping a run can leave another
    # unsupported, so this repeats.
    for _round in range(6):
        changed = False
        survivors: List[Run] = []
        for i, r in enumerate(keep):
            total = _coords_len(r.coords)
            others = [o for j, o in enumerate(keep) if j != i]
            pos = keeps_for(r, others)
            if not pos:
                dropped += 1
                removed_len += total
                changed = True
                continue
            lo, hi = min(pos), max(pos)
            if hi - lo >= total - 0.05:
                survivors.append(r)
                continue
            if hi - lo < 1.0:
                # a single support left: only worth keeping if service hangs
                # off it, and then only as-is (a zero-length piece is nothing)
                if any(on_network(p, [(r.coords[j], r.coords[j + 1])
                                      for j in range(len(r.coords) - 1)])
                       for p in leg_pts):
                    survivors.append(r)
                    restored += 1
                else:
                    dropped += 1
                    removed_len += total
                changed = True
                continue
            piece = _substring(r.coords, lo, hi)
            if len(piece) < 2:
                dropped += 1
                removed_len += total
                changed = True
                continue
            removed_len += total - _coords_len(piece)
            trimmed += 1
            changed = True
            survivors.append(Run(coords=piece, tier=r.tier, pdp=r.pdp,
                                 polygon=r.polygon, src=r.src,
                                 addr=r.addr, hh=r.hh, mfg=r.mfg))
        keep = survivors
        if not changed:
            break

    # invariant: every drop **chain** must still reach the *kept mains*. Legs
    # are NOT network for this test. A leg may branch off another leg (that is
    # the sharing rule), but only through a chain that itself reaches the mains
    # — the roots are resolved against the kept mains, so a chain left hanging
    # on a trimmed run, or on an aerial leg that is no trench at all, is caught
    # (measured on Berlin: 1 house stranded 40.6 m from the network).
    detached: List[Tuple[float, float]] = []
    for _round in range(6):
        roots = _leg_attach_points(legs, keep)
        kept_segs = [(r.coords[j], r.coords[j + 1]) for r in keep
                     for j in range(len(r.coords) - 1)]
        detached = [p for p in roots if not on_network(p, kept_segs)]
        if not detached:
            break
        restored_ids = set()
        for i, r in enumerate(mains):
            if any(r is k for k in keep):
                continue
            mine = [(r.coords[j], r.coords[j + 1]) for j in range(len(r.coords) - 1)]
            if any(on_network(p, mine) for p in detached):
                restored_ids.add(i)
        if not restored_ids:
            break
        for i, r in enumerate(mains):
            if i in restored_ids:
                keep.append(r)
                restored += 1
        removed_len = sum(_coords_len(r.coords) for r in mains) \
            - sum(_coords_len(r.coords) for r in keep)
        log("tails: %d drop chain(s) had no mains left - %d run(s) restored"
            % (len(detached), len(restored_ids)))
    if detached:
        log("tails: WARNING %d drop chain(s) still detached from the mains"
            % len(detached))

    if trimmed or dropped:
        log("tails: trimmed %d run(s), dropped %d unserved run(s) - %.0f m not dug"
            % (trimmed, dropped, removed_len))
    return keep, {"trimmed_runs": trimmed, "dropped_runs": dropped,
                  "removed_m": round(removed_len, 1), "restored_runs": restored,
                  "detached_legs": len(detached)}


def _anchor_points(pdps, houses, mfgs) -> List[Tuple[float, float]]:
    """Every point a trench is allowed to end at (premise, PDP, MFG)."""
    out: List[Tuple[float, float]] = []
    for group in (houses, pdps, mfgs):
        out.extend((float(p["x"]), float(p["y"])) for p in group)
    return out


def _span_groups(span_rows: Sequence[dict], join_tol: float) -> List[int]:
    """Connected-group id per span — geometry, not just shared endpoints.

    Two spans are one group when they share an end, or when one's end lands on
    the other's geometry within ``join_tol`` (a service leg starting mid-span on
    another run). This is the relation the duct router walks, so a group is one
    trench network a duct can be laid along — and a second group is a place the
    duct has to leave the trench to reach.
    """
    tol = 1.0

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
    return [find(i) for i in range(len(span_rows))]


def detached_span_groups(span_rows: Sequence[dict], join_tol: float = 1.5,
                         gap_max_m: float = 200.0) -> List[dict]:
    """Trench groups that are NOT the network — trench the duct cannot reach.

    The published trench is meant to be ONE connected network: every duct,
    cable and chamber rides it, and the pipeline reports "1 connected part".
    The stitch pass, though, only closes ends within ``_STITCH_M`` (0.5 m), so
    a group that stops further short than that is never joined and the map
    quietly holds several networks at once.

    Reported, never repaired: joining two groups means inventing trench the
    design never asked for. On the Berlin reference run (2026-09-21) it reports
    nothing — the stitch pass does close this network — and that is the point of
    having it: the same day's worst-looking trench defect turned out to be the
    duct router, not the trench. The router docked vertices onto passing spans
    and saw the trench as 93 networks, while every one of those pieces touches
    another to within 1 mm (worst 0.07 m, none of them a chamber). A group this
    function DOES report is a real gap, and its cost is real: a distribution
    duct serving a premise on a detached group has no trench to follow across
    the gap, so it crosses on a chord and leaves the trench by exactly that
    distance.

    ``gap_m`` is the distance from the group's own geometry to the nearest
    geometry of any OTHER group, so a group sitting beside a corridor it never
    touches reads as 0 m, not as the distance between two far ends. Each entry
    is ``{spans, length_m, drops, gap_m}``, worst gap first. When no span
    carries ``length_m`` the largest group is taken by vertex count.
    """
    if not span_rows:
        return []
    groups = _span_groups(span_rows, join_tol)
    by_g: Dict[int, List[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        by_g[g].append(i)

    verts: Dict[int, List[Tuple[float, float]]] = {}
    drawn: Dict[int, float] = {}
    for g, idx in by_g.items():
        pts: List[Tuple[float, float]] = []
        for i in idx:
            pts.extend(_span_coords(span_rows[i]))
        verts[g] = pts
        drawn[g] = sum(float(span_rows[i].get("length_m") or 0.0) for i in idx)
    if not verts:
        return []
    if max(drawn.values()) <= 0.0:
        drawn = {g: float(len(pts)) for g, pts in verts.items()}
    main = max(drawn, key=lambda g: drawn[g])

    # One segment grid over the whole design, so the gap search stays local
    # instead of group-by-group. Segments, not vertices: a group whose vertex is
    # 40 m from another group's corridor but whose nearest point is 3 m away is
    # 3 m off it, and only a point-to-segment distance says so.
    cell = 30.0

    def _cells_of(a, b):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for cx in range(int(math.floor(x0 / cell)),
                        int(math.floor(x1 / cell)) + 1):
            for cy in range(int(math.floor(y0 / cell)),
                            int(math.floor(y1 / cell)) + 1):
                yield (cx, cy)

    seg_grid: Dict[Tuple[int, int], List[Tuple[int, Tuple[float, float],
                                                  Tuple[float, float]]]] = defaultdict(list)
    for g, idx in by_g.items():
        for i in idx:
            c = _span_coords(span_rows[i])
            for j in range(len(c) - 1):
                for cellxy in _cells_of(c[j], c[j + 1]):
                    seg_grid[cellxy].append((g, c[j], c[j + 1]))
    reach = int(math.ceil(gap_max_m / cell)) + 1

    out: List[dict] = []
    for g, idx in by_g.items():
        if g == main or not verts[g]:
            continue
        gap = gap_max_m
        for px, py in verts[g]:
            cx, cy = int(math.floor(px / cell)), int(math.floor(py / cell))
            for gx in range(cx - reach, cx + reach + 1):
                for gy in range(cy - reach, cy + reach + 1):
                    for og, a, b in seg_grid.get((gx, gy), ()):
                        if og == g:
                            continue
                        d = _point_seg_dist(px, py, a, b)
                        if d < gap:
                            gap = d
        out.append({"spans": len(idx), "length_m": round(drawn[g], 1),
                    "drops": sum(1 for i in idx
                                 if span_rows[i].get("SRC") == "house-drop"),
                    "gap_m": round(gap, 1)})
    out.sort(key=lambda d: -d["gap_m"])
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
    groups = _span_groups(span_rows, max(1.0, params.prune_join_m))

    def find(i: int) -> int:
        return groups[i]

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

    This says nothing about whether the network is in ONE piece — a drop whose
    end touches another drop is "not loose" here and still severed from the
    mains. ``detached_span_groups`` is the check for that.
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
    osm_sidewalks = [
        (coords, cls) for coords, cls, _tags in
        (_part_unpack(p) for p in walkable)
        if cls in PURE_FOOTWAY_CLASSES
    ]
    routing_walkable = osm_sidewalks if params.sidewalk_only else walkable
    log(f"roads: {len(walkable)} walkable part(s), {len(vehicular)} carriageway part(s), "
        f"{len(osm_sidewalks)} OSM sidewalk/footway part(s) selected for routing")
    if params.sidewalk_only and not osm_sidewalks:
        raise SystemExit("no OSM sidewalk/footway features available for longitudinal trench routing")

    # ── street graph + routing ───────────────────────────────────────────
    # The vehicular layer is intentionally NOT passed to the graph. It is used
    # later by detect_drills() to classify road crossings as HDD. This prevents
    # residential/service carriageways from becoming longitudinal trench
    # carriers while preserving the existing route, node, chamber, duct and
    # cable logic.
    sg = build_street_graph(
        routing_walkable, params,
        anchors=[(p["x"], p["y"]) for p in pdps]
        + [(h["x"], h["y"]) for h in houses]
        + [(m["x"], m["y"]) for m in mfgs])
    log(f"street graph: {sg.G.number_of_nodes()} node(s), {sg.G.number_of_edges()} edge(s)")
    log(f"kerb band: {sg.kerb_parts} carriageway part(s) drawn at the kerb "
        f"(max {sg.kerb_max_offset_m:.1f} m off the centreline); no trench is "
        f"laid down the middle of a road, crossings are drills (HDD)")
    log(f"pavement continuity: {sg.sidewalk_links} break(s) bridged "
        f"({sg.sidewalk_link_m:.0f} m of gap), so the route can stay on the "
        f"pavement instead of stepping onto the carriageway")

    backbone_edges = design_backbone(sg, mfgs[0], pdps, params, log)
    spine_edges, edge_houses = design_spine(sg, pdps, houses, params, log)

    # ── assemble runs (Feeder wins over Distribution where they overlap) ──
    feeder_keys = set(backbone_edges)
    dist_keys = set().union(*spine_edges.values()) if spine_edges else set()
    dist_only = dist_keys - feeder_keys

    runs: List[Run] = []
    # The origin every span belongs to. ``cable_layer`` reads MFG_ID on the
    # feeder and garden layers to plan the shared feeder from the MFG down, so
    # it is stamped on the whole network rather than only on the backbone.
    mfg_id = mfgs[0].get("MFG_ID") or mfgs[0].get("SRC_ID") or None
    pdp_by_key = {}
    for p in pdps:
        k = _snap_anchor(sg, p["x"], p["y"], params.pdp_search_m)
        if k:
            pdp_by_key[k] = p
    for keys, tier, pid in ((feeder_keys, "Feeder", None), (dist_only, "Distribution", None)):
        if not keys:
            continue
        raw_runs, breaks, run_edge_keys = runs_from_edges(keys, sg)
        for coords, ekeys in zip(raw_runs, run_edge_keys):
            straight = _straighten(coords, params)
            # Attach the premises this run was dug for. A shared street run
            # often carries several houses' drops, so the value is the whole
            # set — that is the truth about the span, and it is what lets the
            # cabling stage fan the shared trunk back out per address.
            addr, hh = houses_on_edges(ekeys, edge_houses, houses)
            runs.append(Run(coords=straight, tier=tier, pdp=pid,
                            polygon=None, src="street-graph", edge_keys=ekeys,
                            addr=addr, hh=hh, mfg=mfg_id))
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

    # ── keep only the network that reaches the MFG / its PDPs ────────────
    # Runs come from independent edge sets, so the assembly can leave fragments
    # that no route reaches. They are dropped BEFORE the drops are designed, so
    # a house that would have attached to a dead fragment attaches to the real
    # network instead.
    before = len(runs)
    runs, conn_stats = keep_mfg_component(runs, mfgs[0], pdps, params, log)
    if before != len(runs):
        log("runs after connectivity filter: %d" % len(runs))
    # Every PDP must end up ON a trench, or its splitter cannot be cabled.
    # ANCHOR-EXACT: the tolerance is the physical "touching" distance, not the
    # chamber-separation heuristic. With the default gap a splitter 1.9 m off
    # the network was left alone (<= min_node_sep_m = 10 m), so the trench — and
    # every duct and cable laid in it — stopped short of the splitter: measured
    # on Berlin, MFG 1.91 m and 10 of 31 PDPs 1.2-1.9 m off the trench.
    runs, spur_stats = connect_unreached_pdps(
        runs, pdps, params, log, max_gap_m=params.anchor_touch_m)
    # The MFG is the root of the whole design (feeder cables originate there),
    # so it gets the same treatment as a splitter.
    runs, mfg_conn = connect_unreached_mfg(runs, mfgs[0], params, log)

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
        _house = leg["house"]
        # ── orientation ─────────────────────────────────────────────────
        # The designer builds a leg footway → house (the projection onto the
        # mains is computed first and is the natural start). The published
        # drop trench must run **object → footway**, because ``cable_layer``
        # identifies the shared footway end as the line's LAST point: it groups
        # garden rows by ``coords[-1]`` and matches each one to the
        # distribution row ending at the same footway point. Publishing the
        # house-last order silently made every house its own group, so the
        # shared-trunk logic never fired. The internal working copy stays
        # footway-first (it is what ``_split_drop_legs`` samples), only the
        # published line is reversed.
        r = Run(coords=list(reversed(leg["coords"])), tier="Garden", pdp=None,
                polygon=(_house.get("POLYGON_ID") or None),
                src="house-drop",
                # A drop leg exists for exactly one premise.
                addr=_addr_of(_house), hh=_hh_of(_house), mfg=mfg_id)
        r.tier_type = leg["type"]          # type decided by leg length
        r.footway_pt = leg["coords"][0]    # the mains-side end (authoritative)
        runs.append(r)
        # NOTE: legs are NOT added to ``network_parts`` — a later house must
        # snap to the designed mains, never to another house's drop leg.

    # ── trim the mains back to what is actually connected ────────────────
    # Drops are already fixed, so the run set can be cut without touching the
    # service: only MFG/PDP ends, leg attachments and junctions keep a trench.
    mains_only = [r for r in runs if r.src != "house-drop"]
    drops_only = [r for r in runs if r.src == "house-drop"]
    mains_only, tail_stats = trim_unserved_tails(
        mains_only, drops_only, _anchor_points(pdps, [], mfgs), params, log)

    # ── re-establish "every ANCHOR on a trench" on the FINAL run set ────────
    # The trim cuts a run back to its own supports, so a PDP that was 8 m from
    # the network (closer than ``min_node_sep_m``, hence no spur) can end up
    # beside it with nothing touching — measured on Berlin: PDP00019 left
    # **17.77 m** from the nearest trench, a cabinet that cannot be cabled.
    # The first pass runs before the trim, so the guarantee has to be remade
    # here.
    #
    # TOLERANCE: this used to be 2.0 m, which is why the published network
    # stopped 1.2-1.9 m short of its anchors (MFG 1.91 m, 10 of 31 PDPs) — a
    # "close enough for a chamber" heuristic applied to a question that is
    # really "does the trench touch the cabinet". The tolerance is now the
    # physical touching distance the duct and cable stages club at (0.05 m),
    # so a splitter or cabinet that is 1.9 m off gets a 1.9 m connector and the
    # feeder/distribution trunk physically starts on it.
    mains_only, pdp_spur_stats = connect_unreached_pdps(
        mains_only, pdps, params, log, max_gap_m=params.anchor_touch_m)
    # The MFG is the root of the feeder: same guarantee, same tolerance.
    mains_only, mfg_conn = connect_unreached_mfg(mains_only, mfgs[0], params, log)
    runs = mains_only + drops_only
    log("runs after trimming: %d (%d feeder, %d distribution, %d garden)"
        % (len(runs), sum(1 for r in runs if r.tier == "Feeder"),
           sum(1 for r in runs if r.tier == "Distribution"),
           sum(1 for r in runs if r.tier == "Garden")))

    # ── drills over carriageways ─────────────────────────────────────────
    drills = detect_drills(vehicular, runs, params, log)

    # ── per-run HDD arcs (the drill replaces the crossing length) ─────────
    drill_arcs: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for d in drills:
        arc0, arc1 = d.get("arc0"), d.get("arc1")
        if arc0 is None or arc1 is None:      # older callers without the clamp
            arc = d.get("arc")
            if arc is None:
                continue
            half = d["width"] / 2.0
            arc0, arc1 = arc - half, arc + half
        drill_arcs[d["run"]].append((arc0, arc1))

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
        class_intervals = _run_class_intervals(r, sg)
        r.class_intervals = class_intervals
        for sp in split_spans(r, nodes, params, getattr(r, "type_map", None)):
            dom, dom_tags = _span_surface_context(sp, class_intervals)
            # The offset this span's geometry was published at — the same rule
            # build_street_graph used (per-class band, or the flat override).
            if params.kerb_offset_m > 0.0 and dom in KERB_CLASSES:
                kerb_off = (kerb_offset_for(dom) if params.kerb_offset_per_class
                            else params.kerb_offset_m)
            else:
                kerb_off = 0.0
            sp["surface"], sp["reinstate"] = _surface_for(
                sp["tier"], sp["type"], dom, kerb_off, dom_tags)
            sp["class_intervals"] = class_intervals
            spans.append(sp)
    surf_mix: Dict[str, float] = defaultdict(float)
    for sp in spans:
        surf_mix[sp["surface"]] += sp["length"]
    if surf_mix:
        log("surfaces: " + ", ".join(
            "%s %.0f m" % (k, v)
            for k, v in sorted(surf_mix.items(), key=lambda kv: -kv[1])))

    # node ids
    for i, n in enumerate(nodes):
        n["NODE_ID"] = "TN-%05d" % (i + 1)
    span_rows: List[dict] = []
    for i, sp in enumerate(spans):
        sn = sp["start"]["NODE_ID"] if sp["start"] else ""
        en = sp["end"]["NODE_ID"] if sp["end"] else ""
        if "surface" in sp:
            surf, reinstate = sp["surface"], sp["reinstate"]
        else:
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
            # Premise attribution — which house(s) this chamber-to-chamber
            # span was dug for. A drop leg names one address; a shared spine
            # span names every address that rides it (comma-joined).
            "ADDR_ID": sp.get("addr"), "HH": sp.get("hh"),
            "MFG_ID": sp.get("mfg"),
            "SRC": sp["src"],
            # Corridor restriction evidence (NOT a construction class — see
            # FIELD_LINE): the span is still Open Cut / HDD / Garden.
            "AERIAL_ZONE": 1 if aerial_reason else 0,
            "AERIAL_ZONE_REASON": aerial_reason or None,
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

    # Every duct is laid along the published trench, so it has to be ONE
    # network. The stitch pass closes ends only within _STITCH_M, and a group
    # that stops short of that stays severed — with the distribution duct that
    # serves a premise on it having no trench to follow across the gap.
    detached = detached_span_groups(span_rows)
    if detached:
        log("network: %d trench group(s) detached from the mains - %.0f m of "
            "trench, %d house drop(s), gaps to %.1f m (a duct cannot follow "
            "trench that is not there)"
            % (len(detached), sum(d["length_m"] for d in detached),
               sum(d["drops"] for d in detached),
               max(d["gap_m"] for d in detached)))

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

    # ── premise attribution check ───────────────────────────────────────
    # Every house the design spans should be reachable through a trench that
    # NAMES it: the drop leg carries the address, and the shared spines above
    # it carry the addresses of everything routed along them. A span with no
    # address at all is either a pure backbone connector (legitimate) or a
    # house the attribution pass missed (a bug) — the two are told apart by
    # whether any house drop depends on it, so the count is reported instead
    # of assumed.
    _attributed = [r for r in span_rows if r.get("ADDR_ID")]
    _garden_unattributed = [r for r in span_rows
                            if r["TRENCH_TIER"] == "Garden"
                            and not r.get("ADDR_ID")]
    _hh_billed = sum(float(r.get("HH") or 0.0)
                     for r in span_rows if r["TRENCH_TIER"] == "Garden")
    log("premise attribution: %d/%d span(s) name the address(es) they serve "
        "(%d Garden span(s) without an address, %.0f household(s) on the "
        "drop legs)"
        % (len(_attributed), len(span_rows), len(_garden_unattributed),
           _hh_billed))

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
            "ADDR_ID": _addr_of(leg["house"]), "HH": _hh_of(leg["house"]),
            # An aerial leg replaces the DROP leg it would have been dug as
            # (docs/aerial planning.docx: aerial is only ever evaluated at the
            # customer-connection stage), so its tier is the Drop network —
            # not Garden, which is the excavated micro-trench class.
            "TRENCH_TIER": "Drop", "TRENCH_TYPE": "Aerial",
            # The construction METHOD, so no consumer has to infer "not dug"
            # from the type string alone.
            "CONSTRUCTION_METHOD": "Overhead",
            "EXCAVATION": 0,
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
        # Splitter coverage: every splitter must own a node (and therefore a
        # chamber) — either its own, or by taking over the structure that sat
        # inside its keep-out. Reported so a regression cannot hide, since the
        # failure mode (cabinet left off the chamber, no duct entering it) is
        # invisible in the span counts.
        "splitters": {
            "total": len(pdps),
            "own_node": sum(1 for n in nodes if n["NODE_TYPE"] == "PDP"),
            "took_over_structure": sum(
                1 for n in nodes if n.get("ref") and n["NODE_TYPE"] != "PDP"),
        },
        "carrier_length_by_class_m": {k: round(v, 1)
                                     for k, v in sorted(carrier_mix.items())},
        "carriageway_carrier_m": round(carriage_m, 1),
        "drills": len(drills),
        "garden_legs": len(legs),
        # Which premises each span was dug for (see Run.addr). Reported so a
        # regression in the attribution is visible in the run log rather than
        # only downstream, where the cabling stage silently builds no cable.
        "premise_attribution": {
            "spans_with_address": len(_attributed),
            "spans_total": len(span_rows),
            "garden_spans_without_address": len(_garden_unattributed),
            "garden_legs": len(legs),
            "households_on_drop_legs": round(_hh_billed, 1),
        },
        "aerial_legs": len(aerial_rows),
        "aerial_length_m": round(sum(r["length_m"] for r in aerial_rows), 1),
        "aerial_by_reason": _count_by(
            [{"r": r["AERIAL_REASON"]} for r in aerial_rows], "r"),
        # Corridor restriction count: spans crossing land where underground is
        # not permitted. They are still excavated spans (feeder/distribution are
        # UG-only per docs/aerial planning.docx) — this is permit evidence, not
        # an aerial classification.
        "trench_spans_in_aerial_zone": sum(1 for r in span_rows if r["AERIAL_ZONE"]),
        "pruned_spans": len(pruned_rows),
        "pruned_length_m": round(sum(s["length_m"] for s in pruned_rows), 1),
        "tails": tail_stats,
        "connectivity": conn_stats,
        "pdp_spurs": spur_stats,
        "pdp_spurs_after_trim": pdp_spur_stats,
        "mfg_connected": mfg_conn,
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
    ap.add_argument("--no-trim", action="store_true",
                    help="keep unserved mains tails (default: trim to the last "
                         "leg attachment / anchor / junction)")
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
        "trim_tails": not args.no_trim,
    }
    design(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
