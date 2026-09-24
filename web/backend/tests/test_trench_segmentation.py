"""Unit tests for the chamber-to-chamber trench segmentation.

The trench network used to be published as long routed runs (and ducts as one
corridor carrying a whole chamber chain in SECTION_CHAIN). It is now published
as chamber-to-chamber spans: each feature starts at a chamber and ends at the
next one, with its own length, while geometry the chambers do not touch stays
in one piece.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/ -v
"""

import pathlib
import sys

import pytest

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils import attr_enrich  # noqa: E402


LINE = [(0.0, 0.0), (0.0, 50.0)]          # 50 m straight line
CHAMBERS = [
    (0.0, 0.0, "HH-0001"),                 # at the start
    (0.0, 25.0, "DHH-0002"),               # mid-line
    (0.0, 50.0, "DHH-0003"),               # at the end
    (25.0, 25.0, "DHH-9999"),              # 25 m away — never a hit
]


# ── projection ─────────────────────────────────────────────────────────────

def test_projection_finds_only_chambers_on_the_line():
    hits = attr_enrich._project_chambers_on_part(LINE, CHAMBERS, 5.0)
    assert [cid for _pos, _xy, cid in hits] == ["HH-0001", "DHH-0002", "DHH-0003"]


def test_projection_positions_are_in_corridor_order():
    hits = attr_enrich._project_chambers_on_part(LINE, CHAMBERS, 5.0)
    positions = [pos for pos, _xy, _cid in hits]
    assert positions == sorted(positions)
    assert positions[0] == pytest.approx(0.0)
    assert positions[1] == pytest.approx(25.0)
    assert positions[2] == pytest.approx(50.0)


def test_projection_snaps_a_chamber_near_the_end_onto_the_endpoint():
    coords = [(0.0, 0.0), (0.0, 50.0)]
    hits = attr_enrich._project_chambers_on_part(coords, [(0.0, 49.9, "DHH-0003")], 5.0)
    assert len(hits) == 1
    assert hits[0][0] == pytest.approx(50.0)


def test_projection_ignores_chambers_far_from_the_line():
    hits = attr_enrich._project_chambers_on_part(LINE, CHAMBERS, 1.0)
    ids = [cid for _pos, _xy, cid in hits]
    assert "DHH-9999" not in ids          # 25 m off the line
    assert ids == ["HH-0001", "DHH-0002", "DHH-0003"]  # the three ON the line


# ── cutting ────────────────────────────────────────────────────────────────

def test_chambers_at_both_ends_and_the_middle_split_the_run_in_two():
    hits = attr_enrich._project_chambers_on_part(LINE, CHAMBERS, 5.0)
    spans = attr_enrich._spans_of_part(LINE, hits)
    # HH-0001 -> DHH-0002 -> DHH-0003; the trailing piece is zero-length
    # because the last chamber sits exactly on the run end, so it is dropped
    assert len(spans) == 2
    first, second = spans
    assert first[1] == "HH-0001" and first[2] == "DHH-0002"
    assert second[1] == "DHH-0002" and second[2] == "DHH-0003"
    assert attr_enrich._coords_len(first[0]) == pytest.approx(25.0)
    assert attr_enrich._coords_len(second[0]) == pytest.approx(25.0)


def test_run_with_no_chamber_is_not_cut():
    assert attr_enrich._spans_of_part(LINE, []) == []


def test_span_endpoints_line_up_so_the_network_stays_continuous():
    coords = [(0.0, 0.0), (0.0, 20.0), (0.0, 50.0)]
    hits = attr_enrich._project_chambers_on_part(coords, CHAMBERS, 5.0)
    spans = attr_enrich._spans_of_part(coords, hits)
    total = sum(attr_enrich._coords_len(c) for c, _s, _e in spans)
    assert total == pytest.approx(attr_enrich._coords_len(coords))
    assert len(spans) >= 2
    for (coords, _s, _e), (nxt, _s2, _e2) in zip(spans, spans[1:]):
        assert coords[-1][0] == pytest.approx(nxt[0][0])
        assert coords[-1][1] == pytest.approx(nxt[0][1])


# ── trench measure (closing a joint along the trench) ──────────────────────

