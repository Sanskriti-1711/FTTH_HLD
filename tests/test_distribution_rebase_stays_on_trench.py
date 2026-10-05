"""The distribution rebase must leave every route ON the trench (rule D10).

The rebase draws a legacy distribution line on `Final_Trenches` by routing
between its two ends. When the trench network offers no connected route between
them — a break in the design, or two ends on severed pieces — the old code kept
the line's own **sidewalk** geometry and flagged it for review. Measured on the
CV1 2DE run: 144 of 2336 routes stayed on the pavement, every one about 3 m off
(the sidewalk offset), which is 1.8 % of the layer's length.

A duct may only ever lie ON the trench, so the fallback now PROJECTS the line
onto the trench: densify to `REBASE_PROJECT_DENSIFY_M` and snap every point to
its nearest trench point. It does not claim connectivity — it only stops the
duct from being drawn off the network.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
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
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.duct_layer import DuctLayer

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS_AUTHID = "EPSG:3857"


class _Fb:
    """Minimal feedback stand-in that keeps the messages it was given."""

    def __init__(self):
        self.info = []
        self.errors = []

    def pushInfo(self, msg):
        self.info.append(str(msg))

    def reportError(self, msg):
        self.errors.append(str(msg))

    def text(self):
        return "\n".join(self.info + self.errors)


def _line_layer(geoms, name):
    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("TRENCH_ID", QMetaType.Type.QString)])
    lyr.updateFields()
    feats = []
    for i, coords in enumerate(geoms):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        f["TRENCH_ID"] = f"T{i + 1}"
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _write_distribution(path, coords):
    """A on-disk line layer the rebase can open and edit (it needs a data path)."""
    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", "distribution", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([
        QgsField("REVIEW", QMetaType.Type.Int),
        QgsField("INFRA_STATUS", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY(
        [QgsPointXY(x, y) for x, y in coords]))
    f["REVIEW"] = 0
    f["INFRA_STATUS"] = ""
    pr.addFeatures([f])
    lyr.updateExtents()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    err, _msg, _new, _layer = QgsVectorFileWriter.writeAsVectorFormatV3(
        lyr, str(path), QgsProject.instance().transformContext(), opts)
    assert err == QgsVectorFileWriter.NoError, err
    return str(path)


def _context():
    """A processing context kept alive for the call.

    `mapLayerFromString` returns a layer OWNED by the context; passing a
    temporary `QgsProcessingContext()` lets it be garbage-collected the moment
    the call returns, deleting the layer and making the rebase iterate nothing.
    """
    ctx = QgsProcessingContext()
    _CONTEXTS.append(ctx)
    return ctx


_CONTEXTS = []


def _max_offset(geom, ref):
    return max(
        QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y())).distance(ref)
        for p in geom.vertices()
    )


# Two trench pieces 4 m apart at x=100: the union does not bridge a real gap
# (ROUTE_JOIN_TOL_M stays 0), so no connected route exists between their ends.
SPLIT_TRENCH = [[(0, 0), (100, 0)], [(100, 4), (200, 4)]]


def test_a_route_with_no_connected_path_is_projected_onto_the_trench(tmp_path):
    algo = DuctLayer()
    trench = _line_layer(SPLIT_TRENCH, "trench")
    # A sidewalk line 3 m off the corridor, running the length of both pieces.
    src = _write_distribution(tmp_path / "dist.gpkg", [(0, 3), (200, 3)])
    fb = _Fb()

    changed, unresolved = algo._rebase_distribution_output(
        src, trench, _context(), fb)

    assert changed == 1
    assert unresolved == 0
    assert "projected onto the trench" in fb.text()

    out = QgsVectorLayer(src, "out", "ogr")
    geom = next(out.getFeatures()).geometry()
    ref = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    # The source line sat 3 m off; every projected vertex now lands on the trench.
    assert _max_offset(geom, ref) <= 0.05


def test_a_route_with_a_connected_path_is_still_routed_not_projected(tmp_path):
    """The projection is a fallback — a routable pair keeps the routed path."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (200, 0)]], "trench")
    src = _write_distribution(tmp_path / "dist.gpkg", [(0, 3), (200, 3)])
    fb = _Fb()

    changed, unresolved = algo._rebase_distribution_output(
        src, trench, _context(), fb)

    assert changed == 1 and unresolved == 0
    assert "projected onto the trench" not in fb.text()
    out = QgsVectorLayer(src, "out", "ogr")
    geom = next(out.getFeatures()).geometry()
    ref = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    assert _max_offset(geom, ref) <= 0.05


def test_a_route_with_no_trench_in_range_is_still_unresolved_and_flagged(tmp_path):
    """Nothing to project onto → the old behaviour (keep it, flag for review)."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]], "trench")
    # 300 m from any trench: outside the 50 m candidate radius.
    src = _write_distribution(tmp_path / "dist.gpkg",
                              [(1000, 1000), (1200, 1000)])
    fb = _Fb()

    changed, unresolved = algo._rebase_distribution_output(
        src, trench, _context(), fb)

    assert changed == 0
    assert unresolved == 1
    assert "no trench to project onto" in fb.text()
    out = QgsVectorLayer(src, "out", "ogr")
    feat = next(out.getFeatures())
    assert feat["REVIEW"] == 1
    assert feat["INFRA_STATUS"] == "Review Required"
