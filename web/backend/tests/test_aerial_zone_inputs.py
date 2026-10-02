"""Tests for the aerial-zone inputs an area run produces.

The aerial rules themselves are covered in `test_aerial_feasibility.py` and
`test_trench_design.py`. What is tested here is the thing that made them inert
on generated projects: the landuse layer they are derived FROM only ever
arrived from a manual multi-layer upload, so an area-generated run derived no
zones at all and quietly trenched houses standing in parks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import design  # noqa: E402
import osm_source  # noqa: E402


# ---------------------------------------------------------------------------
# build_inputs writes the layer the derivation reads
# ---------------------------------------------------------------------------

POLYGON = {"type": "Polygon",
           "coordinates": [[[13.31, 52.41], [13.42, 52.41],
                            [13.42, 52.47], [13.31, 52.47], [13.31, 52.41]]]}


def _landuse_row(osm_id, landuse=None, natural=None, leisure=None, boundary=None):
    return {
        "osm_id": osm_id,
        "landuse": landuse,
        "natural": natural,
        "leisure": leisure,
        "boundary": boundary,
        "geom_json": json.dumps({
            "type": "Polygon",
            "coordinates": [[[13.32, 52.42], [13.33, 52.42],
                             [13.33, 52.43], [13.32, 52.43], [13.32, 52.42]]],
        }),
    }


def _stub_landuse(monkeypatch, rows):
    monkeypatch.setattr(osm_source, "_query", lambda sql, params=(): list(rows))


def test_an_area_run_writes_the_landuse_the_zones_are_derived_from(monkeypatch, tmp_path):
    # The path is the contract: `design.design_inputs` looks in
    # `inputs/osm/landuse/`, so a layer written anywhere else is never read.
    _stub_landuse(monkeypatch, [_landuse_row(1, landuse="forest")])
    out = osm_source.write_landuse_geojson(
        str(tmp_path / "inputs" / "osm" / "landuse" / "landuse.geojson"), POLYGON)
    assert out is not None
    assert Path(out).is_file()
    assert out.endswith(os.path.join("osm", "landuse", "landuse.geojson"))


def test_the_written_layer_carries_the_fclass_the_consumer_reads(monkeypatch, tmp_path):
    # `derive_aerial_zones` reads `fclass` and NOTHING else, and the store has no
    # such column — it keeps landuse/natural/leisure/boundary apart because OSM
    # does. So fclass has to be resolved by the writer or every row is dropped
    # for having an empty class and no zone is ever derived.
    rows = [
        _landuse_row(1, landuse="forest"),
        _landuse_row(2, leisure="park"),
        _landuse_row(3, natural="wood"),
    ]
    _stub_landuse(monkeypatch, rows)
    out = osm_source.write_landuse_geojson(str(tmp_path / "lu.geojson"), POLYGON)
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    classes = {f["properties"]["fclass"] for f in payload["features"]}
    assert classes == {"forest", "park", "wood"}


def test_fclass_prefers_the_most_specific_tag(monkeypatch, tmp_path):
    # A row tagged both ways is common in OSM (a leisure=park inside a
    # landuse=grass verge). `landuse` wins because that is what the restricted
    # list is mostly written against, and `natural` is the last resort.
    _stub_landuse(monkeypatch, [
        _landuse_row(1, landuse="forest", leisure="park"),
        _landuse_row(2, leisure="park", natural="wood"),
    ])
    out = osm_source.write_landuse_geojson(str(tmp_path / "lu.geojson"), POLYGON)
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    by_id = {f["properties"]["osm_id"]: f["properties"]["fclass"]
             for f in payload["features"]}
    assert by_id[1] == "forest"
    assert by_id[2] == "park"


def test_a_row_with_no_resolvable_class_is_left_out(monkeypatch, tmp_path):
    # A boundary=protected_area row with no landuse/natural/leisure resolves to
    # nothing; writing it with fclass="" would be a row the consumer silently
    # discards, so it is not written.
    _stub_landuse(monkeypatch, [_landuse_row(1, boundary="protected_area")])
    assert osm_source.write_landuse_geojson(str(tmp_path / "lu.geojson"), POLYGON) is None


def test_an_area_with_no_landuse_writes_nothing_at_all(monkeypatch, tmp_path):
    # Returning None rather than an empty file: "the derivation ran and found
    # nothing" and "there was nothing to look at" are different facts, and an
    # empty file collapses them.
    _stub_landuse(monkeypatch, [])
    assert osm_source.write_landuse_geojson(str(tmp_path / "lu.geojson"), POLYGON) is None


def test_a_database_failure_writes_nothing_and_raises_nothing(monkeypatch, tmp_path):
    # An optional constraint must never fail a run.
    def _boom(sql, params=()):
        raise RuntimeError("postgis_unavailable")
    monkeypatch.setattr(osm_source, "_query", _boom)
    assert osm_source.write_landuse_geojson(str(tmp_path / "lu.geojson"), POLYGON) is None


# ---------------------------------------------------------------------------
# A design that could not be protected now says so
# ---------------------------------------------------------------------------

def _write_landuse(path: Path, classes):
    features = []
    for i, cls in enumerate(classes):
        x = 400000 + i * 4000
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [x, 300000], [x, 302000], [x + 3000, 302000],
                [x + 3000, 300000], [x, 300000]]]},
            "properties": {"fclass": cls},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}),
                    encoding="utf-8")
    return str(path)


def test_restricted_ground_is_detected_so_a_miss_can_be_reported(tmp_path):
    layer = _write_landuse(tmp_path / "lu.geojson", ["park", "grass", "forest"])
    assert design._restricted_landuse_classes(layer) == ["forest", "park"]


def test_verge_classes_do_not_count_as_restricted(tmp_path):
    # `grass` is mostly roadside verge in OSM. Treating it as restricted is what
    # would blanket an entire AOI with "no digging here", so it is excluded —
    # and this test is what stops it being re-added by accident.
    layer = _write_landuse(tmp_path / "lu.geojson", ["grass", "scrub", "garden"])
    assert design._restricted_landuse_classes(layer) == []


def test_an_absent_landuse_layer_makes_no_claim_either_way(tmp_path):
    assert design._restricted_landuse_classes(None) == []
    assert design._restricted_landuse_classes(str(tmp_path / "nope.geojson")) == []


def test_a_layer_without_an_fclass_field_is_not_reported_as_restricted(tmp_path):
    # Silently [] rather than raising: this is a reporting aid, and a reporting
    # aid must never be the reason a design fails.
    path = tmp_path / "nofclass.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": None, "properties": {"landuse": "park"},
    }]}), encoding="utf-8")
    assert design._restricted_landuse_classes(str(path)) == []


# ---------------------------------------------------------------------------
# The PIPELINE path filters the mask; the designer path always did
# ---------------------------------------------------------------------------
#
# The rules above only ever guarded `design.derive_aerial_zones` — the
# standalone designer. An AREA run does not go through it: `main.py` hands the
# trench designer the RAW landuse layer as AERIAL_ZONES, and the designer took
# every polygon in it. In a residential ward `residential`/`retail`/`commercial`
# are the majority of the landuse layer, so the "no dig" mask covered the AOI
# and nearly every drop leg came back aerial.
#
# Measured on run ab8399fcc (North Edgbaston, 2 620 premises): 188 polygons in,
# 2 040 of 2 057 drop legs classified aerial for `zone`. These tests pin the
# filter at the consumption point so the two paths cannot drift again.

HLD_ROOT = BACKEND_DIR.parents[1]
if str(HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(HLD_ROOT))


def _write_landuse_wgs84(path: Path, classes):
    """A landuse layer in REAL lon/lat.

    `_write_landuse` above writes projected-looking coordinates, which is fine
    for the class-reporting tests (they never reproject) but not for the mask:
    `_read_polygons_geom` transforms to the design CRS, and a GeoJSON is WGS84 by
    definition, so out-of-range values abort the transform. These are ~0.01 deg
    squares near Berlin — far above MIN_ZONE_AREA_M2, so none is dropped as a
    sliver for its size.
    """
    features = []
    for i, cls in enumerate(classes):
        lon, lat = 13.30 + i * 0.02, 52.40
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [lon, lat], [lon + 0.01, lat], [lon + 0.01, lat + 0.01],
                [lon, lat + 0.01], [lon, lat]]]},
            "properties": {"fclass": cls},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}),
                    encoding="utf-8")
    return str(path)


def _mask_classes(path):
    """(kept classes, dropped count) as _read_polygons_geom reports them."""
    from HLDPlanning.design.trench_design import _read_polygons_geom
    geom = _read_polygons_geom(str(path), 27700)
    if geom is None:
        return {}, 0
    return (getattr(geom, "_aerial_class_counts", {}) or {},
            getattr(geom, "_aerial_dropped", 0))


def test_residential_and_commercial_landuse_cannot_blanket_the_aoi(tmp_path):
    layer = _write_landuse_wgs84(tmp_path / "lu.geojson",
                                 ["residential", "retail", "commercial", "park"])
    kept, dropped = _mask_classes(layer)
    assert list(kept) == ["park"], kept
    assert dropped == 3


def test_the_pipeline_mask_and_the_derivation_agree_on_the_same_layer(tmp_path):
    # The whole point of the shared list: one rule, so "the area has restricted
    # land" and "the zones were derived from it" can never disagree.
    classes = ["residential", "park", "grass", "allotments", "retail", "wood"]
    layer = _write_landuse_wgs84(tmp_path / "lu.geojson", classes)
    kept, _dropped = _mask_classes(layer)
    assert sorted(kept) == design._restricted_landuse_classes(layer)


def test_a_hand_supplied_no_dig_layer_without_fclass_is_honoured_as_is(tmp_path):
    # An operator who draws "do not dig here" has already decided; the filter
    # must not silently discard it. It is still reported, so the two cases are
    # never confused.
    path = tmp_path / "handmade.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [[[13.30, 52.40],
                      [13.32, 52.40], [13.32, 52.42], [13.30, 52.42],
                      [13.30, 52.40]]]}},
    ]}), encoding="utf-8")
    from HLDPlanning.design.trench_design import _read_polygons_geom
    geom = _read_polygons_geom(str(path), 27700)
    assert geom is not None
    assert getattr(geom, "_aerial_unfiltered", False) is True


def test_an_unknown_or_blank_class_is_never_restricted():
    # The safe direction is to allow digging, not to invent a no-dig zone.
    from HLDPlanning.design.aerial_feasibility import is_restricted_landuse
    for value in ("", None, "residential", "retail", "unknown_class"):
        assert is_restricted_landuse(value) is False
    assert is_restricted_landuse("PARK") is True      # case-insensitive
