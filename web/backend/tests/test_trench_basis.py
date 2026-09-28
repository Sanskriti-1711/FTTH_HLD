"""Tests for the trench basis rules: kerb, motorway, and the shared sidewalk.

Four operator rules, each of which used to be an implicit consequence of some
other constant:

* the street graph keys its nodes on the ROAD geometry, never on a derived
  offset — the junctions depend on OSM's shared vertices, and keying on offset
  coordinates fragmented the whole network (see the test for the numbers);
* a MOTORWAY is never carried and never open-cut — it can only be crossed, and
  every crossing is a drill (HDD);
* the PDP sits on the SAME sidewalk the trench is built on (the network stage
  sampled candidates 8 m off the centreline while the trench was offset 3 m,
  which is what put splitter cabinets inside residential blocks);
* the trench stage's road filter is generated from the declared class policy, so
  a never-class cannot be let in by a second hand-maintained list;
* a trench is NEVER drawn down the middle of a carriageway: where the only line
  along a street is its centreline, the published geometry is laid on the KERB
  band, ramping back to the exact OSM vertex at every end and junction.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_trench_basis.py -v
"""

import math
import pathlib
import sys

import pytest

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.algorithms import network_layer as nl  # noqa: E402
from HLDPlanning.algorithms import trench_layer as tl  # noqa: E402
from HLDPlanning.design import trench_design as td  # noqa: E402


# ── the graph keeps its junction topology ───────────────────────────────────
#
# A street-carried run IS published on the road centrelines (down a carriageway
# where no footway is mapped — 8.1 % of the carrier length on the reference
# project). Two attempts to shift that geometry to the kerb were reverted; the
# notes in build_street_graph record why, and the test below pins the invariant
# that made the first one fatal, so it cannot be re-introduced by accident.

def test_graph_nodes_stay_on_the_original_centreline_coordinates():
    """Node keys come from the ROAD geometry, never from a derived offset.

    Offsetting carriageway edges to their kerb before keying the nodes looked
    like a local improvement and was a disaster: OSM lets lines share exact
    vertices at intersections, so keying on offset coordinates fragments the
    graph at every junction on a carriageway. On the reference project that cost
    6 components, 13 unreachable PDPs behind 392 m spurs and the MFG reaching 18
    of 31. This pins the invariant so it cannot be re-introduced.
    """
    p = td.Params()
    walkable = [([(0.0, 0.0), (100.0, 0.0)], "residential"),
                ([(100.0, 0.0), (100.0, 100.0)], "footway")]
    sg = td.build_street_graph(walkable, p)
    # The two parts meet at (100, 0). Keying on the ORIGINAL geometry makes that
    # one node and one component; an offset key would split it in two.
    assert len(sg.main_component) == sg.G.number_of_nodes()
    assert sum(1 for x, y in sg.node_xy.values()
               if abs(x - 100.0) < 0.25 and abs(y) < 0.25) == 1
    # every node lies on one of the two original lines (y == 0 or x == 100)
    for x, y in sg.node_xy.values():
        assert abs(y) < 0.25 or abs(x - 100.0) < 0.25, (x, y)


# ── the motorway: never a carrier, never open-cut ───────────────────────────

def test_motorway_is_never_a_carrier():
    assert td._class_factor("motorway") == math.inf
    assert td._class_factor("motorway_link") == math.inf


def test_an_ordinary_street_stays_routable():
    """The rule is about motorways: a residential street must stay finite."""
    assert math.isfinite(td._class_factor("residential"))
    assert math.isfinite(td._class_factor("service"))
    assert math.isfinite(td._class_factor("footway"))


def test_motorway_is_not_in_the_graph_basis():
    """A motorway never even enters the graph the router walks."""
    assert "motorway" not in td.WALKABLE_CLASSES
    assert "motorway_link" not in td.WALKABLE_CLASSES


def test_the_carrier_lists_do_not_contradict_themselves():
    for c in td.NEVER_CARRIER_CLASSES:
        assert c not in td.PREFERRED_CARRIER_CLASSES
        assert c not in td.FALLBACK_CARRIER_CLASSES


def test_every_carriageway_carrier_has_a_width():
    """A carriageway carrier needs a width: it is the drill (HDD) length base."""
    for c in td.PREFERRED_CARRIER_CLASSES + td.FALLBACK_CARRIER_CLASSES:
        if c in td.PURE_FOOTWAY_CLASSES:
            continue
        assert c in td.VEHICULAR_WIDTH_M, c


# ── the shared sidewalk: the PDP is where the trench is ─────────────────────

