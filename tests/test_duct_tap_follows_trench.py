"""Duct taps must follow the trench, not cut the corner (rule D10).

A distribution duct has to physically reach every pseudo-HH/coupler (rule D9),
and everything in the design has to lie ON the trench network (rule D10). The
first implementation of the tap reached the coupler with a *straight chord*,
which is why the published ducts stopped matching the trench: measured on the
2026-09-21 Berlin run, 2,076 m of the distribution layer (21 % of its length)
was single-segment chords up to 123 m long lying 1-21 m off the network.

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
    """An in-memory line layer holding one feature per coordinate list."""
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


def _point_layer(points, name):
    lyr = QgsVectorLayer(f"Point?crs={CRS_AUTHID}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("POLYGON_ID", QMetaType.Type.QString)])
    lyr.updateFields()
    feats = []
    for i, (x, y) in enumerate(points):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f["POLYGON_ID"] = ""
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


# An L-shaped trench: east along y=0, then north up x=100.
L_TRENCH = [(0, 0), (100, 0), (100, 100)]


def _max_offset(geom, trench_geom):
    return max((QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y()))
                .distance(trench_geom)) for p in geom.vertices())


def test_trench_link_follows_the_corner_instead_of_cutting_it():
    """Between two points on different arms, the link is the trench, not the chord."""
    algo = DuctLayer()
    trench = _line_layer([L_TRENCH], "trench")

    link = algo._trench_link(trench, (50, 0), (100, 50))

    assert link is not None and not link.isEmpty()
    # Around the corner: 50 m east + 50 m north. The straight chord would be
    # 70.71 m — so asserting the trench length proves the corner was followed.
    assert link.length() == pytest.approx(100.0, abs=1e-6)
    assert _max_offset(link, trench.getFeature(1).geometry()) == pytest.approx(0.0, abs=1e-9)


def test_trench_link_is_none_when_no_single_trench_carries_both():
    """Two different trenches → no single-feature link (the route handles it)."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (50, 0)], [(100, 0), (100, 100)]], "trench")

    assert algo._trench_link(trench, (50, 0), (100, 50)) is None


def test_trench_route_crosses_features_at_a_crossing():
    """Ends on two different trenches: the route follows both, through the node."""
    algo = DuctLayer()
    # A road and a crossing street, published as two separate features.
    trench = _line_layer([[(0, 0), (100, 0)], [(50, -50), (50, 50)]], "trench")

    route = algo._trench_route(trench, (10, 0), (50, 40))

    assert route is not None and not route.isEmpty()
    # (10,0) -> crossing (50,0) -> (50,40). The straight chord would be 56.57 m.
    assert route.length() == pytest.approx(80.0, abs=1e-6)
    ref = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    assert _max_offset(route, ref) == pytest.approx(0.0, abs=1e-9)


def test_trench_route_is_none_when_the_network_is_broken():
    """No path between the two features → None, so the caller reports a fallback."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(0, 500), (100, 500)]], "trench")

    assert algo._trench_route(trench, (10, 0), (50, 500)) is None


def test_trench_route_refuses_an_absurd_detour():
    """A path that carries both the long way round is not the path between them."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (0, 1000), (20, 1000), (20, 0), (10, 0)]], "trench")

    assert algo._trench_route(trench, (0, 0), (10, 0)) is None


def test_trench_connector_prefers_the_route_over_a_single_feature():
    """The connector routes across features — it needs no single one to carry both."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(50, -50), (50, 50)]], "trench")

    route = algo._trench_connector(trench, (10, 0), (50, 40))

    assert route is not None
    assert route.length() == pytest.approx(80.0, abs=1e-6)


def test_attach_taps_reaches_a_coupler_on_another_trench():
    """The duct turns at the crossing and still lands exactly on the coupler."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(50, -50), (50, 50)]], "trench")
    taps = _point_layer([(50, 40)], "pseudo")
    fb = _Fb()

    duct = QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(10, 0)])
    out = algo._attach_taps(duct, taps, [], 0.5, fb, corridor_lyr=trench)

    assert out.distance(
        QgsGeometry.fromPointXY(QgsPointXY(50, 40))) == pytest.approx(0.0, abs=1e-6)
    ref = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    assert _max_offset(out, ref) == pytest.approx(0.0, abs=1e-9)
    assert out.length() == pytest.approx(10.0 + 80.0, abs=1e-6)
    assert "straight connector" not in fb.text()


