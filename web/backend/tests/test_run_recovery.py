"""Unit tests for run recovery: what a run's status is when its thread is gone.

The engine holds a run's stage and progress in memory while the pipeline
streams its output.  Restarting the engine kills that thread, the project row
keeps its last write ("running"), and the status page then shows a run that is
neither alive nor finished.  The qgis_process child usually OUTLIVES the
restart -- it kept writing one project's ducts for two hours afterwards -- so
"the owner is gone" is not by itself proof that a run failed.

These tests pin the rule the status page now follows: the output files are the
evidence.  A run whose files are still changing is reported at the stage those
files show; a run with every stage written and every file intact is completed
for real (so the outputs page can show it); anything else is failed, naming the
stage it reached.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _write_gpkg(path: Path, features: int = 3) -> None:
    """A minimal but real GeoPackage (a GeoPackage is a SQLite file)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT)")
        conn.execute(
            "INSERT INTO gpkg_contents (table_name) VALUES (?)", (path.stem,)
        )
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{path.stem}" (fid INTEGER PRIMARY KEY, geom BLOB)'
        )
        for fid in range(features):
            conn.execute(f'INSERT INTO "{path.stem}" (fid, geom) VALUES (?, ?)', (fid, b""))
        conn.commit()
    finally:
        conn.close()


def _stage_output(output_dir: Path, stages: int) -> None:
    """Write the marker files of the first `stages` pipeline stages."""
    for name, markers in main._STAGE_OUTPUT_MARKERS[:stages]:
        for marker in markers:
            _write_gpkg(output_dir / marker)


