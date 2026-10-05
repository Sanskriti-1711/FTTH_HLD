"""Is the duct network a continuous chain along the trenches?

Builds a graph from the duct geometry (vertices snapped within 0.5 m), then
asks the questions the field asks:

  feeder       : is every PDP reachable from the MFG along feeder duct?
  feeder alone : same, but only spans whose owner trench tier is Feeder
  distribution : does every coupler reach its PDP's chamber along the ducts?

Usage: python tmp/duct_connectivity.py <run_dir>
"""
import sys
from collections import defaultdict
from pathlib import Path

from osgeo import ogr

ogr.UseExceptions()
SNAP = 0.5


def xy_of(g):
    return [(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())]


def parts(geom):
    g = geom.Clone()
    try:
        g = g.GetLinearGeometry()
    except Exception:
        pass
    if g.GetGeometryName() == "LINESTRING":
        return [g]
    if g.GetGeometryName() == "MULTILINESTRING":
        return [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
    return []


_KEEP = []


def segs(ds, layer_name):
    _KEEP.append(ds)
    lyr = ds.GetLayerByName(layer_name) if layer_name else ds.GetLayer(0)
    out = []
    layer = lyr
    for f in layer:
        g = f.GetGeometryRef()
        if g is None:
            continue
        for p in parts(g):
            xy = xy_of(p)
            for i in range(len(xy) - 1):
                out.append((xy[i], xy[i + 1]))
    return out


def d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return d(p, a)
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return d(p, (a[0] + t * dx, a[1] + t * dy))


def build(segments):
    """Snap segment endpoints into nodes; return (adj, node_xy, find)."""
    nodes = []
    parent = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def node_at(pt):
        for i, q in enumerate(nodes):
            if d(pt, q) <= SNAP:
                return i
        nodes.append(pt)
        parent[len(nodes) - 1] = len(nodes) - 1
        return len(nodes) - 1

    edges = []
    for a, b in segments:
        ia, ib = node_at(a), node_at(b)
        if ia == ib:
            continue
        edges.append((ia, ib))
    for ia, ib in edges:
        union(ia, ib)
    # merge nodes that snapping left distinct but are within SNAP of each other
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if d(nodes[i], nodes[j]) <= SNAP:
                union(i, j)
    return nodes, find


def nearest(pt, nodes):
    if not nodes:
        return None
    return min(range(len(nodes)), key=lambda i: d(pt, nodes[i]))


def points(path, name):
    ds = ogr.Open(str(path / f"{name}.gpkg"))
    if ds is None:
        return []
    _KEEP.append(ds)
    lyr = ds.GetLayer(0)
    idf = next((n for n in ("SRC_ID", "PDP_ID", "MFG_ID", "DUCT_UID", "STRUCT_ID", "id")
                if lyr.GetLayerDefn().GetFieldIndex(n) >= 0), None)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        out.append(((g.GetX(), g.GetY()), str(f.GetField(idf) if idf else f.GetFID())))
    return out


def main():
    run = Path(sys.argv[1])
    feeder = segs(ogr.Open(str(run / "Feeder_Ducts.gpkg")), None) \
        if (run / "Feeder_Ducts.gpkg").exists() else []
    dist = segs(ogr.Open(str(run / "Distribution_Ducts.gpkg")), None) \
        if (run / "Distribution_Ducts.gpkg").exists() else []
    ducts_all = feeder + dist

    mfg = points(run, "MFG")
    pdps = points(run, "PDPs")
    couplers = points(run, "Coupleurs")

    def reach(segs_, label):
        if not segs_ or not mfg or not pdps:
            return
        nds, fnd = build(segs_)
        m = nearest(mfg[0][0], nds)
        root = fnd(m)
        ok, bad = 0, []
        for p, n in pdps:
            i = nearest(p, nds)
            if fnd(i) == root:
                ok += 1
            else:
                bad.append((n, round(d(p, nds[i]), 1)))
        comps = defaultdict(int)
        for i in range(len(nds)):
            comps[fnd(i)] += 1
        print(f"  {label}: {len(nds)} nodes / {len(comps)} components "
              f"(largest {max(comps.values())})")
        print(f"    PDPs reachable from MFG: {ok}/{len(pdps)}"
              + (f"   unreachable: {bad[:10]}" if bad else ""))

    reach(feeder, "FEEDER ducts only")
    reach(dir and ducts_all, "feeder + distribution")

    # distribution: does each coupler sit ON the distribution duct network?
    if couplers and dist:
        dnodes, dfind = build(dist)
        un = 0
        worst = []
        for cp, cid in couplers:
            # point-to-segment distance (a coupler taps a duct mid-span)
            lin = min(_pt_seg(cp, a, b) for a, b in dist)
            worst.append(lin)
            if lin > 1.0:
                un += 1
        s = sorted(worst)
        print(f"  couplers >1 m off the distribution duct line: {un}/{len(couplers)}  "
              f"p50 {s[len(s)//2]:.2f}  p90 {s[int(len(s)*0.9)]:.2f}  max {max(s):.2f} m")
        comps = defaultdict(int)
        for i in range(len(dnodes)):
            comps[dfind(i)] += 1
        print(f"  distribution duct components: {len(comps)} "
              f"(largest {max(comps.values())} nodes, "
              f"{sum(1 for v in comps.values() if v == 1)} singletons)")


if __name__ == "__main__":
    main()
