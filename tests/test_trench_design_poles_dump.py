"""The pole hand-over: the drop chain gets POINTS, never a field-name clash.

The trench designer reads FILES, so the stage materialises every in-memory
layer to a GeoPackage before handing the paths over. For the existing poles
that write is the one place a *survey file* could fail the whole run: a planner
whose pole layer spells the capacity pair ``CAPACITY_TOTAL`` while a brownfield
archive spells it ``capacity_total`` produces a perfectly valid GeoJSON that
GeoPackage refuses ("Cannot create field capacity_total. A field with the same
name already exists.") — and the run died in the trench stage, several minutes
in, over an attribute nobody reads.

Only the geometry matters here: the docx chain asks one question of a pole
("is there an existing telecom pole within 50 m?"), so the stage writes the
points with no attributes at all. This pins that, and that the points, their
order and their CRS survive the round trip.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_trench_design_poles_dump.py

``processing.run`` is shimmed
------------------------------
``qgis.processing.run`` is only a placeholder that the Processing *plugin*
patches at load time, and the test harness loads no plugins:

    QgsNotSupportedException: Processing plugin has not been loaded

That stub is the plugin's business, not this stage's, so the fixture below
reaches the same algorithm through ``QgsApplication.processingRegistry()``
instead — the registry is what the plugin's own wrapper calls underneath. The
write under test, its parameters and its output are unchanged.
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
from qgis.PyQt.QtCore import QVariant

from HLDPlanning.algorithms import trench_design_layer as tdl
from HLDPlanning.algorithms.trench_design_layer import (
    TrenchDesignLayerAlgorithm as TDL,
)

pytestmark = pytest.mark.usefixtures("qgis_app")


@pytest.fixture(autouse=True)

def run_algorithms_through_the_registry(monkeypatch):
    """Execute ``processing.run`` through the algorithm registry.

    The plugin's wrapper adds parameter marshalling and error reporting; the
    algorithm that does the write, and the arguments this stage hands it, are
    identical — which is all an assertion about the written GeoPackage needs.
    """
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

_CRS = "EPSG:25833"
_COORDS = [(397000.0, 5822000.0), (397045.5, 5822044.5)]


def _pole_layer(extra_fields):
    """A pole point layer carrying ``extra_fields`` next to ``POLE_ID``."""
    layer = QgsVectorLayer(f"Point?crs={_CRS}", "poles", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("POLE_ID", QVariant.String)]
                           + [QgsField(name, QVariant.Int)
                              for name in extra_fields])
    layer.updateFields()
    features = []
    for index, (x, y) in enumerate(_COORDS):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feat["POLE_ID"] = f"PL-000{index + 1}"
        for name in extra_fields:
            feat[name] = 2
        features.append(feat)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _dump(layer, path: Path):
    return TDL._dump_points(layer, str(path), QgsProcessingContext(),
                            QgsProcessingFeedback())


def test_the_conflicting_spellings_that_killed_the_run_no_longer_reach_the_file(
        tmp_path):
    """``CAPACITY_TOTAL`` and ``capacity_total`` in one layer is the exact
    input that made the GeoPackage write fail. The dumped layer has neither."""
    layer = _pole_layer(["CAPACITY_TOTAL", "capacity_total", "CAPACITY_USED",
                         "capacity_used"])
    assert sorted(layer.fields().names()) == [
        "CAPACITY_TOTAL", "CAPACITY_USED", "POLE_ID", "capacity_total",
        "capacity_used",
    ]

    path = tmp_path / "Existing_Poles.gpkg"
    out = Path(_dump(layer, path))

    assert out.is_file()
    written = QgsVectorLayer(str(out), "Existing_Poles", "ogr")
    assert written.isValid()
    assert written.featureCount() == len(_COORDS)
    # A GeoPackage always carries its own `fid`; what must be gone is every
    # attribute the caller's file had — the collision lived in those.
    assert sorted(set(written.fields().names()) - {"fid"}) == []


def test_the_pole_points_reach_the_designer_unchanged(tmp_path):
    """Dropping the attributes must not move, lose or re-project a pole."""
    layer = _pole_layer(["CAPACITY_TOTAL"])

    written = QgsVectorLayer(str(_dump(layer, tmp_path / "Existing_Poles.gpkg")),
                            "Existing_Poles", "ogr")

    got = sorted((f.geometry().asPoint().x(), f.geometry().asPoint().y())
                 for f in written.getFeatures())
    assert got == pytest.approx(sorted(_COORDS))
    assert written.crs().authid() == _CRS


def test_a_pole_layer_with_no_geometry_does_not_write_an_empty_file(tmp_path):
    """An empty dump would read back as "no poles", which is the correct
    answer — but the write must not raise on the way there."""
    layer = QgsVectorLayer(f"Point?crs={_CRS}", "poles", "memory")
    layer.dataProvider().addAttributes([QgsField("POLE_ID", QVariant.String)])
    layer.updateFields()

    out = Path(_dump(layer, tmp_path / "Existing_Poles.gpkg"))

    written = QgsVectorLayer(str(out), "Existing_Poles", "ogr")
    assert written.featureCount() == 0
