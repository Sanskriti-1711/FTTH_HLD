"""Tests for osm_source: the household rule, premise assembly, and the file contract.

Everything here is deliberately free of a database and of the network.  The
household rule is the number the whole design is sized on, so it is tested as
pure functions over dicts; the workbook test reads the engine's own output back
through openpyxl and checks the headers against the plugin's alias table.
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import countries  # noqa: E402
import osm_source  # noqa: E402

# HLD_Planning_01/HLDPlanning/utils/sheet_utils.py -- the plugin's alias table.
SHEET_UTILS = Path(__file__).resolve().parents[3] / "HLDPlanning" / "utils" / "sheet_utils.py"

# Headers object_layer MUST detect for a generated workbook to be consumable.
CORE_HEADERS = ("ADDR_ID", "Address", "Housenumber", "City", "Postcode",
                "Country", "District", "HH", "LATITUDE", "LONGITUDE")


# ---------------------------------------------------------------------------
# Area/postcode input normalization
# ---------------------------------------------------------------------------

def test_bare_postcode_is_no_longer_assumed_to_be_german():
    # The old rule rewrote any five-digit code to "<code>, Germany".  A US ZIP
    # 10001 was therefore searched as "10001, Germany" and resolved to a street
    # in Tuebingen -- a valid-looking answer for the wrong place.  Five digits
    # are also used by France, Spain, Italy, Mexico and Norway, so no shape of
    # postcode can identify a country.
    assert osm_source.nominatim_query("10001") == "10001"
    assert osm_source.nominatim_query(" 12105 ") == "12105"
    assert "Germany" not in osm_source.nominatim_query("12105")


def test_composed_label_passes_through_nominatim_query_unchanged():
    value = "Mariendorf, Berlin, Germany"
    assert osm_source.nominatim_query(value) == value


def test_compose_area_builds_the_label_in_name_postcode_city_country_order():
    assert osm_source.compose_area(postcode="12105", city="Berlin",
                                   country_code="DE") == "12105, Berlin, Germany"
    assert osm_source.compose_area(area_name="Mariendorf", postcode="12105",
                                   city="Berlin", country_code="DE") == \
        "Mariendorf, 12105, Berlin, Germany"


def test_compose_area_states_no_country_when_none_was_given():
    # Nothing is invented for the caller: no country in, no country in the label.
    assert osm_source.compose_area(postcode="12105") == "12105"
    assert osm_source.compose_area(city="Berlin", country_code="not-a-country") == "Berlin"


def test_compose_area_does_not_repeat_a_part():
    assert osm_source.compose_area(area_name="Berlin", city="Berlin",
                                   country_code="DE") == "Berlin, Germany"


def test_compose_area_resolves_the_country_name_from_the_code():
    for code in ("US", "us", "United States"):
        assert osm_source.compose_area(postcode="10001", city="New York",
                                       country_code=code) == "10001, New York, United States"


@pytest.mark.parametrize("value,expected", [
    ("12105", True), ("10001", True), ("SW1A 1AA", True),
    ("201301", True), ("HX1 2AB", True),
    ("", False), ("Berlin", False), ("Mariendorf, Berlin, Germany", False),
    ("12105, Berlin", False),
])
def test_looks_like_postcode(value, expected):
    assert osm_source.looks_like_postcode(value) is expected


def test_resolution_key_separates_the_same_label_by_country():
    # "Berlin" filtered to DE and to US are different searches and must not
    # share an osm.area_cache entry.
    assert osm_source.resolution_key("Berlin", "DE") != osm_source.resolution_key("Berlin", "US")
    # And the code is normalized, so "de" and "DE" are the same search.
    assert osm_source.resolution_key("Berlin", "de") == osm_source.resolution_key("Berlin", "DE")


def test_resolution_key_is_versioned_so_old_cache_rows_are_not_reused():
    # Rows written before the country became an explicit input were built by the
    # old assumption ("12105" searched as "12105, Germany"). Reading one would
    # silently reinstate the guesswork, so the key carries a schema version.
    assert osm_source.resolution_key("12105") != osm_source.area_key("12105")


@pytest.mark.parametrize("kwargs,expected", [
    ({"postcode": "12105"}, "postcode"),
    ({"postcode": "12105", "city": "Berlin"}, "postcode"),
    ({"area_name": "Mariendorf"}, "area"),
    ({"area_name": "Mariendorf", "postcode": "12105"}, "area"),
    ({"city": "Berlin"}, "area"),
    ({"area": "12105"}, "postcode"),
    ({"area": "12105, Berlin, Germany"}, "area"),
    ({"area": "Mariendorf, Berlin, Germany"}, "area"),
])
def test_input_type_reflects_how_the_area_was_given(kwargs, expected):
    # The label alone cannot answer this: "12105, Berlin, Germany" has a comma,
    # so a postcode search would be recorded as a name search.
    assert osm_source.input_type_for(**kwargs) == expected


@pytest.mark.parametrize("value,expected", [
    ("DE", "DE"), ("de", "DE"), ("Germany", "DE"), ("germany", "DE"),
    ("USA", ""), ("", ""), (None, ""), ("XX", "XX"),
])
def test_normalize_country_code(value, expected):
    assert countries.normalize_country_code(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("DE", "Germany"), ("de", "Germany"), ("Germany", "Germany"),
    ("US", "United States"), ("nonsense", ""),
])
def test_country_name(value, expected):
    assert countries.country_name(value) == expected


def test_country_options_are_sorted_and_complete_enough():
    options = countries.country_options()
    assert len(options) > 200
    assert options == sorted(options, key=lambda o: o["name"])
    codes = {o["code"] for o in options}
    for code in ("DE", "US", "GB", "FR", "IN", "BR", "ZA", "AU", "XK"):
        assert code in codes


def test_suggest_places_needs_two_characters_and_does_no_network_call():
    # A single character must not reach Nominatim (1 req/s policy) and must not
    # raise: the combobox asks this on every keystroke.
    result = osm_source.suggest_places("b")
    assert result["places"] == []
    assert result["reason"] == "too_short"


# ---------------------------------------------------------------------------
# polygon_area_km2 -- the area of the BOUNDARY, not of its envelope
# ---------------------------------------------------------------------------

def test_area_of_a_degree_cell_matches_the_analytic_sphere_value():
    # Independent check: the integral of R^2 cos(lat) over a 1 x 1 degree cell at
    # the equator is R^2 * radians(1) * (sin(radians(1)) - sin(0)) = 12,363.7 km^2.
    R = 6371.0088
    analytic = (R ** 2) * math.radians(1.0) * math.sin(math.radians(1.0))
    square = {"type": "Polygon",
              "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    assert osm_source.polygon_area_km2(square) == pytest.approx(analytic, rel=1e-9)


def test_area_subtracts_holes_and_sums_multipolygons():
    holed = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
            [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75], [0.25, 0.25]],
        ],
    }
    whole = {"type": "Polygon",
             "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    hole = {"type": "Polygon",
            "coordinates": [[[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75], [0.25, 0.25]]]}
    assert osm_source.polygon_area_km2(holed) == pytest.approx(
        osm_source.polygon_area_km2(whole) - osm_source.polygon_area_km2(hole), rel=1e-9
    )
    # Two identical cells (same latitude) sum to exactly twice one of them.
    twin = {
        "type": "MultiPolygon",
        "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                        [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]]],
    }
    assert osm_source.polygon_area_km2(twin) == pytest.approx(
        2 * osm_source.polygon_area_km2(whole), rel=1e-9
    )
    # A degree cell nearer the pole is SMALLER, which is exactly why this is
    # spherical maths and not a planar shoelace over degrees. A test asserting
    # otherwise is the bug, not the function.
    north = {"type": "Polygon",
             "coordinates": [[[0, 60], [1, 60], [1, 61], [0, 61], [0, 60]]]}
    assert osm_source.polygon_area_km2(north) < osm_source.polygon_area_km2(whole)


@pytest.mark.parametrize("value", [None, {}, {"type": "Point", "coordinates": [1, 2]},
                                   {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}])
def test_area_of_something_that_is_not_an_area_is_zero(value):
    assert osm_source.polygon_area_km2(value) == 0.0


def test_a_box_is_always_larger_than_the_shape_inside_it():
    # This is the whole reason the reported area stopped being the bbox: for
    # postcode 12105 the envelope is 7.31 km^2 against a 5.81 km^2 boundary, so
    # publishing the bbox overstated the job by 26 %.
    triangle = {"type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    box = {"type": "Polygon",
           "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    assert osm_source.polygon_area_km2(triangle) < osm_source.polygon_area_km2(box)


# ---------------------------------------------------------------------------
# object_properties -- the objects layer carries ALL of a building's attributes
# ---------------------------------------------------------------------------

def test_object_properties_keep_every_raw_osm_tag():
    row = {
        "osm_id": 123, "lon": 13.38, "lat": 52.44, "building": "apartments",
        "tags": {"building": "apartments", "roof:shape": "gabled",
                 "start_date": "1905", "building:levels": "4", "amenity": "library"},
    }
    props = osm_source.object_properties(row)
    # Tags an inspector looks for are present under the name OSM uses...
    assert props["roof:shape"] == "gabled"
    assert props["start_date"] == "1905"
    assert props["amenity"] == "library"
    # ...and tag_count counts only the real OSM tags, not the derived fields.
    assert props["tag_count"] == 5
    assert props["osm_object_id"] == 123
    assert props["osm_object_type"] == "way"


def test_object_properties_do_not_let_a_null_column_erase_a_real_tag():
    # `building=yes` lives only in the tags here and the column is NULL. Copying
    # the column over the tag blanked out real data in the objects layer.
    props = osm_source.object_properties({"osm_id": 9, "tags": {"building": "yes"}})
    assert props["building"] == "yes"
    assert props["tag_count"] == 1
    # A populated column still fills in a tag that is genuinely absent.
    filled = osm_source.object_properties({"osm_id": 9, "building": "church", "tags": {}})
    assert filled["building"] == "church"
    assert filled["building_type"] == "church"


def test_object_properties_survive_missing_or_string_tags():
    assert osm_source.object_properties({"osm_id": 1})["tag_count"] == 0
    assert osm_source.object_properties({"osm_id": 1})["osm_object_id"] == 1
    # A tags value that came back from PostGIS as a string is parsed, not dropped.
    props = osm_source.object_properties({"osm_id": 2, "tags": '{"building": "yes"}'})
    assert props["building"] == "yes"
    assert props["tag_count"] == 1
    # Unparseable junk must not raise: the layer still renders.
    assert osm_source.object_properties({"osm_id": 3, "tags": "not json"})["tag_count"] == 0


def test_object_properties_round_the_footprint_it_was_given():
    props = osm_source.object_properties({"osm_id": 4, "footprint_m2": 480.123456})
    assert props["footprint_m2"] == 480.1
    assert osm_source.object_properties({"osm_id": 4})["footprint_m2"] is None


# ---------------------------------------------------------------------------
# parse_flats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("3", 3),
    (3, 3),
    (" 7 ", 7),
    ("1-6", 6),          # a range means the highest dwelling number
    ("1 - 6", 6),
    ("2 to 9", 8),
    ("1;3;5", 3),        # a list means its length
    ("1,2,3,4", 4),
    ("", None),
    (None, None),
    ("unknown", None),
    ("0", 1),            # a nonsense zero becomes the floor of one
])
def test_parse_flats(value, expected):
    assert osm_source.parse_flats(value) == expected


# ---------------------------------------------------------------------------
# estimate_households -- OSM tags first, heuristic second
# ---------------------------------------------------------------------------

def test_building_flats_tag_wins_over_the_heuristic():
    hh, method = osm_source.estimate_households(
        {"building": "apartments", "building_flats": "12", "building_levels": "4",
         "footprint_m2": 900}
    )
    assert (hh, method) == (12, "building_flats")


def test_levels_and_footprint_are_the_fallback():
    # 4 levels x 480 m^2 / 85 m^2 = 22.6 -> 23
    hh, method = osm_source.estimate_households(
        {"building": "apartments", "building_levels": "4", "footprint_m2": 480}
    )
    assert (hh, method) == (23, "levels_x_footprint")


def test_heuristic_needs_both_levels_and_footprint():
    assert osm_source.estimate_households(
        {"building": "apartments", "building_levels": "4"}
    ) == (None, None)
    assert osm_source.estimate_households(
        {"building": "apartments", "footprint_m2": 480}
    ) == (None, None)


def test_estimate_is_capped_and_never_zero():
    hh, method = osm_source.estimate_households(
        {"building": "apartments", "building_levels": "40", "footprint_m2": 100_000}
    )
    assert method == "levels_x_footprint"
    assert hh == osm_source.MAX_FLATS_PER_BUILDING

    hh, _ = osm_source.estimate_households(
        {"building": "house", "building_levels": "1", "footprint_m2": 10}
    )
    assert hh >= 1


def test_unit_area_follows_building_type():
    assert osm_source.unit_area_for("apartments") == osm_source.UNIT_AREA_M2["apartments"]
    assert osm_source.unit_area_for("terrace") == osm_source.UNIT_AREA_M2["terrace"]
    assert osm_source.unit_area_for("detached") == osm_source.UNIT_AREA_M2["detached"]
    assert osm_source.unit_area_for("yes") == osm_source.UNIT_AREA_M2["default"]
    assert osm_source.unit_area_for(None) == osm_source.UNIT_AREA_M2["default"]


# ---------------------------------------------------------------------------
# distribution_for -- the parts must sum EXACTLY to the building total
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("total,n", [(23, 3), (1, 1), (1, 5), (100, 7), (7, 3), (2, 2)])
def test_distribution_sums_to_the_total(total, n):
    parts = osm_source.distribution_for(total, [str(i) for i in range(n)])
    assert len(parts) == n
    assert sum(parts) == max(total, n)
    assert all(p >= 1 for p in parts)


def test_distribution_matches_the_documented_worked_example():
    # docs: 23 dwellings over three addresses -> 9 / 7 / 7
    assert osm_source.distribution_for(23, ["2", "4", "6"]) == [9, 7, 7]


def test_distribution_of_nothing_is_empty():
    assert osm_source.distribution_for(10, []) == []


# ---------------------------------------------------------------------------
# assemble_premises
# ---------------------------------------------------------------------------

def _building(osm_id, **kwargs):
    row = {
        "osm_id": osm_id, "building": "apartments", "name": None,
        "addr_street": None, "addr_housenumber": None, "addr_postcode": None,
        "addr_city": "Berlin", "addr_suburb": None,
        "building_levels": "4", "building_flats": None,
        "footprint_m2": 480.0, "lon": 13.38, "lat": 52.44,
    }
    row.update(kwargs)
    return row


def _address(osm_id, housenumber, **kwargs):
    row = {
        "osm_id": osm_id, "addr_street": "Mariendorfer Damm",
        "addr_housenumber": housenumber, "addr_postcode": "12107",
        "addr_city": "Berlin", "addr_suburb": "Mariendorf", "addr_flats": None,
        "lon": 13.381, "lat": 52.441,
    }
    row.update(kwargs)
    return row


def test_addresses_in_one_building_split_its_total():
    buildings = [_building(1)]
    addresses = [_address(11, "2"), _address(12, "4"), _address(13, "6")]
    premises, stats = osm_source.assemble_premises(
        buildings, addresses, {11: 1, 12: 1, 13: 1}
    )
    assert [p["HH"] for p in premises] == [9, 7, 7]
    assert sum(p["HH"] for p in premises) == 23
    assert stats["duplicates_merged"] == 0
    # Ordered by housenumber, addressed per premise, ids short and stable.
    assert [p["Housenumber"] for p in premises] == ["2", "4", "6"]
    assert [p["ADDR_ID"] for p in premises] == ["OSM-W1-P1", "OSM-W1-P2", "OSM-W1-P3"]


def test_premise_country_and_city_come_from_the_area_not_a_hardcoded_default():
    # These were the literals "Berlin"/"Germany" on every row, which mislabelled
    # every non-German project in the generated workbook and the BOQ after it.
    premises, _ = osm_source.assemble_premises(
        [_building(1, addr_city=None)], [_address(11, "5", addr_city=None)], {11: 1},
        country="United States", city="New York",
    )
    assert premises[0]["Country"] == "United States"
    assert premises[0]["City"] == "New York"


def test_premise_city_prefers_the_osm_tag_over_the_area_hint():
    premises, _ = osm_source.assemble_premises(
        [_building(1)], [_address(11, "5")], {11: 1},
        country="Germany", city="Somewhere Else",
    )
    assert premises[0]["City"] == "Berlin"
    assert premises[0]["Country"] == "Germany"


def test_premise_country_is_empty_when_the_area_country_is_unknown():
    # Empty beats wrong: the Country column is consumed by the design.
    premises, _ = osm_source.assemble_premises(
        [_building(1, addr_city=None)], [_address(11, "5", addr_city=None)], {11: 1}
    )
    assert premises[0]["Country"] == ""
    assert premises[0]["City"] == ""


def test_explicit_address_flats_beats_the_building_split():
    buildings = [_building(1)]
    addresses = [_address(11, "2"), _address(12, "4", addr_flats="9")]
    premises, _ = osm_source.assemble_premises(
        buildings, addresses, {11: 1, 12: 1}
    )
    by_hno = {p["Housenumber"]: p for p in premises}
    assert by_hno["4"]["HH"] == 9
    assert by_hno["4"]["HH_METHOD"] == "addr_flats"


def test_building_without_any_address_becomes_a_centroid_premise():
    premises, stats = osm_source.assemble_premises([_building(7)], [], {})
    assert len(premises) == 1
    assert premises[0]["ADDR_ID"] == "OSM-W7-C"
    assert premises[0]["HH_METHOD"] == "levels_x_footprint"
    assert stats["boundary_buildings"] == 1


def test_excluded_building_classes_are_dropped():
    buildings = [_building(1, building="garage"), _building(2, building="apartments")]
    premises, stats = osm_source.assemble_premises(buildings, [], {})
    assert [p["OSM_ID"] for p in premises] == [2]
    assert stats["buildings_excluded"] == 1


def test_duplicate_street_and_number_is_merged():
    buildings = []
    addresses = [
        _address(11, "12", addr_street="Mariendorfer Damm"),
        _address(12, " 12 ", addr_street="mariendorfer damm "),
    ]
    premises, stats = osm_source.assemble_premises(buildings, addresses, {})
    assert len(premises) == 1
    assert stats["duplicates_merged"] == 1


def test_address_without_a_building_falls_back_to_one_household():
    premises, _ = osm_source.assemble_premises([], [_address(11, "5")], {})
    assert premises[0]["HH"] == 1
    assert premises[0]["HH_METHOD"] == "fallback_one"
    assert premises[0]["ADDR_ID"] == "OSM-N11"


def test_addr_id_never_exceeds_the_ogr_field_width():
    # The >48-char ADDR_ID warning is a known open item on the manual path; a
    # generated id must stay clear of it.
    buildings = [_building(123456789012, building_flats="4")]
    addresses = [_address(987654321012345, "12")]
    premises, _ = osm_source.assemble_premises(buildings, addresses, {987654321012345: 123456789012})
    assert all(len(p["ADDR_ID"]) <= 48 for p in premises)


def test_lowest_housenumber_absorbs_the_remainder_regardless_of_input_order():
    buildings = [_building(1)]
    addresses = [_address(13, "6"), _address(11, "2"), _address(12, "4")]
    premises, _ = osm_source.assemble_premises(
        buildings, addresses, {11: 1, 12: 1, 13: 1}
    )
    assert [p["Housenumber"] for p in premises] == ["2", "4", "6"]
    assert premises[0]["HH"] == 9


# ---------------------------------------------------------------------------
# household_summary
# ---------------------------------------------------------------------------

def test_household_summary_reports_the_estimated_share():
    premises = [
        {"HH": 10, "HH_METHOD": "building_flats"},
        {"HH": 10, "HH_METHOD": "levels_x_footprint"},
    ]
    summary = osm_source.household_summary(premises)
    assert summary["total"] == 20
    assert summary["estimated_share"] == 0.5
    assert summary["by_method"] == {"building_flats": 1, "levels_x_footprint": 1}


def test_household_summary_of_no_premises_is_not_a_division_error():
    summary = osm_source.household_summary([])
    assert summary["total"] == 0
    assert summary["estimated_share"] == 0.0


# ---------------------------------------------------------------------------
# sub_area_breakdown -- how a too-large area gets narrowed honestly
# ---------------------------------------------------------------------------

def test_sub_areas_group_by_postcode_and_district():
    premises = [
        {"Postcode": "12107", "District": "Mariendorf", "HH": 5},
        {"Postcode": "12107", "District": "Mariendorf", "HH": 3},
        {"Postcode": "12109", "District": "Mariendorf", "HH": 1},
        {"Postcode": "12279", "District": "Lankwitz", "HH": 9},
    ]
    breakdown = osm_source.sub_area_breakdown(premises)
    assert [p["value"] for p in breakdown["postcode"]] == ["12107", "12109", "12279"]
    assert breakdown["postcode"][0] == {"value": "12107", "premises": 2, "households": 8}
    assert [d["value"] for d in breakdown["district"]] == ["Mariendorf", "Lankwitz"]
    assert breakdown["district"][0]["premises"] == 3


def test_sub_areas_skip_blank_values_and_limit_results():
    premises = [{"Postcode": "", "District": None, "HH": 1}]
    premises += [{"Postcode": f"12{i:03d}", "District": "D", "HH": 1} for i in range(12)]
    breakdown = osm_source.sub_area_breakdown(premises, limit=5)
    assert len(breakdown["postcode"]) == 5
    assert all(p["value"] for p in breakdown["postcode"])
    assert breakdown["district"] == [{"value": "D", "premises": 12, "households": 12}]


# ---------------------------------------------------------------------------
# The written files -- the contract with the pipeline
# ---------------------------------------------------------------------------

def test_workbook_headers_are_still_valid_aliases_in_the_plugin():
    """The plugin's EXPECTED_MAP is the authority; guard against silent drift.

    sheet_utils imports pandas at module level, so the alias table is read from
    source rather than imported -- the point is to catch a rename in the plugin
    that would make a generated workbook unreadable.
    """
    assert SHEET_UTILS.is_file(), f"plugin alias table not found at {SHEET_UTILS}"
    source = SHEET_UTILS.read_text(encoding="utf-8")
    block = re.search(r"EXPECTED_MAP\s*=\s*\{(.*?)\n\}", source, flags=re.DOTALL)
    assert block, "EXPECTED_MAP could not be parsed out of sheet_utils.py"
    aliases = set(re.findall(r'"([^"]+)"', block.group(1)))
    for header in CORE_HEADERS:
        assert header in aliases, (
            f"{header!r} is no longer a detected alias in sheet_utils.EXPECTED_MAP — "
            "generated workbooks would silently lose that column"
        )


def _plugin_alias_map():
    """Parse EXPECTED_MAP out of the plugin's sheet_utils.py, pandas-free.

    Returns {key: [alias, ...]} in the plugin's own declaration order, which is
    the order autodetect_mapping() tries them in.
    """
    assert SHEET_UTILS.is_file(), f"plugin alias table not found at {SHEET_UTILS}"
    source = SHEET_UTILS.read_text(encoding="utf-8")
    block = re.search(r"EXPECTED_MAP\s*=\s*\{(.*?)\n\}", source, flags=re.DOTALL)
    assert block, "EXPECTED_MAP could not be parsed out of sheet_utils.py"
    mapping = {}
    for entry in re.finditer(r'"([a-z_]+)"\s*:\s*\[(.*?)\]', block.group(1), flags=re.DOTALL):
        mapping[entry.group(1)] = re.findall(r'"([^"]+)"', entry.group(2))
    return mapping


def _resolve(alias_map, headers):
    """Resolve like autodetect_mapping(): first alias matching a header by name.

    Only the name-matching half is modelled here.  The plugin also *drops*
    latitude/longitude when the columns carry no numeric values, which is why
    the fixture below supplies real coordinates rather than placeholders.
    """
    lower = {str(h).lower(): h for h in headers}
    resolved = {}
    for key, options in alias_map.items():
        for name in options:
            if name.lower() in lower:
                resolved[key] = lower[name.lower()]
                break
    return resolved


def test_every_key_the_object_layer_needs_is_produced(tmp_path):
    """The reverse of the alias check: does the workbook satisfy *every* key?

    The forward test only proves our headers are known aliases.  A key the
    plugin looks for but our workbook never provides is silent at run time --
    autodetect_mapping() simply omits it from the mapping, so the stage runs
    with a degraded address instead of failing.  Every key must resolve from a
    workbook written by the generator itself.
    """
    alias_map = _plugin_alias_map()
    assert alias_map, "EXPECTED_MAP could not be parsed out of sheet_utils.py"

    premise = {h: "x" for h in osm_source.EXCEL_HEADERS}
    premise.update({"LATITUDE": 52.441, "LONGITUDE": 13.381, "HH": 9})
    path = osm_source.write_address_workbook(
        str(tmp_path / "out" / "Main_DataSet.xlsx"), [premise]
    )

    from openpyxl import load_workbook

    rows = load_workbook(path, read_only=True).active.iter_rows(values_only=True)
    headers = [c for c in next(rows) if c is not None]

    resolved = _resolve(alias_map, headers)
    missing = sorted(set(alias_map) - set(resolved))
    assert not missing, (
        f"a generated workbook does not satisfy {missing}; the object layer would "
        f"run without those fields. headers={headers}"
    )


def test_every_premise_carries_coordinates_so_nothing_needs_geocoding():
    """The object layer geocodes *only* rows without usable coordinates.

    Generated rows therefore never take that path -- which is what keeps a
    workbook independent of Nominatim, and out of the plugin's default-country
    fallback.  A premise with missing or out-of-range coordinates would be sent
    to Nominatim at run time instead.
    """
    buildings = [_building(1), _building(2, addr_street=None, addr_housenumber=None)]
    addresses = [
        _address(11, "2"),                            # inside building 1
        _address(12, "40", lat=52.39, lon=13.30),     # no building of its own
    ]
    premises, _ = osm_source.assemble_premises(buildings, addresses, {11: 1, 12: 1})
    assert premises, "fixture produced no premises"

    for p in premises:
        lat, lon = p["LATITUDE"], p["LONGITUDE"]
        assert isinstance(lat, (int, float)) and isinstance(lon, (int, float)), (
            f"premise {p['ADDR_ID']} has non-numeric coordinates ({lat!r}, {lon!r})"
        )
        assert -90 <= lat <= 90 and -180 <= lon <= 180, (
            f"premise {p['ADDR_ID']} has out-of-range coordinates ({lat}, {lon})"
        )
        assert (lat, lon) != (0, 85), "(0, 85) is the plugin's not-found sentinel"


def test_write_address_workbook_round_trips(tmp_path):
    from openpyxl import load_workbook

    premises = [{
        "ADDR_ID": "OSM-W1-P1", "Address": "Mariendorfer Damm", "Housenumber": "2",
        "City": "Berlin", "Postcode": "12107", "Country": "Germany",
        "District": "Mariendorf", "HH": 9, "HH_METHOD": "levels_x_footprint",
        "LATITUDE": 52.441, "LONGITUDE": 13.381, "OSM_ID": 1,
    }]
    path = osm_source.write_address_workbook(str(tmp_path / "out" / "Main_DataSet.xlsx"), premises)
    wb = load_workbook(path, read_only=True)
    assert wb.sheetnames == ["Addresses"]
    rows = list(wb.active.iter_rows(values_only=True))
    assert list(rows[0]) == list(osm_source.EXCEL_HEADERS)
    assert rows[1][0] == "OSM-W1-P1"
    assert rows[1][osm_source.EXCEL_HEADERS.index("HH")] == 9
    assert len(rows) == 2


def test_write_roads_geojson_keeps_fclass_and_geometry(tmp_path):
    roads = [{
        "osm_id": 5, "highway": "residential", "fclass": "residential",
        "name": "Mariendorfer Damm", "oneway": "yes", "geom_json": json.dumps(
            {"type": "LineString", "coordinates": [[13.38, 52.44], [13.39, 52.45]]}
        ),
    }]
    path = osm_source.write_roads_geojson(str(tmp_path / "roads.geojson"), roads)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    feature = payload["features"][0]
    assert feature["properties"]["fclass"] == "residential"
    assert feature["properties"]["name"] == "Mariendorfer Damm"
    assert feature["geometry"]["type"] == "LineString"


# ---------------------------------------------------------------------------
# The derived pavement (sidewalk) carrier network
# ---------------------------------------------------------------------------

def _road_row(osm_id, fclass, coords, name=None):
    return {
        "osm_id": osm_id, "fclass": fclass, "highway": fclass, "name": name,
        "geom_json": json.dumps({"type": "LineString", "coordinates": coords}),
    }


def _line_distance_m(row, point):
    """Distance from a point to a road row's line, in metres (local planar)."""
    coords = json.loads(row["geom_json"])["coordinates"]
    mx = 111320.0 * math.cos(math.radians(coords[0][1]))
    my = 110540.0
    px = ((point[0] - coords[0][0]) * mx, (point[1] - coords[0][1]) * my)
    pts = [(((c[0] - coords[0][0]) * mx, (c[1] - coords[0][1]) * my)) for c in coords]
    best = float("inf")
    for a, b in zip(pts, pts[1:]):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px[0] - a[0]) * vx + (px[1] - a[1]) * vy) / L2))
        best = min(best, math.hypot(px[0] - (a[0] + t * vx), px[1] - (a[1] + t * vy)))
    return best


