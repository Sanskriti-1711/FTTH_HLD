"""The served object layer must carry a building's household count.

The design writes ONE ROW PER PREMISE (a physical service location), which is
what the network needs, but a block that became several premises then reads as
several one-household buildings.  `add_household_aggregates` collapses the
per-location spread into the BUILDING total and puts it, the service-location
count and the (possibly mixed) estimating method on every row of that building
-- so the layer states the count the block really stands for, and the demand
stages can count each building exactly once.
"""

from __future__ import annotations

import pathlib
import sys

import pandas as pd

# HLDPlanning/ lives three levels above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from HLDPlanning.utils.sheet_utils import (  # noqa: E402
    HOUSEHOLD_AGGREGATE_COLUMNS,
    add_household_aggregates,
    household_method_label,
    households_by_object,
)
import osm_source  # noqa: E402

OBJECT_LAYER_SRC = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "object_layer.py"
                    ).read_text(encoding="utf-8")


def _layer(rows):
    return pd.DataFrame(rows)


def test_a_blocks_premises_all_carry_the_building_total():
    df = _layer([
        {"OSM_ID": 1, "HH": 3, "HH_METHOD": "building_flats"},
        {"OSM_ID": 1, "HH": 1, "HH_METHOD": "fallback_one"},
    ])
    add_household_aggregates(df)
    # Both rows of the block read 4 households across 2 service locations -- not
    # 3 and 1 -- and the per-location spread column is gone.
    assert df["households"].tolist() == [4, 4]
    assert df["premises"].tolist() == [2, 2]
    assert "HH" not in df.columns
    assert "HH_METHOD" not in df.columns


def test_a_single_premise_building_is_its_own_aggregate():
    df = _layer([{"OSM_ID": 2, "HH": 5, "HH_METHOD": "building_levels"}])
    add_household_aggregates(df)
    assert df.loc[0, "households"] == 5
    assert df.loc[0, "premises"] == 1


def test_the_aggregates_are_read_when_they_are_already_named():
    # object_layer hands the helper a frame that already carries `households`
    # (ensure_households_column renamed it); the helper must not then treat the
    # building total as a per-location count and square it.
    df = _layer([
        {"OSM_ID": 1, "households": 4, "household_method": "fallback_one"},
        {"OSM_ID": 1, "households": 4, "household_method": "fallback_one"},
    ])
    add_household_aggregates(df)
    assert df["households"].tolist() == [8, 8]  # 4 + 4 per location, counted once
    assert df["premises"].tolist() == [2, 2]


def test_a_mixed_building_says_mixed_rather_than_picking_a_method():
    df = _layer([
        {"OSM_ID": 1, "HH": 12, "HH_METHOD": "building_flats"},
        {"OSM_ID": 1, "HH": 1, "HH_METHOD": "fallback_one"},
    ])
    add_household_aggregates(df)
    # Sorted and joined, so the label is stable whatever the row order was.
    assert set(df["household_method"]) == {"mixed(building_flats,fallback_one)"}


def test_a_row_with_no_building_identity_is_its_own_object():
    df = _layer([
        {"OSM_ID": None, "HH": 5, "HH_METHOD": "levels_x_footprint"},
        {"OSM_ID": None, "HH": 1, "HH_METHOD": "fallback_one"},
    ])
    add_household_aggregates(df)
    # A missing building id must NOT pool two unrelated rows into one block.
    assert df["households"].tolist() == [5, 1]
    assert df["premises"].tolist() == [1, 1]


def test_the_schema_appears_even_without_a_building_column():
    df = _layer([{"HH": 2}, {"HH": 7}])
    add_household_aggregates(df)
    for col in HOUSEHOLD_AGGREGATE_COLUMNS:
        assert col in df.columns
    assert df["households"].tolist() == [2, 7]
    assert df["premises"].tolist() == [1, 1]


def test_an_empty_frame_still_gets_the_columns():
    df = _layer([])
    add_household_aggregates(df)
    for col in HOUSEHOLD_AGGREGATE_COLUMNS:
        assert col in df.columns
    assert len(df) == 0


def test_the_object_layer_writes_the_aggregates():
    # The wiring lives in the QGIS algorithm, which cannot be imported here.
    assert "add_household_aggregates(df, hh_col=\"households\")" in OBJECT_LAYER_SRC
    # A thin export must keep them too, or the aggregates disappear from the
    # served layer the moment someone turns that profile on.
    assert "thin_keep += list(HOUSEHOLD_AGGREGATE_COLUMNS)" in OBJECT_LAYER_SRC
    # The old per-location name must not survive anywhere on the output.
    assert '"HH",\n' not in OBJECT_LAYER_SRC


def test_each_building_is_counted_once_for_demand():
    # The splitter plan and the polygon clubbing read this, so a building's total
    # must land once however many address rows repeat it.
    rows = [
        {"OSM_ID": 1, "households": 5, "ADDR_ID": "a"},
        {"OSM_ID": 1, "households": 5, "ADDR_ID": "b"},
        {"OSM_ID": 1, "households": 5, "ADDR_ID": "c"},
        {"OSM_ID": 2, "households": 2, "ADDR_ID": "d"},
    ]
    homes = households_by_object(rows)
    assert sum(homes.values()) == 7  # not 17


def test_an_anonymous_row_still_counts_once():
    rows = [{"households": 4}, {"households": 4}]
    assert sum(households_by_object(rows).values()) == 8


def test_the_method_label_matches_the_review_layer():
    # The pre-run review layer words a mixed building the same way; if either
    # helper changes, one layer would disagree with the other on the map.
    for methods in ([], ["fallback_one"], ["a", "b"], ["b", "a", "b"],
                    ["", "fallback_one"], ["a", "b", "c"]):
        assert household_method_label(methods) == osm_source.household_method_label(methods)
