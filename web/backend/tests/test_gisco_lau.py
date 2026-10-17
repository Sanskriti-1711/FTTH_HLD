"""Tests for the Eurostat GISCO LAU path.

Pure throughout: no network and no database. The GISCO client takes an injected
opener, the CSV parser takes a StringIO, the GeoPackage reader is exercised
against blobs built here, and the apportionment takes plain dicts. What is being
tested is mostly the ways this data can be read WRONG rather than whether it
loads -- a wrong population column, a dropped country filter, or a share of a
municipality that sums past 100% all produce numbers that look fine.
"""

from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gisco_lau as g  # noqa: E402


def fake_opener(payloads):
    """An opener answering `payloads` by URL suffix, so nothing hits the network."""
    def _open(url, *args, **kwargs):
        for suffix, value in payloads.items():
            if url.endswith(suffix):
                return value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        raise AssertionError(f"unexpected url: {url}")
    return _open


# ---------------------------------------------------------------------------
# The vintage index -- the URL shape is what the previous revision got wrong
# ---------------------------------------------------------------------------

def test_the_vintage_index_is_read_from_the_service_not_hard_coded():
    opener = fake_opener({"datasets.json": {"lau-2011": {"title": "LAU 2011"},
                                            "lau-2024": {"title": "LAU 2024"}}})
    assert g.dataset_years(opener=opener) == ["2011", "2024"]
    assert g.latest_year(opener=opener) == "2024"


def test_the_csv_url_comes_from_the_vintage_index():
    # The real path is `.../lau/csv/LAU_RG_01M_2024_4326.csv`. An earlier
    # revision built `.../lau/lau-2024/packages.json`, which 404s -- and a 404
    # was misread as a licence refusal, so the shape is pinned here.
    files = {"csv": {"LAU_RG_01M_2024_4326.csv": "csv/LAU_RG_01M_2024_4326.csv",
                     "LAU_RG_01M_2024_3035.csv": "csv/LAU_RG_01M_2024_3035.csv"}}
    assert g.csv_url("2024", 4326, files=files) == (
        f"{g.GISCO_BASE}/csv/LAU_RG_01M_2024_4326.csv")
    assert g.csv_url("2024", 3035, files=files) == (
        f"{g.GISCO_BASE}/csv/LAU_RG_01M_2024_3035.csv")
    assert "/lau-2024/" not in g.csv_url("2024", 4326, files=files)


def test_asking_for_a_crs_the_vintage_does_not_publish_names_the_ones_it_does():
    files = {"csv": {"LAU_RG_01M_2024_4326.csv": "csv/x.csv"}}
    with pytest.raises(g.LauError) as exc:
        g.csv_url("2024", 3857, files=files)
    assert "4326" in str(exc.value)


# ---------------------------------------------------------------------------
# The population column: the wrong one is a wrong answer of the worst kind
# ---------------------------------------------------------------------------

def test_the_population_column_is_found_through_its_vintage_name():
    # 2011 and 2024 name it differently (POP_2011 vs POP_2024), so it cannot be
    # a constant -- and in 2024 there is no LAU_ID column at all.
    text = ("GISCO_ID,CNTR_CODE,LAU_ID,LAU_NAME,POP_2011,POP_DENS_2011,AREA_KM2,YEAR\n"
            "DE_1,DE,1,Town,1000,500,10,2011\n")
    rows = list(g.parse_lau_csv(io.StringIO(text), "DE", "2011"))
    assert rows[0]["population"] == 1000
    assert rows[0]["area_km2"] == 10.0
    assert rows[0]["year"] == "2011"


def test_the_vintage_year_comes_from_the_file_not_the_command_line():
    # A vintage with no YEAR column still states its year in the population
    # column's name. It matters because the register stamps this onto every row
    # it writes: taken from a `--year` argument instead, two loads of the same
    # country would be indistinguishable, and a wrong argument would be recorded
    # as a fact about the data.
    text = ("GISCO_ID,CNTR_CODE,LAU_NAME,POP_2024,AREA_KM2\n"
            "DE_1,DE,Town,1000,10\n")
    assert list(g.parse_lau_csv(io.StringIO(text), "DE"))[0]["year"] == "2024"
    # Density shares the `pop` prefix and carries no vintage: it must not be read
    # as either the count or its year.
    assert g._year_from_population_column("POP_DENS_2024") == ""
    assert g._year_from_population_column("POP_2011") == "2011"
    assert g._year_from_population_column("") == ""


