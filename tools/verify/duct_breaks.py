# -*- coding: utf-8 -*-
"""Is every duct break AT a chamber, or are some mid-span?

The rule: a duct may only break at a chamber — between two chambers the same
duct must be continuous. For each pair of disconnected pieces of duct linework,
find the two closest points, attach each to the duct END it is part of, and read
that end's chamber label. Same chamber at both sides => the break is at a
chamber (allowed). Anything else => a break in open ground (not allowed).

Usage:
    unset PYTHONPATH && python tmp/duct_breaks.py <run_dir> [max_gap_m]
"""
import os
import sys
from collections import Counter

from osgeo import ogr
from shapely import wkb
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

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


def ends_with_labels(feats):
    """Every polyline end of every feature, with its chamber label."""
    out = []
    for f, g in feats:
        ps = list(g.geoms) if hasattr(g, "geoms") else [g]
        for p in ps:
            cs = list(p.coords)
            if len(cs) < 2:
                continue
            out.append((cs[0], f.get("START_CHAMBER"), f.get("SPAN_KIND")))
            out.append((cs[-1], f.get("END_CHAMBER"), f.get("SPAN_KIND")))
    return out


def main():
    run_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    max_gap = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0
    for fn in LAYERS:
        path = os.path.join(run_dir, fn)
        if not os.path.exists(path):
            continue
        feats = rows(path)
        u = unary_union([g for _f, g in feats])
        parts = [p for p in (u.geoms if hasattr(u, "geoms") else [u])
                 if not p.is_empty and p.length > 0.005]
        ends = ends_with_labels(feats)
        from shapely.geometry import Point
        end_pts = [Point(e[0]) for e in ends]
        tree = STRtree(end_pts)

        print()
        print("=" * 74)
        print("%s  —  %d features, %d disconnected piece(s)"
              % (os.path.splitext(fn)[0], len(feats), len(parts)))

        # connect pieces into groups within max_gap so we only look at real
        # neighbour breaks, not opposite ends of the design
        groups = list(range(len(parts)))

        def find(i):
            while groups[i] != i:
                groups[i] = groups[groups[i]]
                i = groups[i]
            return i

        # For each piece, the break it is part of is the gap to its NEAREST
        # other piece. Classify that one break per piece.
        at_chamber = mid_span = unknown = 0
        near_buckets = Counter()
        gaps = []
        worst = []
        for i in range(len(parts)):
            best = None
            for j in range(len(parts)):
                if i == j:
                    continue
                d = parts[i].distance(parts[j])
                if best is None or d < best[0]:
                    best = (d, j)
            if best is None:
                continue
            d, j = best
            gaps.append(d)
            if d <= 2.0:
                near_buckets["<=2 m (technical)"] += 1
            elif d <= 10.0:
                near_buckets["2-10 m"] += 1
            elif d <= 40.0:
                near_buckets["10-40 m"] += 1
            else:
                near_buckets[">40 m (isolated)"] += 1
            try:
                cij, cji = nearest_points(parts[i], parts[j])
            except Exception:
                unknown += 1
                continue
            li = _label_at(cij, end_pts, ends, tree)
            lj = _label_at(cji, end_pts, ends, tree)
            if li is None or lj is None:
                unknown += 1
            elif li and lj and str(li) == str(lj):
                at_chamber += 1
            else:
                mid_span += 1
                worst.append((d, li, lj))
        n = at_chamber + mid_span + unknown
        print("  one break per piece (nearest other piece) : %d" % n)
        print("    nearest gap bucket : %s" % dict(near_buckets))
        print("    break AT a chamber (both ends name it) : %d" % at_chamber)
        print("    break MID-SPAN / other chamber / unlabelled : %d" % mid_span)
        print("    end not resolvable to a feature end : %d" % unknown)
        if gaps:
            s = sorted(gaps)
            print("  nearest-other-piece gap p50 %.2f | max %.2f m"
                  % (s[len(s) // 2], s[-1]))
        worst.sort(reverse=True)
        for d, li, lj in worst[:5]:
            print("      %.2f m  labels: %r vs %r" % (d, li, lj))


def _label_at(pt, end_pts, ends, tree):
    if not end_pts:
        return None
    idx = tree.nearest(pt)
    d = end_pts[idx].distance(pt)
    if d > 1.0:
        return None
    return ends[idx][1]


if __name__ == "__main__":
    main()
