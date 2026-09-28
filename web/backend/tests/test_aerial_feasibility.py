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
