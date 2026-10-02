"""A full distribution duct is capped against its own route, not its chord.

`_rebase_distribution_output` re-draws each legacy (sidewalk-graph) duct as a
route on the trench. It used to borrow the *tap* detour cap, which compares the
route to the straight chord. A winding distribution route is routinely many
times its chord, so the cap refused it and the duct was left on the sidewalk
(rule D10). The rebase now passes the legacy route length as the cap's
reference; a tap still uses the chord.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.duct_layer import DuctLayer

pytestmark = pytest.mark.usefixtures("qgis_app")
CRS = "EPSG:3857"

# A U-shaped trench: east along y=0, down to y=20, then back west to x=0.
# The two ends (0,0) and (0,20) are a 20 m chord apart but 220 m apart along it.
U_TRENCH = [(0, 0), (100, 0), (100, 20), (0, 20)]


def _line_layer(name, lines, *, multi=False):
    kind = "MultiLineString" if multi else "LineString"
    layer = QgsVectorLayer(f"{kind}?crs={CRS}", name, "memory")
    assert layer.isValid()
    provider = layer.dataProvider()
    provider.addAttributes([
        QgsField("POLYGON_ID", QMetaType.Type.QString),
        QgsField("pdp_ids", QMetaType.Type.QString),
        QgsField("REVIEW", QMetaType.Type.Int),
        QgsField("INFRA_STATUS", QMetaType.Type.QString),
    ])
    layer.updateFields()
    for coords in lines:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        feature["POLYGON_ID"] = "POLY1"
        feature["pdp_ids"] = "PDP1"
        feature["REVIEW"] = 0
        feature["INFRA_STATUS"] = "Proposed"
        provider.addFeature(feature)
    layer.updateExtents()
    return layer


def _context_with_layer(layer):
    project = QgsProject.instance()
    project.addMapLayer(layer)
    context = QgsProcessingContext()
    context.setProject(project)
    return context


def test_chord_cap_would_refuse_the_u_route():
    """Pin the cause: with only the chord to go on, the route is 'absurd'."""
    algo = DuctLayer()
    trench = _line_layer("trench", [U_TRENCH])
    # 220 m of trench for a 20 m chord is 11x the chord → over the tap cap.
    assert algo._trench_route(trench, (0, 0), (0, 20)) is None


def test_rebase_uses_the_legacy_route_length_and_keeps_the_duct_on_the_trench():
    """The same route is accepted once it is measured against what it replaces."""
    algo = DuctLayer()
    trench = _line_layer("trench", [U_TRENCH])
    ducts = _line_layer("ducts", [U_TRENCH])
    original = ducts.getFeature(1).geometry().asWkb()

    changed, unresolved = algo._rebase_distribution_output(
        ducts.id(), trench, _context_with_layer(ducts), None
    )

    assert (changed, unresolved) == (1, 0)
    feature = ducts.getFeature(1)
    assert feature.geometry().asWkb() != original
    trench_union = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    # Every vertex sits on the trench, and it is the trench path, not the chord.
    assert all(
        QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y())).distance(trench_union)
        == pytest.approx(0.0, abs=1e-6)
        for p in feature.geometry().vertices()
    )
    assert feature.geometry().length() == pytest.approx(220.0, abs=1e-6)
    assert feature["REVIEW"] == 0
    assert feature["INFRA_STATUS"] == "Proposed"


def test_tap_still_uses_the_chord_cap():
    """The reference is opt-in: a tap without it keeps the old refusal."""
    algo = DuctLayer()
    trench = _line_layer("trench", [U_TRENCH])
    assert algo._trench_connector(trench, (0, 0), (0, 20)) is None
    routed = algo._trench_connector(trench, (0, 0), (10, 0))
    assert routed is not None and routed.length() == pytest.approx(10.0, abs=1e-6)
