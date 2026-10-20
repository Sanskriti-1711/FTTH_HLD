"""Tests for the single-flight wait in `ensure_area_data`.

Why this exists: a cold city download is 10-16 minutes and `FETCH_WAIT_SECONDS`
was 15 minutes, so a caller that arrived at the START of someone else's download
fell out of the wait a little before it finished -- and then started its own
download of the same bbox.  Two threads wrote the same rows and the same
progress entry, so B16 9BH in Birmingham reported "3 of 7 groups" and then
"1 of 7" with the counts reset: the page looked like it was going backwards,
and it was.

These tests are pure: the network, the database and the fetch itself are all
monkeypatched.  What is asserted is HOW MANY downloads run for one bbox, because
a second one does not fail -- it silently doubles the wait.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import osm_source  # noqa: E402

BBOX = [-1.955832191, 52.470333177, -1.912208945, 52.493337407]
AREA = "B16 9BH, Birmingham, United Kingdom"


@pytest.fixture()
def detached(monkeypatch):
    """No database, no network, and a wait measured in milliseconds."""
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "init_schema", lambda: None)
    monkeypatch.setattr(osm_source, "FETCH_WAIT_SECONDS", 0.05)
    osm_source._FETCHES.clear()
    yield monkeypatch
    osm_source._FETCHES.clear()


def _start(monkeypatch, cached=None, sink=None):
    """Run `ensure_area_data` on its own thread, capturing what it returns."""
    result: dict = {}

    def run():
        try:
            result["value"] = osm_source.ensure_area_data(AREA, BBOX)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            result["error"] = exc

    monkeypatch.setattr(osm_source, "covered_by_cache", lambda bbox: cached)
    if sink is not None:
        monkeypatch.setattr(osm_source, "_run_area_fetch", sink)
    thread = threading.Thread(target=run, name="waiter", daemon=True)
    thread.start()
    return thread, result


def test_a_waiter_never_starts_a_second_download_that_outlives_one_wait(detached):
    """A caller that arrives mid-download keeps waiting, it does not fork."""
    calls: list[tuple] = []
    key = osm_source._bbox_key(BBOX)
    osm_source._set_fetch_state(key, "fetching_buildings", groups_done=2)

    thread, result = _start(detached, sink=lambda *args: calls.append(args))

    # _join_inflight polls every 0.5s, so the window has to outlast one poll:
    # with a single-slice budget the waiter falls out of the wait at ~0.5s and
    # starts the twin download, which is what this asserts does NOT happen.
    time.sleep(1.5)
    assert thread.is_alive(), "the waiter gave up instead of waiting"
    assert calls == [], "a second download was started for the same bbox"

    osm_source._set_fetch_state(key, "failed", groups_done=2, error="overpass down")
    thread.join(2)

    assert not thread.is_alive()
    assert isinstance(result.get("error"), RuntimeError)
    assert "overpass_fetch_failed" in str(result["error"])
    assert calls == [], "a failed download was retried without being asked to"


def test_a_waiter_returns_the_cache_row_of_the_download_it_waited_for(detached):
    """Waiting past one slice still resolves to the other download's rows."""
    calls: list[tuple] = []
    cache_row: dict = {}
    key = osm_source._bbox_key(BBOX)
    osm_source._set_fetch_state(key, "fetching_buildings", groups_done=2)

    def covered(bbox):
        return cache_row or None

    detached.setattr(osm_source, "_run_area_fetch", lambda *args: calls.append(args))
    detached.setattr(osm_source, "covered_by_cache", covered)

    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update({"value": osm_source.ensure_area_data(AREA, BBOX)}),
        daemon=True,
    )
    thread.start()

    # Longer than one wait slice: the old code gave up here and fetched again.
    time.sleep(1.0)
    cache_row.update({"fetched_at": "2026-10-20T07:54:00", "counts": {"buildings": 4957}})
    osm_source._set_fetch_state(key, "ready", groups_done=7, source="fetched")
    thread.join(5)

    assert not thread.is_alive()
    assert calls == [], "the waiter started its own download of the same bbox"
    assert result["value"]["source"] == "cache"
    assert result["value"]["counts"] == {"buildings": 4957}


def test_a_finished_fetch_without_a_cache_row_is_fetched_once(detached):
    """A fetch that produced no extract leaves nothing to reuse: fetch it."""
    key = osm_source._bbox_key(BBOX)
    osm_source._set_fetch_state(key, "ready", groups_done=7)

    calls: list[tuple] = []

    def fake_fetch(k, area, bbox):
        calls.append((k, area, bbox))
        osm_source._set_fetch_state(k, "ready", groups_done=7, source="fetched")

    thread, result = _start(detached, sink=fake_fetch)
    thread.join(5)

    assert not thread.is_alive()
    assert len(calls) == 1
    assert result["value"]["source"] == "fetched"


def test_two_callers_share_one_download(detached):
    """The bbox is claimed under the lock, so a peer waits instead of fetching."""
    calls: list[tuple] = []
    cache_row: dict = {}

    def fake_fetch(k, area, bbox):
        calls.append((k, area, bbox))
        time.sleep(0.2)
        cache_row.update({"fetched_at": "2026-10-20T07:54:00", "counts": {"buildings": 4957}})
        osm_source._set_fetch_state(k, "ready", groups_done=7, source="fetched")

    def covered(bbox):
        return cache_row or None

    threads = []
    results: list[dict] = []
    for name in ("first", "second"):
        out: dict = {}

        def run(out=out):
            try:
                out["value"] = osm_source.ensure_area_data(AREA, BBOX)
            except BaseException as exc:  # noqa: BLE001
                out["error"] = exc

        detached.setattr(osm_source, "covered_by_cache", covered)
        detached.setattr(osm_source, "_run_area_fetch", fake_fetch)
        thread = threading.Thread(target=run, name=name, daemon=True)
        threads.append(thread)
        results.append(out)
        thread.start()
        time.sleep(0.02)

    for thread in threads:
        thread.join(5)

    assert len(calls) == 1, f"expected one download, started {len(calls)}"
    assert all(not thread.is_alive() for thread in threads)
    assert results[0]["value"]["source"] == "fetched"
    assert results[1]["value"]["source"] == "cache"
