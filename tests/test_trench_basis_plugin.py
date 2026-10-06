"""The trench-basis rules that read the QGIS plugin algorithms.

`web/backend/tests/test_trench_basis.py` holds the rest of this rule set: the
`trench_design` half, which needs only GDAL and networkx and so runs on the
backend's Anaconda interpreter. The rules below read `trench_layer` and
`network_layer` instead, and those import `qgis.core` — a compiled module built
for CPython 3.12. Importing them under Anaconda fails outright, which is why
they live here: `HLD_Planning_01/tools/run_all_tests.cmd` runs this directory
under QGIS's own interpreter, so these assertions execute there instead of
taking the whole backend collection down with them (they ran nowhere at all
while they sat next to the `trench_design` tests).

What is pinned here:

* the PDP sits on the SAME sidewalk the trench is built on (the network stage
  sampled candidates 8 m off the centreline while the trench was offset 3 m);
* the trench stage's road filter is generated from the declared class policy, so
  a never-class cannot be let in by a second hand-maintained list;
* the trench band, the kerb the cabinet sits at and the pavement the engine
  models are one distance in all three codebases that lay it.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest

from HLDPlanning.algorithms import network_layer as nl
from HLDPlanning.algorithms import trench_layer as tl
from HLDPlanning.design import trench_design as td

pytestmark = pytest.mark.usefixtures("qgis_app")


# ── the shared sidewalk: the PDP is where the trench is ─────────────────────

def test_the_pdp_and_the_trench_agree_on_where_the_sidewalk_is():
    """These two constants are the same physical distance and drifted apart once:
    the trench at 3 m and the PDP at 8 m put cabinets inside the blocks."""
    assert nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK == tl.SIDEWALK_OFFSET_M


def test_the_kerb_band_is_the_kerb_the_cabinet_sits_at():
    """A splitter is a street cabinet: laying the trench KERB_OFFSET_M out from
    the centreline is what leaves the cabinet beside its own trench instead of
    mid-road or 5 m inside the block."""
    assert td.KERB_OFFSET_M == nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK


def test_the_pavement_band_the_kerb_and_the_plugin_are_one_rule():
    """The plugin half of "three codebases lay the band".

    The backend half — `osm_source`'s duplicated width table against
    `trench_design`'s — is in the sibling file, which cannot import these two
    modules.
    """
    assert (td.KERB_OFFSET_M == nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK
            == tl.SIDEWALK_OFFSET_M)


# ── the road filter is generated from the policy ────────────────────────────

def test_the_road_filter_excludes_every_never_class():
    expr = tl.trench_road_filter_expr()
    assert "motorway" not in expr, "a motorway must never be dug along"
    for c in tl.TRENCH_NEVER_CLASSES:
        assert "'%s'" % c not in expr, c


def test_the_road_filter_allows_every_allowed_class():
    expr = tl.trench_road_filter_expr()
    for c in tl.TRENCH_ROAD_CLASSES:
        assert '"fclass"=\'%s\'' % c in expr, c


def test_the_trench_class_policy_is_disjoint():
    assert not [c for c in tl.TRENCH_NEVER_CLASSES if c in tl.TRENCH_ROAD_CLASSES]
    assert "motorway" in tl.TRENCH_NEVER_CLASSES
    assert "motorway" not in tl.TRENCH_ROAD_CLASSES


def test_the_road_filter_keeps_bridges_and_tunnels_out():
    expr = tl.trench_road_filter_expr()
    assert "bridge" in expr and "tunnel" in expr