def test_density_is_never_read_as_population():
    # POP_DENS sits directly beside POP and shares its prefix. Reading it as the
    # population would inflate every municipality in Europe, so the column is
    # chosen with the density column explicitly excluded -- and the order is
    # reversed here to prove it is not just reading the first `pop` it sees.
    text = ("GISCO_ID,CNTR_CODE,LAU_NAME,POP_DENS_2024,POP_2024,AREA_KM2,YEAR\n"
            "DE_1,DE,Town,99999,1000,10,2024\n")
    rows = list(g.parse_lau_csv(io.StringIO(text), "DE", "2024"))
    assert rows[0]["population"] == 1000


def test_a_file_with_no_population_column_is_refused():
    text = "GISCO_ID,CNTR_CODE,LAU_NAME,YEAR\nDE_1,DE,Town,2024\n"
    with pytest.raises(g.LauError) as exc:
        list(g.parse_lau_csv(io.StringIO(text), "DE"))
    assert "population" in str(exc.value)


def test_an_unpublished_population_is_none_not_zero():
    # GISCO ships 0 for municipalities whose population is not published (every
    # Albanian LAU in 2024). A 0 that means "not published" must not become a
    # household count of zero, which would place a real town in the register as
    # empty.
    text = ("GISCO_ID,CNTR_CODE,LAU_NAME,POP_2024,AREA_KM2,YEAR\n"
            "AL_1,AL,Kurbin,0,269.1,2024\n"
            "DE_1,DE,Town,1000,10,2024\n")
    rows = list(g.parse_lau_csv(io.StringIO(text), None, "2024"))
    assert rows[0]["population"] == 0
    assert g.households_for_population(rows[0]["population"], "AL") is None
    assert g.households_for_population(rows[1]["population"], "DE") == 500


def test_the_country_filter_is_applied():
    text = ("GISCO_ID,CNTR_CODE,LAU_NAME,POP_2024,AREA_KM2,YEAR\n"
            "DE_1,DE,Town,1000,10,2024\n"
            "FR_1,FR,Ville,2000,20,2024\n")
    assert [r["lau_id"] for r in g.parse_lau_csv(io.StringIO(text), "DE")] == ["DE_1"]
    assert len(list(g.parse_lau_csv(io.StringIO(text), None))) == 2


def test_the_country_is_recovered_from_the_id_when_the_column_is_absent():
    text = "GISCO_ID,LAU_NAME,POP_2024,AREA_KM2,YEAR\nDE_1,Town,1000,10,2024\n"
    rows = list(g.parse_lau_csv(io.StringIO(text), "DE"))
    assert rows[0]["country_code"] == "DE"


# ---------------------------------------------------------------------------
# Population -> households is an ESTIMATE, and is treated as one
# ---------------------------------------------------------------------------

def test_household_size_is_published_per_country_with_an_eu27_fallback():
    assert g.persons_per_household("DE") == (2.0, True)
    assert g.persons_per_household("SK") == (3.1, True)
    # A country with no published figure falls back to the EU27 average AND says
    # so, because "estimated with a national basis" and "estimated with none"
    # are different claims.
    people, published = g.persons_per_household("AD")
    assert published is False
    assert people == g.EU27_PERSONS_PER_HOUSEHOLD


def test_households_for_population_refuses_to_answer_for_nothing():
    assert g.households_for_population(None, "DE") is None
    assert g.households_for_population(0, "DE") is None
    assert g.households_for_population(-5, "DE") is None
    assert g.households_for_population("", "DE") is None


# ---------------------------------------------------------------------------
# Apportioning: the shares have to sum to the municipality, never past it
# ---------------------------------------------------------------------------