def test_pavement_carriers_paves_both_kerbs_at_the_kerb_distance():
    # A trench runs in the pavement, and the designer routes ONLY on footway
    # classes -- so this is the layer it needs, on BOTH kerbs because its
    # cabinets are placed on either side.
    # Three vertices, so the middle one is an offset vertex rather than an
    # extended end (the ends reach past the junction node, by PAVEMENT_EXTEND_M).
    street = _road_row(1, "residential",
                       [[13.3800, 52.4400], [13.3830, 52.4400], [13.3860, 52.4400]],
                       "Test Street")
    result = osm_source.pavement_carriers([street])
    assert len(result["rows"]) == 2
    assert {r["fclass"] for r in result["rows"]} == {"footway"}
    assert {r["highway"] for r in result["rows"]} == {"footway"}
    assert {r["carrier_source"] for r in result["rows"]} == {osm_source.PAVEMENT_SOURCE}
    assert all(r["name"] == "Test Street" for r in result["rows"])
    assert result["derived_km"] > 0 and result["mapped_km"] == 0
    for row in result["rows"]:
        middle = json.loads(row["geom_json"])["coordinates"][1]
        assert _line_distance_m(street, middle) == pytest.approx(
            osm_source.PAVEMENT_OFFSET_M, abs=0.2)
    # one line each side of the street, not two on the same side
    sides = {round(json.loads(r["geom_json"])["coordinates"][0][1] - 52.4400, 5)
             for r in result["rows"]}
    assert len(sides) == 2


