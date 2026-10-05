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
