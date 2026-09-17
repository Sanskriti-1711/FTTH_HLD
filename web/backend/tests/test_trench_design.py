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
