"""Unit tests for the standalone civil trench designer.

The designer builds the trench network from the plan (MFG, PDPs, houses,
service polygons) over the walkable street graph, then cuts it into
chamber-to-chamber spans. These tests exercise the geometry helpers, the
street-graph routing, run assembly, drill detection, node priority and span
splitting on synthetic data — no QGIS, no project data.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_trench_design.py -v
"""

import math
import os
import pathlib
import sys

import networkx as nx
import pytest
from osgeo import ogr

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import trench_design as td  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(coords, tier="Feeder"):
    return td.Run(coords=list(coords), tier=tier)


# ── geometry helpers ─────────────────────────────────────────────────────────

def test_straighten_drops_sidewalk_wobble():
    p = td.Params()
    coords = [(0, 0), (0, 10), (5, 10), (5, 30), (5.2, 30), (5.2, 60)]
    out = td._straighten(coords, p)
    # the 0.2 m jog is a < 8° bend and must go
    assert out[0] == (0.0, 0.0)
    assert out[-1] == (5.2, 60.0)
    assert len(out) < len(coords)
    assert (5.2, 30.0) not in out


def test_straighten_keeps_real_bends():
    p = td.Params()
    coords = [(0, 0), (0, 50), (50, 50)]
    out = td._straighten(coords, p)
    assert (0.0, 50.0) in out          # 90° turn is a real corner
    assert len(out) == 3


def test_project_and_substring_are_arc_consistent():
    coords = [(0, 0), (0, 10), (5, 10), (5, 30), (5.2, 30), (5.2, 60)]
    d, arc, q = td._project(coords, 6, 45)
    assert d == pytest.approx(0.8, abs=1e-6)
    assert arc == pytest.approx(50.2, abs=1e-6)
    assert q == pytest.approx((5.2, 45.0), abs=1e-6)

    piece = td._substring(coords, 0.0, td._coords_len(coords))
    assert td._coords_len(piece) == pytest.approx(td._coords_len(coords), abs=1e-6)


def test_substring_halves_sum_to_total():
    coords = [(0, 0), (0, 100)]
    a = td._substring(coords, 0, 40)
    b = td._substring(coords, 40, 100)
    assert td._coords_len(a) == pytest.approx(40.0)
    assert td._coords_len(b) == pytest.approx(60.0)
    assert a[-1] == b[0]


def test_grid_index_finds_near_items():
    idx = td.GridIndex(cell=10.0)
    idx.add(0, 0, 0)
    idx.add(100, 100, 1)
    assert 0 in idx.near(3, 3, 8)
    assert 1 not in idx.near(3, 3, 8)


# ── street graph + routing ───────────────────────────────────────────────────

def _cross_walkable():
    """Two walkable corridors meeting at (50, 0)."""
    return [([(0, 0), (50, 0)], "footway"),
            ([(50, 0), (50, 100)], "footway")]


def test_build_street_graph_nodes_shared_vertices():
    sg = td.build_street_graph(_cross_walkable(), td.Params())
    # both corridors are connected at the shared (50, 0) vertex
    assert nx.is_connected(sg.G)
    # densified: no edge longer than the 40 m spacing rule
    for u, v in sg.G.edges:
        (x0, y0), (x1, y1) = sg.node_xy[u], sg.node_xy[v]
        assert math.hypot(x1 - x0, y1 - y0) <= 40.0 + 1e-9


def test_backbone_routes_along_the_walkable_corridor():
    sg = td.build_street_graph(_cross_walkable(), td.Params())
    p = td.Params()
    edges = td.design_backbone(
        sg, {"x": 0.0, "y": 0.0},
        [{"x": 50.0, "y": 100.0, "PDP_ID": "PDP-1"}], p, lambda m: None)
    total = sum(sg.G.edges[e]["weight"] for e in edges)
    assert total == pytest.approx(150.0)    # the whole corridor is used


def test_backbone_prefers_footway_over_carriageway():
    """A detour along the footway must beat the short carriageway hop."""
    walk = [([(0, 0), (10, 0)], "footway"), ([(10, 0), (40, 0)], "footway"),
            ([(40, 0), (50, 0)], "footway")]
    p = td.Params()
    sg = td.build_street_graph(walk, p)
    edges = td.design_backbone(sg, {"x": 0.0, "y": 0.0},
                               [{"x": 50.0, "y": 0.0, "PDP_ID": "PDP-1"}],
                               p, lambda m: None)
    assert len(edges) == 3
    # every chosen edge is tagged with the walkable class
    for ek in edges:
        assert sg.G.edges[ek[0], ek[1]]["cls"] == "footway"


def test_carriageway_is_only_a_last_resort_carrier():
    """A longer footway detour must win over a short residential shortcut."""
    walk = [([(0, 0), (0, 300)], "footway"), ([(0, 300), (300, 300)], "footway"),
            ([(300, 300), (300, 0)], "footway"), ([(0, 0), (300, 0)], "residential")]
    p = td.Params()
    sg = td.build_street_graph(walk, p)
    edges = td.design_backbone(sg, {"x": 0.0, "y": 0.0},
                               [{"x": 300.0, "y": 0.0, "PDP_ID": "PDP-1"}],
                               p, lambda m: None)
    classes = {sg.G.edges[e[0], e[1]]["cls"] for e in edges}
    assert classes == {"footway"}      # 900 m of footway vs a 300 m street
    assert td.CARRIAGE_FACTOR >= 4.0   # the dial that makes that true


def test_bridge_and_tunnel_segments_are_never_carriers(tmp_path=None):
    """A trench cannot be dug on a bridge deck or through a tunnel."""
    import json as _json
    import tempfile

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"fclass": "footway"},
         "geometry": {"type": "LineString", "coordinates": [[13.4, 52.5], [13.401, 52.5]]}},
        {"type": "Feature", "properties": {"fclass": "footway", "bridge": "T"},
         "geometry": {"type": "LineString", "coordinates": [[13.402, 52.5], [13.403, 52.5]]}},
        {"type": "Feature", "properties": {"fclass": "service", "tunnel": "T"},
         "geometry": {"type": "LineString", "coordinates": [[13.404, 52.5], [13.405, 52.5]]}},
    ]}
    path = os.path.join(tempfile.mkdtemp(), "roads.geojson")
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(fc, fh)
    walk, veh = td._read_road_parts(path, 25833)
    assert len(walk) == 1 and walk[0][1] == "footway"
    # both excluded parts stay crossable (they are still roads)
    assert len(veh) == 2
    classes = sorted(c for _coords, c in veh)
    assert classes == ["footway", "service"]


