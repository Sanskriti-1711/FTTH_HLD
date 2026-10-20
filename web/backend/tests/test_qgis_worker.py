"""The QGIS worker, and the engine's client for it.

The split exists because the engine and QGIS could not share a 512 MB instance
(measured 2026-10-20: 126 MB + 443 MB).  What these tests protect is the thing
that makes the split safe: the worker runs the SAME argv the API builds, the
same stdout lines drive the same stage strip, and the outputs land back in the
same directory with a manifest saying which files this run wrote.

The end-to-end test uses a real ``ThreadingHTTPServer`` and a real subprocess
(``sys.executable -c ...`` in place of ``qgis_process``), so the protocol is
exercised over HTTP rather than mocked.
"""

import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

import pytest
from http.server import ThreadingHTTPServer

import main
import qgis_worker as worker

TOKEN = "test-token"


def _script(name="Final_Trenches.gpkg"):
    """A stand-in for qgis_process: prints stage lines and writes a layer.

    The output directory arrives as ``argv[1]``, the way the real argv carries
    ``OUTPUT_DIR=...``
    """
    return (
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "print('Running Object Layer')\n"
        "(out / %r).write_bytes(b'SQLite format 3\\x00')\n"
        "print('Running Network Layer')\n"
    ) % name


def _server(tmp_path, monkeypatch, executable=None, concurrency=1):
    monkeypatch.setattr(worker, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(worker, "TOKEN", TOKEN)
    monkeypatch.setattr(worker, "EXECUTABLE", executable or sys.executable)
    monkeypatch.setattr(worker, "CONCURRENCY", concurrency)
    worker._jobs.clear()
    worker._queue.clear()
    worker._active.update({"running": 0, "dispatcher": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), worker.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _call(server, path, data=None, method="GET", token=TOKEN, raw=False):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("X-FTTH-Token", token)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return body if raw else json.loads(body or b"{}")


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# ── auth and safety ────────────────────────────────────────────────────────

def test_job_endpoints_refuse_a_bad_token(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _call(server, "/jobs/p1", token="wrong")
        assert exc.value.code == 401
    finally:
        server.shutdown()


def test_an_unconfigured_worker_refuses_to_be_driven(tmp_path, monkeypatch):
    """No token configured must fail closed, not run jobs for anyone."""
    server = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "TOKEN", "")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _call(server, "/jobs/p1", token="anything")
        assert exc.value.code == 503
    finally:
        server.shutdown()


def test_a_job_id_cannot_escape_the_output_root(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "OUTPUT_DIR", tmp_path)
    with pytest.raises(ValueError):
        worker._job_dir("../escape")
    with pytest.raises(ValueError):
        worker._job_dir("")


def test_an_archive_with_a_traversal_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "OUTPUT_DIR", tmp_path)
    with pytest.raises(ValueError):
        worker.unpack_inputs("p1", _zip({"../../evil.txt": b"x"}))


# ── protocol ───────────────────────────────────────────────────────────────

def test_a_job_runs_and_returns_its_outputs_with_a_manifest(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    try:
        _call(server, "/jobs/p1/inputs", data=_zip({"inputs/roads.geojson": b"{}"}),
              method="PUT")
        started = _call(server, "/jobs/p1", method="POST",
                        data=json.dumps({"args": ["-c", _script(), str(tmp_path / "p1")]}).encode())
        assert started["state"] in ("queued", "running")

        offset, state, lines = 0, "queued", []
        for _ in range(100):
            batch = _call(server, f"/jobs/p1/log?offset={offset}")
            lines.extend(batch["lines"])
            offset = batch["offset"]
            state = batch["state"]
            if state in ("done", "failed"):
                break
            time.sleep(0.1)

        assert state == "done"
        assert "Running Object Layer" in lines
        assert (tmp_path / "p1" / "Final_Trenches.gpkg").is_file()

        result = _call(server, "/jobs/p1/result", raw=True)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read(worker.MANIFEST))
        assert "Final_Trenches.gpkg" in names
        assert "Final_Trenches.gpkg" in manifest
        # The uploaded inputs are still in the bundle; the worker does not judge
        # which files matter.
        assert "inputs/roads.geojson" in names
    finally:
        server.shutdown()


def test_a_second_job_waits_for_the_first(tmp_path, monkeypatch):
    """Concurrency 1: QGIS gets the instance to itself, so jobs queue."""
    server = _server(tmp_path, monkeypatch, concurrency=1)
    slow = (
        "import sys, time, pathlib\n"
        "print('Running Object Layer')\n"
        "time.sleep(0.6)\n"
        "pathlib.Path(sys.argv[1], 'x.gpkg').write_bytes(b'x')\n"
    )
    try:
        for job in ("p1", "p2"):
            _call(server, f"/jobs/{job}/inputs", data=_zip({}), method="PUT")
        _call(server, "/jobs/p1", method="POST",
              data=json.dumps({"args": ["-c", slow, str(tmp_path / "p1")]}).encode())
        second = _call(server, "/jobs/p2", method="POST",
                       data=json.dumps({"args": ["-c", slow, str(tmp_path / "p2")]}).encode())

        assert second["state"] == "queued"
        deadline = time.time() + 20
        while time.time() < deadline:
            if _call(server, "/jobs/p2")["state"] == "done":
                break
            time.sleep(0.1)
        assert _call(server, "/jobs/p2")["state"] == "done"
    finally:
        server.shutdown()


# ── the engine's side ──────────────────────────────────────────────────────

def test_a_running_job_can_be_stopped(tmp_path, monkeypatch):
    """Stopping a run used to mean restarting the worker, which lost every job."""
    server = _server(tmp_path, monkeypatch)
    slow = (
        "import pathlib, sys, time\n"
        "print('Running Trench Layer')\n"
        "time.sleep(30)\n"
        "pathlib.Path(sys.argv[1], 'should_not_exist.gpkg').write_bytes(b'x')\n"
    )
    try:
        _call(server, "/jobs/p1/inputs", data=_zip({}), method="PUT")
        _call(server, "/jobs/p1", method="POST",
              data=json.dumps({"args": ["-c", slow, str(tmp_path / "p1")]}).encode())
        deadline = time.time() + 20
        while time.time() < deadline and _call(server, "/jobs/p1")["state"] != "running":
            time.sleep(0.05)

        _call(server, "/jobs/p1", method="DELETE")
        deadline = time.time() + 20
        while time.time() < deadline and _call(server, "/jobs/p1")["state"] != "canceled":
            time.sleep(0.1)

        assert _call(server, "/jobs/p1")["state"] == "canceled"
        assert not (tmp_path / "p1" / "should_not_exist.gpkg").exists()
    finally:
        server.shutdown()


def test_a_queued_job_can_be_stopped_before_it_starts(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch, concurrency=1)
    slow = "import sys, time\ntime.sleep(5)\n"
    try:
        for job in ("p1", "p2"):
            _call(server, f"/jobs/{job}/inputs", data=_zip({}), method="PUT")
        _call(server, "/jobs/p1", method="POST",
              data=json.dumps({"args": ["-c", slow]}).encode())
        _call(server, "/jobs/p2", method="POST",
              data=json.dumps({"args": ["-c", slow]}).encode())

        _call(server, "/jobs/p2", method="DELETE")

        assert _call(server, "/jobs/p2")["state"] == "canceled"
    finally:
        server.shutdown()


def test_a_lost_job_fails_the_run_instead_of_polling_forever(tmp_path, monkeypatch):
    """A worker restart empties its job store: "unknown" must end the wait."""
    project = tmp_path / "proj1"
    project.mkdir()
    calls = []

    def fake_call(path, data=None, method="GET", timeout=120):
        if path == "/health":
            return b'{"state": "ok"}'
        calls.append((method, path))
        if method == "PUT" or method == "POST":
            return b"{}"
        return b'{"state": "unknown", "lines": [], "offset": 0}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as exc:
        main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    assert "lost this run" in str(exc.value)
    assert len([c for c in calls if c[0] == "GET"]) == 1


def test_the_stop_endpoint_kills_the_worker_job(monkeypatch):
    killed = []

    def fake_call(path, data=None, method="GET", timeout=120):
        killed.append((method, path))
        return b'{"state": "canceling"}'

    task = main._task("proj-stop")
    task.update({"status": "running", "stage": "Duct Layer", "stage_index": 5})
    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)

    answer = main.stop_project("proj-stop")

    assert killed == [("DELETE", "/jobs/proj-stop")]
    assert answer["worker"]["state"] == "canceling"
    assert any("Stop requested" in m["text"] for m in task["messages"])


