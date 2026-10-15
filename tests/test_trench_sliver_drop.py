"""Junction slivers must not become trench rows (duplicate-row regression).

The trench-design consolidation used to emit every main-corridor fragment
as its own row, including residue of a few millimetres left where two runs
meet. Those rows are not a trench: they cannot hold a chamber, a duct or a
cable, and they inflate the trench count. Main-corridor pieces under
TRENCH_SLIVER_M are dropped. Real parallel trenches (a couple of metres
apart) and garden legs are kept.

Runs under the QGIS interpreter, like the rest of the suite:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest
from qgis.core import QgsGeometry

from HLDPlanning.algorithms.trench_design_layer import (
    TRENCH_SLIVER_M,
    TrenchDesignLayerAlgorithm,
)

pytestmark = pytest.mark.usefixtures("qgis_app")


def _row(wkt, tier="Open Cut"):
    return {
        "_geom": QgsGeometry.fromWkt(wkt),
        "TRENCH_TIER": tier,
        "trench_type": "Open Cut",
        "MFG_ID": "MFG-1",
        "PDP_ID": "PDP-1",
    }


def test_sliver_threshold_is_five_centimetres():
    assert TRENCH_SLIVER_M == 0.05


def test_main_corridor_sliver_is_dropped_and_parallel_trench_kept():
    main = _row("LineString(0 0, 10 0)")
    sliver = _row("LineString(10 0, 10.01 0)")  # 0.01 m junction residue
    parallel = _row("LineString(0 2, 10 2)")    # 2 m away: a real trench

    out = TrenchDesignLayerAlgorithm._consolidate_physical_corridors(
        [main, sliver, parallel], None)

    lengths = sorted(round(r["_geom"].length(), 2) for r in out)
    assert len(out) == 2
    assert lengths == [10.0, 10.0]


def test_garden_sliver_is_not_dropped():
    garden = _row("LineString(0 0, 0.01 0)", tier="Garden")
    out = TrenchDesignLayerAlgorithm._consolidate_physical_corridors(
        [garden], None)
    assert len(out) == 1