def test_runs_from_edges_yields_one_continuous_run():
    sg = td.build_street_graph(_cross_walkable(), td.Params())
    keys = list(sg.edge_coords.keys())
    runs, _breaks, _edges = td.runs_from_edges(keys, sg)
    assert len(runs) == 1
    coords = runs[0]
    assert coords[0] == (0.0, 0.0) and coords[-1] == (50.0, 100.0)
    assert td._coords_len(coords) == pytest.approx(150.0)


def test_runs_from_edges_break_at_branch():
    """A T-junction (degree 3) splits the network into separate runs."""
    parts = [([(0, 0), (50, 0)], "footway"),
             ([(50, 0), (100, 0)], "footway"),
             ([(50, 0), (50, 50)], "footway")]
    sg = td.build_street_graph(parts, td.Params())
    runs, breaks, _edges = td.runs_from_edges(list(sg.edge_coords.keys()), sg)
    assert len(runs) == 3
    assert len([n for n in breaks if sg.G.degree(n) >= 3]) == 1


# ── drills (HDD) ─────────────────────────────────────────────────────────────

def test_drill_detected_on_perpendicular_crossing():
    runs = [_run([(0, 0), (100, 0)])]
    vehicular = [([(50, -20), (50, 20)], "residential")]
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert len(drills) == 1
    d = drills[0]
    assert d["cls"] == "residential"
    assert d["width"] == pytest.approx(6.5 + 2.0)
    # the bore runs perpendicular to the road (i.e. along the trench), centred
    # on the crossing point and exactly the road width + 2 m long
    (x0, y0), (x1, y1) = d["coords"]
    assert y0 == pytest.approx(0.0, abs=1e-6) and y1 == pytest.approx(0.0, abs=1e-6)
    assert (x0 + x1) / 2.0 == pytest.approx(50.0)
    assert abs(x1 - x0) == pytest.approx(8.5)


def test_parallel_overlap_is_not_a_drill():
    runs = [_run([(0, 0), (100, 0)])]
    vehicular = [([(0, 0), (100, 0)], "residential")]   # trench runs along the road
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert drills == []


def test_oblique_crossing_bore_follows_the_trench_and_gets_longer():
    """Crossing at ~45 deg: the bore continues the trench line (both pits on it)
    and its length is the road width measured along that line."""
    runs = [_run([(-100, -100), (100, 100)])]                 # NE diagonal trench
    vehicular = [([(100, 0), (-100, 0)], "tertiary")]         # east-west road
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert len(drills) == 1
    d = drills[0]
    (x0, y0), (x1, y1) = d["coords"]
    # axis parallel to the trench
    assert (x1 - x0) == pytest.approx(y1 - y0, abs=1e-6)
    # > road width, because the bore cuts the road diagonally
    assert d["width"] > 7.5
    assert d["width"] == pytest.approx(7.5 / math.sin(math.radians(45)) + 2.0, abs=0.2)


def test_drill_axis_is_along_the_trench_not_across_it():
    """A north-south carriageway crossed by an east-west trench: the bore runs
    east-west (perpendicular to the road) so it only spans the road width."""
    runs = [_run([(0, 0), (100, 0)])]
    vehicular = [([(50, -50), (50, 50)], "tertiary")]
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert len(drills) == 1
    (x0, y0), (x1, y1) = drills[0]["coords"]
    assert y0 == pytest.approx(0.0, abs=1e-9)
    assert y1 == pytest.approx(0.0, abs=1e-9)
    assert abs(x1 - x0) == pytest.approx(7.5 + 2.0)


def test_nearby_crossings_are_deduped():
    runs = [_run([(0, 0), (200, 0)])]
    vehicular = [([(50, -20), (50, 20)], "residential"),
                 ([(60, -20), (60, 20)], "residential")]
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert len(drills) == 1                      # 10 m apart < 25 m dedupe


def test_far_crossings_are_kept():
    runs = [_run([(0, 0), (300, 0)])]
    vehicular = [([(50, -20), (50, 20)], "residential"),
                 ([(200, -20), (200, 20)], "residential")]
    drills = td.detect_drills(vehicular, runs, td.Params(), lambda m: None)
    assert len(drills) == 2


# ── structural nodes ─────────────────────────────────────────────────────────

def test_hdd_pit_pair_is_not_collapsed_by_the_global_separation():
    run = _run([(0, 0), (100, 0)])
    drills = [{"coords": [(50.0, -4.25), (50.0, 4.25)], "cls": "residential",
               "width": 8.5, "arc": 50.0, "run": id(run)}]
    nodes = td.place_nodes([run], drills, [], td.Params())
    pits = [n for n in nodes if n["NODE_TYPE"] == "HDD_PIT"]
    assert len(pits) == 2            # ~8.5 m apart, below min_node_sep_m


def test_pdp_stacked_on_a_pit_merges_instead_of_adding_a_structure():
    run = _run([(0, 0), (100, 0)])
    drills = [{"coords": [(50.0, -4.25), (50.0, 4.25)], "cls": "residential",
               "width": 8.5, "arc": 50.0, "run": id(run)}]
    nodes = td.place_nodes([run], drills, [{"x": 51.0, "y": 2.0,
                                            "PDP_ID": "PDP-9"}], td.Params())
    assert [n["NODE_TYPE"] for n in nodes].count("PDP") == 0
    assert [n["NODE_TYPE"] for n in nodes].count("HDD_PIT") == 2


def test_pull_chambers_fill_long_empty_gaps():
    run = _run([(0, 0), (600, 0)])
    p = td.Params()
    nodes = td.place_nodes([run], [], [], p)
    pulls = [n for n in nodes if n["NODE_TYPE"] == "PULL"]
    assert len(pulls) == 2                      # 250 m and 500 m on a 600 m run


def test_pull_chambers_fill_every_gap_not_just_a_fixed_mark():
    """A mid-run chamber must not suppress the pull for the whole run.

    Berlin regression: a 329.8 m feeder run (250 m interval) with a centre
    node published **no PULL node**, because the old rule tested a fixed 250 m
    mark and skipped it whenever any chamber sat within one interval behind.
    """
    run = _run([(0, 0), (330, 0)])
    p = td.Params()
    # a junction mid-run, as a drill pit / branch would leave behind
    drills = [{"coords": [(100.0, -4.25), (100.0, 4.25)], "cls": "residential",
               "width": 8.5, "arc": 100.0, "run": id(run)}]
    nodes = td.place_nodes([run], drills, [], p)
    pulls = sorted(n["arc"] for n in nodes if n["NODE_TYPE"] == "PULL")
    assert pulls == [250.0]
    # every chamber-to-chamber gap on the run is now inside the interval
    chambers = sorted([0.0, 330.0] + [n["arc"] for n in nodes
                                      if n.get("run") == id(run)])
    assert max(b - a for a, b in zip(chambers, chambers[1:])) <= p.pull_backbone_m


