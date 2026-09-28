# -*- coding: utf-8 -*-
"""Lateral road cross-section model for surface attribution.

The trench designer routes on OSM centreline ways and then publishes the trench
on a kerb band. Knowing *where* the published trench sits relative to the road
centreline lets us distinguish:

* carriageway → Asphalt / full road restoration
* grass verge → Grass / seed
* footway     → Footpath / pavement

This module is intentionally geometry-only: it takes a road centreline and OSM
tags, builds a set of lateral bands, and classifies sample points or line
geometries by their signed/absolute offset from the centreline. It does NOT
require AI; it is the deterministic spatial core that any later ML evidence
(photos, aerial imagery) plugs into.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import bisect
# roads when OSM tags are silent; explicit ``width`` / ``lanes`` / ``sidewalk``
# tags override them.
DEFAULT_LANE_WIDTH_M = 3.5
DEFAULT_VERGE_WIDTH_M = 1.0
DEFAULT_FOOTWAY_WIDTH_M = 2.0

# Total carriageway width when no tag is available. Matches the drill-width
# lookup used by the trench designer.
DEFAULT_CARRIAGEWAY_WIDTH_M: Dict[str, float] = {
    "motorway": 14.0, "motorway_link": 6.0,
    "trunk": 12.0, "trunk_link": 6.0,
    "primary": 11.0, "primary_link": 5.0,
    "secondary": 9.0, "secondary_link": 4.0,
    "tertiary": 7.5, "tertiary_link": 4.0,
    "residential": 6.5, "unclassified": 6.0,
    "service": 5.0, "living_street": 5.5, "track": 4.0,
}

# Surface vocabulary used downstream (matches attr_enrich expectations).
SURFACE_CARRIAGEWAY = "Asphalt"
SURFACE_VERGE = "Grass"
SURFACE_FOOTWAY = "Footway"
SURFACE_UNKNOWN = "Unknown"


@dataclass(frozen=True)
class RoadTags:
    """The OSM tags that drive the cross-section."""
    highway: str
    lanes: Optional[int] = None
    sidewalk: Optional[str] = None  # "both", "left", "right"
    width: Optional[float] = None   # total road width in metres
    surface: Optional[str] = None   # raw surface tag, used when available

    @classmethod
    def from_osm(cls, highway: str, tags: Optional[dict] = None
                 ) -> "RoadTags":
        """Build from a raw OSM tag dict (string values, any may be absent)."""
        t = tags or {}
        return cls(highway=highway,
                   lanes=_parse_lanes(t.get("lanes")),
                   sidewalk=t.get("sidewalk"),
                   width=_parse_width(t.get("width")),
                   surface=t.get("surface"))


@dataclass(frozen=True)
class RoadCrossSection:
    """Symmetric lateral bands around a road centreline.

    ``bounds`` lists (distance_from_centreline, surface_class) from the inside
    out. A point whose absolute offset is less than the first bound is on the
    carriageway; between the first and second bound it is on the verge; between
    the second and third it is on the footway; beyond the last bound it is
    unknown.
    """
    centerline: Sequence[Tuple[float, float]]
    bounds: List[Tuple[float, str]]
    surface_tag: Optional[str]


def _parse_lanes(raw) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(str(raw).split(";")[0].split(",")[0])
    except Exception:
        return None


def _parse_width(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    for suffix in ("m", " metres", " meters"):
        if s.lower().endswith(suffix):
            s = s[:-len(suffix)].strip()
    try:
        return float(s)
    except Exception:
        return None


def _total_carriageway_width_m(tags: RoadTags) -> float:
    """Best-effort total carriageway width (both directions)."""
    if tags.width is not None:
        return tags.width
    lanes = _parse_lanes(tags.lanes)
    if lanes is not None and lanes > 0:
        return lanes * DEFAULT_LANE_WIDTH_M
    return DEFAULT_CARRIAGEWAY_WIDTH_M.get(tags.highway, 6.0)


def build_cross_section(centerline: Sequence[Tuple[float, float]],
                        tags: RoadTags,
                        verge_width_m: float = DEFAULT_VERGE_WIDTH_M,
                        footway_width_m: float = DEFAULT_FOOTWAY_WIDTH_M
                        ) -> RoadCrossSection:
    """Build symmetric lateral bands for a road segment."""
    return RoadCrossSection(centerline=tuple(centerline),
                            bounds=cross_section_bounds(tags, verge_width_m,
                                                        footway_width_m),
                            surface_tag=tags.surface)


def cross_section_bounds(tags: RoadTags,
                         verge_width_m: float = DEFAULT_VERGE_WIDTH_M,
                         footway_width_m: float = DEFAULT_FOOTWAY_WIDTH_M
                         ) -> List[Tuple[float, str]]:
    """Pure band boundaries for a road's tags — no geometry needed.

    For pure pedestrian ways (``highway=footway`` etc.) the entire band is
    footway. For carriageways, bands are carriageway → verge → footway when a
    sidewalk is tagged, otherwise just carriageway.
    """
    hwy = (tags.highway or "").lower()
    pure_pedestrian = hwy in ("footway", "path", "pedestrian", "sidewalk",
                              "cycleway", "steps", "bridleway")
    has_sidewalk = tags.sidewalk in ("both", "left", "right", "yes",
                                     "separate")

    bounds: List[Tuple[float, str]] = []

    if pure_pedestrian:
        # The whole way is a pedestrian corridor; any point on it is footway.
        width = _total_carriageway_width_m(tags)
        bounds.append((width / 2.0, SURFACE_FOOTWAY))
    else:
        carr_half = _total_carriageway_width_m(tags) / 2.0
        bounds.append((carr_half, SURFACE_CARRIAGEWAY))
        if has_sidewalk:
            bounds.append((carr_half + verge_width_m, SURFACE_VERGE))
            bounds.append((carr_half + verge_width_m + footway_width_m,
                           SURFACE_FOOTWAY))
    return bounds


def surface_at_offset(bounds: Sequence[Tuple[float, str]],
                      offset_m: float,
                      surface_tag: Optional[str] = None
                      ) -> str:
    """Surface class at an absolute lateral offset from the centreline.

    Pure interval lookup — the geometry-free path used by the trench designer,
    which already knows the kerb-band offset its geometry was published at.
    Beyond the last band → ``SURFACE_UNKNOWN``.
    """
    cls = SURFACE_UNKNOWN
    for bound, band_cls in bounds:
        if offset_m <= bound:
            cls = band_cls
            break
    if cls == SURFACE_CARRIAGEWAY and surface_tag:
        cls = surface_tag.title()
    return cls


def _nearest_on_polyline(poly: Sequence[Tuple[float, float]], x: float, y: float
                         ) -> Tuple[Tuple[float, float], float, float]:
    """Nearest point on ``poly`` to ``(x, y)``.

    Returns ``(nearest_point, distance_to_nearest, arc_along_poly)``.
    """
    best_pt = poly[0]
    best_d2 = float("inf")
    best_arc = 0.0
    arc = 0.0
    for i in range(len(poly) - 1):
        x1, y1 = poly[i]
        x2, y2 = poly[i + 1]
        dx, dy = x2 - x1, y2 - y1
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0.0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / seg_len2))
        nx = x1 + t * dx
        ny = y1 + t * dy
        d2 = (x - nx) ** 2 + (y - ny) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_pt = (nx, ny)
            best_arc = arc + t * math.sqrt(seg_len2)
        arc += math.sqrt(seg_len2)
    return best_pt, math.sqrt(best_d2), best_arc


def _left_normal(poly: Sequence[Tuple[float, float]], idx: int
                 ) -> Tuple[float, float]:
    """Unit normal pointing to the left of the polyline at ``idx``."""
    n = len(poly)
    if n < 2:
        return (0.0, 0.0)
    x0, y0 = poly[max(0, idx - 1)]
    x1, y1 = poly[min(n - 1, idx + 1)]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0.0:
        return (0.0, 0.0)
    # left normal of (dx, dy) is (-dy, dx)
    return (-dy / length, dx / length)


def classify_point(cs: RoadCrossSection, x: float, y: float
                   ) -> Tuple[str, float]:
    """Classify a point against the cross-section.

    Returns ``(surface_class, confidence)``. Confidence is 1.0 when the point
    clearly lies inside a band, 0.5 when it sits beyond the last band (the road
    edge is poorly defined), and 0.0 when the nearest centreline point is far
    away (point is off-road).
    """
    if len(cs.centerline) < 2:
        return (SURFACE_UNKNOWN, 0.0)

    (nx, ny), distance, _arc = _nearest_on_polyline(cs.centerline, x, y)
    # Very far from the road centreline → not on this road.
    if distance > 30.0:
        return (SURFACE_UNKNOWN, 0.0)

    # Decide which band the absolute offset falls into.
    cls = surface_at_offset(cs.bounds, distance, cs.surface_tag)
    confidence = 1.0 if cls != SURFACE_UNKNOWN else 0.5
    return (cls, confidence)


def _cum(coords: Sequence[Tuple[float, float]]) -> List[float]:
    out = [0.0]
    for i in range(len(coords) - 1):
        x0, y0 = coords[i]
        x1, y1 = coords[i + 1]
        out.append(out[-1] + math.hypot(x1 - x0, y1 - y0))
    return out


def classify_line(cs: RoadCrossSection,
                  line: Sequence[Tuple[float, float]],
                  step_m: float = 5.0
                  ) -> List[Tuple[float, float, str, float]]:
    """Classify a line geometry into surface intervals along its length.

    Returns ``(arc_start, arc_end, surface_class, mean_confidence)`` for each
    contiguous run of the same class.
    """
    if len(line) < 2 or len(cs.centerline) < 2:
        return []

    total = _cum(line)[-1]
    if total <= 0.0:
        return []

    samples: List[Tuple[float, str, float]] = []
    n = max(2, int(math.ceil(total / step_m)) + 1)
    cum = _cum(line)
    for i in range(n):
        arc = total * i / (n - 1)
        # Interpolate point at arc.
        j = max(0, min(len(cum) - 2, bisect.bisect_right(cum, arc) - 1))
        seg_len = cum[j + 1] - cum[j]
        if seg_len <= 0.0:
            t = 0.0
        else:
            t = (arc - cum[j]) / seg_len
        x0, y0 = line[j]
        x1, y1 = line[j + 1]
        x = x0 + t * (x1 - x0)
        y = y0 + t * (y1 - y0)
        cls, conf = classify_point(cs, x, y)
        samples.append((arc, cls, conf))

    # Merge contiguous same-class samples.
    intervals: List[Tuple[float, float, str, float]] = []
    s0, c0, conf0 = samples[0]
    acc_conf = conf0
    count = 1
    for arc, cls, conf in samples[1:]:
        if cls == c0:
            acc_conf += conf
            count += 1
            continue
        intervals.append((s0, arc, c0, acc_conf / count))
        s0, c0, conf0 = arc, cls, conf
        acc_conf, count = conf, 1
    intervals.append((s0, total, c0, acc_conf / count))
    return intervals