def _age(path: Path, seconds: float) -> None:
    """Backdate every file in the directory, as an abandoned run looks."""
    when = time.time() - seconds
    for item in path.iterdir():
        if item.is_file():
            os.utime(item, (when, when))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose outputs live in a temp dir, with PostGIS switched off."""
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.postgis, "is_available", lambda: False)
    monkeypatch.setattr(main, "_register_downloads", lambda *a, **k: [])
    main.tasks.clear()
    output_dir = tmp_path / "proj1"
    output_dir.mkdir(parents=True, exist_ok=True)
    yield output_dir
    main.tasks.clear()


# ── the stage the files say the run is on ──────────────────────────────────

def test_the_stage_comes_from_the_files_that_exist(project):
    _stage_output(project, 3)  # objects, polygons, network

    info = main._stage_from_outputs(project)

    assert info["stage_name"] == "Trench Layer"
    assert info["stage_index"] == 3
    assert info["last_complete_stage"] == "Network Layer"
    assert info["all_stages_written"] is False
    assert "Final_Trenches.gpkg" in info["missing"]


def test_a_run_with_every_stage_written_reads_as_complete(project):
    _stage_output(project, len(main.PIPELINE_STAGES))

    info = main._stage_from_outputs(project)

    assert info["all_stages_written"] is True
    assert info["missing"] == []
    # With every stage present, the stage reported is the last one -- that is
    # the stage the run was on when it stopped writing.
    assert info["stage_name"] == main.PIPELINE_STAGES[-1]
    assert info["progress"] == 100


def test_a_half_written_geopackage_is_reported_as_damaged(project):
    """A pipeline killed mid-write leaves a GeoPackage that fails its check.
    This is how a truncated output is told apart from a finished one."""
    _stage_output(project, len(main.PIPELINE_STAGES))
    (project / "Distribution_Ducts.gpkg").write_bytes(b"SQLite format 3\x00 truncated")

    info = main._stage_from_outputs(project)

    assert "Distribution_Ducts.gpkg" in info["damaged"]


def test_an_empty_output_directory_says_nothing(project):
    assert main._stage_from_outputs(project) is None
    assert main._stage_from_outputs(project.parent / "nope") is None


# ── settling an orphaned run ───────────────────────────────────────────────

def test_a_run_whose_files_are_still_changing_stays_running(project):
    """An engine restart does not kill qgis_process: its files can keep moving
    for a long time afterwards, and reporting that as a failure would be wrong.
    The stage shown is the one the files have reached."""
    _stage_output(project, 4)
    task = main._task("proj1")
    task.update({"status": "running", "stage": "Object Layer", "stage_index": 0})

    main._recover_orphan_run("proj1", task)

    assert task["status"] == "running"
    # Reported at the stage its files prove, not the one the lost thread claimed.
    # Four stages are written (object, polygon, network, trench), so the run is
    # on the NEXT one. Index 4 is the Chamber Layer: the cascade is trench →
    # chambers → ducts → cables, because the duct and cable stages both run on
    # chamber-to-chamber spans.
    assert task["stage"] == "Chamber Layer"
    assert task["stage_index"] == 4


def test_a_finished_run_is_completed_so_the_outputs_page_can_show_it(project, monkeypatch):
    _stage_output(project, len(main.PIPELINE_STAGES))
    _age(project, main.ORPHAN_DEAD_SECONDS + 60)
    ingested = []
    monkeypatch.setattr(
        main, "_ingest_outputs",
        lambda pid, out: ingested.append(pid) or [{"name": "objects", "feature_count": 3}],
    )
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)

    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["stage"] == "Complete"
    assert task["layers"] == [{"name": "objects", "feature_count": 3}]
    assert ingested == ["proj1"]


def test_a_damaged_run_fails_and_says_which_stage_it_reached(project):
    _stage_output(project, len(main.PIPELINE_STAGES))
    (project / "Distribution_Ducts.gpkg").write_bytes(b"not a database")
    _age(project, main.ORPHAN_BROKEN_SECONDS + 60)
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)

    assert task["status"] == "failed"
    # Named, not a generic failure: the planner needs to know where it stopped.
    assert "Duct Layer" in task["error"]
    assert "Distribution_Ducts.gpkg" in task["error"]
    assert "run the area again" in task["error"].lower()


def test_an_incomplete_run_fails_with_the_stage_it_reached(project):
    _stage_output(project, 2)  # objects and polygons only
    _age(project, main.ORPHAN_DEAD_SECONDS + 60)
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)

    assert task["status"] == "failed"
    assert "Polygon Layer" in task["error"]
    # The files it never got to are named, so the gap is legible.
    assert "PDPs.gpkg" in task["error"]


def test_a_run_that_never_wrote_anything_fails_immediately(project):
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)

    assert task["status"] == "failed"
    assert "never wrote" in task["error"] or "before this run wrote" in task["error"]


def test_a_finished_run_is_not_settled_twice(project, monkeypatch):
    """Two status polls arriving together must not both ingest the outputs."""
    _stage_output(project, len(main.PIPELINE_STAGES))
    _age(project, main.ORPHAN_DEAD_SECONDS + 60)
    calls = []
    monkeypatch.setattr(
        main, "_ingest_outputs",
        lambda pid, out: calls.append(pid) or [{"name": "objects"}],
    )
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)
    main._recover_orphan_run("proj1", task)  # second poll, now completed

    assert calls == ["proj1"]


def test_the_settled_state_is_written_to_postgis(project, monkeypatch):
    """The project list and the outputs page read the row, so a stage that only
    exists in this process's memory is invisible everywhere else."""
    _stage_output(project, 2)
    _age(project, main.ORPHAN_DEAD_SECONDS + 60)
    written = {}

    def fake_upsert(project_id, **kwargs):
        written.update(kwargs)
        written["project_id"] = project_id

    monkeypatch.setattr(main.postgis, "is_available", lambda: True)
    monkeypatch.setattr(main.postgis, "upsert_project", fake_upsert)
    task = main._task("proj1")
    task.update({"status": "running"})

    main._recover_orphan_run("proj1", task)

    assert written["status"] == "failed"
    assert written["stage_name"]
    assert written["stage_index"] is not None
    assert written["stage_count"] == len(main.PIPELINE_STAGES)


