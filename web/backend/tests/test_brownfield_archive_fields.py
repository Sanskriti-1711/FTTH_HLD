"""The brownfield archive must survive a GeoPackage: no field name twins.

``tools/make_brownfield_layers.py`` draws the handover archive from a finished
design and stamps the capacity pair the loader AUTO-DETECTS
(``capacity_total`` / ``capacity_used``). The design's own rows already carry
the UPPER-case pair (the chamber stage writes ``CAPACITY_TOTAL`` /
``CAPACITY_USED``), so an archive could ship both spellings in one feature —
legal in GeoJSON, refused by GeoPackage, because a GeoPackage field name is
case-insensitive:

    Cannot create field capacity_total. A field with the same name already
    exists.

The trench stage materialises BF_CHAMBERS / BF_POLES to a GeoPackage (the
designer reads files, not QGIS layers), so that collision did not degrade the
run — it killed it, in the middle of the pipeline, on the very archive the
planner had just been handed. The archive is now built so the twins can never
coexist, and this pins that:

* the stamped pair wins over whatever the source row spelled;
* no stamped row carries two names that differ only by case.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_brownfield_archive_fields.py -v
"""

import sys
from pathlib import Path

import pytest

# tools/ is a sibling of web/: the archive builder is part of the engine, not
# of the FastAPI backend, but it imports the backend's `occupancy`.
_HLD_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_HLD_ROOT / "tools"))

import make_brownfield_layers as mbf  # noqa: E402


def _case_collisions(keys):
    """Names that a GeoPackage would treat as the same field."""
    lowered = [str(k).lower() for k in keys]
    return sorted({k for k in keys if lowered.count(str(k).lower()) > 1})


def test_the_designs_upper_case_capacity_pair_does_not_survive_stamping():
    """The chamber/pole rows spell it ``CAPACITY_TOTAL``; the archive must not
    ship that next to the ``capacity_total`` the loader keys on."""
    props = {
        "STRUCT_ID": "CH-001",
        "CAPACITY_TOTAL": 4,
        "CAPACITY_USED": 3,
    }

    out = mbf._stamp(props, "chamber", src_id="CH-001", capacity_total=4,
                     capacity_used=3)

    assert "CAPACITY_TOTAL" not in out and "CAPACITY_USED" not in out
    assert out["capacity_total"] == 4
    assert out["capacity_used"] == 3
    assert _case_collisions(out) == []


def test_the_stamped_capacity_wins_over_the_rows_own_spelling():
    """The value the builder derived is the one that ships — not the twin it
    replaced (a chamber with an unrecorded capacity comes in as 1-way)."""
    props = {"CAPACITY_TOTAL": 0, "CAPACITY_USED": 0, "ASSET_TYPE": "old"}

    out = mbf._stamp(props, "chamber", capacity_total=1, capacity_used=0)

    assert (out["capacity_total"], out["capacity_used"]) == (1, 0)


@pytest.mark.parametrize("kind", ["trench", "duct", "fibre", "chamber",
                                  "pole", "pdp", "mfg"])
def test_every_asset_kind_stamps_a_collision_free_row(kind):
    """Whatever the source row already carried — including every stamped name
    in the other case — the row that ships has one spelling per field."""
    props = {name.upper(): "x" for name in
             ("STAGE", "ASSET_TYPE", "SRC_ID", "INFRA_STATUS",
              "VERIFY_STATUS", "CAPACITY_TOTAL", "CAPACITY_USED", "USE_MODE")}
    props.update({name: "y" for name in
                  ("STAGE", "ASSET_TYPE", "SRC_ID", "INFRA_STATUS",
                   "VERIFY_STATUS", "capacity_total", "capacity_used",
                   "USE_MODE")})
    props["SRC_ID"] = "BF_POLE_00001"

    out = mbf._stamp(props, kind, src_id="BF_POLE_00002",
                     capacity_total=2, capacity_used=1)

    assert _case_collisions(out) == []
    assert out["STAGE"] == "brownfield"
    assert out["ASSET_TYPE"] == kind
    # The row's own SRC_ID is kept; only the fallback comes from `src_id`.
    assert out["SRC_ID"] == "BF_POLE_00001"
    assert out["USE_MODE"] == "brownfield"
