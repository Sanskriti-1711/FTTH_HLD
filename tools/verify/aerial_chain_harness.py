"""Standalone smoke test for the aerial chain (Pole Layer -> Aerial Drop Layer).

The aerial branch was unreachable for so long that its routing loop had never
executed; running the whole 10-minute pipeline to find the next crash is a
waste.  This runs the two aerial stages on their own against a finished run's
trenches, so a fix can be verified in seconds.

Usage: python tmp/aerial_chain_harness.py <output_dir>
"""
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.abspath(".")
QGIS = r"C:\Program Files\QGIS 3.44.6\bin\qgis_process-qgis.bat"
PLUGIN = os.path.join(ROOT, "HLD_Planning_01")
PY312SITE = r"C:\Program Files\QGIS 3.44.6\apps\Python312\Lib\site-packages"

from osgeo import ogr  # noqa: E402


def _read_layer(path):
    ds = ogr.Open(path)
    if ds is None:
        raise SystemExit("cannot open %s" % path)
    return ds, ds.GetLayer(0)


def build_premises(out_dir, dest):
    """Objects flagged aerial_required=1 for the addresses Aerial_Drops names."""
    drops = os.path.join(out_dir, "Aerial_Drops.gpkg")
    objects = os.path.join(out_dir, "Objects.gpkg")
    dds, dlyr = _read_layer(drops)
    addrs = set()
    for f in dlyr:
        v = f.GetField("addr_id")
        if v not in (None, ""):
            addrs.add(str(v).strip())
    print("addresses classified aerial:", sorted(addrs))

    ods, olyr = _read_layer(objects)
    if olyr.GetLayerDefn().GetFieldIndex("ADDR_ID") < 0:
        raise SystemExit("Objects has no ADDR_ID")

    drv = ogr.GetDriverByName("GeoJSON")
    if os.path.exists(dest):
        drv.DeleteDataSource(dest)
    out = drv.CreateDataSource(dest)
    clone = out.CreateLayer("premises", olyr.GetSpatialRef(), ogr.wkbPoint)
    clone.CreateField(ogr.FieldDefn("aerial_required", ogr.OFTInteger))
    clone.CreateField(ogr.FieldDefn("addr_id", ogr.OFTString))

    n = 0
    for f in olyr:
        v = f.GetField("ADDR_ID")
        if v is None or str(v).strip() not in addrs:
            continue
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        nf = ogr.Feature(clone.GetLayerDefn())
        nf.SetGeometry(ogr.CreateGeometryFromWkb(g.ExportToWkb()))
        nf.SetField("aerial_required", 1)
        nf.SetField("addr_id", str(v).strip())
        clone.CreateFeature(nf)
        n += 1
    print("premises written:", n, "->", dest)
    out, ods, dds = None, None, None
    return n


def _q(p):
    return '"%s"' % p if " " in p else p


def _run(args, label):
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QGIS_PLUGINPATH"] = PLUGIN
    env["PYTHONPATH"] = PY312SITE
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(" ".join(_q(a) for a in args), cwd=ROOT, env=env,
                          shell=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    text = proc.stdout + "\n" + proc.stderr
    for line in text.splitlines():
        if re.search(r"[Aa]erial|pole|Pole|error|Error|Traceback|TypeError|"
                     r"execution|ERROR|complete", line):
            print("  |", line.strip()[:170])
    print("%s exit=%s" % (label, proc.returncode))
    return proc.returncode


def main(out_dir):
    tmp = os.path.join(ROOT, "tmp")
    zones = os.path.abspath(os.path.join(
        out_dir, os.pardir, "0dc85304582e40f4bef4981bf57d1db5",
        "design", "Aerial_Zones.geojson"))
    if not os.path.exists(zones):
        raise SystemExit("aerial zones not found: %s" % zones)

    poles = os.path.join(tmp, "harness_poles.gpkg")
    legs = os.path.join(out_dir, "Aerial_Drops.gpkg")
    for p in (poles,):
        if os.path.exists(p):
            os.remove(p)

    print("=== Pole Layer ===")
    rc = _run([QGIS, "run", "hldplanning:08_pole_layer", "--",
               "INPUT_GARDEN_TRENCHES=%s" % _q(os.path.join(out_dir, "Garden_Trench.gpkg")),
               "INPUT_FEEDER_TRENCHES=%s" % _q(os.path.join(out_dir, "Feeder_Trench.gpkg")),
               "INPUT_PDP=%s" % _q(os.path.join(out_dir, "PDPs.gpkg")),
               "INPUT_AERIAL_ZONES=%s" % _q(zones),
               "INPUT_AERIAL_LEGS=%s" % _q(legs),
               "POLE_SPACING_M=50",
               "OUT_POLES=%s" % _q(poles)], "pole")
    if rc != 0:
        return rc

    premises = os.path.join(tmp, "aerial_premises.geojson")
    if build_premises(out_dir, premises) == 0:
        raise SystemExit("no premises flagged - nothing to test")

    out_t = os.path.join(tmp, "harness_aerial_trench.gpkg")
    out_c = os.path.join(tmp, "harness_aerial_cable.gpkg")
    for p in (out_t, out_c):
        if os.path.exists(p):
            os.remove(p)

    print("=== Aerial Drop Layer ===")
    rc = _run([QGIS, "run", "hldplanning:09_aerial_drop_layer", "--",
               "INPUT_PREMISES=%s" % _q(premises),
               "INPUT_POLES=%s" % _q(poles),
               "INPUT_AERIAL_ZONES=%s" % _q(zones),
               "INPUT_AERIAL_LEGS=%s" % _q(legs),
               "POLE_SPACING_M=50",
               "OUT_AERIAL_TRENCH=%s" % _q(out_t),
               "OUT_AERIAL_CABLE=%s" % _q(out_c)], "aerial")
    if rc != 0:
        return rc

    print("=== output ===")
    for path, label in ((poles, "Poles"), (out_t, "Aerial_Drop_Trenches"),
                        (out_c, "Aerial_Cable")):
        if not os.path.exists(path):
            print(label, "-> not written")
            continue
        ds = ogr.Open(path)
        lyr = ds.GetLayer(0)
        names = [lyr.GetLayerDefn().GetFieldDefn(j).GetName()
                 for j in range(lyr.GetLayerDefn().GetFieldCount())]
        print(label, "->", lyr.GetFeatureCount(), "features")
        for i, f in enumerate(lyr):
            if i >= 6:
                break
            row = {n: f.GetField(n) for n in names
                   if n in ("POLE_ID", "EQUIPMENT", "AERIAL_TRENCH_ID",
                            "AERIAL_REASON", "LENGTH_M", "FROM_POLE",
                            "TO_PREMISE")}
            print("   ", row)
        ds = None
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1])))