def test_measure_along_reports_offset_measure_and_foot():
    coords = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0)]     # 20 m L-shape
    dist, measure, fx, fy = attr_enrich._measure_along(coords, 3.0, 12.0)
    assert dist == pytest.approx(2.0)                   # 2 m off the second leg
    assert measure == pytest.approx(13.0)               # 10 m + 3 m along it
    assert (fx, fy) == pytest.approx((3.0, 10.0))       # foot of the perpendicular


def test_measure_along_takes_the_nearest_leg_of_a_line():
    coords = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
    dist, measure, _fx, _fy = attr_enrich._measure_along(coords, 2.0, 8.0)
    assert dist == pytest.approx(2.0)                   # first leg is nearer
    assert measure == pytest.approx(8.0)


def test_substring_coords_takes_the_piece_between_two_measures():
    coords = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
    piece = attr_enrich._substring_coords(coords, 5.0, 15.0)
    assert piece[0] == pytest.approx((0.0, 5.0))
    assert piece[-1] == pytest.approx((5.0, 10.0))
    assert attr_enrich._coords_len(piece) == pytest.approx(10.0)


def test_substring_coords_is_empty_when_the_range_is_reversed():
    coords = [(0.0, 0.0), (0.0, 10.0)]
    assert attr_enrich._substring_coords(coords, 8.0, 2.0) == []


def test_substring_coords_rebuilds_the_intermediate_vertices():
    coords = [(0.0, 0.0), (0.0, 10.0), (0.0, 20.0), (0.0, 30.0)]
    piece = attr_enrich._substring_coords(coords, 5.0, 25.0)
    assert piece == pytest.approx([(0.0, 5.0), (0.0, 10.0), (0.0, 20.0), (0.0, 25.0)])


def test_measure_along_is_none_for_a_degenerate_line():
    assert attr_enrich._measure_along([(1.0, 1.0), (1.0, 1.0)], 2.0, 2.0) is None


# ── end to end on a real GeoPackage ────────────────────────────────────────

def _write_line_layer(path, geom_type, coords_per_feature, fields=()):
    """A one-layer GPKG of lines, the shape the segmenter is handed."""
    ogr = pytest.importorskip("osgeo.ogr")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    lyr = ds.CreateLayer("layer", geom_type=geom_type)
    for name, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(name, ftype))
    for points in coords_per_feature:
        if geom_type == ogr.wkbMultiLineString:
            geom = ogr.Geometry(ogr.wkbMultiLineString)
            ls = ogr.Geometry(ogr.wkbLineString)
            for x, y in points:
                ls.AddPoint_2D(x, y)
            geom.AddGeometry(ls)
        else:
            geom = ogr.Geometry(ogr.wkbLineString)
            for x, y in points:
                geom.AddPoint_2D(x, y)
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(geom)
        lyr.CreateFeature(f)
    ds = None


def _reread(path):
    ogr = pytest.importorskip("osgeo.ogr")
    ds = ogr.Open(str(path))
    rows = [f for f in ds.GetLayer(0)]
    ds = None
    return rows


def _run_duct_segmenter(work, duct_path, trench_path, chambers):
    return attr_enrich._segment_layer_at_chambers(
        str(duct_path), chambers, None, "Feeder ducts", 10.0,
        span_len_fields=("BUNDLE_LEN_M",), snap_ends_m=0.0,
        end_tol_m=10.0, trench_path=str(trench_path))


