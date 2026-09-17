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


# ── end to end on a real GeoPackage ────────────────────────────────────────

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
