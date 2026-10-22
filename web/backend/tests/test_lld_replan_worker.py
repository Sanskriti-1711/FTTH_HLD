"""Mode B on the QGIS worker must ship every path its command names.

The pipeline runs on the worker, in its own container, so the arguments'
absolute paths have to exist there.  A verify run writes into
``outputs/<project>/`` and its inputs are inside it, so zipping the output
directory is enough.  Mode B reads the address workbook and roads from
``outputs/<project>/inputs/`` and its brownfield files from
``outputs/<project>/replan/<version>/brownfield/`` — neither is inside the
design directory it writes to.

On 2026-10-22 the hosted engine shipped only the design directory, and
qgis_process exited 1 with "Incorrect parameter value for ROADS": the file was
never uploaded.  These tests pin the payload both ways round.

Run (from HLD_Planning_01/web/backend):

    env -u PYTHONPATH -u PYTHONHOME python -m pytest tests/test_lld_replan_worker.py -v
"""

import io
import json
import sys
import zipfile

import pytest

import main as engine


PROJECT = "modeb-worker-project"
VERSION = "LLD-W01"


def _result_zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(sorted(members)))
    return buffer.getvalue()


@pytest.fixture()
def outputs(tmp_path, monkeypatch):
    """A project tree shaped like a Mode B run's, with its inputs present."""
    root = tmp_path / "outputs"
    project = root / PROJECT
    (project / "inputs").mkdir(parents=True)
    (project / "inputs" / "Main_DataSet.xlsx").write_bytes(b"xlsx")
    (project / "inputs" / "roads.geojson").write_text("{}", encoding="utf-8")
    brown = project / "replan" / VERSION / "brownfield"
    brown.mkdir(parents=True)
    (brown / "bf_trenches.geojson").write_text("{}", encoding="utf-8")
    (brown / "bf_ducts.geojson").write_text("{}", encoding="utf-8")
    (project / "replan" / VERSION / "design").mkdir(parents=True)

    monkeypatch.setattr(engine, "OUTPUT_DIR", root)
    monkeypatch.setattr(engine, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(engine, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(engine, "QGIS_WORKER_POLL_S", 0.01)
    monkeypatch.setattr(engine, "_lld_append", lambda *a, **k: None)
    return root


def _fake_worker(uploads, calls):
    def fake_call(path, data=None, method="GET", timeout=120):
        calls.append((method, path))
        if method == "PUT":
            uploads.append(zipfile.ZipFile(io.BytesIO(data)))
            return b"{}"
        if method == "POST":
            return b"{}"
        if path.endswith("/log?offset=0") or "/log?" in path:
            return b'{"lines": [], "offset": 0, "state": "done"}'
        if path.endswith("/result"):
            return _result_zip(
                {
                    "replan/%s/design/Final_Trenches.gpkg" % VERSION: b"gpkg",
                    # The worker echoes the inputs it was given; they must come
                    # back unchanged, not nested under the design directory.
                    "inputs/roads.geojson": b"{}",
                }
            )
        return b'{"state": "done", "exit_code": 0}'

    return fake_call


def test_mode_b_ships_the_inputs_and_brownfield_the_worker_needs(outputs, monkeypatch):
    uploads, calls = [], []
    monkeypatch.setattr(engine, "_worker_call", _fake_worker(uploads, calls))

    design = outputs / PROJECT / "replan" / VERSION / "design"
    written = engine._run_command(
        PROJECT,
        ["qgis_process", "run", "hldplanning:end_to_end_pipeline", "--"],
        design,
        transfer=(outputs / PROJECT, ("inputs", "replan/%s" % VERSION)),
    )

    assert uploads, "nothing was uploaded to the worker"
    names = set(uploads[0].namelist())
    # Every path the command names, at the name the worker will see it.
    assert "inputs/Main_DataSet.xlsx" in names
    assert "inputs/roads.geojson" in names
    assert "replan/%s/brownfield/bf_trenches.geojson" % VERSION in names
    assert "replan/%s/brownfield/bf_ducts.geojson" % VERSION in names
    # Members are relative to the project tree: an "outputs/" prefix would
    # unpack one directory too deep and break every absolute path.
    assert not any(name.startswith("outputs/") for name in names)

    # The run's files land where the pipeline writes them.
    assert (design / "Final_Trenches.gpkg").exists()
    assert not (design / "inputs").exists(), "the inputs were unpacked into the design dir"
    assert written is not None


def test_a_verify_run_still_ships_only_its_output_directory(outputs, monkeypatch):
    """The HLD/verify path is unchanged: paths are inside the output dir."""
    uploads, calls = [], []
    monkeypatch.setattr(engine, "_worker_call", _fake_worker(uploads, calls))

    project = outputs / PROJECT
    (project / "Final_Trenches.gpkg").write_bytes(b"gpkg")
    expected = set(
        zipfile.ZipFile(io.BytesIO(engine._zip_directory(project))).namelist()
    )
    engine._run_command("modeb-worker-project", ["qgis_process", "run", "x", "--"], project)

    # Unchanged behaviour: the payload is exactly the output directory, whose
    # own sub-directories (inputs/ in a verify run) travel with it.
    assert "Final_Trenches.gpkg" in expected
    assert set(uploads[0].namelist()) == expected


def test_the_lld_replan_passes_the_project_tree_as_the_transfer_root(outputs, monkeypatch):
    """Mode B hands the engine the tree its command's paths belong to."""
    captured = {}

    def fake_run(project_id, cmd, output_dir, transfer=None):
        captured["transfer"] = transfer
        captured["design"] = output_dir
        return []

    monkeypatch.setattr(engine, "_run_command", fake_run)
    monkeypatch.setattr(engine, "_find_qgis_process", lambda: "qgis_process")
    monkeypatch.setattr(engine, "_restore_inputs", lambda *a, **k: None)
    monkeypatch.setattr(engine.occupancy, "write_duct_brownfield", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_write_replan_brownfield", lambda *a, **k: [])
    monkeypatch.setattr(engine, "_ensure_geojson", lambda *a, **k: None)
    monkeypatch.setattr(engine, "lld_tasks", {})

    # The pipeline writes no layers here; the run fails at its own check.
    engine._run_lld_replan(PROJECT, VERSION, {"features": []})
    task = engine.lld_tasks[engine._lld_key(PROJECT, VERSION)]
    assert task["status"] == "failed"
    assert "no layers" in str(task.get("error") or "").lower()

    transfer_root, subpaths = captured["transfer"]
    assert transfer_root == outputs / PROJECT
    assert "inputs" in subpaths
    assert "replan/%s" % VERSION in subpaths
    # The pipeline still writes to the design directory, not to the tree root.
    assert captured["design"] == outputs / PROJECT / "replan" / VERSION / "design"