def test_a_failed_run_is_not_resurrected_as_completed_by_the_disk_restore(project, monkeypatch):
    """The outputs page opens a project by restoring it from its files, and
    that restore used to force ANY non-running task to completed / 100 % — so
    an interrupted run reported itself finished the moment anyone looked at it.
    The status is the record; the restore only supplies the files."""
    _stage_output(project, len(main.PIPELINE_STAGES))
    (project / "Distribution_Ducts.gpkg").write_bytes(b"not a database")
    task = main._task("proj1")
    task.update({
        "status": "failed",
        "error": "interrupted during the Duct Layer",
        "stage": "Duct Layer",
        "stage_index": 4,
    })
    # A restore that would export GeoJSON needs ogr2ogr; stub it out.
    monkeypatch.setattr(main, "_ensure_geojson", lambda gpkg, geojson: False)

    restored = main._restore_task_from_disk("proj1")

    assert restored is not None
    assert restored["status"] == "failed"
    assert restored["progress"] != 100
    assert restored["error"] == "interrupted during the Duct Layer"


def test_a_damaged_output_is_not_published_by_the_restore(project, monkeypatch):
    """Serving a half-written GeoPackage to the results map breaks the page
    instead of showing the rest of the design."""
    _stage_output(project, len(main.PIPELINE_STAGES))
    (project / "Distribution_Ducts.gpkg").write_bytes(b"not a database")
    task = main._task("proj1")
    task.update({"status": "failed", "error": "interrupted"})
    monkeypatch.setattr(main, "_ensure_geojson", lambda gpkg, geojson: False)

    restored = main._restore_task_from_disk("proj1")

    assert "Distribution_Ducts.gpkg" in restored["damaged_outputs"]
    ducts_files = (restored["files"] or {}).get("ducts", [])
    assert not any("Distribution_Ducts" in f for f in ducts_files)
    # The rest of the ducts layer is still there.
    assert any("Feeder_Ducts" in f for f in ducts_files)


def test_a_live_run_is_never_settled(project):
    """Only runs this process does not own are settled. A pipeline streaming
    right now is in the registry because THIS process started it."""
    _stage_output(project, 2)
    task = main._task("proj1")
    task.update({"status": "running", "stage": "Trench Layer", "stage_index": 3})

    assert main._owned_in_process("proj1") is True

    # A task rebuilt from the project row is the opposite: no thread here.
    task["_from_db_row"] = True
    assert main._owned_in_process("proj1") is False
    del task["_from_db_row"]

    # And even when asked, a run whose files are still changing is not settled.
    other = project.parent / "proj2"
    other.mkdir(parents=True, exist_ok=True)
    for _name, markers in main._STAGE_OUTPUT_MARKERS[:4]:
        for marker in markers:
            _write_gpkg(other / marker)
    task2 = main._task("proj2")
    task2.update({"status": "running", "stage": "Object Layer", "stage_index": 0})
    main._recover_orphan_run("proj2", task2)
    assert task2["status"] == "running"


# ── freshness: what a run is allowed to publish ──────────────────────────────

def test_a_layer_this_run_did_not_write_is_removed_not_published(tmp_path):
    """A stage with nothing to publish must not leave the previous layer behind.

    The pole + aerial stages return early when no leg is aerial ("nothing to
    build") and never create their sinks, so ``Aerial_Spans.gpkg`` /
    ``Aerial_Cable.gpkg`` / ``Aerial_Drops.gpkg`` kept the PREVIOUS run's
    geometry and the portal served it.  Project 24cd3573: a run the designer
    logged as "Aerial drops: none - every drop leg could be trenched" still
    showed 6 aerial drops, 8 aerial spans and 8 aerial cables on the map.
    """
    started = time.time()
    stale = tmp_path / "Aerial_Spans.gpkg"
    _write_gpkg(stale)
    fresh = tmp_path / "Final_Trenches.gpkg"
    _write_gpkg(fresh)
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("not a layer", encoding="utf-8")
    # Explicit mtimes, so the test does not depend on filesystem resolution.
    old = started - 600
    os.utime(stale, (old, old))

    assert main._drop_stale_outputs("stale1", tmp_path, started) == ["Aerial_Spans.gpkg"]
    assert not stale.exists()
    assert fresh.exists()          # written by this run: kept
    assert unrelated.exists()      # not a published layer: untouched