def test_the_pdp_and_the_trench_agree_on_where_the_sidewalk_is():
    """These two constants are the same physical distance and drifted apart once:
    the trench at 3 m and the PDP at 8 m put cabinets inside the blocks."""
    assert nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK == tl.SIDEWALK_OFFSET_M


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


# ── the kerb band: never a trench down the middle of a carriageway ──────────

def test_the_kerb_band_is_the_kerb_the_cabinet_sits_at():
    """The trench at the kerb and the PDP at the kerb are the same distance.

    A splitter is a street cabinet: laying the trench KERB_OFFSET_M out from the
    centreline is what leaves the cabinet beside its own trench instead of
    mid-road or 5 m inside the block.
    """
    assert td.KERB_OFFSET_M == nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK


def test_the_pavement_band_and_the_kerb_band_are_one_rule():
    """Three codebases lay the band; they may not drift apart.

    The engine backend cannot import plugin code, so osm_source duplicates the
    carriageway width table. This pins the rules equal and the tables identical
    — a trench, its derived pavement and its splitter cabinet sit on one band.
    """
    import osm_source
    assert osm_source.PAVEMENT_FOOTWAY_INSET_M == td.KERB_FOOTWAY_INSET_M
    assert osm_source.PAVEMENT_WIDTH_M == td.VEHICULAR_WIDTH_M
    for cls in td.VEHICULAR_WIDTH_M:
        assert osm_source.pavement_offset_for(cls) == td.kerb_offset_for(cls)
    assert td.KERB_OFFSET_M == osm_source.PAVEMENT_OFFSET_M
    assert (td.KERB_OFFSET_M == nl.NetworkLayerAlgorithm.DEFAULT_SIDEWALK
            == tl.SIDEWALK_OFFSET_M)


def test_a_street_with_no_pavement_is_drawn_at_the_kerb_not_the_middle():
    """The reported defect: a run down a carriageway centreline.

    One 200 m `residential` way with no footway mapped. The DRAWN geometry must
    leave the centreline and sit KERB_OFFSET_M to one side, while both OSM ends
    — which is where the junctions are — stay exactly where they were.
    """
    p = td.Params()
    sg = td.build_street_graph([([(0.0, 0.0), (200.0, 0.0)], "residential")], p)
    pts = [pt for coords in sg.edge_coords.values() for pt in coords]
    assert max(abs(y) for _, y in pts) == pytest.approx(td.KERB_OFFSET_M, abs=0.01)
    assert (0.0, 0.0) in pts and (200.0, 0.0) in pts
    # node positions still come from the road geometry, not from the kerb band
    assert all(abs(y) < 0.01 for _, y in sg.node_xy.values())
    assert sg.kerb_parts == 1
    assert sg.kerb_max_offset_m == pytest.approx(td.KERB_OFFSET_M, abs=0.01)


def test_the_kerb_band_leaves_every_junction_and_end_exactly_alone():
    coords = [(0.0, 0.0), (30.0, 0.0), (60.0, 0.0)]
    band = td._kerb_band(coords, {0, 2}, td.KERB_OFFSET_M, td.KERB_RAMP_M, 1)
    assert band[0] == (0.0, 0.0)
    assert band[2] == (60.0, 0.0)
    assert band[1][0] == pytest.approx(30.0)
    assert band[1][1] == pytest.approx(td.KERB_OFFSET_M)


def test_the_kerb_offset_flares_in_over_the_ramp():
    """A vertex 5 m from a junction is offset 5/ramp of the way, not fully."""
    coords = [(0.0, 0.0), (5.0, 0.0), (60.0, 0.0)]
    band = td._kerb_band(coords, {0, 2}, 3.0, 10.0, 1)
    assert band[1][1] == pytest.approx(1.5)


def test_the_ramp_is_the_declared_one():
    """A vertex part-way to a junction is offset by its share of the ramp.

    Scaling the ramp down on short pieces was tried and REVERTED: it pushed the
    kerb band off a piece's own ends and made the trench read worse on the map
    than leaving it on the centreline there.
    """
    band = td._kerb_band([(0.0, 0.0), (3.0, 0.0), (60.0, 0.0)], {0, 2}, 3.0, 6.0, 1)
    assert band[1][1] == pytest.approx(1.5)      # 3 m from a junction, 6 m ramp