def _overlap(postcode, lau_id, overlap, area, population):
    return {"postcode": postcode, "lau_id": lau_id, "overlap_km2": overlap,
            "lau_area_km2": area, "lau_population": population}


def test_the_shares_sum_back_to_the_population_that_was_shared_out():
    # The property that matters: apportioning must not create or destroy people.
    overlaps = [_overlap("10115", "DE_1", 5.0, 10.0, 1000),
                _overlap("10117", "DE_1", 5.0, 10.0, 1000)]
    records, stats = g.attribute_population(overlaps, "DE")
    assert sum(r["population"] for r in records) == 1000
    assert [r["population"] for r in records] == [500, 500]
    assert [r["households"] for r in records] == [250, 250]


def test_overlaps_that_exceed_the_municipality_are_scaled_back_to_it():
    # Three postcodes covering 200% of one LAU: without the cap the LAU's
    # population would be handed out twice over, and every count would be
    # inflated by a factor nobody would notice.
    overlaps = [_overlap("A", "DE_1", 5.0, 10.0, 1000),
                _overlap("B", "DE_1", 5.0, 10.0, 1000),
                _overlap("C", "DE_1", 10.0, 10.0, 1000)]
    records, stats = g.attribute_population(overlaps, "DE")
    assert sum(r["population"] for r in records) == 1000
    assert stats["clamped_lau_totals"] == 1


def test_a_single_overlap_cannot_claim_more_than_the_whole_municipality():
    # A postcode polygon that covers a neighbouring district is the usual cause,
    # and it must not be able to invent population.
    records, stats = g.attribute_population([_overlap("A", "DE_1", 30.0, 10.0, 1000)], "DE")
    assert records[0]["population"] == 1000
    assert stats["clamped"] == 1


def test_a_municipality_without_a_population_contributes_nothing():
    records, stats = g.attribute_population(
        [_overlap("A", "AL_1", 5.0, 10.0, 0), _overlap("A", "DE_1", 5.0, 10.0, 1000)], "DE")
    assert stats["dropped_no_population"] == 1
    assert sum(r["population"] for r in records) == 500


def test_records_say_they_are_estimates():
    # A population-derived household count and a counted one are different
    # claims, and the row has to carry which it is.
    records, _stats = g.attribute_population([_overlap("A", "DE_1", 10.0, 10.0, 1000)], "DE")
    assert records[0]["basis"] == "gisco_lau_estimate"


def test_the_source_can_be_a_generator():
    # attribute_population makes two passes, so a single-use iterator off a
    # download or a cursor must not be exhausted by the first one.
    overlaps = (_overlap(pc, "DE_1", 5.0, 10.0, 1000) for pc in ("A", "B"))
    records, _stats = g.attribute_population(overlaps, "DE")
    assert sum(r["population"] for r in records) == 1000


def test_rows_missing_a_shape_are_counted_not_guessed():
    records, stats = g.attribute_population(
        [{"postcode": "A", "lau_id": "DE_1", "overlap_km2": None,
          "lau_area_km2": 10.0, "lau_population": 1000}], "DE")
    assert records == []
    assert stats["dropped_no_area"] == 1


# ---------------------------------------------------------------------------
# The GeoPackage header, which is how the polygons are read without GDAL
# ---------------------------------------------------------------------------