def test_the_stale_geojson_twin_goes_too_and_the_run_reports_it(tmp_path):
    started = time.time()
    old = started - 600
    for name in ("Aerial_Cable.gpkg", "Aerial_Cable.geojson"):
        path = tmp_path / name
        path.write_text('{"type": "FeatureCollection", "features": []}',
                        encoding="utf-8")
        os.utime(path, (old, old))

    deleted = main._drop_stale_outputs("stale2", tmp_path, started)
    assert sorted(deleted) == ["Aerial_Cable.geojson", "Aerial_Cable.gpkg"]
    assert not (tmp_path / "Aerial_Cable.geojson").exists()

    messages = "\n".join(
        str(m.get("text")) for m in main.tasks["stale2"]["messages"]
    )
    assert "Aerial_Cable.gpkg" in messages
    assert "did not write" in messages


def test_a_fresh_layer_keeps_its_geojson_twin(tmp_path):
    """The twin is the engine's own conversion, not the pipeline's output.

    Judging each file by its own mtime deleted the twin of every layer the run
    DID write (the pipeline writes the GeoPackage; the engine converts it to
    GeoJSON afterwards), so a clean run reported a dozen layers it never
    skipped. Staleness follows the GeoPackage.
    """
    started = time.time()
    _write_gpkg(tmp_path / "Chambers.gpkg")          # written by this run
    twin = tmp_path / "Chambers.geojson"
    twin.write_text('{"type": "FeatureCollection", "features": []}',
                    encoding="utf-8")
    old = started - 600
    os.utime(twin, (old, old))                       # last run's conversion

    assert main._drop_stale_outputs("stale3", tmp_path, started) == []
    assert twin.exists()
    assert (tmp_path / "Chambers.gpkg").exists()


def test_a_layer_with_only_a_stale_geojson_is_still_stale(tmp_path):
    started = time.time()
    twin = tmp_path / "Aerial_Drops.geojson"
    twin.write_text('{"type": "FeatureCollection", "features": []}',
                    encoding="utf-8")
    old = started - 600
    os.utime(twin, (old, old))

    assert main._drop_stale_outputs("stale4", tmp_path, started) == [
        "Aerial_Drops.geojson"]
    assert not twin.exists()


# ── a completed task in memory must not outrank a newer publish ─────────────
#
# The results page reads /ftth/hld/results/<id>, and that endpoint used to
# consult the project row ONLY when the project was unknown to the engine. An
# engine that had run a project earlier in its own life therefore kept serving
# that run's layers for good: a re-ingest (or a re-run started from another
# process) published new layers into PostGIS and onto disk, the page showed the
# previous design — a stale aerial route included — and nothing short of an
# engine restart changed it.
#
# The row is the record every publish writes, so a completed task older than
# the row is stale and must be replaced. An in-flight run keeps its own task:
# its thread is the only thing that knows the live stage.