def test_the_kerb_band_does_not_change_the_route_the_nodes_or_the_weights():
    """The claim that makes this safe for every project.

    Weights are computed from the OSM geometry and the keys from the OSM
    coordinates, so turning the kerb band on can change WHERE a run is drawn and
    nothing else: same nodes, same edges, same weights, same components.
    """
    walk = [([(0.0, 0.0), (60.0, 0.0), (60.0, 60.0)], "residential"),
            ([(60.0, 60.0), (120.0, 60.0)], "footway")]
    on = td.build_street_graph(walk, td.Params())
    off = td.build_street_graph(walk, td.Params(kerb_offset_m=0.0))
    assert on.G.number_of_nodes() == off.G.number_of_nodes()
    assert on.G.number_of_edges() == off.G.number_of_edges()
    assert dict(on.node_xy) == dict(off.node_xy)
    assert on.main_component == off.main_component
    for ek in off.edge_coords:
        assert on.G[ek[0]][ek[1]]["weight"] == off.G[ek[0]][ek[1]]["weight"]
        assert on.G[ek[0]][ek[1]]["cls"] == off.G[ek[0]][ek[1]]["cls"]


def test_two_ways_along_one_street_land_on_the_same_kerb():
    """Digitisation direction must not choose the side.

    The earlier per-way left/right rule put the two ways of one street on
    OPPOSITE kerbs, which separated rows that used to coincide (26/31 PDPs,
    2 components). The side is a function of geometry only.
    """
    p = td.Params()

    def drawn(walk):
        sg = td.build_street_graph(walk, p)
        return [pt[1] for coords in sg.edge_coords.values() for pt in coords]

    fwd = drawn([([(0.0, 0.0), (200.0, 0.0)], "residential")])
    rev = drawn([([(200.0, 0.0), (0.0, 0.0)], "residential")])
    assert min(fwd) > -0.01 and min(rev) > -0.01, (min(fwd), min(rev))
    assert max(fwd) > 2.9 and max(rev) > 2.9, (max(fwd), max(rev))


def test_the_kerb_band_follows_the_pavement_when_there_is_one():
    p = td.Params()
    walk = [([(0.0, 0.0), (200.0, 0.0)], "residential"),
            ([(0.0, 5.0), (200.0, 5.0)], "footway")]
    sg = td.build_street_graph(walk, p)
    street = [pt[1] for ek, coords in sg.edge_coords.items()
              if sg.G[ek[0]][ek[1]]["cls"] == "residential" for pt in coords]
    assert max(street) == pytest.approx(
        td.kerb_offset_for("residential"), abs=0.01), \
        "the trench must be on the pavement side, at its class band"
    assert max(street) < 4.9, "and at its own band, not on the pavement line"


def test_a_real_pavement_is_never_moved():
    """Only carriageway geometry is laid at the kerb; the pavement is the basis."""
    p = td.Params()
    walk = [([(0.0, 0.0), (200.0, 0.0)], "residential"),
            ([(0.0, 5.0), (200.0, 5.0)], "footway")]
    sg = td.build_street_graph(walk, p)
    foot = [pt for ek, coords in sg.edge_coords.items()
            if sg.G[ek[0]][ek[1]]["cls"] == "footway" for pt in coords]
    assert foot and all(abs(y - 5.0) < 0.01 for _, y in foot)


def test_the_kerb_band_is_laid_on_the_cabinet_side():
    """With no pavement mapped, the side the network's anchors are on wins."""
    p = td.Params()
    sg = td.build_street_graph([([(0.0, 0.0), (200.0, 0.0)], "residential")],
                               p, anchors=[(100.0, -3.0)])
    ys = [pt[1] for coords in sg.edge_coords.values() for pt in coords]
    assert min(ys) < -2.9


# ── pavement continuity: the route must not have to leave the pavement ──────

def _broken_pavement_walk():
    """A pavement with a 4 m break, and a residential BYPASS round it.

    The bypass is connected to both pavement ends, so it is a real alternative
    the router may take — which is what makes the route assertion meaningful.
    """
    return [([(0.0, 0.0), (96.0, 0.0)], "footway"),        # break 96 -> 100
            ([(100.0, 0.0), (200.0, 0.0)], "footway"),
            ([(0.0, 0.0), (0.0, 20.0)], "residential"),
            ([(0.0, 20.0), (200.0, 20.0)], "residential"),
            ([(200.0, 20.0), (200.0, 0.0)], "residential")]


def test_the_pavement_link_rule_is_OFF_by_default():
    """Default off, on the measurement (see Params.sidewalk_link_m).

    It bridges 718 breaks / 6 374 m on the reference project and moves the
    published trench by 0 m, because a break's loose end is a dead end the route
    never had to pass through — and with it on the duct and cable layers read
    worse. The feature is kept for projects that need it, not left switched on.
    """
    assert td.Params().sidewalk_link_m == 0.0
    sg = td.build_street_graph(_broken_pavement_walk(), td.Params())
    assert sg.sidewalk_links == 0


