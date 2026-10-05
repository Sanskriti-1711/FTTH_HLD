"""Fast length-weighted "is this duct on the trench?" measurement.

`on_trench_check.py` computes `geom.difference(corridor).length` per feature,
where the corridor is a buffered union of every trench line. On a city-scale run
that union is a huge multipolygon and the difference is O(huge) per duct —
minutes to hours. This measures the same thing by SAMPLING: walk each duct at
`step_m`, ask an STRtree of the trench lines for the nearest one, and count a
sample as off the network when its distance exceeds `tol`. An off sample counts
its share of the duct length, so the reported `off_%` is length-weighted.

Usage: python tmp/duct_off_trench_fast.py <run_dir> [tol_m] [step_m]
"""
import math
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from osgeo import ogr  # noqa: E402
from shapely import wkb  # noqa: E402
from shapely.geometry import Point  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402

LAYERS = [
    ("Feeder_Ducts.gpkg", "feeder_ducts"),
    ("Distribution_Ducts.gpkg", "distribution_ducts"),
    ("Drop_Ducts.gpkg", "drop_ducts"),
    ("Feeder_Cable.gpkg", "feeder_cable"),
    ("Distribution_Cable.gpkg", "distribution_cable"),
]


def load(path, lines_only=False):
    if not os.path.exists(path):
        return []
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        try:
            geom = wkb.loads(bytes(g.ExportToWkb()))
        except Exception:
            continue  # a degenerate row (0/1-point part) is not measurable
        if geom.is_empty:
            continue
        if lines_only and geom.geom_type not in ("LineString", "MultiLineString"):
            continue
        out.append(geom)
    ds = None
    return out


def iter_lines(geom):
    if geom.geom_type == "LineString":
        if len(geom.coords) >= 2:
            yield list(geom.coords)
    elif geom.geom_type == "MultiLineString":
        for part in geom.geoms:
            if len(part.coords) >= 2:
                yield list(part.coords)


def walk(coords, step):
    """Yield points along a coordinate list, roughly every `step` metres."""
    for i in range(len(coords) - 1):
        (x0, y0), (x1, y1) = coords[i], coords[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(seg // step))
        for k in range(n):
            t = k / n
            yield (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
    if coords:
        yield coords[-1]


def main():
    d = sys.argv[1]
    tol = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    step = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0

    trench = load(os.path.join(d, "Final_Trenches.gpkg"), lines_only=True)
    if not trench:
        print("no Final_Trenches.gpkg in", d)
        return 1
    tree = STRtree(trench)
    print(f"trenches: {len(trench)} feature(s); corridor = +{tol} m; step {step} m")
    print()
    print(f"{'layer':<22}{'n':>5}{'len_m':>11}{'off_m':>11}{'off_%':>8}{'>1m':>6}")
    for fname, label in LAYERS:
        feats = load(os.path.join(d, fname), lines_only=True)
        if not feats:
            print(f"{label:<22}{'-':>5}   (missing)")
            continue
        total = off = 0.0
        over = 0
        for geom in feats:
            length = geom.length
            total += length
            pts = []
            for line in iter_lines(geom):
                pts.extend(walk(line, step))
            if not pts:
                continue
            share = length / len(pts)
            off_len = 0.0
            for pt in pts:
                pg = Point(pt)
                idx = tree.nearest(pg)
                if idx is None or trench[idx].distance(pg) > tol:
                    off_len += share
            off += min(off_len, length)
            if off_len > 1.0:
                over += 1
        print(f"{label:<22}{len(feats):>5}{total:>11.1f}{off:>11.1f}"
              f"{(100.0 * off / total if total else 0):>7.1f}%{over:>6}")
    print()
    print("verdict: a layer is on the trench only when off_% is ~0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
