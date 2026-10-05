"""Audit the feeder duct dedupe: is every deleted row really a duplicate?

Replays segmentation on the run's duct output, then for the two passes
(merge per chamber pair, absorb chamber stubs) reports, per removal, how far
the deleted geometry lies from the row that inherits it and how much of the
deleted length is NOT covered by that row (buffer 0.5 m).

Usage: python tmp/duct_drop_audit.py <run_dir>
"""
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "HLD_Planning_01"))

from HLDPlanning.utils import attr_enrich as AE  # noqa: E402
from osgeo import ogr  # noqa: E402

ogr.UseExceptions()
PROBE = HERE / "ductaudit"
COVER_M = 0.5


def line_xy(g):
    gg = g.Clone()
    try:
        gg = gg.GetLinearGeometry()
    except Exception:
        pass
    parts = []
    if gg.GetGeometryName() == "LINESTRING":
        parts = [gg]
    elif gg.GetGeometryName() == "MULTILINESTRING":
        parts = [gg.GetGeometryRef(i) for i in range(gg.GetGeometryCount())]
    return [[(p.GetX(i), p.GetY(i)) for i in range(p.GetPointCount())] for p in parts]


def rows(path):
    d = ogr.Open(str(path))
    l = d.GetLayer(0)
    out = []
    for f in l:
        g = f.GetGeometryRef()
        if g is None:
            continue
        out.append({
            "fid": f.GetFID(),
            "sc": str(f.GetField("START_CHAMBER") or "").strip(),
            "ec": str(f.GetField("END_CHAMBER") or "").strip(),
            "geom": g.Clone(),
            "len": g.Length(),
            "cables": str(f.GetField("cables_carried") or ""),
        })
    return out, d, l


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _min_dist_pt(pt, parts):
    best = float("inf")
    for xy in parts:
        for i in range(len(xy) - 1):
            d = _seg_dist(pt, xy[i], xy[i + 1])
            if d < best:
                best = d
    return best


def covered_fraction(a, b):
    """share of line ``a``'s length lying within COVER_M of line ``b``"""
    bparts = line_xy(b)
    if not bparts:
        return 0.0
    total = covered = 0.0
    for xy in line_xy(a):
        for i in range(len(xy) - 1):
            seg = math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])
            if seg <= 0:
                continue
            total += seg
            # sample every 1 m so a detour wider than the tolerance is missed
            n = max(2, int(seg) + 1)
            for k in range(n):
                t = k / (n - 1)
                pt = (xy[i][0] + t * (xy[i + 1][0] - xy[i][0]),
                      xy[i][1] + t * (xy[i + 1][1] - xy[i][1]))
                if _min_dist_pt(pt, bparts) <= COVER_M:
                    covered += seg / (n - 1)
    return covered / total if total > 0 else 1.0


def main():
    run = Path(sys.argv[1])
    if PROBE.exists():
        shutil.rmtree(PROBE)
    PROBE.mkdir(parents=True)
    for n in ("Feeder_Ducts", "Distribution_Ducts", "Drop_Ducts", "Chambers",
              "Final_Trenches", "MFG", "PDPs", "Coupleurs", "Objects"):
        s = run / f"{n}.gpkg"
        if s.exists():
            shutil.copy2(s, PROBE / f"{n}.gpkg")
    shutil.copy2(run / "Feeder_Ducts_Runs.gpkg", PROBE / "Feeder_Ducts.gpkg")
    p = lambda n: str(PROBE / f"{n}.gpkg")  # noqa: E731

    AE.segment_ducts_at_chambers(p("Feeder_Ducts"), p("Distribution_Ducts"),
                                 p("Chambers"), None)
    AE.enrich_ducts(p("Feeder_Ducts"), p("Distribution_Ducts"), p("Drop_Ducts"),
                    p("Final_Trenches"), p("Chambers"), None)

    rs, _d, _l = rows(p("Feeder_Ducts"))
    print(f"segmented rows: {len(rs)}  (total {sum(r['len'] for r in rs):,.0f} m)")

    # ── merge_ducts_per_chamber_span: keep longest per (sc, ec) ──
    groups = defaultdict(list)
    for r in rs:
        if r["sc"] or r["ec"]:
            groups[(r["sc"], r["ec"])].append(r)
    n_folded = 0
    distinct = coincide = 0
    lost_distinct = []
    for key, feats in groups.items():
        if len(feats) < 2:
            continue
        feats.sort(key=lambda r: r["len"], reverse=True)
        keep = feats[0]
        for r in feats[1:]:
            n_folded += 1
            cov = covered_fraction(r["geom"], keep["geom"])
            if cov >= 0.95:
                coincide += 1
            else:
                distinct += 1
                lost_distinct.append((
                    key, round(r["len"], 1), round(cov * 100, 1),
                    round(r["geom"].Distance(keep["geom"]), 2)))
    print(f"\nmerge: {n_folded} row(s) folded across {sum(1 for v in groups.values() if len(v) > 1)}"
          f" chamber pair group(s)")
    print(f"  already covered by the kept row (>=95 %): {coincide}")
    print(f"  DISTINCT geometry lost:                    {distinct}")
    for key, ln, cov, dist in sorted(lost_distinct, key=lambda x: -x[1])[:12]:
        print(f"    {key[0]!s:>10} -> {key[1]!s:<10} len {ln:>7.1f} m  "
              f"covered {cov:>5.1f}%  {dist} m from the kept row")

    # ── absorb_chamber_stubs: delete sc == ec rows ──
    stubs = [r for r in rs if r["sc"] and r["sc"] == r["ec"]]
    real = [r for r in rs if not (r["sc"] and r["sc"] == r["ec"]) and (r["sc"] or r["ec"])]
    print(f"\nabsorb: {len(stubs)} stub row(s), {len(real)} real span(s)")
    cov_list = []
    for r in stubs:
        cands = [x for x in real if x["sc"] == r["sc"] or x["ec"] == r["sc"]]
        if not cands:
            cov_list.append((r, None, -1.0, None))
            continue
        best = min(cands, key=lambda x: x["geom"].Distance(r["geom"]))
        cov_list.append((r, best, covered_fraction(r["geom"], best["geom"]),
                         r["geom"].Distance(best["geom"])))
    ok = sum(1 for _, _b, c, _d in cov_list if c >= 0.95)
    bad = [(r, b, c, d) for r, b, c, d in cov_list if not c >= 0.95]
    print(f"  stubs fully covered by the inheriting span: {ok}/{len(stubs)}")
    print(f"  stubs whose geometry would be LOST:         {len(bad)}")
    for r, b, c, d in sorted(bad, key=lambda x: -x[0]["len"])[:12]:
        print(f"    {r['sc']:>10} len {r['len']:>7.1f} m  covered {c * 100:>5.1f}%  "
              f"{d if d is None else round(d, 2)} m from the span that inherits it")


if __name__ == "__main__":
    main()
