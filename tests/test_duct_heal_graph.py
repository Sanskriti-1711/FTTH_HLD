"""`_heal_graph` join rules: exact on a normal ward, bounded on a dense cell.

A cell is 3 m wide, so every pair of nodes inside it is a candidate and the pass
costs n^2 for that cell. A full ward averages ~5 nodes per cell and heals in
seconds (measured: 160,000 nodes / 31,500 cells in 4.5 s), but coincident
vertices -- parallel duplicate rows, stacked weld points -- put thousands in one
cell: measured 300 nodes 0.14 s, 1,200 3.8 s, 3,000 24 s. Run b95f3713
(2026-11-03) advanced 16 cells in 341 s while adding 20 links, so those cells
held thousands of nodes that were ALREADY joined and the pass paid n^2 to learn
it.

Above `_HEAL_DENSE_NODES` a cell is healed to CONNECTED instead of complete:
each node takes one link to a neighbour it is not already joined to. Both rules
are pinned here.

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_duct_heal_graph.py
"""

from __future__ import annotations

import math
import random
import time

import networkx as nx
import pytest

from HLDPlanning.algorithms.duct_layer import (
    _HEAL_DENSE_NODES,
    AlgDistributionDucts,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

HEAL_M = 3.0


def _brute_force(nodes, heal_m=HEAL_M):
    want = set()
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            d = math.hypot(a[0] - b[0], a[1] - b[1])
            if 0 < d <= heal_m:
                want.add(frozenset((a, b)))
    return want


def _edges(G):
    return {frozenset(e) for e in G.edges()}


def _sparse_graph():
    """A jog across several cells plus one pair far apart: no dense cell here."""
    nodes = [
        (0.0, 0.0), (0.4, 0.2), (1.9, 1.1), (2.6, 2.6),
        (3.2, 0.1), (4.8, 1.4), (6.1, 2.9), (7.4, 0.3),
        (12.0, 12.0),
    ]
    G = nx.Graph()
    G.add_nodes_from(nodes)
    return G, nodes


def test_a_normal_cell_gets_exactly_the_pairs_within_the_tolerance():
    G, nodes = _sparse_graph()

    healed = AlgDistributionDucts._heal_graph(G, HEAL_M)

    assert _edges(G) == _brute_force(nodes)
    assert healed == len(_brute_force(nodes))
    assert frozenset({(0.0, 0.0), (12.0, 12.0)}) not in _edges(G)


def test_existing_edges_are_kept_and_not_recounted():
    nodes = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    G = nx.Graph()
    G.add_nodes_from(nodes)
    G.add_edge(nodes[0], nodes[1], weight=1.0, length=1.0, kind="link")

    healed = AlgDistributionDucts._heal_graph(G, HEAL_M)

    assert _edges(G) == _brute_force(nodes)
    assert healed == len(_brute_force(nodes)) - 1


def _dense_cluster(n, connected=True, seed=5):
    """n nodes packed inside ONE 3 m cell."""
    rnd = random.Random(seed)
    pts = [(round(rnd.uniform(0, 0.5), 4), round(rnd.uniform(0, 0.5), 4))
           for _ in range(n)]
    G = nx.Graph()
    G.add_nodes_from(pts)
    if connected:
        for i, a in enumerate(pts):
            for b in pts[i + 1:]:
                d = math.hypot(a[0] - b[0], a[1] - b[1])
                if 0 < d <= HEAL_M:
                    G.add_edge(a, b, weight=d, length=d, kind="trench")
    else:
        for a, b in zip(pts, pts[1:]):
            G.add_edge(a, b, weight=0.05, length=0.05, kind="trench")
    return G


def test_a_dense_cell_is_healed_in_a_bounded_time():
    """The shape that stalled run b95f3713: thousands of nodes in one cell."""
    n = _HEAL_DENSE_NODES * 8
    G = _dense_cluster(n, connected=True)
    assert len(G.nodes) >= _HEAL_DENSE_NODES

    started = time.time()
    AlgDistributionDucts._heal_graph(G, HEAL_M)
    elapsed = time.time() - started

    assert elapsed < 5.0, f"dense cell took {elapsed:.1f} s"


def test_a_dense_cell_is_left_connected_rather_than_complete():
    """Healing needs the cell joined; it does not need every pair of it."""
    # A 20 x 20 lattice at 0.1 m inside one cell, with no edges at all: 400
    # isolated nodes, every one within 3 m of every other.
    side = 20
    nodes = [(i * 0.1, j * 0.1) for i in range(side) for j in range(side)]
    G = nx.Graph()
    G.add_nodes_from(nodes)
    assert nx.number_connected_components(G) == len(nodes)

    healed = AlgDistributionDucts._heal_graph(G, HEAL_M)

    assert nx.number_connected_components(G) == 1
    assert healed > 0
    # Connected, not complete: about a link per node, not n^2/2 of them (the
    # sub-cell spine and the join add a bounded handful on top).
    assert healed <= len(nodes) + 64
    for a, b in G.edges():
        assert math.hypot(a[0] - b[0], a[1] - b[1]) <= HEAL_M


def test_a_dense_cell_adds_nothing_when_everything_is_already_joined():
    G = _dense_cluster(_HEAL_DENSE_NODES * 2, connected=True)
    edges_before = G.number_of_edges()

    healed = AlgDistributionDucts._heal_graph(G, HEAL_M)

    assert healed == 0
    assert G.number_of_edges() == edges_before
