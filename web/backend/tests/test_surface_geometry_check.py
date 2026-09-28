"""Tests for the engine-side surface geometry check.

The check asks the geometry where a trench was actually drawn and compares it
with the ``SURFACE`` the span claims, using ``surface_cross_section``'s own
classifier and confidence. Pure Python — no QGIS — so it runs in the normal
backend suite.

Run with:
    python -m pytest web/backend/tests/test_surface_geometry_check.py
"""

from __future__ import annotations

import math
import pathlib
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import surface_cross_section as sx  # noqa: E402
from HLDPlanning.design import surface_geometry_check as sgc  # noqa: E402

BASE_LON, BASE_LAT = 13.4000, 52.5000
_KX = 111320.0 * math.cos(math.radians(BASE_LAT))
_KY = 110540.0


def _ll(east_m: float, north_m: float):
    """Local metres east/north of the base → WGS84 lon/lat."""
    return (BASE_LON + east_m / _KX, BASE_LAT + north_m / _KY)


def _line(*xy):
    return [_ll(e, n) for e, n in xy]


# A residential street whose kerb band is modelled: carriageway to 3.25 m,
# verge to 4.25 m, footway to 6.25 m (half of 6.5 m + 1.0 + 2.0).
ROAD_BOTH = sgc.Road(
    centerline=_line((-20, 0), (80, 0)),
    tags=sx.RoadTags.from_osm("residential", {"sidewalk": "both"}),
    road_id="R-both",
)

# The same street with no footway to sit on — carriageway only.
ROAD_NO_SW = sgc.Road(
    centerline=_line((-20, 0), (80, 0)),
    tags=sx.RoadTags.from_osm("residential", {"sidewalk": "no"}),
    road_id="R-nosw",
)


# ======================================================================
# Agreement
# ======================================================================

def test_span_in_the_carriageway_agrees_with_asphalt():
    span = sgc.Span("TR-1", _line((0, 1.0), (50, 1.0)), "Asphalt")
    rep = sgc.check_spans([span], [ROAD_NO_SW])
    assert rep["checked"] == 1
    assert rep["agreed"] == 1
    assert rep["flags"] == []


def test_kerb_band_span_agrees_with_footway():
    span = sgc.Span("TR-2", _line((0, 4.5), (50, 4.5)), "Footway")
    rep = sgc.check_spans([span], [ROAD_BOTH])
    assert rep["agreed"] == 1
    assert rep["flags"] == []


def test_verge_band_agrees_with_garden():
    # Grass is the verge band — the same family the "Garden" claim names.
    span = sgc.Span("TR-3", _line((0, 3.7), (50, 3.7)), "Garden")
    rep = sgc.check_spans([span], [ROAD_BOTH])
    assert rep["agreed"] == 1
    assert rep["flags"] == []


# ======================================================================
# Contradiction
# ======================================================================

def test_span_in_the_carriageway_claiming_footway_is_flagged():
    span = sgc.Span("TR-4", _line((0, 1.0), (50, 1.0)), "Footway")
    rep = sgc.check_spans([span], [ROAD_NO_SW])
    assert len(rep["flags"]) == 1
    flag = rep["flags"][0]
    assert flag["claimed_family"] == sgc.FOOTWAY
    assert flag["geometric_family"] == sgc.ROAD


def test_kerb_band_span_claiming_asphalt_is_flagged():
    span = sgc.Span("TR-5", _line((0, 4.5), (50, 4.5)), "Asphalt")
    rep = sgc.check_spans([span], [ROAD_BOTH])
    assert len(rep["flags"]) == 1
    assert rep["flags"][0]["geometric_family"] == sgc.FOOTWAY


def test_the_classifier_confidence_travels_on_the_flag():
    span = sgc.Span("TR-6", _line((0, 1.0), (50, 1.0)), "Footway")
    rep = sgc.check_spans([span], [ROAD_NO_SW])
    flag = rep["flags"][0]
    # Every sample landed in a modelled band, at full confidence.
    assert flag["confidence"] == 1.0
    assert flag["known_share"] == 1.0
    assert "carriageway" in flag["message"]


def test_a_multi_band_span_is_judged_by_its_majority_band():
    # Long leg on the footway, short leg cutting back to the carriageway.
    span = sgc.Span("TR-7", _line((0, 4.5), (50, 4.5), (50, 0.5)), "Asphalt")
    rep = sgc.check_spans([span], [ROAD_BOTH])
    assert len(rep["flags"]) == 1
    assert rep["flags"][0]["geometric_family"] == sgc.FOOTWAY


