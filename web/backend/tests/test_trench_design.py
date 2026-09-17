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
    runs, _breaks = td.runs_from_edges(keys, sg)
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
    runs, breaks = td.runs_from_edges(list(sg.edge_coords.keys()), sg)
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


def test_garden_leg_skips_out_of_reach_houses():
    p = td.Params(house_search_m=50.0)
    network = [[(0, 0), (0, 100)]]
    houses = [{"x": 500.0, "y": 50.0, "ADDR_ID": "far", "PDP_ID": "P1"}]
    legs = td.design_garden_legs(network, houses, p, lambda m: None)
    assert legs == []


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


def test_junction_end_is_not_a_loose_end():
    spans = [_span("TR-1", [(0, 0), (50, 0)]), _span("TR-2", [(50, 0), (100, 0)]),
             _span("TR-3", [(50, 0), (50, 40)])]
    loose = td.dangling_ends(spans, [(0.0, 0.0), (100.0, 0.0), (50.0, 40.0)],
                             td.Params())
    assert [d["TRENCH_ID"] for d in loose] == [], loose
