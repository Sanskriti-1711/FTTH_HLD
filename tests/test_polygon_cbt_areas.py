"""Growth builder with the optional CBT-area output.

Runs the seeded-growth polygon algorithm on a synthetic grid of buildings and
checks the CBT invariants from CABINET_SPINE_PLAN.md Phase 1:

  * every CBT area holds at most MAX_HH_PER_CBT_AREA homes;
  * the CBT homes of each PDP area add up to that PDP's homes;
  * with the cap set to 0 the CBT output is empty and the PDP output is unchanged.

Runs under the QGIS interpreter, like the rest of the suite:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.polygon_layer import PolygonLayerAlgorithm

pytestmark = pytest.mark.usefixtures("qgis_app")


def _buildings_layer(n_side=12, spacing=40.0, homes=4):
    """A square grid of single-home buildings, `homes` each, in a metric CRS."""
    lyr = QgsVectorLayer("Point?crs=EPSG:27700", "buildings", "memory")
    prov = lyr.dataProvider()
    prov.addAttributes([
        QgsField("households", QMetaType.Type.Int),
        QgsField("ADDR_ID", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    feats = []
    for i in range(n_side):
        for j in range(n_side):
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(
                QgsPointXY(400000 + i * spacing, 300000 + j * spacing)))
            f["households"] = homes
            f["ADDR_ID"] = f"A{i:03d}{j:03d}"
            feats.append(f)
    prov.addFeatures(feats)
    return lyr


def _run(lyr, cbt_cap, tmp_path):
    alg = PolygonLayerAlgorithm()
    alg.initAlgorithm()
    ctx = QgsProcessingContext()
    fb = QgsProcessingFeedback()
    params = {
        "INPUT": lyr,
        "METHOD": 3,  # seeded_growth
        "MIN_HH_PER_POLYGON": 32,
        "MAX_HH_PER_POLYGON": 64,
        "MAX_HH_PER_CBT_AREA": cbt_cap,
        "OUT": str(tmp_path / "pdp.gpkg"),
        "CBT_OUT": str(tmp_path / "cbt.gpkg"),
    }
    return alg.run(params, ctx, fb)


def test_cbt_areas_respect_cap_and_sum_to_pdp_homes(tmp_path):
    lyr = _buildings_layer()
    result, ok = _run(lyr, 16, tmp_path)
    assert ok, "growth run failed"

    pdp = QgsVectorLayer(result["OUT"], "pdp", "ogr")
    cbt = QgsVectorLayer(result["CBT_OUT"], "cbt", "ogr")
    assert pdp.isValid() and cbt.isValid()
    assert cbt.featureCount() > 0

    cbt_hh_of_pdp = {}
    for f in cbt.getFeatures():
        hh = f["households"]
        assert hh <= 16, f"CBT {f['CBT_AREA_ID']} holds {hh} homes (> 16)"
        cbt_hh_of_pdp[f["POLYGON_ID"]] = cbt_hh_of_pdp.get(f["POLYGON_ID"], 0) + hh

    for f in pdp.getFeatures():
        pid = f["POLYGON_ID"]
        assert cbt_hh_of_pdp.get(pid, 0) == f["households"], (
            f"PDP {pid}: CBT homes {cbt_hh_of_pdp.get(pid, 0)} != {f['households']}"
        )


def test_cbt_output_off_when_cap_is_zero(tmp_path):
    lyr = _buildings_layer(n_side=6)
    result, ok = _run(lyr, 0, tmp_path)
    assert ok
    cbt = QgsVectorLayer(result["CBT_OUT"], "cbt", "ogr")
    assert cbt.featureCount() == 0


def _grid_layer(homes_per_building):
    """Buildings on a grid, one per entry, with the given homes each."""
    n = len(homes_per_building)
    side = 1
    while side * side < n:
        side += 1
    lyr = QgsVectorLayer("Point?crs=EPSG:27700", "buildings", "memory")
    prov = lyr.dataProvider()
    prov.addAttributes([
        QgsField("households", QMetaType.Type.Int),
        QgsField("ADDR_ID", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    feats = []
    for k, hh in enumerate(homes_per_building):
        i, j = divmod(k, side)
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(400000 + i * 40.0, 300000 + j * 40.0)))
        f["households"] = hh
        f["ADDR_ID"] = f"A{k:05d}"
        feats.append(f)
    prov.addFeatures(feats)
    return lyr


@pytest.mark.parametrize("total", [8, 16, 17, 32, 33, 48, 64, 250])
def test_home_count_edge_cases_respect_cap(total, tmp_path):
    # One home per building, so the building count equals the home count.
    lyr = _grid_layer([1] * total)
    result, ok = _run(lyr, 16, tmp_path)
    assert ok

    cbt = QgsVectorLayer(result["CBT_OUT"], "cbt", "ogr")
    hh = [f["households"] for f in cbt.getFeatures()]
    assert hh, "no CBT areas for a non-empty input"
    assert max(hh) <= 16
    assert sum(hh) == total


def test_each_cbt_area_is_one_contiguous_piece(tmp_path):
    lyr = _grid_layer([1] * 250)
    result, ok = _run(lyr, 16, tmp_path)
    assert ok

    cbt = QgsVectorLayer(result["CBT_OUT"], "cbt", "ogr")
    for f in cbt.getFeatures():
        geom = f.geometry()
        assert geom.isGeosValid()
        assert len(geom.asGeometryCollection()) == 1, (
            f"CBT {f['CBT_AREA_ID']} is split into several pieces"
        )


def test_pdp_polygons_identical_with_cbt_cap_off(tmp_path):
    lyr = _buildings_layer()
    off_dir = tmp_path / "off"
    on_dir = tmp_path / "on"
    off_dir.mkdir()
    on_dir.mkdir()

    off, ok_off = _run(lyr, 0, off_dir)
    on, ok_on = _run(lyr, 16, on_dir)
    assert ok_off and ok_on

    def _pdp_wkt(path):
        layer = QgsVectorLayer(path, "pdp", "ogr")
        return sorted(
            (f["POLYGON_ID"], f["households"], f.geometry().asWkt())
            for f in layer.getFeatures()
        )

    assert _pdp_wkt(off["OUT"]) == _pdp_wkt(on["OUT"])
