# -*- coding: utf-8 -*-
"""Does each duct follow a trench path? Measure, do not assert.

For every duct feature:
  * L_off   = length of the duct OUTSIDE the trench corridor (trench union +0.5 m)
              > 0 means the duct leaves the trench network
  * rider   = the trench it overlaps most (the path it actually follows)
  * cover   = share of the duct length that single trench covers

Usage:
    unset PYTHONPATH && python tmp/duct_trench_coincidence.py <run_dir>
"""
import os
import sys
from collections import Counter, defaultdict

from osgeo import ogr
from shapely import wkb
from shapely.ops import unary_union
from shapely.strtree import STRtree

TOL = 0.5


def load(path):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    rows = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        geom = wkb.loads(bytes(g.ExportToWkb()))
        rows.append(({n: f.GetField(n) for n in names}, geom))
    return names, rows


def med(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[len(xs) // 2]


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def main(run_dir):
    _, trenches = load(os.path.join(run_dir, "Final_Trenches.gpkg"))
    print("trenches: %d" % len(trenches))
    tgeoms = [g for _, g in trenches]
    tunion = unary_union(tgeoms)
    corridor = tunion.buffer(TOL, cap_style=2, join_style=2)
    tb = [g.buffer(TOL, cap_style=2, join_style=2) for g in tgeoms]
    tree = STRtree(tb)

    for fname, label in (("Feeder_Ducts.gpkg", "feeder"),
                         ("Distribution_Ducts.gpkg", "distribution"),
                         ("Drop_Ducts.gpkg", "drop")):
        path = os.path.join(run_dir, fname)
        if not os.path.exists(path):
            continue
        names, ducts = load(path)
        total = 0.0
        off_total = 0.0
        offs = []
        riders = Counter()
        covers = []
        multi = 0
        print("\n" + "=" * 72)
        print("%s — %d ducts" % (fname, len(ducts)))
        for attrs, g in ducts:
            L = g.length
            if L <= 0:
                continue
            total += L
            off = g.difference(corridor).length
            off_total += off
            offs.append(off)
            # which trench does it ride?
            try:
                cand = tree.query(g.buffer(TOL, cap_style=2, join_style=2),
                                  predicate="intersects")
            except TypeError:
                cand = tree.query(g.buffer(TOL, cap_style=2, join_style=2))
            best = None
            hits = []
            for i in cand:
                try:
                    ov = g.intersection(tb[int(i)]).length
                except Exception:
                    continue
                if ov <= 0:
                    continue
                hits.append((ov, int(i)))
                if best is None or ov > best[0]:
                    best = (ov, int(i))
            if best is None:
                riders["<none>"] += 1
                covers.append(0.0)
                continue
            ov, i = best
            a = trenches[i][0]
            tier = a.get("SERVES_TIER") or a.get("TRENCH_TIER") or "?"
            riders[str(tier)] += 1
            covers.append(ov / L)
            # how many distinct trenches does it need?
            need = [h for h in hits if h[0] / L > 0.05]
            if len(need) > 1:
                multi += 1
        print("  total length            : %10.1f m" % total)
        print("  length OFF the trench   : %10.1f m  (%.1f%% of all duct length)"
              % (off_total, 100.0 * off_total / total if total else 0))
        over = sum(1 for o in offs if o > 1.0)
        print("  ducts with >1 m off     : %d of %d" % (over, len(offs)))
        print("  off-trench length  p50  : %8.2f m" % med(offs))
        print("  off-trench length  p90  : %8.2f m" % pct(offs, 90))
        print("  off-trench length  max  : %8.2f m" % (max(offs) if offs else 0))
        print("  single-trench cover p50 : %8.1f %%" % (100 * med(covers)))
        print("  ducts needing >1 trench : %d" % multi)
        print("  ridden SERVES_TIER      :", dict(riders))


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else \
        "HLD_Planning_01/web/backend/outputs/f0426f446acd4b02ada8595e1bb3e3a9"
    main(d)
