"""MFG service areas are sized by premise load and road reach, not polygon count."""

from __future__ import annotations

import pytest

from HLDPlanning.utils.mfg_partition import partition_mfg_service_areas


def _line_distances(nodes: list[str], positions: list[float]) -> dict[tuple[str, str], float]:
    return {
        (left, right): abs(x - y)
        for left, x in zip(nodes, positions)
        for right, y in zip(nodes, positions)
    }


def test_many_small_polygons_share_one_mfg_when_load_and_road_reach_allow():
    """Polygon count does not force extra MFGs when a single catchment fits."""
    nodes = [f"poly-{i:02d}" for i in range(30)]
    loads = {node: 100 for node in nodes}
    positions = [i * 50.0 for i in range(len(nodes))]

    areas = partition_mfg_service_areas(
        loads, _line_distances(nodes, positions),
        min_hh=2000, target_hh=3000, max_hh=4000, max_road_m=3000,
    )

    assert len(areas) == 1
    assert len(areas[0]["polygon_keys"]) == 30
    assert areas[0]["hh_count"] == 3000
    assert areas[0]["range_m"] <= 3000
    assert areas[0]["review"] == 0


def test_target_capacity_does_not_split_an_area_below_the_hard_maximum():
    """The 3,000 target cannot create an extra MFG if 4,000 capacity fits."""
    nodes = [f"poly-{i:02d}" for i in range(35)]
    loads = {node: 100 for node in nodes}
    positions = [i * 50.0 for i in range(len(nodes))]

    areas = partition_mfg_service_areas(
        loads, _line_distances(nodes, positions),
        min_hh=2000, target_hh=3000, max_hh=4000, max_road_m=3000,
    )

    assert len(areas) == 1
    assert areas[0]["hh_count"] == 3500
    assert areas[0]["review"] == 0


def test_three_km_is_measured_from_the_mfg_anchor_not_between_premises():
    """Premises 4 km apart can share one MFG when both are within 3 km of it."""
    nodes = ["west", "center", "east"]
    loads = {"west": 600, "center": 1800, "east": 600}
    positions = [0.0, 2000.0, 4000.0]

    areas = partition_mfg_service_areas(
        loads, _line_distances(nodes, positions),
        min_hh=2000, target_hh=3000, max_hh=4000, max_road_m=3000,
    )

    assert len(areas) == 1
    assert areas[0]["hh_count"] == 3000
    assert areas[0]["seed_polygon"] == "center"
    assert areas[0]["range_m"] == 2000


def test_hard_capacity_and_road_range_split_independent_of_polygon_boundaries():
    """Two MFG areas result only when capacity or road reach requires it."""
    nodes = [f"poly-{i:02d}" for i in range(50)]
    loads = {node: 100 for node in nodes}
    positions = [i * 100.0 for i in range(len(nodes))]

    areas = partition_mfg_service_areas(
        loads, _line_distances(nodes, positions),
        min_hh=2000, target_hh=3000, max_hh=4000, max_road_m=3000,
    )

    assert len(areas) == 2
    assert sum(area["hh_count"] for area in areas) == 5000
    assert all(area["hh_count"] <= 4000 for area in areas)
    assert all(area["range_m"] <= 3000 for area in areas)


def test_unreachable_roads_form_reviewable_areas_not_false_shortcuts():
    """Disconnected service components remain separate and are not cross-routed."""
    loads = {"west-a": 1200, "west-b": 1100, "east-a": 900}
    distances = {
        ("west-a", "west-b"): 900,
        ("west-b", "west-a"): 900,
        ("east-a", "east-a"): 0,
    }

    areas = partition_mfg_service_areas(
        loads, distances, min_hh=2000, target_hh=3000,
        max_hh=4000, max_road_m=3000,
    )

    assert len(areas) == 2
    assert {tuple(area["polygon_keys"]) for area in areas} == {
        ("west-a", "west-b"), ("east-a",)
    }
    east = next(area for area in areas if "east-a" in area["polygon_keys"])
    assert "UNDER_MINIMUM" in east["capacity_status"]
    assert east["range_m"] == 0
    assert east["review"] == 1


def test_capacity_bounds_are_validated():
    with pytest.raises(ValueError, match="min_hh <= target_hh <= max_hh"):
        partition_mfg_service_areas(
            {"one": 100}, {("one", "one"): 0},
            min_hh=2000, target_hh=4000, max_hh=3000,
        )
