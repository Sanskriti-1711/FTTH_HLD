"""The distribution spine stays inside its PDP's own polygon.

``design_spine`` used to route each PDP over the whole street network. When the
shortest path to a house crossed into a neighbouring polygon, the published
duct was clipped back to its own region afterwards, which left the region's
pseudo points (its garden-leg ends) with no duct to reach. With ``regions``,
the tree is routed over the nodes inside the PDP's own polygon first, and only
a house unreachable there falls back to the whole network.

Run under the QGIS interpreter (see tests/conftest.py):

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import networkx as nx
import pytest
from osgeo import ogr

from HLDPlanning.design import trench_design as td

pytestmark = pytest.mark.usefixtures("qgis_app")


def _square(x0, y0, x1, y1):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


class _Stub:
    """Just the StreetGraph surface ``design_spine`` and ``_snap_anchor`` use."""

    def __init__(self, node_xy, edges):
        self.node_xy = node_xy
        self.G = nx.Graph()
        for n in node_xy:
            self.G.add_node(n)
        for a, b, w in edges:
            self.G.add_edge(a, b, weight=w)
        self.main_component = set()

    def nearest_node(self, x, y, tol):
        return self._near(x, y, tol)

    def nearest_main_node(self, x, y, tol):
        return self._near(x, y, tol)

    def _near(self, x, y, tol):
        best, key = tol, None
        for n, (nx_, ny) in self.node_xy.items():
            d = ((nx_ - x) ** 2 + (ny - y) ** 2) ** 0.5
            if d <= best:
                best, key = d, n
        return key


# Region A is the square 0..50 x 0..50. The PDP and house sit inside it.
# The in-region route P-A1-A2-H is long (3000); the shortcut via B is 2.
NODES = {
    "P": (0.0, 0.0), "A1": (0.0, 40.0), "A2": (40.0, 40.0), "H": (40.0, 0.0),
    "B1": (20.0, 80.0),
}
IN_A_EDGES = [("P", "A1", 1000.0), ("A1", "A2", 1000.0), ("A2", "H", 1000.0)]
SHORTCUT = [("P", "B1", 1.0), ("B1", "H", 1.0)]


def _run(sg, regions):
    pdp = {"PDP_ID": "PDP1", "x": 0.0, "y": 0.0, "POLYGON_ID": "A"}
    house = {"PDP_ID": "PDP1", "x": 40.0, "y": 0.0, "POLYGON_ID": "A"}
    params = td.Params()
    per_pdp, _edge_houses = td.design_spine(
        sg, [pdp], [house], params, lambda _m: None, regions=regions)
    return per_pdp["PDP1"]


def _nodes_of(edges):
    return {n for e in edges for n in e}


def test_spine_takes_the_in_region_route_over_the_shortcut():
    sg = _Stub(NODES, IN_A_EDGES + SHORTCUT)
    regions = {"A": _square(0, 0, 50, 50)}
    edges = _run(sg, regions)
    assert _nodes_of(edges) <= {"P", "A1", "A2", "H"}, edges


def test_without_regions_the_spine_crosses_into_the_neighbour():
    sg = _Stub(NODES, IN_A_EDGES + SHORTCUT)
    edges = _run(sg, None)
    assert "B1" in _nodes_of(edges), edges


def test_house_with_no_in_region_route_falls_back_to_the_network():
    # Only the shortcut connects the house: it must still be served.
    sg = _Stub(NODES, SHORTCUT)
    regions = {"A": _square(0, 0, 50, 50)}
    edges = _run(sg, regions)
    assert "B1" in _nodes_of(edges), edges
    assert ("B1", "H") in edges or ("H", "B1") in edges
