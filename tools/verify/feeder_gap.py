# -*- coding: utf-8 -*-
"""Find the gap(s) that split the feeder duct, with the chamber labels.

    unset PYTHONPATH && python tmp/feeder_gap.py <run_dir> [layer] [min_gap]
"""
import os
import sys

from osgeo import ogr
from shapely import wkb
from shapely.ops import nearest_points, unary_union

MIN_GAP = 0.05


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


def main():
    run_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    fn = sys.argv[2] if len(sys.argv) > 2 else "Feeder_Ducts.gpkg"
    min_gap = float(sys.argv[3]) if len(sys.argv) > 3 else MIN_GAP
    max_gap = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
    feats = rows(os.path.join(run_dir, fn))
    if not feats:
        raise SystemExit("cannot read %s" % fn)

    # End points with their chamber labels, to name the gap.
    ends = []
    for f, g in feats:
        ps = list(g.geoms) if hasattr(g, "geoms") else [g]
        for p in ps:
            cs = list(p.coords)
            if len(cs) >= 2:
                ends.append((cs[0], f.get("START_CHAMBER"), f.get("SPAN_KIND")))
                ends.append((cs[-1], f.get("END_CHAMBER"), f.get("SPAN_KIND")))

    u = unary_union([g for _f, g in feats])
    pieces = [p for p in (u.geoms if hasattr(u, "geoms") else [u])
              if not p.is_empty and p.length > 0.005]
    print("%s : %d features -> %d exactly-touching pieces" % (fn, len(feats), len(pieces)))

    gaps = []
    for i in range(len(pieces)):
        for j in range(i + 1, len(pieces)):
            d = pieces[i].distance(pieces[j])
            if d < 1e-9 or d > max_gap:
                continue
            a, b = nearest_points(pieces[i], pieces[j])
            gaps.append((d, a, b, i, j, pieces[i].length, pieces[j].length))
    gaps.sort(key=lambda t: t[0], reverse=True)
    print("gaps <=%.0f m between pieces : %d" % (max_gap, len(gaps)))
    print()
    print("largest gaps (m) | piece lengths | chamber label at each side")
    shown = 0
    for d, a, b, _i, _j, la, lb in gaps:
        if d < min_gap:
            continue
        lab_a = _label(ends, a)
        lab_b = _label(ends, b)
        print("   %5.3f m | %7.1f m / %7.1f m | %s  <->  %s"
              % (d, la, lb, lab_a, lab_b))
        shown += 1
        if shown >= 12:
            break
    if not shown:
        print("   (none above %.2f m)" % min_gap)


def _xy(pt):
    """Points expose .x/.y as properties; some builds return plain tuples."""
    x = getattr(pt, "x", None)
    y = getattr(pt, "y", None)
    if x is None or y is None:
        return pt[0], pt[1]
    return x, y


def _label(ends, pt):
    px, py = _xy(pt)
    best = None
    for xy, cid, kind in ends:
        d = ((xy[0] - px) ** 2 + (xy[1] - py) ** 2) ** 0.5
        if best is None or d < best[0]:
            best = (d, cid, kind)
    if best is None:
        return "?"
    d, cid, kind = best
    return "%s [%s] (%.2f m from end)" % (cid or "-", kind or "-", d)


if __name__ == "__main__":
    main()
