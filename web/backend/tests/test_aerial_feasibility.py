"""Unit tests for the shared aerial-feasibility decision table.

``HLDPlanning/design/aerial_feasibility.py`` holds the rules both engines
consult when they decide whether a house drop may be buried: the legacy QGIS
stage (``algorithms/trench_layer._eval_drop_feasibility``) and the standalone
designer (``design/trench_design._drop_feasibility_reason``). The module is
pure Python — no QGIS, no GDAL — so the table and its planar crossing
geometry are tested here directly.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_aerial_feasibility.py -v
"""

import pathlib
import sys

import pytest

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import aerial_feasibility as af  # noqa: E402


# ── the decision table ──────────────────────────────────────────────────────

def test_buried_is_the_default_when_nothing_argues_against_it():
    assert af.evaluate_drop_feasibility() == (True, "ug_default")
    assert af.evaluate_drop_feasibility(
        distance_m=50.0, road_class="residential", terrain="sand",
    ) == (True, "ug_default")


def test_spare_duct_beats_every_negative_fact():
    # long, across a major road, on rock — a brownfield duct wins anyway
    assert af.evaluate_drop_feasibility(
        distance_m=5000.0, road_class="primary", terrain="rock",
        has_spare_duct=True, crosses_barrier=True,
    ) == (True, "spare_duct_available")


def test_distance_threshold_uses_max_ug_drop_m():
    assert af.evaluate_drop_feasibility(
        distance_m=301.0) == (False, "distance_threshold")
    assert af.evaluate_drop_feasibility(
        distance_m=300.0) == (True, "ug_default")     # at the limit is fine
    assert af.evaluate_drop_feasibility(
        distance_m=80.0, max_ug_drop_m=50.0) == (False, "distance_threshold")


def test_no_measured_distance_skips_the_distance_rule():
    # a caller with no network to measure against must not guess
    assert af.evaluate_drop_feasibility(
        distance_m=None) == (True, "ug_default")


def test_prohibited_crossing_beats_major_road_crossing():
    assert af.evaluate_drop_feasibility(
        crosses_barrier=True, road_class="primary",
    ) == (False, "prohibited_crossing")


@pytest.mark.parametrize("road_class", [
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
])
def test_major_road_crossing_for_every_barrier_class(road_class):
    assert af.evaluate_drop_feasibility(
        road_class=road_class) == (False, "major_road_crossing")


@pytest.mark.parametrize("road_class", [
    "residential", "tertiary", "service", "unclassified", "living_street",
    "track", "footway", "",
])
def test_minor_roads_do_not_block_burial(road_class):
    assert af.evaluate_drop_feasibility(
        road_class=road_class) == (True, "ug_default")


@pytest.mark.parametrize("terrain", ["rock", "ROCK", "wetland", "water", "marsh"])
def test_terrain_constraint_for_unbuildable_ground(terrain):
    assert af.evaluate_drop_feasibility(
        terrain=terrain) == (False, "terrain_constraint")


def test_class_matching_is_case_insensitive():
    assert af.evaluate_drop_feasibility(
        road_class="PRIMARY") == (False, "major_road_crossing")


# ── planar crossing geometry ────────────────────────────────────────────────

def test_segments_intersect_crossing_and_disjoint():
    assert af.segments_intersect((0, -1), (0, 1), (-1, 0), (1, 0)) is True
    assert af.segments_intersect((0, -2), (0, -1), (-1, 0), (1, 0)) is False
    assert af.segments_intersect((0, -1), (0, 1), (1, -1), (1, 1)) is False  # parallel


def test_segments_intersect_touching_counts():
    # a leg that meets the road at a T-junction still "crosses" it
    assert af.segments_intersect((0, -1), (0, 0), (-1, 0), (1, 0)) is True


def test_polyline_crosses_needs_real_geometry():
    assert af.polyline_crosses([(0, -2), (0, 2)], [(-10, 0), (10, 0)]) is True
    assert af.polyline_crosses([(0, 2), (0, 4)], [(-10, 0), (10, 0)]) is False
    assert af.polyline_crosses([(0, 0)], [(-10, 0), (10, 0)]) is False     # < 2 pts
    assert af.polyline_crosses([(0, -2), (0, 2)], [(0, 0)]) is False


def test_polyline_crosses_works_across_several_segments():
    # dog-leg whose second segment crosses the road
    assert af.polyline_crosses([(0, 5), (20, 5), (20, -5)],
                               [(30, 0), (-30, 0)]) is True


# ── crossed_road_class ──────────────────────────────────────────────────────

