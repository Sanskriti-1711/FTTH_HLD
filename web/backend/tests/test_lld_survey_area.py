"""Approved building and zone changes are design truth too.

The oneclick brownfield contract carries corridors only (BF_TRENCHES, BF_DUCTS,
BF_FIBRE, BF_EXISTING_PDP/MFG, BF_CHAMBERS), so an approved survey change to a
building or a zone had no path into a re-plan: the engineer's household count
never reached the design and the results page counted only the corridor
changes (LLD-V02, 2026-10-23: 3 of 6 approved changes).

``_apply_approved_area_changes`` folds them into the fresh design; these tests
pin the rules it follows — the premise/zone that stands there is *updated* (its
design identifiers survive), anything the design does not have is *added* as a
new premise/zone, and every applied change is stamped with ``change_id``.

``_ensure_approved_trench_support`` covers the other half of the same gap: an
approved trench that carries no duct or cable (a reroute whose relay found no
dependent, or a trench the survey created) used to be published as civil work
with nothing in it while the BOQ billed its metres.

Run from the engine backend dir:

    env -u PYTHONPATH python -m pytest tests/test_lld_survey_area.py -v
"""

import main as engine


def _point(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _line(coords):
    return {"type": "LineString", "coordinates": coords}


def _square(lon, lat, size):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat],
            [lon + size, lat],
            [lon + size, lat + size],
            [lon, lat + size],
            [lon, lat],
        ]],
    }


