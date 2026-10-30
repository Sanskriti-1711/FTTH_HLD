"""A tiny HTTP service that runs ``qgis_process`` for the engine API.

Why this exists: the engine and QGIS were sharing one 512 MB container, and the
two peaks did not fit.  Measured on a 3.5 km2 ward on 2026-10-20, the API process
held 126 MB and ``qgis_process`` 443 MB (peak 455) against a 512 MiB cap -- the
container was OOM-killed at the Duct Layer, and the run was reported as "the
engine restarted".  Given a container of its own, QGIS fits.

Deliberately NOT FastAPI/uvicorn: the worker's own footprint comes out of the
same 512 MB QGIS needs, and a stdlib ``http.server`` costs ~20 MB where the web
stack costs ~66.  It also keeps this service dependent on nothing but the
standard library, so it boots in under a second and cannot break when the API's
dependencies change.

Protocol (all job endpoints need ``X-FTTH-Token: $QGIS_WORKER_TOKEN``):

    PUT  /jobs/<id>/inputs   body = zip of the project's output directory
    POST /jobs/<id>          body = {"args": ["run", "hldplanning:...", "--", ...]}
    GET  /jobs/<id>          {"state": running|done|failed, "exit_code": n, ...}
    GET  /jobs/<id>/log?offset=N   {"lines": [...], "offset": M, "state": ...}
    GET  /jobs/<id>/result   zip of the output directory, with a manifest
    GET  /health

The arguments are built by the API, not here: ``BF_DUCTS``, ``PERMIT_DELAYS``,
``POLY_BARRIER_EXTRA`` and friends are absolute paths that resolve on the worker
because both services unpack to the same ``/app/web/backend/outputs/<id>``
layout from the same image.
"""

from __future__ import annotations

import collections
import hmac
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import meminfo

BACKEND_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BACKEND_DIR / "outputs"
# The manifest a finished job carries, so the API can tell the files THIS run
# wrote from whatever a previous run left behind.  mtimes cannot: the two
# services are separate clocks, and a file written a second before the API took
# its start time would look stale and be deleted.
MANIFEST = "._written.json"

TOKEN = os.environ.get("QGIS_WORKER_TOKEN", "")
EXECUTABLE = os.environ.get("QGIS_WORKER_EXECUTABLE", "")
# How long a finished job is kept for a late result fetch (an API restart in the
# middle of a run must still be able to collect the outputs).
JOB_RETENTION_S = int(os.environ.get("QGIS_WORKER_JOB_RETENTION_S", "21600"))
MAX_LINES = int(os.environ.get("QGIS_WORKER_MAX_LINES", "20000"))
# How many of those lines are written to the recovery file. The live log is
# served from memory, so the persisted copy only has to be long enough to
# explain a restart; rewriting all 20,000 every 40 lines was heavy enough to
# stall the worker's own health endpoint.
PERSIST_LINES = int(os.environ.get("QGIS_WORKER_PERSIST_LINES", "2000"))
# One QGIS process at a time.  The whole point of this service is that QGIS gets
# the instance to itself; two concurrent designs would put it straight back over
# the cap.  Jobs therefore QUEUE -- the API polls a queued job exactly like a
# running one, so nothing above this changes.
CONCURRENCY = max(1, int(os.environ.get("QGIS_WORKER_CONCURRENCY", "1")))
# How long a queued or running job may say nothing before `DELETE /jobs?stale=`
# cancels it. One slot means one dead job starves every later run, and the run
# it starves is the one the planner is watching.
STALE_JOB_IDLE_S = int(os.environ.get("QGIS_WORKER_STALE_IDLE_S", "600"))
# The pipeline runs HERE, so every switch that changes its output has to arrive
# with the job: this service's own environment carries only the worker's token,
# and that is why a review enabled on the engine never reviewed anything on a
# worker run.  The list is an allow-list, not a passthrough -- a job body comes
# off the wire and must not be able to set PYTHONPATH or LD_PRELOAD.
ENV_PASSTHROUGH_PREFIXES = ("SURFACE_AI_", "GEMINI_", "GOOGLE_API_", "HLD_", "OSM_")
ENV_PASSTHROUGH_NAMES = frozenset(("GEMINI_API_KEY", "GOOGLE_API_KEY"))


