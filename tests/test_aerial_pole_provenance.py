"""Pole provenance: did this aerial drop hang off plant that exists?

``Aerial_Spans`` has always carried ``POLE_ID`` / ``FROM_POLE``, and that was
not enough to answer the question a reviewer asks first: is this drop lashed to
a pole that is **already in the ground**, or to one the run still has to build?
The planner knew — it searches the poles this run designs and the existing /
brownfield inventory in one list and takes the nearest eligible one — but it
threw that away.

``POLE_SOURCE`` now names it on both outputs:

    ``existing``  an eligible pole from the existing/brownfield inventory
    ``planned``   an eligible pole this run designs
    ``building``  a house-to-house span ('chain' leg) — no pole at all
    ``""``        nothing could be resolved

These tests run the real algorithm through the registry (``processing.run`` is
a plugin placeholder the harness never loads — see
``test_trench_design_poles_dump.py`` for the same shim) on synthetic layers, so
the anchor choice and the published field are pinned together.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_aerial_pole_provenance.py
"""

from __future__ import annotations

import pytest

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms import aerial_drop_planner as adp

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS = "EPSG:25833"
ALGO = adp.AerialDropLayerAlgorithm()

# A street corner in Berlin, in metres — any projected origin does.
X0, Y0 = 397000.0, 5822000.0


def _processing_run(params):
    """Run the algorithm itself and hand back its sink layers.

    The algorithm is instantiated directly rather than looked up in the
    processing registry: the plugin *provider* is not loaded in this harness,
    and the stage under test does not depend on how it was looked up. Its
    params, its gates and its published fields are the same either way.
    """
    context = QgsProcessingContext()
    results, _ok = ALGO.run(params, context, QgsProcessingFeedback())
    out = {}
    for key, value in (results or {}).items():
        layer = context.takeResultLayer(value) if isinstance(value, str) else value
        out[key] = layer
    return out


def _layer(geom_type, name, fields):
    layer = QgsVectorLayer(f"{geom_type}?crs={CRS}", name, "memory")
    layer.dataProvider().addAttributes(
        [QgsField(field, kind) for field, kind in fields])
    layer.updateFields()
    return layer


_STRING = QMetaType.Type.QString
_DOUBLE = QMetaType.Type.Double
_BOOL = QMetaType.Type.Bool


def _add(layer, geom, **attrs):
    feat = QgsFeature(layer.fields())
    feat.setGeometry(geom)
    for key, value in attrs.items():
        feat[key] = value
    layer.dataProvider().addFeature(feat)
    return feat


def _pt(x, y):
    return QgsGeometry.fromPointXY(QgsPointXY(X0 + x, Y0 + y))


def _line(coords):
    return QgsGeometry.fromPolylineXY(
        [QgsPointXY(X0 + x, Y0 + y) for x, y in coords])


def _premises(rows):
    """rows: (addr, x, y) — every premise flagged aerial_required."""
    layer = _layer("Point", "premises", [
        ("ADDR_ID", _STRING),
        ("aerial_required", _BOOL),
    ])
    features = []
    for addr, x, y in rows:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(_pt(x, y))
        feat["ADDR_ID"] = addr
        feat["aerial_required"] = True
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def _legs(rows):
    """rows: (addr, coords) — the legs the trench stage classified aerial."""
    layer = _layer("LineString", "aerial_legs", [("ADDR_ID", _STRING)])
    features = []
    for addr, coords in rows:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(_line(coords))
        feat["ADDR_ID"] = addr
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def _poles(rows, name="poles"):
    """rows: (pole_id, x, y, height, used, total) — eligible by default."""
    layer = _layer("Point", name, [
        ("POLE_ID", _STRING), ("HEIGHT_M", _DOUBLE),
        ("CAPACITY_TOTAL", _DOUBLE), ("CAPACITY_USED", _DOUBLE),
    ])
    features = []
    for pole_id, x, y, height, used, total in rows:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(_pt(x, y))
        feat["POLE_ID"] = pole_id
        feat["HEIGHT_M"] = height
        feat["CAPACITY_USED"] = used
        feat["CAPACITY_TOTAL"] = total
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def _run(premises, poles, bf_poles=None, legs=None):
    results = _processing_run({
        adp.AerialDropLayerAlgorithm.P_PREMISES: premises,
        adp.AerialDropLayerAlgorithm.P_POLES: poles,
        adp.AerialDropLayerAlgorithm.P_BF_POLES: bf_poles,
        adp.AerialDropLayerAlgorithm.P_LEGS: legs,
        adp.AerialDropLayerAlgorithm.P_ROADS: None,
        adp.AerialDropLayerAlgorithm.P_AERIAL_ZONES: None,
        adp.AerialDropLayerAlgorithm.P_SPACING: 50.0,
        adp.AerialDropLayerAlgorithm.OUT_AERIAL_TRENCH: "memory:",
        adp.AerialDropLayerAlgorithm.OUT_AERIAL_CABLE: "memory:",
    })
    return (results.get(adp.AerialDropLayerAlgorithm.OUT_AERIAL_TRENCH),
            results.get(adp.AerialDropLayerAlgorithm.OUT_AERIAL_CABLE))