def test_the_stop_endpoint_refuses_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    with pytest.raises(main.HTTPException) as exc:
        main.stop_project("never-started")
    assert exc.value.status_code == 404


def test_the_engine_collects_a_worker_run_into_the_same_directory(tmp_path, monkeypatch):
    """End to end: argv in, stdout relayed, outputs unpacked, manifest applied."""
    outputs = tmp_path / "outputs"
    project = outputs / "proj1"
    (project / "inputs").mkdir(parents=True)
    (project / "inputs" / "roads.geojson").write_text("{}", encoding="utf-8")
    # A leftover from a previous run (an hour old, as one would be): it must NOT
    # survive as if this run wrote it.
    stale = project / "Aerial_Spans.gpkg"
    stale.write_bytes(b"old")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    server = _server(outputs, monkeypatch)
    monkeypatch.setattr(main, "QGIS_WORKER_URL",
                        f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(main, "QGIS_WORKER_TOKEN", TOKEN)
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.05)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(main, "_layer_output_names",
                        lambda: {"Final_Trenches.gpkg", "Aerial_Spans.gpkg"})
    try:
        task = main._task("proj1")
        task.update({"status": "running"})
        written = main._run_command(
            "proj1", [sys.executable, "-c", _script(), str(project)], project,
        )

        assert "Final_Trenches.gpkg" in written
        assert (project / "Final_Trenches.gpkg").is_file()
        # Progress was driven by the worker's relayed stdout, not just at the end.
        assert task["stage"] == "Network Layer"
        assert any("Running Object Layer" in m["text"] for m in task["messages"])

        # The previous run's layer is removed because this run did not write it.
        deleted = main._drop_stale_outputs("proj1", project, time.time(), written=written)
        assert "Aerial_Spans.gpkg" in deleted
        assert not (project / "Aerial_Spans.gpkg").exists()
    finally:
        server.shutdown()


