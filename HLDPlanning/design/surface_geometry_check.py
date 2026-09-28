# -*- coding: utf-8 -*-
"""Engine-side surface geometry check — does the trench sit where it claims?

The attribute rules in ``fiber-backend/survey/anomaly.py`` can only compare one
captured field against another. This module asks the geometry: **where was the
trench actually drawn?** Every span is classified against the road cross-section
model (``surface_cross_section.classify_line``), and a span is flagged when its
drawn position contradicts the ``SURFACE`` it claims.

It is deterministic geometry — no AI — and it runs as a verification pass at the
end of every run (``utils.attr_enrich.verify_surface_geometry``), the same place
``verify_duct_continuity`` reports on the feeder chain.

Confidence
----------
``classify_line`` returns a confidence per interval (1.0 clearly inside a band,
0.5 past the modelled road edge). This module turns that into two numbers
carried on every flag:

* ``known_share`` — the share of the span's length that landed in a modelled
  band (carriageway / verge / footway) rather than beyond the road edge.
* ``confidence`` — length-weighted mean of the classifier's confidence.

A mismatch is only reported as a **contradiction** when the span sits on the
modelled road for most of its length (``known_share >= MIN_COVERED``); anything
less is counted ``uncertain`` — the honest answer when the geometry is off the
road bands rather than proof of a wrong label.

Coordinates are WGS84 lon/lat, as every published layer is; distances are
converted to metres through a local equirectangular approximation so the
cross-section's metre bands mean what they say.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import surface_cross_section as sx

# Surface families — deliberately mirrored from
# ``fiber-backend/survey/anomaly.py``; the two repos cannot import each other
# and must agree about what "road", "footway" and "garden" mean.
ROAD = "road"
FOOTWAY = "footway"
GARDEN = "garden"

_SURFACE_FAMILY: Dict[str, Tuple[str, ...]] = {
    ROAD: ("asphalt", "road", "carriageway", "tarmac"),
    FOOTWAY: ("footway", "footpath", "sidewalk", "pavement"),
    GARDEN: ("garden", "grass", "lawn", "dirt", "unpaved", "seed"),
}


def _reverse(table: Dict[str, Tuple[str, ...]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for family, words in table.items():
        for w in words:
            out[w] = family
    return out


_SURFACE_LOOKUP = _reverse(_SURFACE_FAMILY)

# A trench rides the kerb band beside its street, not the centreline.
SNAP_M = 40.0
# Most of the span must sit on the modelled road before a mismatch is a fact.
MIN_COVERED = 0.5
# ``classify_line`` sampling step, in metres.
STEP_M = 5.0


# ── Vocabulary ──────────────────────────────────────────────────────────────

def surface_family(value) -> Optional[str]:
    """road / footway / garden for a claimed SURFACE word, else None."""
    if value is None:
        return None
    return _SURFACE_LOOKUP.get(str(value).strip().lower())


def class_family(value) -> Optional[str]:
    """Family of a class the cross-section model produced.

    ``Unknown`` is "off the modelled bands" — no evidence, not a family. A
    named carriageway surface (the model returns the OSM ``surface`` tag in
    place of ``Asphalt``) is a road.
    """
    if value is None:
        return None
    word = str(value).strip().lower()
    if word == "unknown":
        return None
    fam = _SURFACE_LOOKUP.get(word)
    if fam is not None:
        return fam
    return ROAD


def _family_label(family: Optional[str]) -> str:
    return {ROAD: "the carriageway", FOOTWAY: "the footway",
            GARDEN: "the verge/off-road strip"}.get(family, "the road")


# ── Geometry ────────────────────────────────────────────────────────────────

def project_local_m(coords: Sequence[Tuple[float, float]],
                    origin: Tuple[float, float]) -> List[Tuple[float, float]]:
    """WGS84 lon/lat → local metres (equirectangular) about ``origin``."""
    lon0, lat0 = origin
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    return [((float(lon) - lon0) * kx, (float(lat) - lat0) * ky)
            for lon, lat in coords]


def _dist_point_segment(x: float, y: float,
                        a: Tuple[float, float],
                        b: Tuple[float, float]) -> float:
    """Distance from a point to one projected-metre segment."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        return math.hypot(x - ax, y - ay)
    t = ((x - ax) * dx + (y - ay) * dy) / seg2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy))


