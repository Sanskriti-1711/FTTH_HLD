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


@pytest.fixture(autouse=True)
def _fresh_worker_wake_state():
    """Every test starts with no warm worker and no shared wake flight."""
    main._worker_ready_until = 0.0
    main._worker_flight = None
    yield
    main._worker_ready_until = 0.0
    main._worker_flight = None


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
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.02)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as exc:
        main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    # A job the worker never learned is re-submitted ONCE (a POST that never
    # landed looks identical to a lost job), and then the run fails instead of
    # polling for ever.
    assert "lost this run" in str(exc.value)
    assert len([c for c in calls if c[0] == "POST"]) >= 2


def test_a_job_that_stops_logging_fails_the_run_instead_of_hanging(
        tmp_path, monkeypatch):
    """A wedged qgis_process keeps its job "running" for ever.

    Run a7499f9d (2026-10-30) sat at "Chamber Layer 44 %" for 50 minutes with a
    healthy engine, a healthy worker and one registered job: no terminal state,
    no exception, nothing in the log to poll. The only exit was
    QGIS_PROCESS_TIMEOUT (10800 s), so the planner would have waited three
    hours to learn the run had wedged. Silence past QGIS_STALL is now a failure
    that names the stage it died in.
    """
    project = tmp_path / "proj1"
    project.mkdir()
    polls = {"n": 0}

    def fake_call(path, data=None, method="GET", timeout=120):
        if path == "/health":
            return b'{"state": "ok"}'
        if method in ("PUT", "POST"):
            return b"{}"
        polls["n"] += 1
        if polls["n"] == 1:
            # One stage line, then the wedge: the job never says another word.
            return (b'{"state": "running", "offset": 40, '
                    b'"lines": ["[75%] Running Chamber Layer"]}')
        return b'{"state": "running", "lines": [], "offset": 40}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.02)
    monkeypatch.setattr(main, "QGIS_STALL_S", 0.2)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    text = str(exc.value)
    assert "stopped reporting progress" in text
    # The stage it died in is named, so the log says WHERE it hung.
    assert "Chamber Layer" in text
    # It fails on silence, not by waiting out the three-hour ceiling.
    assert time.monotonic() - started < 30


def test_a_job_that_never_starts_fails_fast_instead_of_sitting_at_zero(
        tmp_path, monkeypatch):
    """A run queued behind a wedged job never reaches qgis_process at all.

    Run ca6c9752 (2026-10-30) sat at "Object Layer 0 %" for 16 minutes while the
    worker held two jobs and one slot. The job was accepted -- so the engine
    logged "Running on the QGIS worker" -- and then not one line was written,
    not even the worker's own first "[worker] applied N run setting(s)" line.
    The stall guard above cannot see that: it measures silence from a RUNNING
    job, so a job that never starts had no exit short of the three-hour ceiling.
    """
    project = tmp_path / "proj1"
    project.mkdir()

    def fake_call(path, data=None, method="GET", timeout=120):
        if path == "/health":
            return b'{"state": "ok"}'
        if method in ("PUT", "POST"):
            return b"{}"
        return b'{"state": "queued", "lines": [], "offset": 0}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.02)
    monkeypatch.setattr(main, "QGIS_WORKER_START_S", 0.2)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    text = str(exc.value)
    # It names the failure and the state the job was stuck in.
    assert "never started qgis_process" in text
    assert "queued" in text
    # Fast, not the three-hour ceiling.
    assert time.monotonic() - started < 30


def test_a_job_that_keeps_logging_is_not_treated_as_hung(tmp_path, monkeypatch):
    """Any new line resets the detector: a slow stage that reports is not hung."""
    project = tmp_path / "proj1"
    project.mkdir()
    polls = {"n": 0}

    def fake_call(path, data=None, method="GET", timeout=120):
        if path == "/health":
            return b'{"state": "ok"}'
        if method in ("PUT", "POST"):
            return b"{}"
        if path.endswith("/result"):
            return b""
        polls["n"] += 1
        if polls["n"] >= 12:
            return b'{"state": "done", "exit_code": 0, "lines": [], "offset": 400}'
        return json.dumps({
            "state": "running",
            "lines": ["  [chamber] rule %d working" % polls["n"]],
            "offset": 40 + polls["n"] * 10,
        }).encode()

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.02)
    # Shorter than the whole run takes, longer than a single poll's gap.
    monkeypatch.setattr(main, "QGIS_STALL_S", 0.2)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(main, "_extract_into", lambda body, out: [])

    written = main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    assert written == []
    assert polls["n"] >= 12


