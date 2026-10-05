"""How far do chambers sit from the distribution DUCT LINE?

The cut projects each chamber onto the duct and breaks there, so what matters
is the perpendicular distance from the chamber to the duct's segments — not the
distance between vertices.  If chambers are 5-15 m off the line, a 5 m cut
tolerance silently leaves whole runs uncut.

Usage: python tmp/chamber_on_duct.py <output_dir>
"""
import math
import os
import sys

from osgeo import ogr


def vertices(g):
    out = []
    if g is None or g.IsEmpty():
        return out
    wkt = g.ExportToWkt()
    body = wkt[wkt.index("(") + 1:].replace("(", "").replace(")", "")
    for part in body.split(","):
        b = part.strip().split()
        if len(b) >= 2:
            try:
                out.append((float(b[0]), float(b[1])))
            except ValueError:
                pass
    return out


def pt_seg_dist(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_path(p, verts):
    return min((pt_seg_dist(p, verts[i], verts[i + 1])
                for i in range(len(verts) - 1)), default=1e18)


def main(out):
    ds = ogr.Open(os.path.join(out, "Chambers.gpkg"))
    ch = []
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        p = g.Centroid().GetPoint(0)
        ch.append((p[0], p[1], f.GetField("STRUCT_ID")))
    ds = None

    ds = ogr.Open(os.path.join(out, "Distribution_Ducts.gpkg"))
    ducts = []
    for f in ds.GetLayer(0):
        ducts.append((f.GetField("DUCT_ID"), str(f.GetField("SPAN_KIND") or ""),
                      vertices(f.GetGeometryRef())))
    ds = None

    print("=== chamber -> nearest distribution duct LINE ===")
    ds_list = []
    for cx, cy, cid in ch:
        bd, bduct = 1e18, ""
        for did, _k, verts in ducts:
            if len(verts) < 2:
                continue
            d = dist_to_path((cx, cy), verts)
            if d < bd:
                bd, bduct = d, did
        ds_list.append((bd, cid, bduct))
    ds_list.sort()
    vals = [d for d, _c, _u in ds_list]
    print("  n=%d  p50=%.1f  p90=%.1f  max=%.1f  <=5m=%d  <=10m=%d  <=15m=%d  >15m=%d"
          % (len(vals), vals[len(vals) // 2], vals[int(len(vals) * .9)], vals[-1],
             sum(1 for v in vals if v <= 5), sum(1 for v in vals if v <= 10),
             sum(1 for v in vals if v <= 15), sum(1 for v in vals if v > 15)))
    print("  closest 8:", [(round(v, 2), c, u) for v, c, u in ds_list[:8]])
    print("  furthest 6:", [(round(v, 1), c, u) for v, c, u in ds_list[-6:]])

    print("\n=== per duct: chambers within 5 / 10 / 15 m of the line ===")
    for did, kind, verts in sorted(ducts, key=lambda x: -len(x[2])):
        if len(verts) < 2:
            continue
        n5 = n10 = n15 = 0
        for cx, cy, _cid in ch:
            d = dist_to_path((cx, cy), verts)
            if d <= 5:
                n5 += 1
            if d <= 10:
                n10 += 1
            if d <= 15:
                n15 += 1
        if kind in ("Unchambered", "") or n5 == 0:
            print("  %-24s %-14s verts=%3d chambers<=5m=%2d  <=10m=%2d  <=15m=%2d"
                  % (did, kind, len(verts), n5, n10, n15))


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]))
