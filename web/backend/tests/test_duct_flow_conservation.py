"""The duct dedupe passes must never delete geometry nothing else carries.

Two rows can name the same chamber pair and still be two different paths
between those chambers (the duct builder clubs cables by proximity, so two
clubs can both run A->B on different corridors). Folding those onto one row
deleted duct the field has to build: on Berlin the feeder layer came out as
**7 disconnected pieces with 11 PDPs stranded**, because

  * ``merge_ducts_per_chamber_span`` kept the longest row per (start, end)
    pair and deleted the rest — 8 of the 48 deleted rows were 5-50 % covered
    by the row that kept them, and
  * ``absorb_chamber_stubs`` deleted a stub whose inheriting span covered only
    7-13 % of it — 28 rows with geometry nothing else had.

The rule now: fold/absorb only what the receiver already carries
(``_covered_share`` >= 95 % within 0.5 m).

Same file, same idea, one attribute: ``PARENT_TRENCH`` (which trench a duct
rides in) was blank on **all 555 duct rows** of a Berlin run because the lookup
asked for ``SRC_ID``/``id`` — and ``id`` is NULL on every published trench row,
the trench stage publishes ``TRENCH_ID``.

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

ogr = pytest.importorskip("osgeo.ogr")
ogr.UseExceptions()


def _polyline(points):
    ls = ogr.Geometry(ogr.wkbLineString)
    for x, y in points:
        ls.AddPoint_2D(float(x), float(y))
    ml = ogr.Geometry(ogr.wkbMultiLineString)
    ml.AddGeometry(ls)
    return ml


def _write_layer(path, rows, fields):
    """rows: list of (geometry, {field: value})"""
    drv = ogr.GetDriverByName("GPKG")
    if path.exists():
        drv.DeleteDataSource(str(path))
    ds = drv.CreateDataSource(str(path))
    lyr = ds.CreateLayer("ducts", geom_type=ogr.wkbMultiLineString)
    for name, typ in fields:
        lyr.CreateField(ogr.FieldDefn(name, typ))
    for geom, props in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(geom)
        for k, v in props.items():
            f.SetField(k, v)
        lyr.CreateFeature(f)
    ds = None


def _read(path):
    ds = ogr.Open(str(path))
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        out.append({lyr.GetLayerDefn().GetFieldDefn(i).GetName():
                    f.GetField(i) for i in range(lyr.GetLayerDefn().GetFieldCount())}
                   | {"geom": f.GetGeometryRef().Clone()})
    return out, ds


DUP_FIELDS = [
    ("START_CHAMBER", ogr.OFTString), ("END_CHAMBER", ogr.OFTString),
    ("SPAN_KIND", ogr.OFTString), ("REVIEW", ogr.OFTInteger),
    ("cables_carried", ogr.OFTString), ("pdp_ids", ogr.OFTString),
    ("capacity_total", ogr.OFTInteger), ("ways_used", ogr.OFTInteger),
    ("N_DUCTS", ogr.OFTInteger), ("WAYS_TOTAL", ogr.OFTInteger),
    ("WAYS", ogr.OFTInteger), ("BUNDLE_LEN_M", ogr.OFTReal),
    ("length_m", ogr.OFTReal), ("OCCUPANCY_PCT", ogr.OFTReal),
    ("SPARE_PCT", ogr.OFTReal),
]


# ── _covered_share ───────────────────────────────────────────────────────────

def test_covered_share_is_one_for_the_same_line():
    a = _polyline([(0, 0), (0, 100)])
    assert attr_enrich._covered_share(a, a.Clone()) >= 0.99


def test_covered_share_is_low_for_a_detour_between_the_same_ends():
    straight = _polyline([(0, 0), (0, 100)])
    detour = _polyline([(0, 0), (60, 50), (0, 100)])
    share = attr_enrich._covered_share(detour, straight)
    assert share < 0.2, share


# ── merge_ducts_per_chamber_span ─────────────────────────────────────────────

def test_merge_folds_duplicates_and_keeps_a_different_path(tmp_path):
    path = tmp_path / "Feeder_Ducts.gpkg"
    corridor = [(0, 0), (0, 100)]
    _write_layer(path, [
        # the real corridor, twice — a genuine duplicate
        (_polyline(corridor), {"START_CHAMBER": "DHH-1", "END_CHAMBER": "DHH-2",
                               "cables_carried": "FEEDER-CABLE-001", "pdp_ids": "PDP00001"}),
        (_polyline([(0, 0), (0, 50), (0, 100)]), {"START_CHAMBER": "DHH-1",
                                                  "END_CHAMBER": "DHH-2",
                                                  "cables_carried": "FEEDER-CABLE-002",
                                                  "pdp_ids": "PDP00002"}),
        # a SECOND path between the same two chambers — must survive
        (_polyline([(0, 0), (80, 50), (0, 100)]), {"START_CHAMBER": "DHH-1",
                                                   "END_CHAMBER": "DHH-2",
                                                   "cables_carried": "FEEDER-CABLE-003",
                                                   "pdp_ids": "PDP00003"}),
    ], DUP_FIELDS)

    attr_enrich.merge_ducts_per_chamber_span(str(path), None)
    rows, _ds = _read(path)

    assert len(rows) == 2, "the duplicate folds, the second path stays"
    kept_cables = sorted(
        c for r in rows for c in str(r["cables_carried"]).split(",") if c)
    assert kept_cables == ["FEEDER-CABLE-001", "FEEDER-CABLE-002",
                           "FEEDER-CABLE-003"], kept_cables
    # the surviving second path is still the detour, not the straight line
    detour = [r for r in rows if r["pdp_ids"] == "PDP00003"]
    assert detour, "the second path keeps a row"
    assert attr_enrich._covered_share(
        detour[0]["geom"], _polyline([(0, 0), (0, 100)])) < 0.2


# ── absorb_chamber_stubs ─────────────────────────────────────────────────────

def test_absorb_keeps_a_stub_the_span_does_not_cover(tmp_path):
    path = tmp_path / "Feeder_Ducts.gpkg"
    _write_layer(path, [
        # the real chamber-to-chamber span
        (_polyline([(0, 0), (0, 100)]), {"START_CHAMBER": "DHH-1",
                                         "END_CHAMBER": "DHH-2",
                                         "cables_carried": "FEEDER-CABLE-001"}),
        # a tail leaving DHH-1 and coming back to it, 40 m off the span
        (_polyline([(0, 0), (0, -40), (0, -40), (0, 0)]),
         {"START_CHAMBER": "DHH-1", "END_CHAMBER": "DHH-1",
          "cables_carried": "FEEDER-CABLE-009"}),
    ], DUP_FIELDS)

    attr_enrich.absorb_chamber_stubs(str(path), None, "Feeder ducts")
    rows, _ds = _read(path)

    assert len(rows) == 2, "geometry nothing else carries is never deleted"
    tail = [r for r in rows if r["cables_carried"] == "FEEDER-CABLE-009"]
    assert tail, "the tail keeps its own row"
    assert tail[0]["END_CHAMBER"] == "", "a tail is not a chamber-to-chamber span"
    assert tail[0]["SPAN_KIND"] == "Duct tail"
    assert tail[0]["REVIEW"] == 1


# ── PARENT_TRENCH ────────────────────────────────────────────────────────────

def test_enrich_ducts_links_the_published_trench_id(tmp_path):
    """A duct on a trench publishes that trench's id — TRENCH_ID, not `id`."""
    trench = tmp_path / "Final_Trenches.gpkg"
    _write_layer(trench, [
        (_polyline([(0, 0), (0, 100)]),
         # the real published schema: TRENCH_ID carries the id, `id` is NULL
         {"TRENCH_ID": "TR-000001", "id": None,
          "TRENCH_TYPE": "Open Cut", "TRENCH_TIER": "Feeder"}),
    ], [("TRENCH_ID", ogr.OFTString), ("id", ogr.OFTInteger),
        ("TRENCH_TYPE", ogr.OFTString), ("TRENCH_TIER", ogr.OFTString)])
    duct = tmp_path / "Feeder_Ducts.gpkg"
    _write_layer(duct, [
        (_polyline([(0, 10), (0, 90)]),
         {"cables_carried": "FEEDER-CABLE-001", "INFRA_STATUS": "Proposed"}),
    ], [("cables_carried", ogr.OFTString), ("INFRA_STATUS", ogr.OFTString)])

    attr_enrich.enrich_ducts(str(duct), str(tmp_path / "Distribution_Ducts.gpkg"),
                             str(tmp_path / "Drop_Ducts.gpkg"),
                             str(trench), str(tmp_path / "Chambers.gpkg"), None)
    rows, _ds = _read(duct)

    assert rows, "the duct is still published"
    assert rows[0]["PARENT_TRENCH"] == "TR-000001", \
        "the duct names the trench it rides in"


def test_absorb_still_removes_a_stub_that_lies_on_the_span(tmp_path):
    path = tmp_path / "Feeder_Ducts.gpkg"
    _write_layer(path, [
        (_polyline([(0, 0), (0, 100)]), {"START_CHAMBER": "DHH-1",
                                         "END_CHAMBER": "DHH-2",
                                         "cables_carried": "FEEDER-CABLE-001"}),
        # same corridor — pure duplication, deleting it costs no coverage
        (_polyline([(0, 0), (0, 30)]), {"START_CHAMBER": "DHH-1",
                                       "END_CHAMBER": "DHH-1",
                                       "cables_carried": "FEEDER-CABLE-002"}),
    ], DUP_FIELDS)

    attr_enrich.absorb_chamber_stubs(str(path), None, "Feeder ducts")
    rows, _ds = _read(path)

    assert len(rows) == 1, "a covered stub is still absorbed"
    assert "FEEDER-CABLE-002" in str(rows[0]["cables_carried"]), \
        "the absorbed stub hands its cables to the span"