def job_env(raw: Any) -> Dict[str, str]:
    """The subset of a job's requested environment this worker will apply."""
    if not isinstance(raw, dict):
        return {}
    allowed: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not name or "=" in name or value is None:
            continue
        if name in ENV_PASSTHROUGH_NAMES or name.startswith(ENV_PASSTHROUGH_PREFIXES):
            allowed[name] = str(value)
    return allowed

_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
_queue: Deque[str] = collections.deque()
_sched = threading.Lock()
_active = {"running": 0, "dispatcher": False}


# ── jobs ──────────────────────────────────────────────────────────────────

def _qgis() -> str:
    if EXECUTABLE:
        return EXECUTABLE
    found = shutil.which("qgis_process")
    if not found:
        raise RuntimeError("qgis_process was not found on the worker")
    return found


def _job_dir(job_id: str) -> Path:
    # A job id comes off the wire and becomes a directory name, so it must not
    # be able to leave the output root.  Rejected rather than sanitised: two
    # different ids must never map to the same directory.
    safe = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")[:64]
    if not safe or safe != job_id:
        raise ValueError("job id must contain letters, digits, '-' or '_'")
    return OUTPUT_DIR / safe


def _blank_job() -> Dict[str, Any]:
    return {"state": "unknown", "lines": [], "started_at": None,
            "finished_at": None, "exit_code": None, "error": None,
            "pid": None}


def _job(job_id: str, create: bool = True) -> Dict[str, Any]:
    """This job's entry.

    ``create=False`` is for READS. A poll for a job the worker has never seen
    used to ``setdefault`` an "unknown" entry into the store, so every poll for
    a run the worker did not know left a phantom job behind: /health then
    reported ``jobs: 1`` with nothing running (seen 2026-10-30 12:34), and the
    stale sweep would have had to reason about entries that never existed.
    """
    with _lock:
        if job_id in _jobs:
            return _jobs[job_id]
        if not create:
            return _blank_job()
        job = _blank_job()
        _jobs[job_id] = job
        return job


def _state_dir() -> Path:
    return OUTPUT_DIR / ".jobs"


def _state_file(job_id: str) -> Path:
    return _state_dir() / f"{job_id}.json"


def _save_job(job_id: str, job: Dict[str, Any]) -> None:
    """Persist this job, so a replaced instance is not instant amnesia.

    The job store was RAM-only while the INPUTS were already on disk, so when
    Render replaced the instance the next poll answered "unknown" and the run
    died as "the worker restarted and lost this run" -- including jobs that had
    not started yet and could simply have been run.
    """
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "state": job.get("state"),
            "args": job.get("args") or [],
            "exit_code": job.get("exit_code"),
            "error": job.get("error"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "queued_at": job.get("queued_at"),
            "last_line_at": job.get("last_line_at"),
            "lines": (job.get("lines") or [])[-PERSIST_LINES:],
        }
        tmp = _state_file(job_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _state_file(job_id))
    except OSError:
        # Recovery is a nicety; losing it must never fail a job that runs.
        pass


