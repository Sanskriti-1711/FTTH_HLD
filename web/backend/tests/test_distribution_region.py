"""Unit tests for confining the distribution duct to its own polygon.

Rule D5: a distribution duct connects its own PDP to the pseudo object points of
ITS polygon. The pass has two halves, tested separately here:

  * CONFINEMENT — a duct is clipped to the polygon its own row names (plus a
    boundary tolerance), and a row that lies entirely outside it is dropped.
    This is safe to do because every off-region piece of a real run was measured
    to be a SPUR, never a bridge between two points of the region
    (``tmp/dist_cross_serve.py``), so a clip cannot sever a route.
  * REACH — a pseudo object point of the region that the region's duct does not
    reach is joined to it: along the trench when one trench carries both ends,
    otherwise by a straight connector (counted). A point of ANOTHER region is
    never joined to this region's duct.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/ -v
"""

import pathlib
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils import attr_enrich  # noqa: E402


class _FB(object):
    def __init__(self):
        self.lines = []

    def pushInfo(self, message):
        self.lines.append(str(message))

    def pushWarning(self, message):
        self.lines.append("WARN " + str(message))


def _mk(path, geom_type, rows, fields):
    """rows = [(geometry builder, {field: value})]"""
    ogr = pytest.importorskip("osgeo.ogr")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    lyr = ds.CreateLayer("layer", geom_type=geom_type)
    for name, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(name, ftype))
    for build, props in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(build())
        for k, v in props.items():
            f.SetField(k, v)
        lyr.CreateFeature(f)
    ds = None


def _poly(points):
    ogr = pytest.importorskip("osgeo.ogr")
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in points:
        ring.AddPoint_2D(x, y)
    ring.AddPoint_2D(points[0][0], points[0][1])
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(ring)
    return lambda: g


def _ml(points):
    ogr = pytest.importorskip("osgeo.ogr")
    g = ogr.Geometry(ogr.wkbMultiLineString)
    ls = ogr.Geometry(ogr.wkbLineString)
    for x, y in points:
        ls.AddPoint_2D(x, y)
    g.AddGeometry(ls)
    return lambda: g


def _line(points):
    ogr = pytest.importorskip("osgeo.ogr")
    g = ogr.Geometry(ogr.wkbLineString)
    for x, y in points:
        g.AddPoint_2D(x, y)
    return lambda: g


def _pt(x, y):
    ogr = pytest.importorskip("osgeo.ogr")
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(x, y)
    return lambda: g


def _rows(path):
    """[(fields dict, cloned geometry)] — clones, because the features' geometry
    is owned by the dataset and reading it after the dataset is released is a
    segfault (it has been hit before in this project's own preview tool)."""
    ogr = pytest.importorskip("osgeo.ogr")
    ds = ogr.Open(str(path))
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        out.append(({n: f.GetField(n) for n in names},
                    g.Clone() if g is not None else None))
    ds = None
    return out


def _setup_region(tmp_path, duct_geom, taps, trench_geom=None,
                  trench_type=None):
    """A POLY00001 square with one duct; POLY00002 sits beside it."""
    ogr = pytest.importorskip("osgeo.ogr")
    polys = tmp_path / "Polygons.gpkg"
    ducts = tmp_path / "Distribution_Ducts.gpkg"
    tp = tmp_path / "Pseudo_HH.gpkg"
    _mk(polys, ogr.wkbPolygon, [
        (_poly([(0, 0), (100, 0), (100, 100), (0, 100)]), {"POLYGON_ID": "POLY00001"}),
        (_poly([(100, 0), (200, 0), (200, 100), (100, 100)]), {"POLYGON_ID": "POLY00002"}),
    ], [("POLYGON_ID", ogr.OFTString)])
    _mk(ducts, ogr.wkbMultiLineString, [
        (duct_geom, {"POLYGON_ID": "POLY00001", "length_m": 0.0}),
    ], [("POLYGON_ID", ogr.OFTString), ("length_m", ogr.OFTReal)])
    _mk(tp, ogr.wkbPoint, [
        (_pt(x, y), {"POLYGON_ID": pid}) for (x, y, pid) in taps
    ], [("POLYGON_ID", ogr.OFTString)])
    trench = None
    if trench_geom is not None:
        trench = tmp_path / "Final_Trenches.gpkg"
        _mk(trench, trench_type or ogr.wkbLineString, [(trench_geom, {})], [])
    return polys, ducts, tp, trench