def test_crossing_streets_form_one_connected_pavement():
    # The designer nodes its graph on shared VERTICES, so two pavements that
    # merely cross are two graphs.  Measured on the ward run that fed it the OSM
    # footways alone: 77 pieces, 44 of 45 splitters unreachable from the MFG.
    roads = [
        _road_row(1, "residential", [[13.3800, 52.4400], [13.3860, 52.4400]]),
        _road_row(2, "service", [[13.3830, 52.4370], [13.3830, 52.4430]]),
    ]
    result = osm_source.pavement_carriers(roads)
    assert len(result["rows"]) == 4
    assert result["pieces"] == 1


def test_pavement_carriers_rebuilds_the_mapped_footways():
    # The node/tying passes put vertices ON the mapped footways, and a connection
    # that is not a vertex of both lines is not a connection.  So the mapped
    # footways come back rebuilt and their originals are named for removal --
    # emitting the derived lines alone reached 1 splitter of 45.
    roads = [
        _road_row(1, "residential", [[13.3800, 52.4400], [13.3850, 52.4400]]),
        _road_row(2, "footway", [[13.3820, 52.4400], [13.3820, 52.4408]]),
    ]
    result = osm_source.pavement_carriers(roads)
    assert result["replaced_osm_ids"] == [2]
    rebuilt = [r for r in result["rows"] if r["osm_id"] == 2]
    assert len(rebuilt) == 1
    assert rebuilt[0]["fclass"] == "footway"
    assert rebuilt[0]["carrier_source"] == "osm-footway"
    assert result["mapped_km"] > 0


def test_arterials_are_not_paved_by_default(monkeypatch):
    # The class ladder exists so a trench does not run down an arterial; a
    # derived pavement there would be the cheapest carrier in that ladder.
    roads = [_road_row(1, "primary", [[13.3800, 52.4400], [13.3850, 52.4400]])]
    assert osm_source.pavement_carriers(roads)["rows"] == []
    monkeypatch.setattr(osm_source, "PAVEMENT_ARTERIALS", True)
    paved = osm_source.pavement_carriers(roads)
    assert len(paved["rows"]) == 2
    assert paved["arterials"] is True


def test_pavement_carriers_without_pavable_roads():
    result = osm_source.pavement_carriers(
        [{"fclass": "primary", "geom_json": None},
         {"fclass": "construction", "geom_json": None}])
    assert result["rows"] == []
    assert result["pieces"] == 0 and result["derived_km"] == 0.0


def test_road_summary_groups_kilometres_by_fclass():
    roads = [{
        "fclass": "residential", "highway": "residential",
        "geom_json": json.dumps({"type": "LineString",
                                 "coordinates": [[13.38, 52.44], [13.39, 52.44]]}),
    }]
    summary = osm_source.road_summary(roads)
    assert summary["total_km"] > 0
    assert summary["total_km"] == summary["by_fclass"]["residential"]


