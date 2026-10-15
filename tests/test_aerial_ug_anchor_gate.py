"""An aerial drop may only leave the ground where the UG network reaches.

An aerial leg replaces a dug drop, so the fibre still has to *reach the pole*:
``pole_layer`` plants its first pole at station 0 of the leg and
``aerial_drop_planner`` hangs the span on the nearest eligible pole. Neither
stage can lay conduit, so if the leg's network end is not on the network the
pole stands in mid-air and the premise is left with an overhead span and no path
back to its PDP — a detached premise that still bills as served.

That is not hypothetical. The drop legs are cut from the mains **before**
``trim_unserved_tails`` trims them back, so a leg attached to a run the trim
later removed keeps its geometry and loses its landing. On project
``24cd3573`` (Berlin) 19 of 60 aerial legs started off the network, 7 of them
25-45 m away, and every one was anchored anyway.

``_demote_unanchored_aerial_legs`` is the gate, and it runs on the FINAL run
set — the last point at which the leg can still be put back underground.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_aerial_ug_anchor_gate.py
"""

from __future__ import annotations

from HLDPlanning.design import trench_design as td

# A 100 m mains run along y = 0. Everything is in metres, in the designer's own
# projected CRS, because the gate is a distance test.
MAINS = td.Run(coords=[(0.0, 0.0), (100.0, 0.0)], tier="Distribution",
               src="street-graph")


def _leg(start, addr="A1", **extra):
    """A drop leg running from ``start`` out to the premise it serves."""
    leg = {
        "coords": [start, (start[0], start[1] + 30.0)],
        "type": "Aerial",
        "house": {"ADDR_ID": addr, "households": 1, "POLYGON_ID": "P1"},
        "length": 30.0,
    }
    leg.update(extra)
    return leg


def _gate(legs, runs=None, poles=(), params=None):
    logged = []
    kept, demoted = td._demote_unanchored_aerial_legs(
        legs, runs if runs is not None else [MAINS],
        list(poles), params or td.Params(), logged.append)
    return kept, demoted, logged


def test_a_leg_leaving_the_ground_on_the_network_stays_aerial():
    kept, demoted, _ = _gate([_leg((40.0, 0.0))])
    assert len(kept) == 1
    assert demoted == []
    assert kept[0]["ug_anchor"] == "on-network"


def test_a_leg_whose_landing_the_trim_removed_goes_back_underground():
    # The leg was cut from a mains run that ``trim_unserved_tails`` has since
    # removed: 20 m adrift, so there is nothing for the pole to stand on.
    kept, demoted, logged = _gate([_leg((40.0, 20.0))])
    assert kept == []
    assert len(demoted) == 1
    assert demoted[0]["aerial_fallback"] == "no_ug_anchor"
    # The demotion is a finding, not a silent reassignment.
    assert logged and "1 of 1" in logged[0]


def test_an_existing_pole_standing_on_the_network_keeps_the_leg_aerial():
    # The docx's own "existing telecom pole within 50 m" condition, and the feed
    # that makes it usable: the pole is planted ON the network.
    poles = [{"x": 40.0, "y": 0.0, "POLE_ID": "PL-0001"}]
    kept, demoted, _ = _gate([_leg((40.0, 20.0))], poles=poles)
    assert demoted == []
    assert kept[0]["ug_anchor"] == "existing-pole-on-network"


def test_an_existing_pole_off_the_network_does_not_count():
    # A pole 20 m off the mains is no better than no pole: it still cannot be
    # fed, so the leg falls back to trench.
    poles = [{"x": 40.0, "y": 20.0, "POLE_ID": "PL-0001"}]
    kept, demoted, _ = _gate([_leg((40.0, 20.0))], poles=poles)
    assert kept == []
    assert len(demoted) == 1


def test_a_chain_leg_inherits_its_parents_anchor():
    # A house-to-house span leaves a BUILDING, not the ground, so its feed is
    # its parent's aerial span: anchored parent => the chain leg is anchored.
    parent = _leg((40.0, 0.0), addr="A1")
    child = _leg((60.0, 15.0), addr="A2", aerial_reason="chain",
                 parent_addr="A1")
    kept, demoted, _ = _gate([parent, child])
    assert demoted == []
    assert [leg["ug_anchor"] for leg in kept] == [
        "on-network", "chain-from-anchored-parent"]


def test_a_chain_leg_whose_parent_was_demoted_is_demoted_too():
    # The parent has no landing, so the child hanging off it has none either —
    # the whole chain goes underground rather than leaving the far house
    # overhead with nothing under it.
    parent = _leg((40.0, 20.0), addr="A1")
    child = _leg((60.0, 15.0), addr="A2", aerial_reason="chain",
                 parent_addr="A1")
    kept, demoted, _ = _gate([parent, child])
    assert kept == []
    assert [leg["house"]["ADDR_ID"] for leg in demoted] == ["A1", "A2"]


def test_no_aerial_legs_is_a_no_op():
    kept, demoted, logged = _gate([])
    assert kept == [] and demoted == [] and logged == []