def test_a_broken_pavement_is_joined_across_its_gap():
    """The 49 090 breaks on the reference project are all this: loose ends."""
    sg = td.build_street_graph(_broken_pavement_walk(),
                               td.Params(sidewalk_link_m=15.0))
    assert sg.sidewalk_links == 1
    assert sg.sidewalk_link_m == pytest.approx(4.0, abs=0.1)


def test_the_route_stays_on_the_pavement_instead_of_stepping_onto_the_road():
    """The defect this closes: the router crossed the break on the carriageway.

    With the break bridged, the pavement route (4 m of link) beats the 240 m
    residential bypass by a factor of ~700, so the run never touches a street.
    """
    sg = td.build_street_graph(_broken_pavement_walk(),
                               td.Params(sidewalk_link_m=15.0))
    pa = sg.nearest_node(0.0, 0.0, 5.0)
    pb = sg.nearest_node(200.0, 0.0, 5.0)
    path = td._route(sg.G, pa, pb)
    assert path is not None
    classes = {sg.G[path[i]][path[i + 1]]["cls"] for i in range(len(path) - 1)}
    assert classes == {"footway", "sidewalk_link"}, classes
    # and the link carries the footway's own cost, so a real pavement always
    # beats it wherever one exists
    link_w = [d["weight"] for _u, _v, d in sg.G.edges(data=True)
              if d.get("cls") == "sidewalk_link"]
    assert link_w and link_w[0] == pytest.approx(4.0, abs=0.1)


def test_a_gap_beyond_the_cap_is_not_bridged():
    """A cap, not a licence to join any two loose ends on the map."""
    walk = [([(0.0, 0.0), (50.0, 0.0)], "footway"),
            ([(100.0, 0.0), (150.0, 0.0)], "footway")]      # 50 m break
    sg = td.build_street_graph(walk, td.Params(sidewalk_link_m=15.0))
    assert sg.sidewalk_links == 0
    assert sg.sidewalk_link_m == 0.0


def test_the_two_ends_of_one_pavement_are_not_a_break():
    """A U-shaped pavement must not be short-circuited across its own ends."""
    coords = [(0.0, 0.0), (50.0, 0.0), (50.0, 3.0), (0.6, 3.0)]
    sg = td.build_street_graph([(coords, "footway")],
                               td.Params(sidewalk_link_m=15.0))
    assert sg.sidewalk_links == 0


def test_the_sidewalk_links_do_not_depend_on_the_input_order():
    walk = _broken_pavement_walk()
    p = td.Params(sidewalk_link_m=15.0)
    a = td.build_street_graph(list(reversed(walk)), p)
    b = td.build_street_graph(walk, p)
    assert a.sidewalk_links == b.sidewalk_links == 1

    def link_coords(sg):
        # the two endpoints of a link are ordered by node id, and node ids follow
        # the input order — the SET of points is what must not change
        out = []
        for ek, d in sg.edge_coords.items():
            if sg.G[ek[0]][ek[1]]["cls"] != "sidewalk_link":
                continue
            out.append(tuple(sorted(tuple(round(v, 3) for v in pt) for pt in d)))
        return sorted(out)

    assert link_coords(a) == link_coords(b)


def test_the_sidewalk_link_rule_can_be_turned_off():
    sg = td.build_street_graph(_broken_pavement_walk(), td.Params())
    assert sg.sidewalk_links == 0
    assert td.Params(sidewalk_link_m=15.0).sidewalk_link_m == 15.0


def test_a_service_aisle_is_banded_too():
    """A driveway/parking aisle is still a carriageway.

    Service ways were the single biggest source of centreline-carried trench on
    the reference project (206 m of 252 m) when they were left out of the band.
    """
    assert "service" in td.KERB_CLASSES
    p = td.Params()
    sg = td.build_street_graph([([(0.0, 0.0), (200.0, 0.0)], "service")], p)
    ys = [pt[1] for coords in sg.edge_coords.values() for pt in coords]
    # banded at ITS class width (5 m service -> 3.75 m), not the base band
    assert max(abs(y) for y in ys) == pytest.approx(
        td.kerb_offset_for("service"), abs=0.01)
    assert td.kerb_offset_for("service") != td.KERB_OFFSET_M


def test_the_kerb_rule_can_be_turned_off():
    p = td.Params(kerb_offset_m=0.0)
    sg = td.build_street_graph([([(0.0, 0.0), (200.0, 0.0)], "residential")], p)
    assert all(abs(y) < 0.01 for coords in sg.edge_coords.values() for _, y in coords)
    assert sg.kerb_parts == 0