# ---------------------------------------------------------------------------
# Overpass element classification
# ---------------------------------------------------------------------------

def _way(osm_id, tags, coords, closed=False):
    if closed:
        coords = list(coords) + [coords[0]]
    return {
        "type": "way", "id": osm_id, "tags": tags,
        "geometry": [{"lon": c[0], "lat": c[1]} for c in coords],
    }


def _node(osm_id, tags, lon=13.38, lat=52.44):
    return {"type": "node", "id": osm_id, "tags": tags, "lon": lon, "lat": lat}


def test_closed_highway_way_is_kept_as_a_line():
    # A roundabout/loop is tagged highway but arrives as a closed ring.  It was
    # being dropped, which removes real roads from the routing graph.
    elements = [
        _way(1, {"highway": "residential"}, [(13.38, 52.44), (13.39, 52.44), (13.39, 52.45)], closed=True),
        _way(2, {"highway": "footway"}, [(13.40, 52.44), (13.41, 52.44)]),
    ]
    classified = osm_source.classify_elements(elements)
    assert len(classified["roads"]) == 2
    for row in classified["roads"]:
        geometry = json.loads(row[1])
        assert geometry["type"] == "LineString"
        assert len(geometry["coordinates"]) >= 2
    # fclass is the highway class, which the permit engine maps to an authority.
    assert {row[3] for row in classified["roads"]} == {"residential", "footway"}


def test_elements_are_classified_into_the_right_tables():
    elements = [
        _node(10, {"addr:housenumber": "12", "addr:street": "Mariendorfer Damm", "addr:flats": "1-6"}),
        _way(20, {"building": "apartments", "building:levels": "4", "addr:housenumber": "12"},
             [(13.38, 52.44), (13.381, 52.44), (13.381, 52.441)], closed=True),
        _way(30, {"highway": "service", "name": "Hofweg"}, [(13.39, 52.44), (13.39, 52.45)]),
        _way(40, {"landuse": "forest"}, [(13.41, 52.44), (13.411, 52.44), (13.411, 52.441)], closed=True),
    ]
    classified = osm_source.classify_elements(elements)
    assert len(classified["address_nodes"]) == 1
    assert len(classified["buildings"]) == 1
    assert len(classified["roads"]) == 1
    assert len(classified["landuse"]) == 1

    addr = classified["address_nodes"][0]
    assert addr[2] == "Mariendorfer Damm"   # addr_street
    assert addr[7] == "1-6"                  # addr_flats passed through raw

    bldg = classified["buildings"][0]
    assert bldg[2] == "apartments"
    assert bldg[9] == "4"                    # building_levels


def test_every_classified_row_matches_its_table_column_count():
    """A row with the wrong arity fails the INSERT, so pin it here instead."""
    elements = [
        _node(10, {"addr:housenumber": "12"}),
        _way(20, {"building": "apartments", "addr:housenumber": "12"},
             [(13.38, 52.44), (13.381, 52.44), (13.381, 52.441)], closed=True),
        _way(30, {"highway": "service"}, [(13.39, 52.44), (13.39, 52.45)]),
        _way(40, {"landuse": "forest"}, [(13.41, 52.44), (13.411, 52.44), (13.411, 52.441)], closed=True),
    ]
    classified = osm_source.classify_elements(elements)
    for table, rows in classified.items():
        expected = len(osm_source._TABLE_COLUMNS[table])
        assert rows, f"{table} classified nothing"
        for row in rows:
            assert len(row) == expected, f"{table} row arity {len(row)} != {expected}"


# ---------------------------------------------------------------------------
# The preview reports a large area; the run cap is what refuses
# ---------------------------------------------------------------------------
#
# Measured on Birmingham (a 266.9 km^2 city boundary, 257,127 premises): the
# preview refused before it could name a single usable postcode, and the
# refusal quoted the RUN's cap while the preview's own cap was a different
# number.  Reporting the size is the preview's job, so the numbers that matter
# -- the cap that applied, and where to narrow -- are now carried, not guessed.

def _premise(i, postcode="12107", district="Mariendorf", hh=1):
    return {
        "ADDR_ID": f"OSM-W{i}", "Address": "Mariendorfer Damm", "Housenumber": str(i),
        "City": "Berlin", "Postcode": postcode, "Country": "Germany",
        "District": district, "HH": hh, "HH_METHOD": "levels_x_footprint",
        "LATITUDE": 52.44, "LONGITUDE": 13.38, "OSM_ID": i,
    }


def _stub_area(monkeypatch, premises, roads=({},), resolution=None):
    """Make preview_area run without a database, the network or the pipeline.

    `resolution` overrides the boundary facts a caller wants to exercise -- the
    rung, the input type, what was matched -- so the postcode warnings can be
    driven without a Nominatim call.
    """
    base = {
        "polygon": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "bbox": [13.31, 52.41, 13.42, 52.47],
        "country": "Germany", "city": "Berlin", "polygon_source": "nominatim",
        "area_km2": 3.36,
        "matched": "Mariendorf, Berlin", "input_type": "area",
    }
    base.update(resolution or {})
    monkeypatch.setattr(osm_source, "resolve_area", lambda *a, **k: dict(base))
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "ensure_area_data", lambda *a, **k: {"source": "cache"})
    monkeypatch.setattr(osm_source, "read_area_rows", lambda poly: ([], [], {}, list(roads)))
    monkeypatch.setattr(
        osm_source, "assemble_premises",
        lambda *a, **k: (list(premises), {"duplicates_merged": 0, "buildings_excluded": 0}),
    )


def test_preview_reports_an_over_cap_area_instead_of_refusing(monkeypatch):
    premises = [_premise(i) for i in range(osm_source.MAX_PREMISES + 1)]
    _stub_area(monkeypatch, premises)

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")  # must not raise

    assert out["premises"]["count"] == osm_source.MAX_PREMISES + 1
    assert out["over_run_cap"] is True
    assert out["can_run"] is False
    assert out["run_cap"] == osm_source.MAX_PREMISES
    # The narrowing chips are the whole reason a refusal was the wrong answer.
    assert out["sub_areas"]["postcode"], "a refused preview could not offer these"
    assert "12107" in [e["value"] for e in out["sub_areas"]["postcode"]]
    scale = [w for w in out["warnings"] if "per-run cap" in w]
    assert scale, "no word about the cap: %s" % out["warnings"]
    assert str(osm_source.MAX_PREMISES) in scale[0]
    assert "12107" in out["run_blocked_reason"]


def test_preview_does_not_refuse_where_it_used_to(monkeypatch):
    """The preview used to raise at its own 50,000-premise cap, which is why a
    257,127-premise area could not be previewed at all.  That threshold is gone:
    only the run refuses, and it says so in the response instead."""
    bulk = [{"ADDR_ID": f"OSM-W{i}", "Postcode": "B1", "District": "",
             "HH": 1, "HH_METHOD": "fallback_one"} for i in range(50_001)]
    _stub_area(monkeypatch, bulk)

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert out["premises"]["count"] == 50_001
    assert out["over_run_cap"] is True
    assert out["can_run"] is False


def test_preview_still_honours_a_cap_the_caller_asks_for(monkeypatch):
    _stub_area(monkeypatch, [_premise(i) for i in range(10)])

    with pytest.raises(osm_source.TooManyPremises) as excinfo:
        osm_source.preview_area("Mariendorf, Berlin, Germany", max_premises=5)

    assert excinfo.value.count == 10
    assert excinfo.value.cap == 5, "the cap that applied is not the run's"


def test_preview_says_a_run_can_start_for_an_ordinary_area(monkeypatch):
    _stub_area(monkeypatch, [_premise(i) for i in range(3)])

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert out["can_run"] is True
    assert out["run_blocked_reason"] is None
    assert out["over_run_cap"] is False


def test_preview_blocks_a_run_when_there_are_no_roads(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)], roads=())

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert out["can_run"] is False
    assert "road" in out["run_blocked_reason"].lower()
    assert any("road" in w.lower() for w in out["warnings"])


# ---------------------------------------------------------------------------
# A postcode that is not a postcode boundary
# ---------------------------------------------------------------------------
#
# "B1, Birmingham" matches building/office -- an 8,000 m2 office block that
# happens to contain the postcode -- and the rung still reads `nominatim`, so a
# single building was indistinguishable from a real postcode polygon.  That is a
# silently wrong design area, not a rounding error.

def test_postcode_counts_as_a_boundary_only_when_the_match_is_one():
    f = osm_source.postcode_defines_the_area
    # A real postcode relation, as 12105, Berlin returns.
    assert f("12105", "nominatim", "boundary", "postal_code") is True
    # An office block that contains the postcode.
    assert f("B1", "nominatim", "building", "office") is False
    # The enclosing city -- which IS a boundary, just not a postcode one.
    assert f("B14 7", "nominatim", "boundary", "administrative") is False
    # No OSM object at all, only address points synthesised into a label.
    assert f("B11 3SA", "administrative", "place", "postcode") is False
    # A loaded POSTCODE dataset exists to supply the boundary OSM does not have.
    assert f("10001", "dataset", "place", "postcode", dataset_kind="postcode") is True
    # A loaded WARD dataset does not: a ward merely contains the postcode, so
    # calling it a postcode boundary would silence the warning that says the
    # design area is not the postcode.
    assert f("B11 3SA", "dataset", "place", "postcode", dataset_kind="ward") is False
    assert f("B11 3SA", "dataset", "place", "postcode", dataset_kind="") is False


def test_postcode_question_does_not_apply_to_a_place_search():
    f = osm_source.postcode_defines_the_area
    assert f("", "nominatim", "boundary", "administrative") is None
    assert f("   ", "bbox", "", "") is None


def _postcode_resolution(**overrides):
    base = {
        "input_type": "postcode", "polygon_source": "nominatim",
        "postcode_is_boundary": False, "matched_category": "building",
        "matched_type": "office", "area_km2": 0.008,
        "matched": "B1, 50, Summer Hill Road, Jewellery Quarter, Birmingham",
    }
    base.update(overrides)
    return base


def test_preview_warns_when_a_postcode_resolved_to_a_building(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)], resolution=_postcode_resolution())

    out = osm_source.preview_area("B1, Birmingham, United Kingdom")

    hit = [w for w in out["warnings"] if "did not resolve to a postcode boundary" in w]
    assert hit, "a single building was offered with no warning: %s" % out["warnings"]
    # It must say WHAT was matched and how big it is, or the planner cannot judge.
    assert "building/office" in hit[0]
    assert "0.008 km" in hit[0]
    assert "not the postcode" in hit[0]


def test_preview_is_quiet_for_a_real_postcode_boundary(monkeypatch):
    """Germany must not start warning about its own postcodes."""
    _stub_area(monkeypatch, [_premise(1)], resolution=_postcode_resolution(
        postcode_is_boundary=True, matched_category="boundary",
        matched_type="postal_code", area_km2=3.36,
    ))

    out = osm_source.preview_area("12105, Berlin, Germany")

    assert not [w for w in out["warnings"] if "did not resolve to a postcode" in w]


def test_preview_is_quiet_for_an_area_search(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)], resolution={
        "input_type": "area", "postcode_is_boundary": None,
        "matched_category": "boundary", "matched_type": "administrative",
    })

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert not [w for w in out["warnings"] if "did not resolve to a postcode" in w]


