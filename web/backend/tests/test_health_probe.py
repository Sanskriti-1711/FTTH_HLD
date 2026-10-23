"""The liveness probe must not scan the store.

Measured on 2026-10-20 against the store this engine serves -- 1,175,963
buildings, 435,185 roads, 129,652 address nodes, 117,254 landuse rows --
/health answered in 0.86-0.96 s because it ran a `SELECT count(*)` over each of
those tables on every call.  keep-warm.yml pings that route every ten minutes so
the free Render service never sleeps, and it is the probe a platform health
check uses, so the cost landed on exactly the caller that needs an answer in
milliseconds: a probe slower than its own timeout reads as "the engine is down"
while the engine is healthy.

The tests below pin the two halves of the fix: /health answers from the cached
snapshot without touching the database, and /health/deep still reads it live.
"""

import time

import pytest

import main


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """Every test starts from a cold cache, and leaves the real one untouched."""
    monkeypatch.setattr(main, "_health_snapshot", {})
    monkeypatch.setattr(main, "_health_snapshot_at", 0.0)
    monkeypatch.setattr(main, "_health_snapshot_error", None)
    monkeypatch.setattr(main, "_health_refreshing", False)


def _good_snapshot():
    return {
        "postgis": {"available": True},
        "osm": {"loaded": True, "tables": {"buildings": 10}},
    }


def _forbid_snapshot(monkeypatch):
    def fail():
        raise AssertionError("the probe read the store on the request path")

    monkeypatch.setattr(main, "_store_snapshot", fail)


def _count_refreshes(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_refresh_store_in_background", lambda: calls.append(1))
    return calls


def test_liveness_does_not_read_the_store(monkeypatch):
    """The whole point: a cold cache must not make the probe wait on four scans."""
    _forbid_snapshot(monkeypatch)
    refreshes = _count_refreshes(monkeypatch)

    body = main.health()

    assert body["status"] == "ok"
    assert body["store_cache"]["pending"] is True
    assert body["store_cache"]["stale"] is True
    assert body["postgis"] == {}
    # ... and it asked for a refresh rather than doing one itself.
    assert refreshes == [1]


def test_a_fresh_snapshot_is_served_without_a_refresh(monkeypatch):
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(main, "_health_snapshot_at", time.time())
    _forbid_snapshot(monkeypatch)
    refreshes = _count_refreshes(monkeypatch)

    body = main.health()

    assert body["postgis"] == {"available": True}
    assert body["osm"]["tables"] == {"buildings": 10}
    assert body["store_cache"]["stale"] is False
    assert body["store_cache"]["age_seconds"] < 5
    assert "pending" not in body["store_cache"]
    assert refreshes == []


def test_a_stale_snapshot_is_still_served(monkeypatch):
    """Stale is not the same as absent: show the last read, and refresh behind it."""
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(
        main, "_health_snapshot_at", time.time() - main.HEALTH_STORE_TTL - 60
    )
    _forbid_snapshot(monkeypatch)
    refreshes = _count_refreshes(monkeypatch)

    body = main.health()

    assert body["postgis"] == {"available": True}
    assert body["store_cache"]["stale"] is True
    assert body["store_cache"]["age_seconds"] > main.HEALTH_STORE_TTL
    assert refreshes == [1]


def test_a_refresh_in_flight_is_not_duplicated(monkeypatch):
    """A burst of probes must not become a burst of table scans."""
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs["name"])

        def start(self):
            started.append("started")

    monkeypatch.setattr(main.threading, "Thread", FakeThread)

    main._refresh_store_in_background()
    main._refresh_store_in_background()
    main._refresh_store_in_background()

    assert started == ["health-store", "started"]
    assert main._health_refreshing is True


def test_a_failed_refresh_keeps_the_last_good_snapshot(monkeypatch):
    """A dead database is reported next to the data, never instead of it.

    This is what the old blocking probe could not do: it returned one answer per
    call, so an outage erased the last thing the store was known to hold.
    """
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(main, "_health_snapshot_at", time.time() - 3600)

    def boom():
        raise RuntimeError("password authentication failed for user \"root\"")

    monkeypatch.setattr(main, "_store_snapshot", boom)
    _count_refreshes(monkeypatch)
    main._refresh_store()  # what the background thread would have done

    body = main.health()

    assert body["postgis"] == {"available": True}
    assert "password authentication failed" in body["store_cache"]["error"]
    assert main._health_snapshot == _good_snapshot()
    # The failure is visible through the age too, which is what a monitor reads.
    assert body["store_cache"]["stale"] is True


def test_deep_reads_the_store_live(monkeypatch):
    """The escape hatch for an operator who needs the truth right now."""
    monkeypatch.setattr(
        main, "_health_snapshot", {"postgis": {"available": False}, "osm": {"loaded": False}}
    )
    monkeypatch.setattr(main, "_health_snapshot_at", time.time())
    monkeypatch.setattr(main, "_store_snapshot", _good_snapshot)

    body = main.health_deep()

    assert body["postgis"] == {"available": True}
    assert body["osm"]["loaded"] is True
    assert body["store_cache"]["stale"] is False
    assert main._health_snapshot == _good_snapshot()


def test_both_routes_are_registered():
    paths = {route.path for route in main.app.routes}

    assert "/health" in paths
    assert "/health/deep" in paths


# ---------------------------------------------------------------------------
# /health must not report work that does not exist
# ---------------------------------------------------------------------------
# `_task()` is a lookup that also creates, and its default status is "queued".
# A Mode B re-plan line from a thread with no LLD sink used to call it, leaving a
# placeholder that nothing finishes: /health then reported a completed run as
# `queued` for the life of the container, and the release workflow's "wait for
# the engine to be idle" step sat out its 20-minute timeout before deploying
# (measured 2026-10-23 on the 1.7.22 deploy).


def test_a_log_line_does_not_make_the_engine_look_busy(monkeypatch):
    """The log may create an entry (it has to land somewhere); health must not
    read that entry as work waiting for a slot."""
    monkeypatch.setattr(main, "tasks", {})
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(main, "_health_snapshot_at", time.time())

    main._append("ghost-project", "info", "a pipeline line with no run in scope")

    body = main.health()

    assert main.tasks["ghost-project"]["status"] == "queued"
    assert "ghost-project" not in body["runs"]["active"]
    assert body["runs"]["queued"] == 0


def test_a_log_line_still_reaches_a_real_run(monkeypatch):
    monkeypatch.setattr(main, "tasks", {})
    task = main._task("real-project")
    task.update({"status": "running", "stage": "Object Layer"})

    main._append("real-project", "info", "Progress 3/10")

    assert [m["text"] for m in task["messages"]] == ["Progress 3/10"]


def test_a_placeholder_is_not_queued_work(monkeypatch):
    monkeypatch.setattr(main, "tasks", {})
    main._task("placeholder")  # exactly what `_task()` creates: queued, no stage
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(main, "_health_snapshot_at", time.time())

    body = main.health()

    assert "placeholder" not in body["runs"]["active"]
    assert body["runs"]["queued"] == 0


def test_a_submitted_run_is_queued_work(monkeypatch):
    monkeypatch.setattr(main, "tasks", {})
    task = main._task("submitted")
    task.update({"status": "queued", "stage": "Queued", "output_dir": "/tmp/out"})
    monkeypatch.setattr(main, "_health_snapshot", _good_snapshot())
    monkeypatch.setattr(main, "_health_snapshot_at", time.time())

    body = main.health()

    assert body["runs"]["active"] == ["submitted"]
    assert body["runs"]["queued"] == 1
    assert body["runs"]["running"] == 0