def test_crossed_road_class_finds_a_crossing_not_a_parallel_run():
    parts = [([(-50, 0), (50, 0)], "primary", {})]
    assert af.crossed_road_class([(0, -10), (0, 10)], parts) == "primary"
    # runs 5 m alongside the same road — not a crossing
    assert af.crossed_road_class([(0, 5), (50, 5)], parts) is None


def test_crossed_road_class_ignores_non_barrier_roads():
    parts = [([(-50, 0), (50, 0)], "residential", {}),
             ([(-50, 10), (50, 10)], "service", {})]
    assert af.crossed_road_class([(0, -20), (0, 30)], parts) is None


def test_crossed_road_class_accepts_pairs_and_tagged_triples():
    pair = [([(-50, 0), (50, 0)], "primary")]
    triple = [([(-50, 0), (50, 0)], "primary", {"surface": "asphalt"})]
    assert af.crossed_road_class([(0, -10), (0, 10)], pair) == "primary"
    assert af.crossed_road_class([(0, -10), (0, 10)], triple) == "primary"


def test_crossed_road_class_returns_the_first_barrier_found():
    parts = [([(-50, 10), (50, 10)], "secondary", {}),
             ([(-50, -10), (50, -10)], "primary", {})]
    assert af.crossed_road_class([(0, -20), (0, 20)], parts) == "secondary"


# ── crossing_reason / crossed_carriageway (docs/aerial planning.docx) ────────
#
# The docx's drop decision chain makes "road crossing required" and "driveway
# cutting required" their own aerial triggers, separate from the barrier set
# ``crossed_road_class`` covers.

def test_crossing_reason_names_the_two_docx_triggers():
    assert af.crossing_reason("residential") == "road_crossing"
    assert af.crossing_reason("PRIMARY") == "road_crossing"
    assert af.crossing_reason("service") == "driveway_crossing"
    assert af.crossing_reason("track") == "driveway_crossing"


def test_crossing_reason_is_empty_for_pavements_and_junk():
    for cls in ("footway", "path", "pedestrian", "cycleway", "steps",
                "bridleway", "sidewalk", "", None, "  "):
        assert af.crossing_reason(cls) == "", cls


def test_crossed_carriageway_reports_class_and_reason():
    parts = [([(-50, 0), (50, 0)], "residential", {})]
    assert af.crossed_carriageway([(0, -10), (0, 10)], parts) == (
        "residential", "road_crossing")
    service = [([(-50, 0), (50, 0)], "service", {})]
    assert af.crossed_carriageway([(0, -10), (0, 10)], service) == (
        "service", "driveway_crossing")


def test_crossed_carriageway_ignores_parallel_pavements_and_short_lines():
    parts = [([(-50, 0), (50, 0)], "residential", {}),
             ([(0, 4), (50, 4)], "footway", {})]
    assert af.crossed_carriageway([(0, 5), (50, 5)], parts) is None
    assert af.crossed_carriageway([(0, 5)], parts) is None


def test_a_public_road_outranks_a_driveway_whatever_the_read_order():
    road = [([(-50, 0), (50, 0)], "residential", {})]
    drive = [([(-50, 5), (50, 5)], "service", {})]
    line = [(0, -10), (0, 20)]
    assert af.crossed_carriageway(line, drive + road) == (
        "residential", "road_crossing")
    assert af.crossed_carriageway(line, road + drive) == (
        "residential", "road_crossing")


def test_each_trigger_can_be_switched_off_independently():
    road = [([(-50, 0), (50, 0)], "residential", {})]
    drive = [([(-50, 5), (50, 5)], "service", {})]
    line = [(0, -10), (0, 20)]
    assert af.crossed_carriageway(line, road, road=False) is None
    assert af.crossed_carriageway(line, road, driveway=False) == (
        "residential", "road_crossing")
    # a disabled category must not hide the other one
    assert af.crossed_carriageway(line, drive + road, road=False) == (
        "service", "driveway_crossing")
    assert af.crossed_carriageway(line, drive + road,
                                 road=False, driveway=False) is None


# ── the rest of the docx drop chain (setback, pole, length, density, permit) ─
#
# docs/aerial planning.docx prints the drop decision as a chain and names a
# switch per step; the §4 business triggers sit beside it. The rules are applied
# in one place, so they are tested in one place.

def test_the_docx_chain_limits_are_the_docx_values():
    assert af.SETBACK_TRIGGER_M == 20.0
    assert af.EXISTING_POLE_TRIGGER_M == 50.0
    assert af.TRENCH_LENGTH_URBAN_M == 100.0
    assert af.TRENCH_LENGTH_RURAL_M == 300.0
    assert af.DENSITY_MIN_HOMES_PER_KM == 20.0
    assert af.PERMIT_DELAY_TRIGGER_DAYS == 60.0