def _load_state() -> int:
    """Reload persisted jobs at boot. Returns how many were restored.

    A job that was RUNNING cannot be resumed -- qgis_process died with the old
    instance -- so it is marked failed with that reason instead of answering
    "unknown", which the engine could only report as a restart. A job that was
    still QUEUED never started, so it is queued again: its inputs are on disk
    and nothing has to be rebuilt.
    """
    directory = _state_dir()
    if not directory.is_dir():
        return 0
    restored = 0
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job_id = str(payload.get("job_id") or path.stem)
        if not job_id or "".join(ch for ch in job_id
                                 if ch.isalnum() or ch in "-_") != job_id:
            continue
        job = _blank_job()
        job.update({key: payload.get(key) for key in
                    ("state", "exit_code", "error", "started_at",
                     "finished_at", "queued_at", "last_line_at")})
        job["lines"] = [str(line) for line in payload.get("lines") or []]
        job["args"] = [str(arg) for arg in payload.get("args") or []]
        if job["state"] == "running":
            job.update({
                "state": "failed",
                "finished_at": time.time(),
                "error": ("The worker instance was replaced while this job was "
                          "running, so qgis_process was lost. Run it again."),
            })
        with _lock:
            _jobs[job_id] = job
        restored += 1
        if (job["state"] == "queued" and job["args"]
                and _job_dir(job_id).is_dir()):
            with _sched:
                if job_id not in _queue:
                    _queue.append(job_id)
                if not _active["dispatcher"]:
                    _active["dispatcher"] = True
                    threading.Thread(target=_dispatch, name="qgis-dispatch",
                                     daemon=True).start()
    return restored


def start_job(job_id: str, args: List[str], env: Any = None) -> Dict[str, Any]:
    """Queue ``qgis_process`` with the API's arguments.  Returns immediately.

    ``env`` is the API's review/feature configuration for THIS run (see
    :func:`job_env`), applied on top of the worker's own environment.
    """
    out_dir = _job_dir(job_id)
    if not out_dir.is_dir():
        raise FileNotFoundError(f"no inputs were uploaded for {job_id}")
    job = _job(job_id)
    with _lock:
        if job["state"] in ("queued", "running"):
            return {"state": job["state"], "job_id": job_id}
        job.update(
            {"state": "queued", "args": list(args), "env": job_env(env),
             "lines": [], "started_at": None,
             "finished_at": None, "exit_code": None, "error": None,
             "queued_at": time.time(), "last_line_at": None}
        )
    _save_job(job_id, job)
    with _sched:
        if job_id not in _queue:
            _queue.append(job_id)
        if not _active["dispatcher"]:
            _active["dispatcher"] = True
            threading.Thread(target=_dispatch, name="qgis-dispatch", daemon=True).start()
    return {"state": "queued", "job_id": job_id}


def _dispatch() -> None:
    """Run queued jobs, ``CONCURRENCY`` at a time, until the queue is empty."""
    while True:
        with _sched:
            while _queue and _active["running"] < CONCURRENCY:
                job_id = _queue.popleft()
                _active["running"] += 1
                job = _job(job_id)
                args = job.get("args") or []
                threading.Thread(
                    target=_run_job, args=(job_id, args, job.get("env") or {}),
                    daemon=True, name=f"qgis-{job_id[:8]}",
                ).start()
            if not _queue and _active["running"] == 0:
                _active["dispatcher"] = False
                return


def _run_job(job_id: str, args: List[str], env: Optional[Dict[str, str]] = None) -> None:
    job = _job(job_id)
    out_dir = _job_dir(job_id)
    with _lock:
        # A cancel can land in the moment between leaving the queue and this
        # thread starting, and setting state="running" unconditionally used to
        # overwrite it: the job then ran for its full duration after DELETE had
        # already answered 200 (seen on a job that started 1 s after the
        # cancel). A canceled job must never start qgis_process.
        canceled_early = (
            job.get("canceled") is True or job.get("state") == "canceled"
        )
        if canceled_early:
            job.update({
                "state": "canceled",
                "finished_at": time.time(),
                "error": job.get("error") or "canceled before it started",
            })
        else:
            job.update({"state": "running", "started_at": time.time(),
                        "last_line_at": time.time()})
    _save_job(job_id, job)
    try:
        if canceled_early:
            return
        _run_qgis(job_id, job, out_dir, args, env or {})
    finally:
        with _sched:
            _active["running"] -= 1