def test_preview_centroid_warning_counts_what_the_workbook_will_carry(monkeypatch):
    """The old warning counted the ADDR_ID suffix, so a US ZIP reported
    5,946 premises with no addr:housenumber when 46 % carried one.
    The planner must be told how many will ship with NO address and how many
    took one from the building polygon."""
    building_with_address = {
        "ADDR_ID": "OSM-W7-C", "Address": "High Street", "Housenumber": "7",
        "City": "Asheville", "Postcode": "28801", "Country": "United States",
        "District": "", "HH": 1, "HH_METHOD": "fallback_one",
        "LATITUDE": 35.58, "LONGITUDE": -82.55, "OSM_ID": 7,
    }
    bare_building = {
        "ADDR_ID": "OSM-W8-C", "Address": "", "Housenumber": "",
        "City": "Kenya", "Postcode": "", "Country": "Kenya",
        "District": "", "HH": 1, "HH_METHOD": "fallback_one",
        "LATITUDE": -0.02, "LONGITUDE": 37.07, "OSM_ID": 8,
    }
    _stub_area(monkeypatch, [building_with_address, bare_building])

    out = osm_source.preview_area("Asheville, United States")

    w = " ".join(out["warnings"])
    assert "1 premise(s) have neither a street nor a house number" in w
    assert "1 premise(s) took their address from the building" in w
    assert "have no addr:housenumber" not in w
    assert out["premises"]["without_address"] == 1
    assert out["premises"]["from_building_polygon"] == 1
    assert out["premises"]["from_building_centroid"] == 2


def test_preview_colours_a_fully_addressed_centroid_as_the_building_not_anonymous(monkeypatch):
    """An anonymous warning must not fire when every centroid row carries an
    address from the building polygon: the gap is not the anonymous one."""
    premises = [
        {
            "ADDR_ID": f"OSM-W{i}-C", "Address": "Kaiserstrasse", "Housenumber": str(i),
            "City": "Berlin", "Postcode": "12105", "Country": "Germany",
            "District": "", "HH": 1, "HH_METHOD": "fallback_one",
            "LATITUDE": 52.44, "LONGITUDE": 13.38, "OSM_ID": 100 + i,
        }
        for i in (17, 18)
    ]
    _stub_area(monkeypatch, premises)

    out = osm_source.preview_area("Berlin, Germany")
    w = " ".join(out["warnings"])

    assert "have neither a street nor a house number" not in w
    assert "2 premise(s) took their address from the building" in w
    assert out["premises"]["without_address"] == 0


def test_the_postcode_warning_does_not_duplicate_the_administrative_one(monkeypatch):
    """When the boundary ladder already explained itself, say it once."""
    _stub_area(monkeypatch, [_premise(1)], resolution=_postcode_resolution(
        polygon_source="administrative", matched_category="place",
        matched_type="postcode", area_km2=266.915,
        boundary_name="Birmingham, West Midlands, England", boundary_admin_level="city",
    ))

    out = osm_source.preview_area("B11 3SA, Birmingham, United Kingdom")

    admin = [w for w in out["warnings"] if "enclosing administrative boundary" in w]
    pc = [w for w in out["warnings"] if "did not resolve to a postcode boundary" in w]
    assert admin, "the administrative fallback must still explain itself"
    assert not pc, "one explanation is enough"


def test_too_many_premises_carries_the_count_and_the_cap():
    err = osm_source.TooManyPremises(257127, 20000, hint=" e.g. postcode B1")
    assert err.count == 257127
    assert err.cap == 20000
    assert err.hint == " e.g. postcode B1"
    assert isinstance(err, ValueError)
    # The token stays machine-readable, so anything matching the prefix or
    # reading the two numbers out of a log still works.
    assert str(err) == "too_many_premises:257127:20000"


def test_oversize_detail_quotes_the_cap_that_actually_applied():
    run = osm_source.oversize_detail(257127, osm_source.MAX_PREMISES, " e.g. postcode B1 (9)")
    assert "per-run cap is 20000" in run
    assert " e.g. postcode B1 (9)" in run

    # A cap the caller set is NOT the per-run cap, and calling it that sends the
    # planner looking for a setting that would not have helped.
    caller = osm_source.oversize_detail(3000, 1000)
    assert "the cap this request set (1000)" in caller
    assert "per-run cap" not in caller
    assert "20000" not in caller


def test_oversize_detail_never_claims_max_premises_can_be_raised_by_request():
    """It cannot: the run cap is a constant inside build_inputs, no request field
    reaches it.  The old text said 'or raise max_premises knowingly', which is
    only true of the preview."""
    text = osm_source.oversize_detail(25000, osm_source.MAX_PREMISES)
    assert "not a request option" in text
    assert "raise max_premises" not in text


# ---------------------------------------------------------------------------
# A hint must be reachable, or it costs a round trip to discover it is not
# ---------------------------------------------------------------------------
#
# The B11 3SA preview is the 3.396 km2 ward *Sparkbrook & Balsall Heath East*,
# 6,058 premises, over the cap -- and it offered "e.g. postcode B11 3SA (73
# premises)", a real bucket INSIDE the ward.  Typing it back resolves through the
# containing-point rung to the same ward, so the suggestion was a loop.

def _ward_resolution(**over):
    res = {
        "country_code": "GB", "input_type": "postcode", "polygon_source": "dataset",
        "boundary_kind": "ward", "boundary_name": "Sparkbrook & Balsall Heath East",
        "boundary_code": "E05011170", "area_km2": 3.396,
    }
    res.update(over)
    return res


def test_a_postcode_inside_a_loaded_ward_boundary_is_not_offered():
    kinds = osm_source.sub_area_kinds_that_narrow(_ward_resolution())
    assert kinds == [], kinds
    assert "same area" in osm_source.narrowing_fallback(kinds)


def test_a_postcode_dataset_would_make_the_postcode_reachable_again():
    """A postcode dataset is a strictly smaller polygon than a ward, which is what
    breaks the loop -- so the offer returns as soon as one is loaded."""
    kinds = osm_source.sub_area_kinds_that_narrow(_ward_resolution(), loaded_kinds=["postcode"])
    assert "postcode" in kinds


def test_a_postcode_is_still_offered_from_an_area_that_is_not_a_dataset_polygon():
    """Everywhere else a containing polygon is strictly smaller, so the suggestion
    stands -- including the measured case it was written for (a 266.9 km2 city
    narrowing to a 3.4 km2 ward)."""
    for source, kind in (("administrative", None), ("nominatim", None), ("bbox", None),
                         ("dataset", "postcode")):
        kinds = osm_source.sub_area_kinds_that_narrow(
            {"polygon_source": source, "boundary_kind": kind})
        assert "postcode" in kinds, (source, kind)


def test_a_district_is_only_offered_when_a_named_dataset_can_resolve_it():
    """The strict one, and it is strict because of a measurement.

    The Mariendorf preview offered "or Tempelhof (13 premises)" -- a real
    13-premise bucket inside it -- and Tempelhof resolves to **12.137 km²**,
    larger than the 9.343 km² area being previewed.  A district name has no
    polygon of its own; the only one we can stand behind is a loaded one.
    """
    berlin = {"polygon_source": "nominatim", "boundary_kind": None, "area_km2": 9.343}

    # Germany: nothing loaded that carries names, so no district is offered.
    assert osm_source.sub_area_kinds_that_narrow(berlin) == ["postcode"]
    # With the ONS wards loaded, a district name (Handsworth) is a real 1.565 km2
    # polygon inside a 266.9 km2 city -- that one can be stood behind.
    city = {"polygon_source": "administrative", "boundary_kind": None}
    assert osm_source.sub_area_kinds_that_narrow(city, loaded_kinds=["ward"]) == [
        "postcode", "district"]
    # A loaded postcode dataset resolves postcodes, not names.
    assert osm_source.sub_area_kinds_that_narrow(city, loaded_kinds=["postcode"]) == ["postcode"]


def test_the_postcode_buckets_that_do_narrow_are_measured_ones():
    """Why postcodes stay in the offer for Germany: 12107 (4.38 km²) is genuinely
    smaller than the 9.343 km² Mariendorf it sits in, unlike the district name."""
    sub_areas = {"postcode": [{"value": "12107", "premises": 3404}]}
    assert "12107" in osm_source.narrowing_hint(sub_areas, kinds=["postcode"])


def test_the_hint_stops_at_the_ward_boundary_but_keeps_the_buckets():
    sub_areas = {
        "postcode": [{"value": "B11 3SA", "premises": 73}],
        "district": [{"value": "Sparkbrook", "premises": 2}],
    }
    hint = osm_source.narrowing_hint(sub_areas, kinds=[])
    assert hint == "", "a bucket inside the current boundary is not a narrowing"
    # The breakdown is a fact about the area and is still reported; only the
    # OFFER changes.
    assert sub_areas["postcode"][0]["premises"] == 73


def test_the_hint_does_not_offer_the_postcode_we_are_already_in():
    sub_areas = {"postcode": [{"value": "12105", "premises": 5000},
                             {"value": "12107", "premises": 900}]}

    hint = osm_source.narrowing_hint(sub_areas, exclude=["12105"])

    assert "12105" not in hint, "previewing 12105 and suggesting 12105 is a loop"
    assert "12107" in hint, "the next bucket is a real narrowing"


def test_own_area_values_are_the_input_postcode_and_a_postcode_boundary_code():
    assert osm_source.own_area_values({}, "12105") == ["12105"]
    assert osm_source.own_area_values(
        {"boundary_kind": "postcode", "boundary_code": "10001"}) == ["10001"]
    assert osm_source.own_area_values({"boundary_kind": "ward", "boundary_code": "E0501"}) == []


def test_own_area_values_include_the_name_we_searched_for():
    """Measured: the Mariendorf preview offered "e.g. postcode 12107 (3404
    premises), or Mariendorf (6131 premises)" -- its own area name."""
    assert "Mariendorf" in osm_source.own_area_values(
        {"area": "Mariendorf, Berlin, Germany"})
    # A dataset ward's own record name is a bucket it could be offered as too.
    assert "Handsworth" in osm_source.own_area_values(
        {"area": "Handsworth, Birmingham, United Kingdom", "polygon_source": "dataset",
         "boundary_name": "Handsworth"})
    # The full display name of an administrative boundary is not a bucket value,
    # and excluding it must not become a way to match nothing.
    assert osm_source.own_area_values({"polygon_source": "administrative"}) == []


def test_the_hint_does_not_offer_the_area_it_is_describing(monkeypatch):
    sub_areas = {
        "postcode": [{"value": "12107", "premises": 3404}],
        "district": [{"value": "Mariendorf", "premises": 6131}],
    }

    hint = osm_source.narrowing_hint(sub_areas, exclude=["Mariendorf"])

    assert "12107" in hint, "a genuinely different postcode is still a narrowing"
    assert "Mariendorf" not in hint


def test_preview_reports_which_buckets_it_can_narrow_to(monkeypatch):
    _stub_area(monkeypatch, [_premise(i) for i in range(2)], resolution=_ward_resolution())
    monkeypatch.setattr(osm_source, "boundary_dataset_kinds", lambda cc: ["ward"])

    out = osm_source.preview_area("B11 3SA, Birmingham, United Kingdom",
                                 postcode="B11 3SA")

    assert out["sub_areas_resolvable"] == []
    assert out["sub_areas"]["postcode"], "the breakdown is still reported"