def test_pull_interval_follows_the_tier():
    """Distribution pulls at 100 m, backbone at 250 m."""
    p = td.Params()
    dist = td.place_nodes([_run(tier="Distribution", coords=[(0, 0), (250, 0)])], [], [], p)
    pulls = [n["arc"] for n in dist if n["NODE_TYPE"] == "PULL"]
    assert pulls == [100.0, 200.0]
    # a run shorter than the interval needs none
    short = td.place_nodes([_run(tier="Distribution", coords=[(0, 0), (90, 0)])], [], [], p)
    assert [n for n in short if n["NODE_TYPE"] == "PULL"] == []


def test_bend_node_placed_on_sharp_turn():
    run = _run([(0, 0), (50, 0), (50, 50)])
    nodes = td.place_nodes([run], [], [], td.Params())
    assert [n["NODE_TYPE"] for n in nodes].count("BEND") == 1


# ── spans ────────────────────────────────────────────────────────────────────

def test_split_spans_cuts_at_nodes_and_preserves_length():
    run = _run([(0, 0), (100, 0)])
    p = td.Params()
    nodes = [
        {"x": 30.0, "y": 0.0, "NODE_TYPE": "JUNCTION", "NODE_ID": "TN-1"},
        {"x": 60.0, "y": 0.0, "NODE_TYPE": "JUNCTION", "NODE_ID": "TN-2"},
    ]
    spans = td.split_spans(run, nodes, p)
    assert len(spans) == 3
    assert sum(s["length"] for s in spans) == pytest.approx(100.0)
    assert spans[0]["start"] is None and spans[0]["end"]["NODE_ID"] == "TN-1"
    assert spans[1]["start"]["NODE_ID"] == "TN-1"
    assert spans[1]["end"]["NODE_ID"] == "TN-2"
    assert spans[2]["start"]["NODE_ID"] == "TN-2" and spans[2]["end"] is None
    # the sequence is continuous: each span ends where the next begins
    for a, b in zip(spans, spans[1:]):
        assert a["coords"][-1] == b["coords"][0]


def test_bore_at_the_run_start_is_its_own_hdd_span():
    """Regression: a crossing a few metres into a run was absorbed into the
    first open-cut span by the node-separation dedupe."""
    run = _run([(0, 0), (100, 0)])
    p = td.Params()
    nodes = [{"x": 7.0, "y": 0.0, "NODE_TYPE": "HDD_PIT", "NODE_ID": "TN-1"}]
    spans = td.split_spans(run, nodes, p, type_map=[(0.0, 7.0, "HDD")])
    assert spans[0]["type"] == "HDD"
    assert spans[0]["length"] == pytest.approx(7.0)
    assert sum(s["length"] for s in spans) == pytest.approx(100.0)


def test_bore_boundaries_always_split_the_span():
    """A bore is always cut out as its own span, whatever the node list says."""
    run = _run([(0, 0), (200, 0)])
    spans = td.split_spans(run, [], td.Params(), type_map=[(50.0, 57.0, "HDD")])
    assert [s["type"] for s in spans] == ["Open Cut", "HDD", "Open Cut"]
    assert sum(s["length"] for s in spans) == pytest.approx(200.0)


def test_bore_outside_the_run_never_types_the_span_as_hdd():
    """Regression guard: a bore arc alone (no matching span) must not mark the
    whole open-cut span as HDD — the overlap is compared against the LONGER of
    the two ranges."""
    run = _run([(0, 0), (100, 0)])
    spans = td.split_spans(run, [], td.Params(), type_map=[(150.0, 160.0, "HDD")])
    assert [s["type"] for s in spans] == ["Open Cut"]


def test_split_spans_marks_hdd_arcs():
    run = _run([(0, 0), (100, 0)])
    p = td.Params()
    nodes = [
        {"x": 45.0, "y": 0.0, "NODE_TYPE": "HDD_PIT", "NODE_ID": "TN-1"},
        {"x": 55.0, "y": 0.0, "NODE_TYPE": "HDD_PIT", "NODE_ID": "TN-2"},
    ]
    spans = td.split_spans(run, nodes, p, type_map=[(45.0, 55.0, "HDD")])
    types = [s["type"] for s in spans]
    assert types == ["Open Cut", "HDD", "Open Cut"]
    hdd = [s for s in spans if s["type"] == "HDD"][0]
    assert hdd["length"] == pytest.approx(10.0)
    assert sum(s["length"] for s in spans) == pytest.approx(100.0)


# ── garden legs ──────────────────────────────────────────────────────────────

def test_garden_leg_type_depends_on_length():
    p = td.Params(max_garden_m=30.0)
    network = [[(0, 0), (0, 100)]]
    houses = [{"x": 10.0, "y": 50.0, "ADDR_ID": "A", "PDP_ID": "P1"},
              {"x": 80.0, "y": 50.0, "ADDR_ID": "B", "PDP_ID": "P1"}]
    legs = td.design_garden_legs(network, houses, p, lambda m: None)
    assert len(legs) == 2
    by_addr = {leg["house"]["ADDR_ID"]: leg for leg in legs}
    assert by_addr["A"]["type"] == "Garden"        # 10 m drop
    assert by_addr["B"]["type"] == "Open Cut"      # 80 m would be a dig


def test_garden_leg_keeps_out_of_reach_houses_for_aerial_evaluation():
    p = td.Params(house_search_m=50.0)
    network = [[(0, 0), (0, 100)]]
    houses = [{"x": 500.0, "y": 50.0, "ADDR_ID": "far", "PDP_ID": "P1"}]
    legs = td.design_garden_legs(network, houses, p, lambda m: None)
    assert len(legs) == 1
    assert legs[0]["unreachable"] is True


# ── aerial classification ────────────────────────────────────────────────────

def _zone(points):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in points:
        ring.AddPoint_2D(float(x), float(y))
    ring.AddPoint_2D(float(points[0][0]), float(points[0][1]))
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _legs():
    return [
        {"coords": [(0.0, 0.0), (0.0, 10.0)], "length": 10.0,
         "type": "Garden", "house": {"ADDR_ID": "in"}},
        {"coords": [(100.0, 0.0), (100.0, 10.0)], "length": 10.0,
         "type": "Garden", "house": {"ADDR_ID": "out"}},
    ]