def test_an_unreachable_worker_says_so_instead_of_hanging(tmp_path, monkeypatch):
    project = tmp_path / "proj1"
    project.mkdir()
    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(main, "QGIS_WORKER_TOKEN", TOKEN)
    # The real budget is 180 s (a free instance's cold start); the wait itself is
    # exercised in the wake test below, so keep this one fast.
    monkeypatch.setattr(main, "QGIS_WORKER_WAKE_S", 0.2)

    with pytest.raises(RuntimeError) as exc:
        main._run_command("proj1", [sys.executable, "-c", "print(1)"], project)

    assert "QGIS worker" in str(exc.value)
    assert "did not answer within" in str(exc.value)


def test_a_sleeping_worker_is_waited_for_instead_of_failing_the_run(tmp_path, monkeypatch):
    """A free worker answers 502 while it boots; that must not kill the run.

    Measured 2026-10-20 13:20: a run against the sleeping worker was settled as
    failed in 34 s with "HTTP Error 502: Bad Gateway", and the planner lost the
    ward it had just started.
    """
    project = tmp_path / "proj1"
    project.mkdir()
    asleep = {"n": 2}
    seen = []

    def cold(path, data=None, method="GET", timeout=120):
        seen.append((method, path))
        if asleep["n"]:
            asleep["n"] -= 1
            raise urllib.error.HTTPError(
                "http://worker" + path, 502, "Bad Gateway", {}, io.BytesIO(b"")
            )
        if path == "/health":
            return b'{"state": "ok"}'
        if method in ("PUT", "POST"):
            return b"{}"
        return b'{"state": "done", "exit_code": 0, "lines": [], "offset": 0}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", cold)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(main, "_extract_into", lambda body, out: [])

    written = main._run_command_remote(
        "proj1", ["qgis_process", "run", "x"], project
    )

    assert written == []
    # The job was only queued after the worker stopped answering 502.
    assert ("POST", "/jobs/proj1") in seen
    assert seen[0][1] == "/health"
