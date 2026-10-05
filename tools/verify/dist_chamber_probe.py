"""Why do distribution ducts come out Unchambered / Duct tap / Chamber stub?

For every distribution duct row in a finished run, print its SPAN_KIND, the
chamber ids it carries, and the distance from each END VERTEX to the nearest
chamber.  Separates "the snapping tolerance is too tight" from "there is no
chamber anywhere near this duct".

Usage: python tmp/dist_chamber_probe.py <output_dir>
"""
import math
import os
import sys

from osgeo import ogr


def coords_of(g):
    out = []
    if g is None or g.IsEmpty():
        return out
    wkt = g.ExportToWkt()
    body = wkt[wkt.index("(") + 1:]
    body = body.replace("(", "").replace(")", "")
    for part in body.split(","):
        bits = part.strip().split()
        if len(bits) >= 2:
            try:
                out.append((float(bits[0]), float(bits[1])))
            except ValueError:
                pass
    return out


def main(out):
    ds = ogr.Open(os.path.join(out, "Chambers.gpkg"))
    if ds is None:
        raise SystemExit("no Chambers.gpkg")
    lyr = ds.GetLayer(0)
    ch = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        pt = g.Centroid().GetPoint(0)
        ch.append((pt[0], pt[1], f.GetField("STRUCT_ID")))
    ds = None
    print("chambers:", len(ch))

    ds = ogr.Open(os.path.join(out, "Distribution_Ducts.gpkg"))
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]

    def near(x, y):
        best, bd = "", 1e18
        for cx, cy, cid in ch:
            d = math.hypot(cx - x, cy - y)
            if d < bd:
                bd, best = d, cid
        return best, bd

    buckets = {}
    examples = {}
    n = 0
    for f in lyr:
        n += 1
        kind = str(f.GetField("SPAN_KIND") or "")
        sc = str(f.GetField("START_CHAMBER") or "").strip()
        ec = str(f.GetField("END_CHAMBER") or "").strip()
        cc = coords_of(f.GetGeometryRef())
        if not cc:
            continue
        _, d0 = near(*cc[0])
        _, d1 = near(*cc[-1])
        row = (round(d0, 1), round(d1, 1), bool(sc), bool(ec))
        b = buckets.setdefault(kind, {"n": 0, "d0": [], "d1": [], "stamped": 0})
        b["n"] += 1
        b["d0"].append(d0)
        b["d1"].append(d1)
        if sc or ec:
            b["stamped"] += 1
        examples.setdefault(kind, []).append(
            (f.GetField("DUCT_ID") or f.GetField("duct_id"), row,
             len(cc), round(float(f.GetField("length_m") or 0), 1)))
    ds = None

    print("distribution ducts:", n)
    for kind, b in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
        d0, d1 = sorted(b["d0"]), sorted(b["d1"])
        allD = sorted(d0 + d1)
        p50 = allD[len(allD) // 2]
        p90 = allD[int(len(allD) * 0.9)]
        over5 = sum(1 for d in allD if d > 5.0)
        over15 = sum(1 for d in allD if d > 15.0)
        over50 = sum(1 for d in allD if d > 50.0)
        print(f"\n  {kind or '(empty)':16s} n={b['n']:4d}  rows carrying a chamber id: {b['stamped']}")
        print(f"      end->nearest chamber  p50={p50:8.1f}  p90={p90:8.1f}"
              f"  >5m={over5}  >15m={over15}  >50m={over50}")
        print(f"      worst pairs: {[(round(x,1), round(y,1)) for x, y in list(zip(d0, d1))[:6]]}")
        for ex in examples[kind][:4]:
            print("      ex:", ex)


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]))