def _feature(layer, geometry, **props):
    props["layer"] = layer
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _dataset(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# ── Approved buildings ──────────────────────────────────────────────────────


def test_a_surveyed_building_updates_the_premise_it_stands_for():
    by_layer = {
        "objects": [
            _feature(
                "objects",
                _point(13.40000, 52.50000),
                feature_id="LLD-objects-aaaa",
                ADDR_ID="OSM-W86615261-C",
                SRC_ID="OSM-W86615261-C",
                households=1,
                building_type="semi_detached",
            )
        ]
    }
    dataset = _dataset(
        _feature(
            "objects",
            _point(13.40002, 52.50001),  # ~2 m away: the same building
            approved=True,
            change_id="chg-building-1",
            original_hld_feature_id=None,
            household_count=5,
            building_type="detached",
            survey_status="visited",
        )
    )

    applied = engine._apply_approved_area_changes(by_layer, dataset)

    assert applied["objects"] == 1
    assert applied["polygons"] == 0
    assert len(by_layer["objects"]) == 1, "an existing premise must be updated, not duplicated"
    props = by_layer["objects"][0]["properties"]
    assert props["household_count"] == 5
    # The design's demand field is translated from the survey's own name, so
    # the BOQ's HP line and the drop capacity see the surveyed households.
    assert props["households"] == 5
    assert props["building_type"] == "detached"
    # The design's own identity survives, so drops, permits and BOQ still resolve.
    assert props["feature_id"] == "LLD-objects-aaaa"
    assert props["ADDR_ID"] == "OSM-W86615261-C"
    assert props["change_id"] == "chg-building-1"
    assert props["survey_authoritative"] is True
    # Field geometry wins.
    assert by_layer["objects"][0]["geometry"]["coordinates"] == [13.40002, 52.50001]


def test_a_building_the_design_does_not_have_is_added_with_an_address_id():
    by_layer = {
        "objects": [
            _feature("objects", _point(13.4000, 52.5000), feature_id="LLD-objects-a", ADDR_ID="OSM-1")
        ]
    }
    dataset = _dataset(
        _feature(
            "objects",
            _point(13.4020, 52.5030),  # ~350 m away: a different building
            approved=True,
            change_id="chg-building-2",
            household_count=3,
        )
    )

    engine._apply_approved_area_changes(by_layer, dataset)

    assert len(by_layer["objects"]) == 2
    new = by_layer["objects"][1]["properties"]
    assert new["household_count"] == 3
    assert new["change_id"] == "chg-building-2"
    assert str(new["ADDR_ID"]).startswith("SURVEY-")
    assert new["lld_created"] is True
    assert new["survey_authoritative"] is True


def test_a_moved_building_is_matched_by_its_original_id_however_far_it_moved():
    by_layer = {
        "objects": [
            _feature("objects", _point(13.4000, 52.5000), feature_id="HLD-obj-7", ADDR_ID="OSM-7")
        ]
    }
    dataset = _dataset(
        _feature(
            "objects",
            _point(13.4050, 52.5050),  # far: proximity would miss it
            approved=True,
            change_id="chg-moved",
            original_hld_feature_id="HLD-obj-7",
        )
    )

    engine._apply_approved_area_changes(by_layer, dataset)

    assert len(by_layer["objects"]) == 1
    props = by_layer["objects"][0]["properties"]
    assert props["feature_id"] == "HLD-obj-7"
    assert props["change_id"] == "chg-moved"
    assert by_layer["objects"][0]["geometry"]["coordinates"] == [13.4050, 52.5050]


# ── Approved zones ──────────────────────────────────────────────────────────


def test_an_approved_zone_replaces_the_zone_that_contains_it():
    by_layer = {
        "polygons": [
            _feature(
                "polygons",
                _square(13.39, 52.49, 0.02),
                feature_id="LLD-polygons-a",
                POLYGON_ID="POLY00001",
                SRC_ID="POLY00001",
                HH=103,
            )
        ]
    }
    dataset = _dataset(
        _feature(
            "polygons",
            _square(13.395, 52.495, 0.005),
            approved=True,
            change_id="chg-zone",
            HH=70,
        )
    )

    applied = engine._apply_approved_area_changes(by_layer, dataset)

    assert applied["polygons"] == 1
    assert len(by_layer["polygons"]) == 1
    props = by_layer["polygons"][0]["properties"]
    assert props["HH"] == 70
    assert props["change_id"] == "chg-zone"
    # The design's zone numbering survives: PDPs are named after it.
    assert props["POLYGON_ID"] == "POLY00001"
    assert props["feature_id"] == "LLD-polygons-a"
    assert props["survey_authoritative"] is True


def test_a_zone_the_design_does_not_cover_is_added():
    by_layer = {"polygons": [_feature("polygons", _square(13.39, 52.49, 0.002), HH=10)]}
    dataset = _dataset(
        _feature("polygons", _square(13.50, 52.60, 0.002), approved=True, change_id="chg-new-zone", HH=8)
    )

    engine._apply_approved_area_changes(by_layer, dataset)

    assert len(by_layer["polygons"]) == 2
    new = by_layer["polygons"][1]["properties"]
    assert new["change_id"] == "chg-new-zone"
    assert new["HH"] == 8
    assert new["lld_created"] is True


# ── What is deliberately not applied here ───────────────────────────────────


def test_unapproved_and_corridor_changes_are_left_to_the_corridor_passes():
    by_layer = {"objects": [], "polygons": [], "final_trenches": []}
    dataset = _dataset(
        _feature("objects", _point(13.4, 52.5), approved=False, change_id="chg-x"),
        _feature("final_trenches", _line([[13.4, 52.5], [13.401, 52.5]]), approved=True),
    )

    applied = engine._apply_approved_area_changes(by_layer, dataset)

    assert applied == {"objects": 0, "polygons": 0}
    assert by_layer["objects"] == []
    assert by_layer["final_trenches"] == []


# ── An approved trench with nothing in it ───────────────────────────────────


def test_an_approved_trench_with_nothing_in_it_gets_its_duct():
    trench = _feature(
        "final_trenches",
        _line([[-1.9312, 52.4766], [-1.9310, 52.4767], [-1.9308, 52.4768]]),
        feature_id="LLD-final_trenches-a",
        approved=True,
        change_id="chg-trench",
        TRENCH_TIER="Distribution",
        survey_authoritative=True,
    )
    by_layer = {"final_trenches": [trench], "distribution_ducts": [], "feeder_ducts": []}

    created = engine._ensure_approved_trench_support(by_layer)

    assert created == {"feeder_ducts": 0, "distribution_ducts": 1}
    ducts = by_layer["distribution_ducts"]
    assert len(ducts) == 1
    props = ducts[0]["properties"]
    assert props["change_id"] == "chg-trench"
    assert props["lld_created"] is True
    assert props["survey_authoritative"] is True
    # The duct lies exactly where the trench is: one feature, one path.
    assert ducts[0]["geometry"] == trench["geometry"]


def test_a_feeder_trench_gets_a_feeder_duct():
    by_layer = {
        "final_trenches": [
            _feature(
                "final_trenches",
                _line([[13.4, 52.5], [13.401, 52.5]]),
                approved=True,
                TRENCH_TIER="Feeder",
            )
        ]
    }

    engine._ensure_approved_trench_support(by_layer)

    assert len(by_layer["feeder_ducts"]) == 1
    assert "distribution_ducts" not in by_layer or not by_layer["distribution_ducts"]


def test_a_trench_that_already_carries_a_duct_is_left_alone():
    line = [[13.4, 52.5], [13.401, 52.5]]
    by_layer = {
        "final_trenches": [_feature("final_trenches", _line(line), approved=True)],
        "distribution_ducts": [_feature("distribution_ducts", _line(line))],
    }

    created = engine._ensure_approved_trench_support(by_layer)

    assert created == {"feeder_ducts": 0, "distribution_ducts": 0}
    assert len(by_layer["distribution_ducts"]) == 1


def test_a_trench_the_design_planned_is_not_touched():
    by_layer = {
        "final_trenches": [
            _feature("final_trenches", _line([[13.4, 52.5], [13.401, 52.5]]), feature_id="T-9")
        ]
    }

    created = engine._ensure_approved_trench_support(by_layer)

    assert created == {"feeder_ducts": 0, "distribution_ducts": 0}
    assert "distribution_ducts" not in by_layer
