"""Compare the original linear coupler scan with the grid-prefiltered one.

Reads the real Dist_Ducts / Coupleurs GPKGs from a completed run and checks that
every coupler resolves to the same DIST_DUCT_ID / DIST_DUCT_SAME_POLY under both
algorithms.  Run with the QGIS Python:

  "/c/Program Files/QGIS 3.44.6/bin/python-qgis.bat" tmp/bench_couplers.py <out_dir>
"""
import math
import os
import sys

from osgeo import ogr

TOL = 5.0
CELL = 25.0
MAX_CELLS = 4096


def _parts(geom):
    out = []
    if geom is None or geom.IsEmpty():
        return out
    n = geom.GetGeometryCount()
    if n:
        for i in range(n):
            p = geom.GetGeometryRef(i)
            if p is None:
                continue
            out.append([(p.GetX(j), p.GetY(j)) for j in range(p.GetPointCount())])
    else:
        out.append([(geom.GetX(j), geom.GetY(j)) for j in range(geom.GetPointCount())])
    return [p for p in out if len(p) >= 2]


def _dps(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _field(lyr, f, name):
    idx = lyr.GetLayerDefn().GetFieldIndex(name)
    if idx < 0:
        return None
    return f.GetField(idx)


def load(out_dir):
    ds = ogr.Open(os.path.join(out_dir, "Distribution_Ducts.gpkg"))
    lyr = ds.GetLayer(0)
    ducts = []
    for f in lyr:
        did = ""
        for fld in ("DUCT_ID", "DUCT_UID"):
            v = _field(lyr, f, fld)
            if v not in (None, ""):
                did = str(v).strip()
                break
        poly = str(_field(lyr, f, "POLYGON_ID") or "").strip()
        for p in _parts(f.geometry()):
            ducts.append({"id": did, "poly": poly, "pts": p})
    ds = None
    cs = ogr.Open(os.path.join(out_dir, "Coupleurs.gpkg"))
    clyr = cs.GetLayer(0)
    couplers = []
    for f in clyr:
        g = f.geometry()
        if g is None or g.IsEmpty() or g.GetGeometryType() != ogr.wkbPoint:
            continue
        couplers.append((g.GetX(), g.GetY(),
                         str(_field(clyr, f, "POLYGON_ID") or "").strip().upper()))
    cs = None
    return ducts, couplers


def solve(px, py, poly, ducts, candidates):
    best_id, best_d, best_same, best_score = "", float("inf"), 0, float("inf")
    for di in candidates:
        d = ducts[di]
        pts = d["pts"]
        dist = min(_dps(px, py, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                   for i in range(len(pts) - 1))
        same = bool(poly) and d["poly"].upper() == poly
        score = dist - (1e-6 if same else 0.0)
        if score < best_score:
            best_score, best_d, best_id, best_same = score, dist, d["id"], int(same)
    if best_id and best_d <= TOL:
        return best_id, best_same
    return "", 0


def main():
    out_dir = sys.argv[1]
    ducts, couplers = load(out_dir)
    grid, large = {}, []
    for i, d in enumerate(ducts):
        xs = [p[0] for p in d["pts"]]
        ys = [p[1] for p in d["pts"]]
        cx0, cx1 = int(min(xs) // CELL), int(max(xs) // CELL)
        cy0, cy1 = int(min(ys) // CELL), int(max(ys) // CELL)
        if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > MAX_CELLS:
            large.append(i)
            continue
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                grid.setdefault((cx, cy), []).append(i)

    import time as _time
    _t0 = _time.perf_counter()
    old_results = [solve(px, py, poly, ducts, range(len(ducts)))
                   for px, py, poly in couplers]
    _t1 = _time.perf_counter()
    new_results = []
    for px, py, poly in couplers:
        found = set(large)
        for cx in range(int((px - TOL) // CELL), int((px + TOL) // CELL) + 1):
            for cy in range(int((py - TOL) // CELL), int((py + TOL) // CELL) + 1):
                found.update(grid.get((cx, cy), ()))
        new_results.append(solve(px, py, poly, ducts, sorted(found)))
    _t2 = _time.perf_counter()
    mism = 0
    linked = 0
    for old, new in zip(old_results, new_results):
        if old != new:
            mism += 1
            if mism <= 5:
                print("  MISMATCH old", old, "new", new)
        if old[0]:
            linked += 1
    print(f"linear_scan={_t1 - _t0:.2f}s  grid_scan={_t2 - _t1:.2f}s")
    print(f"ducts={len(ducts)} couplers={len(couplers)} linked={linked} mismatches={mism}")
    print("RESULT", "OK" if mism == 0 else "FAIL")


if __name__ == "__main__":
    main()