def test_labelled_end_walks_along_the_trench_to_reach_its_chamber(tmp_path):
    """The joint closes ON the trench — never across the footway to it."""
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    # duct stops 5 m short of the chamber; the trench carries on to it
    _write_line_layer(duct, ogr.wkbMultiLineString, [[(0.0, 0.0), (0.0, 50.0)]],
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    _write_line_layer(trench, ogr.wkbLineString, [[(0.0, 0.0), (0.0, 55.0)]])

    published = _run_duct_segmenter(tmp_path, duct, trench, [(0.0, 55.0, "HH-0002")])

    rows = _reread(duct)
    assert published == 1 and len(rows) == 1
    row = rows[0]
    assert row.GetField("END_CHAMBER") == "HH-0002"
    coords = attr_enrich._line_parts(row.GetGeometryRef())[0]
    # the end reached the chamber by taking the trench piece, not a chord
    assert coords[0] == pytest.approx((0.0, 0.0))
    assert coords[-1] == pytest.approx((0.0, 55.0))
    for x, _y in coords:
        assert x == pytest.approx(0.0)              # every new vertex on the trench
    assert row.GetField("SPAN_LEN_M") == pytest.approx(55.0, abs=0.1)
    assert row.GetField("BUNDLE_LEN_M") == pytest.approx(55.0, abs=0.1)


def test_end_is_left_alone_when_the_trench_route_exceeds_the_limit(tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_line_layer(duct, ogr.wkbMultiLineString, [[(0.0, 0.0), (0.0, 50.0)]],
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    # a 42 m detour between the duct's end and the chamber 2 m past it
    _write_line_layer(trench, ogr.wkbLineString,
                      [[(0.0, 50.0), (20.0, 50.0), (20.0, 52.0), (0.0, 52.0)]])

    published = _run_duct_segmenter(tmp_path, duct, trench, [(0.0, 52.0, "HH-0002")])

    rows = _reread(duct)
    assert published == 1 and len(rows) == 1
    coords = attr_enrich._line_parts(rows[0].GetGeometryRef())[0]
    assert coords[-1] == pytest.approx((0.0, 50.0))     # unchanged


def test_end_off_the_trench_is_not_extended(tmp_path):
    """Extending from an end that is not on the network would draw a chord."""
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_line_layer(duct, ogr.wkbMultiLineString, [[(5.0, 0.0), (5.0, 50.0)]],
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    _write_line_layer(trench, ogr.wkbLineString, [[(0.0, 0.0), (0.0, 55.0)]])

    published = _run_duct_segmenter(tmp_path, duct, trench, [(5.0, 55.0, "HH-0002")])

    rows = _reread(duct)
    assert published == 1 and len(rows) == 1
    coords = attr_enrich._line_parts(rows[0].GetGeometryRef())[0]
    assert coords[-1] == pytest.approx((5.0, 50.0))     # left as it was


class _Feedback(object):
    def __init__(self):
        self.lines = []

    def pushInfo(self, message):
        self.lines.append(str(message))

    def pushWarning(self, message):
        self.lines.append("WARN " + str(message))


def test_a_chamber_behind_an_unlaid_end_is_closed_backward(tmp_path):
    """A chamber behind the end is closed BACKWARD when nothing is laid there.

    The chamber sits at the trench's start and the duct ends 10 m along that
    same piece. The old rule refused this outright, on the assumption that the
    piece between the two is duct the network already has — an assumption the
    reference run falsified (22 distribution chamber joints published OPEN,
    every one inside the extension cap and with the chamber named at both
    ends). No other duct reaches this chamber here, so the end walks back along
    the trench and the run counts it as backward.
    """
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_line_layer(duct, ogr.wkbMultiLineString, [[(0.0, 10.0), (0.0, 20.0)]],
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    _write_line_layer(trench, ogr.wkbLineString, [[(0.0, 0.0), (0.0, 60.0)]])
    feedback = _Feedback()

    published = attr_enrich._segment_layer_at_chambers(
        str(duct), [(0.0, 0.0, "HH-0001")], feedback, "Feeder ducts", 10.0,
        span_len_fields=("BUNDLE_LEN_M",), snap_ends_m=0.0, end_tol_m=10.0,
        trench_path=str(trench))

    rows = _reread(duct)
    assert published == 1 and len(rows) == 1
    coords = attr_enrich._line_parts(rows[0].GetGeometryRef())[0]
    assert coords[0] == pytest.approx((0.0, 0.0))       # closed BACK to the chamber
    assert coords[-1] == pytest.approx((0.0, 20.0))
    joints = [ln for ln in feedback.lines if "[joints]" in ln]
    assert len(joints) == 1
    assert "1 of them BACKWARD" in joints[0]


def test_a_chamber_behind_a_laid_piece_is_still_refused(tmp_path):
    """The refusal survives where it was right: the patch is already duct.

    Another duct's geometry already reaches chamber (its endpoint sits on it),
    so walking backward would lay a second duct over the first. The end stays
    put and the run reports it as ``behind``.
    """
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_line_layer(duct, ogr.wkbMultiLineString,
                      [[(0.0, 0.0), (0.0, 10.0)],      # already laid
                       [(0.0, 10.0), (0.0, 20.0)]],    # under test
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    _write_line_layer(trench, ogr.wkbLineString, [[(0.0, 0.0), (0.0, 60.0)]])
    feedback = _Feedback()

    attr_enrich._segment_layer_at_chambers(
        str(duct), [(0.0, 0.0, "HH-0001")], feedback, "Feeder ducts", 10.0,
        span_len_fields=("BUNDLE_LEN_M",), snap_ends_m=0.0, end_tol_m=10.0,
        trench_path=str(trench))

    rows = _reread(duct)
    under_test = [r for r in rows
                  if attr_enrich._line_parts(r.GetGeometryRef())[0][-1][1] > 15.0]
    assert len(under_test) == 1
    coords = attr_enrich._line_parts(under_test[0].GetGeometryRef())[0]
    assert coords[0] == pytest.approx((0.0, 10.0))      # untouched
    joints = [ln for ln in feedback.lines if "[joints]" in ln]
    assert len(joints) == 1
    assert "whose chamber is behind the end" in joints[0]
    assert "0 with no trench piece between the two" in joints[0]


def test_a_span_already_at_its_chamber_is_not_moved(tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")
    duct = tmp_path / "Feeder_Ducts.gpkg"
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_line_layer(duct, ogr.wkbMultiLineString, [[(0.0, 0.0), (0.0, 50.0)]],
                      fields=[("BUNDLE_LEN_M", ogr.OFTReal)])
    _write_line_layer(trench, ogr.wkbLineString, [[(0.0, 0.0), (0.0, 55.0)]])

    _run_duct_segmenter(tmp_path, duct, trench, [(0.0, 50.0, "HH-0002")])

    rows = _reread(duct)
    coords = attr_enrich._line_parts(rows[0].GetGeometryRef())[0]
    assert coords[-1] == pytest.approx((0.0, 50.0))
    assert rows[0].GetField("SPAN_LEN_M") == pytest.approx(50.0, abs=0.1)



def test_segment_writes_chamber_spans_with_lengths(tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")

    def _write(path, geom_type, features):
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
        lyr = ds.CreateLayer("layer", geom_type=geom_type)
        if geom_type == ogr.wkbLineString:
            lyr.CreateField(ogr.FieldDefn("trench_type", ogr.OFTString))
            lyr.CreateField(ogr.FieldDefn("length_m", ogr.OFTReal))
        else:
            lyr.CreateField(ogr.FieldDefn("STRUCT_ID", ogr.OFTString))
        for geom, props in features:
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetGeometry(geom)
            for k, v in props.items():
                f.SetField(k, v)
            lyr.CreateFeature(f)
        ds = None

    def _line(points):
        g = ogr.Geometry(ogr.wkbLineString)
        for x, y in points:
            g.AddPoint_2D(x, y)
        return g

    def _point(x, y):
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(x, y)
        return g

    trench = tmp_path / "Final_Trenches.gpkg"
    chambers = tmp_path / "Chambers.gpkg"
    _write(trench, ogr.wkbLineString, [
        (_line([(0, 0), (0, 20), (0, 50)]), {"trench_type": "Open Cut"}),
        (_line([(100, 100), (100, 130)]), {"trench_type": "Garden"}),
    ])
    _write(chambers, ogr.wkbPoint, [
        (_point(0, 20), {"STRUCT_ID": "DHH-0001"}),
        (_point(0, 50), {"STRUCT_ID": "HH-0002"}),
    ])

    published = attr_enrich.segment_trenches_at_chambers(
        str(trench), str(chambers))

    ds = ogr.Open(str(trench))
    lyr = ds.GetLayer(0)
    rows = [f for f in lyr]
    ds = None

    # run 1 → 2 chamber spans (start→DHH-0001, DHH-0001→HH-0002),
    # run 2 → untouched unchambered run
    assert published == 3
    assert len(rows) == 3
    spans = [f for f in rows if f.GetField("SPAN_KIND") == "Chamber span"]
    rest = [f for f in rows if f.GetField("SPAN_KIND") == "Unchambered"]
    assert len(spans) == 2 and len(rest) == 1

    assert spans[0].GetField("END_CHAMBER") == "DHH-0001"
    assert spans[1].GetField("START_CHAMBER") == "DHH-0001"
    assert spans[1].GetField("END_CHAMBER") == "HH-0002"
    assert spans[1].GetField("SPAN_LEN_M") == pytest.approx(30.0)
    assert spans[1].GetField("length_m") == pytest.approx(
        spans[1].GetGeometryRef().Length(), abs=0.2)

    # total length is preserved and the unchambered run keeps its geometry
    total = sum(f.GetGeometryRef().Length() for f in rows)
    assert total == pytest.approx(80.0)
    assert rest[0].GetField("trench_type") == "Garden"
