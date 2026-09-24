"""Duct chamber segmentation must not move the duct off its trench (rule D10).

The published duct layers are cut chamber to chamber by
`attr_enrich.segment_ducts_at_chambers`. That pass also used to *pull* each
span's end vertices onto the chamber it was labelled with, copying the
chamber's coordinates into the duct. A chamber is only ever hand-placed *near*
the duct (measured p50 0.8 m, p90 5.6 m off its line), so the move was mostly
lateral — and because a published span is a coarse 2-6 point polyline, moving
one end by up to the 10 m tolerance swung the whole span off the trench.

Measured on the 2026-09-21 Berlin run, before the fix: 57 % of published feeder
span ends sat exactly (<=1 mm) on a chamber and the feeder layer drifted from
0.0 % to 19.9 % of its length off the trench network (distribution 21.1 % ->
49.1 %). After: feeder 0.0 %, distribution 20.8 %.

These run under the QGIS interpreter (they only need OGR, but that is the
interpreter the suite uses):

    ./HLD_Planning_01/tools/qgis_python.cmd \
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import os

import pytest
from osgeo import ogr, osr

from HLDPlanning.utils.attr_enrich import (
    _segment_layer_at_chambers,
    segment_ducts_at_chambers,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

# A duct running north along x=0, with its chambers hand-placed 5 m to the side.
DUCT = [(0.0, 0.0), (0.0, 100.0)]
CHAMBERS = [(5.0, 0.0, "HH-0001"), (5.0, 50.0, "HH-0002"), (5.0, 100.0, "HH-0003")]


def _srs():
    s = osr.SpatialReference()
    s.ImportFromEPSG(3857)
    return s


def _write_line_gpkg(path, coords):
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer("Ducts", _srs(), ogr.wkbMultiLineString)
    lyr.CreateField(ogr.FieldDefn("DUCT_ID", ogr.OFTString))
    f = ogr.Feature(lyr.GetLayerDefn())
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    ls = ogr.Geometry(ogr.wkbLineString)
    for x, y in coords:
        ls.AddPoint_2D(x, y)
    ml.AddGeometry(ls)
    f.SetGeometry(ml)
    f.SetField("DUCT_ID", "D-1")
    lyr.CreateFeature(f)
    ds = None


def _write_point_gpkg(path, points):
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer("Chambers", _srs(), ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("STRUCT_ID", ogr.OFTString))
    for x, y, sid in points:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({x} {y})"))
        f.SetField("STRUCT_ID", sid)
        lyr.CreateFeature(f)
    ds = None


def _vertices(path):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    pts = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        for i in range(g.GetGeometryCount() or 1):
            part = g.GetGeometryRef(i) if g.GetGeometryCount() else g
            for j in range(part.GetPointCount()):
                pts.append(part.GetPoint_2D(j))
    return pts


def _rows(path):
    ds = ogr.Open(path)
    return ds.GetLayer(0).GetFeatureCount()


def test_duct_segmentation_cuts_at_chambers_but_never_moves_the_duct(tmp_path):
    """The pass still publishes chamber-to-chamber spans — the duct just stays put."""
    feeder = str(tmp_path / "Feeder_Ducts.gpkg")
    dist = str(tmp_path / "Distribution_Ducts.gpkg")
    chambers = str(tmp_path / "Chambers.gpkg")
    _write_line_gpkg(feeder, DUCT)
    _write_line_gpkg(dist, DUCT)
    _write_point_gpkg(chambers, CHAMBERS)

    assert _rows(feeder) == 1
    segment_ducts_at_chambers(feeder, dist, chambers, None)

    # It really did cut the run into chamber-to-chamber spans...
    assert _rows(feeder) == 2
    # ...and every vertex is still on the duct's own line: the chambers sit at
    # x = 5, and a single one of them reaching a duct vertex is the bug.
    xs = [x for x, _y in _vertices(feeder)]
    assert xs, "the segmented layer has no vertices"
    assert max(abs(x) for x in xs) == pytest.approx(0.0, abs=1e-9)


def test_the_snap_is_what_moved_the_geometry(tmp_path):
    """Pins the mechanism: with snapping on, the same input drifts to x = 5."""
    duct = str(tmp_path / "Ducts.gpkg")
    _write_line_gpkg(duct, DUCT)

    _segment_layer_at_chambers(duct, CHAMBERS, None, "Ducts", 10.0,
                               span_len_fields=(), snap_ends_m=10.0,
                               end_tol_m=10.0)

    xs = [x for x, _y in _vertices(duct)]
    # Every span end was teleported onto a chamber — off the duct's own line.
    assert max(abs(x) for x in xs) > 4.9
