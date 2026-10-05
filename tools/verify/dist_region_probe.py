# -*- coding: utf-8 -*-
"""Is each distribution duct inside its OWN polygon, and does it reach its PDP's points?

Two questions, measured separately on a finished run:

  * CONFINEMENT — how much of each published distribution duct's length lies
    outside the polygon(s) it names in POLYGON_ID (a duct may be a bridge in
    the layer, but the design rule is "the distribution flows inside its own
    polygon").
  * COVERAGE — for every PDP (one per polygon), do all of the pseudo object
    points (the couplers) tagged to that polygon sit on a distribution duct
    that also carries that polygon id?

    unset PYTHONPATH && python tmp/dist_region_probe.py <run_dir> [tol_m]
"""
import os
import sys
from collections import defaultdict

from osgeo import ogr
from shapely import wkb
from shapely.geometry import LineString
from shapely.ops import unary_union

TOL = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5


def shp(path, where=None):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        try:
            geom = wkb.loads(bytes(g.ExportToWkb()))
        except Exception:
            continue
        row = {n: f.GetField(n) for n in names}
        out.append((row, geom))
    ds = None
    return out


def ids(value):
    return [v.strip().upper() for v in str(value or "").replace(";", ",").split(",") if v.strip()]


def main():
    run = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    polys = {}
    for row, geom in shp(os.path.join(run, "Polygons.gpkg")):
        pid = str(row.get("POLYGON_ID") or "").strip().upper()
        if pid and not geom.is_empty:
            polys[pid] = geom.buffer(TOL)

    print("regions: %d polygon(s), tolerance %.2f m" % (len(polys), TOL))

    # ── confinement ───────────────────────────────────────────────────────
    for stem in os.environ.get("PROBE_STEMS", "Distribution_Ducts,Distribution_Ducts_Runs,"
                              "Distribution_Cable,Final_Trenches").split(","):
        if not os.path.isfile(os.path.join(run, "%s.gpkg" % stem)):
            continue
        rows = shp(os.path.join(run, "%s.gpkg" % stem))
        if not rows:
            continue
        total = off = 0.0
        outside_rows = []
        straddling = 0
        for row, geom in rows:
            try:
                ln = geom.length
            except Exception:
                continue
            if ln <= 0 or geom.geom_type not in ("LineString", "MultiLineString"):
                continue
            pids = ids(row.get("POLYGON_ID"))
            if len(pids) > 1:
                straddling += 1
            inside = 0.0
            for pid in pids:
                pg = polys.get(pid)
                if pg is None:
                    continue
                try:
                    inside += geom.intersection(pg).length
                except Exception:
                    pass
            total += ln
            o = max(0.0, ln - inside)
            off += o
            if o > 1.0:
                outside_rows.append((o, ln, ",".join(pids) or "-",
                                     str(row.get("DUCT_ID") or "")))
        outside_rows.sort(reverse=True)
        # How far outside the region does the layer actually go, and how many
        # of its rows are boundary straddles rather than genuine departures?
        far = 0.0
        straddles = 0
        for row, geom in rows:
            pids = ids(row.get("POLYGON_ID"))
            if not pids:
                continue
            pg = None
            for pid in pids:
                if pid in polys:
                    pg = polys[pid] if pg is None else pg.union(polys[pid])
            if pg is None:
                continue
            try:
                out_geom = geom.difference(pg)
            except Exception:
                continue
            if out_geom.is_empty:
                continue
            far = max(far, out_geom.distance(pg))
            straddles += 1 if out_geom.length and \
                out_geom.distance(pg) <= TOL else 0
        print("\n%s: %d row(s), %.1f m" % (stem, len(rows), total))
        print("  furthest any row reaches beyond its polygon: %.2f m (%d row(s) "
              "outside only as a boundary straddle) -> NOT a verdict"
              % (far, straddles))
        print("  off its own polygon(s): %.1f m (%.1f%%); %d row(s) >1 m out; "
              "%d row(s) name >1 polygon"
              % (off, 100 * off / total if total else 0, len(outside_rows), straddling))
        for o, ln, pid, did in outside_rows[:8]:
            print("     %7.1f m of %7.1f m  %-24s %s" % (o, ln, pid, did))

    # ── coverage: every pseudo point of a PDP on a duct of that PDP's polygon ──
    ducts = defaultdict(list)
    for row, geom in shp(os.path.join(run, "Distribution_Ducts.gpkg")):
        for pid in ids(row.get("POLYGON_ID")):
            ducts[pid].append(geom)
    couplers = shp(os.path.join(run, "Coupleurs.gpkg"))
    same_poly_flag = defaultdict(int)
    per_region = defaultdict(lambda: [0, 0])          # pid -> [points, reached]
    unreached = []
    for row, geom in couplers:
        pid = (str(row.get("POLYGON_ID") or "").strip().upper()
               or str(row.get("pdp_pol_id") or "").strip().upper())
        flag = str(row.get("DIST_DUCT_SAME_POLY") or "")
        same_poly_flag[flag or "(blank)"] += 1
        per_region[pid][0] += 1
        hit = False
        for g in ducts.get(pid, []):
            try:
                if g.distance(geom) <= TOL:
                    hit = True
                    break
            except Exception:
                continue
        if hit:
            per_region[pid][1] += 1
        else:
            unreached.append((pid, str(row.get("coupler_id") or ""),
                              str(row.get("DIST_DUCT_ID") or "")))
    print("\nCoupleurs: %d, DIST_DUCT_SAME_POLY values %s"
          % (len(couplers), dict(same_poly_flag)))

    # Where does a stranded point actually sit, and how far is the nearest duct
    # of its OWN polygon versus any polygon? That separates "the region has a
    # duct nearby" from "the region has no duct there at all".
    all_ducts = []
    for row, geom in shp(os.path.join(run, "Distribution_Ducts.gpkg")):
        all_ducts.append((ids(row.get("POLYGON_ID")), geom))
    inside_own = 0
    d_own, d_any = [], []
    for row, geom in couplers:
        pid = str(row.get("POLYGON_ID") or "").strip().upper()
        pg = polys.get(pid)
        inside_own += 1 if (pg is not None and pg.contains(geom)) else 0
        bo, ba = float("inf"), float("inf")
        for pids, g in all_ducts:
            try:
                dd = g.distance(geom)
            except Exception:
                continue
            ba = min(ba, dd)
            if pid in pids:
                bo = min(bo, dd)
        if ba > TOL:
            d_own.append(bo)
            d_any.append(ba)

    def pct(v):
        v = sorted(v)
        if not v:
            return "-"
        return "p50 %.2f p90 %.2f max %.2f" % (v[len(v) // 2], v[int(len(v) * 0.9)], v[-1])

    print("  points inside their own polygon (%.2f m tol): %d of %d"
          % (TOL, inside_own, len(couplers)))
    if d_any:
        print("  of the %d points >%.2f m from ANY duct: distance to the nearest"
              " duct of their OWN polygon: %s" % (len(d_any), TOL, pct(d_own)))
        print("                                        nearest duct of ANY polygon: %s"
              % pct(d_any))
    bad = {p: v for p, v in per_region.items() if v[1] < v[0]}
    print("regions where a PDP's pseudo points are NOT all on a duct of that "
          "polygon: %d of %d" % (len(bad), len(per_region)))
    for pid in sorted(bad, key=lambda p: per_region[p][1] - per_region[p][0])[:10]:
        n, r = per_region[pid]
        print("   %-12s %d/%d reached (%d stranded)" % (pid or "(none)", r, n, n - r))
    print("stranded points: %d" % len(unreached))
    for pid, cid, d in unreached[:8]:
        print("   %-12s %-12s DIST_DUCT_ID=%s" % (pid or "(none)", cid, d or "(none)"))


if __name__ == "__main__":
    main()
