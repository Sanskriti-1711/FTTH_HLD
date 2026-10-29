"""CBT-node assignment in the network stage.

A building takes the CBT_ID of the CBT node nearest to it, within its own
polygon. The helper below is the one the Final_Object_Layer step calls.

Runs under the QGIS interpreter, like the rest of the suite:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest
from qgis.core import QgsGeometry, QgsPointXY

pytestmark = pytest.mark.usefixtures("qgis_app")


def _pt(x, y):
    return QgsGeometry.fromPointXY(QgsPointXY(x, y))


def test_building_takes_nearest_cbt_node():
    from HLDPlanning.algorithms.network_layer import _nearest_cbt_node

    nodes = [("CBT00001", _pt(0, 0)), ("CBT00002", _pt(100, 0))]
    assert _nearest_cbt_node(_pt(10, 5), nodes) == "CBT00001"
    assert _nearest_cbt_node(_pt(90, -5), nodes) == "CBT00002"


def test_no_cbt_nodes_gives_none():
    from HLDPlanning.algorithms.network_layer import _nearest_cbt_node

    assert _nearest_cbt_node(_pt(10, 5), []) is None


def test_network_algorithm_declares_optional_cbt_input_and_output():
    from HLDPlanning.algorithms.network_layer import NetworkLayerAlgorithm

    alg = NetworkLayerAlgorithm()
    alg.initAlgorithm()
    names = {p.name() for p in alg.parameterDefinitions()}
    assert NetworkLayerAlgorithm.INPUT_CBT_AREAS in names
    assert NetworkLayerAlgorithm.OUT_CBT_NODES in names
    cbt_in = alg.parameterDefinition(NetworkLayerAlgorithm.INPUT_CBT_AREAS)
    assert cbt_in.flags() & cbt_in.FlagOptional