def _seed(job_id, state, *, idle_s, lines=()):
    """A job entry as the worker would hold it, ``idle_s`` seconds quiet."""
    now = time.time()
    worker._jobs[job_id] = {
        "state": state,
        "lines": list(lines),
        "started_at": now - idle_s if state == "running" else None,
        "finished_at": None,
        "exit_code": None,
        "error": None,
        "pid": 1 if state == "running" else None,
        "queued_at": now - idle_s,
        "last_line_at": now - idle_s if state == "running" else None,
    }


def test_the_worker_lists_its_jobs_so_a_held_slot_is_visible(tmp_path, monkeypatch):
    """A count cannot tell a deadlocked slot from finished history.

    While run ca6c9752 (2026-10-30) sat at 0 % for sixteen minutes, /health said
    only ``jobs: 2``. Clearing it meant guessing which job to blame. The list
    names the job, its state and how long it has been silent.
    """
    server = _server(tmp_path, monkeypatch)
    _seed("wedged", "running", idle_s=1200, lines=["[75%] Running Chamber Layer"])
    _seed("fresh", "queued", idle_s=5)

    listing = _call(server, "/jobs")

    assert listing["concurrency"] == 1
    assert listing["queue"] == []
    rows = {row["job_id"]: row for row in listing["jobs"]}
    assert rows["wedged"]["state"] == "running"
    assert rows["wedged"]["idle_s"] > 1000
    assert rows["fresh"]["state"] == "queued"
    # Oldest silence first: the wedged job is the one to look at.
    assert listing["jobs"][0]["job_id"] == "wedged"


def test_stale_jobs_are_canceled_without_restarting_the_worker(tmp_path, monkeypatch):
    """The held slot is cleared in place, so other jobs keep their state."""
    server = _server(tmp_path, monkeypatch)
    _seed("old", "queued", idle_s=3600)
    _seed("new", "queued", idle_s=5)
    worker._queue.extend(["old", "new"])

    answer = _call(server, "/jobs?stale=600", method="DELETE")

    assert [row["job_id"] for row in answer["canceled"]] == ["old"]
    assert worker._jobs["old"]["state"] == "canceled"
    assert worker._jobs["new"]["state"] == "queued"
    assert list(worker._queue) == ["new"]


def test_a_job_that_is_still_logging_is_never_canceled(tmp_path, monkeypatch):
    """A slow stage that reports is working, not stale -- the sweep must say so."""
    server = _server(tmp_path, monkeypatch)
    _seed("busy", "running", idle_s=5, lines=["[design] backbone: 33/33 PDP(s)"])
    worker._queue.clear()

    answer = _call(server, "/jobs?stale=600", method="DELETE")

    assert answer["canceled"] == []
    assert worker._jobs["busy"]["state"] == "running"


def test_polling_an_unknown_job_does_not_invent_it(tmp_path, monkeypatch):
    """A read must not create a job.

    The worker answered ``jobs: 1`` on 2026-10-30 with nothing running, because
    every poll for an id it had not seen wrote an "unknown" entry into the store.
    """
    server = _server(tmp_path, monkeypatch)

    answer = _call(server, "/jobs/never-seen")

    assert answer["state"] == "unknown"
    assert _call(server, "/health")["jobs"] == 0


