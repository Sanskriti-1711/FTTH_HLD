"""The published trench fields must describe the geometry that ships.

`AlgDistributionDucts` stamps OFF_TRENCH_LEN_M / TRENCH_STATUS against the
sidewalk route it drew; `DuctLayer._rebase_distribution_output` then moves that
geometry onto Final_Trenches. Measured on the Mariendorf run, the fields and the
map then disagreed by 57 % of the layer's length: the layer said 6,348 of 7,142
ducts were off the trench while 97.6 % of its 1,436 km sat within 5 cm of one.

`DuctLayer._restamp_trench_metrics` rewrites both fields from the rebased
geometry, so "ducts lie on the trench" can be read off the layer itself.

The field counts METRES OF DUCT that are not covered by a trench, not the
distance from the duct to one: a 100 m duct lying 10 m away from a trench reads
100.0 m off, not 10.0.
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

CRS_AUTHID = "EPSG:3857"
_CONTEXTS = []


class _Fb:
    def __init__(self):
        self.info = []
        self.errors = []

    def pushInfo(self, msg):
        self.info.append(str(msg))

    def reportError(self, msg):
        self.errors.append(str(msg))

    def text(self):
        return "\n".join(self.info + self.errors)


def _context():
    ctx = QgsProcessingContext()
    _CONTEXTS.append(ctx)
    return ctx


def _line_layer(geoms, name="trench"):
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


def _write_ducts(path, coords, off, status):
    """An on-disk distribution layer carrying the fields the restamp rewrites."""
    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", "distribution", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([
        QgsField("OFF_TRENCH_LEN_M", QMetaType.Type.Double),
        QgsField("TRENCH_STATUS", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY(
        [QgsPointXY(x, y) for x, y in coords]))
    f["OFF_TRENCH_LEN_M"] = off
    f["TRENCH_STATUS"] = status
    pr.addFeatures([f])
    lyr.updateExtents()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    err, _msg, _new, _layer = QgsVectorFileWriter.writeAsVectorFormatV3(
        lyr, str(path), QgsProject.instance().transformContext(), opts)
    assert err == QgsVectorFileWriter.NoError, err
    return str(path)


def test_a_duct_drawn_on_the_trench_is_published_as_on_it(tmp_path):
    """The stale 'OFF TRENCH' written by the routing pass is replaced by 0."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]])
    src = _write_ducts(tmp_path / "dist.gpkg", [(0, 0), (100, 0)],
                       100.0, "OFF TRENCH 100.0 m")
    fb = _Fb()

    changed = algo._restamp_trench_metrics(src, trench, _context(), fb)

    assert changed == 1
    feat = next(QgsVectorLayer(src, "out", "ogr").getFeatures())
    assert feat["OFF_TRENCH_LEN_M"] <= 0.05
    assert feat["TRENCH_STATUS"] == "On trench"
    assert "1 of 1 duct(s) lie on" in fb.text()


def test_a_lead_in_keeps_its_band(tmp_path):
    """90 m of duct on the trench plus a 10 m leg off it reads as a lead-in."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]])
    src = _write_ducts(tmp_path / "dist.gpkg",
                       [(0, 0), (90, 0), (90, 10)], 0.0, "")
    fb = _Fb()

    assert algo._restamp_trench_metrics(src, trench, _context(), fb) == 1
    feat = next(QgsVectorLayer(src, "out", "ogr").getFeatures())
    # 10 m of leg, minus the 5 cm the trench's coverage buffer reaches into it
    # where the leg turns off the trench -- a tolerance bleeding 5 cm past the
    # trench end is the price of reading a centimetre-off duct as on it.
    assert feat["OFF_TRENCH_LEN_M"] == pytest.approx(9.95, abs=0.01)
    assert feat["TRENCH_STATUS"].startswith("Lead-in")
    assert "0 of 1 duct(s) lie on" in fb.text()


def test_a_duct_beyond_the_lead_in_cap_is_flagged_off(tmp_path):
    """A 42 m leg is past the 25 m lead-in cap: it stays OFF TRENCH, not hidden."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]])
    src = _write_ducts(tmp_path / "dist.gpkg",
                       [(0, 0), (100, 0), (100, 42)], 0.0, "")
    fb = _Fb()

    assert algo._restamp_trench_metrics(src, trench, _context(), fb) == 1
    feat = next(QgsVectorLayer(src, "out", "ogr").getFeatures())
    assert feat["OFF_TRENCH_LEN_M"] == pytest.approx(41.95, abs=0.01)
    assert feat["TRENCH_STATUS"].startswith("OFF TRENCH")
    assert "off by 42.0 m" in fb.text()


def test_a_duct_snapped_a_couple_of_centimetres_off_still_reads_as_on_it(tmp_path):
    """Coverage is within 5 cm, not exact collinearity.

    The rebase projects a broken route onto the trench point-wise, so its
    vertices land on the trench while the chords run a centimetre or two beside
    it.  Subtracting the trench line from that duct used to leave its whole
    length behind, which is how the Birmingham run of 2026-11-02 published
    "4 of 346 duct(s) lie on Final_Trenches" for a layer that had just been
    rebased onto it.
    """
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]])
    src = _write_ducts(tmp_path / "dist.gpkg", [(0, 0.02), (100, 0.02)],
                       100.0, "OFF TRENCH 100.0 m")
    fb = _Fb()

    assert algo._restamp_trench_metrics(src, trench, _context(), fb) == 1
    feat = next(QgsVectorLayer(src, "out", "ogr").getFeatures())
    assert feat["OFF_TRENCH_LEN_M"] <= 0.05
    assert feat["TRENCH_STATUS"] == "On trench"
    assert "1 of 1 duct(s) lie on" in fb.text()


def test_a_duct_a_metre_beside_the_trench_is_still_off(tmp_path):
    """The tolerance must not swallow a duct that genuinely is not on it."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)]])
    src = _write_ducts(tmp_path / "dist.gpkg", [(0, 1.0), (100, 1.0)], 0.0, "")
    fb = _Fb()

    assert algo._restamp_trench_metrics(src, trench, _context(), fb) == 1
    feat = next(QgsVectorLayer(src, "out", "ogr").getFeatures())
    assert feat["OFF_TRENCH_LEN_M"] == pytest.approx(100.0, abs=0.05)
    assert feat["TRENCH_STATUS"].startswith("OFF TRENCH")
