"""The OSM download must not cost the sum of its groups.

Why this exists: the seven Overpass groups were fetched one after another, so a
city's buildings — minutes on their own — held the six small reference layers
back and the page sat on "0 of 7 groups" for eight minutes.  Worse, a query that
simply needed longer than its budget was retried three times on the same mirror
by ``_http_json`` (10.5 minutes) before ``overpass_query`` moved on and started
the same query again.

These tests pin both fixes: the groups run concurrently, the storing stays on
one thread, and a timeout fails fast instead of being retried.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_osm_fetch_groups.py -v
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import urllib.error

import pytest

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import osm_source  # noqa: E402

GROUPS = [(name, body) for name, body in osm_source._OVERPASS_GROUPS.items()]
BBOX = (-0.20, 51.44, -0.10, 51.52)


def _slow_query(seconds=0.2, log=None):
    def _query(body, bbox, timeout=180):
        if log is not None:
            log.append((body, timeout, threading.get_ident()))
        time.sleep(seconds)
        return [], "https://mirror.test/api/interpreter"
    return _query


# ── concurrency ───────────────────────────────────────────────────────────


def test_the_groups_are_fetched_concurrently(monkeypatch):
    monkeypatch.setattr(osm_source, "overpass_query", _slow_query(0.2))

    started = time.perf_counter()
    done = osm_source.fetch_groups(GROUPS, BBOX, workers=len(GROUPS))
    elapsed = time.perf_counter() - started

    assert sorted(done) == sorted(name for name, _ in GROUPS)
    # Serially this is 7 x 0.2 s = 1.4 s. Concurrently it is one wave.
    assert elapsed < 0.7, f"groups still ran serially ({elapsed:.2f}s)"


def test_every_group_lands_exactly_once(monkeypatch):
    seen = []
    monkeypatch.setattr(osm_source, "overpass_query", _slow_query(0.01))

    osm_source.fetch_groups(
        GROUPS, BBOX, workers=3, on_group=lambda name, els, mirror: seen.append(name)
    )

    assert sorted(seen) == sorted(name for name, _ in GROUPS)


def test_storing_happens_on_the_calling_thread(monkeypatch):
    """Every write to the OSM schema must stay on one thread."""
    worker_threads = []
    store_threads = []
    monkeypatch.setattr(osm_source, "overpass_query", _slow_query(0.01, log=worker_threads))

    osm_source.fetch_groups(
        GROUPS, BBOX, workers=3,
        on_group=lambda name, els, mirror: store_threads.append(threading.get_ident()),
    )

    assert len(set(store_threads)) == 1
    assert store_threads[0] == threading.get_ident()
    assert len(set(t for _b, _t, t in worker_threads)) > 1, "nothing actually ran in parallel"


def test_one_worker_keeps_the_old_serial_behaviour(monkeypatch):
    log = []
    monkeypatch.setattr(osm_source, "overpass_query", _slow_query(0.01, log=log))

    order = osm_source.fetch_groups(GROUPS, BBOX, workers=1)

    assert order == [name for name, _ in GROUPS]


def test_the_worker_count_is_bounded(monkeypatch):
    monkeypatch.setenv("OSM_FETCH_WORKERS", "99")
    assert osm_source._group_workers(7) == 7
    monkeypatch.setenv("OSM_FETCH_WORKERS", "0")
    assert osm_source._group_workers(7) == 1
    monkeypatch.setenv("OSM_FETCH_WORKERS", "nonsense")
    assert osm_source._group_workers(7) == 3
    monkeypatch.delenv("OSM_FETCH_WORKERS", raising=False)
    assert osm_source._group_workers(7) == 3


# ── budgets ───────────────────────────────────────────────────────────────


def test_the_heavy_group_gets_the_longer_budget(monkeypatch):
    log = []
    monkeypatch.setattr(osm_source, "overpass_query", _slow_query(0.01, log=log))

    osm_source.fetch_groups(GROUPS, BBOX, workers=1)

    by_body = {body: timeout for body, timeout, _t in log}
    buildings = osm_source._OVERPASS_GROUPS["buildings"]
    roads = osm_source._OVERPASS_GROUPS["roads"]
    assert by_body[buildings] == 300
    assert by_body[roads] == 180


# ── a failure still fails the area ────────────────────────────────────────


def test_a_failing_group_fails_the_fetch(monkeypatch):
    def _query(body, bbox, timeout=180):
        if body == osm_source._OVERPASS_GROUPS["roads"]:
            raise RuntimeError("Overpass request failed on all mirrors")
        return [], "https://mirror.test/api/interpreter"

    monkeypatch.setattr(osm_source, "overpass_query", _query)

    with pytest.raises(RuntimeError, match="all mirrors"):
        osm_source.fetch_groups(GROUPS, BBOX, workers=3)


# ── timeouts are not retried ──────────────────────────────────────────────


def test_a_timeout_is_not_retried(monkeypatch):
    """Retrying the identical too-big query only repeats the wait."""
    calls = []

    def _urlopen(req, timeout=None):
        calls.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(osm_source.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        osm_source._http_json("https://mirror.test/api/interpreter", data=b"q", timeout=30)

    assert len(calls) == 1, "a timeout must move on to the next mirror, not repeat"


def test_a_wrapped_timeout_is_treated_the_same(monkeypatch):
    def _urlopen(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("read timed out"))

    monkeypatch.setattr(osm_source.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        osm_source._http_json("https://mirror.test/api/interpreter", data=b"q", timeout=30)


def test_a_flaky_connection_is_still_retried(monkeypatch):
    """The retry exists for a flaky connection — that must keep working."""
    calls = []

    def _urlopen(req, timeout=None):
        calls.append(timeout)
        raise urllib.error.URLError(ConnectionResetError("reset by peer"))

    monkeypatch.setattr(osm_source.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(osm_source.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="HTTP request failed"):
        osm_source._http_json("https://mirror.test/api/interpreter", data=b"q", timeout=30)

    assert len(calls) == 3