def test_a_duct_is_clipped_to_its_own_polygon(tmp_path):
    # starts inside POLY00001, runs 50 m into POLY00002
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (150.0, 50.0)]), [(30.0, 50.0, "POLY00001")])
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)

    rows = _rows(ducts)
    assert len(rows) == 1
    fields, geom = rows[0]
    parts = attr_enrich._line_parts(geom)
    assert len(parts) == 1
    # the kept geometry stops at the boundary + tolerance, nothing beyond it
    assert max(x for x, _y in parts[0]) == pytest.approx(102.0, abs=0.05)
    assert fields["length_m"] == pytest.approx(82.0, abs=0.5)
    text = " ".join(fb.lines)
    assert "[region]" in text and "off-region removed" in text


def test_a_duct_entirely_outside_its_polygon_is_dropped(tmp_path):
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(120.0, 50.0), (180.0, 50.0)]), [(30.0, 50.0, "POLY00001")])
    fb = _FB()
    n = attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)
    assert _rows(ducts) == []
    assert "1 empty row(s) dropped" in " ".join(fb.lines)
    assert n == 0


def test_an_unreached_point_is_joined_to_its_region_duct(tmp_path):
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(60.0, 40.0, "POLY00001")])
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)

    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    # There is no trench carrying the tap point in this fixture. The point is
    # therefore reported unresolved; no off-trench connector is fabricated.
    assert len(parts) == 1
    text = " ".join(fb.lines)
    assert "0/1" in text
    assert "no connected trench path" in text


def test_the_link_follows_the_trench_when_one_carries_both_ends(tmp_path):
    # an L-shaped trench through the duct end and the point: the link must be
    # the 20 m trench path, not the 14.1 m chord
    _, ducts, tp, trench = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(70.0, 40.0, "POLY00001")],
        trench_geom=_line([(60.0, 50.0), (60.0, 45.0), (70.0, 45.0), (70.0, 40.0)]))
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp),
        trench_path=str(trench), feedback=fb)

    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    assert len(parts) == 2
    link = min(parts, key=attr_enrich._coords_len)
    assert attr_enrich._coords_len(link) == pytest.approx(20.0, abs=0.01)
    assert len(link) == 4                        # every trench vertex is kept
    assert "extended along the trench" in " ".join(fb.lines)
    assert "straight connector" not in " ".join(fb.lines)


def test_a_multi_span_link_follows_the_chamber_chain(tmp_path):
    """A distribution link may cross chamber-bounded trench spans, but stays
    on those spans instead of becoming a straight off-trench shortcut."""
    ogr = pytest.importorskip("osgeo.ogr")

    def _two_spans():
        ml = ogr.Geometry(ogr.wkbMultiLineString)
        for part in ([(60.0, 50.0), (60.0, 45.0)],
                     [(60.0, 45.0), (70.0, 45.0), (70.0, 40.0)]):
            ls = ogr.Geometry(ogr.wkbLineString)
            for x, y in part:
                ls.AddPoint_2D(x, y)
            ml.AddGeometry(ls)
        return ml

    _, ducts, tp, trench = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(70.0, 40.0, "POLY00001")],
        trench_geom=_two_spans, trench_type=ogr.wkbMultiLineString)
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp),
        trench_path=str(trench), feedback=fb)

    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    assert len(parts) == 2
    link = min(parts, key=attr_enrich._coords_len)
    # the 20 m trench chain, not the 14.1 m off-network chord
    assert attr_enrich._coords_len(link) == pytest.approx(20.0, abs=0.05)
    text = " ".join(fb.lines)
    assert "joined span by span" in text
    assert "straight connector" not in text


def test_a_link_is_built_span_by_span_through_the_chambers():
    """The assembly itself: two spans sharing the chamber vertex (60, 45)."""
    spans = [[(60.0, 50.0), (60.0, 45.0)],
             [(60.0, 45.0), (70.0, 45.0), (70.0, 40.0)]]
    link = attr_enrich._trench_network_path(spans, 60.0, 50.0, 70.0, 40.0)
    assert link is not None
    assert attr_enrich._coords_len(link) == pytest.approx(20.0, abs=0.01)
    assert link[0] == (60.0, 50.0) and link[-1] == (70.0, 40.0)
    assert len(link) == 4          # the shared chamber vertex is not duplicated


def test_trench_that_does_not_join_up_still_yields_no_path():
    """Then a chord IS the honest answer — and the caller counts it."""
    spans = [[(60.0, 50.0), (60.0, 45.0)],       # dead end
             [(80.0, 45.0), (70.0, 40.0)]]       # a piece that never connects
    assert attr_enrich._trench_network_path(spans, 60.0, 50.0, 70.0, 40.0) is None


