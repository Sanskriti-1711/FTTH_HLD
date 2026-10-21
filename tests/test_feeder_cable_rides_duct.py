"""A feeder cable must lie IN a feeder duct (rule C2).

The two layers are routed by different producers — the duct stage runs before
the cable stage and cannot see it — so they can, and did, take different paths.
Measured on the SERVED payload of project 34b2cfad… (2026-10-21): 1,670 m
(19.7 %) of the feeder cable had no feeder duct within 0.5 m, and 7 of its 28
PDPs sat 40-258 m off the duct network while the cable reached them.

`attr_enrich.extend_feeder_ducts_along_cables` lays the duct along the cable's
own route (the cable is the layer that is already on the trench), so the rule
holds by construction instead of by two producers happening to agree.

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_feeder_cable_rides_duct.py
"""

from __future__ import annotations

import os

import pytest
from osgeo import ogr, osr

from HLDPlanning.utils.attr_enrich import (
    _FEEDER_COVER_M,
    extend_feeder_ducts_along_cables,
)


def _srs():
    s = osr.SpatialReference()
    s.ImportFromEPSG(3857)
    return s


def _line(path, coords, fields=(), values=(), name="Layer"):
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer(name, _srs(), ogr.wkbLineString)
    for fname, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    feat = ogr.Feature(lyr.GetLayerDefn())
    line = ogr.Geometry(ogr.wkbLineString)
    for x, y in coords:
        line.AddPoint_2D(x, y)
    feat.SetGeometry(line)
    for fname, value in values:
        feat.SetField(fname, value)
    lyr.CreateFeature(feat)
    ds = None


def _point(path, points, id_field="STRUCT_ID"):
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer("Chambers", _srs(), ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn(id_field, ogr.OFTString))
    for x, y, sid in points:
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({x} {y})"))
        feat.SetField(id_field, sid)
        lyr.CreateFeature(feat)
    ds = None


CABLE_FIELDS = [
    ("cable_id", ogr.OFTInteger),
    ("CABLE_TYPE", ogr.OFTString),
    ("PDP_IDS", ogr.OFTString),
    ("POLYGON_ID", ogr.OFTString),
    ("MFG_ID", ogr.OFTString),
]
DUCT_FIELDS = [
    ("RUN_ID", ogr.OFTString),
    ("SPAN_KIND", ogr.OFTString),
    ("DUCT_TYPE", ogr.OFTString),
    ("WAYS", ogr.OFTInteger),
    ("DIAMETER_MM", ogr.OFTInteger),
    ("color", ogr.OFTString),
    ("START_CHAMBER", ogr.OFTString),
    ("END_CHAMBER", ogr.OFTString),
]


def _fixture(tmp_path, duct_coords, cable_coords, chambers=(), trenches=()):
    d = str(tmp_path)
    _line(os.path.join(d, "Feeder_Ducts.gpkg"), duct_coords, DUCT_FIELDS,
          (("RUN_ID", "RUN-00001"), ("SPAN_KIND", "Chamber span"),
           ("DUCT_TYPE", "4-Way HDPE"), ("WAYS", 4), ("DIAMETER_MM", 110),
           ("color", "#d81b60")))
    _line(os.path.join(d, "Feeder_Cable.gpkg"), cable_coords, CABLE_FIELDS,
          (("cable_id", 7), ("CABLE_TYPE", "Feeder"),
           ("PDP_IDS", "PDP00001,PDP00002"), ("POLYGON_ID", "POLY00001"),
           ("MFG_ID", "MFG00001")))
    _point(os.path.join(d, "Chambers.gpkg"), chambers)
    if trenches:
        _line(os.path.join(d, "Final_Trenches.gpkg"), trenches, [("TRENCH_ID", ogr.OFTString)],
              (("TRENCH_ID", "TR-000001"),))
    return d


def _geoms(path):
    ds = ogr.Open(path, 0)
    lyr = ds.GetLayer(0)
    return [f.GetGeometryRef().Clone() for f in lyr]


def _rows(path):
    ds = ogr.Open(path, 0)
    return ds.GetLayer(0).GetFeatureCount()


