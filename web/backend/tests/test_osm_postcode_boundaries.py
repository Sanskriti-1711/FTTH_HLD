"""Tests for storing an OSM postcode boundary under its postcode.

Why this exists: a German postcode DOES have a real boundary in OpenStreetMap
(`boundary=postal_code`), and `resolve_area` has always found it -- but it was
kept only in `osm.area_cache`, under the exact label that was searched.  The
European household register apportions a municipality's published population
across the postcode polygons in `osm.boundary_areas`, so with none stored a
German load refused, and "Germany has no household register" read like a data
gap when the real cause was a polygon that was found and then dropped.

These tests are pure: the network and the database are both monkeypatched, and
what is asserted is WHICH polygon is stored under WHICH key -- because a
mis-stored polygon does not fail, it silently apportions population onto the
wrong people.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import osm_source  # noqa: E402

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[13.355, 52.435], [13.386, 52.435], [13.386, 52.461],
                     [13.355, 52.461], [13.355, 52.435]]],
}

POSTCODE_RESOLUTION = {
    "polygon_source": "nominatim",
    "matched_category": "boundary",
    "matched_type": "postal_code",
    "matched": "12105, Tempelhof, Berlin, Deutschland",
    "osm_type": "relation",
    "osm_id": 1105327,
    "polygon": SQUARE,
    "resolved_at": "2026-10-17T09:00:00",
}


def _no_database(monkeypatch):
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)


def test_a_postcode_boundary_from_osm_is_stored_under_its_postcode(monkeypatch):
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)
    captured = {}
    monkeypatch.setattr(
        osm_source, "boundary_dataset_ingest",
        lambda records, source: captured.update(records=list(records), source=source) or 1,
    )

    assert osm_source.persist_postcode_boundary("de", "12105", POSTCODE_RESOLUTION) is True
    record = captured["records"][0]
    assert captured["source"] == osm_source.OSM_POSTCODE_SOURCE
    # Under the POSTCODE, in the form the register and the ladder both key on.
    assert record["country_code"] == "DE"
    assert record["code"] == "12105"
    assert record["kind"] == "postcode"
    # Provenance travels with the row: the licence and the OSM object it came
    # from are what let a planner tell this apart from a government dataset.
    assert record["properties"]["licence"] == osm_source.OSM_POSTCODE_LICENCE
    assert "ODbL" in record["properties"]["licence"]
    assert record["properties"]["osm_id"] == 1105327
    assert record["geometry"] is SQUARE


def test_an_enclosing_admin_boundary_is_not_stored_as_the_postcode(monkeypatch):
    # "B14 7, Birmingham" resolves to boundary/administrative -- the city.  It
    # is a real polygon, and storing it under a postcode would make the register
    # hand a district's whole population to one postcode.
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stored")))
    resolution = dict(POSTCODE_RESOLUTION, matched_type="administrative",
                      matched="Birmingham, United Kingdom")
    assert osm_source.persist_postcode_boundary("GB", "B147", resolution) is False


def test_a_bbox_or_ward_rung_is_not_stored(monkeypatch):
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stored")))
    for source in ("bbox", "administrative", "dataset"):
        resolution = dict(POSTCODE_RESOLUTION, polygon_source=source)
        assert osm_source.persist_postcode_boundary("DE", "12105", resolution) is False


def test_a_point_geometry_is_not_stored(monkeypatch):
    # A postcode that Nominatim knows only as a label has no polygon; a Point
    # cannot be apportioned onto and must not be written as one.
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stored")))
    resolution = dict(POSTCODE_RESOLUTION, polygon={"type": "Point", "coordinates": [13.37, 52.44]})
    assert osm_source.persist_postcode_boundary("DE", "12105", resolution) is False


def test_an_existing_row_is_left_alone(monkeypatch):
    # A US ZCTA or a UK postcode dataset already owns that key.  Replacing it
    # with an OSM polygon for the same code is a downgrade, and it would also
    # touch `loaded_at` on every resolve.
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup",
                        lambda cc, code: {"kind": "postcode", "code": code})
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stored")))
    assert osm_source.persist_postcode_boundary("US", "10001", POSTCODE_RESOLUTION) is False


def test_an_empty_postcode_is_not_stored(monkeypatch):
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stored")))
    assert osm_source.persist_postcode_boundary("DE", "", POSTCODE_RESOLUTION) is False
    assert osm_source.persist_postcode_boundary("", "12105", POSTCODE_RESOLUTION) is False


def test_a_database_failure_does_not_fail_the_resolve(monkeypatch):
    # This runs inside resolve_area: a cache write must never be able to turn a
    # resolved area into an error.
    _no_database(monkeypatch)
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)

    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(osm_source, "boundary_dataset_ingest", boom)
    assert osm_source.persist_postcode_boundary("DE", "12105", POSTCODE_RESOLUTION) is False


def test_resolve_area_stores_the_postcode_boundary_it_found(monkeypatch):
    # The wiring, not the store: resolve_area must hand the resolution it chose
    # (and the postcode the caller stated) to the persistence step.
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: False)
    feature = {
        "type": "Feature",
        "geometry": SQUARE,
        "properties": {
            "osm_type": "relation",
            "osm_id": 1105327,
            "category": "boundary",
            "type": "postal_code",
            "display_name": "12105, Tempelhof, Tempelhof-Schöneberg, Berlin, Deutschland",
            "address": {"country_code": "de", "city": "Berlin"},
            "lat": 52.44,
            "lon": 13.37,
        },
    }
    monkeypatch.setattr(osm_source, "nominatim_search", lambda area, code="": [feature])
    seen = []
    monkeypatch.setattr(
        osm_source, "persist_postcode_boundary",
        lambda cc, pc, res: seen.append((cc, pc, res)) or False,
    )

    resolution = osm_source.resolve_area(
        osm_source.compose_area(postcode="12105", city="Berlin", country_code="DE"),
        country_code="DE", input_type="postcode", postcode="12105",
    )

    assert resolution["polygon_source"] == "nominatim"
    assert resolution["postcode_is_boundary"] is True
    assert seen and seen[0][0] == "DE" and seen[0][1] == "12105"
    assert seen[0][2]["polygon"] == SQUARE


def test_a_bare_postcode_label_still_yields_a_stored_code(monkeypatch):
    # `resolve_area("12105")` with no `postcode=` argument: the code has to come
    # from the area text, or the same place resolves under two keys depending on
    # which caller asked.
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: False)
    feature = {
        "type": "Feature",
        "geometry": SQUARE,
        "properties": {
            "osm_type": "relation", "osm_id": 1105327,
            "category": "boundary", "type": "postal_code",
            "display_name": "12105, Berlin, Deutschland",
            "address": {"country_code": "de"},
            "lat": 52.44, "lon": 13.37,
        },
    }
    monkeypatch.setattr(osm_source, "nominatim_search", lambda area, code="": [feature])
    seen = []
    monkeypatch.setattr(osm_source, "persist_postcode_boundary",
                        lambda cc, pc, res: seen.append(pc) or False)

    osm_source.resolve_area("12105", country_code="DE")
    assert seen == ["12105"]


def test_a_cached_resolution_still_stores_its_boundary(monkeypatch):
    # Every German postcode was ALREADY resolved and cached before boundaries
    # were stored, so a cache hit that returned early would mean the fix only
    # ever applied to postcodes nobody had looked at yet.
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "dataset_stamp", lambda code: "")
    cached_resolution = dict(
        POSTCODE_RESOLUTION,
        area="12105, Germany",
        country_code="DE",
        dataset_stamp="",
        bbox=[13.355, 52.435, 13.386, 52.461],
    )
    monkeypatch.setattr(
        osm_source, "_query",
        lambda sql, params=(): [{
            "area": "12105, Germany", "display_name": cached_resolution["matched"],
            "osm_type": "relation", "osm_id": 1105327,
            "polygon_source": "nominatim", "resolution": cached_resolution,
            "bbox_json": None, "poly_json": None,
        }],
    )
    monkeypatch.setattr(osm_source, "nominatim_search",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    seen = []
    monkeypatch.setattr(osm_source, "persist_postcode_boundary",
                        lambda cc, pc, res: seen.append((cc, pc)) or False)

    osm_source.resolve_area("12105, Germany", country_code="DE",
                            input_type="postcode", postcode="12105")
    assert seen == [("DE", "12105")]


def test_the_dataset_stamp_ignores_the_rows_a_resolve_writes(monkeypatch):
    # The stamp exists to invalidate the cache when an operator loads a dataset.
    # Rows written BY a resolve are not that: counting them would invalidate the
    # row the same call just wrote, once per new postcode.
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    captured = {}

    def fake_query(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return [{"at": None}]

    monkeypatch.setattr(osm_source, "_query", fake_query)
    osm_source.dataset_stamp("DE")
    assert "source <>" in captured["sql"]
    assert osm_source.OSM_POSTCODE_SOURCE in captured["params"]
