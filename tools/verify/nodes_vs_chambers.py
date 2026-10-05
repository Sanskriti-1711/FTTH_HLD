"""Are the trench nodes the chambers, or the candidates for them?

Answers, per NODE_TYPE: how many nodes exist, how many carry a chamber within
``TOL`` metres, and how many chambers sit on no node at all. Only HDD pits,
junctions and PDPs need a structure — a bend or a pull point is a trench
feature, not a place you dig a hole.

Usage: python tmp/nodes_vs_chambers.py <run_dir> [tol_m]
"""
import sys
from collections import Counter

from osgeo import ogr

TOL = 2.0
src = ogr.Open  # noqa: F841  (kept explicit for readability)


def load(path):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    rows = []
    for f in lyr:
        g = f.geometry()
        rows.append({"props": {n: f[n] for n in names},
                     "x": g.GetX() if g else None,
                     "y": g.GetY() if g else None})
    return rows


def main():
    run = sys.argv[1].rstrip("/\\") + "/"
    tol = float(sys.argv[2]) if len(sys.argv) > 2 else TOL

    nodes = load(run + "Trench_Nodes.gpkg")
    chambers = load(run + "Chambers.gpkg")

    print("trench nodes : %d" % len(nodes))
    print("chambers     : %d   (tolerance %.1f m)\n" % (len(chambers), tol))

    # Which node does each chamber sit on?
    chamber_node = []
    for c in chambers:
        best, dist = None, None
        for n in nodes:
            if n["x"] is None or c["x"] is None:
                continue
            d = ((n["x"] - c["x"]) ** 2 + (n["y"] - c["y"]) ** 2) ** 0.5
            if dist is None or d < dist:
                best, dist = n, d
        chamber_node.append((c, best, dist))

    on = [(c, n, d) for c, n, d in chamber_node if d is not None and d <= tol]
    off = [(c, n, d) for c, n, d in chamber_node if d is None or d > tol]

    by_type = Counter(n["props"].get("NODE_TYPE") for n in nodes)
    hit_type = Counter(n["props"].get("NODE_TYPE") for _c, n, _d in on)
    print("chambers placed ON a node (<= %.1f m): %d of %d" % (tol, len(on), len(chambers)))
    print("  %-10s %8s %8s %8s" % ("NODE_TYPE", "nodes", "withHH", "missing"))
    for t in sorted(by_type):
        missing = by_type[t] - hit_type.get(t, 0)
        flag = "   <- no structure needed" if t in ("BEND", "PULL") else ""
        print("  %-10s %8d %8d %8d%s"
              % (t, by_type[t], hit_type.get(t, 0), missing, flag))
    print("  %-10s %8d %8d %8d" % ("TOTAL", len(nodes), len(on), len(nodes) - len(on)))

    print("\nchamber SUBTYPE vs the node that hosts it:")
    combos = Counter((c["props"].get("SUBTYPE"),
                      (n["props"].get("NODE_TYPE") if n else "NO NODE"))
                     for c, n, _d in on + off)
    for (sub, nt), k in sorted(combos.items(), key=lambda kv: -kv[1]):
        print("  %-10s on %-12s %4d" % (sub, nt, k))

    if off:
        print("\nchambers NOT on a node (%.1f m): %d" % (tol, len(off)))
        for c, n, d in off[:8]:
            dd = "n/a" if d is None else "%.2f m" % d
            print("   %-10s %-8s nearest node %s"
                  % (c["props"].get("STRUCT_ID"), c["props"].get("SUBTYPE"), dd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