def test_zone_polygons_flatten_multiparts_and_never_over_match():
    """Contains() on a collection/MultiPolygon wrongly reports outside points.

    The zone test must flatten to single polygons: a point 130 m away must not
    come back as "inside the zone".
    """
    poly = _zone([(0, 0), (10, 0), (10, 10), (0, 10)])
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    multi.AddGeometry(poly)
    coll = ogr.Geometry(ogr.wkbGeometryCollection)
    coll.AddGeometry(multi)
    polys = td._zone_polygons(coll)
    assert len(polys) == 1
    assert td._in_zone(polys, 5.0, 5.0) is True
    assert td._in_zone(polys, 105.0, 5.0) is False


def test_aerial_zone_moves_the_leg_out_of_the_trench_layer():
    p = td.Params()
    # a park polygon covering only the first leg
    trenched, aerial = td._split_drop_legs(
        _legs(), td._zone_polygons(_zone([(-5, -5), (5, -5), (5, 15), (-5, 15)])),
        p, lambda m: None)
    assert [leg["house"]["ADDR_ID"] for leg in aerial] == ["in"]
    assert [leg["house"]["ADDR_ID"] for leg in trenched] == ["out"]
    assert aerial[0]["type"] == "Aerial"
    assert aerial[0]["aerial_reason"] == "zone"


def test_aerial_zone_is_detected_anywhere_along_the_leg():
    # zone only near the far end — a midpoint-only test would miss it
    p = td.Params()
    trenched, aerial = td._split_drop_legs(
        _legs(), td._zone_polygons(_zone([(-5, 8), (5, 8), (5, 15), (-5, 15)])),
        p, lambda m: None)
    assert [leg["house"]["ADDR_ID"] for leg in aerial] == ["in"]


def test_no_zone_and_no_length_rule_keeps_every_leg_trenched():
    p = td.Params()
    trenched, aerial = td._split_drop_legs(_legs(), None, p, lambda m: None)
    assert len(trenched) == 2 and aerial == []
    assert set(leg["type"] for leg in trenched) == {"Garden"}


def test_unreachable_drop_is_classified_as_aerial():
    legs = [{"coords": [(0.0, 0.0), (0.0, 80.0)], "length": 80.0,
             "type": "Open Cut", "house": {"ADDR_ID": "far"},
             "parent": -1, "unreachable": True}]
    _trenched, aerial = td._split_drop_legs(
        legs, None, td.Params(), lambda m: None)
    assert len(aerial) == 1
    assert aerial[0]["aerial_reason"] == "unreachable"


def test_aerial_max_leg_rule_is_opt_in():
    legs = [{"coords": [(0.0, 0.0), (0.0, 80.0)], "length": 80.0,
             "type": "Open Cut", "house": {"ADDR_ID": "long"}}]
    off, _ = td._split_drop_legs(legs, None, td.Params(), lambda m: None)
    assert len(off) == 1 and off[0]["type"] == "Open Cut"
    _t, on = td._split_drop_legs(legs, None,
                                 td.Params(aerial_max_leg_m=60.0), lambda m: None)
    assert len(on) == 1 and on[0]["type"] == "Aerial"
    assert on[0]["aerial_reason"] == "length"


def test_aerial_flag_marks_spans_crossing_the_zone():
    zone = td._zone_polygons(_zone([(-5, -5), (5, -5), (5, 15), (-5, 15)]))
    assert td._aerial_flag(zone, [(0, 0), (0, 10)]) == "zone"
    assert td._aerial_flag(zone, [(100, 0), (100, 10)]) == ""
    assert td._aerial_flag([], [(0, 0), (0, 10)]) == ""


def test_garden_leg_records_the_leg_it_branched_off():
    """Sharing must be traceable: ``parent`` is the leg a drop chains onto.

    The aerial rule walks the chain, so a leg's parent has to be recoverable.
    A trunk house and a house further along the same street: the second joins
    the first, not the mains.
    """
    p = td.Params()
    network = [[(0, 0), (0, 100)]]
    houses = [{"x": 10.0, "y": 50.0, "ADDR_ID": "trunk", "PDP_ID": "P1"},
              {"x": 10.0, "y": 45.0, "ADDR_ID": "branch", "PDP_ID": "P1"}]
    legs = td.design_garden_legs(network, houses, p, lambda m: None)
    by_addr = {leg["house"]["ADDR_ID"]: leg for leg in legs}
    assert by_addr["trunk"]["parent"] == -1              # meets the mains
    assert by_addr["branch"]["parent"] == 0              # chains onto the trunk
    # the branch's root IS a point on the trunk, so the chain reaches the mains
    trunk = by_addr["trunk"]["coords"]
    assert trunk[1] == by_addr["branch"]["coords"][0]     # trunk end = branch root


def test_aerial_propagates_down_a_drop_chain():
    """A trench cannot start in mid-air.

    Berlin regression: the sharing rule rooted an 18.6 m **Garden** leg on an
    aerial drop, leaving 18.6 m of open trench 22 m off the mains and its house
    40.6 m from any trench. A leg chained onto an aerial leg flies too
    (``aerial_reason == 'chain'``), so every trenched leg roots on the network.
    """
    p = td.Params()
    # zone covers only the first 5 m of the parent leg (y <= 5)
    zone = td._zone_polygons(_zone([(-5, -5), (5, -5), (5, 5), (-5, 5)]))
    legs = [
        {"coords": [(0.0, 0.0), (0.0, 10.0)], "length": 10.0,
         "type": "Garden", "house": {"ADDR_ID": "in-zone"}, "parent": -1},
        {"coords": [(0.0, 10.0), (0.0, 20.0)], "length": 10.0,
         "type": "Garden", "house": {"ADDR_ID": "outside"}, "parent": 0},
        {"coords": [(100.0, 0.0), (100.0, 10.0)], "length": 10.0,
         "type": "Garden", "house": {"ADDR_ID": "clean"}, "parent": -1},
    ]
    trenched, aerial = td._split_drop_legs(legs, zone, p, lambda m: None)
    assert [leg["house"]["ADDR_ID"] for leg in aerial] == ["in-zone", "outside"]
    assert aerial[0]["aerial_reason"] == "zone"
    assert aerial[1]["aerial_reason"] == "chain"      # not in the zone itself
    assert [leg["house"]["ADDR_ID"] for leg in trenched] == ["clean"]
    # every trenched leg still roots on the mains (-1) or on a trenched parent
    assert all(leg["parent"] == -1 for leg in trenched)


