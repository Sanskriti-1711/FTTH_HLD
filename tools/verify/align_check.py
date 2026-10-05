# -*- coding: utf-8 -*-
"""Alignment check for a published run: (1) couplers vs the duct line they serve,
(2) labelled duct ends vs the chamber they name. Measure, do not assert.

Usage: python tmp/align_check.py <run_dir>
"""
import os
import sys

from osgeo import ogr
from shapely import wkb
from shapely.geometry import Point
from shapely.ops import unary_union


def rows(path):
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    out = []
    for ft in lyr:
        g = ft.GetGeometryRef()
        geom = wkb.loads(bytes(g.ExportToWkb())) if g else None
        out.append(({n: ft.GetField(n) for n in names}, geom))
    return out


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[i]


def coupler_check(d):
    """Distance from each coupler to the duct that SERVES it.

    Not to the union of every duct: drop ducts terminate on every coupler by
    construction, so a union silently reports 0.00 m. DIST_DUCT_ID names the
    serving distribution duct, so measure that feature.
    """
    # DUCT_ID names a duct RUN shared by many span rows, so union them all —
    # comparing against a single row reports a phantom offset.
    parts = {}
    for f, g in rows(os.path.join(d, "Distribution_Ducts.gpkg")):
        if g is not None and f.get("DUCT_ID"):
            parts.setdefault(str(f.get("DUCT_ID")), []).append(g)
    dist_ducts = {k: unary_union(v) for k, v in parts.items()}
    net = unary_union([g for _, g in rows(os.path.join(d, "Distribution_Ducts.gpkg"))])
    pts = rows(os.path.join(d, "Coupleurs.gpkg"))

    exact, fallback, unlinked = [], [], 0
    allnet = []
    for f, g in pts:
        if g is None:
            continue
        allnet.append(g.distance(net))
        ref = f.get("DIST_DUCT_ID")
        if ref and str(ref) in dist_ducts:
            exact.append(g.distance(dist_ducts[str(ref)]))
        elif ref:
            unlinked += 1
        else:
            fallback.append(g.distance(net))

    print("  [whole distribution network] >1 m off: %d / %d (%.1f%%)  max=%.2f m"
          % (len([x for x in allnet if x > 1.0]), len(allnet),
             100.0 * len([x for x in allnet if x > 1.0]) / max(1, len(allnet)),
             max(allnet or [0.0])))

    print("=== COUPLERS vs the duct that serves them (DIST_DUCT_ID) ===")
    print("  couplers                    : %d" % len(pts))
    print("  linked to a named duct      : %d" % len(exact))
    print("  label set but duct missing  : %d" % unlinked)
    print("  no label (vs dist. network) : %d" % len(fallback))
    for name, vals in (("named serving duct", exact), ("distribution network", fallback)):
        if not vals:
            continue
        off = [x for x in vals if x > 1.0]
        print("  [%s] >1 m off: %d / %d (%.1f%%)  p50=%.2f p90=%.2f max=%.2f m"
              % (name, len(off), len(vals), 100.0 * len(off) / len(vals),
                 pct(vals, 0.5), pct(vals, 0.9), max(vals)))
    return len(exact), len(fallback), unlinked


def chamber_check(d):
    ch = {}
    for f, g in rows(os.path.join(d, "Chambers.gpkg")):
        if g is not None:
            ch[str(f.get("STRUCT_ID"))] = g
    print("=== LABELLED DUCT ENDS vs their chamber ===")
    for lay in ("Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg"):
        p = os.path.join(d, lay)
        if not os.path.exists(p):
            continue
        dists = []
        missing = 0
        for f, g in rows(p):
            if g is None:
                continue
            parts = list(g.geoms) if hasattr(g, "geoms") else [g]
            ends = [parts[0].coords[0], parts[-1].coords[-1]]
            for field, end in zip(("START_CHAMBER", "END_CHAMBER"), ends):
                cid = f.get(field)
                if not cid or str(cid) not in ch:
                    missing += 1
                    continue
                dists.append(Point(end).distance(ch[str(cid)]))
        print("  %-22s ends measured=%d  unresolved label=%d"
              % (lay, len(dists), missing))
        if dists:
            over = [x for x in dists if x > 1.0]
            print("      >1 m from the labelled chamber: %d (%.0f%%)  "
                  "p50=%.2f p90=%.2f max=%.2f m"
                  % (len(over), 100.0 * len(over) / len(dists),
                     pct(dists, 0.5), pct(dists, 0.9), max(dists)))


if __name__ == "__main__":
    d = sys.argv[1]
    coupler_check(d)
    print()
    chamber_check(d)
