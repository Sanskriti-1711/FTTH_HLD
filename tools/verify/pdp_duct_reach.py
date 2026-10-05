"""Is each PDP *physically* connected: does a duct reach it, and is there a chamber?

For every PDP reports the distance to the nearest feeder duct, the nearest duct
endpoint, the nearest distribution duct and the nearest chamber.

Usage: python tmp/pdp_duct_reach.py <run_dir>
"""
import glob
import math
import os
import sys

from osgeo import ogr


def load(path, want_lines=None):
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        nm = g.GetGeometryName().upper()
        if want_lines is True and "LINE" not in nm:
            continue
        p = f.items() or {}
        p["__geom__"] = g.Clone()
        p["__name__"] = nm
        out.append(p)
    ds = None
    return out


def pick(d, pats):
    for p in pats:
        hits = glob.glob(os.path.join(d, p))
        if hits:
            return sorted(hits)[0]
    return None


def parts(g):
    nm = g.GetGeometryName().upper()
    if nm.startswith("MULTI") or nm.startswith("GEOMETRYCOLLECTION"):
        return [g.GetGeometryRef(i).Clone() for i in range(g.GetGeometryCount())]
    return [g]


def endpoints(g):
    out = []
    for sg in parts(g):
        if sg.GetPointCount() >= 1:
            out.append(sg.GetPoint(0))
            out.append(sg.GetPoint(sg.GetPointCount() - 1))
    return out


def main(d):
    pdp_f = pick(d, ["PDPs.gpkg", "pdps.geojson", "*PDP*.gpkg", "*pdp*.geojson"])
    fed_f = pick(d, ["Feeder_Ducts.gpkg", "feeder_ducts.geojson", "*Feeder_Duct*.gpkg"])
    dst_f = pick(d, ["Distribution_Ducts.gpkg", "distribution_ducts.geojson",
                     "*Distribution_Duct*.gpkg"])
    if dst_f is None:
        dst_f = pick(d, ["*duct*.gpkg", "*ducts*.geojson"])
    ch_f = pick(d, ["Chambers.gpkg", "chambers.geojson", "*Chamber*.gpkg"])
    pdps = [p for p in load(pdp_f) if "POINT" in p["__name__"]] or load(pdp_f)
    fed = load(fed_f, want_lines=True) if fed_f else []
    dst = load(dst_f, want_lines=True) if dst_f else []
    ch = [p for p in load(ch_f)] if ch_f else []
    print(f"files: pdp={os.path.basename(pdp_f or '?')} feeder_duct={os.path.basename(fed_f or '?')} "
          f"dist_duct={os.path.basename(dst_f or '?')} chamber={os.path.basename(ch_f or '?')}")
    print(f"counts: pdps={len(pdps)} feader_ducts={len(fed)} dist_ducts={len(dst)} chambers={len(ch)}\n")
    if not fed:
        print("NO FEEDER DUCTS FOUND")
    fed_ends = [e for p in fed for e in endpoints(p["__geom__"])]
    print(f"{'PDP':12s} {'feeder':>7s} {'f-duct-end':>10s} {'dist':>7s} {'chamber':>7s}")
    no_duct = []
    for p in pdps:
        g = p["__geom__"]
        pid = p.get("PDP_ID") or p.get("id") or "?"
        df = min((g.Distance(x["__geom__"]) for x in fed), default=float("inf"))
        de = min((math.hypot(g.GetX() - e[0], g.GetY() - e[1]) for e in fed_ends), default=float("inf"))
        dd = min((g.Distance(x["__geom__"]) for x in dst), default=float("inf"))
        dc = min((g.Distance(x["__geom__"]) for x in ch), default=float("inf"))
        flag = ""
        if df > 2.0:
            flag = "   <-- no feeder duct"
            no_duct.append((pid, df))
        print(f"{pid:12s} {df:7.2f} {de:10.2f} {dd:7.2f} {dc:7.2f}{flag}")
    print(f"\nPDPs with no feeder duct within 2 m: {len(no_duct)}")
    for pid, v in no_duct:
        print(f"   {pid} {v:.2f} m")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
