"""A feeder duct needs a WELDED trench graph, not a lattice-snapped one.

The feeder-duct stage builds a graph from the trench network, snaps the MFG and
the PDPs onto it, and emits one duct per MFG→PDP route. Its node identity is a
lattice: ``round_key_xy(x, y, node_tol)`` rounds a coordinate onto a 0.5 m grid
(`utils/geom.py`). Two spans that touch *nearly* — an endpoint that lands 3 cm
from another span's interior — therefore get **different** nodes, because the two
coordinates sit in different cells. Breaks are only ever registered where
``lineintersections`` found a true crossing plus each span's own ends, so nothing
joins them either.

That is not a corner case. Measured on project `24cd3573` (Mariendorf,
2026-10-13): 236 of 762 span endpoints sit 0-0.05 m from another span and 274
more sit exactly on one; the published trench layer is **one** component within
0.5 m (380 joins) with all 31 PDPs on the MFG's component, while the graph the
stage built was **80** components reaching **0 of 31** PDPs. Every guard then
skipped the MFG and the run published zero feeder ducts behind a green
"✅ Feeder ducts created: 0".

`duct_layer.WELD_TOL_M` is the fix: a free span end is welded onto any span whose
interior it touches within that gap, registering the break **at the end's own
coordinates** so both spans share one coordinate — and therefore one node —
however the node is keyed.

These tests pin both halves: the near-miss junction yields a duct, and switching
the weld off is what loses it (so the test fails if the weld is removed, not just
if the routing changes). Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_feeder_duct_weld.py
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

from HLDPlanning.algorithms import duct_layer as dl

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS = "EPSG:25833"
ALG = dl.AlgFeederDuctsNoSplit()

# A projected origin in metres (the CRS only has to be metric and projected).
X0, Y0 = 397000.0, 5822000.0

# The gap at the T-junction: a branch that starts 3 cm off the trunk. Well
# inside the default node_tol (0.5 m) and far inside the 1.5 m snap tolerance,
# and outside GEOS intersection (the lines never meet).
GAP_M = 0.03

TRUNK = [(0.0, 0.0), (0.0, 400.0)]
BRANCH = [(0.0, 200.0 + GAP_M), (200.0, 200.0 + GAP_M)]
MFG = (0.0, 0.0, "MFG00001")
PDPS = [(100.0, 200.0 + GAP_M, "PDP00001"), (200.0, 200.0 + GAP_M, "PDP00002")]

_STRING = QMetaType.Type.QString


class _EmptyPointLayer:
    """What ``lineintersections`` answers here: the spans never meet."""

    def __init__(self):
        self._layer = QgsVectorLayer(f"Point?crs={CRS}", "intersections", "memory")
        self._layer.updateFields()

    def getFeatures(self):
        return self._layer.getFeatures()


class _Processing:
    """``qgis.processing`` shim — the plugin is not loaded in this harness.

    Only the four algorithms the stage calls, and faithfully for this input: the
    trunk and the branch do not intersect (that is the whole point of the test),
    and both are already single-part, so ``fixgeometries`` /
    ``multiparttosingleparts`` / ``deleteduplicategeometries`` are the identity.
    """

    def run(self, alg_id, params, context=None, feedback=None):
        if str(alg_id).endswith("lineintersections"):
            return {"OUTPUT": _EmptyPointLayer()}
        return {"OUTPUT": params.get("INPUT")}


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.warnings = []

    def pushWarning(self, msg):  # noqa: D102
        self.warnings.append(msg)


def _lines(rows):
    layer = QgsVectorLayer(f"LineString?crs={CRS}", "Final_Trenches", "memory")
    layer.dataProvider().addAttributes([QgsField("TRENCH_ID", _STRING)])
    layer.updateFields()
    feats = []
    for coords in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(X0 + x, Y0 + y) for x, y in coords]))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


def _points(rows):
    layer = QgsVectorLayer(f"Point?crs={CRS}", "points", "memory")
    layer.dataProvider().addAttributes([QgsField("ID", _STRING)])
    layer.updateFields()
    feats = []
    for x, y, name in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + x, Y0 + y)))
        f["ID"] = name
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


def _run(monkeypatch, weld_tol):
    """Run the real stage at one weld tolerance; return (ducts, feedback)."""
    monkeypatch.setattr(dl, "processing", _Processing())
    monkeypatch.setattr(dl, "WELD_TOL_M", weld_tol)

    mfg = _points([MFG])
    pdp = _points(PDPS)
    params = {
        ALG.L_NET: _lines([TRUNK, BRANCH]),
        ALG.L_MFG: mfg,
        ALG.L_PDP: pdp,
        ALG.F_MFGID: "ID",
        ALG.F_PDPID: "ID",
        ALG.SNAP_TOL: 1.5,
        ALG.NODE_TOL: 0.50,
        ALG.END_EPS: 0.25,
        ALG.INT_EPS: 0.25,
        ALG.INC_TRUNK: True,
        ALG.MAX_K: 4,
        ALG.ADD_STYLE: False,
        ALG.O_DUCTS: "memory:",
    }
    context = QgsProcessingContext()
    feedback = _Feedback()
    results, _ok = ALG.run(params, context, feedback)
    ducts = context.takeResultLayer(results[ALG.O_DUCTS])
    return ducts, feedback


def _length_m(ducts):
    return sum(float(f["length_m"] or 0.0) for f in ducts.getFeatures())


def test_a_near_miss_t_junction_still_yields_a_feeder_duct(monkeypatch):
    ducts, feedback = _run(monkeypatch, dl.WELD_TOL_M)

    assert ducts is not None and ducts.isValid()
    rows = list(ducts.getFeatures())
    assert rows, "the stage published no duct at all"

    served = {p for f in rows
              for p in str(f["pdp_ids"] or "").split(",") if p.strip()}
    assert served == {"PDP00001", "PDP00002"}, (
        f"the route reaches {sorted(served)}; both PDPs are on the branch")
    # The trunk (0→200 m) plus the branch (≤200 m) is the route, and it can only
    # be that long if the branch was joined to the trunk.
    assert _length_m(ducts) > 200.0
    assert not [w for w in feedback.warnings if "not welded" in w]


def test_the_weld_is_what_joins_the_near_miss(monkeypatch):
    """With the weld off, the same input produces no duct — and says so."""
    ducts, feedback = _run(monkeypatch, 0.0)

    assert ducts is not None
    assert ducts.featureCount() == 0, (
        "0 ducts is the pre-weld behaviour: the branch end and the trunk "
        "interior land in different lattice cells")
    assert [w for w in feedback.warnings if "not welded" in w], (
        "an empty feeder layer must not be silent: the warning names the "
        "reached nodes and PDPs so 'no route' and 'graph came apart' differ")
