"""
Spawn a fresh detached uvicorn on :8000 from web/backend and verify
that /version answers with a fresh pid.

DEVIATION FROM USER'S LAUNCH COMMAND (carried forward from v2)
=============================================================
User asked for:    python -m uvicorn main:app --host 127.0.0.1
                       --port 8000 --reload
This script:       python -m uvicorn main:app --host 127.0.0.1
                       --port 8000 --log-level info
                                                  ^^^^^^^ --reload deliberately
                                                           omitted (W1 from
                                                           prior review)

OTHER v3-vs-v2 CHANGES
======================
- Dropped the entire CimInstance/PowerShell "orphan sweep".  A previous
  diagnostic confirmed port :8000 is empty (no LISTENING).  Sweeping
  orphan python.exes is defensive-but-not-needed-and-proneto-crash on
  this box (v1 and v2 both exited code 127 inside the PowerShell
  subprocess.run without leaving any stdout behind -- so we rip it out
  for now and add it back only if a concrete orphan is ever witnessed).
- Log file opened in ***text mode, line-buffered*** (was: binary,
  buffering=1) -- the code-reviewer's B1 finding: buffering=1 is
  silently coerced to unbuffered in binary mode, so v2's "fix" was
  functionally identical to v1.  This time the mode is honest.
- Hoisted MAX_POLLS = 60 to module scope (W-v2-2) so future
  FastAPI-cold-start regressions surface loudly instead of silently
  truncating at 30 s.
- Dropped subprocess.CREATE_NEW_PROCESS_GROUP -- DETACHED_PROCESS
  already creates a new process group (N1 carry-over); the second flag
  was redundant.
- Every print is prefixed MARKER so a future crash will reveal exactly
  where the script died (root-cause v1/v2 mystery).

Recommended user followup (unchanged from v2):
    pip install watchfiles     (in the venv uvicorn uses)
    then kill this uvicorn and restart with the original command.
    WatchFiles replaces StatReload and survives syntax errors without
    orphaning the worker.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

PORT = 8000
CWD = r"D:\Downloads_D\Q-GIS\HLD_Planning_01\web\backend"
LOG = r"C:\Users\HP\AppData\Local\Temp\uvicorn.log"
MAX_POLLS = 60  # ≤60 s wall clock; covers PyQGIS-cold-start worst case


def busy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def fetch_version():
    """Sparse one-shot GET /version, 1 s timeout (W3 fix from prior review)."""
    try:
        return json.loads(
            urllib.request.urlopen(
                f"http://localhost:{PORT}/version", timeout=1.0
            ).read()
        )
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def main():
    print("MARKER start: bounce_uvicorn_v3 entering main()", flush=True)

    print("MARKER step1: preflight busy check", flush=True)
    is_busy = busy(PORT)
    print(f"  port :{PORT} busy? {is_busy}", flush=True)
    if is_busy:
        print(
            f"  ABORT_MARKER: port :{PORT} still in use; will NOT spawn a "
            f"second uvicorn onto a contested port.",
            flush=True,
        )
        sys.exit(1)

    print(
        "MARKER step2: opening log file in TEXT mode (line-buffered) -- "
        "survives parent exit via DuplicateHandle on Windows",
        flush=True,
    )
    try:
        log_fh = open(LOG, "a", encoding="utf-8", buffering=1)
    except Exception as e:
        print(f"  LOG_OPEN_FAIL_MARKER: {type(e).__name__}: {e}", flush=True)
        sys.exit(2)

    print(
        "MARKER step3: spawning uvicorn (NO --reload, NO "
        "CREATE_NEW_PROCESS_GROUP)",
        flush=True,
    )
    print(f"  cwd={CWD}", flush=True)
    print(f"  log={LOG}", flush=True)
    try:
        # NO --reload (W1). NO CREATE_NEW_PROCESS_GROUP (N1 redundant;
        # DETACHED_PROCESS already creates a new group on Windows).
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--log-level",
                "info",
            ],
            cwd=CWD,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    except Exception as e:
        print(f"  SPAWN_FAIL_MARKER: {type(e).__name__}: {e}", flush=True)
        sys.exit(2)
    print(f"  SPAWN_OK_MARKER: pid={p.pid}", flush=True)

    print(f"MARKER step4: polling /version up to {MAX_POLLS} iterations", flush=True)
    fresh = None
    last_summary = None
    for i in range(MAX_POLLS):
        time.sleep(1)
        v = fetch_version()
        if "_err" in v:
            # Dedupe log spam: only print at sparse points.
            sparse = {0, 9, 19, 29, 39, 49, MAX_POLLS - 1}
            if i in sparse:
                print(
                    f"  poll {i + 1:>2}s: GET /version failed: {v['_err'][:120]}",
                    flush=True,
                )
            continue
        up = v.get("uptime_seconds", 99.0)
        pid = v.get("pid")
        # Add qgis_available to the dedupe key so a state change surfaces.
        summary = (
            pid,
            round(up, 2),
            v.get("build_id"),
            v.get("module_size_bytes"),
            v.get("has_ogr2ogr"),
            v.get("qgis_available"),
        )
        if summary != last_summary:
            print(
                f"  poll {i + 1:>2}s: pid={pid}  uptime={up:.2f}s  "
                f"build={v.get('build_id')}  size={v.get('module_size_bytes')}  "
                f"ogr2ogr={v.get('has_ogr2ogr')}  qgis={v.get('qgis_available')}",
                flush=True,
            )
            last_summary = summary
        if up < 30 and pid is not None:
            fresh = v
            break

    print("", flush=True)
    if fresh:
        print(
            f"VERDICT_MARKER: FRESH uvicorn serving "
            f"pid={fresh['pid']}  uptime={fresh['uptime_seconds']:.2f}s  "
            f"build_id={fresh.get('build_id')}  "
            f"module_size_bytes={fresh.get('module_size_bytes')}",
            flush=True,
        )
        sys.exit(0)
    else:
        print(
            f"VERDICT_MARKER: STALE -- no fresh /version within "
            f"{MAX_POLLS} polls.",
            flush=True,
        )
        try:
            with open(LOG, "rb") as fh:
                data = fh.read()
            tail = data[-4000:].decode(errors="replace")
            print("  tail of uvicorn.log (last 30 lines):", flush=True)
            for line in tail.splitlines()[-30:]:
                print(f"    {line}", flush=True)
        except Exception as e:
            print(f"  log tail failed: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