def _segment_samples(a: Tuple[float, float], b: Tuple[float, float],
                     max_step: float):
    """Yield points no farther than ``max_step`` apart along a segment.

    The grid stores a long segment in cells along its path, rather than filling
    every cell in its potentially huge rectangular envelope. Nearest lookup
    expands its cell search by half a step, so every segment within the requested
    radius is still a candidate; distance is then measured against the exact
    segment, not these samples.
    """
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    count = max(1, int(math.ceil(length / max_step)))
    for i in range(count + 1):
        t = i / count
        yield (a[0] + t * (b[0] - a[0]),
               a[1] + t * (b[1] - a[1]))


class _SegmentRoadIndex:
    """Uniform grid of road segments for local nearest-road lookup.

    Segment samples are spaced by at most half a grid cell, avoiding the large
    empty rectangles a whole-road bbox index creates on long or diagonal roads.
    Each candidate is still checked against the exact segment geometry.
    """

    def __init__(self, roads_m: Sequence[Tuple[str, "sx.RoadTags",
                                               Sequence[Tuple[float, float]]]],
                 cell: float = 100.0):
        self._roads = list(roads_m)
        self._cell = max(float(cell), 1.0)
        self._segments: List[Tuple[int, Tuple[float, float],
                                   Tuple[float, float]]] = []
        self._grid: Dict[Tuple[int, int], List[int]] = {}
        sample_step = self._cell / 2.0
        for road_i, (_rid, _tags, line) in enumerate(self._roads):
            for i in range(len(line) - 1):
                a, b = line[i], line[i + 1]
                if a == b:
                    continue
                segment_i = len(self._segments)
                self._segments.append((road_i, a, b))
                for x, y in _segment_samples(a, b, sample_step):
                    cell_key = (int(x // self._cell), int(y // self._cell))
                    bucket = self._grid.setdefault(cell_key, [])
                    # Several samples from one segment can land in one cell.
                    if not bucket or bucket[-1] != segment_i:
                        bucket.append(segment_i)

    def _candidate_segments(self, x: float, y: float,
                            max_dist_m: float) -> set:
        # The nearest point on a candidate segment can be up to half a sample
        # step from an indexed sample. Search that margin as well as the radius.
        margin = max_dist_m + self._cell / 2.0
        min_cx = int((x - margin) // self._cell)
        max_cx = int((x + margin) // self._cell)
        min_cy = int((y - margin) // self._cell)
        max_cy = int((y + margin) // self._cell)
        candidates = set()
        for cx in range(min_cx, max_cx + 1):
            for cy in range(min_cy, max_cy + 1):
                candidates.update(self._grid.get((cx, cy), ()))
        return candidates

    def nearest(self, x: float, y: float,
                max_dist_m: float = SNAP_M):
        """Nearest road within ``max_dist_m`` as (road_id, tags, line), else None."""
        best = None
        best_d = float(max_dist_m)
        for segment_i in sorted(self._candidate_segments(x, y, max_dist_m)):
            road_i, a, b = self._segments[segment_i]
            d = _dist_point_segment(x, y, a, b)
            if d < best_d:
                best_d = d
                best = self._roads[road_i]
        return best


def _polyline_length_m(poly: Sequence[Tuple[float, float]]) -> float:
    return sum(math.hypot(poly[i + 1][0] - poly[i][0],
                          poly[i + 1][1] - poly[i][1])
               for i in range(len(poly) - 1))


# ── Inputs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Road:
    """A road centreline (WGS84 lon/lat) with the tags that shape its bands."""
    centerline: Sequence[Tuple[float, float]]
    tags: "sx.RoadTags"
    road_id: str = ""


@dataclass(frozen=True)
class Span:
    """One published trench span and the surface it claims."""
    span_id: str
    line: Sequence[Tuple[float, float]]   # WGS84 lon/lat
    surface: str                          # claimed SURFACE value


# The optimized segment grid is defined above, before the classifier. Keep this
# public name for callers and tests.
RoadIndex = _SegmentRoadIndex


# ── The check ───────────────────────────────────────────────────────────────

def check_spans(spans: Iterable[Span], roads: Iterable[Road], *,
                snap_m: float = SNAP_M, min_covered: float = MIN_COVERED,
                step_m: float = STEP_M,
                coordinates_are_projected: bool = False) -> dict:
    """Compare every span's drawn position with the surface it claims.

    Returns::

        {"checked": n, "agreed": n, "uncertain": n, "no_road": n,
         "no_claim": n, "flags": [ {...}, ... ]}

    A flag carries ``span_id``, ``claimed`` / ``claimed_family``,
    ``geometric`` / ``geometric_family``, ``confidence``, ``known_share`` and a
    plain-language ``message``. Never raises — a span it cannot judge is
    counted, not flagged.
    """
    report = {
        "checked": 0, "agreed": 0, "uncertain": 0,
        "no_road": 0, "no_claim": 0, "flags": [],
    }
    spans = list(spans)
    roads = [r for r in roads if r.centerline and len(r.centerline) >= 2]
    if not spans or not roads:
        return report

    # The public geometry-only API defaults to WGS84 lon/lat and projects to a
    # local metric frame. The engine's OGR adapter already reprojects both
    # layers into the trench CRS (EPSG:25833 metres), so it opts out here rather
    # than treating metre coordinates as degrees and scaling them by 111 km.
    if coordinates_are_projected:
        metric = lambda coords: [(float(x), float(y)) for x, y in coords]
    else:
        origin = tuple(spans[0].line[0]) if spans[0].line else tuple(roads[0].centerline[0])
        metric = lambda coords: project_local_m(coords, origin)
    index = RoadIndex([
        (r.road_id, r.tags, metric(r.centerline))
        for r in roads
    ])

    for span in spans:
        if not span.line or len(span.line) < 2:
            continue
        report["checked"] += 1
        claimed = surface_family(span.surface)
        if claimed is None:
            report["no_claim"] += 1
            continue

        line_m = metric(span.line)
        mid = line_m[len(line_m) // 2]
        road = index.nearest(mid[0], mid[1], snap_m)
        if road is None:
            # No road near enough to judge — an off-network drop is expected
            # here, so this is "no evidence", never a contradiction.
            report["no_road"] += 1
            continue

        _rid, tags, road_line = road
        cs = sx.build_cross_section(road_line, tags)
        intervals = sx.classify_line(cs, line_m, step_m=step_m)
        total = sum((s1 - s0) for s0, s1, _c, _f in intervals) or 0.0
        if total <= 0.0:
            report["uncertain"] += 1
            continue

        by_family: Dict[str, float] = {}
        conf_sum = 0.0
        for s0, s1, cls, conf in intervals:
            share = s1 - s0
            fam = class_family(cls)
            if fam is not None:
                by_family[fam] = by_family.get(fam, 0.0) + share
            conf_sum += conf * share
        known = sum(by_family.values())
        known_share = known / total
        confidence = conf_sum / total

        if not by_family or known_share < min_covered:
            # Mostly off the modelled road — the classifier itself is saying
            # "I cannot see the road here", so say nothing.
            report["uncertain"] += 1
            continue

        geometric = max(by_family.items(), key=lambda kv: kv[1])[0]
        if geometric == claimed:
            report["agreed"] += 1
            continue

        report["flags"].append({
            "span_id": span.span_id,
            "claimed": span.surface,
            "claimed_family": claimed,
            "geometric": geometric,
            "geometric_family": geometric,
            "confidence": round(confidence, 2),
            "known_share": round(known_share, 2),
            "message": (
                "span claims %s but %.0f%% of its drawn length sits in %s "
                "(confidence %.2f)"
                % (span.surface, known_share * 100.0, _family_label(geometric),
                   confidence)
            ),
        })
    return report
