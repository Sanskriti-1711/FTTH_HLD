"""Tests for surviving Nominatim's 429 instead of failing the area.

Why this exists: Nominatim limits by IP, and every preview from the engine's
shared host comes from one IP.  A run of previews therefore hit `429 Too Many
Requests` and the page reported "area cannot be loaded" -- while the boundary
for that same area was already in `osm.area_cache`, unchanged.  Two things are
checked here:

* `_http_json` waits the time the server NAMED (`Retry-After`, seconds or an
  HTTP date), bounded by a cap so an hour-long wait cannot hang a request.
* `resolve_area` serves the cached boundary when the lookup is still refused,
  and says so (`stale`) rather than passing a cached polygon off as fresh.

Pure tests: the network and the database are both monkeypatched.
"""

from __future__ import annotations

import email.message
import email.utils
import json
import sys
import time
import urllib.error
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import osm_source  # noqa: E402

URL = "https://nominatim.openstreetmap.org/search?q=x"

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-2.99, 53.40], [-2.97, 53.40], [-2.97, 53.42],
                     [-2.99, 53.42], [-2.99, 53.40]]],
}


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rate_limit(retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(URL, 429, "Too Many Requests", headers, None)


def _ok(payload):
    return lambda *a, **k: _Response(payload)


def test_a_429_waits_the_seconds_the_server_named(monkeypatch):
    # 2 s / 4 s of blind backoff against a limit that says "wait 5" is three
    # requests inside the window the server asked us to stay out of.
    calls = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit(5)
        return _Response({"ok": True})

    slept = []
    monkeypatch.setattr(osm_source.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda s: slept.append(s))

    assert osm_source._http_json(URL) == {"ok": True}
    assert slept == [5.0]


def test_an_http_date_retry_after_is_read_as_seconds(monkeypatch):
    when = email.utils.formatdate(time.time() + 12, usegmt=True)
    calls = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit(when)
        return _Response({"ok": True})

    slept = []
    monkeypatch.setattr(osm_source.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda s: slept.append(s))

    osm_source._http_json(URL)
    assert len(slept) == 1 and 10 <= slept[0] <= 12


def test_an_hour_long_retry_after_is_capped(monkeypatch):
    # Holding a request open for an hour is a hang, not a retry: the cap is
    # what turns "come back later" into an answer the caller can act on.
    calls = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit(3600)
        return _Response({"ok": True})

    slept = []
    monkeypatch.setattr(osm_source.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda s: slept.append(s))

    osm_source._http_json(URL)
    assert slept == [osm_source.RETRY_AFTER_CAP_S]


def test_a_persistent_429_raises_rate_limited_with_the_wait(monkeypatch):
    slept = []
    monkeypatch.setattr(osm_source.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_rate_limit(7)))
    monkeypatch.setattr(osm_source.time, "sleep", lambda s: slept.append(s))

    try:
        osm_source._http_json(URL, retries=2)
    except osm_source.RateLimited as exc:
        assert exc.status == 429
        assert exc.retry_after == 7
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("a 429 that never clears must raise RateLimited")
    # One wait between the two attempts, none after the last one.
    assert slept == [7.0]


def test_a_non_rate_limit_http_error_is_still_a_plain_failure(monkeypatch):
    # 404 is not a pause: waiting for it would only delay the real error.
    slept = []
    monkeypatch.setattr(
        osm_source.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError(URL, 404, "Not Found", email.message.Message(), None)
        ),
    )
    monkeypatch.setattr(osm_source.time, "sleep", lambda s: slept.append(s))

    try:
        osm_source._http_json(URL, retries=1)
    except osm_source.RateLimited:  # pragma: no cover - must not happen
        raise AssertionError("a 404 is not a rate limit")
    except RuntimeError as exc:
        assert "404" in str(exc)
    assert slept == []


CACHED = {
    "area": "L1 8JQ, Liverpool, England, United Kingdom",
    "country_code": "GB",
    "polygon_source": "dataset",
    "polygon": SQUARE,
    "bbox": [-2.99, 53.40, -2.97, 53.42],
    "resolved_at": "2026-10-22T09:00:00",
    "dataset_stamp": "old-stamp",
}


def _limited(monkeypatch, cached):
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "dataset_stamp", lambda code: "new-stamp")
    monkeypatch.setattr(osm_source, "_cached_resolution", lambda key: cached)
    monkeypatch.setattr(
        osm_source, "nominatim_search",
        lambda *a, **k: (_ for _ in ()).throw(osm_source.RateLimited(URL, 429, 20)),
    )


def test_the_cached_boundary_is_served_when_nominatim_is_limited(monkeypatch):
    # The polygon does not change between two previews of the same area, so a
    # rate limit must not become "area cannot be loaded".
    _limited(monkeypatch, dict(CACHED))

    resolution = osm_source.resolve_area("L1 8JQ, Liverpool", country_code="GB")

    assert resolution["polygon"] == SQUARE
    assert resolution["stale"] is True
    assert "429" in resolution["stale_reason"]
    assert resolution["retry_after"] == 20
    assert "2026-10-22T09:00:00" in resolution["stale_reason"]


FEATURE = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-2.98, 53.41]},
    "properties": {
        "osm_type": "node", "osm_id": 1,
        "category": "place", "type": "postcode",
        "display_name": "L1 8JQ, Liverpool, England, United Kingdom",
        "address": {"country_code": "gb", "city": "Liverpool"},
        "lat": 53.41, "lon": -2.98,
    },
}


def _admin_limited(monkeypatch, cached):
    """Search succeeds; the administrative-boundary rung behind it gets the 429."""
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(osm_source, "schema_ready", lambda: True)
    monkeypatch.setattr(osm_source, "dataset_stamp", lambda code: "new-stamp")
    monkeypatch.setattr(osm_source, "_cached_resolution", lambda key: cached)
    monkeypatch.setattr(osm_source, "nominatim_search", lambda *a, **k: [FEATURE])
    monkeypatch.setattr(osm_source, "boundary_dataset_lookup", lambda cc, code: None)
    monkeypatch.setattr(
        osm_source, "admin_boundary_for_point",
        lambda lat, lon: (_ for _ in ()).throw(osm_source.RateLimited(URL, 429, 15)),
    )


def test_a_429_from_the_administrative_rung_serves_the_cached_boundary(monkeypatch):
    # The live failure: the search answered, the reverse lookup behind it was
    # limited, and the whole preview 502'd on an area already in the cache.
    _admin_limited(monkeypatch, dict(CACHED))

    resolution = osm_source.resolve_area("L1 8JQ, Liverpool", country_code="GB")

    assert resolution["polygon"] == SQUARE
    assert resolution["stale"] is True
    assert resolution["retry_after"] == 15


def test_a_429_from_the_administrative_rung_does_not_invent_a_rectangle(monkeypatch):
    # With nothing cached the ladder must NOT fall through to the bbox: that
    # would silently turn a rate limit into a design area nobody chose.
    _admin_limited(monkeypatch, None)

    try:
        osm_source.resolve_area("L1 8JQ, Liverpool", country_code="GB")
    except osm_source.RateLimited as exc:
        assert exc.status == 429
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("an uncached rung must not degrade to a rectangle")


def test_an_uncached_area_still_reports_the_rate_limit(monkeypatch):
    # Nothing to serve: the caller must hear the real cause, not a made-up
    # boundary.  A rectangle invented here would silently widen the design.
    _limited(monkeypatch, None)

    try:
        osm_source.resolve_area("L1 8JQ, Liverpool", country_code="GB")
    except osm_source.RateLimited as exc:
        assert exc.status == 429
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("an uncached area must not invent a boundary")
