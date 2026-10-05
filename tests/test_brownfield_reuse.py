"""The brownfield registry must reuse existing assets, and obey its gate.

The brownfield one-click pipeline is the SAME pipeline as the greenfield one
(`BrownfieldOneClickAlgorithm(EndToEndPipelineAlgorithm)`), so every stage
change flows through. What does NOT flow automatically is the brownfield
integration: the registry that carries the existing assets and the consumers
that reuse them. These tests pin the pieces that had drifted:

* the process-scoped registry and its reuse gate (with the gate off, a stored
  registry must be invisible to downstream stages — otherwise a standalone
  "Load Brownfield" run earlier in the same QGIS session leaks reuse into a
  greenfield design);
* `find_nearest_point_asset` reuse queries for chambers/poles;
* the chamber and pole output profiles carrying `REUSE_SOURCE`, so a reused
  asset id reaches the layer.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from qgis.core import QgsProcessingContext

from HLDPlanning.algorithms.oneclick import EndToEndPipelineAlgorithm
from HLDPlanning.utils.brownfield import (
    AssetType,
    BrownfieldRegistry,
    InfraStatus,
)
from HLDPlanning.utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields

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

    def pushWarning(self, msg):
        self.info.append(str(msg))

    def isCanceled(self):
        return False

    def text(self):
        return "\n".join(self.info + self.errors)


class _Steps:
    """MultiStepFeedback stand-in: `setCurrentStep` is all Stage 0 uses."""

    def __init__(self):
        self.step = None

    def setCurrentStep(self, n):
        self.step = n


def _point_layer(coords, name):
    lyr = QgsVectorLayer(f"Point?crs={CRS_AUTHID}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("id", QMetaType.Type.QString)])
    lyr.updateFields()
    feats = []
    for i, (x, y) in enumerate(coords):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f["id"] = f"A{i}"
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _registry():
    return BrownfieldRegistry(
        QgsCoordinateReferenceSystem(CRS_AUTHID), _Fb())


_CONTEXTS = []


def _context():
    """A processing context the algorithm can use; kept alive for the test."""
    ctx = QgsProcessingContext()
    _CONTEXTS.append(ctx)
    return ctx


def test_reuse_gate_hides_a_stored_registry():
    """A registry stored with reuse off is invisible to `load_from_project`."""
    reg = _registry()
    reg.load_chambers(_point_layer([(0, 0)], "chambers"), id_field="id")
    assert reg.asset_count == 1

    try:
        BrownfieldRegistry.store_registry(reg, enabled=True)
        assert BrownfieldRegistry.load_from_project() is reg

        BrownfieldRegistry.set_reuse_enabled(False)
        assert BrownfieldRegistry.load_from_project() is None
        # The gate must not clear the registry: the Existing Infrastructure
        # map layers still come from it while reuse is off.
        BrownfieldRegistry.set_reuse_enabled(True)
        assert BrownfieldRegistry.load_from_project() is reg
    finally:
        BrownfieldRegistry.store_registry(None, enabled=False)


def test_a_stored_empty_registry_is_never_loaded():
    try:
        BrownfieldRegistry.store_registry(_registry(), enabled=True)
        assert BrownfieldRegistry.load_from_project() is None
    finally:
        BrownfieldRegistry.store_registry(None, enabled=False)


def test_greenfield_stage_zero_off_applies_the_reuse_gate():
    """The parent Stage 0 must disable reuse when USE_BROWNFIELD is off.

    Only the brownfield subclass used to apply the gate, so a registry left
    over from a standalone Load Brownfield run in the same session was still
    reused by a later greenfield run with the toggle off.
    """
    reg = _registry()
    reg.load_chambers(_point_layer([(0, 0)], "chambers"), id_field="id")
    try:
        BrownfieldRegistry.store_registry(reg, enabled=True)
        assert BrownfieldRegistry.load_from_project() is reg

        algo = EndToEndPipelineAlgorithm()
        ctx = _context()
        algo._run_stage_brownfield(
            {}, ctx, _Steps(), _Fb(), {}, None,
        )
        # Toggle off (empty parameters) → downstream must see no registry.
        assert BrownfieldRegistry.load_from_project() is None
    finally:
        BrownfieldRegistry.store_registry(None, enabled=False)


def test_nearest_point_asset_finds_the_right_asset_type():
    reg = _registry()
    reg.load_chambers(_point_layer([(0, 0), (50, 0)], "chambers"),
                      id_field="id")
    reg.load_poles(_point_layer([(0.5, 0.5)], "poles"), id_field="id")

    pt = QgsPointXY(0.2, 0.1)
    chamber = reg.find_nearest_point_asset(
        pt, 2.0, asset_types={AssetType.CHAMBER, AssetType.PDP})
    pole = reg.find_nearest_point_asset(
        pt, 2.0, asset_types={AssetType.POLE})

    assert chamber is not None and pole is not None
    assert chamber[0].startswith("BF_CHAMBER_")
    assert pole[0].startswith("BF_POLE_")
    # 20 m away → outside the tolerance, no false reuse.
    assert reg.find_nearest_point_asset(
        QgsPointXY(20, 20), 2.0, asset_types={AssetType.CHAMBER}) is None


def test_consuming_a_chamber_marks_it_reused_and_blocks_second_use():
    reg = _registry()
    reg.load_chambers(_point_layer([(0, 0)], "chambers"), id_field="id")
    aid = reg.asset_ids()[0]

    assert reg.has_capacity(aid)
    assert reg.consume_capacity(aid)
    assert not reg.has_capacity(aid)
    assert reg.is_reused(aid)
    assert reg.classify_asset(aid) == InfraStatus.REUSED
    # capacity_total defaults to 1, so it cannot be handed out twice.
    assert not reg.consume_capacity(aid)


def test_chamber_and_pole_profiles_carry_reuse_source():
    for profile in ("CHAMBER", "POLE"):
        names = [f.name() for f in build_fields(THIN_PROFILES[profile])]
        assert COMMON_FIELDS.REUSE_SOURCE in names, profile
