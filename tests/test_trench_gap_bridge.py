"""A duct may bridge a short gap between two trench features, and only that.

The published trench network sometimes has a ~7 m hole between a region's
trench island and the main trench. No connected chain joins them, so the
duct link returned None and the pseudo point was left unconnected. The link
search now falls back to one straight connector of at most
DUCT_TRENCH_BRIDGE_M. A longer gap still returns None, and a real chain is
always preferred over a bridge.

Pure coordinates; runs under the QGIS interpreter like the rest of the suite.
"""

from __future__ import annotations

from HLDPlanning.utils.attr_enrich import (
    DUCT_TRENCH_BRIDGE_M,
    _trench_network_path,
)


def _parts(gap_m):
    # Two trench features on the x axis, separated by gap_m metres.
    return [
        [(0.0, 0.0), (10.0, 0.0)],
        [(10.0 + gap_m, 0.0), (30.0 + gap_m, 0.0)],
    ]


def test_bridge_limit_is_eight_metres():
    assert DUCT_TRENCH_BRIDGE_M == 8.0


def test_seven_metre_gap_is_bridged_when_allowed():
    path = _trench_network_path(
        _parts(7.0), 0.0, 0.0, 37.0, 0.0,
        bridge_m=DUCT_TRENCH_BRIDGE_M,
    )
    assert path is not None
    assert path[0] == (0.0, 0.0)
    assert path[-1] == (37.0, 0.0)


def test_seven_metre_gap_is_not_bridged_without_permission():
    path = _trench_network_path(
        _parts(7.0), 0.0, 0.0, 37.0, 0.0,
    )
    assert path is None


def test_gap_longer_than_the_limit_is_not_bridged():
    path = _trench_network_path(
        _parts(12.0), 0.0, 0.0, 42.0, 0.0,
        bridge_m=DUCT_TRENCH_BRIDGE_M,
    )
    assert path is None


def test_connected_chain_is_used_without_a_bridge():
    # Two features that share an end: the real chain is used, no connector.
    parts = [
        [(0.0, 0.0), (10.0, 0.0)],
        [(10.0, 0.0), (20.0, 0.0)],
    ]
    path = _trench_network_path(
        parts, 0.0, 0.0, 20.0, 0.0, bridge_m=DUCT_TRENCH_BRIDGE_M,
    )
    assert path is not None
    assert all(abs(y) < 1e-9 for _x, y in path)
    assert len(path) <= 4
