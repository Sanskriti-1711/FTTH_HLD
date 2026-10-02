"""The served object layer must carry a building's household count.

The design writes ONE ROW PER PREMISE, which is what the network needs, but a
block that became several premises then reads as several one-household
buildings.  `add_household_aggregates` puts the building total, the premise
count and the (possibly mixed) estimating method on every row of the building,
so the served Objects layer can be read the same way as the pre-run review
layer.  These tests pin that wording so the two layers cannot drift apart.
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
    # Both rows of the block read 4 households across 2 premises -- not 3 and 1.
    assert df["households"].tolist() == [4, 4]
    assert df["premises"].tolist() == [2, 2]


def test_a_single_premise_building_is_its_own_aggregate():
    df = _layer([{"OSM_ID": 2, "HH": 5, "HH_METHOD": "building_levels"}])
    add_household_aggregates(df)
    assert df.loc[0, "households"] == 5
    assert df.loc[0, "premises"] == 1


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
    assert "add_household_aggregates(df)" in OBJECT_LAYER_SRC
    # A thin export must keep them too, or the aggregates disappear from the
    # served layer the moment someone turns that profile on.
    assert "thin_keep += list(HOUSEHOLD_AGGREGATE_COLUMNS)" in OBJECT_LAYER_SRC


def test_the_method_label_matches_the_review_layer():
    # The pre-run review layer words a mixed building the same way; if either
    # helper changes, one layer would disagree with the other on the map.
    for methods in ([], ["fallback_one"], ["a", "b"], ["b", "a", "b"],
                    ["", "fallback_one"], ["a", "b", "c"]):
        assert household_method_label(methods) == osm_source.household_method_label(methods)
