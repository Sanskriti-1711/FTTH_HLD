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


def test_an_empty_mirror_does_not_hide_the_mirror_that_has_the_data(monkeypatch):
    """The first mirror to answer used to decide -- including when it said nothing.

    Measured 2026-10-21: osm.ch answered `{"elements": []}` for B16 8UU while
    overpass-api.de returned 858 buildings for the same query.  The fetch took
    the empty answer, and the area failed as if the whole service were down.
    """
    silent = "https://silent.example/api/interpreter"
    live = "https://live.example/api/interpreter"
    monkeypatch.setattr(osm_source, "ordered_overpass_mirrors", lambda: [silent, live])
    monkeypatch.setattr(osm_source, "OVERPASS_ROUNDS", 1)
    monkeypatch.setattr(osm_source, "_OVERPASS_EMPTY", {})

    def answer(url, *a, **k):
        if url == silent:
            return {"elements": []}
        return {"elements": [{"type": "way", "id": 7}, {"type": "way", "id": 8}]}

    monkeypatch.setattr(osm_source, "_http_json", answer)

    elements, used = osm_source.overpass_query('way["building"]', BBOX, timeout=5)

    assert used == live
    assert len(elements) == 2
    # ...and the empty one is tried last next time rather than first again.
    assert osm_source._emptied_recently(silent, osm_source.time.monotonic())
    assert not osm_source._emptied_recently(live, osm_source.time.monotonic())


def test_every_mirror_answering_nothing_is_still_reported_as_empty(monkeypatch):
    """No transport error: the caller must see the empty answer and refuse it."""
    a = "https://a.example/api/interpreter"
    b = "https://b.example/api/interpreter"
    monkeypatch.setattr(osm_source, "ordered_overpass_mirrors", lambda: [a, b])
    monkeypatch.setattr(osm_source, "OVERPASS_ROUNDS", 1)
    monkeypatch.setattr(osm_source, "_http_json", lambda *a, **k: {"elements": []})

    elements, used = osm_source.overpass_query('way["building"]', BBOX, timeout=5)

    assert elements == []
    assert used in (a, b)


def test_a_demoted_mirror_is_tried_after_the_ones_carrying_data(monkeypatch):
    silent = "https://silent.example/api/interpreter"
    live = "https://live.example/api/interpreter"
    monkeypatch.setattr(osm_source, "OVERPASS_URLS", (silent, live))
    monkeypatch.setattr(osm_source, "_OVERPASS_HEALTH", {})
    monkeypatch.setattr(osm_source, "_OVERPASS_EMPTY", {})
    monkeypatch.setattr(osm_source, "_probe_overpass_mirror", lambda url: True)

    assert osm_source.ordered_overpass_mirrors() == [silent, live]

    osm_source._mark_overpass_empty(silent)

    assert osm_source.ordered_overpass_mirrors() == [live, silent]


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


MAP_API_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="52.47" lon="-1.93"><tag k="addr:housenumber" v="10"/></node>
  <node id="2" lat="52.4701" lon="-1.93"/>
  <node id="3" lat="52.4702" lon="-1.93"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="building" v="yes"/></way>
  <way id="11"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
  <way id="12"><nd ref="99"/><nd ref="98"/><tag k="building" v="yes"/></way>
