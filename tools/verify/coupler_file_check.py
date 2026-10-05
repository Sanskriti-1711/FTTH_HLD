"""Couplers/chambers vs trench in the engine's OWN GPKG output.

Answers the ingest question: is a coupler that floats on the platform map
already floating in the run the engine wrote, or did publishing move it?

Usage (needs the QGIS python):
    . tmp/qgis_env.sh && "$QGSPY" tmp/coupler_file_check.py <run_dir> [...]
"""
import sys
from osgeo import ogr, osr

LAYERS = [("Final_Trenches.gpkg", "trench"),
          ("Coupleurs.gpkg", "couplers"),
          ("Chambers.gpkg", "chambers"),
          ("Drop_Ducts.gpkg", "drop_ducts"),
          ("Distribution_Ducts.gpkg", "distribution_ducts")]


def load(run_dir, fname):
    path = run_dir.rstrip("/") + "/" + fname
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    out = []
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        out.append(g.Clone())
    ds = None
    return out


def seg_dist(geoms_lines, g):
    best = 1e18
    for ln in geoms_lines:
        d = ln.Distance(g)
        if d < best:
            best = d
    return best


def main():
    for run_dir in sys.argv[1:]:
        print("=" * 76)
        print(run_dir)
        trench = load(run_dir, "Final_Trenches.gpkg")
        if not trench:
            print("  no Final_Trenches.gpkg")
            continue
        ref = ogr.Geometry(ogr.wkbGeometryCollection)
        for g in trench:
            ref.AddGeometry(g)
        srs = trench[0].GetSpatialReference()
        code = srs.GetAuthorityCode(None) if srs else "?"
        print("  trenches=%d  crs=EPSG:%s" % (len(trench), code))
        for fname, label in LAYERS[1:]:
            geoms = load(run_dir, fname)
            if not geoms:
                continue
            dists = [seg_dist(trench, g) for g in geoms]
            dists.sort()
            over = sum(1 for d in dists if d > 5.0)
            print("  %-20s n=%-5d p50=%8.2f p90=%8.2f max=%8.2f  >5m=%d"
                  % (label, len(dists), dists[len(dists) // 2],
                     dists[min(len(dists) - 1, int(round(0.9 * (len(dists) - 1))))],
                     dists[-1], over))


if __name__ == "__main__":
    main()
