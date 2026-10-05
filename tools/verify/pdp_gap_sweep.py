"""Sweep all project output dirs: how many PDPs are off the trench network?

Usage: python tmp/pdp_gap_sweep.py <outputs_root>
"""
import glob
import os
import sys

from osgeo import ogr


def load(path):
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is not None:
            p = f.items() or {}
            p["__geom__"] = g.Clone()
            out.append(p)
    ds = None
    return out


def pick(d, pats):
    for p in pats:
        hits = glob.glob(os.path.join(d, p))
        if hits:
            return sorted(hits)[0]
    return None


def main(root):
    rows = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(d):
            continue
        pdp_f = pick(d, ["PDPs.gpkg", "pdps.geojson", "*PDPs*.gpkg", "*pdp*.geojson"])
        tr_f = pick(d, ["Final_Trenches.gpkg", "final_trenches.geojson",
                        "*trench_layer.gpkg", "Feeder_Trench.gpkg"])
        if not pdp_f or not tr_f:
            continue
        pdps = load(pdp_f)
        trs = [p for p in load(tr_f) if p["__geom__"].GetGeometryName().upper().find("LINE") >= 0]
        if not pdps or not trs:
            continue
        off = []
        for p in pdps:
            g = p["__geom__"]
            pid = p.get("PDP_ID") or p.get("id") or "?"
            best = min((g.Distance(t["__geom__"]) for t in trs), default=float("inf"))
            if best > 0.5:
                off.append((best, pid))
        rows.append((os.path.basename(d), len(pdps), len(trs), off))
    print(f"{'run':34s} {'pdps':>5s} {'trnch':>6s} {'off':>4s}  worst")
    for name, npdp, ntr, off in rows:
        worst = f"{max(off)[1]} {max(off)[0]:.2f} m" if off else "-"
        mark = "  <<<" if off else ""
        print(f"{name:34s} {npdp:5d} {ntr:6d} {len(off):4d}  {worst}{mark}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
