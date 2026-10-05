"""Where does the feeder chain break — the trench, the cable or the duct?

For each network layer, builds a graph at a sweep of snap tolerances and
reports how many components there are and the largest gap between components.

Usage: python tmp/feeder_chain_break.py <run_dir>
"""
import sys
from collections import defaultdict
from pathlib import Path

from osgeo import ogr

ogr.UseExceptions()


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


def d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def pts(path, name, need=()):
    ds = ogr.Open(str(path / f"{name}.gpkg"))
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    idf = next((n for n in ("SRC_ID", "PDP_ID", "MFG_ID", "id") if n in names), None)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        keep = all((f.GetField(k) in (None, "", "Feeder")) if k in names else True
                   for k in need)
        out.append(((g.GetX(), g.GetY()), str(f.GetField(idf) if idf else ""), keep))
    return out


def segments(path, name):
    ds = ogr.Open(str(path / f"{name}.gpkg"))
    if ds is None:
        return []
    out = []
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        if g is None:
            continue
        for p in parts(g):
            xy = xy_of(p)
            for i in range(len(xy) - 1):
                out.append((xy[i], xy[i + 1]))
    return out


def components(segments_, snap):
    nodes, parent = [], {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def node_at(p):
        for i, q in enumerate(nodes):
            if d(p, q) <= snap:
                return i
        nodes.append(p)
        parent[len(nodes) - 1] = len(nodes) - 1
        return len(nodes) - 1

    for a, b in segments_:
        ia, ib = node_at(a), node_at(b)
        if ia != ib:
            union(ia, ib)
    groups = defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(i)
    return nodes, groups


def main():
    run = Path(sys.argv[1])
    mfg = pts(run, "MFG")
    pdps = pts(run, "PDPs")
    layers = {
        "Final_Trenches": segments(run, "Final_Trenches"),
        "Feeder_Cable": segments(run, "Feeder_Cable"),
        "Feeder_Ducts": segments(run, "Feeder_Ducts"),
    }
    m = mfg[0][0] if mfg else None
    for name, segs_ in layers.items():
        print(f"=== {name}: {len(segs_)} segments")
        for snap in (0.5, 1.0, 2.0, 5.0, 10.0, 25.0):
            nodes, groups = components(segs_, snap)
            # which component holds the MFG, and how many PDPs share it
            mi = min(range(len(nodes)), key=lambda i: d(m, nodes[i])) if m else None
            root = None
            if mi is not None:
                for r, mem in groups.items():
                    if mi in mem:
                        root = r
            ok = 0
            for p, _id, _k in pdps:
                pi = min(range(len(nodes)), key=lambda i: d(p, nodes[i]))
                rr = next((r for r, mem in groups.items() if pi in mem), None)
                if rr is not None and rr == root:
                    ok += 1
            print(f"    snap {snap:>5.1f} m: {len(groups)} component(s), "
                  f"{ok}/{len(pdps)} PDPs on the MFG component")
        # biggest inter-component gap
        nodes, groups = components(segs_, 0.5)
        reps = [min((nodes[i] for i in mem), key=lambda p: p[0]) for mem in groups.values()]
        gaps = []
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                gaps.append((d(reps[i], reps[j]), reps[i], reps[j]))
        gaps.sort()
        if gaps:
            print(f"    closest two components are {gaps[0][0]:.2f} m apart")
        print()


if __name__ == "__main__":
    main()
