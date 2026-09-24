"""Tier provenance on the published trench layer.

`_ensure_backbone_reach` relabels a Distribution spine to `Feeder` so the MFG →
every PDP path exists on the Feeder tier. That relabel is deliberate and stays;
what these tests pin is that the promotion is now *recorded*, so a consumer can
tell a real trunk from a promoted distribution spine.

Measured on Berlin before this change (`tmp/on_trench_check.py`): against a
trench of its own tier, `distribution_ducts` p90 32.0 m / max 85.2 m (37 of 154
over 10 m) and `distribution_cable` p90 55.8 m / max 90.9 m (159 of 315 over
10 m) — because ~24 % of ducts and ~50 % of cables ride spans whose
`TRENCH_TIER` says Feeder. Nothing is geometrically wrong: every feature is
0.00 m from *a* trench. Only the label was ambiguous.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_tests_qgis.py
"""

from __future__ import annotations

import pytest

from HLDPlanning.algorithms.trench_design_layer import (
    _FINAL_FIELDS,
    TrenchDesignLayerAlgorithm,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

ALGO = TrenchDesignLayerAlgorithm()


def _span(x0, y0, x1, y1):
    """A span row as `_ensure_backbone_reach` sees it."""
    from qgis.core import QgsGeometry, QgsPointXY

    return {
        "_geom": QgsGeometry.fromMultiPolylineXY(
            [[QgsPointXY(x0, y0), QgsPointXY(x1, y1)]]),
        "TRENCH_TIER": "Distribution",
    }


def _feeder(x0, y0, x1, y1, promoted_from=None):
    row = _span(x0, y0, x1, y1)
    row["TRENCH_TIER"] = "Feeder"
    row["PROMOTED_FROM"] = promoted_from
    return row


# --- published contract -----------------------------------------------------

def test_published_schema_carries_provenance():
    """Both fields must reach the layer, or the platform loses the distinction."""
    names = [n for n, _t in _FINAL_FIELDS]

    assert "PROMOTED_FROM" in names
    assert "SERVES_TIER" in names


def test_published_schema_still_builds_valid_sink_fields():
    """The added fields must not make the sink spec unbuildable."""
    from HLDPlanning.algorithms.trench_design_layer import _fields

    fields = _fields(_FINAL_FIELDS)

    assert fields.names() == [n for n, _t in _FINAL_FIELDS]
    assert fields.indexFromName("SERVES_TIER") >= 0


def test_field_names_are_unique_case_insensitively():
    """GPKG treats column names case-insensitively; a clash breaks creation."""
    names = [n.lower() for n, _t in _FINAL_FIELDS]

    assert len(names) == len(set(names))


# --- _stamp_tier_provenance -------------------------------------------------

def test_stamp_derives_serves_tier_from_the_promotion_record():
    rows = [
        {"TRENCH_TIER": "Feeder", "PROMOTED_FROM": "Distribution"},
        {"TRENCH_TIER": "Feeder", "PROMOTED_FROM": None},
        {"TRENCH_TIER": "Garden", "PROMOTED_FROM": ""},
    ]

    promoted = ALGO._stamp_tier_provenance(rows)

    assert promoted == 1
    assert rows[0]["SERVES_TIER"] == "Distribution"
    assert rows[1]["SERVES_TIER"] == "Feeder"
    assert rows[2]["SERVES_TIER"] == "Garden"


def test_stamp_normalises_an_empty_origin_to_null():
    """Empty string means 'not promoted', and must not read as a tier."""
    rows = [{"TRENCH_TIER": "Feeder", "PROMOTED_FROM": "  "}]

    assert ALGO._stamp_tier_provenance(rows) == 0
    assert rows[0]["PROMOTED_FROM"] is None
    assert rows[0]["SERVES_TIER"] == "Feeder"


def test_stamp_counts_every_promoted_span():
    rows = [{"TRENCH_TIER": "Feeder", "PROMOTED_FROM": "Distribution"}
            for _ in range(3)]

    assert ALGO._stamp_tier_provenance(rows) == 3


# --- the promotion itself ---------------------------------------------------

def _promote_scenario():
    """MFG on a short Feeder stub, then a long Distribution spine to the PDP.

    The PDP is 50 m from the Feeder backbone, well past ``_BACKBONE_TOL_M``
    (1.0 m), so ``_promote`` walks the spine back to the backbone and relabels
    it — the real Berlin case.
    """
    rows = [
        _feeder(0, 0, 10, 0),
        _span(10, 0, 60, 0),
    ]
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 60, 0)]
    return rows, anchors


