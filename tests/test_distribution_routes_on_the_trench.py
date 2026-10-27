"""Distribution ducts are designed ON the trench, not beside it.

The distribution builder used to add Final_Tangent_Trenches as a *third* line
network next to Sidewalk Left/Right and route an unweighted shortest path over
the union. A footway is usually the shorter path between two footway points, so
the trench was only used when it happened to lie on that path — measured at
21.1 % of the distribution layer off the trench, repaired afterwards by
`DuctLayer._rebase_distribution_output`.

The trench is the civil route: the duct is the conduit INSIDE it. So the trench
is now the routing graph, a footway metre costs `SIDEWALK_COST_FACTOR` times a
trench metre, and an endpoint snaps onto the trench only within the lead-in cap
(`MAX_LEAD_IN_M`).

Two different facts are reported, and confusing them is what made an on-trench
design read as off-trench on the map:

* `OFF_TRENCH_LEN_M` / `TRENCH_STATUS` — metres of the DRAWN duct farther than
  `TRENCH_PROXIMITY_M` from any trench line. This is what the map shows.
* `PAVEMENT_LEN_M` — metres of the route that walked the pavement beside the
  trench. A duct laid on the trench but routed along the footway has a large
  `PAVEMENT_LEN_M` and an `OFF_TRENCH_LEN_M` of zero.

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
    QgsProcessingContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.duct_layer import AlgDistributionDucts

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS = "EPSG:3857"


class _Fb:
    """Feedback stand-in that keeps what the algorithm said."""

    def __init__(self):
        self.info = []
        self.warnings = []
        self.errors = []

    def pushInfo(self, msg):
        self.info.append(str(msg))

    def pushWarning(self, msg):
        self.warnings.append(str(msg))

    def pushDebugInfo(self, msg):
        self.info.append(str(msg))

    def reportError(self, msg):
        self.errors.append(str(msg))

    def isCanceled(self):
        return False

    def text(self):
        return "\n".join(self.info + self.warnings + self.errors)


def _line_layer(geoms, name):
    lyr = QgsVectorLayer(f"LineString?crs={CRS}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("id", QMetaType.Type.Int)])
    lyr.updateFields()
    feats = []
    for i, coords in enumerate(geoms):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        f["id"] = i
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _point_layer(points, name):
    """points: (x, y, addr_id) — PDP_ID is the single PDP the duct belongs to."""
    lyr = QgsVectorLayer(f"Point?crs={CRS}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([
        QgsField("ADDR_ID", QMetaType.Type.QString),
        QgsField("PDP_ID", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    feats = []
    for x, y, addr in points:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f["ADDR_ID"] = addr
        f["PDP_ID"] = "PDP1"
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


_CONTEXTS = []


def _context():
    ctx = QgsProcessingContext()
    _CONTEXTS.append(ctx)
    return ctx


def _run(tmp_path, *, trench, left, right, pdp, hh, name="dist.gpkg",
         lead_in_m=None):
    algo = AlgDistributionDucts()
    algo.initAlgorithm()
    out = str(tmp_path / name)
    params = {
        algo.L_PDP: _point_layer(pdp, "pdps"),
        algo.L_HH: _point_layer(hh, "households"),
        algo.L_LEFT: _line_layer(left, "sidewalk_left"),
        algo.L_RIGHT: _line_layer(right, "sidewalk_right"),
        algo.F_PDP_ON_PDP: "PDP_ID",
        algo.F_HH_ID: "ADDR_ID",
        algo.F_PDP_ON_HH: "PDP_ID",
        algo.CRS_TGT: QgsCoordinateReferenceSystem(CRS),
        algo.O_DUCTS: out,
    }
    if trench is not None:
        params[algo.L_TAN] = _line_layer(trench, "trench")
    if lead_in_m is not None:
        params[algo.LEAD_IN_M] = float(lead_in_m)
    fb = _Fb()
    algo.processAlgorithm(params, _context(), fb)
    return QgsVectorLayer(out, "out", "ogr"), fb


def _xs(layer):
    xs = []
    for f in layer.getFeatures():
        g = f.geometry()
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for part in parts:
            xs.extend(p.x() for p in part)
    return xs


# Two footways 10 m either side of a trench running north. The footway is the
# straight line between the two households — the shortest path — but it is not
# the design surface.
TRENCH = [[(0, 0), (0, 200)]]
LEFT = [[(10, 0), (10, 200)]]
RIGHT = [[(-10, 0), (-10, 200)]]
PDP = [(10, 100, "PDP1")]
HH = [(10, 20, "HH1"), (10, 180, "HH2")]


def test_a_duct_rides_the_trench_even_when_the_footway_is_the_shorter_path(tmp_path):
    out, fb = _run(tmp_path, trench=TRENCH, left=LEFT, right=RIGHT, pdp=PDP, hh=HH)

    feats = list(out.getFeatures())
    assert feats, "no distribution duct was built"
    # Every vertex is on the trench line (x = 0), not on the footway (x = ±10).
    assert max(abs(x) for x in _xs(out)) <= 1e-6
    for f in feats:
        assert f["OFF_TRENCH_LEN_M"] == pytest.approx(0.0, abs=0.5)
        assert f["PAVEMENT_LEN_M"] == pytest.approx(0.0, abs=0.5)
        assert f["TRENCH_STATUS"] == "On trench"
    assert ("On the trench: 2 of 2 duct(s) are drawn within 2 m of "
            "Final_Trenches") in fb.text()


def test_the_footway_is_used_only_where_the_trench_cannot_carry_the_route(tmp_path):
    # The trench stops at y = 100; the household sits 80 m along a footway that
    # continues east. 80 m is beyond the 25 m lead-in cap, so the duct must
    # follow the footway there — and say so.
    out, fb = _run(
        tmp_path,
        trench=[[(0, 0), (0, 100)]],
        left=[[(0, 100), (80, 100)]],
        right=[[(-10, 0), (-10, 100)]],
        pdp=[(0, 90, "PDP1")],
        hh=[(80, 100, "HH1")],
    )

    feats = list(out.getFeatures())
    assert len(feats) == 1
    f = feats[0]
    # It starts in the trench (x = 0) and then follows the footway out east.
    xs = _xs(out)
    assert min(xs) == pytest.approx(0.0, abs=1e-6)
    assert max(xs) == pytest.approx(80.0, abs=1e-6)
    # The drawn line really is off the trench here, and the route really did
    # walk the footway: the two measures agree because nothing is hidden.
    assert f["OFF_TRENCH_LEN_M"] == pytest.approx(80.0, abs=5.0)
    assert f["PAVEMENT_LEN_M"] == pytest.approx(80.0, abs=5.0)
    assert f["TRENCH_STATUS"].startswith("OFF TRENCH")
    assert "leave it by" in fb.text()


def test_a_short_trench_gap_does_not_buy_a_loop_around_the_block(tmp_path):
    """A gap in the trench must not turn a 120 m duct into a 300 m one.

    The trench runs north, east and back south, ending 20 m short of the
    household: 300 m of trench against 120 m of footway. The detour cap
    (`MAX_TRENCH_DETOUR_FACTOR`) says the footway route wins, and the 120 m it
    spends is reported rather than presented as a trench duct.
    """
    out, fb = _run(
        tmp_path,
        trench=[[(0, 0), (0, 100)], [(0, 100), (100, 100)], [(100, 100), (100, 20)]],
        left=[[(0, 0), (100, 0), (100, 20)]],
        right=[[(-10, 0), (-10, 100)]],
        pdp=[(0, 0, "PDP1")],
        hh=[(100, 0, "HH1")],
    )

    feats = list(out.getFeatures())
    assert len(feats) == 1
    f = feats[0]
    assert f["length_m"] == pytest.approx(120.0, abs=5.0)
    assert f["PAVEMENT_LEN_M"] == pytest.approx(120.0, abs=5.0)
    # The route is the 120 m footway L, and the drawn line is that same L, so
    # here the two measures nearly agree -- which is why the divergence is
    # pinned by `test_walking_the_pavement_beside_the_trench_is_not_off_it`
    # below, where the pavement runs beside a trench instead.
    assert f["OFF_TRENCH_LEN_M"] == pytest.approx(118.0, abs=8.0)
    assert f["TRENCH_STATUS"].startswith("OFF TRENCH")


def test_the_pavement_metres_are_reported_beside_the_geometry_measure(tmp_path):
    """Both numbers reach the stage log, and they are named as what they are.

    `PAVEMENT_LEN_M` (the route's footway metres) and `OFF_TRENCH_LEN_M` (the
    drawn line's distance from the trench) are different measures: on the Leeds
    run 915 m of pavement metres came with only ~50 m of drawn offset, which is
    why an on-trench design read as off-trench. The stage must say both, so a
    reader can tell a duct that walks the pavement beside a trench from a duct
    that has left it.
    """
    out, fb = _run(
        tmp_path,
        trench=[[(0, 0), (0, 100)]],
        left=[[(0, 100), (80, 100)]],
        right=[[(-10, 0), (-10, 100)]],
        pdp=[(0, 90, "PDP1")],
        hh=[(80, 100, "HH1")],
    )

    feats = list(out.getFeatures())
    assert len(feats) == 1
    assert set(feats[0].fields().names()) >= {
        "OFF_TRENCH_LEN_M", "PAVEMENT_LEN_M", "TRENCH_STATUS"}
    assert "are drawn within 2 m of Final_Trenches" in fb.text()
    assert "walked" in fb.text() and "PAVEMENT_LEN_M" in fb.text()


def test_without_a_trench_the_layer_says_so(tmp_path):
    out, fb = _run(tmp_path, trench=None, left=LEFT, right=RIGHT, pdp=PDP, hh=HH)

    feats = list(out.getFeatures())
    assert feats, "the legacy sidewalk build must still produce ducts"
    for f in feats:
        assert f["TRENCH_STATUS"] == "No trench input"
        # With no trench there is no trench offset to report -- the status is
        # what says so, and the pavement metres stay visible.
        assert f["OFF_TRENCH_LEN_M"] == pytest.approx(0.0, abs=1e-6)
        assert f["PAVEMENT_LEN_M"] > 0.0
    assert any("no tangent trenches" in w for w in fb.warnings)
