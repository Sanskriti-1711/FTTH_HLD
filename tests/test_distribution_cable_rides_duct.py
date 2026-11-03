"""A distribution TRUNK cable must lie IN a distribution duct (rule D5).

Distribution ducts do not come from `duct_layer._build_route_ducts` —
`USE_ROUTE_BASED_DISTRIBUTION` is False, so they come from the embedded
algorithm plus `_rebase_distribution_output`, and the containment union inside
the route builder never runs for this tier.

Measured on run e31739ed (2026-11-03, Berlin 12105 drawn section, v1.7.60):

    Distribution trunk -> Distribution_Ducts    49.3 %  (5,551 of 11,252 m)
    Distribution drop  -> Drop_Ducts           100.0 %
    Feeder cable       -> Feeder_Ducts          99.1 %

The two tiers that go through the route builder are covered; the one that does
not is half-covered. `attr_enrich.extend_distribution_ducts_along_cables` lays
the missing duct along the cable's own route, so the rule holds by construction.

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_distribution_cable_rides_duct.py
"""

from __future__ import annotations

import os

import pytest
from osgeo import ogr, osr

from HLDPlanning.utils.attr_enrich import (
    _FEEDER_COVER_M,
    extend_distribution_ducts_along_cables,
)


def _srs():
    s = osr.SpatialReference()
    s.ImportFromEPSG(3857)
    return s


def _write_line(path, rows, fields, name="Layer"):
    """rows = [(coords, {field: value}), ...]"""
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer(name, _srs(), ogr.wkbLineString)
    for fname, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    for coords, values in rows:
        feat = ogr.Feature(lyr.GetLayerDefn())
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in coords:
            line.AddPoint_2D(x, y)
        feat.SetGeometry(line)
        for fname, value in values.items():
            feat.SetField(fname, value)
        lyr.CreateFeature(feat)
    ds = None


CABLE_FIELDS = [
    ("cable_id", ogr.OFTInteger),
    ("CABLE_TYPE", ogr.OFTString),
    ("PDP_IDS", ogr.OFTString),
    ("POLYGON_ID", ogr.OFTString),
]
DUCT_FIELDS = [
    ("RUN_ID", ogr.OFTString),
    ("SPAN_KIND", ogr.OFTString),
    ("DUCT_TYPE", ogr.OFTString),
    ("WAYS", ogr.OFTInteger),
    ("DIAMETER_MM", ogr.OFTInteger),
    ("color", ogr.OFTString),
]


def _fixture(tmp_path, duct_rows, cable_rows):
    d = str(tmp_path)
    _write_line(os.path.join(d, "Distribution_Ducts.gpkg"), duct_rows, DUCT_FIELDS)
    _write_line(os.path.join(d, "Distribution_Cable.gpkg"), cable_rows, CABLE_FIELDS)
    return d


def _duct_row(coords, run_id="RUN-00001", ways=4, dtype="2-Way HDPE"):
    return (coords, {"RUN_ID": run_id, "SPAN_KIND": "Chamber span",
                     "DUCT_TYPE": dtype, "WAYS": ways, "DIAMETER_MM": 50,
                     "color": "#1e88e5"})


def _cable_row(coords, ctype="Distribution", cable_id=7):
    return (coords, {"cable_id": cable_id, "CABLE_TYPE": ctype,
                     "PDP_IDS": "PDP00001", "POLYGON_ID": "POLY00001"})


def _geoms(path):
    ds = ogr.Open(path, 0)
    return [f.GetGeometryRef().Clone() for f in ds.GetLayer(0)]


def _rows(path):
    ds = ogr.Open(path, 0)
    return ds.GetLayer(0).GetFeatureCount()


def _off_duct(run_dir, tol=_FEEDER_COVER_M):
    """Metres of DISTRIBUTION trunk cable more than `tol` from every duct."""
    ducts = _geoms(os.path.join(run_dir, "Distribution_Ducts.gpkg"))
    cover = ducts[0].Buffer(tol)
    for g in ducts[1:]:
        cover = cover.Union(g.Buffer(tol))
    off = 0.0
    for cable in _geoms(os.path.join(run_dir, "Distribution_Cable.gpkg")):
        gap = cable.Difference(cover)
        if gap is not None and not gap.IsEmpty():
            off += gap.Length()
    return off


def test_a_trunk_leg_with_no_duct_gets_one(tmp_path):
    d = _fixture(tmp_path, [_duct_row([(0, 0), (0, 100)])],
                 [_cable_row([(0, 0), (0, 160)])])

    assert _off_duct(d) == pytest.approx(60.0, abs=1.0)
    added = extend_distribution_ducts_along_cables(d)

    assert added >= 1
    assert _off_duct(d) == pytest.approx(0.0, abs=0.05)


def test_a_drop_cable_is_never_pulled_into_the_distribution_duct(tmp_path):
    """A garden leg belongs to Drop_Ducts; clubbing it in would duct the garden."""
    d = _fixture(tmp_path, [_duct_row([(0, 0), (0, 100)])],
                 [_cable_row([(0, 0), (1000, 0)], ctype="Drop")])

    assert extend_distribution_ducts_along_cables(d) == 0
    assert _rows(os.path.join(d, "Distribution_Ducts.gpkg")) == 1


def test_a_trunk_already_in_a_duct_adds_nothing(tmp_path):
    d = _fixture(tmp_path, [_duct_row([(0, 0), (0, 100)])],
                 [_cable_row([(0, 0), (0, 100)])])

    assert extend_distribution_ducts_along_cables(d) == 0
    assert _rows(os.path.join(d, "Distribution_Ducts.gpkg")) == 1


def test_the_fill_carries_the_published_distribution_profile(tmp_path):
    d = _fixture(tmp_path, [_duct_row([(0, 0), (0, 100)], ways=4,
                                      dtype="4-Way HDPE")],
                 [_cable_row([(0, 0), (0, 200)])])

    extend_distribution_ducts_along_cables(d)

    ds = ogr.Open(os.path.join(d, "Distribution_Ducts.gpkg"), 0)
    fill = next(f for f in ds.GetLayer(0)
                if str(f.GetField("RUN_ID") or "").startswith("RUN-FILL-"))
    assert fill.GetField("DUCT_TYPE") == "4-Way HDPE"
    assert fill.GetField("WAYS") == 4
    assert fill.GetField("WAYS_TOTAL") == 4
    assert fill.GetField("cables_carried") == "7"
    assert fill.GetField("pdp_ids") == "PDP00001"
    assert fill.GetField("REVIEW") == 0


def test_a_second_pass_adds_nothing(tmp_path):
    d = _fixture(tmp_path, [_duct_row([(0, 0), (0, 100)])],
                 [_cable_row([(0, 0), (0, 220)])])

    assert extend_distribution_ducts_along_cables(d) >= 1
    rows = _rows(os.path.join(d, "Distribution_Ducts.gpkg"))
    assert extend_distribution_ducts_along_cables(d) == 0
    assert _rows(os.path.join(d, "Distribution_Ducts.gpkg")) == rows
