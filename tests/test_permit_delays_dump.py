"""The permit hand-over: one canonical field, whatever shape the register is.

The permit layer reaches the designer as a FILE, and the designer reads one
field for the lead time. Writing that file is where a planner's own register
could fail the run: a layer carrying both ``LEAD_TIME_DAYS`` and
``lead_time_days`` is valid GeoJSON and refused by GeoPackage ("Cannot create
field lead_time_days. A field with the same name already exists.") — the same
trap the pole hand-over had.

``_dump_permit_delays`` therefore rebuilds the layer around one ``lead_time_days``
field. This pins that it does, that it survives every geometry a register
actually arrives in (authority areas as polygons, a per-street register as
lines, a depot as a point) and that it refuses a layer with no readable lead
time rather than writing an empty one — "this authority is fast" and "we never
asked" are different facts.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_permit_delays_dump.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms import trench_design_layer as tdl
from HLDPlanning.algorithms.trench_design_layer import (
    TrenchDesignLayerAlgorithm as TDL,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS = "EPSG:25833"
X0, Y0 = 397000.0, 5822000.0


@pytest.fixture(autouse=True)
def run_algorithms_through_the_registry(monkeypatch):
    """``processing.run`` shim — see test_trench_design_poles_dump.py."""
    from qgis.analysis import QgsNativeAlgorithms

    registry = QgsApplication.processingRegistry()
    if registry.providerById("native") is None:
        registry.addProvider(QgsNativeAlgorithms())

    def _run(alg_id, params, context=None, feedback=None,
             is_child_algorithm=False):
        algorithm = registry.createAlgorithmById(alg_id)
        assert algorithm is not None, f"no algorithm {alg_id!r} in the registry"
        context = context or QgsProcessingContext()
        feedback = feedback or QgsProcessingFeedback()
        results, _ok = algorithm.run(params, context, feedback)
        return results or {}

    monkeypatch.setattr(tdl.processing, "run", _run)


def _square(offset=0.0):
    ring = [(X0 + offset, Y0), (X0 + 50 + offset, Y0),
            (X0 + 50 + offset, Y0 + 50), (X0 + offset, Y0 + 50),
            (X0 + offset, Y0)]
    return QgsGeometry.fromPolygonXY(
        [[QgsPointXY(x, y) for x, y in ring]])


def _permit_layer(geom_type, features):
    """features: (geometry, properties dict)."""
    layer = QgsVectorLayer(f"{geom_type}?crs={CRS}", "permits", "memory")
    names = []
    for _geom, props in features:
        for name in props:
            if name not in names:
                names.append(name)
    layer.dataProvider().addAttributes(
        [QgsField(name, QMetaType.Type.Double) for name in names])
    layer.updateFields()
    for geom, props in features:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        for name, value in props.items():
            feat[name] = value
        layer.dataProvider().addFeature(feat)
    layer.updateExtents()
    return layer


def _dump(layer, path: Path):
    return TDL._dump_permit_delays(layer, str(path), QgsProcessingContext(),
                                   QgsProcessingFeedback())


def _read(path):
    layer = QgsVectorLayer(str(path), "Permit_Delays", "ogr")
    assert layer.isValid()
    return layer


# ── the field is normalised ─────────────────────────────────────────────────

def test_the_case_twins_that_killed_the_run_are_gone(tmp_path):
    layer = _permit_layer("Polygon", [
        (_square(), {"LEAD_TIME_DAYS": 90.0, "lead_time_days": 30.0}),
    ])
    assert sorted(layer.fields().names()) == ["LEAD_TIME_DAYS",
                                              "lead_time_days"]

    written = _read(Path(_dump(layer, tmp_path / "Permit_Delays.gpkg")))

    assert sorted(written.fields().names()) == ["fid", "lead_time_days"]
    (feat, ) = written.getFeatures()
    # the first accepted spelling wins, deterministically
    assert feat["lead_time_days"] == 90.0


def test_any_of_the_accepted_spellings_is_read(tmp_path):
    for i, field in enumerate(tdl.permit_field_names()):
        layer = _permit_layer("Polygon", [
            (_square(i * 100.0), {field: 45.0}),
        ])
        written = _read(Path(_dump(layer, tmp_path / f"{field}.gpkg")))
        (feat, ) = written.getFeatures()
        assert feat["lead_time_days"] == 45.0, field


def test_features_without_a_readable_lead_time_are_dropped(tmp_path):
    layer = _permit_layer("Polygon", [
        (_square(0.0), {"lead_time_days": 45.0}),
        (_square(100.0), {}),
    ])
    written = _read(Path(_dump(layer, tmp_path / "Permit_Delays.gpkg")))
    assert written.featureCount() == 1


def test_a_layer_with_no_readable_lead_time_is_refused(tmp_path):
    """None, not an empty file: the caller reports it, and the trigger then
    stays project-wide instead of silently reading as "every authority is
    fast"."""
    layer = _permit_layer("Polygon", [(_square(), {"name": 0.0})])
    assert _dump(layer, tmp_path / "Permit_Delays.gpkg") is None


# ── every geometry a register arrives in ────────────────────────────────────

def test_a_polygon_register_round_trips(tmp_path):
    layer = _permit_layer("Polygon", [(_square(), {"lead_time_days": 90.0})])
    written = _read(Path(_dump(layer, tmp_path / "p.gpkg")))
    assert written.featureCount() == 1
    assert written.crs().authid() == CRS


def test_a_multipolygon_register_survives(tmp_path):
    """The shape an authority layer is dissolved into — and the one a
    single-geometry memory layer silently refuses."""
    multi = QgsGeometry.collectGeometry([_square(0.0), _square(200.0)])
    assert multi.wkbType() == 6            # MultiPolygon
    layer = _permit_layer("MultiPolygon",
                          [(multi, {"lead_time_days": 90.0})])

    written = _read(Path(_dump(layer, tmp_path / "mp.gpkg")))

    assert written.featureCount() == 1
    (feat, ) = written.getFeatures()
    assert feat["lead_time_days"] == 90.0
    assert not feat.geometry().isEmpty()


def test_a_per_street_register_of_lines_survives(tmp_path):
    line = QgsGeometry.fromPolylineXY(
        [QgsPointXY(X0, Y0), QgsPointXY(X0 + 100, Y0)])
    layer = _permit_layer("LineString", [(line, {"lead_time_days": 75.0})])

    written = _read(Path(_dump(layer, tmp_path / "l.gpkg")))

    assert written.featureCount() == 1
    (feat, ) = written.getFeatures()
    assert feat["lead_time_days"] == 75.0


def test_a_multiline_register_survives(tmp_path):
    multi = QgsGeometry.fromMultiPolylineXY([[
        QgsPointXY(X0, Y0), QgsPointXY(X0 + 100, Y0)]])
    layer = _permit_layer("MultiLineString",
                          [(multi, {"lead_time_days": 20.0})])

    written = _read(Path(_dump(layer, tmp_path / "ml.gpkg")))

    assert written.featureCount() == 1


def test_a_single_depot_point_survives(tmp_path):
    point = QgsGeometry.fromPointXY(QgsPointXY(X0, Y0))
    layer = _permit_layer("Point", [(point, {"lead_time_days": 10.0})])

    written = _read(Path(_dump(layer, tmp_path / "pt.gpkg")))

    assert written.featureCount() == 1
    (feat, ) = written.getFeatures()
    assert feat["lead_time_days"] == 10.0