def _gpkg_blob(wkb, srid=3035, envelope=1, empty=False):
    flags = 1 | ((envelope & 7) << 1) | (0x10 if empty else 0)
    header = b"GP" + bytes([0, flags]) + struct.pack("<i", srid)
    header += b"\x00" * {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[envelope]
    return header + wkb


def test_the_gpkg_header_is_stripped_and_its_srid_kept():
    wkb = b"\x01\x06\x00\x00\x00" + b"body"
    got, srid = g.gpkg_wkb(_gpkg_blob(wkb, srid=3035, envelope=1))
    assert got == wkb
    assert srid == 3035


def test_every_envelope_size_is_skipped_correctly():
    # Getting this length wrong hands PostGIS a WKB that starts mid-envelope,
    # which either fails or -- worse -- parses as a different polygon.
    for indicator, size in ((0, 0), (1, 32), (2, 48), (3, 48), (4, 64)):
        wkb = b"\x01\x06\x00\x00\x00" + b"payload"
        got, _srid = g.gpkg_wkb(_gpkg_blob(wkb, envelope=indicator))
        assert got == wkb, f"envelope indicator {indicator} mis-skipped"


def test_an_empty_geometry_is_returned_empty_rather_than_as_junk():
    got, srid = g.gpkg_wkb(_gpkg_blob(b"", srid=3035, empty=True))
    assert got == b""
    assert srid == 3035


def _write_gpkg(path, columns, rows, table="LAU_RG_01M_2024_3035.gpkg", srid=3035):
    """A minimal single-layer GeoPackage, the shape `gpkg_layer` expects."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, srs_id INTEGER)")
        conn.execute("INSERT INTO gpkg_contents VALUES (?, 'features', ?)", (table, srid))
        defs = ", ".join(f'"{name}" {typ}' for name, typ in columns)
        conn.execute(f'CREATE TABLE "{table}" ({defs})')
        placeholders = ", ".join("?" for _ in rows[0])
        conn.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
        conn.commit()
    finally:
        conn.close()
    return str(path)


def test_the_gpkg_reader_returns_the_municipality_name(tmp_path):
    # `osm.gisco_lau` has a `name` column and nothing filled it, so the table
    # behind every European household count listed the municipality as "".
    # GISCO writes LAU_NAME; older layers spell it NAME_LATN.
    blob = _gpkg_blob(b"\x01\x06\x00\x00\x00body", srid=3035)
    path = _write_gpkg(
        tmp_path / "lau.gpkg",
        [("fid", "INTEGER PRIMARY KEY"), ("geom", "BLOB"), ("GISCO_ID", "TEXT"),
         ("CNTR_CODE", "TEXT"), ("LAU_NAME", "TEXT")],
        [(1, blob, "DE_11000000", "DE", "Berlin, Stadt"),
         (2, blob, "FR_1", "FR", "Ville")],
    )
    rows = list(g.read_lau_geometries(path, "DE"))
    assert [r["name"] for r in rows] == ["Berlin, Stadt"]
    assert rows[0]["lau_id"] == "DE_11000000"
    assert rows[0]["srid"] == 3035


def test_a_layer_without_a_name_column_yields_no_name_rather_than_failing(tmp_path):
    blob = _gpkg_blob(b"\x01\x06\x00\x00\x00body", srid=3035)
    path = _write_gpkg(
        tmp_path / "lau.gpkg",
        [("fid", "INTEGER PRIMARY KEY"), ("geom", "BLOB"), ("GISCO_ID", "TEXT"),
         ("CNTR_CODE", "TEXT")],
        [(1, blob, "DE_1", "DE")],
    )
    rows = list(g.read_lau_geometries(path, "DE"))
    assert rows[0]["name"] == ""
    assert rows[0]["lau_id"] == "DE_1"


def test_a_reserved_envelope_indicator_is_refused():
    blob = bytearray(_gpkg_blob(b"\x01\x06\x00\x00\x00junk"))
    blob[3] = (blob[3] & ~0x0E) | (7 << 1)  # indicator 7 is reserved
    with pytest.raises(g.LauError) as exc:
        g.gpkg_wkb(bytes(blob))
    assert "reserved" in str(exc.value)


def test_a_non_geopackage_blob_is_refused():
    with pytest.raises(g.LauError):
        g.gpkg_wkb(b"PK\x03\x04not a gpkg geometry")
    with pytest.raises(g.LauError):
        g.gpkg_wkb(b"")


# ---------------------------------------------------------------------------
# Provenance: the licence is a restriction on USE, so it travels with the rows
# ---------------------------------------------------------------------------

def test_the_licence_records_the_non_commercial_restriction():
    # Access is anonymous, use is not: GISCO requires attribution to
    # EuroGeographics and forbids commercial use. Neither is visible from the
    # API, so both are asserted here rather than left to prose.
    assert "non-commercial" in g.GISCO_LICENCE.lower()
    assert "EuroGeographics" in g.GISCO_LICENCE