def test_a_queued_job_survives_an_instance_replacement(tmp_path, monkeypatch):
    """The inputs are on disk, so a queued job should not die with the process."""
    server = _server(tmp_path, monkeypatch)
    job = "p-restore"
    (tmp_path / job / "inputs").mkdir(parents=True)
    worker._jobs[job] = worker._blank_job()
    worker._jobs[job].update({
        "state": "queued",
        "args": ["-c", _script(), str(tmp_path / job)],
        "queued_at": time.time(),
    })
    worker._save_job(job, worker._jobs[job])

    # The replacement: the store is empty, because it lived in RAM.
    worker._jobs.clear()
    worker._queue.clear()
    worker._active.update({"running": 0, "dispatcher": False})

    assert worker._load_state() == 1

    deadline = time.time() + 30
    while time.time() < deadline:
        if _call(server, f"/jobs/{job}")["state"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert _call(server, f"/jobs/{job}")["state"] == "done"


def test_a_job_that_was_running_says_the_instance_was_replaced(tmp_path, monkeypatch):
    """It cannot be resumed -- qgis_process died with the old instance -- but it
    must NOT answer "unknown", which the engine reports as a bare restart."""
    _server(tmp_path, monkeypatch)
    job = "p-lost"
    (tmp_path / job / "inputs").mkdir(parents=True)
    worker._jobs[job] = worker._blank_job()
    worker._jobs[job].update({"state": "running", "args": ["-c", "pass"],
                              "started_at": time.time() - 60,
                              "last_line_at": time.time() - 60})
    worker._save_job(job, worker._jobs[job])
    worker._jobs.clear()

    worker._load_state()

    restored = worker._jobs[job]
    assert restored["state"] == "failed"
    assert "replaced" in restored["error"]


def test_an_unseen_job_is_submitted_again_instead_of_failing(tmp_path, monkeypatch):
    """Run a7499f9d failed at 0 % with "the worker restarted" and no restart.

    The worker answers "unknown" for an id whose POST never landed, so the
    engine must re-submit once before believing the run is lost.
    """
    project = tmp_path / "proj1"
    project.mkdir()
    posts = {"n": 0}
    polls = {"n": 0}

    def fake_call(path, data=None, method="GET", timeout=120):
        if path == "/health":
            return b'{"state": "ok"}'
        if method in ("PUT", "POST"):
            if method == "POST":
                posts["n"] += 1
            return b"{}"
        if path.endswith("/result"):
            return b""
        polls["n"] += 1
        if polls["n"] == 1:
            return b'{"state": "unknown", "lines": [], "offset": 0}'
        if polls["n"] < 4:
            return b'{"state": "running", "lines": ["[design] working"], "offset": 10}'
        return b'{"state": "done", "exit_code": 0, "lines": [], "offset": 20}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", fake_call)
    monkeypatch.setattr(main, "QGIS_WORKER_POLL_S", 0.02)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(main, "_extract_into", lambda body, out: [])

    written = main._run_command_remote("proj1", ["qgis_process", "run", "x"], project)

    assert written == []
    assert posts["n"] >= 2


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
    monkeypatch.setattr(main, "QGIS_WORKER_RETRY_S", 0.01)
    monkeypatch.setattr(main, "QGIS_WORKER_MAX_RETRY_S", 0.05)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    monkeypatch.setattr(main, "_extract_into", lambda body, out: [])

    written = main._run_command_remote(
        "proj1", ["qgis_process", "run", "x"], project
    )

    assert written == []
    # The job was only queued after the worker stopped answering 502.
    assert ("POST", "/jobs/proj1") in seen
    assert seen[0][1] == "/health"


# ── the run's settings travel with the job ──────────────────────────────────

def _env_script():
    """A stand-in for qgis_process that prints the settings it was given."""
    return (
        "import os\n"
        "print('SURFACE_AI_REVIEW=' + os.environ.get('SURFACE_AI_REVIEW', 'unset'))\n"
        "print('GEMINI_API_KEY=' + os.environ.get('GEMINI_API_KEY', 'unset'))\n"
        "print('PYTHONPATH=' + os.environ.get('PYTHONPATH', 'unset'))\n"
        "print('Running Object Layer')\n"
    )


def test_the_run_settings_reach_the_qgis_process(tmp_path, monkeypatch):
    """The pipeline runs on the worker, so the review flag has to travel with it.

    The worker's own environment carries its token and nothing else, which is
    why a review enabled on the engine produced no report on a worker run.
    """
    monkeypatch.setenv("PYTHONPATH", "worker-only")
    server = _server(tmp_path, monkeypatch)
    try:
        _call(server, "/jobs/p1/inputs", data=_zip({"inputs/roads.geojson": b"{}"}),
              method="PUT")
        _call(
            server, "/jobs/p1", method="POST",
            data=json.dumps({
                "args": ["-c", _env_script(), str(tmp_path / "p1")],
                "env": {
                    "SURFACE_AI_REVIEW": "1",
                    "SURFACE_AI_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "test-key",
                    # Not on the allow-list: a job body comes off the wire.
                    "PYTHONPATH": "/evil",
                    "LD_PRELOAD": "/evil.so",
                },
            }).encode(),
        )

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
        assert "SURFACE_AI_REVIEW=1" in lines
        assert "GEMINI_API_KEY=test-key" in lines
        # The worker's own environment is not replaced by the job's.
        assert "PYTHONPATH=worker-only" in lines
        assert any("applied 3 run setting(s)" in line for line in lines)
    finally:
        server.shutdown()


def test_only_the_allow_listed_settings_are_accepted():
    accepted = worker.job_env({
        "SURFACE_AI_REVIEW": "1",
        "HLD_MEMORY_SAMPLE_SECONDS": "5",
        "OSM_PAVEMENT_ARTERIALS": "x",
        "GEMINI_API_KEY": "k",
        "PYTHONPATH": "/evil",
        "BASH_ENV": "/evil",
        "BAD=NAME": "x",
        "IGNORED": None,
    })
    assert set(accepted) == {
        "SURFACE_AI_REVIEW", "HLD_MEMORY_SAMPLE_SECONDS",
        "OSM_PAVEMENT_ARTERIALS", "GEMINI_API_KEY",
    }
    assert worker.job_env(None) == {}
    assert worker.job_env("SURFACE_AI_REVIEW=1") == {}


def test_the_engine_forwards_its_review_settings(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "engine-key")

    env = main._worker_env()

    assert env["SURFACE_AI_REVIEW"] == "1"
    assert env["SURFACE_AI_PROVIDER"] == "gemini"
    assert env["GEMINI_API_KEY"] == "engine-key"
    assert "PATH" not in env


def test_a_run_warms_the_worker_before_it_is_needed(monkeypatch):
    """The boot overlaps the run, instead of being paid when the job is queued."""
    probed = []

    def fake_wake(budget=None, project_id=""):
        probed.append((budget, project_id))

    task = main._task("proj-warm")
    task.update({"messages": []})
    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_wake", fake_wake)

    main.wake_worker("proj-warm")
    for _ in range(100):
        if probed:
            break
        time.sleep(0.05)

    assert probed, "the worker was never woken ahead of the pipeline"
    assert any("QGIS worker ready" in m["text"] for m in task["messages"])


def test_resolving_an_area_warms_the_worker_before_run(monkeypatch):
    """The preview a planner reads is when the boot should start, not Run.

    Resolving an area is minutes of Nominatim and OSM download, and the worker's
    cold start is minutes too.  Warming at Run time put both in series: the
    2026-10-22 run logged ``QGIS worker ready (415s)`` with nothing running.
    """
    order = []

    def fake_preview(area, **kwargs):
        order.append("preview")
        return {"area": area, "bbox": []}

    monkeypatch.setattr(main.osm_source, "preview_area", fake_preview)
    monkeypatch.setattr(main.osm_source, "ensure_area_data_background",
                        lambda area, bbox: {"state": "queued"})
    monkeypatch.setattr(main, "wake_worker",
                        lambda project_id="": order.append("wake"))

    main.resolve_area_endpoint({"area": "B16 8UU"})

    assert order[:2] == ["wake", "preview"], (
        "the worker must start booting before the preview does its work, "
        f"so the two waits overlap; got {order}"
    )


# ── the wake: one stream, a real backoff, and 429 is not a boot ──────────────

def _http_error(path, code, headers=None):
    return urllib.error.HTTPError(
        "http://worker" + path, code, "error", headers or {}, io.BytesIO(b"")
    )


def test_a_rate_limited_worker_backs_off_and_recovers(monkeypatch):
    """A 429 is retried after Retry-After, not three seconds later."""
    stamps = []

    def probe(path, data=None, method="GET", timeout=120):
        stamps.append(time.monotonic())
        if len(stamps) <= 2:
            raise _http_error(path, 429, {"Retry-After": "0.05"})
        return b'{"status": "ok"}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", probe)
    monkeypatch.setattr(main, "QGIS_WORKER_WAKE_S", 2.0)
    monkeypatch.setattr(main, "QGIS_WORKER_RETRY_S", 0.05)
    monkeypatch.setattr(main, "QGIS_WORKER_MAX_RETRY_S", 0.1)

    main._worker_wake()

    assert len(stamps) == 3
    assert stamps[1] - stamps[0] >= 0.04
    assert stamps[2] - stamps[1] >= 0.04


def test_a_rate_limit_is_reported_as_rate_limiting_not_sleep(monkeypatch):
    def probe(path, data=None, method="GET", timeout=120):
        raise _http_error(path, 429, {"Retry-After": "0.02"})

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", probe)
    monkeypatch.setattr(main, "QGIS_WORKER_WAKE_S", 0.3)
    monkeypatch.setattr(main, "QGIS_WORKER_RETRY_S", 0.02)
    monkeypatch.setattr(main, "QGIS_WORKER_MAX_RETRY_S", 0.02)

    with pytest.raises(RuntimeError) as exc:
        main._worker_wake()

    text = str(exc.value)
    assert "HTTP 429" in text
    assert "rate limiting" in text
    assert "sleeping" not in text


def test_a_rejected_worker_fails_fast_instead_of_waiting(monkeypatch):
    calls = []

    def probe(path, data=None, method="GET", timeout=120):
        calls.append(path)
        raise _http_error(path, 401)

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", probe)
    monkeypatch.setattr(main, "QGIS_WORKER_WAKE_S", 60)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        main._worker_wake()

    assert time.monotonic() - started < 1
    assert len(calls) == 1
    assert "QGIS_WORKER_TOKEN" in str(exc.value)


def test_two_callers_share_one_probe_stream(monkeypatch):
    """The accepted run and the job start must not probe twice."""
    calls = []

    def probe(path, data=None, method="GET", timeout=120):
        calls.append(time.monotonic())
        time.sleep(0.05)
        return b'{"status": "ok"}'

    monkeypatch.setattr(main, "QGIS_WORKER_URL", "http://worker")
    monkeypatch.setattr(main, "_worker_call", probe)

    callers = [threading.Thread(target=main._worker_wake) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(2)

    assert len(calls) == 1
    assert not any(caller.is_alive() for caller in callers)
