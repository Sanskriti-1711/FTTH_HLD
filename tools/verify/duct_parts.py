# -*- coding: utf-8 -*-
"""How many parts is the duct in, and are the breaks only at chamber joints?

A duct is meant to run chamber to chamber: consecutive spans of the same duct
run must MEET at the chamber they share. This measures, per layer:

  * connected components of the duct geometry (features joined when they come
    within TOUCH_M of each other) — "how many parts is it in"
  * chamber joints that are OPEN: two spans of the same run that name the same
    chamber but do not meet there, with the gap
  * spans whose labelled chamber is more than 1 m from that end

Usage:
    unset PYTHONPATH && python tmp/duct_parts.py <run_dir> [touch_m]
"""
import os
import sys
from collections import Counter, defaultdict

from osgeo import ogr
from shapely import wkb

TOUCH_M = 1.0


def rows(path):
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    out = []
    for ft in lyr:
        g = ft.GetGeometryRef()
        if g is None:
            continue
        out.append(({n: ft.GetField(n) for n in names},
                    wkb.loads(bytes(g.ExportToWkb()))))
    return out


def parts_of(geom):
    return list(geom.geoms) if hasattr(geom, "geoms") else [geom]


def ends_of(geom):
    ps = parts_of(geom)
    return [ps[0].coords[0], ps[-1].coords[-1]]


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def check_layer(run_dir, fname, touch_m):
    path = os.path.join(run_dir, fname)
    if not os.path.exists(path):
        return
    feats = rows(path)
    label = os.path.splitext(fname)[0]
    print("=" * 72)
    print("%s  —  %d features" % (label, len(feats)))
    if not feats:
        return

    # ---- connected components over geometric touching -------------------
    # Bucket by rounded coordinate so this stays O(n) rather than O(n^2).
    dsu = DSU(len(feats))
    cell = max(touch_m, 0.5)
    buckets = defaultdict(list)
    for i, (_a, g) in enumerate(feats):
        for p in parts_of(g):
            for x, y in p.coords:
                buckets[(round(x / cell), round(y / cell))].append(i)
    for idxs in buckets.values():
        for i in idxs[1:]:
            dsu.union(idxs[0], i)
    # widen: join features whose geometries come within touch_m (parallel runs)
    for (cx, cy), idxs in list(buckets.items()):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                other = buckets.get((cx + dx, cy + dy))
                if not other:
                    continue
                for i in idxs:
                    for j in other:
                        if i == j or dsu.find(i) == dsu.find(j):
                            continue
                        if feats[i][1].distance(feats[j][1]) <= touch_m:
                            dsu.union(i, j)
    comps = Counter(dsu.find(i) for i in range(len(feats)))
    print("  connected parts (touch <= %.1f m) : %d" % (touch_m, len(comps)))
    sizes = sorted(comps.values(), reverse=True)
    print("  part sizes                        : %s"
          % (sizes[:12] + (["..."] if len(sizes) > 12 else [])))
    # per-run view
    runs = defaultdict(list)
    for i, (a, _g) in enumerate(feats):
        key = str(a.get("RUN_ID") or a.get("DUCT_ID") or "")
        runs[key].append(i)
    broken_runs = 0
    for key, idxs in runs.items():
        roots = {dsu.find(i) for i in idxs}
        if len(roots) > 1:
            broken_runs += 1
    print("  runs                              : %d  (in >1 part: %d)"
          % (len(runs), broken_runs))

    # ---- chamber joints -------------------------------------------------
    # Features that name the same chamber should meet at it.
    by_chamber = defaultdict(list)
    for i, (a, g) in enumerate(feats):
        for field in ("START_CHAMBER", "END_CHAMBER"):
            cid = a.get(field)
            if cid:
                e = ends_of(g)[0 if field == "START_CHAMBER" else -1]
                by_chamber[str(cid)].append((i, e, field))
    open_joints = 0
    joint_gaps = []
    worst = []
    for cid, entries in by_chamber.items():
        if len(entries) < 2:
            continue
        far = 0
        for i, e, field in entries:
            for j, e2, field2 in entries:
                if i >= j:
                    continue
                d = ((e[0] - e2[0]) ** 2 + (e[1] - e2[1]) ** 2) ** 0.5
                if d > 1.0:
                    far += 1
                    joint_gaps.append(d)
                    worst.append((d, cid, i, j))
        if far:
            open_joints += 1
    print("  chambers named by >=2 spans        : %d"
          % len([c for c, v in by_chamber.items() if len(v) >= 2]))
    print("  chambers where those spans DO NOT meet (>1 m): %d" % open_joints)
    if joint_gaps:
        s = sorted(joint_gaps)
        print("  joint gap p50 / p90 / max          : %.2f / %.2f / %.2f m"
              % (s[len(s) // 2], s[max(0, len(s) // 10)], s[-1]))
        worst.sort(reverse=True)
        print("  worst joints (gap, chamber):")
        for d, cid, i, j in worst[:5]:
            print("     %7.2f m  %s  (feature %d <-> %d)" % (d, cid[:24], i, j))


def main():
    run_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    touch = float(sys.argv[2]) if len(sys.argv) > 2 else TOUCH_M
    for fn in ("Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg"):
        check_layer(run_dir, fn, touch)


if __name__ == "__main__":
    main()
