"""The published trench SURFACE must name the real surface, not a constant.

``sidewalk`` reaches ``attr_enrich.enrich_trenches`` in two shapes: a
boolean-ish flag from the legacy stage ("true"/"false"/"1"/"0") and the actual
surface NAME from the designer ("Footway"/"Asphalt"/"Garden",
``design.trench_design._surface_for``). Both "false" and a surface name are
truthy strings, so reading a name as a flag stamped every span
``SURFACE=Footpath`` / ``REINSTATE=Sidewalk`` — including the HDD road
crossings the designer had marked Asphalt. Those values feed the Surface
Restoration Plan, the traffic-plan reinstatement text, the permit drawings and
the permit rule ``TRAFFIC_001`` (which only fires on Asphalt/Footpath).

Regression guard for the 78-span HDD case on UI-Brownfield-Verify.
"""
import pathlib
import sys

import pytest

ogr = pytest.importorskip("osgeo.ogr")
ogr.UseExceptions()

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils import attr_enrich  # noqa: E402


def _line(x):
    ls = ogr.Geometry(ogr.wkbLineString)
    ls.AddPoint_2D(float(x), 0.0)
    ls.AddPoint_2D(float(x) + 10.0, 0.0)
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    ml.AddGeometry(ls)
    return ml


FIELDS = [
    ("sidewalk", ogr.OFTString), ("trench_type", ogr.OFTString),
    ("SURFACE", ogr.OFTString), ("REINSTATE", ogr.OFTString),
    ("USAGE_TYPE", ogr.OFTString), ("CONSTRUCT", ogr.OFTString),
    ("WIDTH_MM", ogr.OFTInteger), ("DEPTH_MM", ogr.OFTInteger),
    ("length_m", ogr.OFTReal), ("INFRA_STATUS", ogr.OFTString),
]


def _write(path, rows):
    drv = ogr.GetDriverByName("GPKG")
    if path.exists():
        drv.DeleteDataSource(str(path))
    ds = drv.CreateDataSource(str(path))
    lyr = ds.CreateLayer("trenches", geom_type=ogr.wkbMultiLineString)
    for name, typ in FIELDS:
        lyr.CreateField(ogr.FieldDefn(name, typ))
    for i, (sidewalk, ttype) in enumerate(rows):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(_line(i * 20))
        f.SetField("sidewalk", sidewalk)
        f.SetField("trench_type", ttype)
        lyr.CreateFeature(f)
    ds = None


def _surfaces(tmp_path, rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "Final_Trenches.gpkg"
    _write(path, rows)
    attr_enrich.enrich_trenches(str(path), None)
    ds = ogr.Open(str(path))
    lyr = ds.GetLayer(0)
    out = {}
    for f in lyr:
        out[f["trench_type"]] = (f["SURFACE"], f["REINSTATE"])
    ds = None
    return out


def test_a_surface_name_is_not_read_as_a_sidewalk_flag(tmp_path):
    """The designer's 'Asphalt' means road reinstatement, not footpath."""
    got = _surfaces(tmp_path, [("Asphalt", "HDD"), ("Footway", "Open Cut"),
                               ("Garden", "Garden")])
    assert got["HDD"] == ("Asphalt", "Road")
    assert got["Open Cut"] == ("Footpath", "Sidewalk")


@pytest.mark.parametrize("spelling", ["asphalt", "Asphalt", " road ", "tarmac"])
def test_road_surface_spellings_all_reinstate_as_road(tmp_path, spelling):
    got = _surfaces(tmp_path, [(spelling, "HDD")])
    assert got["HDD"] == ("Asphalt", "Road")


def test_a_boolean_flag_still_reads_as_a_flag(tmp_path):
    """The legacy stage's flag must keep its old meaning."""
    got = _surfaces(tmp_path, [("false", "Open Cut")])
    assert got["Open Cut"] == ("Asphalt", "Road")

    got2 = _surfaces(tmp_path / "b", [("true", "Open Cut")])
    assert got2["Open Cut"] == ("Footpath", "Sidewalk")

    got3 = _surfaces(tmp_path / "c", [("0", "Open Cut")])
    assert got3["Open Cut"] == ("Asphalt", "Road")

    got4 = _surfaces(tmp_path / "d", [("1", "Open Cut")])
    assert got4["Open Cut"] == ("Footpath", "Sidewalk")


def test_a_mixed_flag_reports_the_mix(tmp_path):
    got = _surfaces(tmp_path, [("mixed", "Open Cut")])
    assert got["Open Cut"] == ("Mixed (Footpath + Asphalt)",
                               "Mixed (Sidewalk + Road)")
