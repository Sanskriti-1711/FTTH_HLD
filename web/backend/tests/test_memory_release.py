"""Memory the engine can give back, and the guard that stops it being killed.

Why this exists: on 2026-10-21 a run failed with "The engine process was killed
or restarted before this run wrote any output. This instance has 512 MB..." and
that wording was read as an out-of-memory kill.  Render recorded no `oomKilled`
event -- seven deploys that day replaced the container instead.  So the fixes are
two: an engine that does not hold memory it cannot use (finished tasks, finished
fetch entries, a re-askable suggestion cache, and the area build's own buffers),
and a start that refuses out loud when the container really is close to its cap
rather than being killed with no trace.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_memory_release.py -v
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import main  # noqa: E402
import meminfo  # noqa: E402
import osm_source  # noqa: E402


def _finished(pid: str, minutes_ago: int) -> dict:
    return {
        "project_id": pid,
        "status": "completed",
        "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
        "messages": [],
        "layers": [],
        "downloads": [],
    }


def test_finished_runs_do_not_accumulate_for_the_life_of_the_process():
    main.tasks.clear()
    for i in range(40):
        main.tasks[f"old{i}"] = _finished(f"old{i}", minutes_ago=100 - i)

    dropped = main._evict_finished_tasks()

    assert dropped == 40 - main._TASKS_KEPT
    assert len(main.tasks) == main._TASKS_KEPT
    # The NEWEST survive: they are the ones a planner is most likely to open.
    assert "old39" in main.tasks
    assert "old0" not in main.tasks
    main.tasks.clear()


def test_a_running_run_is_never_evicted():
    main.tasks.clear()
    main.tasks["live"] = {
        "project_id": "live", "status": "running", "updated_at": "2000-01-01T00:00:00+00:00",
    }
    for i in range(30):
        main.tasks[f"done{i}"] = _finished(f"done{i}", minutes_ago=i)

    main._evict_finished_tasks()

    assert "live" in main.tasks, "a running run is the one thing that must stay"
    main.tasks.clear()


def test_the_task_being_read_is_not_evicted_out_from_under_the_caller():
    """Evicting it would re-create an empty "queued" stub: a finished run that
    suddenly reports it never started."""
    main.tasks.clear()
    main.tasks["mine"] = _finished("mine", minutes_ago=999)
    for i in range(40):
        main.tasks[f"new{i}"] = _finished(f"new{i}", minutes_ago=i)

    task = main._task("mine")

    assert task["status"] == "completed"
    main.tasks.clear()


def test_release_caches_drops_what_can_be_rebuilt_and_keeps_a_live_fetch(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        osm_source, "_FETCHES",
        {
            "ready-old": {"state": "ready", "updated_at": stale, "started_at": stale},
            "failed-old": {"state": "failed", "updated_at": stale, "started_at": stale},
            "running": {"state": "fetching_buildings", "updated_at": fresh, "started_at": fresh},
        },
    )
    monkeypatch.setattr(
        osm_source, "_SUGGEST_CACHE", {("bir", "gb"): (0.0, {"results": []})}
    )

    freed = osm_source.release_caches()

    assert freed["suggestions"] == 1
    assert freed["fetches"] >= 2
    assert set(osm_source._FETCHES) == {"running"}


def test_a_fetch_that_is_over_the_cap_keeps_only_the_newest(monkeypatch):
    now = datetime.now(timezone.utc)
    entries = {
        f"k{i}": {
            "state": "ready",
            "updated_at": (now - timedelta(minutes=60 - i)).isoformat(),
            "started_at": (now - timedelta(minutes=60 - i)).isoformat(),
        }
        for i in range(60)
    }
    monkeypatch.setattr(osm_source, "_FETCHES", entries)

    osm_source._evict_fetches(keep=10)

    assert len(osm_source._FETCHES) == 10
    assert "k59" in osm_source._FETCHES       # newest
    assert "k0" not in osm_source._FETCHES


def test_the_area_build_refuses_to_start_when_the_container_is_tight(monkeypatch, tmp_path):
    """A refusal is visible and retryable; a kill leaves nothing behind."""
    main.tasks.clear()
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: False)
    monkeypatch.setattr(meminfo, "tight", lambda *a, **k: True)
    monkeypatch.setattr(meminfo, "rss_mb", lambda *a, **k: 470.0)
    monkeypatch.setattr(meminfo, "limit_mb", lambda *a, **k: 512.0)
    monkeypatch.setattr(meminfo, "trim", lambda *a, **k: True)

    def never(*a, **k):
        raise AssertionError("the area build must not start on a tight container")

    monkeypatch.setattr(osm_source, "build_inputs", never)

    main._run_area_pipeline("tightrun", "B16 8UU", tmp_path)

    task = main.tasks["tightrun"]
    assert task["status"] == "failed"
    assert "470" in task["error"] and "512" in task["error"]
    assert "Nothing was saved" in task["error"]
    main.tasks.clear()
