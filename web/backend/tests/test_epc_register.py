"""EPC certificates are the one free ADDRESS-LEVEL UK dwelling count.

One certificate per dwelling, at postcode + UPRN, so a postcode's dwelling count
is the number of DISTINCT dwellings behind it -- not the number of certificates,
because a dwelling is re-certified over its life.  These tests pin that
aggregation, the UPRN rows that let a premise be matched by address, and the
prefix filter that lets one project load a slice of a national archive.

The bulk download needs a GOV.UK One Login, so no test downloads anything: the
reader is pure over a text handle.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import household_register as hr  # noqa: E402

EPC_HEADER = "LMK_KEY,UPRN,POSTCODE,CURRENT_ENERGY_RATING\n"


def _epc(*rows: str) -> io.StringIO:
    return io.StringIO(EPC_HEADER + "\n".join(rows) + "\n")


def _read(handle, areas=None):
    return hr.read_epc_csv(handle, hr.EPC_SOURCE, areas)


def test_the_postcode_total_is_distinct_dwellings_not_certificates():
    # Three certificates, two dwellings: one UPRN was re-certified.
    records, stats = _read(_epc(
        "c1,100000000001,B16 9BH,C",
        "c2,100000000002,B16 9BH,C",
        "c3,100000000002,B16 9BH,B",
    ))
    totals = [r for r in records if "uprn" not in r]
    assert totals == [{"postcode": "B16 9BH", "households": 2}]
    assert stats["certificates_read"] == 3
    assert stats["dwellings"] == 2


def test_each_dwelling_also_gets_a_uprn_row_of_one():
    records, _ = _read(_epc(
        "c1,100000000001,B16 9BH,C",
        "c2,100000000002,B16 9BH,C",
    ))
    uprn_rows = [r for r in records if "uprn" in r]
    assert {r["uprn"]: r["households"] for r in uprn_rows} == {
        "100000000001": 1, "100000000002": 1,
    }
    # The postcode TOTAL must come first: load_register keeps the first row it
    # sees for a postcode, and a dwelling's `1` is not the postcode's count.
    assert "uprn" not in records[0]


def test_rows_without_a_uprn_are_counted_by_certificate_and_reported():
    records, stats = _read(_epc(
        "c1,,B16 9BH,C",
        "c2,,B16 9BH,C",
        "c3,100000000009,B16 9BH,C",
    ))
    totals = [r for r in records if "uprn" not in r]
    assert totals == [{"postcode": "B16 9BH", "households": 3}]
    assert stats["certificates_without_uprn"] == 2
    # A certificate-shaped identity must not masquerade as a UPRN row.
    assert [r for r in records if "uprn" in r] == [
        {"postcode": "B16 9BH", "uprn": "100000000009", "households": 1}
    ]


def test_the_prefix_filter_loads_one_projects_slice():
    records, stats = _read(_epc(
        "c1,1,B16 9BH,C", "c2,2,B17 1AA,C", "c3,3,SW1A 1AA,C",
    ), areas=["B16"])
    assert [r["postcode"] for r in records if "uprn" not in r] == ["B16 9BH"]
    assert stats["dwellings"] == 1


def test_a_row_with_no_postcode_is_skipped():
    records, stats = _read(_epc("c1,1,,C", "c2,2,B16 9BH,C"))
    assert stats["dwellings"] == 1
    assert [r["postcode"] for r in records if "uprn" not in r] == ["B16 9BH"]


def test_an_epc_file_without_a_postcode_column_is_refused():
    handle = io.StringIO("CURRENT_ENERGY_RATING,UPRN\nC,1\n")
    with pytest.raises(ValueError):
        hr.read_epc_csv(handle, hr.EPC_SOURCE)


def test_the_epc_source_is_registered_and_not_a_geography_lookup():
    source = hr.REGISTER_SOURCES["epc"]
    # has_dwellings_count must NOT be False, or ingest_source refuses it -- the
    # flag that rejects the ONSPD geography lookup would reject this too.
    assert source.get("has_dwellings_count") is not False
    assert source.get("aggregates_to_postcode") is True
    assert source["url"].startswith("https://get-energy-performance-data")


def test_the_epc_reader_goes_through_read_register_file(tmp_path):
    # The CLI path: one CSV, read from disk, aggregated before it reaches the DB.
    path = tmp_path / "certificates.csv"
    path.write_text(EPC_HEADER + "c1,1,B16 9BH,C\nc2,2,B16 9BH,C\n", encoding="utf-8")
    records = list(hr.read_register_file(str(path), hr.EPC_SOURCE))
    assert [r for r in records if "uprn" not in r] == [
        {"postcode": "B16 9BH", "households": 2}
    ]


def test_the_cli_path_loads_the_aggregated_totals(tmp_path, monkeypatch):
    # The link between the reader and the database, without touching a database:
    # what `--source epc --file ... --areas ...` would write.
    path = tmp_path / "certificates.csv"
    path.write_text(EPC_HEADER + "".join([
        "c1,1,B16 9BH,C\n",
        "c2,2,B16 9BH,C\n",
        "c3,3,B17 1AA,C\n",
    ]), encoding="utf-8")
    written = []
    monkeypatch.setattr(hr.postgis, "is_available", lambda: True)
    monkeypatch.setattr(hr, "register_purge", lambda *a, **k: 0)
    monkeypatch.setattr(hr, "register_ingest",
                        lambda rows, *a, **k: (written.extend(rows), len(rows))[1])

    result = hr.ingest_source("epc", str(path), areas=["B16"])

    assert result["loaded"] == 3  # one postcode total + its two dwellings
    assert result["source"] == hr.EPC_SOURCE["name"]
    totals = [r for r in written if "uprn" not in r]
    assert totals == [{"postcode": "B16 9BH", "households": 2}]
    # B17 was filtered out before any row was built.
    assert all(r["postcode"] == "B16 9BH" for r in written)


def test_a_countless_epc_file_is_refused_before_the_register_is_purged(
        tmp_path, monkeypatch):
    path = tmp_path / "not_epc.csv"
    path.write_text("UPRN,CURRENT_ENERGY_RATING\n1,C\n", encoding="utf-8")
    monkeypatch.setattr(hr.postgis, "is_available", lambda: True)

    def unexpected_purge(*_a, **_k):
        raise AssertionError("a file with no postcode must not purge existing rows")

    monkeypatch.setattr(hr, "register_purge", unexpected_purge)
    with pytest.raises(ValueError, match="postcode column"):
        hr.ingest_source("epc", str(path))


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _api_pages(pages, seen):
    """An opener that serves `pages` in order and records the requests."""
    queue = list(pages)

    def opener(request):
        seen.append(request)
        return _FakeResponse(queue.pop(0) if queue else {"rows": []})

    return opener


def test_the_api_reader_aggregates_like_the_bulk_file():
    seen = []
    pages = [{"rows": [
        {"UPRN": "100000000001", "POSTCODE": "B16 9BH", "LMK_KEY": "c1"},
        {"UPRN": "100000000002", "POSTCODE": "B16 9BH", "LMK_KEY": "c2"},
        {"UPRN": "100000000002", "POSTCODE": "B16 9BH", "LMK_KEY": "c3"},
    ]}]
    records = list(hr.read_epc_api(hr.EPC_API_SOURCE, ["B16 9BH"],
                                   opener=_api_pages(pages, seen), token="t"))
    # Three certificates, two dwellings: same rule as the CSV.
    assert [r for r in records if "uprn" not in r] == [
        {"postcode": "B16 9BH", "households": 2}
    ]
    assert len([r for r in records if "uprn" in r]) == 2
    assert len(seen) == 1


def test_the_api_request_is_authenticated_and_filtered():
    seen = []
    list(hr.read_epc_api(hr.EPC_API_SOURCE, ["B16 9BH"],
                         opener=_api_pages([{"rows": []}], seen), token="secret"))
    request = seen[0]
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.full_url.startswith(hr.EPC_API_SOURCE["url"])
    assert "postcode=B16+9BH" in request.full_url
    assert "page_size=" in request.full_url


def test_a_next_link_is_followed_instead_of_counting_pages():
    seen = []
    pages = [
        {"rows": [{"UPRN": "1", "POSTCODE": "B16 9BH", "LMK_KEY": "c1"}],
         "next": "https://api.test/next?page=2"},
        {"rows": [{"UPRN": "2", "POSTCODE": "B16 9BH", "LMK_KEY": "c2"}]},
    ]
    records = list(hr.read_epc_api(hr.EPC_API_SOURCE, ["B16 9BH"],
                                   opener=_api_pages(pages, seen), token="t"))
    assert seen[1].full_url == "https://api.test/next?page=2"
    assert [r for r in records if "uprn" not in r] == [
        {"postcode": "B16 9BH", "households": 2}
    ]


def test_the_api_token_comes_from_the_environment_and_is_required():
    source = hr.EPC_API_SOURCE
    assert hr.epc_api_token(source, {"EPC_API_TOKEN": " abc "}) == "abc"
    # Legacy/second spelling still works.
    assert hr.epc_api_token(source, {"EPC_API_KEY": "xyz"}) == "xyz"
    with pytest.raises(ValueError, match="needs a token"):
        hr.epc_api_token(source, {})


def test_the_api_source_is_registered_as_a_count_source():
    source = hr.REGISTER_SOURCES["epc_api"]
    assert source.get("has_dwellings_count") is not False
    assert source.get("aggregates_to_postcode") is True
    assert source.get("remote") is True


def test_ingesting_the_api_source_needs_postcodes_or_areas(monkeypatch):
    monkeypatch.setattr(hr.postgis, "is_available", lambda: True)
    monkeypatch.setenv("EPC_API_TOKEN", "t")
    with pytest.raises(ValueError, match="needs --postcodes"):
        hr.ingest_source("epc_api")


def test_the_epc_note_states_what_the_number_is():
    note = hr.EPC_SOURCE["note"]
    assert "DWELLINGS" in note and "England" in note
    # The licence travels with the numbers into the design's provenance block.
    assert hr.EPC_SOURCE["licence"].startswith("Open Government Licence")
