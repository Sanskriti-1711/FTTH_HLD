# -*- coding: utf-8 -*-
"""Aerial feasibility — the shared answer to "can this drop be buried?".

The legacy stage's ``trench_layer._eval_drop_feasibility`` and the designer's
``_split_drop_legs`` both need this decision, so the **rules** live here once,
free of QGIS and GDAL. Geometry is the caller's job: measuring a distance,
testing a crossing and reading a terrain tag all differ between the QGIS stage
and the standalone designer, so each caller works out its facts and this module
applies the same table to them.

Reasons (unchanged from the original scorer):

    ``spare_duct_available``  buried preferred — brownfield duct exists
    ``distance_threshold``    too far from the network for economical UG
    ``prohibited_crossing``   a barrier forbids the crossing
    ``major_road_crossing``   major carriageway — expensive restoration
    ``terrain_constraint``    rocky / waterlogged / protected ground
    ``ug_default``            buried is fine

``no_duct_no_pole_ug_required`` is carried in the original docstring but never
returned by its body; it is not invented here either.

No ML — this is the deterministic scoring model the AI reroute pass (A21) will
one day put learned weights on top of.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

# Roads too expensive to open-cut across: restoration, traffic management and
# reinstatement dominate the cost of a drop that crosses one.
AERIAL_BARRIER_CLASSES = frozenset({
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
})

# Ground a buried drop should not be forced through.
AERIAL_TERRAIN_TYPES = frozenset({
    "rock", "rocky", "scree", "boulder",
    "water", "wetland", "marsh", "swamp", "flood",
    "canal", "ditch",
})

# Default max economical UG drop distance (metres). Rural projects can raise
# it; the designer exposes the same knob as ``Params.max_ug_drop_m``.
MAX_UG_DROP_M_DEFAULT = 300.0

# ─────────────────────────────────────────────────────────────────────────────
# Which OSM landuse classes are grounds for "do not dig here"
# ─────────────────────────────────────────────────────────────────────────────
#
# This is the ONE list that decides it, and both entry points read it:
# ``trench_design._read_polygons_geom`` (the pipeline path) and
# ``web/backend/design.derive_aerial_zones`` (the standalone designer). It
# used to live only in the second, so the pipeline handed the designer the
# whole landuse layer and the designer took every polygon in it as a
# no-excavation zone.
#
# That is not a subtle over-count. In a residential ward the landuse layer is
# mostly `residential`, `retail` and `commercial` — the ground the network
# exists to serve — so on run ab8399fcc (North Edgbaston, 2 620 premises) the
# mask blanketed the AOI and 2 040 of 2 057 drop legs were classified aerial
# for `zone`. Restricted land is a small minority of a ward, so the filter has
# to be an allow-list; a deny-list would eventually include the suburbs.
#
# Roadside classes (grass, scrub, garden, recreation_ground) are deliberately
# absent: in OSM they are mostly verges, and including them blankets the whole
# AOI for the same reason residential did.
RESTRICTED_LANDUSE = frozenset({
    "park", "forest", "nature_reserve", "meadow", "wetland", "wood",
    "farmland", "farm", "orchard", "vineyard", "cemetery", "allotments",
})

# Zones below this are dropped as slivers/mapping noise rather than treated as
# grounds for aerial construction.
MIN_ZONE_AREA_M2 = 2000.0


def is_restricted_landuse(fclass: object) -> bool:
    """Is this OSM ``fclass`` grounds for a no-excavation zone?

    Accepts the raw field value so a caller can pass what OGR handed back
    without pre-cleaning it. An absent/blank/unknown class is never
    restricted — the safe direction is to allow digging, not to invent a zone.
    """
    return str(fclass or "").strip().lower() in RESTRICTED_LANDUSE


def evaluate_drop_feasibility(*,
                              distance_m: Optional[float] = None,
                              road_class: Optional[str] = None,
                              terrain: Optional[str] = None,
                              has_spare_duct: bool = False,
                              crosses_barrier: bool = False,
                              max_ug_drop_m: float = MAX_UG_DROP_M_DEFAULT
                              ) -> Tuple[bool, str]:
    """(buried_ok, reason) for one drop, from facts the caller measured.

    ``distance_m`` is the house-to-network distance, or ``None`` when the
    caller has no network to measure against (the rule is then skipped rather
    than guessed).
    """
    # 1. Existing spare duct → buried preferred
    if has_spare_duct:
        return (True, "spare_duct_available")

    # 2. Distance to the nearest network point
    if distance_m is not None and distance_m > max_ug_drop_m:
        return (False, "distance_threshold")

    # 3. Barrier crossing
    if crosses_barrier:
        return (False, "prohibited_crossing")

    # 4. Major road type → expensive restoration
    if road_class and str(road_class).lower() in AERIAL_BARRIER_CLASSES:
        return (False, "major_road_crossing")

    # 5. Terrain constraint
    if terrain and str(terrain).lower() in AERIAL_TERRAIN_TYPES:
        return (False, "terrain_constraint")

    return (True, "ug_default")


# ── Planar geometry the callers share (the CRS is projected metres) ─────────

def segments_intersect(p1, p2, p3, p4) -> bool:
    """True when segment p1-p2 crosses segment p3-p4 (touching counts)."""

    def _orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def _on_segment(a, b, c) -> bool:
        return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
                and min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)

    o1 = _orient(p1, p2, p3)
    o2 = _orient(p1, p2, p4)
    o3 = _orient(p3, p4, p1)
    o4 = _orient(p3, p4, p2)

    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0:
        return True
    if o1 == 0 and _on_segment(p1, p2, p3):
        return True
    if o2 == 0 and _on_segment(p1, p2, p4):
        return True
    if o3 == 0 and _on_segment(p3, p4, p1):
        return True
    if o4 == 0 and _on_segment(p3, p4, p2):
        return True
    return False


def polyline_crosses(line: Sequence[Tuple[float, float]],
                     other: Sequence[Tuple[float, float]]) -> bool:
    if len(line) < 2 or len(other) < 2:
        return False
    for i in range(len(line) - 1):
        for j in range(len(other) - 1):
            if segments_intersect(line[i], line[i + 1], other[j], other[j + 1]):
                return True
    return False


def crossed_road_class(line: Sequence[Tuple[float, float]],
                       road_parts: Iterable[Sequence],
                       classes: Iterable[str] = AERIAL_BARRIER_CLASSES
                       ) -> Optional[str]:
    """The first barrier-class road ``line`` crosses, or None.

    A drop crossing a major carriageway is the case the scorer calls
    ``major_road_crossing``; a road it merely runs alongside is not a crossing.
    ``road_parts`` items are ``(coords, fclass)`` pairs — or
    ``(coords, fclass, tags)`` triples as ``trench_design._read_road_parts``
    emits them; the extra element is ignored.
    """
    wanted = {c.lower() for c in classes}
    for part in road_parts:
        coords, fclass = part[0], part[1]
        if str(fclass or "").strip().lower() not in wanted:
            continue
        if polyline_crosses(line, coords):
            return str(fclass).strip().lower()
    return None
