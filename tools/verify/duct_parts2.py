# -*- coding: utf-8 -*-
"""How many parts is each duct layer in — and are the breaks at chambers?

Two answers, because they differ:
  * RUN SEMANTICS  — the plugin's own `_components()` (segment endpoints snapped
    within _DUCT_SNAP_M). This is the number the run logs.
  * TRUE GEOMETRY  — unary_union of the linework, which NODES crossings, so the
    parts of the union are the genuinely connected pieces.

Then, for each break, the gap and the chamber names at the two nearest ends, so
a break can be shown to be at a chamber (expected) or mid-span (not).

Usage:
    unset PYTHONPATH && PYTHONPATH=HLD_Planning_01 python tmp/duct_parts2.py <run_dir>
"""
import os
import sys

from shapely import wkb
from shapely.ops import unary_union
from osgeo import ogr

from HLDPlanning.utils.attr_enrich import (_DUCT_SNAP_M, _components,
                                           _line_segments)

LAYERS = ("Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg")


def rows(path):
    ds = ogr.Open(path)
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


def chambers_at(feat, geom):
    ps = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    e0, e1 = ps[0].coords[0], ps[-1].coords[-1]
    out = []
    for fld, e in (("START_CHAMBER", e0), ("END_CHAMBER", e1)):
        if feat.get(fld):
            out.append((str(feat[fld]), e))
    return out


def main():
    run_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("endpoint snap tolerance (_DUCT_SNAP_M) : %.3f m" % _DUCT_SNAP_M)
    for fn in LAYERS:
        path = os.path.join(run_dir, fn)
        if not os.path.exists(path):
            continue
        feats = rows(path)
        geoms = [g for _f, g in feats]
        print()
        print("=" * 74)
        print("%s  —  %d features" % (os.path.splitext(fn)[0], len(feats)))

        segs = _line_segments(path)
        _nodes, groups = _components(segs)
        print("  RUN SEMANTICS  (endpoint snap) : %d part(s) from %d segment(s)"
              % (len(groups), len(segs)))

        u = unary_union(geoms)
        parts = list(u.geoms) if hasattr(u, "geoms") else [u]
        parts = [p for p in parts if not p.is_empty and p.length > 0.01]
        print("  TRUE GEOMETRY  (noded union)   : %d part(s)" % len(parts))
        lens = sorted((p.length for p in parts), reverse=True)
        print("  part lengths (m)               : %s"
              % [round(x, 1) for x in lens[:10]])

        # Where is each break? Distance from every other part to the biggest.
        if len(parts) > 1:
            main_part = parts[0]
            print("  breaks (part -> nearest other part):")
            gaps = []
            for p in parts[1:]:
                d = main_part.distance(p)
                gaps.append((d, p.length))
            gaps.sort()
            for d, L in gaps[:6]:
                print("      %.2f m gap to a %.1f m part" % (d, L))
            over = [d for d, _L in gaps if d > 1.0]
            print("      gaps >1 m: %d of %d | p50 %.2f | max %.2f m"
                  % (len(over), len(gaps),
                     sorted(d for d, _ in gaps)[len(gaps) // 2],
                     max(d for d, _ in gaps)))
        # Named chambers that the two ends of a chain do not meet at.
        by_ch = {}
        for f, g in feats:
            for cid, e in chambers_at(f, g):
                by_ch.setdefault(cid, []).append(e)
        opened = 0
        gapvals = []
        for cid, ends in by_ch.items():
            if len(ends) < 2:
                continue
            far = 0.0
            for i in range(len(ends)):
                for j in range(i + 1, len(ends)):
                    d = ((ends[i][0] - ends[j][0]) ** 2
                         + (ends[i][1] - ends[j][1]) ** 2) ** 0.5
                    far = max(far, d)
            if far > 1.0:
                opened += 1
                gapvals.append(far)
        print("  chambers named by >=2 spans    : %d"
              % len([c for c, v in by_ch.items() if len(v) >= 2]))
        print("  ...whose spans do NOT meet there (>1 m): %d" % opened)
        if gapvals:
            s = sorted(gapvals)
            print("      gap median %.2f | max %.2f m"
                  % (s[len(s) // 2], s[-1]))
        # Per-run chaining: does each run's own spans form one chain?
        runs = {}
        for i, (f, _g) in enumerate(feats):
            runs.setdefault(str(f.get("RUN_ID") or ""), []).append(i)


if __name__ == "__main__":
    main()