_BASE = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _task_utc(minutes: int) -> str:
    """The shape a task stores: `_now()` is an ISO string."""
    return (_BASE + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _row_utc(minutes: int) -> datetime:
    """The shape a project row carries: a `timestamptz`."""
    return _BASE + timedelta(minutes=minutes)


def _publish(monkeypatch, layers, when):
    """Make PostGIS answer with one published row."""
    row = {
        "status": "completed", "updated_at": when, "roads_filename": "r.geojson",
        "output_dir": None, "downloads": [], "stage_name": "Complete",
        "stage_index": len(main.PIPELINE_STAGES), "progress": 100,
        "runner": "qgis_process", "error": None,
    }
    monkeypatch.setattr(main.postgis, "is_available", lambda: True)
    monkeypatch.setattr(main.postgis, "get_project", lambda project_id: row)
    monkeypatch.setattr(main.postgis, "list_project_layers",
                        lambda project_id: layers)
    return row


def test_a_completed_task_in_memory_does_not_mask_a_newer_publish(project, monkeypatch):
    _publish(monkeypatch, [{"name": "brownfield", "feature_count": 36}],
             _row_utc(5))
    task = main._task("proj1")
    task.update({"status": "completed", "updated_at": _task_utc(1),
                 "layers": [{"name": "aerial_drops", "feature_count": None}]})

    result = main.get_results("proj1")

    assert [layer["name"] for layer in result["layers"]] == ["brownfield"]
    assert result["_from_db_row"] is True


def test_an_in_flight_run_keeps_its_own_layers(project, monkeypatch):
    _publish(monkeypatch, [{"name": "brownfield"}], _row_utc(5))
    task = main._task("proj1")
    task.update({"status": "running", "updated_at": _task_utc(1),
                 "layers": [{"name": "objects"}]})

    result = main.get_results("proj1")

    assert [layer["name"] for layer in result["layers"]] == ["objects"]
    assert not result.get("_from_db_row")


def test_a_row_older_than_the_task_does_not_replace_it(project, monkeypatch):
    _publish(monkeypatch, [{"name": "brownfield"}], _row_utc(1))
    task = main._task("proj1")
    task.update({"status": "completed", "updated_at": _task_utc(5),
                 "layers": [{"name": "trenches"}]})

    result = main.get_results("proj1")

    assert [layer["name"] for layer in result["layers"]] == ["trenches"]


def test_an_unknown_timestamp_never_wins_a_freshness_comparison():
    """A naive row timestamp is read as UTC, and a missing one is not "now".

    Postgres hands back an aware ``timestamptz`` and the task is an ISO string,
    but a naive datetime must still land on the right side of the comparison
    rather than raising or beating a newer task.
    """
    naive_older = datetime(2026, 10, 12, 12, 0, 0)     # = 12:00Z, before 12:01Z
    naive_newer = datetime(2026, 10, 12, 12, 5, 0)     # = 12:05Z, after 12:01Z
    assert main._row_is_newer({"updated_at": naive_older},
                              {"updated_at": _task_utc(1)}) is False
    assert main._row_is_newer({"updated_at": naive_newer},
                              {"updated_at": _task_utc(1)}) is True
    assert main._row_is_newer({"updated_at": None},
                              {"updated_at": _task_utc(1)}) is False
    assert main._row_is_newer({"updated_at": "not a timestamp"},
                              {"updated_at": _task_utc(1)}) is False
    assert main._row_is_newer({"updated_at": _row_utc(5)},
                              {"updated_at": None}) is False


def test_the_live_reads_are_never_cached(monkeypatch):
    """A reload must not be answered from the browser's cache.

    The results endpoint reports THIS run's layers, and a 200 GET with no cache
    directive is cacheable — so a re-published project could keep showing the
    previous layer list (a stale aerial route included) until a hard refresh.
    The downloads and tiles keep their caching on purpose.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "OUTPUT_DIR", Path("nope-does-not-exist"))
    main.tasks.clear()
    client = TestClient(main.app)

    results = client.get("/ftth/hld/results/proj1")
    assert results.headers.get("cache-control") == "no-store"

    # ... and the routes that SHOULD stay cacheable are untouched.
    for path in ("/ftth/hld/download/proj1/Objects.gpkg", "/tiles/objects/1/1/1.pbf"):
        resp = client.get(path)
        assert resp.headers.get("cache-control") != "no-store", path
    main.tasks.clear()


# ── which engine owns a row ────────────────────────────────────────────────

def test_a_run_owned_by_another_engine_is_left_alone(monkeypatch):
    """The project table is shared; "no thread here" is not proof of death.

    Measured 2026-10-20: a run started on the laptop engine at 12:5x was settled
    as failed by the 512 MB Render engine five seconds after that deploy went
    live, because the Render process could not see the laptop's thread -- and
    the planner lost the run.
    """
    monkeypatch.setattr(main.postgis, "ENGINE_INSTANCE", "local")
    main.tasks.clear()

    row = {
        "project_id": "foreign-run", "status": "running",
        "stage_name": "Duct Layer", "stage_index": 5, "progress": 55,
        "engine_instance": "render-engine",
    }

    assert main._recover_orphan_from_row(row) is None
    assert main.tasks.get("foreign-run") is None


def test_a_run_owned_by_this_engine_is_still_recovered(monkeypatch, tmp_path):
    monkeypatch.setattr(main.postgis, "ENGINE_INSTANCE", "local")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    main.tasks.clear()

    row = {
        "project_id": "my-run", "status": "running",
        "stage_name": "Duct Layer", "stage_index": 5, "progress": 55,
        "engine_instance": "local",
    }

    task = main._recover_orphan_from_row(row)

    assert task is not None
    assert task["status"] == "failed"
    assert task["stage"] == "Duct Layer" and task["progress"] == 55


def test_a_row_with_no_owner_is_left_alone(monkeypatch, tmp_path):
    """A row that no engine stamped is not proof of anything.

    Measured 2026-10-20, after the first ownership fix shipped: the laptop
    engine was still running the pre-fix code, so its run had no owner -- and
    the Render engine settled it as failed within seconds, exactly the bug the
    stamp was meant to stop. "No owner" therefore cannot mean "mine": leaving
    a dead row running costs a stale list entry, settling a live run costs the
    run.
    """
    monkeypatch.setattr(main.postgis, "ENGINE_INSTANCE", "local")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    main.tasks.clear()

    assert main._recover_orphan_from_row({"project_id": "old-run", "status": "running"}) is None
    assert main.tasks.get("old-run") is None


# ── writing the row the project list reads ─────────────────────────────────

def test_a_dropped_connection_is_retried_before_the_stage_is_lost(monkeypatch):
    """The row is the only thing the list and the restart guard can read.

    Measured 2026-10-20 (run e8135f4c): the row was last written at 13:40:56
    with "Object Layer 0 %" while the task itself reached "Duct Layer 55 %" at
    13:56:43. The write is best-effort by design -- it must never fail a status
    read -- but a connection that died mid-run has to be retried on a fresh one,
    or every later stage becomes invisible.
    """
    calls = []
    closed = []

    def upsert(*a, **k):
        calls.append(k.get("stage_name"))
        if len(calls) == 1:
            raise RuntimeError("connection already closed")

    monkeypatch.setattr(main.postgis, "is_available", lambda: True)
    monkeypatch.setattr(main.postgis, "upsert_project", upsert)
    monkeypatch.setattr(main.postgis, "close_conn", lambda: closed.append(True))

    main._persist_project_state("run1", {"status": "running", "stage": "Duct Layer",
                                         "stage_index": 5, "progress": 55})

    assert calls == ["Duct Layer", "Duct Layer"]
    assert closed == [True]


def test_a_write_that_keeps_failing_is_reported_not_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(main.postgis, "is_available", lambda: True)
    monkeypatch.setattr(main.postgis, "close_conn", lambda: None)

    def upsert(*a, **k):
        raise RuntimeError("server closed the connection unexpectedly")

    monkeypatch.setattr(main.postgis, "upsert_project", upsert)

    main._persist_project_state("run2", {"status": "running", "stage": "Duct Layer"})

    out = capsys.readouterr().out
    assert "[persist] run2 could not be written" in out
    assert "server closed the connection unexpectedly" in out


def test_a_missing_database_is_reported_once_per_run(monkeypatch, capsys):
    monkeypatch.setattr(main.postgis, "is_available", lambda: False)
    monkeypatch.setattr(main.postgis, "psycopg2", object())
    main._PERSIST_UNAVAILABLE.clear()

    for _ in range(3):
        main._persist_project_state("run3", {"status": "running", "stage": "Duct Layer"})

    out = capsys.readouterr().out
    assert out.count("[persist] run3 not written: PostGIS unavailable") == 1
    main._PERSIST_UNAVAILABLE.clear()