def _open_pty():
    """(master, slave) file descriptors for a pty, or (None, None).

    ``pty`` is POSIX only; a Windows dev box falls back to a plain pipe.
    """
    try:
        import pty

        return pty.openpty()
    except Exception:  # noqa: BLE001 - absence is a supported case
        return None, None


def _pty_lines(master_fd: int):
    """Yield the child's lines as it writes them, ending at EOF.

    A pty reports the closed slave as ``OSError: [Errno 5] Input/output error``
    rather than as a clean end of file, which is why this is a generator and
    not a plain ``readline`` loop.
    """
    with os.fdopen(master_fd, "r", encoding="utf-8", errors="replace", newline="") as stream:
        while True:
            try:
                line = stream.readline()
            except OSError:
                return
            if not line:
                return
            yield line


def _spawn_qgis(args: List[str], env: Dict[str, str]):
    """Start qgis_process and return ``(process, line iterator)``.

    stdout goes through a pty where one exists (the Linux worker): a PIPE makes
    the child's stdio BLOCK buffered, so qgis_process holds up to 4 KB of stage
    output before the job log sees one line. That is indistinguishable from a
    hang -- run 88b7b097 sat at "Duct Layer 55 %" with its last line the stage
    banner while qgis_process was still working, and the engine's stall guard
    failed it as hung. A pty is a terminal, so the child line-buffers and every
    stage line reaches the log as it is written, whatever the stage is doing.
    """
    argv = [_qgis(), *args]
    master_fd, slave_fd = _open_pty()
    if master_fd is not None:
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(BACKEND_DIR),
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=subprocess.STDOUT,
                env=env,
                # Its own session, so cancel_job() can signal the whole tree.
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        return process, _pty_lines(master_fd)
    process = subprocess.Popen(
        argv,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        # Its own session, so cancel_job() can signal the whole tree.
        start_new_session=True,
    )
    return process, iter(process.stdout.readline, "")


