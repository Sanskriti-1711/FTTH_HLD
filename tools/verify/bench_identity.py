"""Isolated correctness + speed check for attr_enrich.stamp_region_identity.

Builds a synthetic output directory (Polygons + point/line layers), runs the
region-stamping pass, prints the stamped values and the elapsed time.  Run it
once with the patched module and once with the previous revision; the printed
``RESULTS_SHA`` must match and the elapsed time must drop.

    "C:/Program Files/QGIS 3.44.6/bin/python-qgis.bat" tmp/bench_identity.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

from osgeo import ogr, osr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from HLDPlanning.utils import attr_enrich  # noqa: E402


def _drv():
    return ogr.GetDriverByName("GPKG")


def _path(d, name):
    return os.path.join(d, name)


def _make_layer(d, name, gtype, fields):
    p = _path(d, name)
    if os.path.exists(p):
        os.remove(p)
    ds = _drv().CreateDataSource(p)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(25833)
    lyr = ds.CreateLayer(name.replace(".gpkg", ""), srs, gtype)
    for fname, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    return ds, lyr


def build(d):
    # 52 polygons on a grid, 60 m squares, spaced 400 m -> most pairs do not
    # overlap, which is exactly what the envelope prefilter must exploit.
    n_side = 8
    poly_ds, poly_lyr = _make_layer(
        d, "Polygons.gpkg", ogr.wkbPolygon,
        [("POLYGON_ID", ogr.OFTString)])
    for i in range(52):
        cx = (i % n_side) * 400.0
        cy = (i // n_side) * 400.0
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((cx, cy), (cx + 60, cy), (cx + 60, cy + 60), (cx, cy + 60), (cx, cy)):
            ring.AddPoint_2D(x, y)
        g = ogr.Geometry(ogr.wkbPolygon)
        g.AddGeometry(ring)
        f = ogr.Feature(poly_lyr.GetLayerDefn())
        f.SetField("POLYGON_ID", "POLY%05d" % i)
        f.SetGeometry(g)
        poly_lyr.CreateFeature(f)
    poly_ds = None

    # 2000 short line spans, each placed inside/near one polygon.
    tr_ds, tr_lyr = _make_layer(
        d, "Final_Trenches.gpkg", ogr.wkbLineString,
        [("TRENCH_ID", ogr.OFTString), ("SURFACE", ogr.OFTString)])
    for i in range(2000):
        cx = (i % n_side) * 400.0 + 10.0
        cy = (i // n_side) * 400.0 + 10.0
        line = ogr.Geometry(ogr.wkbLineString)
        line.AddPoint_2D(cx, cy)
        line.AddPoint_2D(cx + 5.0, cy + 5.0)
        f = ogr.Feature(tr_lyr.GetLayerDefn())
        f.SetField("TRENCH_ID", "TR-%06d" % i)
        f.SetField("SURFACE", "Asphalt")
        f.SetGeometry(line)
        tr_lyr.CreateFeature(f)
    tr_ds = None

    # 400 chambers (points) and 400 trench nodes.
    for name, idf in (("Chambers.gpkg", "STRUCT_ID"), ("Trench_Nodes.gpkg", "NODE_ID")):
        ds, lyr = _make_layer(d, name, ogr.wkbPoint, [(idf, ogr.OFTString)])
        for i in range(400):
            cx = (i % n_side) * 400.0 + 30.0
            cy = (i // n_side) * 400.0 + 30.0
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(cx, cy)
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetField(idf, "%s%06d" % (idf[:2], i))
            f.SetGeometry(pt)
            lyr.CreateFeature(f)
        ds = None

    # A PDP layer so the chamber PDP link also runs.
    pdp_ds, pdp_lyr = _make_layer(d, "PDPs.gpkg", ogr.wkbPoint,
                                  [("PDP_ID", ogr.OFTString)])
    for i in range(52):
        cx = (i % n_side) * 400.0 + 30.0
        cy = (i // n_side) * 400.0 + 30.0
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(cx, cy)
        f = ogr.Feature(pdp_lyr.GetLayerDefn())
        f.SetField("PDP_ID", "PDP%05d" % i)
        f.SetGeometry(pt)
        pdp_lyr.CreateFeature(f)
    pdp_ds = None


CHECKED = (
    ("Final_Trenches.gpkg", "POLYGON_ID", False),
    ("Chambers.gpkg", "POLYGON_ID", True),
    ("Trench_Nodes.gpkg", "POLYGON_ID", True),
    ("Chambers.gpkg", "PDP_ID", "pdp"),
)


def digest(d):
    h = hashlib.sha256()
    for name, field, _kind in CHECKED:
        ds = ogr.Open(_path(d, name), 0)
        lyr = ds.GetLayer(0)
        idx = lyr.GetLayerDefn().GetFieldIndex(field)
        rows = [str(f.GetField(idx)) for f in lyr]
        ds = None
        h.update(("%s.%s=" % (name, field)).encode())
        h.update(",".join(rows).encode())
    return h.hexdigest()


def brute_force(d):
    """Independent reference: the ORIGINAL O(features x polygons) algorithm.

    Written plainly (every polygon for every feature) so it shares no helper
    with the code under test; stamp_region_identity must reproduce it exactly.
    """
    polys = []
    ds = ogr.Open(_path(d, "Polygons.gpkg"), 0)
    lyr = ds.GetLayer(0)
    pid_i = lyr.GetLayerDefn().GetFieldIndex("POLYGON_ID")
    for f in lyr:
        polys.append((str(f.GetField(pid_i)), f.GetGeometryRef().Clone()))
    ds = None

    pdps = []
    ds = ogr.Open(_path(d, "PDPs.gpkg"), 0)
    lyr = ds.GetLayer(0)
    pid_i = lyr.GetLayerDefn().GetFieldIndex("PDP_ID")
    for f in lyr:
        pdps.append((str(f.GetField(pid_i)), f.GetGeometryRef().Clone()))
    ds = None

    out = {}
    for name, field, kind in CHECKED:
        ds = ogr.Open(_path(d, name), 0)
        lyr = ds.GetLayer(0)
        vals = []
        for f in lyr:
            g = f.GetGeometryRef()
            if kind == "pdp":
                pg = ogr.Geometry(ogr.wkbPoint)
                pg.AddPoint_2D(g.GetX(), g.GetY())
                hit = ""
                for pid, pgeom in pdps:
                    if pgeom.Distance(pg) <= 1.0:
                        hit = pid
                        break
                vals.append(hit)
                continue
            if kind is True:            # point layer
                pg = ogr.Geometry(ogr.wkbPoint)
                pg.AddPoint_2D(g.GetX(), g.GetY())
                best, best_d = "", None
                for pid, poly in polys:
                    if poly.Contains(pg):
                        best, best_d = pid, -1.0
                        break
                    dist = poly.Distance(pg)
                    if dist <= 25.0 and (best_d is None or dist < best_d):
                        best, best_d = pid, dist
                vals.append(best)
            else:                        # line layer
                hits = []
                for pid, poly in polys:
                    inter = poly.Intersection(g)
                    if inter is not None and not inter.IsEmpty():
                        L = inter.Length()
                        if L >= 2.0:
                            hits.append((L, pid))
                hits.sort(reverse=True)
                vals.append(",".join(pid for _L, pid in hits))
        ds = None
        out[(name, field)] = vals
    return out


def _norm(v):
    # An unset field is NULL; the reference uses "".  Both mean "no region".
    return "" if v is None else str(v)


def compare(d, expected):
    bad = 0
    for name, field, _kind in CHECKED:
        ds = ogr.Open(_path(d, name), 0)
        lyr = ds.GetLayer(0)
        idx = lyr.GetLayerDefn().GetFieldIndex(field)
        got = [_norm(f.GetField(idx)) for f in lyr]
        ds = None
        exp = [_norm(v) for v in expected[(name, field)]]
        if got != exp:
            bad += 1
            for i, (a, b) in enumerate(zip(got, exp)):
                if a != b:
                    print("MISMATCH %s.%s row %d: got=%r expected=%r"
                          % (name, field, i, a, b))
                    break
    return bad


def check_nearest(d):
    """The gridded _nearest_id must equal a brute-force nearest over the layer."""
    ds = ogr.Open(_path(d, "Chambers.gpkg"), 0)
    lyr = ds.GetLayer(0)
    IDX = lyr.GetLayerDefn().GetFieldIndex("STRUCT_ID")
    ref = []
    for f in lyr:
        g = f.GetGeometryRef()
        ref.append((str(f.GetField(IDX)), g.GetX(), g.GetY()))
    ds = None

    import random
    rng = random.Random(7)
    bad = 0
    for _ in range(3000):
        x = rng.uniform(-100.0, 3300.0)
        y = rng.uniform(-100.0, 3300.0)
        tol = rng.choice((10.0, 15.0))
        best, best_d = "", tol
        dg = ogr.Geometry(ogr.wkbPoint)
        dg.AddPoint_2D(x, y)
        for v, gx, gy in ref:
            pg = ogr.Geometry(ogr.wkbPoint)
            pg.AddPoint_2D(gx, gy)
            dist = pg.Distance(dg)
            if dist <= best_d:
                best_d, best = dist, v
        got = attr_enrich._nearest_id(_path(d, "Chambers.gpkg"), x, y, tol, "STRUCT_ID")
        if got != best:
            bad += 1
            if bad <= 3:
                print("NEAREST MISMATCH (%0.1f,%0.1f,%.0f) got=%r want=%r"
                      % (x, y, tol, got, best))
    ds = None
    return bad


def main():
    d = tempfile.mkdtemp(prefix="bench_identity_")
    try:
        build(d)
        t0 = time.time()
        expected = brute_force(d)
        t_brute = time.time() - t0
        t0 = time.time()
        n = attr_enrich.stamp_region_identity(d, None)
        dt = time.time() - t0
        bad = compare(d, expected)
        print("written=%d new=%.3fs brute=%.3fs mismatched_layers=%d"
              % (n, dt, t_brute, bad))
        nearest_bad = check_nearest(d)
        print("nearest_mismatches=%d" % nearest_bad)
        print("RESULTS_SHA=%s" % digest(d))
        print("CORRECT" if (bad == 0 and nearest_bad == 0) else "INCORRECT")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