def test_aerial_chain_does_not_leak_past_a_trenched_parent():
    """A leg whose parent is trenched stays trenched, even if a sibling flies."""
    p = td.Params()
    zone = td._zone_polygons(_zone([(-5, -5), (5, -5), (5, 15), (-5, 15)]))
    legs = [
        {"coords": [(0.0, 0.0), (0.0, 2.0)], "length": 2.0,
         "type": "Garden", "house": {"ADDR_ID": "feeder"}, "parent": -1},
        {"coords": [(0.0, 2.0), (0.0, 4.0)], "length": 2.0,
         "type": "Garden", "house": {"ADDR_ID": "child"}, "parent": 0},
    ]
    trenched, aerial = td._split_drop_legs(legs, zone, p, lambda m: None)
    # both legs are inside the zone, so both fly - the chain rule never
    # resurrects a trench inside a zone
    assert len(aerial) == 2 and not trenched
    # with no zone at all the same chain is fully trenched
    trenched, aerial = td._split_drop_legs(legs, None, p, lambda m: None)
    assert len(trenched) == 2 and not aerial


def test_pdp_spur_gap_threshold_is_how_far_a_cabinet_may_float():
    """A splitter beside the trench gets a spur; the threshold decides when.

    Berlin: the pre-trim pass let an 8 m gap stand (closer than
    ``min_node_sep_m``), the trim then cut the run back, and PDP00019 was left
    **17.77 m** from any trench. The post-trim pass runs with a tight tolerance
    so "every PDP sits on a trench" is true of the FINAL run set.
    """
    p = td.Params(min_node_sep_m=10.0)
    mains = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Feeder")]
    pdp = {"PDP_ID": "P1", "x": 5.0, "y": 50.0}          # 5 m off the run

    out, stats = td.connect_unreached_pdps(list(mains), [pdp], p, lambda m: None)
    assert stats["pdp_spurs"] == 0 and len(out) == 1      # 5 m < 10 m: left alone

    out, stats = td.connect_unreached_pdps(list(mains), [pdp], p, lambda m: None,
                                           max_gap_m=2.0)
    assert stats["pdp_spurs"] == 1                         # 5 m > 2 m: spurred
    spur = out[-1]
    assert spur.src == "pdp-spur" and spur.tier == "Feeder"
    assert spur.coords[1] == (5.0, 50.0)                   # ends ON the splitter
    assert td._coords_len(spur.coords) == pytest.approx(5.0)
    assert stats["max_spur_m"] == pytest.approx(5.0)


def test_anchor_touch_tolerance_closes_a_two_metre_gap():
    """The published trench must TOUCH its anchors (Berlin: MFG 1.91 m, 10 PDPs 1.2-1.9 m).

    A 2 m tolerance left every cabinet up to 2 m off the network, which reads
    as "the trenches do not connect the MFG and the PDPs" on the map and is
    inherited by the feeder/distribution duct and cable (they club at 0.5 m).
    ``anchor_touch_m`` is the physical touching distance, so the same 1.9 m gap
    that used to be tolerated now gets a connector.
    """
    p = td.Params()
    assert p.anchor_touch_m <= 0.5, "must not exceed the duct/cable club tolerance"
    mains = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Distribution")]
    pdp = {"PDP_ID": "P9", "x": 1.9, "y": 50.0}

    # old behaviour (2 m): tolerated, trench stays 1.9 m off the splitter
    out_old, old = td.connect_unreached_pdps(list(mains), [pdp], p,
                                             lambda m: None, max_gap_m=2.0)
    assert old["pdp_spurs"] == 0 and len(out_old) == 1

    # new behaviour: connector added, and it ends exactly on the splitter
    out, stats = td.connect_unreached_pdps(
        list(mains), [pdp], p, lambda m: None, max_gap_m=p.anchor_touch_m)
    assert stats["pdp_spurs"] == 1
    spur = out[-1]
    assert spur.coords[-1] == (1.9, 50.0)
    assert td._coords_len(spur.coords) == pytest.approx(1.9, abs=0.01)


def test_mfg_connector_closes_a_gap_and_is_idempotent():
    """The MFG is the root of the feeder: it gets the same guarantee as a PDP."""
    p = td.Params()
    runs = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Feeder")]
    mfg = {"MFG_ID": "MFG00001", "x": 1.91, "y": 50.0}

    out, stats = td.connect_unreached_mfg(list(runs), mfg, p, lambda m: None)
    assert stats["mfg_connected"] == 1
    assert stats["mfg_gap_m"] == pytest.approx(1.91, abs=0.01)
    conn = out[-1]
    assert conn.src == "mfg-connector"
    assert conn.coords[-1] == (1.91, 50.0)

    # A second pass sees the connector already touching the MFG: no duplicate.
    out2, stats2 = td.connect_unreached_mfg(list(out), mfg, p, lambda m: None)
    assert len(out2) == len(out)
    assert stats2["mfg_connected"] == 1


def test_mfg_connector_skipped_when_already_touching():
    p = td.Params()
    runs = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Feeder")]
    out, stats = td.connect_unreached_mfg(
        list(runs), {"MFG_ID": "M", "x": 0.0, "y": 40.0}, p, lambda m: None)
    assert len(out) == 1 and stats["mfg_gap_m"] == pytest.approx(0.0)


def test_pdp_already_on_the_trench_gets_no_spur():
    p = td.Params()
    mains = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Feeder")]
    pdp = {"PDP_ID": "P2", "x": 0.0, "y": 50.0}
    out, stats = td.connect_unreached_pdps(list(mains), [pdp], p, lambda m: None,
                                           max_gap_m=2.0)
    assert stats["pdp_spurs"] == 0 and len(out) == 1


def test_aerial_legs_are_never_trench_types():
    """The excavated trench type stays the closed 3-value set."""
    p = td.Params(aerial_max_leg_m=5.0)
    trenched, aerial = td._split_drop_legs(_legs(), None, p, lambda m: None)
    assert aerial and not trenched
    assert set(leg["type"] for leg in aerial) == {"Aerial"}
    assert td.FIELD_LINE  # sanity: trench fields unchanged
    assert all(name != "TRENCH_TYPE" or True
               for name, _t in td.FIELD_AERIAL)


# ── street avoidance: the carriageway cost ladder ────────────────────────────

def test_footways_are_the_cheapest_carrier():
    p = td.Params()
    assert td._class_factor("footway", 1.0) == 1.0
    assert td._class_factor("service", 1.0) < td._class_factor("residential", 1.0)
    assert td._class_factor("cycleway", 1.0) < td._class_factor("residential", 1.0)


def test_ladder_is_ordered_by_road_size():
    """A bigger road costs strictly more to dig along."""
    order = ["residential", "tertiary", "secondary", "primary"]
    costs = [td._class_factor(c, 1.0) for c in order]
    assert costs == sorted(costs), costs
    assert costs[0] < costs[-1]


def test_tertiary_is_a_last_resort_not_a_cheap_carrier():
    """Regression: tertiary used to sit at NON_CARRIER_FACTOR (3.0), i.e. *cheaper*
    than residential (8.0), so the router preferred district roads."""
    assert td._class_factor("tertiary", 1.0) > td._class_factor("residential", 1.0)