def test_attach_taps_reaches_the_coupler_along_the_trench():
    """The T-junction case: the tap goes round the corner, and stays on it."""
    algo = DuctLayer()
    trench = _line_layer([L_TRENCH], "trench")
    taps = _point_layer([(100, 50)], "pseudo")
    fb = _Fb()

    duct = QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(50, 0)])
    out = algo._attach_taps(duct, taps, [], 0.5, fb, corridor_lyr=trench)

    # It reaches the coupler...
    assert out.distance(QgsGeometry.fromPointXY(QgsPointXY(100, 50))) == pytest.approx(0.0, abs=1e-6)
    # ...entirely on the trench (a straight chord would sit 35.4 m off it)...
    assert _max_offset(out, trench.getFeature(1).geometry()) == pytest.approx(0.0, abs=1e-9)
    # ...and the duct material grew by the trench path, not by the chord.
    assert out.length() == pytest.approx(50.0 + 100.0, abs=1e-6)
    assert "follow the trench" in fb.text()
    assert "straight connector" not in fb.text()


def test_attach_taps_falls_back_and_says_so_when_no_trench_reaches():
    """A coupler no single trench connects still gets a connector — reported."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (50, 0)]], "trench")
    taps = _point_layer([(300, 300)], "pseudo")
    fb = _Fb()

    duct = QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(50, 0)])
    out = algo._attach_taps(duct, taps, [], 0.5, fb, corridor_lyr=trench)

    assert out.distance(QgsGeometry.fromPointXY(QgsPointXY(300, 300))) == pytest.approx(0.0, abs=1e-6)
    assert "straight connector" in fb.text()


# ── the published trench has to arrive as ONE network ─────────────────────────
# The union gives a junction a vertex in the span that was split, but the span
# running THROUGH that junction keeps none — so a graph built on coincident
# vertices alone is severed exactly where the drawing shows a join. On the
# 2026-09-21 Berlin run that hid 109 junctions and left one network looking like
# 93; docking the vertices took the graph to 1 part and the distribution taps'
# unroutable chords from 19 to 3.

def test_a_leg_touching_a_passing_span_is_reachable():
    """A stub whose end sits 0.05 m off the middle of a spine joins it."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(50, 0.05), (50, 60)]], "trench")
    route = algo._trench_route(trench, (10.0, 0.0), (50.0, 60.0))
    assert route is not None and not route.isEmpty()
    # ...and it rides the trench instead of crossing the field between the ends
    both = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    assert _max_offset(route, both) <= 0.06


def test_docking_is_what_joins_that_leg():
    """Pin the cause — this must not get 'fixed' by widening a gap later."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(50, 0.05), (50, 60)]], "trench")
    keep = DuctLayer.ROUTE_DOCK_TOL_M
    try:
        DuctLayer.ROUTE_DOCK_TOL_M = 0.0
        algo._net_cache = {}
        assert algo._trench_route(trench, (10.0, 0.0), (50.0, 60.0)) is None
    finally:
        DuctLayer.ROUTE_DOCK_TOL_M = keep


def test_docking_does_not_bridge_a_real_gap():
    """A stub 12 m off the spine stays off it: a gap is not drafting slop."""
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (100, 0)], [(50, 12), (50, 60)]], "trench")
    assert algo._trench_route(trench, (10.0, 0.0), (50.0, 60.0)) is None


def test_a_detour_longer_than_the_path_between_the_points_is_refused():
    """The cap exists because a 7.5 m tap measured a 193 m network route.

    Drawing that detour puts duct on the trench but invents 185 m of duct to
    save 7.5 m of chord, so the route is refused and the caller falls back.
    """
    algo = DuctLayer()
    trench = _line_layer([[(0, 0), (1000, 0)],
                          [(0, 5), (1000, 5)],
                          [(1000, 0), (1000, 5)]], "trench")
    # 5 m apart, and the only way round is 1,005 m
    assert algo._trench_route(trench, (0.0, 0.0), (0.0, 5.0)) is None
    # a genuine along-trench hop is still routed
    hop = algo._trench_route(trench, (0.0, 0.0), (10.0, 0.0))
    assert hop is not None and hop.length() == pytest.approx(10.0, abs=0.05)


def test_attach_taps_without_a_corridor_keeps_the_old_behaviour():
    """No trench layer supplied → the straight spur, as before."""
    algo = DuctLayer()
    taps = _point_layer([(0, 100)], "pseudo")
    fb = _Fb()

    duct = QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(50, 0)])
    out = algo._attach_taps(duct, taps, [], 0.5, fb)

    assert out.distance(QgsGeometry.fromPointXY(QgsPointXY(0, 100))) == pytest.approx(0.0, abs=1e-6)
    assert out.length() == pytest.approx(50.0 + 100.0, abs=1e-6)
    assert "straight connector" in fb.text()
