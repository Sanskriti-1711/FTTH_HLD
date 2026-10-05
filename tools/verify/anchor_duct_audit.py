"""Per-anchor reach from the run's GPKGs: for the MFG and every PDP, nearest
trench span / feeder duct / distribution duct / cables, plus feeder-duct
chamber segmentation status (R1/R2 audit).

Run with QGIS python (needs osgeo):
  "/c/Program Files/QGIS 3.44.6/apps/Python312/python.exe" tmp/anchor_duct_audit.py <rundir>
"""
import math
import sys
from collections import defaultdict
from pathlib import Path

from osgeo import ogr

ogr.UseExceptions()

OUT = Path(sys.argv[1])


def feats(name):
    p = OUT / f"{name}.gpkg"
    if not p.exists():
        return []
    ds = ogr.Open(str(p))
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        if g.GetGeometryType() in (ogr.wkbPoint25D,):
            g = ogr.ForceTo2D(g) if hasattr(ogr, "ForceTo2D") else g
        d = {lyr.GetLayerDefn().GetFieldDefn(i).GetName(): f.GetField(i)
             for i in range(lyr.GetLayerDefn().GetFieldCount())}
        out.append((d, g))
    ds = None
    return out


def lines(g):
    gt = ogr.GT_Flatten(g.GetGeometryType())
    if gt == ogr.wkbLineString:
        return [[(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())]]
    if gt == ogr.wkbMultiLineString:
        return [[(g.GetGeometryRef(i).GetX(k), g.GetGeometryRef(i).GetY(k))
                 for k in range(g.GetGeometryRef(i).GetPointCount())]
                for i in range(g.GetGeometryCount())]
    if gt == ogr.wkbPolygon:
        r = g.GetGeometryRef(0)
        return [[(r.GetX(i), r.GetY(i)) for i in range(r.GetPointCount())]]
    if gt == ogr.wkbMultiPolygon:
        out = []
        for i in range(g.GetGeometryCount()):
            r = g.GetGeometryRef(i).GetGeometryRef(0)
            out.append([(r.GetX(k), r.GetY(k)) for k in range(r.GetPointCount())])
        return out
    return []


def anchors(g):
    gt = ogr.GT_Flatten(g.GetGeometryType())
    if gt == ogr.wkbPoint:
        return [(g.GetX(), g.GetY())]
    ls = lines(g)
    return ls[0] if ls else []


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def nearest(px, py, fs):
    best = (1e18, None)
    for d, g in fs:
        for ln in lines(g):
            for i in range(len(ln) - 1):
                dd = seg_dist(px, py, ln[i][0], ln[i][1], ln[i + 1][0], ln[i + 1][1])
                if dd < best[0]:
                    best = (dd, d)
    return best


trenches = feats("Final_Trenches")
fduct = feats("Feeder_Ducts")
dduct = feats("Distribution_Ducts")
fcab = feats("Feeder_Cable")
dcab = feats("Distribution_Cable")
pdps = feats("PDPs") or feats("Polygons")
mfgs = feats("MFG")

print(f"layers: trench={len(trenches)} f-duct={len(fduct)} d-duct={len(dduct)} "
      f"f-cable={len(fcab)} d-cable={len(dcab)} pdp={len(pdps)} mfg={len(mfgs)}")

# --- R1/R2: every PDP on the feeder duct
rows = []
for d, g in pdps:
    pid = d.get("PDP_ID") or d.get("SRC_ID") or d.get("POLYGON_ID") or "?"
    an = anchors(g)
    if an:
        rows.append((pid, an[0][0], an[0][1]))
for d, g in mfgs:
    an = anchors(g)
    if an:
        rows.append((d.get("MFG_ID") or "MFG", an[0][0], an[0][1]))

print(f"\n{'anchor':<14} {'trench':>8} {'tier':<14} {'f-duct':>8} {'d-duct':>8} "
      f"{'f-cable':>8} {'d-cable':>8}")
miss = []
for pid, px, py in rows:
    td, tprop = nearest(px, py, trenches)
    fd, _ = nearest(px, py, fduct)
    dd, _ = nearest(px, py, dduct)
    fc, _ = nearest(px, py, fcab)
    dc, _ = nearest(px, py, dcab)
    tier = (tprop or {}).get("TRENCH_TIER") or (tprop or {}).get("TIER") or "?"
    print(f"{str(pid):<14} {td:8.2f} {str(tier):<14} {fd:8.2f} {dd:8.2f} "
          f"{fc:8.2f} {dc:8.2f}" + ("" if fd <= 1.0 else "  <-- FEEDER MISS"))
    if fd > 1.0:
        miss.append((pid, fd, td, tier, dd))
print(f"\nR1: {len(rows) - len(miss)}/{len(rows)} anchors on feeder duct (<=1 m); "
      f"{len(miss)} miss")
for pid, fd, td, tier, dd in sorted(miss, key=lambda r: -r[1]):
    print(f"   {pid}: feeder duct {fd:.2f} m | nearest trench {td:.2f} m "
          f"({tier}) | nearest dist duct {dd:.2f} m")

# --- R2: are the feeder/dist ducts cut at chambers (per-span) ?
def cut_report(name, fs):
    starts = [d for d, _g in fs
              if (d.get("START_CHAMBER") or d.get("START_NODE"))]
    lens = [d.get("BUNDLE_LEN_M") or d.get("length_m") or 0 for d, _g in fs]
    tot = sum(float(x or 0) for x in lens) / 1000.0
    print(f"\n{name}: {len(fs)} feature(s); {len(starts)} with a chamber anchor; "
          f"sum length {tot:.3f} km")
    if fs:
        sample = fs[0][0]
        keys = [k for k in ("DUCT_ID", "N_DUCTS", "WAYS_TOTAL", "CLUBS",
                            "BUNDLE_LEN_M", "length_m", "START_CHAMBER",
                            "END_CHAMBER", "SPAN_LEN_M", "RUN_ID")
                if k in sample]
        print("   fields:", ", ".join(keys))


for nm, fs in (("Feeder_Ducts", fduct), ("Distribution_Ducts", dduct),
               ("Feeder_Ducts_Runs", feats("Feeder_Ducts_Runs")),
               ("Distribution_Ducts_Runs", feats("Distribution_Ducts_Runs"))):
    cut_report(nm, fs)

# --- chambers near the anchor gaps (are they cut at chambers at all?)
ch = feats("Chambers")
print(f"\nChambers: {len(ch)}")