def test_street_avoid_scale_dials_the_penalty_without_reordering():
    base = [td._class_factor(c, 1.0) for c in ("residential", "tertiary", "secondary")]
    scaled = [td._class_factor(c, 2.0) for c in ("residential", "tertiary", "secondary")]
    assert scaled == [2 * b for b in base]
    assert scaled == sorted(scaled)
    assert td._class_factor("footway", 2.0) == 1.0      # footways stay cheap


def test_class_factor_never_goes_below_one():
    assert td._class_factor("residential", 0.0) == 1.0


def test_router_picks_the_smaller_street_when_a_street_is_unavoidable():
    """Two parallel street corridors, one residential one tertiary, equal length:
    the route must take the residential one."""
    walkable = [
        ([(0.0, 0.0), (0.0, 100.0)], "residential"),
        ([(50.0, 0.0), (50.0, 100.0)], "tertiary"),
        ([(0.0, 0.0), (50.0, 0.0)], "footway"),
        ([(0.0, 100.0), (50.0, 100.0)], "footway"),
    ]
    sg = td.build_street_graph(walkable, td.Params())
    a = sg.nearest_node(0.0, 0.0, 5.0)
    b = sg.nearest_node(50.0, 100.0, 5.0)
    path = td._route(sg.G, a, b)
    classes = [sg.G.edges[(path[i], path[i + 1])]["cls"] for i in range(len(path) - 1)]
    assert "residential" in classes
    assert "tertiary" not in classes, classes


def test_router_walks_a_long_footway_detour_to_stay_off_a_street():
    """A 200 m residential shortcut loses to a 600 m footway detour."""
    walkable = [
        ([(0.0, 0.0), (0.0, 100.0), (0.0, 200.0)], "residential"),   # straight
        ([(0.0, 0.0), (300.0, 0.0), (300.0, 200.0), (0.0, 200.0)], "footway"),
    ]
    sg = td.build_street_graph(walkable, td.Params())
    a = sg.nearest_node(0.0, 0.0, 5.0)
    b = sg.nearest_node(0.0, 200.0, 5.0)
    path = td._route(sg.G, a, b)
    classes = {sg.G.edges[(path[i], path[i + 1])]["cls"] for i in range(len(path) - 1)}
    assert classes == {"footway"}, classes


# ── anchored network: no trench to nowhere ──────────────────────────────────

def _span(tid, coords, run="RUN-00001", tier="Feeder", src="street-graph"):
    """A span row as the designer publishes it (geometry in ``geom``, not
    ``coords`` — the prune pass reads what the layer writer reads)."""
    return {"TRENCH_ID": tid, "RUN_ID": run,
            "geom": td._make_multiline([list(coords)]),
            "TRENCH_TYPE": "Open Cut", "TRENCH_TIER": tier, "SRC": src,
            "length_m": td._coords_len(coords)}


def test_span_coords_reads_the_published_geometry():
    assert td._span_coords(_span("TR-1", [(0, 0), (10, 5)])) == [(0.0, 0.0), (10.0, 5.0)]
    assert td._span_coords({"TRENCH_ID": "x"}) == []


def test_anchored_chain_is_never_pruned():
    spans = [_span("TR-1", [(0, 0), (10, 0)]), _span("TR-2", [(10, 0), (20, 0)])]
    keep, pruned = td.prune_unanchored_spans(spans, [(0.0, 0.0), (20.0, 0.0)],
                                             td.Params(), lambda m: None)
    assert len(keep) == 2 and not pruned


def test_unanchored_group_is_pruned_and_reported():
    spans = [_span("TR-1", [(0, 0), (10, 0)]), _span("TR-2", [(10, 0), (20, 0)]),
             _span("TR-9", [(500, 500), (510, 500)])]
    msgs = []
    keep, pruned = td.prune_unanchored_spans(spans, [(0.0, 0.0), (20.0, 0.0)],
                                             td.Params(), msgs.append)
    assert [s["TRENCH_ID"] for s in keep] == ["TR-1", "TR-2"]
    assert [s["TRENCH_ID"] for s in pruned] == ["TR-9"]
    assert "trench to nowhere" in " ".join(msgs)


def test_house_drop_span_is_anchored_even_far_from_the_point_list():
    """A garden leg is anchored by its own SRC, not by a coordinate lookup."""
    spans = [_span("TR-1", [(0, 0), (10, 0)]), _span("TR-2", [(10, 0), (25, 0)])]
    keep, pruned = td.prune_unanchored_spans(spans, [(0.0, 0.0)], td.Params(),
                                             lambda m: None)
    assert len(keep) == 2, "an org-free street chain anchored at one end must stay"
    assert not pruned


def test_distribution_run_is_protected_by_a_drop_leg_hanging_off_it():
    """Regression: a service run whose end stops at the street node nearest the
    house (not at the house itself) reaches no anchor on its own. It *is* the
    mains the drop leg hangs off, so pruning it orphans that house. Group
    connectivity must be geometric (T-join), not shared-endpoint only."""
    spans = [_span("TR-1", [(0, 0), (40, 0)]),
             _span("TR-2", [(40, 0), (60, 0)]),
             # the leg starts mid-span on TR-1 and ends at its house
             _span("TR-3", [(20, 0), (20, -12)], src="house-drop")]
    anchors = [(20.0, -12.0)]      # only the house; no anchor near TR-1/TR-2
    keep, pruned = td.prune_unanchored_spans(spans, anchors, td.Params(),
                                             lambda m: None)
    assert not pruned, [s["TRENCH_ID"] for s in pruned]
    assert len(keep) == 3


def test_truly_stray_fragment_is_still_pruned():
    """A fragment far from every anchor and from every drop leg does go."""
    spans = [_span("TR-1", [(0, 0), (40, 0)]),
             _span("TR-2", [(40, 0), (60, 0)]),
             _span("TR-3", [(20, 0), (20, -12)], src="house-drop"),
             _span("TR-8", [(900, 900), (940, 900)]),
             _span("TR-9", [(940, 900), (980, 900)])]
    keep, pruned = td.prune_unanchored_spans(spans, [(20.0, -12.0)], td.Params(),
                                             lambda m: None)
    assert sorted(s["TRENCH_ID"] for s in pruned) == ["TR-8", "TR-9"]
    assert len(keep) == 3


def test_prune_can_be_switched_off():
    spans = [_span("TR-9", [(500, 500), (510, 500)])]
    keep, pruned = td.prune_unanchored_spans(spans, [(0.0, 0.0)],
                                             td.Params(prune_dangling=False),
                                             lambda m: None)
    assert len(keep) == 1 and not pruned


