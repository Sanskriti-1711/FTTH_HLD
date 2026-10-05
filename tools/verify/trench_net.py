"""Authoritative trench connectivity, feature level (no endpoint welding).

Two trench features belong to the same network component when they physically
touch (GEOS distance <= tol).  Then: do the MFG and every PDP land on ONE
component?  This is the question "are the trenches connecting all the PDPs?"
"""
import sys
from collections import defaultdict
from osgeo import ogr

RUN = sys.argv[1] if len(sys.argv) > 1 else (
    "HLD_Planning_01/web/backend/outputs/ductfix2_1789861240")
TOL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
LAYER = sys.argv[3] if len(sys.argv) > 3 else "Final_Trenches"


def load(path):
    ds = ogr.Open(path)
    if ds is None:
        return None
    lyr = ds.GetLayer(0)
    feats = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        feats.append((g.Clone(), f))
    ds = None
    return feats


trench = load(f"{RUN}/{LAYER}.gpkg")
print(f"{LAYER}: {len(trench)} features  (tol {TOL} m)")

idx = ogr.CreateSpatialIndex() if hasattr(ogr, "CreateSpatialIndex") else None
parent = list(range(len(trench)))


def find(i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra


# bbox prefilter, then GEOS distance
boxes = []
for g, _ in trench:
    e = g.GetEnvelope()   # (minx, maxx, miny, maxy)
    boxes.append(e)
links = 0
for i in range(len(trench)):
    ax0, ax1, ay0, ay1 = boxes[i]
    for j in range(i + 1, len(trench)):
        bx0, bx1, by0, by1 = boxes[j]
        if ax1 + TOL < bx0 or bx1 + TOL < ax0:
            continue
        if ay1 + TOL < by0 or by1 + TOL < ay0:
            continue
        if find(i) == find(j):
            continue
        if trench[i][0].Distance(trench[j][0]) <= TOL:
            union(i, j)
            links += 1
print(f"welds (feature-to-feature touches): {links}")

comps = defaultdict(list)
for i in range(len(trench)):
    comps[find(i)].append(i)
ranked = sorted(comps.values(), key=len, reverse=True)
lengths = [sum(trench[i][0].Length() for i in c) for c in ranked]
print(f"*** {len(ranked)} connected component(s) ***")
print(f"    component sizes (features): {[len(c) for c in ranked[:12]]}")
print(f"    component lengths (m)     : {[round(x, 1) for x in lengths[:12]]}")
print(f"    largest holds {lengths[0]/sum(lengths)*100:.1f}% of the network length")

comp_of = {}
for ci, c in enumerate(ranked):
    for i in c:
        comp_of[i] = ci


def locate(src, idf):
    rows = load(src)
    if rows is None:
        print(f"{src}: MISSING")
        return
    where = defaultdict(list)
    off = []
    for g, f in rows:
        pid = str(f[idf] or "?")
        hit = None
        for i, (tg, _) in enumerate(trench):
            if tg.Distance(g) <= 0.5:
                hit = i
                break
        if hit is None:
            off.append(pid)
        else:
            where[comp_of[hit]] = where[comp_of[hit]] + [pid]
    print(f"\n{idf}: {sum(len(v) for v in where.values())} on a trench (0.5 m), "
          f"{len(off)} off any trench {off[:8]}")
    for ci in sorted(where):
        tag = "MAIN" if ci == 0 else f"comp#{ci}"
        print(f"   {tag:8s} ({lengths[ci]:8.1f} m, {len(ranked[ci])} feats): "
              f"{len(where[ci])} -> {sorted(where[ci])[:10]}")


locate(f"{RUN}/PDPs.gpkg", "PDP_ID")
locate(f"{RUN}/MFG.gpkg", "MFG_ID")
