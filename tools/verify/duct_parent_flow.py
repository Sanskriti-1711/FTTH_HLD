"""Does each duct follow the trench span it claims (PARENT_TRENCH), per tier?

Usage: python tmp/duct_parent_flow.py <run_dir>
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

from osgeo import ogr

ogr.UseExceptions()
TOL = 0.5


def parts(geom):
    g = geom.Clone()
    try:
        g = g.GetLinearGeometry()
    except Exception:
        pass
    if g.GetGeometryName() == "LINESTRING":
        return [g]
    if g.GetGeometryName() == "MULTILINESTRING":
        return [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
    return []


def xy_of(g):
    return [(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())]


def seg_dist(x, y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return ((x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2) ** 0.5


def dev(pt, line_xy):
    return min(seg_dist(pt[0], pt[1], line_xy[i][0], line_xy[i][1],
                        line_xy[i + 1][0], line_xy[i + 1][1])
               for i in range(len(line_xy) - 1))


def length(xy):
    return sum(((xy[i + 1][0] - xy[i][0]) ** 2 + (xy[i + 1][1] - xy[i][1]) ** 2) ** 0.5
               for i in range(len(xy) - 1))


def main():
    run = Path(sys.argv[1])
    ds = ogr.Open(str(run / "Final_Trenches.gpkg"))
    lyr = ds.GetLayer(0)
    trenches = {}
    for f in lyr:
        g = f.GetGeometryRef()
        for part in parts(g):
            trenches[f.GetField("TRENCH_ID")] = {
                "xy": xy_of(part), "tier": f.GetField("TRENCH_TIER"),
                "type": f.GetField("TRENCH_TYPE"),
                "chambers": (f.GetField("START_CHAMBER"), f.GetField("END_CHAMBER")),
            }
            break
    tiers = Counter(t["tier"] for t in trenches.values())
    print(f"run {run.name}: {len(trenches)} trench spans  tiers={dict(tiers)}\n")

    chambers = []
    cds = ogr.Open(str(run / "Chambers.gpkg"))
    clyr = cds.GetLayer(0)
    for f in clyr:
        g = f.GetGeometryRef()
        chambers.append((g.GetX(), g.GetY()))
    print(f"{len(chambers)} chambers\n")

    for name in ("Feeder_Ducts", "Distribution_Ducts", "Drop_Ducts"):
        fp = run / f"{name}.gpkg"
        if not fp.exists():
            continue
        dsx = ogr.Open(str(fp))
        dl = dsx.GetLayer(0)
        n = 0
        parent_missing = 0
        parent_tier = Counter()
        dev_p50, chase = [], 0
        same_tier = 0
        chamber_hits = 0
        chamber_ends = 0
        len_ratios = []
        for f in dl:
            n += 1
            pt = f.GetField("PARENT_TRENCH")
            pg = f.GetGeometryRef()
            duct_parts = [xy_of(p) for p in parts(pg)]
            if not pt or pt not in trenches:
                parent_missing += 1
                continue
            ref = trenches[pt]
            parent_tier[ref["tier"]] += 1
            for dxy in duct_parts:
                d = max(dev(p, ref["xy"]) for p in dxy)
                dev_p50.append(d)
                # does the duct reach the far end of its parent span?
                if dev(dxy[-1], ref["xy"]) <= TOL and dev(dxy[0], ref["xy"]) <= TOL:
                    chase += 1
                ln = length(dxy)
                if ln > 0.5:
                    len_ratios.append(ln / max(1e-9, ln))
            want = {"Feeder_Ducts": "Feeder", "Distribution_Ducts": "Distribution",
                    "Drop_Ducts": "Garden"}[name]
            if ref["tier"] == want:
                same_tier += 1
            for e in (f.GetField("START_CHAMBER"), f.GetField("END_CHAMBER")):
                if e:
                    chamber_ends += 1
            ends = [xy_of(p)[0] for p in parts(pg) if p.GetPointCount()] + \
                   [xy_of(p)[-1] for p in parts(pg) if p.GetPointCount()]
            if any(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 < 1.0
                   for x, y in ends for cx, cy in chambers):
                chamber_hits += 1
        s = sorted(dev_p50)
        print(f"--- {name}: {n} features")
        print(f"    parent trench missing: {parent_missing}")
        print(f"    parent tier histogram: {dict(parent_tier)}   on-tier: {same_tier}/{n}")
        if s:
            print(f"    vertex dev vs PARENT_TRENCH span: p50 {s[len(s)//2]:.2f} "
                  f"p90 {s[int(len(s)*0.9)]:.2f} max {max(s):.2f} m")
        print(f"    features with START/END_CHAMBER set: {chamber_ends}, "
              f"with an end <1m from a real chamber: {chamber_hits}")
        print()


if __name__ == "__main__":
    main()