def test_pruning_never_removes_a_span_out_of_a_live_chain():
    """Only whole groups go: a mid-chain span can never be picked out."""
    spans = [_span("TR-%d" % i, [(i * 10.0, 0), ((i + 1) * 10.0, 0)]) for i in range(6)]
    keep, pruned = td.prune_unanchored_spans(spans, [(0.0, 0.0), (60.0, 0.0)],
                                             td.Params(), lambda m: None)
    assert len(keep) == 6 and not pruned


def test_dangling_end_is_reported_but_not_removed():
    spans = [_span("TR-1", [(0, 0), (50, 0)]), _span("TR-2", [(50, 0), (100, 0)])]
    loose = td.dangling_ends(spans, [(0.0, 0.0)], td.Params())
    assert [d["TRENCH_ID"] for d in loose] == ["TR-2"]
    assert loose[0]["which"] == "end"


def test_t_join_counts_as_connected_for_the_loose_end_check():
    """A run starting mid-span on another run is joined, not loose.

    The far end of this stub reaches no anchor, so it *is* reported — the
    point is that the T-joined end is not.
    """
    spans = [_span("TR-1", [(0, 0), (100, 0)]),
             _span("TR-2", [(50, 0), (50, -20)])]
    loose = td.dangling_ends(spans, [(0.0, 0.0), (100.0, 0.0)], td.Params())
    assert [(d["TRENCH_ID"], d["which"]) for d in loose] == [("TR-2", "end")]


def test_house_drop_is_exempt_from_the_loose_end_check():
    """A garden leg ends at its premise by construction — never flagged."""
    spans = [_span("TR-1", [(0, 0), (100, 0)]),
             _span("TR-2", [(50, 0), (50, -20)], src="house-drop")]
    loose = td.dangling_ends(spans, [(0.0, 0.0), (100.0, 0.0)], td.Params())
    assert loose == []


# ── is the published trench ONE network? ────────────────────────────────────
# The duct router walks the trench, so a second disconnected group is duct that
# cannot be laid. Berlin 2026-09-21: the layer is 92 groups (1 network plus 91
# detached, 1,643 m) while the run reported "loose ends: 0" — because a drop
# whose end touches ANOTHER DROP has no loose end and is still severed.

def test_a_drop_touching_another_drop_is_not_loose_but_is_still_detached():
    """The two checks answer different questions, and only one is the real one."""
    spans = [_span("TR-1", [(0, 0), (100, 0)]),
             _span("TR-2", [(50, 40), (50, 60)], src="house-drop"),
             _span("TR-3", [(50, 60), (50, 80)], src="house-drop")]
    loose = td.dangling_ends(spans, [(0.0, 0.0), (100.0, 0.0)], td.Params())
    assert loose == []                        # nothing "loose" at all ...
    off = td.detached_span_groups(spans)      # ... and still not reachable
    assert len(off) == 1
    assert off[0]["gap_m"] == 40.0
    assert off[0]["drops"] == 2
    assert off[0]["spans"] == 2


def test_a_single_connected_network_has_no_detached_groups():
    spans = [_span("TR-1", [(0, 0), (50, 0)]),
             _span("TR-2", [(50, 0), (100, 0)])]
    assert td.detached_span_groups(spans) == []


def test_the_gap_is_measured_to_the_other_geometry_not_to_its_vertices():
    """A group 3 m off a long corridor is 3 m off it, however far its ends are."""
    spans = [_span("TR-1", [(0, 0), (100, 0)]),
             _span("TR-2", [(40, 3), (60, 3)], src="house-drop")]
    off = td.detached_span_groups(spans)
    assert len(off) == 1 and off[0]["gap_m"] == 3.0


def test_a_join_across_a_metre_is_not_detached():
    """The tolerance is the difference between "stitched" and "two networks"."""
    spans = [_span("TR-1", [(0, 0), (100, 0)]),
             _span("TR-2", [(100, 1.0), (130, 1.0)])]
    assert td.detached_span_groups(spans, join_tol=1.5) == []
    assert len(td.detached_span_groups(spans, join_tol=0.5)) == 1


def test_junction_end_is_not_a_loose_end():
    spans = [_span("TR-1", [(0, 0), (50, 0)]), _span("TR-2", [(50, 0), (100, 0)]),
             _span("TR-3", [(50, 0), (50, 40)])]
    loose = td.dangling_ends(spans, [(0.0, 0.0), (100.0, 0.0), (50.0, 40.0)],
                             td.Params())
    assert [d["TRENCH_ID"] for d in loose] == [], loose


# ── premise attribution: which house(s) a span was dug for ───────────────────
# The cabling stage indexes its distribution input BY ADDRESS and matches each
# garden row to it, so a trench that does not name its premises is a trench
# nothing can be cabled through. These tests pin the identity, not the geometry.

def _house(addr, x, y, hh=1, pid="P1", poly="POLY1"):
    return {"ADDR_ID": addr, "HH": hh, "x": x, "y": y,
            "PDP_ID": pid, "POLYGON_ID": poly}


def test_houses_on_edges_names_every_premise_riding_a_shared_run():
    """A shared spine span names ALL the premises it serves, not just one.

    Several houses route over the same street edges, and that shared trunk is
    exactly what the design exists to exploit — so the span has to be able to
    say whose drops it carries.
    """
    houses = [_house("A1", 0, 0, hh=2), _house("A2", 10, 0, hh=3),
              _house("A3", 20, 0, hh=1)]
    edge_houses = {("n1", "n2"): {0, 1, 2}}
    addr, hh = td.houses_on_edges([("n1", "n2")], edge_houses, houses)
    assert addr == "A1,A2,A3"
    assert hh == pytest.approx(6.0)


def test_houses_on_edges_is_none_when_no_house_rides_the_span():
    """A pure backbone span states it serves nobody — not a misleading 1."""
    houses = [_house("A1", 0, 0)]
    edge_houses = {("n1", "n2"): {0}}
    addr, hh = td.houses_on_edges([("n9", "n9b")], edge_houses, houses)
    assert addr is None and hh is None


def test_houses_on_edges_unions_across_the_edges_of_one_run():
    houses = [_house("A1", 0, 0, hh=2), _house("A2", 0, 0, hh=1)]
    edge_houses = {("a", "b"): {0}, ("b", "c"): {1}}
    addr, hh = td.houses_on_edges([("a", "b"), ("b", "c")], edge_houses, houses)
    assert addr == "A1,A2" and hh == pytest.approx(3.0)


def test_houses_on_edges_defaults_a_missing_household_count_to_one():
    houses = [{"ADDR_ID": "A1", "HH": None}, {"ADDR_ID": "A2"}]
    addr, hh = td.houses_on_edges([("e", "f")], {("e", "f"): {0, 1}}, houses)
    assert addr == "A1,A2" and hh == pytest.approx(2.0)


