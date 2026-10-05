"""Which trench are the ducts and cables really built on?

A designer run writes BOTH `Final_Trenches.gpkg` (the designer's published
network) and the legacy per-tier `Feeder_Trench.gpkg` / `Distribution_Trench.gpkg`
/ `Garden_Trench.gpkg`. This measures the two candidate references against each
other and against the duct/cable layers, so "everything must sit on the designer
trenches" is a number and not an impression.

Usage (QGIS python):
    . tmp/qgis_env.sh && "$QGSPY" tmp/trench_provenance_check.py <run_dir> [...]
"""
import os
import sys
from osgeo import ogr

REF = "Final_Trenches.gpkg"
LEGACY = ["Feeder_Trench.gpkg", "Distribution_Trench.gpkg", "Garden_Trench.gpkg"]
FOLLOW = [("Feeder_Ducts.gpkg", "feeder_ducts"),
          ("Distribution_Ducts.gpkg", "distribution_ducts"),
          ("Drop_Ducts.gpkg", "drop_ducts"),
          ("Feeder_Cable.gpkg", "feeder_cable"),
          ("Distribution_Cable.gpkg", "distribution_cable"),
          ("Chambers.gpkg", "chambers"),
          ("Coupleurs.gpkg", "coupleurs")]


def load(run_dir, fname):
    path = os.path.join(run_dir, fname)
    if not os.path.exists(path):
        return []
    ds = ogr.Open(path)
    if ds is None:
        return []
    out = [f.GetGeometryRef().Clone() for f in ds.GetLayer(0)
           if f.GetGeometryRef() is not None]
    ds = None
    return out


def total_len(geoms):
    return sum(g.Length() for g in geoms)


def reach(ref_geoms, geoms):
    """(max, count>1m) distance from each geom to the nearest reference geom."""
    if not geoms or not ref_geoms:
        return None
    worst, over = 0.0, 0
    for g in geoms:
        d = min(r.Length() if False else r.Distance(g) for r in ref_geoms)
        worst = max(worst, d)
        if d > 1.0:
            over += 1
    return worst, over


def main():
    for run_dir in sys.argv[1:]:
        print("=" * 78)
        print(run_dir)
        final = load(run_dir, REF)
        if not final:
            print("  no %s — not a designer run" % REF)
            continue
        legacy = []
        for f in LEGACY:
            g = load(run_dir, f)
            if g:
                legacy.extend(g)
                print("  %-26s n=%-5d length=%10.1f m" % (f, len(g), total_len(g)))
        print("  %-26s n=%-5d length=%10.1f m" % (REF, len(final), total_len(final)))
        print("")

        if legacy:
            r = reach(final, legacy)
            print("  legacy spans vs Final_Trenches : max %.2f m, %d of %d > 1 m"
                  % (r[0], r[1], len(legacy)))
            r = reach(legacy, final)
            print("  Final_Trenches vs legacy spans : max %.2f m, %d of %d > 1 m"
                  % (r[0], r[1], len(final)))
            print("  -> the two references are %s"
                  % ("the SAME geometry (a comparison cannot tell them apart)"
                     if r[0] < 1.0 else "DIFFERENT geometries"))
        else:
            print("  no legacy per-tier trench files in this run")
        print("")

        print("  layer                     on Final_Trenches   on legacy per-tier")
        for fname, label in FOLLOW:
            geoms = load(run_dir, fname)
            if not geoms:
                continue
            a = reach(final, geoms)
            b = reach(legacy, geoms) if legacy else None
            print("  %-24s max %8.2f (%3d>1m)  %s" %
                  (label, a[0], a[1],
                   ("max %8.2f (%3d>1m)" % b if b else "-")))


if __name__ == "__main__":
    main()
