"""Tests for the external household register (UK).

The register exists to replace the OSM household heuristic with a published
count, so what is tested here is not "does it load" but "when a register count
and the heuristic disagree, which one wins, and does the row say which".

Pure throughout: no database and no network. The CSV reader is fed a StringIO,
`apply_register` is fed dicts, and the integration with `assemble_premises` is
exercised through that function's existing pure signature.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import household_register as hr  # noqa: E402
import osm_source  # noqa: E402


# ---------------------------------------------------------------------------
# Postcode normalisation -- a join that misses on whitespace returns nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("B11 3SA", "B11 3SA"),
    ("b11 3sa", "B11 3SA"),
    ("B113SA", "B11 3SA"),
    ("B11-3SA", "B11 3SA"),
    ("  B11   3SA  ", "B11 3SA"),
    ("SW1A1AA", "SW1A 1AA"),
])
def test_postcode_spellings_normalise_to_one_key(raw, expected):
    # The register side, the OSM `addr:postcode` side and a planner's typing all
    # spell a postcode differently. If these do not collapse to one key, a
    # loaded register silently matches nothing -- which reads exactly like "the
    # UK has no household data".
    assert hr.normalize_postcode(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "   ",
    "B11",            # outward code only, no inward code
    "B11 3S",         # inward code too short
    "B11 3SAA",       # inward code too long
    "12345678",
    "1234 3SA",       # outward code with no letter
    "rubbish",        # seven letters: a length rule alone would accept this
    "hello there",
])
def test_unusable_postcodes_normalise_to_empty(raw):
    # "rubbish" is the case that makes this a real test rather than a formality:
    # it is exactly seven characters, so a purely positional split would return
    # " RUB BIS" -- a plausible-looking key that matches nothing, which is
    # indistinguishable from a register with no data for the area.
    assert hr.normalize_postcode(raw) == ""


def test_postcode_outward_is_the_code_before_the_inward_part():
    assert hr.postcode_outward("b11 3sa") == "B11"
    assert hr.postcode_outward("sw1a 1aa") == "SW1A"
    assert hr.postcode_outward("rubbish") == ""


# ---------------------------------------------------------------------------
# Counts: a blank is "no answer", never zero
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("120", 120),
    ("1,204", 1204),
    (" 42 ", 42),
    ("12.0", 12),
    ("", None),
    ("-", None),
    (None, None),
    ("n/a", None),
    ("no data", None),
])
def test_parse_count_reads_a_number_or_says_nothing(raw, expected):
    # A register row with no usable count must fall through to the heuristic.
    # Writing 0 over it would delete every household on a real street, which is
    # the one failure this module exists to prevent.
    assert hr.parse_count(raw) == expected


# ---------------------------------------------------------------------------
# Reading a register file
# ---------------------------------------------------------------------------

def test_onspd_columns_are_accepted_as_they_ship():
    # The ONSPD's own header, not a renamed copy: an operator points --file at a
    # downloaded release and it has to work.
    text = "pcd,dwellings,pop01\nB11 3SA,120,231\nSW1A 1AA,50,90\n"
    rows = list(hr.read_register_csv(io.StringIO(text)))
    assert rows == [
        {"postcode": "B11 3SA", "households": 120, "population": 231},
        {"postcode": "SW1A 1AA", "households": 50, "population": 90},
    ]


def test_an_operator_csv_with_its_own_column_names_is_also_accepted():
    text = "postcode,households\nb11 3sa,120\n"
    rows = list(hr.read_register_csv(io.StringIO(text)))
    assert rows == [{"postcode": "B11 3SA", "households": 120}]


def test_a_uprn_column_is_carried_through_when_present():
    text = "uprn,postcode,dwellings\n100012345,B11 3SA,8\n"
    rows = list(hr.read_register_csv(io.StringIO(text)))
    assert rows == [{"postcode": "B11 3SA", "households": 8, "uprn": "100012345"}]


def test_register_rows_with_no_usable_count_are_skipped_not_zeroed():
    # ONSPD writes 0 or a blank for a postcode with no counted dwellings. Those
    # must not become "0 households here", which would strip every premise in
    # the postcode down to the floor.
    text = "pcd,dwellings\nB11 3SA,120\nB12 0AA,0\nB13 9QQ,\n"
    rows = list(hr.read_register_csv(io.StringIO(text)))
    assert [r["postcode"] for r in rows] == ["B11 3SA"]


def test_register_rows_with_an_unusable_postcode_are_skipped():
    text = "pcd,dwellings\nB11,120\n,50\nB11 3SA,60\n"
    rows = list(hr.read_register_csv(io.StringIO(text)))
    assert [r["postcode"] for r in rows] == ["B11 3SA"]


def test_a_csv_with_no_count_column_is_refused_rather_than_loading_nothing():
    # Silently loading zero rows is the failure this guards: the operator would
    # conclude the UK has no household data rather than that the file is wrong.
    text = "pcd,name\nB11 3SA,Sparkbrook\n"
    with pytest.raises(ValueError, match="dwellings"):
        list(hr.read_register_csv(io.StringIO(text)))


def test_pcd_is_preferred_over_pcds_when_both_are_present():
    # The ONSPD carries both: `pcd` is the 7-character form and `pcds` the
    # 8-character one. Both normalise to the same key, so either works, but the
    # 7-character form is the one the directory is keyed on and must win the
    # column match rather than being lost to prefix matching.
    assert hr._match_column(["pcd", "pcds"], "postcode") == "pcd"
    assert hr._match_column(["pcds"], "postcode") == "pcds"


# ---------------------------------------------------------------------------
# Apportionment: the register total is reproduced exactly
# ---------------------------------------------------------------------------

def test_the_parts_sum_to_the_register_total_exactly():
    # A design that reports 512 households from a 500-household register is wrong
    # in the one number the register was loaded to fix.
    for total, weights in [(10, [1, 1, 1]), (100, [1, 1, 1, 1, 1]),
                           (37, [5, 3, 1, 1]), (7, [12, 1])]:
        parts = hr._weighted_split(total, weights)
        assert sum(parts) == total
        assert all(p >= 1 for p in parts)


def test_apportionment_keeps_the_shape_the_estimate_found():
    # A 12-storey block and a bungalow must not each get the same share of a
    # postcode's households -- that is the whole reason the estimate is used as
    # the WEIGHT rather than discarded.
    parts = hr._weighted_split(20, [12, 1])
    assert parts[0] > parts[1]
    assert sum(parts) == 20


def test_a_total_smaller_than_the_premise_count_floors_at_one_each():
    # It cannot be honoured without zeroing a premise, so every premise keeps at
    # least one household and the caller reports the shortfall.
    parts = hr._weighted_split(2, [1, 1, 1, 1, 1])
    assert parts == [1, 1, 1, 1, 1]


def test_apportionment_is_deterministic_for_the_same_input():
    # It has to be reproducible: the same area previewed twice must produce the
    # same household counts, or a re-run looks like a different design.
    assert hr._weighted_split(10, [3, 1, 1]) == hr._weighted_split(10, [3, 1, 1])


def test_apportionment_gives_equal_weights_equal_shares():
    assert hr._weighted_split(10, [1, 1, 1]) == [4, 3, 3]


# ---------------------------------------------------------------------------
# What wins
# ---------------------------------------------------------------------------

def _premises():
    return [
        {"Postcode": "B11 3SA", "HH": 1, "HH_METHOD": "fallback_one"},
        {"Postcode": "B11 3SA", "HH": 1, "HH_METHOD": "fallback_one"},
        {"Postcode": "B11 3SA", "HH": 1, "HH_METHOD": "fallback_one"},
    ]


def test_a_register_count_replaces_the_heuristic_and_the_row_says_so():
    rows, stats = hr.apply_register(_premises(), {"B11 3SA": 30}, {})
    assert sum(r["HH"] for r in rows) == 30
    assert {r["HH_METHOD"] for r in rows} == {"register_postcode"}
    assert stats["premises_registered"] == 3
    assert stats["register_households"] == 30


def test_the_heuristic_count_is_reported_before_and_after():
    # A planner has to be able to see how far the number moved, or a BOQ that
    # grew 4x has no explanation attached to it.
    _, stats = hr.apply_register(_premises(), {"B11 3SA": 30}, {})
    assert stats["heuristic_households_before"] == 3
    assert stats["heuristic_households_after"] == 30


def test_a_postcode_the_register_does_not_cover_keeps_the_heuristic():
    rows, _ = hr.apply_register(_premises(), {"SW1A 1AA": 30}, {})
    assert all(r["HH_METHOD"] == "fallback_one" for r in rows)
    assert sum(r["HH"] for r in rows) == 3


def test_the_input_rows_are_not_mutated():
    original = _premises()
    hr.apply_register(original, {"B11 3SA": 30}, {})
    # A caller may want the heuristic for comparison -- and the preview's
    # before/after numbers depend on it not being overwritten underneath them.
    assert all(p["HH"] == 1 for p in original)


def test_an_empty_register_leaves_the_heuristic_completely_alone():
    rows, stats = hr.apply_register(_premises(), {}, {})
    assert rows == _premises()
    assert stats["enabled"] is False


def test_a_register_with_fewer_households_than_premises_is_reported_not_hidden():
    # The register and the premise set genuinely disagree here. Each premise is
    # floored at 1 and the gap is reported, rather than the total being silently
    # inflated to match the premise count.
    rows, stats = hr.apply_register(_premises(), {"B11 3SA": 2}, {})
    assert stats["below_premise_count"] == 1
    assert stats["shortfall"] == 1
    assert all(r["HH"] == 1 for r in rows)


def test_a_upprn_wins_over_the_postcode_and_leaves_the_postcode_pool():
    rows, stats = hr.apply_register(
        [{"Postcode": "B11 3SA", "UPRN": "100012345", "HH": 1, "HH_METHOD": "fallback_one"},
         {"Postcode": "B11 3SA", "HH": 1, "HH_METHOD": "fallback_one"}],
        {"B11 3SA": 10},
        {"100012345": 4},
    )
    # The UPRN row is per-premise and exact; the other takes the remainder.
    assert rows[0]["HH"] == 4
    assert rows[0]["HH_METHOD"] == "register_uprn"
    assert rows[1]["HH"] == 6
    assert rows[1]["HH_METHOD"] == "register_postcode"
    assert sum(r["HH"] for r in rows) == 10


def test_postcodes_are_matched_after_normalisation():
    # OSM spells it one way, the register another. A miss here is silent.
    rows, _ = hr.apply_register(
        [{"Postcode": "b113sa", "HH": 1, "HH_METHOD": "fallback_one"}],
        {"B11 3SA": 42}, {},
    )
    assert rows[0]["HH"] == 42
    assert rows[0]["HH_METHOD"] == "register_postcode"


# ---------------------------------------------------------------------------
# Integration with the premise builder
# ---------------------------------------------------------------------------

def _building(osm_id, **over):
    row = {
        "osm_id": osm_id, "building": "house", "lon": 13.38, "lat": 52.44,
        "addr_street": "High Street", "addr_housenumber": str(osm_id),
        "addr_postcode": "B11 3SA", "addr_city": "Birmingham",
        "building_levels": None, "building_flats": None, "footprint_m2": 120.0,
        "tags": {},
    }
    row.update(over)
    return row


def _address(osm_id, number, **over):
    row = {
        "osm_id": osm_id, "addr_street": "High Street", "addr_housenumber": number,
        "addr_postcode": "B11 3SA", "addr_city": "Birmingham",
        "addr_flats": None, "lon": 13.381, "lat": 52.441, "tags": {},
    }
    row.update(over)
    return row


def test_the_register_reaches_the_premises_the_pipeline_actually_writes():
    buildings = [_building(1), _building(2)]
    addresses = [_address(11, "1"), _address(12, "2")]
    join = {11: 1, 12: 2}
    without, _ = osm_source.assemble_premises(buildings, addresses, join, country="United Kingdom")
    with_reg, stats = osm_source.assemble_premises(
        buildings, addresses, join, country="United Kingdom",
        register={"by_postcode": {"B11 3SA": 80}, "by_uprn": {}},
    )
    assert sum(p["HH"] for p in without) == 2
    assert sum(p["HH"] for p in with_reg) == 80
    assert all(p["HH_METHOD"] == "register_postcode" for p in with_reg)
    assert stats["household_register"]["premises_registered"] == 2


def test_no_register_means_byte_identical_premises_to_before():
    # The feature is off by default, so with nothing loaded the output must be
    # exactly what it was -- otherwise turning it off would not be a real option.
    buildings = [_building(1), _building(2)]
    addresses = [_address(11, "1"), _address(12, "2")]
    join = {11: 1, 12: 2}
    plain, plain_stats = osm_source.assemble_premises(buildings, addresses, join)
    none_passed, none_stats = osm_source.assemble_premises(
        buildings, addresses, join, register=None)
    assert plain == none_passed
    assert plain_stats == none_stats
    assert "household_register" not in plain_stats


def test_the_register_is_applied_before_the_per_household_expansion():
    # A 12-flat block and a bungalow in one postcode must not each get the same
    # share of the register's total. If the register were applied after the
    # expansion every row would already be HH=1 and the split would be uniform.
    tall = _building(1, building="apartments", footprint_m2=1000.0,
                     building_levels="12")
    bungalow = _building(2, building="house", footprint_m2=80.0)
    addresses = [_address(11, "1"), _address(12, "2")]
    join = {11: 1, 12: 2}
    rows, _ = osm_source.assemble_premises(
        [tall, bungalow], addresses, join, country="United Kingdom",
        register={"by_postcode": {"B11 3SA": 50}, "by_uprn": {}},
    )
    tall_hh = sum(p["HH"] for p in rows if p["OSM_ID"] == 1)
    bungalow_hh = sum(p["HH"] for p in rows if p["OSM_ID"] == 2)
    assert tall_hh > bungalow_hh
    assert tall_hh + bungalow_hh == 50


def test_a_uprn_tag_on_the_building_is_used_when_the_register_has_one():
    building = _building(1, tags={"ref:uprn": "100012345"})
    addresses = [_address(11, "1")]
    rows, _ = osm_source.assemble_premises(
        [building], addresses, {11: 1}, country="United Kingdom",
        register={"by_postcode": {"B11 3SA": 40}, "by_uprn": {"100012345": 9}},
    )
    assert sum(p["HH"] for p in rows) == 9
    assert rows[0]["HH_METHOD"] == "register_uprn"


def test_the_register_is_ignored_for_a_country_it_does_not_cover():
    # The matching is UK-shaped on purpose: a postcode that happens to look right
    # in another country is not the same thing, so it is not guessed at.
    assert osm_source.household_register_for("Germany", ["B11 3SA"]) is None


def test_the_register_is_ignored_while_the_knob_is_off(monkeypatch):
    monkeypatch.setattr(hr, "REGISTER_ENABLED", False)
    assert osm_source.household_register_for("GB", ["B11 3SA"]) is None


# ---------------------------------------------------------------------------
# Provenance: the number and the licence behind it travel together
# ---------------------------------------------------------------------------

def test_a_register_count_counts_as_measured_not_estimated():
    # This is the payoff: `estimated_share` is what the preview reports as the
    # share of the design resting on a guess, and a register count is not a guess.
    rows, _ = hr.apply_register(_premises(), {"B11 3SA": 30}, {})
    summary = osm_source.household_summary(rows)
    assert summary["estimated_share"] == 0.0
    assert summary["by_method"] == {"register_postcode": 3}


def test_a_partly_covered_area_reports_the_share_that_is_still_estimated():
    rows = _premises() + [
        {"Postcode": "SW1A 1AA", "HH": 1, "HH_METHOD": "fallback_one"},
    ]
    rows, _ = hr.apply_register(rows, {"B11 3SA": 30}, {})
    summary = osm_source.household_summary(rows)
    assert summary["total"] == 31
    assert summary["estimated_share"] == round(1 / 31, 3)


def test_the_preview_warning_says_where_the_register_did_and_did_not_apply():
    # A total that quietly moved from 3 to 80 has to say so, and has to say what
    # the number is: a dwellings count is not a survey of occupied homes.
    warning = osm_source.household_register_warning({
        "enabled": True, "premises_registered": 3, "postcodes_matched": 1,
        "postcodes_seen": 2, "heuristic_households_before": 3,
        "heuristic_households_after": 80, "source": "ONS Postcode Directory (ONSPD)",
    })
    assert "external household register" in warning
    assert "ONS Postcode Directory" in warning
    assert "from 3 to 80" in warning
    assert "1 postcode(s) in this area are not in the register" in warning
    assert "DWELLINGS" in warning


def test_the_preview_warning_names_a_register_that_disagrees_with_the_premises():
    warning = osm_source.household_register_warning({
        "enabled": True, "premises_registered": 2, "postcodes_matched": 1,
        "postcodes_seen": 1, "heuristic_households_before": 2,
        "heuristic_households_after": 2, "below_premise_count": 1, "shortfall": 3,
    })
    assert "FEWER households" in warning
    assert "disagree" in warning


def test_no_warning_when_no_register_was_used():
    # A warning block that fires on every area would be the fastest way to make
    # the real one unreadable.
    assert osm_source.household_register_warning(None) is None
    assert osm_source.household_register_warning({"enabled": False}) is None


def test_the_payload_is_absent_when_no_register_ran():
    assert osm_source._register_payload(None, None) is None
    assert osm_source._register_payload({"enabled": False}, None) is None


def test_the_payload_carries_the_licence_and_the_caveat():
    payload = osm_source._register_payload(
        {"enabled": True, "premises_registered": 3, "postcodes_matched": 1,
         "postcodes_seen": 1, "heuristic_households_before": 3,
         "heuristic_households_after": 80,
         "source": "ONS Postcode Directory (ONSPD)",
         "licence": "Open Government Licence v3.0"},
        None,
    )
    assert payload["source"] == "ONS Postcode Directory (ONSPD)"
    assert payload["licence"] == "Open Government Licence v3.0"
    assert "DWELLINGS" in payload["caveat"]
    assert payload["households_before"] == 3
    assert payload["households_after"] == 80


def test_both_named_sources_state_what_their_number_is():
    # A register whose number nobody can interpret is not provenance. Both
    # sources carry the caveat that reaches the planner, not just a name.
    for source in hr.REGISTER_SOURCES.values():
        assert source.get("licence")
        assert source.get("publisher")
        assert len(source.get("note") or "") > 40
