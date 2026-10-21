"""An empty answer is not a downloaded area.

Why this exists: on 2026-10-21 the area download "completed" in 89 seconds with
every group reporting zero rows.  `overpass.osm.ch` answers 200 with a
well-formed `{"elements": []}` for *everything*, including the liveness probe
(`node(id:1)`), so it sorted first, answered all seven groups with nothing, and
`extract_meta` recorded B16 8UU as downloaded.  From then on `covered_by_cache`
treated the area as ready and every read of it returned zero premises -- while
815 of its buildings were sitting in the store.

Two things had to be true for that to happen, so both are pinned here: a mirror
that answers with nothing is not alive, and a fetch that stored nothing must not
be written down as a finished extract.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_osm_empty_fetch.py -v
"""

from __future__ import annotations

import pathlib
import sys

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import osm_source  # noqa: E402

BBOX = (-1.9317, 52.4703, -1.9132, 52.4814)


def test_the_probe_rejects_a_mirror_that_answers_with_nothing(monkeypatch):
    """`node(id:1)` exists, so an empty list means the mirror is not serving."""
    monkeypatch.setattr(
        osm_source, "_http_json",
        lambda *a, **k: {"version": 0.6, "elements": []},
    )

    assert osm_source._probe_overpass_mirror("https://empty.example/api/interpreter") is False


def test_the_probe_accepts_a_mirror_that_returns_the_node(monkeypatch):
    monkeypatch.setattr(
        osm_source, "_http_json",
        lambda *a, **k: {"elements": [{"type": "node", "id": 1}]},
    )

    assert osm_source._probe_overpass_mirror("https://live.example/api/interpreter") is True


def _empty_fetch(groups, bbox, on_group=None, on_start=None):
    for name, _body in groups:
        if on_start:
            on_start(name)
        if on_group:
            on_group(name, [], "https://empty.example/api/interpreter")


def test_a_fetch_that_stored_nothing_is_refused_and_not_recorded(monkeypatch):
    written = []
    monkeypatch.setattr(osm_source, "_persist_fetches", lambda snapshot: None)
    monkeypatch.setattr(osm_source, "fetch_groups", _empty_fetch)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: written.append(a) or True)

    # The refusal is recorded as a FAILED fetch by `_run_area_fetch` itself,
    # which is what stops `covered_by_cache` claiming the area is downloaded.
    osm_source._run_area_fetch(osm_source._bbox_key(BBOX), "B16 8UU", BBOX)

    # No extract row: the area must stay missing from the cache, not cached empty.
    assert written == []
    state = osm_source.area_fetch_state("B16 8UU", BBOX)
    assert state["state"] == "failed"
    assert "no elements" in (state.get("error") or "")


def test_a_fetch_that_stored_rows_is_still_recorded(monkeypatch):
    written = []
    monkeypatch.setattr(osm_source, "_persist_fetches", lambda snapshot: None)

    def fetched(groups, bbox, on_group=None, on_start=None):
        for name, _body in groups:
            if on_start:
                on_start(name)
            if on_group:
                on_group(name, [{"type": "way", "id": 1}], "https://live.example/api/interpreter")

    monkeypatch.setattr(osm_source, "fetch_groups", fetched)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {"buildings": 1})
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: written.append(a) or True)

    osm_source._run_area_fetch(osm_source._bbox_key(BBOX), "B16 8UU", BBOX)

    assert len(written) == 1
    assert osm_source.area_fetch_state("B16 8UU", BBOX)["state"] == "ready"
