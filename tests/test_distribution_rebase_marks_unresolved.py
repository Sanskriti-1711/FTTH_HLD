"""Unresolved distribution routes must stay visibly flagged for QA review."""

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


def _line_layer(name: str, lines: list[list[tuple[float, float]]], *, multi=False):
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
    feature = QgsFeature(layer.fields())
    parts = [[QgsPointXY(x, y) for x, y in coords] for coords in lines]
    geometry = (
        QgsGeometry.fromMultiPolylineXY(parts) if multi
        else QgsGeometry.fromPolylineXY(parts[0])
    )
    feature.setGeometry(geometry)
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


def test_unresolved_duct_keeps_legacy_geometry_and_gets_review_flags():
    """Disconnected trench components leave the legacy route intact and flagged."""
    trench = _line_layer("trench", [[(0, 0), (10, 0)], [(100, 0), (110, 0)]], multi=True)
    ducts = _line_layer("ducts", [[(0, 0), (10, 0), (100, 0), (110, 0)]])
    original = ducts.getFeature(1).geometry().asWkb()
    algo = DuctLayer()
    algo.ROUTE_DOCK_TOL_M = 0.25

    changed, unresolved = algo._rebase_distribution_output(
        ducts.id(), trench, _context_with_layer(ducts), None
    )

    assert (changed, unresolved) == (0, 1)
    feature = ducts.getFeature(1)
    assert feature.geometry().asWkb() == original
    assert feature["REVIEW"] == 1
    assert feature["INFRA_STATUS"] == "Review Required"


def test_a_rebased_duct_is_moved_to_trench_and_keeps_clear_review_status():
    """A connected route changes geometry and remains a normal proposed duct."""
    trench = _line_layer(
        "trench",
        [[(0, 0), (100, 0)], [(50, -50), (50, 50)]],
        multi=True,
    )
    ducts = _line_layer("ducts", [[(10, 0.05), (50, 40)]])
    algo = DuctLayer()

    changed, unresolved = algo._rebase_distribution_output(
        ducts.id(), trench, _context_with_layer(ducts), None
    )

    assert (changed, unresolved) == (1, 0)
    feature = ducts.getFeature(1)
    trench_union = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    assert feature.geometry().distance(trench_union) == pytest.approx(0.0, abs=1e-6)
    assert feature["REVIEW"] == 0
    assert feature["INFRA_STATUS"] == "Proposed"


def test_multipart_duct_is_not_partially_rebased_when_one_part_is_unreachable():
    """One failed part makes the whole logical duct unresolved."""
    trench = _line_layer("trench", [[(0, 0), (10, 0)]], multi=True)
    ducts = _line_layer(
        "ducts",
        [[(0, 0), (10, 0)], [(100, 0), (110, 0)]],
        multi=True,
    )
    original = ducts.getFeature(1).geometry().asWkb()
    algo = DuctLayer()

    changed, unresolved = algo._rebase_distribution_output(
        ducts.id(), trench, _context_with_layer(ducts), None
    )

    assert (changed, unresolved) == (0, 1)
    feature = ducts.getFeature(1)
    assert feature.geometry().asWkb() == original
    assert feature["REVIEW"] == 1