def _off_duct(run_dir, tol=_FEEDER_COVER_M):
    """Metres of feeder cable more than `tol` from every feeder duct."""
    ducts = _geoms(os.path.join(run_dir, "Feeder_Ducts.gpkg"))
    cover = ducts[0].Buffer(tol)
    for g in ducts[1:]:
        cover = cover.Union(g.Buffer(tol))
    off = 0.0
    for cable in _geoms(os.path.join(run_dir, "Feeder_Cable.gpkg")):
        gap = cable.Difference(cover)
        if gap is not None and not gap.IsEmpty():
            off += gap.Length()
    return off


def test_a_cable_leg_with_no_duct_gets_one(tmp_path):
    """The 60 m run past the end of the duct is ducted, not redrawn."""
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (0, 160)])

    assert _off_duct(d) == pytest.approx(60.0, abs=1.0)
    added = extend_feeder_ducts_along_cables(d)

    assert added >= 1
    assert _off_duct(d) == pytest.approx(0.0, abs=0.05)


def test_a_cable_already_in_a_duct_adds_nothing(tmp_path):
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (0, 100)])

    assert extend_feeder_ducts_along_cables(d) == 0
    assert _rows(os.path.join(d, "Feeder_Ducts.gpkg")) == 1


def test_the_fill_is_cut_at_the_chambers(tmp_path):
    """D3: a duct breaks at a chamber. The 200 m gap straddles one."""
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (0, 300)],
                 chambers=[(0, 0, "HH-0001"), (0, 100, "HH-0002"), (0, 200, "HH-0003")])

    added = extend_feeder_ducts_along_cables(d)

    assert added >= 2
    ds = ogr.Open(os.path.join(d, "Feeder_Ducts.gpkg"), 0)
    lyr = ds.GetLayer(0)
    fills = [f for f in lyr
             if str(f.GetField("RUN_ID") or "").startswith("RUN-FILL-")]
    assert fills, "no fill rows written"
    named = [(f.GetField("START_CHAMBER"), f.GetField("END_CHAMBER")) for f in fills]
    assert any(s and e for s, e in named), f"no chamber-to-chamber fill: {named}"


def test_the_fill_carries_the_feeder_profile_and_identity(tmp_path):
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (0, 200)],
                 trenches=[(0, 0), (0, 300)])

    extend_feeder_ducts_along_cables(d)

    ds = ogr.Open(os.path.join(d, "Feeder_Ducts.gpkg"), 0)
    lyr = ds.GetLayer(0)
    fill = next(f for f in lyr
                if str(f.GetField("RUN_ID") or "").startswith("RUN-FILL-"))
    assert fill.GetField("DUCT_TYPE") == "4-Way HDPE"
    assert fill.GetField("WAYS") == 4
    assert fill.GetField("WAYS_TOTAL") == 4
    assert fill.GetField("cables_carried") == "7"
    assert fill.GetField("pdp_ids") == "PDP00001,PDP00002"
    assert fill.GetField("mfg_id") == "MFG00001"
    assert fill.GetField("PARENT_TRENCH") == "TR-000001"
    assert fill.GetField("SPAN_KIND") in ("Chamber span", "Duct tail")
    assert fill.GetField("REVIEW") == 0


def test_a_second_run_adds_nothing(tmp_path):
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (0, 220)])

    assert extend_feeder_ducts_along_cables(d) >= 1
    rows = _rows(os.path.join(d, "Feeder_Ducts.gpkg"))
    assert extend_feeder_ducts_along_cables(d) == 0
    assert _rows(os.path.join(d, "Feeder_Ducts.gpkg")) == rows


def test_only_the_feeder_cable_is_ducted(tmp_path):
    """A drop cable in the same file is not a feeder route."""
    d = _fixture(tmp_path, [(0, 0), (0, 100)], [(0, 0), (1000, 0)])
    ds = ogr.Open(os.path.join(d, "Feeder_Cable.gpkg"), 1)
    lyr = ds.GetLayer(0)
    f = lyr.GetNextFeature()
    f.SetField("CABLE_TYPE", "Drop")
    lyr.SetFeature(f)
    ds = None

    assert extend_feeder_ducts_along_cables(d) == 0

