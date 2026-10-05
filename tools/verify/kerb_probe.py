"""Does the kerb band actually take the trench off the carriageway centreline?

Builds the designer's own graph twice — kerb band on and off — routes the same
pair of nodes across the reported stretch, and measures the DRAWN geometry
against the OSM carriageway centrelines. Distance ~0 m = down the middle of the
road; ~KERB_OFFSET_M = laid at the kerb.

    unset PYTHONPATH && python tmp/kerb_probe.py
"""
import math
import sys

sys.path.insert(0, "HLD_Planning_01/HLDPlanning/design")
import trench_design as td                               # noqa: E402
from shapely.geometry import LineString, Point           # noqa: E402
from shapely.strtree import STRtree                      # noqa: E402

ROADS = "tmp/roads_osm/roads_aoi_25833.gpkg"

# the stretch the user reported: TR-000079.v2 -> TR-000080 (40 m in the road)
CASES = [
    ("TR-000079/80 PDP00002", (389607.20, 5812093.20), (389637.06, 5812098.93)),
    ("TR-000091", (389232.18, 5811844.71), (389235.86, 5811848.03)),
    ("TR-000094", (389021.98, 5811892.96), (388987.48, 5811909.97)),
]

params = td.Params()
walk, veh = td._read_road_parts(ROADS, params.target_epsg, None)
veh_lines = [LineString(c) for c, _ in veh if len(c) >= 2]
veh_idx = STRtree(veh_lines)
print("road parts: %d walkable, %d carriageway" % (len(walk), len(veh)))

for label, a, b in CASES:
    print("\n--- %s" % label)
    for tag, p in (("kerb OFF", td.Params(kerb_offset_m=0.0)),
                   ("kerb ON ", td.Params())):
        sg = td.build_street_graph(walk, p)
        ka, kb = sg.nearest_node(*a, 60.0), sg.nearest_node(*b, 60.0)
        path = td._route(sg.G, ka, kb) if (ka and kb) else None
        if not path:
            print("   %s no route" % tag)
            continue
        pts = []
        for i in range(len(path) - 1):
            ek = (path[i], path[i + 1]) if path[i] < path[i + 1] else (path[i + 1], path[i])
            pts.extend(sg.edge_coords[ek])
        cls = {sg.G[path[i]][path[i + 1]]["cls"] for i in range(len(path) - 1)}
        d_car = [min((veh_lines[j].distance(Point(pt))
                      for j in veh_idx.query(Point(pt).buffer(60.0, 4))),
                     default=1e9) for pt in pts]
        # length of the drawn route
        ln = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                 for i in range(len(pts) - 1))
        print("   %s  edges %2d  classes %-28s drawn %.1f m | distance to the "
              "nearest carriageway centreline: min %.2f  median %.2f  max %.2f"
              % (tag, len(path) - 1, ",".join(sorted(cls)), ln,
                 min(d_car), sorted(d_car)[len(d_car) // 2], max(d_car)))