def test_preview_keeps_offering_buckets_from_a_city(monkeypatch):
    _stub_area(monkeypatch, [_premise(i) for i in range(2)], resolution={
        "input_type": "area", "polygon_source": "administrative",
        "boundary_admin_level": "city", "area_km2": 266.915,
    })
    monkeypatch.setattr(osm_source, "boundary_dataset_kinds", lambda cc: ["ward"])

    out = osm_source.preview_area("Birmingham, United Kingdom")

    assert out["sub_areas_resolvable"] == ["postcode", "district"]


def test_preview_offers_no_district_where_no_named_dataset_is_loaded(monkeypatch):
    """The German case: postcodes resolve, district names do not."""
    _stub_area(monkeypatch, [_premise(i) for i in range(2)], resolution={
        "input_type": "area", "polygon_source": "nominatim", "area_km2": 9.343,
    })
    monkeypatch.setattr(osm_source, "boundary_dataset_kinds", lambda cc: [])

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert out["sub_areas_resolvable"] == ["postcode"]


def test_narrowing_hint_names_the_largest_buckets_of_each_kind():
    sub_areas = {
        "postcode": [{"value": "B1", "premises": 5000}, {"value": "B2", "premises": 10}],
        "district": [{"value": "Selly Oak", "premises": 900}],
    }
    hint = osm_source.narrowing_hint(sub_areas)
    assert "postcode B1 (5000 premises)" in hint
    assert "Selly Oak (900 premises)" in hint
    assert "B2" not in hint, "only the biggest bucket is offered"


def test_narrowing_hint_is_empty_when_the_data_names_no_smaller_area():
    assert osm_source.narrowing_hint({"postcode": [], "district": []}) == ""
    assert osm_source.narrowing_hint({}) == ""


def test_narrowing_hint_does_not_offer_the_same_value_twice():
    sub_areas = {
        "postcode": [{"value": "B1", "premises": 5}],
        "district": [{"value": "B1", "premises": 5}],
    }
    assert osm_source.narrowing_hint(sub_areas).count("B1") == 1


# ---------------------------------------------------------------------------
# Geometry / cache-key helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Authoritative boundary datasets (ONS wards) and the dataset rung
# ---------------------------------------------------------------------------
#
# Measured, in Birmingham: OSM carries no ward polygons inside the city, so
# "Handsworth, Birmingham" resolved to the whole 266.9 km2 city, and a postcode
# (a tag on address points, never a boundary) resolved to the same city.  The
# dataset rung is what makes either of them narrow.  These tests pin the two
# things that make it safe: it is consulted only when OSM came up short, and the
# polygon it offers must BE the named place or CONTAIN the point.

def test_a_ward_does_not_stand_in_for_a_postcode_boundary():
    """The dataset flag is not "a dataset answered", it is "a postcode polygon".

    Both come from rung 1, and reading them the same way is what would let a
    design be published on a ward the planner never asked for, with no warning.
    """
    f = osm_source.postcode_defines_the_area
    assert f("B11 3SA", "dataset", "", "", dataset_kind="ward") is False
    assert f("10001", "dataset", "", "", dataset_kind="postcode") is True


def test_area_name_candidates_take_the_local_name_first():
    assert osm_source.area_name_candidates("Handsworth, Birmingham, United Kingdom") == [
        "Handsworth", "Handsworth, Birmingham, United Kingdom",
    ]
    assert osm_source.area_name_candidates("Mogarraz") == ["Mogarraz"]
    assert osm_source.area_name_candidates("  ") == []
    assert osm_source.area_name_candidates(", ,") == []


def _ward_record(code="E05011142", name="Handsworth", **over):
    rec = {
        "country_code": "GB", "code": code, "name": name, "kind": "ward",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "properties": {"vintage": "May 2024", "licence": "OGL v3"},
    }
    rec.update(over)
    return rec


def test_fetch_uk_wards_pages_and_carries_its_licence(monkeypatch):
    """The service caps a page at 2,000 and the full set is 8,396, so a page walk
    is the only way to load it -- and it must be ordered, or a page can repeat or
    skip a ward."""
    urls = []
    pages = [
        {"features": [
            {"properties": {"WD24CD": "E05011142", "WD24NM": "Handsworth",
                            "WD24NMW": ""},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
            {"properties": {"WD24CD": "E05011170", "WD24NM": "Sparkbrook & Balsall Heath East",
                            "WD24NMW": ""},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]]}},
        ]},
        {"features": []},
    ]

    def fake_http(url, *a, **k):
        urls.append(url)
        return pages[len(urls) - 1]

    monkeypatch.setattr(osm_source, "_http_json", fake_http)
    records = list(osm_source.fetch_uk_wards(page_size=2))

    assert [r["code"] for r in records] == ["E05011142", "E05011170"]
    assert records[0]["kind"] == "ward"
    assert records[0]["country_code"] == "GB"
    # A loaded polygon has to be traceable to what it came from and under which
    # licence, or it cannot be judged.
    assert records[0]["properties"]["vintage"] == "May 2024"
    assert "Open Government Licence" in records[0]["properties"]["licence"]
    assert "resultOffset=0" in urls[0]
    assert "orderByFields=WD24CD" in urls[0]
    assert "outSR=4326" in urls[0]
    assert len(urls) == 2, "the walk stops on an empty page"


def test_fetch_uk_wards_honours_a_limit(monkeypatch):
    monkeypatch.setattr(osm_source, "_http_json", lambda *a, **k: {"features": [
        {"properties": {"WD24CD": f"E050{i:05d}", "WD24NM": f"W{i}", "WD24NMW": ""},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
        for i in range(5)
    ]})

    assert len(list(osm_source.fetch_uk_wards(page_size=5, limit=3))) == 3


def test_a_reingest_replaces_previous_polygons_rather_than_merging(monkeypatch):
    """An ONS vintage change abolishes wards.  Upserting alone would update the
    survivors and leave the abolished ones loaded as if they still existed."""
    calls = []
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "boundary_dataset_purge",
                        lambda cc, kind=None, source=None: calls.append(("purge", cc, kind)) or 7)
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda chunk, source: calls.append(("load", source, None)) or len(chunk))
    # One ward, so the batched loader makes exactly one call.
    monkeypatch.setattr(osm_source, "fetch_uk_wards",
                        lambda **k: iter([{"code": "E05011142", "name": "Handsworth",
                                          "geometry": {"type": "Polygon",
                                                       "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]))

    result = osm_source.ingest_uk_wards()

    assert calls[0] == ("purge", "GB", "ward"), "purge must run, and first"
    assert calls[1][0] == "load"
    assert calls[1][1] == osm_source.UK_WARDS_SOURCE["name"]
    assert result["purged"] == 7 and result["loaded"] == 1
    assert "Open Government Licence" in result["licence"]

    calls.clear()
    osm_source.ingest_uk_wards(replace=False)
    assert [c[0] for c in calls] == ["load"], "--keep-existing must not purge"


def test_boundary_dataset_containing_asks_for_the_smallest_polygon(monkeypatch):
    seen = {}

    def fake_query(sql, params=()):
        seen["sql"], seen["params"] = sql, params
        return [{"code": "E05011170", "name": "Sparkbrook & Balsall Heath East",
                 "kind": "ward", "admin_level": "ward", "source": "ONS Wards",
                 "properties": {"vintage": "May 2024"}, "area_km2": 3.4,
                 "geom_json": '{"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}'}]

    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", fake_query)

    hit = osm_source.boundary_dataset_containing("gb", -1.87, 52.45)

    assert hit and hit["kind"] == "ward" and hit["area_km2"] == 3.4
    assert hit["geometry"]["type"] == "Polygon"
    assert "ST_Contains" in seen["sql"], "a point outside the polygon must not match"
    assert "ORDER BY ST_Area" in seen["sql"] and "ASC LIMIT 1" in seen["sql"]
    assert seen["params"][0] == "GB" and seen["params"][1] == -1.87


def test_dataset_lookups_do_not_query_without_a_country(monkeypatch):
    """An empty country must not turn into a worldwide search."""
    def boom(*a, **k):
        raise AssertionError("queried without a country")

    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", boom)

    assert osm_source.boundary_dataset_containing("", 1.0, 2.0) is None
    assert osm_source.boundary_dataset_by_name("", ["Handsworth"]) is None
    assert osm_source.boundary_dataset_by_name("GB", []) is None


def test_boundary_dataset_by_name_is_guarded_by_containment(monkeypatch):
    """There is a Handsworth in Birmingham AND one in Sheffield, so the name alone
    is not enough -- it has to be the one inside the area already resolved."""
    seen = {}

    def fake_query(sql, params=()):
        seen["sql"], seen["params"] = sql, params
        return []

    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", fake_query)
    inside = osm_source.bbox_polygon([-2.0, 52.4, -1.8, 52.6])

    osm_source.boundary_dataset_by_name("GB", ["Handsworth"], kind="ward", within=inside)
    assert "ST_Contains" in seen["sql"]
    assert seen["params"][1] == ["handsworth"], "case must not decide the match"

    osm_source.boundary_dataset_by_name("GB", ["Handsworth"], kind="ward", within=None)
    assert "ST_Contains" not in seen["sql"], "no area given, no containment to test"


def _dataset_resolution(**over):
    """A Nominatim result for an area whose OSM ladder came up short."""
    res = {
        "lat": "52.5140", "lon": "-1.9360",
        "bbox": [-2.03, 52.38, -1.72, 52.60],
        "geometry": {"type": "Polygon", "coordinates": [
            [[-2.03, 52.38], [-1.72, 52.38], [-1.72, 52.60], [-2.03, 52.38]]]},
        "properties": {
            "display_name": "Birmingham, West Midlands, England, United Kingdom",
            "osm_type": "relation", "osm_id": 10000,
            "category": "boundary", "type": "administrative",
            "address": {"country_code": "gb", "city": "Birmingham"},
        },
    }
    res.update(over)
    return res


def _stub_ladder(monkeypatch, result, admin=None, containing=None, by_name=None):
    """resolve_area with the network, the database and the pipeline removed."""
    calls = {"containing": [], "by_name": []}
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "_query", lambda *a, **k: [])
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source, "nominatim_query", lambda area: area)
    monkeypatch.setattr(osm_source, "nominatim_search", lambda area, cc="": [result])
    monkeypatch.setattr(osm_source, "pick_area_result", lambda res, area, cc="": res[0])
    monkeypatch.setattr(osm_source, "admin_boundary_for_point", lambda lat, lon: admin)

    def _containing(cc, lon, lat, kind=None):
        calls["containing"].append((cc, lon, lat))
        return containing

    def _by_name(cc, names, kind=None, within=None):
        calls["by_name"].append((cc, list(names), within is not None))
        return by_name

    monkeypatch.setattr(osm_source, "boundary_dataset_containing", _containing)
    monkeypatch.setattr(osm_source, "boundary_dataset_by_name", _by_name)
    return calls


def test_a_ward_inside_the_named_city_is_preferred_over_the_city(monkeypatch):
    ward = _ward_record()
    ward["source"] = osm_source.UK_WARDS_SOURCE["name"]
    calls = _stub_ladder(monkeypatch, _dataset_resolution(), by_name=ward)

    out = osm_source.resolve_area("Handsworth, Birmingham, United Kingdom",
                                 country_code="GB")

    assert out["polygon_source"] == "dataset"
    assert out["boundary_kind"] == "ward"
    assert out["boundary_name"] == "Handsworth"
    assert out["boundary_code"] == "E05011142"
    assert out["postcode_is_boundary"] is None, "this was not a postcode input"
    # It must have asked for the LOCAL name, in the country that resolved, and
    # inside the polygon the ladder already found.
    assert calls["by_name"] == [("GB", ["Handsworth", "Handsworth, Birmingham, United Kingdom"], True)]
    assert not calls["containing"], "a place name must not be resolved by point"


def test_a_postcode_narrows_to_the_ward_that_contains_it(monkeypatch):
    ward = _ward_record("E05011170", "Sparkbrook & Balsall Heath East")
    calls = _stub_ladder(
        monkeypatch,
        _dataset_resolution(
            geometry={"type": "Polygon", "coordinates": [
                [[-1.88, 52.45], [-1.86, 52.45], [-1.86, 52.47], [-1.88, 52.45]]]},
            properties={"display_name": "B11 3SA, Birmingham", "osm_type": "node",
                        "osm_id": 1, "category": "place", "type": "postcode",
                        "address": {"country_code": "gb", "city": "Birmingham"}},
        ),
        containing=ward,
    )

    out = osm_source.resolve_area(
        "B11 3SA, Birmingham, United Kingdom",
        country_code="GB", input_type="postcode", postcode="B11 3SA",
    )

    assert out["polygon_source"] == "dataset"
    assert out["boundary_kind"] == "ward"
    assert out["postcode_is_boundary"] is False, "a ward is not a postcode boundary"
    assert calls["containing"] == [("GB", -1.936, 52.514)]
    assert not calls["by_name"], "a postcode is not resolved by name"


def test_a_real_postcode_boundary_is_never_replaced_by_a_dataset(monkeypatch):
    """12105, Berlin: OSM HAS the postcode polygon.  A loaded ward dataset in
    that country must not be allowed to take a good boundary away."""
    ward = _ward_record("DE0001", "Some Ward")
    calls = _stub_ladder(
        monkeypatch,
        _dataset_resolution(
            geometry={"type": "Polygon", "coordinates": [
                [[13.3, 52.4], [13.4, 52.4], [13.4, 52.5], [13.3, 52.4]]]},
            properties={"display_name": "12105, Tempelhof, Berlin", "osm_type": "relation",
                        "osm_id": 1105327, "category": "boundary", "type": "postal_code",
                        "address": {"country_code": "de", "city": "Berlin"}},
        ),
        containing=ward,
    )

    out = osm_source.resolve_area("12105, Berlin, Germany", country_code="DE",
                                 input_type="postcode", postcode="12105")

    assert out["polygon_source"] == "nominatim"
    assert not out.get("boundary_kind"), "no dataset, no admin rung: nothing to report"
    assert out["postcode_is_boundary"] is True
    assert not calls["containing"], "a real postcode boundary left the ladder alone"


def test_a_place_search_falls_back_to_the_point_only_for_a_postcode(monkeypatch):
    calls = _stub_ladder(monkeypatch, _dataset_resolution(), by_name=None)

    out = osm_source.resolve_area("Mariendorf, Berlin, Germany", country_code="DE")

    assert out["polygon_source"] == "nominatim", "no dataset match, nothing changes"
    assert not calls["containing"]


def test_preview_says_which_dataset_area_a_postcode_landed_on(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)], resolution={
        "input_type": "postcode", "polygon_source": "dataset",
        "boundary_kind": "ward", "boundary_name": "Sparkbrook & Balsall Heath East",
        "boundary_code": "E05011170", "boundary_dataset_km2": 3.4,
        "boundary_source": osm_source.UK_WARDS_SOURCE["name"],
        "postcode_is_boundary": False, "area_km2": 3.4,
        "matched_category": "place", "matched_type": "postcode",
    })

    out = osm_source.preview_area("B11 3SA, Birmingham, United Kingdom")

    hit = [w for w in out["warnings"] if "ward containing it" in w]
    assert hit, "the design area is not the postcode and that must be said: %s" % out["warnings"]
    assert "Sparkbrook & Balsall Heath East" in hit[0]
    assert "E05011170" in hit[0]
    assert osm_source.UK_WARDS_SOURCE["name"] in hit[0]
    # One explanation, not two: the point-only postcode text says "that object",
    # which would be wrong here -- the area is a ward, not what OSM matched.
    assert not [w for w in out["warnings"] if "did not resolve to a postcode boundary" in w]


def test_preview_names_the_dataset_record_when_a_ward_is_used_by_name(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)], resolution={
        "input_type": "area", "polygon_source": "dataset",
        "boundary_kind": "ward", "boundary_name": "Handsworth",
        "boundary_code": "E05011142", "boundary_vintage": "May 2024",
        "boundary_source": osm_source.UK_WARDS_SOURCE["name"],
        "postcode_is_boundary": None, "area_km2": 1.57,
    })

    out = osm_source.preview_area("Handsworth, Birmingham, United Kingdom")

    hit = [w for w in out["warnings"] if "authoritative dataset" in w]
    assert hit
    assert "Handsworth" in hit[0] and "E05011142" in hit[0]
    assert "ward" in hit[0] and "May 2024" in hit[0]