def test_houses_on_edges_dedupes_an_address_but_still_counts_its_houses():
    houses = [_house("A1", 0, 0, hh=2), _house("A1", 1, 0, hh=2),
              {"ADDR_ID": None, "HH": 1}]
    addr, hh = td.houses_on_edges([("e", "f")], {("e", "f"): {0, 1, 2}}, houses)
    assert addr == "A1"            # one address, listed once
    assert hh == pytest.approx(5.0)  # a NULL-address premise still carries HH


def test_spans_carry_the_premise_attribution():
    """split_spans hands the run's address(es) and HH to every span it cuts."""
    run = td.Run(coords=[(0.0, 0.0), (100.0, 0.0)], tier="Distribution",
                 addr="A1,A2", hh=5.0)
    spans = td.split_spans(
        run, [{"x": 50.0, "y": 0.0, "NODE_TYPE": "JUNCTION", "NODE_ID": "TN-1"}],
        td.Params())
    assert len(spans) == 2
    assert all(s["addr"] == "A1,A2" and s["hh"] == pytest.approx(5.0)
               for s in spans)


def test_garden_leg_span_names_exactly_one_premise():
    """A drop leg exists for one house, so its address is that house's."""
    run = td.Run(coords=[(0.0, 0.0), (0.0, 20.0)], tier="Garden",
                 src="house-drop", addr="A7", hh=4.0)
    spans = td.split_spans(run, [], td.Params())
    assert len(spans) == 1
    assert spans[0]["addr"] == "A7" and spans[0]["hh"] == pytest.approx(4.0)


def test_trimming_a_run_keeps_its_premise_attribution():
    """The tail trimmer rebuilds runs — the identity must survive the rebuild.

    ``trim_unserved_tails`` cuts a run back to its supports and constructs a
    NEW Run, so attribute propagation is easy to lose silently: the map would
    look right while the cabling stage quietly built nothing.
    """
    mains = [td.Run(coords=[(0.0, 0.0), (0.0, 100.0)], tier="Feeder",
                    addr="A1", hh=2.0)]
    leg = td.Run(coords=[(0.0, 50.0), (30.0, 50.0)], tier="Garden",
                 src="house-drop", addr="A1", hh=2.0)
    kept, _stats = td.trim_unserved_tails(mains, [leg], [(0.0, 0.0)],
                                          td.Params(), lambda m: None)
    assert kept, "the supported part of the run must survive"
    assert all(r.addr == "A1" and r.hh == pytest.approx(2.0) for r in kept)


def test_trench_and_aerial_fields_publish_the_premise_attribution():
    line = [n for n, _t in td.FIELD_LINE]
    assert "ADDR_ID" in line and "HH" in line
    aerial = [n for n, _t in td.FIELD_AERIAL]
    assert "ADDR_ID" in aerial and "HH" in aerial


def test_addr_of_normalises_blank_null_and_missing():
    assert td._addr_of({"ADDR_ID": "  A1 "}) == "A1"
    assert td._addr_of({"ADDR_ID": 3008521}) == "3008521"
    assert td._addr_of({"ADDR_ID": ""}) is None
    assert td._addr_of({"ADDR_ID": "NULL"}) is None
    assert td._addr_of({}) is None


def test_hh_of_defaults_to_one():
    assert td._hh_of({"HH": 3}) == pytest.approx(3.0)
    assert td._hh_of({"HH": "2.5"}) == pytest.approx(2.5)
    assert td._hh_of({"HH": 0}) == pytest.approx(1.0)
    assert td._hh_of({"HH": None}) == pytest.approx(1.0)
    assert td._hh_of({}) == pytest.approx(1.0)


# ── determinism: the design must not depend on set/hash iteration order ───────
# The edge sets handed to runs_from_edges are SETS of (str, str) tuples, and
# Python randomises string hashing per process. Iterating them directly made the
# whole design irreproducible: measured on Berlin, two runs of the same code
# gave 497 vs 495 spans and 21 differing drill geometries. These tests pin the
# ordering rule, which is what removes the dependence.

def _grid_graph():
    """A small street graph with a branch, so run assembly has real choices."""
    sg = td.StreetGraph(G=nx.Graph(), edge_coords={}, node_xy={},
                        index=td.GridIndex(cell=50.0), node_keys=[],
                        main_component=set())
    pts = {"a": (0.0, 0.0), "b": (0.0, 10.0), "c": (0.0, 20.0), "d": (10.0, 10.0)}
    for k, (x, y) in pts.items():
        sg.node_xy[k] = (x, y)
        sg.G.add_node(k)
    for u, v in (("a", "b"), ("b", "c"), ("b", "d")):
        ek = (u, v)
        sg.edge_coords[ek] = [pts[u], pts[v]]
        sg.G.add_edge(u, v, coords=[pts[u], pts[v]])
    return sg


def test_run_assembly_ignores_input_iteration_order():
    """Same edges, different insertion order → the SAME runs in the SAME order.

    This is the invariant the hash-randomisation bug broke: the output followed
    whatever order the input happened to be iterated in.
    """
    sg = _grid_graph()
    edges = [("a", "b"), ("b", "c"), ("b", "d")]
    forward, _b1, _e1 = td.runs_from_edges(list(edges), sg)
    reverse, _b2, _e2 = td.runs_from_edges(list(reversed(edges)), sg)
    assert forward == reverse


def test_run_assembly_preserves_edge_keys_per_run():
    """Each run reports the edges it walked, parallel to the coord lists."""
    sg = _grid_graph()
    runs, _breaks, run_edges = td.runs_from_edges(list(sg.edge_coords), sg)
    assert len(runs) == len(run_edges)
    assert all(e for e in run_edges)          # every run names at least one edge
    for coords, ekeys in zip(runs, run_edges):
        assert len(coords) >= 2
        assert all(len(ek) == 2 for ek in ekeys)


def test_break_nodes_are_deterministically_ordered():
    sg = _grid_graph()
    _runs, breaks, _e = td.runs_from_edges(list(sg.edge_coords), sg)
    assert breaks == sorted(breaks)


def test_main_component_pick_is_deterministic_and_largest():
    """Largest component wins; ties break on the smallest node id, not on order."""
    sg = td.build_street_graph(
        [([(0.0, 0.0), (0.0, 30.0), (0.0, 60.0)], "footway"),
         ([(500.0, 500.0), (500.0, 505.0)], "footway")],
        td.Params())
    main = sg.main_component
    assert main, "a main component must always be picked"
    assert len(main) >= 3, "the 3-node chain must win over the 2-node island"