</osm>"""


def test_map_api_xml_is_shaped_like_an_overpass_extract():
    """The fallback has to be interchangeable, not merely similar.

    Ways carry `geometry` rebuilt from the same answer's nodes, so the storing
    path and `_element_geometry` cannot tell the two sources apart.
    """
    elements = osm_source._parse_map_api_xml(MAP_API_XML)

    ways = {el["id"]: el for el in elements if el["type"] == "way"}
    assert set(ways) == {10, 11}          # way 12 has no resolvable nodes
    assert len(ways[10]["geometry"]) == 3
    assert osm_source._element_geometry(ways[10])["type"] == "LineString"
    nodes = [el for el in elements if el["type"] == "node"]
    assert nodes[0]["tags"]["addr:housenumber"] == "10"


def test_the_map_api_is_asked_in_lon_lat_order(monkeypatch):
    """A transposed bbox is not an error there -- it is a well-formed EMPTY answer.

    Measured 2026-10-21: (lat, lon) order turned 6.7 MB of B16 8UU into a
    382-byte <osm> with no elements, which the fetch could only read as "this
    area is empty".
    """
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return MAP_API_XML

    def open_url(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(osm_source.urllib.request, "urlopen", open_url)

    osm_source.map_api_extract(BBOX)      # (w, s, e, n)

    bbox = seen["url"].split("bbox=")[1]
    lon0, lat0, lon1, lat1 = (float(v) for v in bbox.split(","))
    assert (lon0, lat0) == (BBOX[0], BBOX[1])
    assert (lon1, lat1) == (BBOX[2], BBOX[3])


def test_group_filters_select_from_a_map_api_extract():
    elements = osm_source._parse_map_api_xml(MAP_API_XML)

    buildings = osm_source.group_elements(
        'way["building"];way["building:part"];way["addr:housenumber"]', elements
    )
    roads = osm_source.group_elements('way["highway"]', elements)
    landuse = osm_source.group_elements('way["landuse"];way["boundary"="protected_area"]', elements)

    assert [el["id"] for el in buildings if el["type"] == "way"] == [10]
    assert [el["id"] for el in roads] == [11]
    assert landuse == []


def _stub_fetch(monkeypatch, map_api):
    """Every Overpass group empty, and a chosen answer from the map API."""
    monkeypatch.setattr(osm_source, "_persist_fetches", lambda snapshot: None)

    def fetched(groups, bbox, on_group=None, on_start=None):
        for name, _body in groups:
            if on_start:
                on_start(name)
            if on_group:
                on_group(name, [], "https://silent.example/api/interpreter")

    monkeypatch.setattr(osm_source, "fetch_groups", fetched)
    monkeypatch.setattr(osm_source, "fetch_groups_from_map_api", map_api)
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: True)


def test_an_empty_overpass_falls_back_to_the_map_api(monkeypatch):
    """A second OSM service is tried before the area is declared undownloadable."""
    stored = []

    def map_api(groups, bbox, on_group=None):
        for name, _body in groups:
            on_group(name, [{"type": "way", "id": 1}], osm_source.OSM_MAP_API_URL)
        return [name for name, _b in groups]

    _stub_fetch(monkeypatch, map_api)
    monkeypatch.setattr(
        osm_source, "store_overpass_elements",
        lambda elements: {"buildings": 1} if elements else {},
    )
    monkeypatch.setattr(osm_source, "_execute", lambda *a, **k: stored.append(a) or True)

    osm_source._run_area_fetch(osm_source._bbox_key(BBOX), "B16 8UU", BBOX)

    state = osm_source.area_fetch_state("B16 8UU", BBOX)
    assert len(stored) == 1, "the area the map API filled must be recorded as downloaded"
    assert state["state"] == "ready"
    # One download feeds all seven groups: the count must not double up.
    assert state["groups_done"] == state["groups_total"] == 7


def test_both_sources_empty_is_refused_and_names_both(monkeypatch):
    def map_api(groups, bbox, on_group=None):
        for name, _body in groups:
            on_group(name, [], osm_source.OSM_MAP_API_URL)
        return []

    _stub_fetch(monkeypatch, map_api)
    monkeypatch.setattr(osm_source, "store_overpass_elements", lambda elements: {})

    osm_source._run_area_fetch(osm_source._bbox_key(BBOX), "B16 8UU", BBOX)

    state = osm_source.area_fetch_state("B16 8UU", BBOX)
    assert state["state"] == "failed"
    assert "no elements" in state["error"]
    assert "map API" in state["error"]


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