# ---------------------------------------------------------------------------
# A cached resolution must not outlive an ingest
# ---------------------------------------------------------------------------
#
# Measured, and the third time a cache hid a rule change on this feature: the ONS
# wards were loaded and "B11 3SA, Birmingham, United Kingdom" still resolved to
# the 266.9 km2 city, from a row written before the ingest -- on exactly the
# postcode the dataset exists to narrow.  A hand-bumped version constant would
# have to be remembered on every ingest, so the cache invalidates itself instead.

def test_dataset_stamp_is_empty_without_a_loaded_dataset(monkeypatch):
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", lambda *a, **k: [{"at": None}])
    assert osm_source.dataset_stamp("GB") == ""

    # No country, no query: this must not become a worldwide lookup.
    assert osm_source.dataset_stamp("") == ""


def test_dataset_stamp_survives_a_missing_table(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", boom)
    assert osm_source.dataset_stamp("GB") == ""


def _cached_row(resolution_extra=None):
    cached_resolution = {
        "area": "B11 3SA, Birmingham, United Kingdom",
        "bbox": [-2.03, 52.38, -1.72, 52.60],
        "polygon": {"type": "Polygon", "coordinates": [
            [[-2.03, 52.38], [-1.72, 52.38], [-1.72, 52.60], [-2.03, 52.38]]]},
        "polygon_source": "administrative",
        "matched": "Birmingham, West Midlands, England, United Kingdom",
        "area_km2": 266.915, "input_type": "postcode",
    }
    cached_resolution.update(resolution_extra or {})
    return {
        "area": cached_resolution["area"],
        "display_name": cached_resolution["matched"],
        "osm_type": "relation", "osm_id": 10000,
        "polygon_source": "administrative", "resolution": cached_resolution,
        "bbox_json": osm_source.json.dumps(osm_source.bbox_polygon([-2.03, 52.38, -1.72, 52.60])),
        "poly_json": osm_source.json.dumps(cached_resolution["polygon"]),
    }


def test_a_cached_area_is_re_resolved_once_the_dataset_changes(monkeypatch):
    served = []
    queries = []

    def fake_query(sql, params=()):
        queries.append(sql)
        if "boundary_areas" in sql:            # the dataset stamp
            return [{"at": "2026-09-24 11:00:00+00"}]
        return [_cached_row()]                  # the area_cache hit

    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", fake_query)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source, "nominatim_query", lambda area: area)
    monkeypatch.setattr(osm_source, "nominatim_search",
                        lambda area, cc="": served.append(area) or [])
    monkeypatch.setattr(osm_source, "boundary_dataset_containing", lambda *a, **k: None)
    monkeypatch.setattr(osm_source, "boundary_dataset_by_name", lambda *a, **k: None)

    with pytest.raises(LookupError):
        osm_source.resolve_area("B11 3SA, Birmingham, United Kingdom", country_code="GB",
                               input_type="postcode", postcode="B11 3SA")

    # The stale row must NOT have been served: the service was asked again.
    assert served, "a row cached before the ingest was served anyway"


def test_a_cached_area_is_served_when_the_dataset_state_is_unchanged(monkeypatch):
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "_query", lambda sql, params=(): (
        [{"at": "2026-09-24 11:00:00+00"}] if "boundary_areas" in sql
        else [_cached_row({"dataset_stamp": "2026-09-24 11:00:00+00"})]
    ))
    monkeypatch.setattr(osm_source, "nominatim_query", lambda area: area)
    monkeypatch.setattr(osm_source, "nominatim_search",
                        lambda area, cc="": pytest.fail("re-resolved an unchanged area"))

    out = osm_source.resolve_area("B11 3SA, Birmingham, United Kingdom", country_code="GB",
                                 input_type="postcode", postcode="B11 3SA")

    assert out["area_km2"] == 266.915
    assert out["polygon_source"] == "administrative"


# ---------------------------------------------------------------------------
# The US ZIP dataset: a country-sized load needs paging, batching and a caveat
# ---------------------------------------------------------------------------
#
# Measured: 33,791 ZCTAs, 29 KB per feature at full TIGER resolution (~980 MB for
# the set), 3.1 KB with an 11 m `maxAllowableOffset` (~106 MB).  A load that size
# has to be paged and batched, and it has to say what a ZCTA IS -- it is the
# Census Bureau's approximation of a ZIP code area, not the USPS delivery route.

def test_fetch_us_zctas_pages_orders_and_simplifies(monkeypatch):
    urls = []
    pages = [{"features": [
        {"properties": {"ZCTA5": "10001", "NAME": "ZCTA5 10001", "POP100": 21150,
                        "HU100": 12751},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
    ]}, {"features": []}]

    def fake_http(url, *a, **k):
        urls.append(url)
        return pages[len(urls) - 1]

    monkeypatch.setattr(osm_source, "_http_json", fake_http)
    records = list(osm_source.fetch_us_zctas(page_size=1))

    assert [r["code"] for r in records] == ["10001"]
    assert records[0]["kind"] == "postcode", "a ZIP bucket is reachable off this kind"
    assert records[0]["country_code"] == "US"
    # Population and housing units come with the polygon, and are evidence about
    # how big the area's design really is.
    assert records[0]["properties"]["population"] == 21150
    assert records[0]["properties"]["housing_units"] == 12751
    assert "maxAllowableOffset" in urls[0], "980 MB without simplification"
    assert "orderByFields=ZCTA5" in urls[0]
    assert len(urls) == 2, "the walk stops on an empty page"


def test_fetch_us_zctas_can_resume_from_an_offset(monkeypatch):
    """A country-sized load outlives one command, so it resumes rather than
    re-fetching what is already loaded."""
    urls = []
    monkeypatch.setattr(osm_source, "_http_json",
                        lambda url, *a, **k: urls.append(url) or {"features": []})

    list(osm_source.fetch_us_zctas(start_offset=29900))

    assert "resultOffset=29900" in urls[0]


def test_a_country_sized_load_is_batched_and_reports_progress(monkeypatch):
    seen = []
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda chunk, source: seen.append(len(chunk)) or len(chunk))

    records = ({"code": str(i), "geometry": {"type": "Point", "coordinates": [0, 0]}}
               for i in range(5))
    total = osm_source.ingests_in_batches(records, source="X", batch=2,
                                          on_batch=lambda n: seen.append(f"msg{n}"))

    assert total == 5
    # Two full batches, the message after each, and the remainder.
    assert seen == [2, "msg2", 2, "msg4", 1, "msg5"]


