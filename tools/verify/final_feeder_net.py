"""Is the FEEDER backbone inside Final_Trenches one component, and does it reach every PDP?"""
import sys
from collections import defaultdict
from osgeo import ogr

RUN = sys.argv[1] if len(sys.argv) > 1 else (
    "HLD_Planning_01/web/backend/outputs/ductfix2_1789861240")
TOL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05

ds = ogr.Open(f"{RUN}/Final_Trenches.gpkg")
lyr = ds.GetLayer(0)
d = lyr.GetLayerDefn()
i_t = d.GetFieldIndex("TRENCH_TIER")
i_ty = d.GetFieldIndex("TRENCH_TYPE")
feeder, allspans = [], []
for f in lyr:
    g = f.GetGeometryRef()
    if g is None or g.IsEmpty():
        continue
    g = g.Clone()
    allspans.append((g, f))
    if str(f.GetField(i_t) or "").lower() == "feeder":
        feeder.append((g, f))
ds = None
print(f"Final_Trenches feeder subset: {len(feeder)} of {len(allspans)} spans "
      f"({sum(g.Length() for g, _ in feeder):.0f} m)")


def components(items, tol):
    par = list(range(len(items)))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    for i in range(len(items)):
        gi = items[i][0]
        ei = gi.GetEnvelope()
        for j in range(i + 1, len(items)):
            gj = items[j][0]
            ej = gj.GetEnvelope()
            if ei[1] + tol < ej[0] or ej[1] + tol < ei[0] or ei[3] + tol < ej[2] or ej[3] + tol < ei[2]:
                continue
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            if gi.Distance(gj) <= tol:
                par[rj] = ri
    comps = defaultdict(list)
    for i in range(len(items)):
        comps[find(i)].append(i)
    return sorted(comps.values(), key=len, reverse=True)


for tol in (0.05, 0.5, 2.0):
    rank = components(feeder, tol)
    lens = [sum(feeder[i][0].Length() for i in c) for c in rank]
    print(f"\ntol {tol} m: {len(rank)} component(s)  sizes {[len(c) for c in rank[:8]]}"
          f"  lengths {[round(x, 1) for x in lens[:8]]}")
    if tol != 0.05:
        continue
    comp_of = {}
    for ci, c in enumerate(rank):
        for i in c:
            comp_of[i] = ci
    for nm, idf in (("PDPs", "PDP_ID"), ("MFG", "MFG_ID")):
        ds2 = ogr.Open(f"{RUN}/{nm}.gpkg")
        lyr2 = ds2.GetLayer(0)
        i_id = lyr2.GetLayerDefn().GetFieldIndex(idf)
        where, off = defaultdict(list), []
        for f in lyr2:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            pid = str(f.GetField(i_id))
            hit = None
            for i, (tg, _) in enumerate(feeder):
                if tg.Distance(g) <= 1.0:
                    hit = i
                    break
            if hit is None:
                off.append(pid)
            else:
                where[comp_of[hit]].append(pid)
        print(f"   {nm}: {sum(len(v) for v in where.values())} within 1 m of the feeder "
              f"backbone, {len(off)} further away {off[:8]}")
        for ci in sorted(where):
            tag = "MAIN" if ci == 0 else f"comp#{ci}"
            print(f"      {tag}: {sorted(where[ci])}")
        ds2 = None