def test_crossing_flags_report_each_category_separately():
    road = [([(-50, 0), (50, 0)], "residential", {})]
    drive = [([(-50, 5), (50, 5)], "service", {})]
    line = [(0, -10), (0, 20)]
    assert af.crossing_flags(line, road) == (True, False)
    assert af.crossing_flags(line, drive) == (False, True)
    assert af.crossing_flags(line, road + drive) == (True, True)
    # a disabled category is not reported …
    assert af.crossing_flags(line, road, road=False) == (False, False)
    # … and must not hide the other one
    assert af.crossing_flags(line, road + drive, road=False) == (False, True)
    assert af.crossing_flags(line, drive, driveway=False) == (False, False)
    assert af.crossing_flags([(0, 0)], road) == (False, False)


def test_nearest_street_m_measures_to_the_segment_not_the_vertex():
    parts = [([(0, 0), (100, 0)], "residential", {})]
    # mid-segment and at the end vertex: both 7 m from the line
    assert af.nearest_street_m((50, 7), parts) == pytest.approx(7.0)
    assert af.nearest_street_m((0, 7), parts) == pytest.approx(7.0)
    # no road geometry at all is "not measured", not a 0 m setback
    assert af.nearest_street_m((0, 7), []) is None


def test_street_length_within_m_clips_the_segment_at_the_circle():
    run = [([(-100, 0), (100, 0)], "residential", {})]
    # a 30 m circle around the origin keeps 60 m of the 200 m segment
    assert af.street_length_within_m((0, 0), run, 30.0) == pytest.approx(60.0)
    assert af.street_length_within_m((0, 500), run, 30.0) == pytest.approx(0.0)


def test_density_per_km_counts_the_homes_inside_the_radius_only():
    homes = [{"x": 1, "y": 1}, {"x": 900, "y": 900}]
    run = [([(-50, 0), (50, 0)], "residential", {})]
    # 100 m of street inside the 250 m circle → 1 home / 0.1 km = 10 homes/km
    assert af.density_per_km((0, 0), homes, run, 250.0) == pytest.approx(10.0)
    # no street to hang customers off is not a dense street
    assert af.density_per_km((0, 0), homes, [], 250.0) is None


def test_the_chain_reports_the_steps_in_the_docx_order():
    assert af.drop_chain_reason(setback_m=25.0, road_crossing=True,
                               nearest_pole_m=10.0) == "setback"
    assert af.drop_chain_reason(road_crossing=True,
                               driveway_crossing=True) == "road_crossing"
    assert af.drop_chain_reason(driveway_crossing=True,
                               nearest_pole_m=10.0) == "driveway_crossing"
    assert af.drop_chain_reason(nearest_pole_m=50.0) == "existing_pole"
    assert af.drop_chain_reason(nearest_pole_m=50.1) == ""
    assert af.drop_chain_reason(setback_m=5.0, nearest_pole_m=400.0) == ""


def test_each_chain_step_can_be_switched_off():
    assert af.drop_chain_reason(setback_m=25.0, setback_trigger=False) == ""
    assert af.drop_chain_reason(nearest_pole_m=1.0, pole_trigger=False) == ""
    assert af.drop_chain_reason(trench_m=150.0, trench_trigger=False) == ""
    assert af.drop_chain_reason(density_km=1.0, density_trigger=False) == ""
    assert af.drop_chain_reason(permit_delay_days=90.0,
                               permit_trigger=False) == ""


def test_the_business_triggers_use_their_own_limits():
    assert af.drop_chain_reason(trench_m=150.0,
                               trench_trigger=True) == "trench_length"
    # 150 m is over the urban limit and under the rural one
    assert af.drop_chain_reason(trench_m=150.0, urban=False,
                               trench_trigger=True) == ""
    assert af.drop_chain_reason(trench_m=400.0, urban=False,
                               trench_trigger=True) == "trench_length"
    assert af.drop_chain_reason(density_km=19.9,
                               density_trigger=True) == "low_density"
    assert af.drop_chain_reason(density_km=20.0, density_trigger=True) == ""
    assert af.drop_chain_reason(permit_delay_days=61.0,
                               permit_trigger=True) == "permit_delay"
    assert af.drop_chain_reason(permit_delay_days=60.0,
                               permit_trigger=True) == ""


def test_an_unmeasured_fact_is_skipped_not_treated_as_a_hit():
    # no road, no pole layer, no street length, no permit register
    assert af.drop_chain_reason(
        setback_m=None, nearest_pole_m=None, trench_m=None, density_km=None,
        permit_delay_days=None, trench_trigger=True, density_trigger=True,
        permit_trigger=True) == ""