def test_a_broken_link_cannot_become_a_cross_town_detour():
    spans = [[(0.0, 0.0), (100.0, 0.0)],
             [(100.0, 0.0), (100.0, 200.0)],
             [(100.0, 200.0), (0.0, 200.0)]]
    long_way = attr_enrich._trench_network_path(spans, 0.0, 0.0, 0.0, 200.0,
                                                cap_m=300.0)
    assert long_way is None                       # 400 m of trench for a 200 m gap
    kept = attr_enrich._trench_network_path(spans, 0.0, 0.0, 0.0, 200.0,
                                            cap_m=500.0)
    assert kept is not None
    assert attr_enrich._coords_len(kept) == pytest.approx(400.0, abs=0.01)


def test_a_point_of_another_region_is_not_joined(tmp_path):
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(150.0, 40.0, "POLY00002")])
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)

    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    assert len(parts) == 1                       # POLY00002's point is not ours
    assert "reached 0/1" in " ".join(fb.lines)


def test_a_point_already_on_the_duct_is_not_extended(tmp_path):
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(40.0, 50.0, "POLY00001")])
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)
    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    assert len(parts) == 1
    assert "reached 1/1 (1 already on a region duct" in " ".join(fb.lines)


def test_a_region_with_no_duct_of_its_own_is_counted_and_named(tmp_path):
    """The pass iterates regions that HAVE a duct row, so a region that got no
    duct simply vanished from the figure — Berlin reported "reached 235/295 ...
    0 left unconnected" while 60 points of the 8 ductless regions sat on
    nothing. The point must be counted, and the region named."""
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]),
        [(40.0, 50.0, "POLY00001"), (150.0, 40.0, "POLY00002")])
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)
    text = " ".join(fb.lines)
    assert "reached 1/2" in text
    assert "1 point(s) in 1 region(s) with NO distribution duct of their own" in text
    assert any(line.startswith("WARN") and "POLY00002" in line for line in fb.lines)


def test_the_runs_layer_is_confined_with_the_ducts(tmp_path):
    """``Distribution_Ducts_Runs`` is the layer the chamber segmentation groups
    runs from. Confining only the ducts left the two disagreeing (3.5 km of
    spans against 6.4 km of runs on Berlin) and the runs still off-polygon.
    Both layers take the same clip and the same links."""
    ogr = pytest.importorskip("osgeo.ogr")
    polys, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (150.0, 50.0)]), [(60.0, 40.0, "POLY00001")])
    runs = tmp_path / "Distribution_Ducts_Runs.gpkg"
    _mk(runs, ogr.wkbMultiLineString, [
        (_ml([(20.0, 50.0), (150.0, 50.0)]),
         {"POLYGON_ID": "POLY00001", "length_m": 0.0}),
    ], [("POLYGON_ID", ogr.OFTString), ("length_m", ogr.OFTReal)])

    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(polys), str(tp), feedback=fb, more_paths=(str(runs),))

    for path in (ducts, runs):
        parts = attr_enrich._line_parts(_rows(path)[0][1])
        assert max(x for x, _y in parts[0]) == pytest.approx(102.0, abs=0.05)
        # The tap is unresolved here because this fixture has no trench path
        # to (60, 40); both layers must remain identical and on-polygon.
        assert len(parts) == 1
    # only the ducts layer reports: the runs layer must not restate the figures
    assert " ".join(fb.lines).count("off-region removed") == 1


def test_a_duct_is_left_alone_when_its_region_is_unknown(tmp_path):
    _, ducts, tp, _ = _setup_region(
        tmp_path, _ml([(20.0, 50.0), (60.0, 50.0)]), [(40.0, 50.0, "POLY00001")])
    ogr = pytest.importorskip("osgeo.ogr")
    # retag the duct with a region that is not in Polygons
    ds = ogr.Open(str(ducts), 1)
    lyr = ds.GetLayer(0)
    for f in lyr:
        f.SetField("POLYGON_ID", "POLY99999")
        lyr.SetFeature(f)
    ds = None
    fb = _FB()
    attr_enrich.confine_distribution_to_region(
        str(ducts), str(tmp_path / "Polygons.gpkg"), str(tp), feedback=fb)
    parts = attr_enrich._line_parts(_rows(ducts)[0][1])
    assert len(parts) == 1
    assert "not in Polygons" in " ".join(fb.lines)
