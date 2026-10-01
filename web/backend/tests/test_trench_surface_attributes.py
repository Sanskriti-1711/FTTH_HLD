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
osr = pytest.importorskip("osgeo.osr")
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
    assert got["Garden"] == ("Garden", "Seed")


def test_carriageway_surface_reinstates_as_road(tmp_path):
    """A residential carrier is asphalt/road, not footpath."""
    got = _surfaces(tmp_path, [("Asphalt", "Open Cut")])
    assert got["Open Cut"] == ("Asphalt", "Road")


@pytest.mark.parametrize("spelling", ["asphalt", "Asphalt", " road ", "tarmac", "carriageway"])
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


# ── surface geometry check: the roads are WGS84, the design is projected ─────
# The OSM roads bundle ships in WGS84 while the design is EPSG:25833. The check
# compared the two raw, so every span sat "kilometres" from every road and the
# first production run reported the whole network off-road (456 of 456) — the
# check had no evidence and flagged nothing.

def _layer_with_srs(path, srs, rows, fields):
    drv = ogr.GetDriverByName("GPKG")
    if path.exists():
        drv.DeleteDataSource(str(path))
    ds = drv.CreateDataSource(str(path))
    lyr = ds.CreateLayer(path.stem, srs=srs, geom_type=ogr.wkbMultiLineString)
    for name, typ in fields:
        lyr.CreateField(ogr.FieldDefn(name, typ))
    for geom, props in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(geom)
        for k, v in props.items():
            f.SetField(k, v)
        lyr.CreateFeature(f)
    ds = None
    return path


def test_surface_check_reprojects_wgs84_roads_to_the_trench_crs(tmp_path):
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(25833)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    # One 100 m trench along a primary road, claimed as asphalt.
    out_dir = tmp_path / "run"
    out_dir.mkdir(parents=True, exist_ok=True)
    trench = out_dir / "Final_Trenches.gpkg"
    _layer_with_srs(trench, utm, [
        (_line_pts((389000.0, 5815000.0), (389100.0, 5815000.0)),
         {"TRENCH_ID": "TR-1", "SURFACE": "Asphalt"}),
    ], [("TRENCH_ID", ogr.OFTString), ("SURFACE", ogr.OFTString)])

    # The same ground as the roads bundle supplies it: WGS84 lon/lat.
    to_wgs = osr.CoordinateTransformation(utm, wgs)
    x0, y0, _ = to_wgs.TransformPoint(389000.0, 5815000.0)
    x1, y1, _ = to_wgs.TransformPoint(389100.0, 5815000.0)
    roads = _layer_with_srs(tmp_path / "roads.gpkg", wgs, [
        (_line_pts((x0, y0), (x1, y1)), {"fclass": "primary"}),
    ], [("fclass", ogr.OFTString)])

    report = attr_enrich.verify_surface_geometry(str(out_dir), None, str(roads))

    assert report["checked"] == 1
    assert report["no_road"] == 0, "the road is under the span, not 1000 km away"
    assert report["agreed"] == 1, "projected metres must not be rescaled as lon/lat"
    assert report["flags"] == []


def _line_pts(a, b):
    ls = ogr.Geometry(ogr.wkbLineString)
    ls.AddPoint_2D(float(a[0]), float(a[1]))
    ls.AddPoint_2D(float(b[0]), float(b[1]))
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    ml.AddGeometry(ls)
    return ml


def test_previous_surface_review_loads_a_valid_artifact(tmp_path):
    path = tmp_path / "surface_ai_review.json"
    path.write_text('{"suggestions": [{"span_id": "T-1"}]}', encoding="utf-8")
    loaded = attr_enrich._previous_surface_review(str(path))
    assert loaded == {"suggestions": [{"span_id": "T-1"}]}


def test_previous_surface_review_starts_fresh_when_unusable(tmp_path):
    assert attr_enrich._previous_surface_review(str(tmp_path / "absent.json")) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert attr_enrich._previous_surface_review(str(broken)) is None
    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2]", encoding="utf-8")
    assert attr_enrich._previous_surface_review(str(wrong_shape)) is None
