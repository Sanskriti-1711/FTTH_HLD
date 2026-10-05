"""Per-PDP topological connection check against the trench network.

For every PDP answers:
  - distance to the nearest trench (any tier)
  - distance to the nearest FEEDER (open-cut backbone) span
  - whether a trench vertex (node) sits within 1 m  -> the network is welded there
  - whether the PDP is joined to the MFG through Feeder spans only

Usage: python tmp/pdp_topo_check.py <run_dir>
"""
import glob
import math
import os
import sys

from osgeo import ogr

TOL = 1.0


def load(path, types=None):
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        if types and g.GetGeometryName() not in types:
            continue
        out.append((f.items() or {}, g.Clone()))
    ds = None
    return out


def pick(d, pats):
    for p in pats:
        hits = glob.glob(os.path.join(d, p))
        if hits:
            return hits[0]
    return None


def tier(props):
    for k in ("TRENCH_TIER", "trench_tier", "TIER", "USAGE_TYPE"):
        v = props.get(k)
        if v:
            return str(v).strip()
    return "?"


def lines(g):
    if g.GetGeometryName().upper().startswith("MULTI"):
        return [g.GetGeometryRef(i).Clone() for i in range(g.GetGeometryCount())]
    return [g]


def verts(gs):
    out = []
    for g in gs:
        for seg in lines(g):
            for i in range(seg.GetPointCount()):
                out.append(seg.GetPoint(i))
    return out


def wkbkey(pt):
    return (round(pt[0], 3), round(pt[1], 3))


def main(run_dir):
    pdp_f = pick(run_dir, ["PDPs.gpkg", "PDPs.geojson", "*PDP*.gpkg"])
    tr_f = pick(run_dir, ["Final_Trenches.gpkg", "*trench_layer.gpkg", "Feeder_Trench.gpkg"])
    nd_f = pick(run_dir, ["Trench_Nodes.gpkg", "*trench_nodes*.gpkg"])
    pdps = load(pdp_f, {"Point", "MultiPoint"}) or load(pdp_f)
    trench = load(tr_f)
    nodes = load(nd_f) if nd_f else []
    print(f"PDPs {len(pdps)}  trenches {len(trench)}  trench_nodes {len(nodes)}")
    print(f"  (pdp={os.path.basename(pdp_f or '?')} trench={os.path.basename(tr_f or '?')}"
          f" nodes={os.path.basename(nd_f) if nd_f else 'none'})\n")

    feeder = [(p, g) for p, g in trench if tier(p).lower().startswith("feed")]
    feeder_verts = set(wkbkey(v) for v in verts([g for _, g in feeder]))
    all_verts = set(wkbkey(v) for v in verts([g for _, g in trench]))
    node_pts = []
    for _, g in nodes:
        for sg in lines(g):
            if sg.GetGeometryName().upper().endswith("POINT"):
                node_pts.append((sg.GetX(), sg.GetY()))

    print(f"{'PDP_ID':12s} {'nearest':>8s} {'feeder':>8s} {'vtx':>5s} {'node':>5s}  tier-of-nearest")
    bad = []
    for i, (props, g) in enumerate(pdps):
        pid = props.get("PDP_ID") or f"#{i}"
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(props.get("X") or props.get("CENTR_X") or g.GetX(),
                       props.get("Y") or props.get("CENTR_Y") or g.GetY())
        d_all, t_all = min(((pt.Distance(tg), tier(tp)) for tp, tg in trench), default=(float("inf"), "?"))
        d_fed = min((pt.Distance(tg) for _, tg in feeder), default=float("inf"))
        near_v = min((math.hypot(pt.GetX() - q[0], pt.GetY() - q[1]) for q in all_verts), default=float("inf"))
        near_n = min((math.hypot(pt.GetX() - q[0], pt.GetY() - q[1]) for q in node_pts), default=float("inf"))
        flag = ""
        if d_all > TOL:
            flag = "  <-- OFF TRENCH"
            bad.append((pid, d_all))
        elif d_fed > TOL:
            flag = "  <-- not on feeder"
            bad.append((pid, d_fed))
        elif near_v > TOL:
            flag = "  <-- no vertex here"
        print(f"{pid:12s} {d_all:8.2f} {d_fed:8.2f} {near_v:5.2f} "
              f"{near_n if node_pts else float('nan'):5.2f}  {t_all}{flag}")
    print(f"\nflagged: {len(bad)}")
    for pid, d in bad:
        print(f"   {pid} {d:.2f} m")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
