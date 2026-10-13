"""The permit-delay input: the fact that lets the docx trigger be per drop.

``docs/aerial planning.docx`` Rule 7 is ``permit_lead_time > 60 days →
aerial_preferred``, and the drop stage shipped that trigger OFF for one reason:
the only number it had was one project-wide value, and switching it on put
**200 of 260** legs of a ward overhead (docs/stages/AERIAL.md §5).

``trench_design._read_permit_delays`` / ``_permit_delay_days`` give the trigger a
value per premise — an authority/service AREA (matched by containment) or a
per-street register as lines/points (matched by proximity) — and keep the
project-wide number as the fallback for a premise no area covers. ``None`` is
returned only when the premise could not be measured at all, because "this
authority is fast" and "we never asked" are different facts.

These tests also pin the score facts the drop stage measures from the same
inputs (crossed national highway, concrete road surface, pole loading), since a
wrong ``None`` there silently costs a drop the points it was owed.

Run from the engine backend dir (``PYTHONPATH`` must NOT point at QGIS's
site-packages — see tests/README.md):

    cd HLD_Planning_01/web/backend
    PYTHONPATH= python -m pytest tests/test_aerial_permit_delays.py -v
"""

import json
import math
import pathlib
import sys

import pytest

# HLDPlanning/ lives above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import trench_design as td  # noqa: E402

EPSG = 25833


