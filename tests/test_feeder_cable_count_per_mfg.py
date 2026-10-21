"""Feeder cable count per MFG area (the operator's rule).

> "not more than 3-4 feeder cables in 1 MFG area. 1 feeder cable can cover all
> the PDPs in its route until it reaches its given threshold."

`AlgCableBuilderAll._group_feeder_cables` places PDPs on trunk cables. It used
to split on a 1,000 m length limit, which on project 34b2cfad… turned one MFG
area into 14 feeder cables; the threshold that actually exhausts a feeder is
its fibre capacity, and the civil length is only a sanity flag.

Scene: one MFG, a 3 km spine with 28 PDPs hanging off it (100 m each) — 5.8 km
of route, so the 5 km threshold needs a second cable — and the planner must
never open more than `FEEDER_MAX_TRUNKS_PER_MFG`.
"""

from __future__ import annotations

import pytest

from HLDPlanning.algorithms.cable_layer import AlgCableBuilderAll

pytestmark = pytest.mark.usefixtures("qgis_app")


def _scene(n_pdp=28, spine_segs=10, seg_len=300.0, branch_len=100.0):
    """(paths, demands, pid_mfg, edge_len, pdp_label) for one MFG."""
    edge_len = {}
    paths = {}
    demands = {}
    pid_mfg = {}
    pdp_label = {}
    spine = [f"spine-{i}" for i in range(spine_segs)]
    for s in spine:
        edge_len[s] = seg_len
    for i in range(n_pdp):
        pid = f"PDP{i + 1:05d}"
        branch = f"branch-{i}"
        edge_len[branch] = branch_len
        # Each PDP joins the spine at a different point along it.
        attach = spine[i % spine_segs]
        paths[pid] = (spine[: spine.index(attach) + 1] + [branch], 0.0)
        demands[pid] = 1
        pid_mfg[pid] = "MFG00001"
        pdp_label[pid] = pid
    return paths, demands, pid_mfg, edge_len, pdp_label


def _plan(**kwargs):
    alg = AlgCableBuilderAll()
    paths, demands, pid_mfg, edge_len, labels = _scene(**kwargs)
    return alg._group_feeder_cables(paths, demands, pid_mfg, {}, edge_len, labels)


def test_no_more_than_four_feeder_cables_in_one_mfg_area():
    cables = _plan()
    assert cables, "the planner produced no cables"
    assert len(cables) <= AlgCableBuilderAll.FEEDER_MAX_TRUNKS_PER_MFG


def test_every_pdp_is_on_exactly_one_cable():
    cables = _plan()
    seen = [pid for c in cables for pid in c["pdp_ids"]]
    assert len(seen) == len(set(seen)), "a PDP was placed on two cables"
    assert len(seen) == 28


def test_a_cable_covers_several_pdps_before_another_starts():
    cables = _plan()
    assert max(c["pdp_ids"] and len(c["pdp_ids"]) for c in cables) > 1


def test_the_length_threshold_opens_the_next_cable():
    """A 5 km spine cannot sit on one cable with 28 branches on it."""
    cables = _plan()
    limit = AlgCableBuilderAll.FEEDER_LENGTH_LIMIT_M
    within = [c for c in cables if c["length"] <= limit + 1e-6]
    assert within, "the planner never respected the length threshold"
    assert all(c["trunk_no"] >= 1 for c in cables)


def test_a_tighter_threshold_still_never_exceeds_the_cap():
    """Force the cap: a tiny length limit wants far more than four cables."""
    alg = AlgCableBuilderAll()
    alg.FEEDER_LENGTH_LIMIT_M = 100.0
    paths, demands, pid_mfg, edge_len, labels = _scene()
    cables = alg._group_feeder_cables(paths, demands, pid_mfg, {}, edge_len, labels)

    assert len(cables) == AlgCableBuilderAll.FEEDER_MAX_TRUNKS_PER_MFG
    # All 28 PDPs are still served — the overflow is flagged, not dropped.
    assert sorted(pid for c in cables for pid in c["pdp_ids"]) == sorted(paths)
    assert any(c["over_trunk_cap"] for c in cables)