# ======================================================================
# Evidence, not accusation
# ======================================================================

def test_off_road_span_is_no_evidence_not_a_contradiction():
    span = sgc.Span("TR-8", _line((0, 200.0), (50, 200.0)), "Garden")
    rep = sgc.check_spans([span], [ROAD_BOTH])
    assert rep["no_road"] == 1
    assert rep["flags"] == []


def test_mostly_off_the_road_is_uncertain_not_a_contradiction():
    # Runs straight out from the kerb: only the first few metres are on the
    # modelled road, so the classifier cannot honestly call a contradiction.
    span = sgc.Span("TR-9", _line((0, 0.0), (0, 30.0)), "Asphalt")
    rep = sgc.check_spans([span], [ROAD_NO_SW])
    assert rep["uncertain"] == 1
    assert rep["flags"] == []


def test_a_claim_we_cannot_model_is_never_flagged():
    span = sgc.Span("TR-10", _line((0, 1.0), (50, 1.0)), "Concrete")
    rep = sgc.check_spans([span], [ROAD_NO_SW])
    assert rep["no_claim"] == 1
    assert rep["flags"] == []


def test_the_nearest_road_decides_the_bands():
    far_footway = sgc.Road(
        centerline=_line((-20, 30.0), (80, 30.0)),
        tags=sx.RoadTags.from_osm("footway"),
        road_id="F-far",
    )
    # Asphalt is right beside ROAD_NO_SW and wrong against the far footway.
    span = sgc.Span("TR-11", _line((0, 1.0), (50, 1.0)), "Asphalt")
    rep = sgc.check_spans([span], [ROAD_NO_SW, far_footway])
    assert rep["agreed"] == 1
    assert rep["flags"] == []


# ======================================================================
# Vocabulary and safety
# ======================================================================

def test_family_vocabulary_is_shared_with_the_attribute_rules():
    assert sgc.surface_family("Asphalt") == sgc.ROAD
    assert sgc.surface_family("Footpath") == sgc.FOOTWAY
    assert sgc.surface_family("Seed") == sgc.GARDEN
    assert sgc.surface_family(None) is None
    assert sgc.class_family("Unknown") is None
    assert sgc.class_family("Grass") == sgc.GARDEN
    # A named carriageway surface is still a road.
    assert sgc.class_family("Cobblestone") == sgc.ROAD


def test_no_roads_or_no_spans_never_raises():
    span = sgc.Span("TR-12", _line((0, 1.0), (50, 1.0)), "Asphalt")
    assert sgc.check_spans([span], [])["checked"] == 0
    assert sgc.check_spans([], [ROAD_NO_SW])["checked"] == 0


def test_spatial_index_handles_long_diagonal_roads_without_filling_the_bbox():
    # A whole-road bbox grid would have to fill roughly 10,000 x 10,000 cells
    # for this diagonal. Segment indexing follows the geometry instead.
    index = sgc.RoadIndex([
        ("diagonal", ROAD_NO_SW.tags, [(0.0, 0.0), (10000.0, 10000.0)]),
    ])
    assert len(index._grid) < 500
    assert index.nearest(5000.0, 5004.0, max_dist_m=10.0)[0] == "diagonal"


def test_nearest_lookup_only_checks_local_segment_candidates():
    roads = [
        (f"R-{i}", ROAD_NO_SW.tags, [(0.0, i * 100.0), (80.0, i * 100.0)])
        for i in range(1000)
    ]
    index = sgc.RoadIndex(roads)
    candidates = index._candidate_segments(40.0, 1.0, 10.0)
    assert len(candidates) < 10
    assert index.nearest(40.0, 1.0, max_dist_m=10.0)[0] == "R-0"


def test_spatial_index_checks_exact_segment_distance_not_sample_distance():
    index = sgc.RoadIndex([
        ("R-exact", ROAD_NO_SW.tags, [(0.0, 0.0), (100.0, 0.0)]),
    ])
    # Midpoint between indexing samples, still 4 m from the exact road.
    assert index.nearest(24.0, 0.0, max_dist_m=5.0)[0] == "R-exact"
    assert index.nearest(24.0, 6.0, max_dist_m=5.0) is None