def _one_span(spans):
    assert spans is not None and spans.isValid()
    rows = list(spans.getFeatures())
    assert len(rows) == 1, f"expected one aerial span, got {len(rows)}"
    return rows[0]


# ── the field is published at all ───────────────────────────────────────────

def test_both_outputs_carry_pole_source():
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    poles = _poles([("PL-1", 0.0, 20.0, 9.0, 1.0, 4.0)])

    spans, cables = _run(premise, poles, legs=legs)

    assert "POLE_SOURCE" in spans.fields().names()
    assert "POLE_SOURCE" in cables.fields().names()


# ── the three verdicts ──────────────────────────────────────────────────────

def test_a_pole_the_run_designs_is_reported_as_planned():
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    poles = _poles([("PL-1", 0.0, 20.0, 9.0, 1.0, 4.0)])

    span = _one_span(_run(premise, poles, legs=legs)[0])

    assert span["POLE_SOURCE"] == "planned"
    assert span["POLE_ID"] == "PL-1"


def test_a_pole_from_the_existing_inventory_is_reported_as_existing():
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    planned = _poles([])
    existing = _poles([("BF-POLE-1", 0.0, 20.0, 9.0, 1.0, 4.0)], "bf_poles")

    span = _one_span(_run(premise, planned, bf_poles=existing, legs=legs)[0])

    assert span["POLE_SOURCE"] == "existing"
    assert span["POLE_ID"] == "BF-POLE-1"


def test_the_nearer_eligible_pole_wins_whichever_layer_it_is_in():
    """Reuse is preferred, not mandatory: the anchor is the nearest eligible
    pole, and the verdict follows the layer it actually came from."""
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    planned = _poles([("PL-1", 0.0, 10.0, 9.0, 1.0, 4.0)])       # 10 m
    existing = _poles([("BF-POLE-1", 0.0, 20.0, 9.0, 1.0, 4.0)],
                      "bf_poles")                                 # 20 m

    span = _one_span(_run(premise, planned, bf_poles=existing, legs=legs)[0])

    assert span["POLE_ID"] == "PL-1"
    assert span["POLE_SOURCE"] == "planned"


def test_a_house_to_house_span_names_a_building_not_a_pole():
    """A 'chain' leg leaves the house it feeds from, so it is neither existing
    plant nor a pole to build — and saying so keeps ``existing`` honest."""
    premises = _premises([("A1", 20.0, 0.0), ("B1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 40.0), (20.0, 0.0)]),
                  ("B1", [(20.0, 0.0), (0.0, 0.0)])])
    poles = _poles([("PL-1", 0.0, 10.0, 9.0, 1.0, 4.0)])

    spans, _cables = _run(premises, poles, legs=legs)

    by_addr = {row["TO_PREMISE"]: row for row in spans.getFeatures()}
    assert by_addr["B1"]["POLE_SOURCE"] == "building"
    assert by_addr["B1"]["POLE_ID"].startswith("BLDG-")


# ── the gates still hold ────────────────────────────────────────────────────

def test_a_pole_at_or_above_the_loading_limit_is_not_an_anchor():
    """POLE_SOURCE must never say ``existing`` about a pole the mandatory
    conditions exclude — the provenance follows the gate, not the inventory."""
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    full = _poles([("BF-FULL", 0.0, 20.0, 9.0, 4.0, 4.0)], "bf_poles")

    spans, _cables = _run(premise, _poles([]), bf_poles=full, legs=legs)

    assert list(spans.getFeatures()) == []


def test_a_pole_below_the_clearance_limit_is_not_an_anchor():
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    low = _poles([("BF-LOW", 0.0, 20.0, 5.0, 1.0, 4.0)], "bf_poles")

    spans, _cables = _run(premise, _poles([]), bf_poles=low, legs=legs)

    assert list(spans.getFeatures()) == []


def test_a_span_over_the_maximum_distance_is_refused():
    premise = _premises([("A1", 0.0, 0.0)])
    legs = _legs([("A1", [(0.0, 30.0), (0.0, 0.0)])])
    far = _poles([("PL-FAR", 0.0, 90.0, 9.0, 1.0, 4.0)])

    spans, _cables = _run(premise, far, legs=legs)

    # 90 m > MAX_DROP_DISTANCE_M (70) — the anchor is beyond the search radius
    assert list(spans.getFeatures()) == []