def _geojson(path, features, crs_epsg=EPSG):
    """Write a projected GeoJSON FeatureCollection, return its path as a string.

    The CRS is explicit because the designer reprojects whatever it is handed:
    a GeoJSON with no CRS member is WGS84 by definition, and feeding it
    projected coordinates silently goes through ``PROJ: Invalid latitude`` and
    lands the polygons nowhere near the premises.
    """
    payload = {"type": "FeatureCollection", "features": features}
    if crs_epsg:
        payload["crs"] = {"type": "name",
                          "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs_epsg}"}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _poly(props, x0, y0, x1, y1):
    return {
        "type": "Feature", "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [[
            [x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
    }


def _line(props, coords):
    return {
        "type": "Feature", "properties": props,
        "geometry": {"type": "LineString", "coordinates": list(coords)},
    }


# ── reading the layer ───────────────────────────────────────────────────────

def test_lead_time_is_read_from_every_accepted_spelling(tmp_path):
    for i, field in enumerate(td.PERMIT_DELAY_FIELDS):
        path = _geojson(tmp_path / f"{field}.geojson",
                        [_poly({field: 90}, 0, 0, 10, 10)])
        areas = td._read_permit_delays(path, EPSG)
        assert len(areas) == 1, field
        assert areas[0]["days"] == 90.0, field


def test_a_feature_without_a_readable_lead_time_is_skipped(tmp_path):
    path = _geojson(tmp_path / "mixed.geojson", [
        _poly({"lead_time_days": 45}, 0, 0, 10, 10),
        _poly({"lead_time_days": "unknown"}, 20, 0, 30, 10),
        _poly({"lead_time_days": ""}, 40, 0, 50, 10),
        _poly({"name": "no lead time at all"}, 60, 0, 70, 10),
    ])
    areas = td._read_permit_delays(path, EPSG)
    # the readable one, and only it — "no value" is not "no delay"
    assert [a["days"] for a in areas] == [45.0]


def test_the_authority_name_is_carried_when_present(tmp_path):
    path = _geojson(tmp_path / "named.geojson",
                    [_poly({"lead_time_days": 30, "name": "Berlin Mitte"},
                           0, 0, 10, 10)])
    assert td._read_permit_delays(path, EPSG)[0]["name"] == "Berlin Mitte"


def test_lines_and_points_are_read_too(tmp_path):
    path = _geojson(tmp_path / "streets.geojson", [
        _line({"lead_time_days": 75}, [(0, 0), (100, 0)]),
    ])
    assert td._read_permit_delays(path, EPSG)[0]["days"] == 75.0


# ── the lookup ──────────────────────────────────────────────────────────────

def _areas(tmp_path, *features):
    return td._read_permit_delays(
        _geojson(tmp_path / "permits.geojson", list(features)), EPSG)


def test_a_containing_area_applies(tmp_path):
    areas = _areas(tmp_path, _poly({"lead_time_days": 90}, 0, 0, 100, 100))
    assert td._permit_delay_days((50, 50), areas, 0.0) == 90.0


def test_a_premise_outside_every_area_falls_back_to_the_project_number(tmp_path):
    areas = _areas(tmp_path, _poly({"lead_time_days": 90}, 0, 0, 10, 10))
    assert td._permit_delay_days((500, 500), areas, 12.0) == 12.0


def test_containment_beats_a_nearer_line(tmp_path):
    areas = _areas(
        tmp_path,
        _line({"lead_time_days": 200}, [(50, 1), (150, 1)]),   # 1 m away
        _poly({"lead_time_days": 30}, 0, 0, 100, 100),         # contains it
    )
    assert td._permit_delay_days((50, 0), areas, 0.0) == 30.0


def test_the_smallest_containing_area_wins_whatever_the_file_order(tmp_path):
    """A register naturally carries a coarse fallback polygon under specific
    ones; the answer must not depend on which was written first."""
    ward = _poly({"lead_time_days": 30, "name": "Gemeinde"}, 0, 0, 1000, 1000)
    road = _poly({"lead_time_days": 90, "name": "Straßenbaulastträger"},
                 40, 40, 60, 60)
    specific_first = _areas(tmp_path, road, ward)
    coarse_first = _areas(tmp_path, ward, road)
    assert td._permit_delay_days((50, 50), specific_first, 0.0) == 90.0
    assert td._permit_delay_days((50, 50), coarse_first, 0.0) == 90.0
    # and a premise only the coarse area covers still gets its number
    assert td._permit_delay_days((900, 900), coarse_first, 0.0) == 30.0


def test_the_nearest_line_within_the_radius_wins(tmp_path):
    areas = _areas(
        tmp_path,
        _line({"lead_time_days": 200}, [(0, 60), (100, 60)]),
        _line({"lead_time_days": 30}, [(0, 5), (100, 5)]),
    )
    assert td._permit_delay_days((50, 0), areas, 0.0) == 30.0


def test_a_line_beyond_the_radius_is_not_the_premises_authority(tmp_path):
    far = td.PERMIT_LOOKUP_M + 10.0
    areas = _areas(tmp_path,
                   _line({"lead_time_days": 300}, [(0, far), (100, far)]))
    assert td._permit_delay_days((50, 0), areas, 7.0) == 7.0


def test_without_a_layer_nothing_is_measured(tmp_path):
    assert td._permit_delay_days((50, 50), [], 90.0) is None
    assert td._permit_delay_days((None, None),
                                 _areas(tmp_path, _poly(
                                     {"lead_time_days": 90}, 0, 0, 10, 10)),
                                 0.0) is None


# ── the facts the designer measures ─────────────────────────────────────────

def _leg(coords=((0, 0), (0, 10)), house=None):
    return {"coords": list(coords),
            "house": house or {"x": 0.0, "y": 10.0},
            "length": 10.0}


def _road(coords, fclass, **tags):
    return (list(coords), fclass, tags)


def test_the_permit_fact_is_per_drop_when_a_layer_is_supplied(tmp_path):
    areas = _areas(tmp_path,
                   _poly({"lead_time_days": 90}, -50, -50, 50, 50),
                   _poly({"lead_time_days": 10}, 100, 100, 200, 200))
    p = td.Params(aerial_permit=True)
    slow = td._drop_score_facts(_leg(house={"x": 0.0, "y": 0.0}), p,
                               permit_areas=areas)
    fast = td._drop_score_facts(_leg(house={"x": 150.0, "y": 150.0}), p,
                               permit_areas=areas)
    assert slow["permit_delay_days"] == 90.0
    assert fast["permit_delay_days"] == 10.0


def test_a_project_number_needs_the_switch_but_a_layer_does_not(tmp_path):
    """The two ways in, and why they differ.

    A single number for a whole project is exactly what made this step ship
    off, so it still needs ``aerial_permit``. A per-drop layer is an opt-in in
    its own right: the planner has stated the authority's lead times, so the
    run applies the docx's rule to them rather than ignoring the evidence.
    """
    # a project number, switch off: not measured (the step is off)
    off = td.Params(permit_delay_days=90.0)
    assert td._drop_score_facts(_leg(), off)["permit_delay_days"] is None
    # the same number, switch on
    on = td.Params(permit_delay_days=90.0, aerial_permit=True)
    assert td._drop_score_facts(_leg(), on)["permit_delay_days"] == 90.0
    # 0 means "unknown", which is NOT a measured 0-day permit
    unknown = td.Params(permit_delay_days=0.0, aerial_permit=True)
    assert td._drop_score_facts(_leg(), unknown)["permit_delay_days"] is None


def test_a_supplied_layer_switches_the_step_on_by_itself(tmp_path):
    areas = _areas(tmp_path, _poly({"lead_time_days": 90}, -50, -50, 50, 50))
    p = td.Params()                       # aerial_permit is False
    assert td._permit_trigger_on(p, areas) is True
    assert td._drop_score_facts(_leg(), p, permit_areas=areas)[
        "permit_delay_days"] == 90.0


def test_without_a_layer_and_without_the_switch_nothing_fires():
    p = td.Params()
    assert td._permit_trigger_on(p, []) is False
    assert td._permit_trigger_on(p, None) is False
    assert td._permit_trigger_on(td.Params(aerial_permit=True), []) is True


def test_a_national_highway_crossing_is_measured():
    # the leg runs (0,0) → (0,10); a trunk at y=5 is a crossing, y=40 is not
    nh = [_road([(-50, 5), (50, 5)], "trunk")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=nh)["crosses_nh"] is True
    alongside = [_road([(-50, 40), (50, 40)], "trunk")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=alongside)["crosses_nh"] is False
    residential = [_road([(-50, 5), (50, 5)], "residential")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=residential)["crosses_nh"] is False
    assert td._drop_score_facts(_leg(), td.Params())["crosses_nh"] is None


def test_a_concrete_road_is_measured_from_the_surface_tag():
    # 25 m from the house: inside the lookup radius, so the surface applies
    concrete = [_road([(-50, 35), (50, 35)], "residential", surface="concrete")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=concrete)["concrete_road"] is True
    asphalt = [_road([(-50, 35), (50, 35)], "residential", surface="asphalt")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=asphalt)["concrete_road"] is False
    # untagged is not "not concrete": it was never measured
    untagged = [_road([(-50, 35), (50, 35)], "residential")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=untagged)["concrete_road"] is None
    # and a street the premise does not sit on is not measured either
    distant = [_road([(-50, 200), (50, 200)], "residential", surface="concrete")]
    assert td._drop_score_facts(_leg(), td.Params(),
                               road_parts=distant)["concrete_road"] is None


def test_the_crossed_surface_is_read_not_only_the_nearest_road():
    # a horizontal drop: the asphalt street 25 m away is the nearest, but the
    # service aisle it CROSSES is the one a crew would have to open
    leg = _leg(coords=((0, 0), (10, 0)), house={"x": 10.0, "y": 0.0})
    parts = [
        _road([(-50, 25), (50, 25)], "residential", surface="asphalt"),
        _road([(5, -5), (5, 5)], "service", surface="concrete"),   # crossed
    ]
    assert td._drop_score_facts(leg, td.Params(),
                               road_parts=parts)["concrete_road"] is True


def test_pole_loading_is_measured_from_the_pole_itself():
    assert td._pole_loading_pct(
        {"CAPACITY_TOTAL": 4.0, "CAPACITY_USED": 4.0}) == 100.0
    assert td._pole_loading_pct(
        {"CAPACITY_TOTAL": 4.0, "CAPACITY_USED": 1.0}) == 25.0
    # a pole that states no capacity is not a full pole
    assert td._pole_loading_pct({"CAPACITY_TOTAL": None,
                                 "CAPACITY_USED": 1.0}) is None
    assert td._pole_loading_pct({}) is None


def test_the_nearest_existing_pole_is_found_and_measured():
    poles = [{"x": 100.0, "y": 0.0}, {"x": 21.0, "y": 0.0}]
    near_m, pole = td._nearest_pole_info((0.0, 0.0), poles, 50.0)
    assert near_m == pytest.approx(21.0)
    assert pole["x"] == 21.0
    assert td._nearest_pole_info((0.0, 0.0), [], 50.0) == (None, {})


# ── the model on a leg ──────────────────────────────────────────────────────

def test_the_score_model_reports_the_band_the_leg_landed_in(tmp_path):
    areas = _areas(tmp_path, _poly({"lead_time_days": 90}, -50, -50, 50, 50))
    p = td.Params(aerial_decision_model="score", aerial_permit=True)
    poles = [{"x": 10.0, "y": 10.0, "CAPACITY_TOTAL": 4.0, "CAPACITY_USED": 1.0}]
    reason, payload = td._drop_score_reason(_leg(), p, permit_areas=areas,
                                            poles=poles)
    # existing pole +25, permit +15 = 40 → the middle band, left trenched
    assert payload["score"] == 40.0
    assert payload["decision"] == "ug"
    assert reason == ""


def test_the_middle_band_is_buildable_when_the_project_asks(tmp_path):
    areas = _areas(tmp_path, _poly({"lead_time_days": 90}, -50, -50, 50, 50))
    p = td.Params(aerial_decision_model="score", aerial_permit=True,
                  aerial_score_compare_aerial=True)
    poles = [{"x": 10.0, "y": 10.0, "CAPACITY_TOTAL": 4.0, "CAPACITY_USED": 1.0}]
    reason, payload = td._drop_score_reason(_leg(), p, permit_areas=areas,
                                            poles=poles)
    assert reason == "score_compare"
    assert payload["decision"] == "aerial"


def test_the_aerial_band_reports_its_own_reason():
    p = td.Params(aerial_decision_model="score")
    poles = [{"x": 10.0, "y": 10.0}]
    leg = _leg(house={"x": 0.0, "y": 10.0, "TERRAIN": "rock"})
    reason, payload = td._drop_score_reason(
        leg, p, road_parts=[_road([(-50, 5), (50, 5)], "trunk")],
        poles=poles)
    # existing pole +25, rock +20, NH crossing +20 = 65
    assert payload["score"] == 65.0
    assert reason == "score_aerial"
    assert set(payload["facts"]) == {"existing_poles", "rocky_terrain",
                                     "nh_crossing"}


def test_a_trenched_leg_still_says_why():
    p = td.Params(aerial_decision_model="score")
    reason, payload = td._drop_score_reason(_leg(), p)
    assert reason == ""
    assert payload["score"] == 0.0
    assert payload["notes"]          # every unmeasured fact is named


# ── the pole inventory the facts are read from ──────────────────────────────

def test_read_points_carries_pole_height_and_capacity(tmp_path):
    path = _geojson(tmp_path / "poles.geojson", [{
        "type": "Feature",
        "properties": {"POLE_ID": "P1", "height_m": 9.0,
                       "capacity_total": 4, "capacity_used": 1},
        "geometry": {"type": "Point", "coordinates": [10.0, 10.0]},
    }])
    (point, ) = td._read_points(path, EPSG)
    assert point["POLE_ID"] == "P1"
    assert point["HEIGHT_M"] == 9.0
    assert point["CAPACITY_TOTAL"] == 4.0
    assert point["CAPACITY_USED"] == 1.0
    assert td._pole_loading_pct(point) == 25.0


def test_read_points_leaves_missing_pole_fields_unmeasured(tmp_path):
    path = _geojson(tmp_path / "bare.geojson", [{
        "type": "Feature", "properties": {"POLE_ID": "P2"},
        "geometry": {"type": "Point", "coordinates": [10.0, 10.0]},
    }])
    (point, ) = td._read_points(path, EPSG)
    # a pole with no stated height is not a 0 m pole
    assert point["HEIGHT_M"] is None
    assert td._pole_loading_pct(point) is None


# ── the parameters ship off / on deliberately ───────────────────────────────

def test_the_new_parameters_default_to_the_documented_behaviour():
    p = td.Params()
    assert p.aerial_decision_model == "chain"      # the docx's IF-chain
    assert p.aerial_score_compare_aerial is False  # middle band stays UG
    assert p.aerial_density_high is None           # not guessed
    assert p.metro_core is False                   # declared, not inferred
    assert p.permit_delays == ""                   # no layer supplied
    assert p.aerial_permit is False                # needs a per-drop input