def test_a_zcta_carries_its_caveat_so_the_planner_sees_it(monkeypatch):
    """A ZIP resolved to its ZCTA is not USPS precision, and the warning says so."""
    _stub_area(monkeypatch, [_premise(1)], resolution={
        "input_type": "postcode", "polygon_source": "dataset",
        "boundary_kind": "postcode", "boundary_name": "ZCTA5 10001",
        "boundary_code": "10001", "boundary_vintage": "2020 Census",
        "boundary_source": osm_source.US_ZCTA_SOURCE["name"],
        "boundary_note": osm_source.US_ZCTA_SOURCE["note"],
        "postcode_is_boundary": True, "area_km2": 1.615,
    })

    out = osm_source.preview_area("10001, New York, United States", postcode="10001")

    hit = [w for w in out["warnings"] if "authoritative dataset" in w]
    assert hit, out["warnings"]
    assert "ZCTA5 10001" in hit[0] and "10001" in hit[0]
    # The ZCTA/ZIP distinction, stated rather than left to the name.
    assert "delivery routes, not areas" in hit[0]
    assert "not the postal service's own boundary" in hit[0]


def test_area_key_ignores_case_and_padding():
    assert osm_source.area_key("Mariendorf, Berlin, Germany") == \
        osm_source.area_key("  mariendorf ,  berlin,germany ")


def test_polygon_bbox_and_bbox_polygon_round_trip():
    bbox = [13.31, 52.41, 13.42, 52.47]
    poly = osm_source.bbox_polygon(bbox)
    assert poly["type"] == "Polygon"
    assert osm_source.polygon_bbox(poly) == bbox


# -----------------------------------------------------------------------
# The area download: one fetch per bbox, with progress
# -----------------------------------------------------------------------
# A cold city takes 10-16 minutes to download from Overpass (measured:
# Southampton 56 km^2 ~16 min).  Three things had to be true for the page to
# show a boundary and nothing else: the boundary call returned, the counts call
# was cut at the gateway's 900 s, and the seven input-layer calls that follow
# each started their OWN download because the cache is only written when the
# whole fetch finishes.  These tests hold those properties in place.

BBOX = [13.31, 52.41, 13.42, 52.47]


@pytest.fixture(autouse=True)
def _clean_fetch_registry():
    """The fetch registry is module state; tests must not leak into each other."""
    with osm_source._FETCH_LOCK:
        osm_source._FETCHES.clear()
    yield
    with osm_source._FETCH_LOCK:
        osm_source._FETCHES.clear()


def test_a_second_caller_joins_the_running_download_instead_of_starting_one(monkeypatch):
    queries = []
    started = threading.Event()
    release = threading.Event()

    def fake_query(body, bbox, timeout=180):
        queries.append(body)
        started.set()
        # Hold the first fetch open so the second call really does find it
        # in flight rather than finished.
        release.wait(5)
        return [], "https://overpass.example/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", fake_query)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "FETCH_WAIT_SECONDS", 5)

    first = osm_source.start_area_fetch("Southampton", BBOX)
    assert first["fetching"] is True
    assert started.wait(5), "the download never started"

    # Six more callers (the input-layer calls the page fires in parallel) for
    # the same bbox: one download, not seven.
    for _ in range(6):
        osm_source.start_area_fetch("Southampton", BBOX)
    release.set()
    for _ in range(100):
        if osm_source.area_fetch_state("Southampton", BBOX)["state"] == "ready":
            break
        time.sleep(0.05)

    state = osm_source.area_fetch_state("Southampton", BBOX)
    assert state["state"] == "ready", state
    # Exactly one pass over the groups -- one download of the city.
    assert len(queries) == len(osm_source._OVERPASS_GROUPS)


def test_a_waiting_caller_gets_the_result_rather_than_refetching(monkeypatch):
    """ensure_area_data() must join the in-flight download and read its cache."""
    queries = []
    release = threading.Event()
    started = threading.Event()

    def fake_query(body, bbox, timeout=180):
        queries.append(body)
        started.set()
        release.wait(5)
        return [], "https://overpass.example/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", fake_query)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "FETCH_WAIT_SECONDS", 5)

    # The cache only exists once the download has FINISHED -- the condition
    # that used to make every parallel caller start its own fetch.
    cache = {"row": None}

    def covered(bbox):
        return cache["row"]

    monkeypatch.setattr(osm_source, "covered_by_cache", covered)

    osm_source.start_area_fetch("Southampton", BBOX)
    assert started.wait(5)
    result = {}

    def caller():
        result["value"] = osm_source.ensure_area_data("Southampton", BBOX)

    waiter = threading.Thread(target=caller)
    waiter.start()
    time.sleep(0.5)
    # The download finishes and writes the extract record.
    cache["row"] = {
        "detail": "https://overpass.example/api/interpreter",
        "fetched_at": osm_source._now(),
        "counts": {"buildings": 10},
    }
    release.set()
    waiter.join(10)

    assert "value" in result, "ensure_area_data never returned"
    assert result["value"]["source"] == "cache"
    # One download shared by both callers.
    assert len(queries) == len(osm_source._OVERPASS_GROUPS)


def test_the_fetch_reports_what_it_is_doing(monkeypatch):
    seen = []

    def fake_query(body, bbox, timeout=180):
        # Capture the state the page would poll WHILE this group runs.
        seen.append(osm_source.area_fetch_state("Kreuzberg", BBOX)["state"])
        return [], "https://overpass.example/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", fake_query)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)

    osm_source.ensure_area_data("Kreuzberg", BBOX)
    state = osm_source.area_fetch_state("Kreuzberg", BBOX)

    # A phase per group, each with a human label and a done-count, then ready.
    assert seen[0] == "fetching_buildings"
    assert "Downloading buildings" in osm_source._FETCH_PHASE_LABELS["fetching_buildings"]
    assert state["state"] == "ready"
    assert state["label"] == "Area data is ready"
    assert state["groups_done"] == state["groups_total"] == len(osm_source._OVERPASS_GROUPS)
    assert state["elapsed_s"] is not None and state["elapsed_s"] >= 0


def test_each_group_is_stored_as_it_arrives(monkeypatch):
    """Storing per group, not at the end: the counts and the point layers
    become available to whatever reads the store while the rest downloads."""
    stored = []

    def fake_query(body, bbox, timeout=180):
        return [{"type": "way", "id": 1}], "https://overpass.example/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", fake_query)
    monkeypatch.setattr(
        osm_source, "store_overpass_elements",
        lambda elements: stored.append(len(list(elements))) or {"buildings": 1},
    )
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)

    out = osm_source.ensure_area_data("Kreuzberg", BBOX)

    assert len(stored) == len(osm_source._OVERPASS_GROUPS)
    # The recorded counts are THIS area's, not the store's totals: four groups
    # each writing one building row. Recording store-wide totals here claimed a
    # 40 km^2 village had downloaded every building the platform has ever seen.
    assert out["counts"]["buildings"] == len(osm_source._OVERPASS_GROUPS)


def test_a_failed_download_is_reported_and_can_be_retried(monkeypatch):
    attempts = []

    def fake_query(body, bbox, timeout=180):
        attempts.append(body)
        if len(attempts) == 1:
            raise RuntimeError("Overpass request failed on all mirrors: 504")
        return [], "https://overpass.example/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", fake_query)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)
    monkeypatch.setattr(osm_source, "FETCH_WAIT_SECONDS", 1)

    with pytest.raises(RuntimeError) as excinfo:
        osm_source.ensure_area_data("Kreuzberg", BBOX)
    # The reason is carried, not swallowed into a generic failure.
    assert "overpass_fetch_failed" in str(excinfo.value)
    assert "504" in str(excinfo.value)

    # A retry is a real retry: the failed entry must not block it.
    out = osm_source.ensure_area_data("Kreuzberg", BBOX)
    assert out["source"] == "fetched"
    assert len(attempts) > len(osm_source._OVERPASS_GROUPS)


def test_the_boundary_call_starts_the_download(monkeypatch):
    """The page asks for the boundary first; the download must already be
    running by the time it asks for the counts, or the wait is paid twice."""
    started = []
    monkeypatch.setattr(
        osm_source, "start_area_fetch",
        lambda area, bbox: started.append((area, list(bbox))) or {"state": "fetching"},
    )
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)

    state = osm_source.ensure_area_data_background("Kreuzberg", BBOX)

    assert started == [("Kreuzberg", BBOX)]
    assert state["state"] == "fetching"


def test_a_boundary_that_resolves_to_a_whole_country_is_not_downloaded_automatically(monkeypatch):
    """"Galway, Ireland" resolves to Galway BAY and the administrative rung
    returns the whole island.  Downloading that inside the engine pegged it so
    hard that even /health stopped answering -- so the automatic download is
    bounded, and the page is told why instead."""
    started = []
    monkeypatch.setattr(
        osm_source, "start_area_fetch",
        lambda area, bbox: started.append((area, list(bbox))) or {"state": "fetching"},
    )
    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: None)
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    # The island: 5.35 x 4.4 degrees.
    ireland = [-11.0134, 51.222, -5.6582, 55.636]

    state = osm_source.ensure_area_data_background("Galway, Ireland", ireland)

    assert not started, "a country-wide download was started by the boundary call"
    assert state["state"] == "not_started"
    assert state["too_large_km2"] > osm_source.AUTO_FETCH_MAX_KM2
    assert "Narrow" in state["reason"]


def test_an_already_downloaded_area_is_not_fetched_again(monkeypatch):
    started = []
    monkeypatch.setattr(osm_source, "start_area_fetch", lambda *a: started.append(a) or {})
    monkeypatch.setattr(
        osm_source, "covered_by_cache",
        lambda bbox: {"detail": "overpass", "fetched_at": osm_source._now(), "counts": {}},
    )
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)

    state = osm_source.ensure_area_data_background("Kreuzberg", BBOX)

    assert not started, "a cached area was downloaded again"
    assert state["source"] == "cache"


def test_the_preview_reports_the_fetch_state_even_on_the_counts_path(monkeypatch):
    _stub_area(monkeypatch, [_premise(1)])
    monkeypatch.setattr(
        osm_source, "area_fetch_state",
        lambda area, bbox: {"state": "ready", "label": "Area data is ready", "source": "cache"},
    )

    out = osm_source.preview_area("Mariendorf, Berlin, Germany")

    assert out["osm_fetch"]["state"] == "ready"


def test_a_progress_poll_never_touches_the_database(monkeypatch):
    """The page polls this every few seconds while a download runs; it must be
    free of writes and work before any download has started."""
    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("area_fetch_state touched the database")

    monkeypatch.setattr(osm_source, "_query", boom)
    monkeypatch.setattr(osm_source, "_execute", boom)

    state = osm_source.area_fetch_state("Nowhere", BBOX)

    assert state["state"] == "unknown"
    assert state["fetching"] is False
    assert state["groups_total"] == len(osm_source._OVERPASS_GROUPS)