def test_promoted_spine_records_what_it_was():
    rows, anchors = _promote_scenario()

    changed = ALGO._ensure_backbone_reach(rows, anchors, None)
    ALGO._stamp_tier_provenance(rows)

    assert changed == 1
    # The relabel still happens — the feeder must reach the PDP.
    assert rows[1]["TRENCH_TIER"] == "Feeder"
    # ...and is now recorded, so the span is still attributable to its tier.
    assert rows[1]["PROMOTED_FROM"] == "Distribution"
    assert rows[1]["SERVES_TIER"] == "Distribution"


def test_genuine_trunk_keeps_no_provenance():
    rows, anchors = _promote_scenario()

    ALGO._ensure_backbone_reach(rows, anchors, None)
    ALGO._stamp_tier_provenance(rows)

    assert rows[0]["PROMOTED_FROM"] is None
    assert rows[0]["SERVES_TIER"] == "Feeder"


def test_garden_drop_is_never_promoted():
    """A Garden leg is a premise drop, not a route — the guard must hold."""
    rows = [
        _feeder(0, 0, 10, 0),
        {"_geom": None, "TRENCH_TIER": "Garden", "PROMOTED_FROM": None},
    ]
    from qgis.core import QgsGeometry, QgsPointXY

    rows[1]["_geom"] = QgsGeometry.fromMultiPolylineXY(
        [[QgsPointXY(10, 0), QgsPointXY(60, 0)]])
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 60, 0)]

    ALGO._ensure_backbone_reach(rows, anchors, None)

    assert rows[1]["TRENCH_TIER"] == "Garden"
    assert rows[1]["PROMOTED_FROM"] is None


def test_feeder_span_absorbed_into_the_backbone_is_not_called_promoted():
    """A span that already reads Feeder never changed tier, so it has no history.

    Real Berlin run `39ec0d86`: 27 spans were absorbed into the backbone set but
    only 16 were genuine relabels — recording the other 11 as promoted invented
    a history that never happened.
    """
    rows = [
        _feeder(0, 0, 10, 0),
        _feeder(10, 0, 30, 0),   # already Feeder, but not on the backbone path
        _span(30, 0, 60, 0),     # the Distribution spine that really promotes
    ]
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 60, 0)]

    ALGO._ensure_backbone_reach(rows, anchors, None)
    promoted = ALGO._stamp_tier_provenance(rows)

    assert rows[1]["PROMOTED_FROM"] is None
    assert rows[1]["SERVES_TIER"] == "Feeder"
    assert rows[2]["PROMOTED_FROM"] == "Distribution"
    assert promoted == 1


def test_span_already_reaching_the_backbone_is_left_alone():
    """Nothing to promote when the PDP already sits on a Feeder span."""
    rows = [
        _feeder(0, 0, 10, 0),
        _feeder(10, 0, 60, 0),
    ]
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 60, 0)]

    changed = ALGO._ensure_backbone_reach(rows, anchors, None)

    assert changed == 0
    assert rows[1]["PROMOTED_FROM"] is None


# --- the detached-island spur ----------------------------------------------

def test_spur_does_not_inherit_the_provenance_it_copies():
    """`_spur` copies `rows[tgt]`, so it must drop an inherited origin.

    The spur is new Feeder construction: claiming it was promoted from
    Distribution would be a fabricated provenance record.
    """
    rows = [
        _feeder(0, 0, 10, 0, promoted_from="Distribution"),
        _feeder(20, 0, 30, 0),
    ]
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 30, 0)]

    ALGO._ensure_backbone_reach(rows, anchors, None)

    assert len(rows) == 3, "the spur should have been appended"
    spur = rows[-1]
    assert spur["TRENCH_TIER"] == "Feeder"
    assert spur["PROMOTED_FROM"] is None
    assert spur["SERVES_TIER"] == "Feeder"


def test_island_beyond_the_bridge_limit_is_reported_not_invented():
    """Past `_FEEDER_BRIDGE_MAX_M` (30 m) the pass must not dig a spur."""
    rows = [
        _feeder(0, 0, 10, 0),
        _feeder(100, 0, 110, 0),
    ]
    anchors = [("MFG", "MFG00001", 0, 0), ("PDP", "PDP00002", 110, 0)]

    changed = ALGO._ensure_backbone_reach(rows, anchors, None)

    assert changed == 0
    assert len(rows) == 2
