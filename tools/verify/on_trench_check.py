"""Do the ducts, cables, couplers and chambers actually lie on the trench?

WHY THIS MEASURES LENGTH, NOT DISTANCE
--------------------------------------
The first version of this check reported, per feature, the distance to the
nearest Final_Trenches span. That number can NEVER show a layer that leaves the
trench: a duct whose nearest point sits on a trench scores 0.00 m however far
the rest of it wanders. Berlin's 2026-09-21 run therefore read "every layer
0.00 m, nothing beyond 5 m" (rule D10 declared satisfied) while **49.1 % of the
distribution duct length and 19.9 % of the feeder duct length** was not on the
trench network at all — 2,076 m of it in single-segment chords up to 123 m long,
because chamber segmenting copied chamber coordinates into the duct (fixed in
``attr_enrich.segment_ducts_at_chambers``) and the distribution tap drew straight
spurs to the couplers (fixed in ``duct_layer.DuctLayer._attach_taps``).

So the check now reports BOTH:
  * ``off_m`` / ``off_%`` — how much of the layer's LENGTH lies outside the
    trench corridor (trench network buffered by ``--tol`` m). For points, the
    distance from the point to the network is the equivalent figure.
  * the original nearest-feature distance percentiles, kept because older run
    notes quote them — they are a floor, not a verdict.

Usage: python tmp/on_trench_check.py <run_dir> [tolerance_m]
"""
import os
import sys

from osgeo import ogr
from shapely import wkb
from shapely.ops import unary_union

LAYERS = [
    ("Feeder_Ducts.gpkg", "feeder_ducts", "Feeder"),
    ("Distribution_Ducts.gpkg", "distribution_ducts", "Distribution"),
    ("Drop_Ducts.gpkg", "drop_ducts", "Garden"),
    ("Feeder_Ducts_Runs.gpkg", "feeder_ducts_runs", "Feeder"),
    ("Distribution_Ducts_Runs.gpkg", "distribution_ducts_runs", "Distribution"),
    ("Feeder_Cable.gpkg", "feeder_cable", "Feeder"),
    ("Distribution_Cable.gpkg", "distribution_cable", "Distribution"),
    ("Coupleurs.gpkg", "coupleurs", None),
    ("Chambers.gpkg", "chambers", None),
    ("Trench_Nodes.gpkg", "trench_nodes", None),
    ("Poles.gpkg", "poles", None),
]


def load(path):
    """Every feature as ({attrs}, shapely geom)."""
    if not os.path.exists(path):
        return []
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        out.append(({n: f.GetField(n) for n in names},
                    wkb.loads(bytes(g.ExportToWkb()))))
    ds = None
    return out


def tier(props):
    for k in ("SERVES_TIER", "TRENCH_TIER", "trench_tier", "tier", "TIER"):
        v = props.get(k)
        if v:
            return str(v).strip().lower()
    return ""


def is_line(geom):
    return geom.geom_type in ("LineString", "MultiLineString",
                              "LinearRing", "GeometryCollection")


def main(d, tol_m=0.5):
    trench = load(os.path.join(d, "Final_Trenches.gpkg"))
    if not trench:
        print("no Final_Trenches.gpkg in %s" % d)
        return 1
    tl = [g for _, g in trench]
    network = unary_union(tl)
    corridor = network.buffer(tol_m, cap_style=2, join_style=2)
    print("trenches: %d | corridor = network + %.2f m" % (len(tl), tol_m))
    print()
    print("%-24s %5s %9s %9s %7s %6s | %6s %7s"
          % ("layer", "n", "len_m", "off_m", "off_%", ">1m",
             "near90", "near_max"))
    for fname, label, want in LAYERS:
        feats = load(os.path.join(d, fname))
        if not feats:
            print("%-24s %5s   (missing)" % (label, "-"))
            continue
        pool = ([t for a, t in trench if tier(a).startswith(want.lower())]
                if want else tl)
        if not pool:
            pool = tl
        near_any = sorted(min(g.distance(t) for t in tl) for _, g in feats)
        near_own = sorted(min(g.distance(t) for t in pool) for _, g in feats)

        def pct(a, q):
            return a[min(len(a) - 1, int(len(a) * q))] if a else 0.0

        lines = [(a, g) for a, g in feats if is_line(g)]
        if lines:
            total = sum(g.length for _, g in lines)
            off = sum(g.difference(corridor).length for _, g in lines)
            over = sum(1 for _, g in lines
                       if g.difference(corridor).length > 1.0)
            print("%-24s %5d %9.1f %9.1f %6.1f%% %6d | %6.2f %7.2f"
                  % (label, len(feats), total, off,
                     100.0 * off / total if total else 0.0, over,
                     pct(near_own, 0.9), near_own[-1]))
        else:
            over = sum(1 for x in near_any if x > 1.0)
            print("%-24s %5d %9s %9.1f %9s %6d | %6.2f %7.2f"
                  % (label, len(feats), "-", pct(near_any, 0.5), "-", over,
                     pct(near_own, 0.9), near_own[-1]))
    print()
    print("off_% is the verdict: a layer is on the trench only when it is ~0 %.")
    print("near90/near_max are nearest-feature distances (a floor, not a verdict).")
    return 0


if __name__ == "__main__":
    run_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    tol = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    sys.exit(main(run_dir, tol))
