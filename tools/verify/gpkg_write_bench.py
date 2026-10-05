"""Is the enrichment cost really autocommit-per-row?

Times the same feature rewrites the way the passes actually do them (read a
feature, SetField, SetFeature — no geometry mutation) three ways:
  1. plain SetFeature (what every pass does today)
  2. inside one StartTransaction/CommitTransaction
  3. plain, with OGR_SQLITE_SYNCHRONOUS=OFF

Usage: python tmp/gpkg_write_bench.py <src_run_dir>
"""
import os
import shutil
import sys
import time

from osgeo import gdal, ogr

ROOT = os.path.abspath(".")
SCRATCH = "BENCH_MARK"


def bench(path, n, mode):
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayer(0)
    if lyr.GetLayerDefn().GetFieldIndex(SCRATCH) < 0:
        lyr.CreateField(ogr.FieldDefn(SCRATCH, ogr.OFTInteger))
    i = lyr.GetLayerDefn().GetFieldIndex(SCRATCH)
    fids = []
    for k, f in enumerate(lyr):
        if k >= n:
            break
        fids.append(f.GetFID())
    t0 = time.time()
    if mode == "tx":
        lyr.StartTransaction()
    for fid in fids:
        f = lyr.GetFeature(fid)
        f.SetField(i, (fid % 7) + 1)
        lyr.SetFeature(f)
        del f
    if mode == "tx":
        lyr.CommitTransaction()
    dt = time.time() - t0
    ds = None
    return dt, len(fids)


def main(src):
    work = os.path.join(ROOT, "tmp", "bench.gpkg")
    src_layer = os.path.join(src, "Final_Trenches.gpkg")
    if not os.path.exists(src_layer):
        raise SystemExit("no Final_Trenches.gpkg in %s" % src)

    res = {}
    for mode in ("plain", "tx"):
        shutil.copy2(src_layer, work)
        dt, n = bench(work, 400, mode)
        res[mode] = dt
        print("  %-7s %4d writes in %7.2fs  (%.1f ms/write)"
              % (mode, n, dt, dt / max(1, n) * 1000), flush=True)

    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    shutil.copy2(src_layer, work)
    dt, n = bench(work, 400, "plain")
    res["sync_off"] = dt
    print("  %-7s %4d writes in %7.2fs  (%.1f ms/write)  [synchronous=OFF]"
          % ("plain*", n, dt, dt / max(1, n) * 1000), flush=True)

    if os.path.exists(work):
        os.remove(work)
    print("\n  speedup vs plain:  transactions %.0fx   synchronous=OFF %.0fx"
          % (res["plain"] / max(0.001, res["tx"]),
             res["plain"] / max(0.001, res["sync_off"])))


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]))