def _run_qgis(job_id: str, job: Dict[str, Any], out_dir: Path,
              args: List[str], job_environment: Optional[Dict[str, str]] = None) -> None:
    """Run one job to completion, streaming its stdout into the job log."""
    started = time.time()
    env = os.environ.copy()
    env.update(job_environment or {})
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if job_environment:
        job["lines"].append(
            "[worker] applied %d run setting(s) from the engine: %s"
            % (len(job_environment), ", ".join(sorted(job_environment)))
        )
    last_save = 0.0
    before = _snapshot(out_dir)
    try:
        process, output = _spawn_qgis(args, env)
        job["pid"] = process.pid
        job["process"] = process
        # Let the worker's own HTTP thread win the CPU. This service answers
        # Render's health check from the same small box qgis_process saturates,
        # and when that check cannot open the port Render restarts the instance
        # -- taking every run in flight with it. Raising the child's niceness
        # needs no privilege and costs the pipeline nothing it cannot spare.
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 19)
        except (AttributeError, OSError):
            pass
        meminfo.trace(f"worker job {job_id[:8]} started", pid=process.pid)
        if job.get("canceled"):
            # The stop landed before Popen returned; kill it now rather than
            # let a canceled run keep the only slot.
            cancel_job(job_id)
        for line in output:
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            with _lock:
                job["lines"].append(text)
                job["last_line_at"] = time.time()
                if len(job["lines"]) > MAX_LINES:
                    del job["lines"][: len(job["lines"]) - MAX_LINES]
            # By TIME, not by line count: a burst of output used to rewrite the
            # whole job file every 40 lines, and on a 0.1-CPU worker that starved
            # the HTTP thread long enough for the health check to fail ("dial tcp
            # ...:10000: i/o timeout"), which restarted the instance and killed
            # the run.
            now = time.time()
            if now - last_save >= 5.0:
                last_save = now
                _save_job(job_id, job)
        try:
            close = getattr(output, "close", None)
            if close is not None:
                close()
        except Exception:  # noqa: BLE001 - the child already ended
            pass
        code = process.wait()
        with _lock:
            job["exit_code"] = code
            if job.get("canceled"):
                job["state"] = "canceled"
                job["error"] = "stopped on request"
            else:
                job["state"] = "done" if code == 0 else "failed"
            job["finished_at"] = time.time()
            # The manifest is written HERE, not by the API: only the worker
            # knows which files this run touched.
            job["written"] = _written_between(before, _snapshot(out_dir))
        _save_job(job_id, job)
        meminfo.trace(
            f"worker job {job_id[:8]} ended", exit_code=code,
            seconds=round(time.time() - started, 1),
        )
    except Exception as exc:  # noqa: BLE001 - reported to the API
        with _lock:
            job["state"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = time.time()
        _save_job(job_id, job)
        meminfo.trace(f"worker job {job_id[:8]} failed", error=str(exc)[:120])


def _idle_seconds(job: Dict[str, Any]) -> float:
    """Seconds since this job last produced a line (or entered its state).

    ``last_line_at`` is the honest clock: a job that is still running but has
    written nothing for twenty minutes is wedged, and one that never started has
    only its queue time to measure. -1 means "no clock at all", which is never
    treated as stale -- an unknown age must not delete work.
    """
    anchor = job.get("last_line_at") or job.get("started_at") or job.get("queued_at")
    if anchor is None:
        return -1.0
    return max(0.0, time.time() - anchor)


def job_list() -> Dict[str, Any]:
    """Every job this worker holds, oldest silence first.

    ``/health`` reports a count, and a count cannot tell a deadlocked slot from
    finished history: on 2026-10-30 the worker said ``jobs: 2`` while one run sat
    at 0 % for sixteen minutes. This answers which job is which, how long it has
    been silent, and whether it is queued or running.
    """
    with _lock:
        rows = [
            {
                "job_id": job_id,
                "state": job["state"],
                "exit_code": job.get("exit_code"),
                "error": job.get("error"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "lines": len(job.get("lines") or []),
                "idle_s": round(_idle_seconds(job), 1),
            }
            for job_id, job in _jobs.items()
        ]
        queued = list(_queue)
    rows.sort(key=lambda row: row["idle_s"], reverse=True)
    return {"jobs": rows, "queue": queued, "running": _active["running"],
            "concurrency": CONCURRENCY, "stale_idle_s": STALE_JOB_IDLE_S}


def cancel_stale_jobs(max_idle_s: float) -> Dict[str, Any]:
    """Cancel queued/running jobs that have been silent past ``max_idle_s``.

    The alternative -- and what was done on 2026-10-30 -- is restarting the
    worker, which throws away every other job's state with it. Cancelling is
    surgical: a job that is making progress keeps its slot, and one that is not
    stops holding everyone else's.
    """
    canceled: List[Dict[str, Any]] = []
    with _lock:
        candidates = [(job_id, job) for job_id, job in _jobs.items()
                      if job["state"] in ("queued", "running")]
        stale = [(job_id, _idle_seconds(job)) for job_id, job in candidates
                 if _idle_seconds(job) > max_idle_s]
    for job_id, idle in stale:
        try:
            answer = cancel_job(job_id)
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            answer = {"state": "failed", "error": str(exc)}
        canceled.append({"job_id": job_id, "idle_s": round(idle, 1), **answer})
    return {"max_idle_s": max_idle_s, "canceled": canceled}


def cancel_job(job_id: str) -> Dict[str, Any]:
    """Stop a queued or running job.

    A run that has to be stopped (a stuck stage, a wrong area) previously could
    only be ended by restarting the worker, which threw away every other job's
    state with it.
    """
    job = _job(job_id)
    if job["state"] == "queued":
        with _sched:
            try:
                _queue.remove(job_id)
            except ValueError:
                pass
        with _lock:
            job.update({"state": "canceled", "finished_at": time.time(),
                        "error": "canceled before it started"})
        _save_job(job_id, job)
        return {"state": "canceled", "job_id": job_id}
    process = job.get("process")
    if job["state"] == "running" and process is None:
        # Between "running" and Popen returning there is a window where a cancel
        # used to be dropped on the floor: the DELETE answered 200 and the run
        # carried on for its whole duration. The flag is read the moment the
        # process exists (see _run_qgis), so setting it here is enough.
        with _lock:
            job["canceled"] = True
        _save_job(job_id, job)
        return {"state": "canceling", "job_id": job_id}
    if job["state"] == "running" and process is not None:
        with _lock:
            job["canceled"] = True
        _save_job(job_id, job)
        try:
            if os.name == "nt":
                process.terminate()
            else:
                # The child runs in its own session: qgis_process starts
                # workers of its own, and killing only the parent leaves them
                # writing outputs.
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        return {"state": "canceling", "job_id": job_id}
    return {"state": job["state"], "job_id": job_id}


def _snapshot(out_dir: Path) -> Dict[str, int]:
    """Every file under the job directory, keyed by relative path."""
    files: Dict[str, int] = {}
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.name == MANIFEST:
            continue
        try:
            files[path.relative_to(out_dir).as_posix()] = path.stat().st_mtime_ns
        except OSError:
            continue
    return files


def _written_between(before: Dict[str, int], after: Dict[str, int]) -> List[str]:
    """Files this run created or rewrote.

    A mtime cutoff cannot answer this: the inputs are extracted seconds before
    the job starts, so "newer than the start" includes every input file.  The
    difference between two snapshots is exact, and it is what the API needs to
    tell this run's layers from the previous run's.
    """
    return sorted(
        name for name, stamp in after.items() if before.get(name) != stamp
    )


# ── inputs / results ──────────────────────────────────────────────────────

def unpack_inputs(job_id: str, payload: bytes) -> Dict[str, Any]:
    """Replace the job's output directory with the uploaded archive."""
    out_dir = _job_dir(job_id)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in archive: {name}")
        archive.extractall(out_dir)
    with _lock:
        _jobs[job_id] = {
            "state": "uploaded", "lines": [], "started_at": None,
            "finished_at": None, "exit_code": None, "error": None, "pid": None,
        }
    return {"state": "uploaded", "job_id": job_id}


def result_zip(job_id: str) -> bytes:
    """The job's output directory as a zip, with the written-file manifest."""
    job = _job(job_id)
    if job["state"] not in ("done", "failed"):
        raise RuntimeError(f"job {job_id} is {job['state']}, not finished")
    out_dir = _job_dir(job_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(out_dir.rglob("*")):
            if not path.is_file() or path.name == MANIFEST:
                continue
            archive.write(path, path.relative_to(out_dir).as_posix())
        archive.writestr(MANIFEST, json.dumps(job.get("written") or []))
    return buffer.getvalue()


def _sweep_old_jobs() -> None:
    now = time.time()
    with _lock:
        stale = [
            key for key, job in _jobs.items()
            if job.get("finished_at") and now - job["finished_at"] > JOB_RETENTION_S
        ]
        for key in stale:
            _jobs.pop(key, None)


# ── HTTP ──────────────────────────────────────────────────────────────────

def _child_memory() -> Any:
    """The running qgis_process's memory, or None when no job is running.

    The worker's own RSS is a rounding error beside the child's: /health used to
    report 25 MB while the design was actually filling the container, so the one
    number readable during a run was the wrong one. The child's arenas cannot be
    released from out here -- this reports it so the ramp is visible before the
    kernel decides what to do about it.
    """
    with _lock:
        pid = next(
            (job.get("pid") for job in _jobs.values()
             if job.get("state") == "running" and job.get("pid")),
            None,
        )
    if not pid:
        return None
    return {
        "pid": pid,
        "rss_mb": meminfo.rss_mb(pid),
        "peak_rss_mb": meminfo.peak_rss_mb(pid),
        "limit_mb": meminfo.limit_mb(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ftth-qgis-worker/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # One line per request is all the platform log needs; the default adds
        # the client address, which is only noise for two services talking.
        sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- helpers --
    def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not TOKEN:
            self._send(503, {"detail": "QGIS_WORKER_TOKEN is not set on the worker"})
            return False
        given = self.headers.get("X-FTTH-Token") or ""
        if not hmac.compare_digest(given, TOKEN):
            self._send(401, {"detail": "bad worker token"})
            return False
        return True

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["health"]:
            self._send(200, {
                "status": "ok",
                "memory": meminfo.report(),
                "child": _child_memory(),
                "jobs": len(_jobs),
                "executable": EXECUTABLE or shutil.which("qgis_process") or None,
            })
            return
        if parts == ["jobs"]:
            if not self._authorised():
                return
            self._send(200, job_list())
            return
        if len(parts) >= 2 and parts[0] == "jobs":
            if not self._authorised():
                return
            job_id = parts[1]
            if len(parts) == 3 and parts[2] == "log":
                offset = int((parse_qs(parsed.query).get("offset") or ["0"])[0] or 0)
                job = _job(job_id, create=False)
                with _lock:
                    lines = job["lines"][offset:]
                    offset = offset + len(lines)
                    state = job["state"]
                # The memory rides along so the engine can log the ramp without a
                # second request every poll -- it already asks for this line.
                self._send(200, {"lines": lines, "offset": offset, "state": state,
                                 "memory": meminfo.report(), "child": _child_memory()})
                return
            if len(parts) == 3 and parts[2] == "result":
                try:
                    self._send(200, result_zip(job_id), "application/zip")
                except (RuntimeError, FileNotFoundError) as exc:
                    self._send(409, {"detail": str(exc)})
                return
            job = _job(job_id, create=False)
            self._send(200, {
                "job_id": job_id, "state": job["state"],
                "exit_code": job["exit_code"], "error": job["error"],
                "started_at": job["started_at"], "finished_at": job["finished_at"],
                "lines": len(job["lines"]),
                "memory": meminfo.report(),
            })
            return
        self._send(404, {"detail": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "inputs":
            if not self._authorised():
                return
            try:
                self._send(200, unpack_inputs(parts[1], self._body()))
            except (ValueError, zipfile.BadZipFile) as exc:
                self._send(400, {"detail": str(exc)})
            return
        self._send(404, {"detail": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts == ["jobs"]:
            if not self._authorised():
                return
            query = parse_qs(urlparse(self.path).query)
            raw = (query.get("stale") or query.get("older_than")
                   or [str(STALE_JOB_IDLE_S)])[0]
            try:
                max_idle = float(raw)
            except (TypeError, ValueError):
                self._send(400, {"detail": "stale must be a number of seconds"})
                return
            self._send(200, cancel_stale_jobs(max_idle))
            return
        if len(parts) == 2 and parts[0] == "jobs":
            if not self._authorised():
                return
            try:
                self._send(200, cancel_job(parts[1]))
            except Exception as exc:  # noqa: BLE001 - never drop the connection
                self._send(500, {"detail": f"could not stop the job: {exc}"})
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "jobs":
            if not self._authorised():
                return
            try:
                payload = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"detail": "body must be JSON"})
                return
            args = payload.get("args")
            if not isinstance(args, list) or not args:
                self._send(400, {"detail": "'args' must be a non-empty list"})
                return
            try:
                self._send(202, start_job(parts[1], [str(a) for a in args],
                                          env=payload.get("env")))
            except FileNotFoundError as exc:
                self._send(409, {"detail": str(exc)})
            except RuntimeError as exc:
                self._send(500, {"detail": str(exc)})
            _sweep_old_jobs()
            return
        self._send(404, {"detail": "not found"})


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    restored = _load_state()
    if restored:
        print(f"qgis worker restored {restored} job(s) from disk", flush=True)
    port = int(os.environ.get("PORT") or os.environ.get("QGIS_WORKER_PORT") or "8000")
    meminfo.trace("worker starting", port=port)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"qgis worker listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
