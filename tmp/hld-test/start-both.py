"""Start the HLD dev stack: uvicorn --reload on :8000 (FastAPI backend)
plus Vite on :5173 (React frontend). Verify both with real HTTP probes,
then leave them running so the user can hit the UI. Print PIDs at the end
so a manual `taskkill /F /PID X,Y,Z` cleans up if needed.

Key design points:
- Reap :8000 and :5173 before spawning (the previous reload-walkthrough
  tests left port :8000 TIME_WAIT residue that's been blocking fresh binds).
- For uvicorn we MUST NOT pass DETACHED_PROCESS: uvicorn --reload uses
  os.kill(pid, signal.CTRL_C_EVENT) to swap workers, and that signal
  requires the sender and target to share a console. DETACHED_PROCESS
  breaks the console link (WinError 6). Plain inheritance + redirected
  stdio keeps the chain intact.
- Vite (npm run dev) does not have a reload/worker-respawn chain, so
  any spawn style is fine. We use the same plain-Popen style for
  consistency.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

ROOT       = Path(__file__).resolve().parent.parent.parent  # .../HLD_Planning_01
BACKEND    = ROOT / 'web' / 'backend'
FRONTEND   = ROOT / 'web' / 'frontend'
FIX        = ROOT / 'tmp' / 'hld-test'
BACK_PORT  = 8000
FRONT_PORT = 5173

PORT_BACK_PID_STR  = str(BACK_PORT)
PORT_FRONT_PID_STR = str(FRONT_PORT)

BACK_LOG  = FIX / 'uvicorn-stack.log'
FRONT_LOG = FIX / 'vite-stack.log'

processes: List[subprocess.Popen] = []
spawned_pids: List[int] = []


def reap_port(port: str, label: str) -> None:
    """Kill any process listening on `port`, iteratively, until the port is
    free. Returns silently once the port is clean, or prints a warning."""
    for attempt in range(8):
        try:
            ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True).stdout
        except Exception as e:
            print(f'  [{label}] netstat failed: {e}', flush=True)
            break
        pids = []
        for line in ns.splitlines():
            if f':{port}' in line and 'LISTENING' in line:
                try:
                    pids.append(int(line.split()[-1]))
                except (ValueError, IndexError):
                    pass
        # De-dup but preserve order
        seen = set(); unique = []
        for p in pids:
            if p not in seen:
                seen.add(p); unique.append(p)
        if not unique:
            print(f'  [{label}] :{port} is free (after {attempt} reap pass(es))', flush=True)
            return
        print(f'  [{label}] Reap pass {attempt}: killing {unique}', flush=True)
        for pid in unique:
            try:
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
            except Exception:
                pass
        time.sleep(2)
    print(f'  [{label}] WARNING: :{port} still held after 8 reap passes', flush=True)


def wait_for(url: str, *, label: str, timeout: float = 60.0) -> int:
    """Poll `url` until it returns 200, or timeout. Returns elapsed seconds."""
    deadline = time.time() + timeout
    t0 = time.time()
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return time.time() - t0
        except (urllib.error.URLError, OSError, ConnectionResetError) as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(f'{label} did not become reachable within {timeout}s (last_err={last_err})')


def show_first_json_lines(label: str, url: str, *, max_bytes: int = 600, want_field_keys: bool = False) -> None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = r.read(max_bytes)
            print(f'  [{label}] {url} -> HTTP {r.status}; first {len(data)} bytes:', flush=True)
            if want_field_keys:
                try:
                    j = json.loads(data)
                    print('  field_keys =', sorted(j.keys()), flush=True)
                    for k in sorted(j.keys()):
                        v = j[k]
                        vs = repr(v)
                        if len(vs) > 120:
                            vs = vs[:120] + '...'
                        print(f'      {k:24s} = {vs}', flush=True)
                except json.JSONDecodeError:
                    print('  (body was not JSON; raw):', data[:200], flush=True)
            else:
                # HTML or text — just print the first chunk
                print('  ', data[:200].replace(b'\n', b'\\n').decode('utf-8', errors='replace'), flush=True)
    except Exception as e:
        print(f'  [{label}] probe failed: {e}', flush=True)


def spawn_uvicorn() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    log_fh = open(BACK_LOG, 'wb')
    proc = subprocess.Popen(
        ['python', '-m', 'uvicorn', 'main:app',
         '--host', '127.0.0.1', '--port', str(BACK_PORT),
         '--log-level', 'info', '--reload'],
        cwd=str(BACKEND),
        stdin=subprocess.DEVNULL,
        stdout=log_fh, stderr=subprocess.STDOUT,
        # NO creationflags here — uvicorn --reload needs shared console.
    )
    processes.append(proc)
    spawned_pids.append(proc.pid)
    print(f'  [backend] supervisor pid={proc.pid}; cwd={BACKEND}; log={BACK_LOG}', flush=True)
    return proc.pid


def spawn_vite() -> int:
    FIX.mkdir(parents=True, exist_ok=True)
    log_fh = open(FRONT_LOG, 'wb')
    # npm on Windows is npm.cmd (a batch shim), not npm.exe. Popen's
    # CreateProcess does NOT search PATHEXT, so passing ['npm', ...]
    # raises FileNotFoundError. Explicit .cmd suffix resolves on every
    # Python 3.7+ install.
    proc = subprocess.Popen(
        ['npm.cmd', 'run', 'dev'],
        cwd=str(FRONTEND),
        stdin=subprocess.DEVNULL,
        stdout=log_fh, stderr=subprocess.STDOUT,
        # Vite has no reload/worker chain, so creationflags don't matter here.
    )
    processes.append(proc)
    spawned_pids.append(proc.pid)
    print(f'  [frontend] dev-server pid={proc.pid}; cwd={FRONTEND}; log={FRONT_LOG}', flush=True)
    return proc.pid


def main() -> int:
    front_url = f'http://localhost:{FRONT_PORT}/'   # Use 'localhost' (resolved via getaddrinfo) instead of '127.0.0.1'.
                                                     # Vite on Windows binds to whichever IP localhost resolves to first — often IPv6 [::1] only — so probing the literal IPv4 fails.
                                                     # 'localhost' gives urllib.urlopen a fall-back ladder over both stacks.
    back_url  = f'http://127.0.0.1:{BACK_PORT}/version'

    print('=' * 70)
    print('HLD STACK STARTUP')
    print('=' * 70)

    print('\n[0] Reap stale listeners (defensive — keeps the run deterministic).')
    print('  -- :8000 (uvicorn)')
    reap_port(PORT_BACK_PID_STR, 'backend')
    print('  -- :5173 (vite)')
    reap_port(PORT_FRONT_PID_STR, 'frontend')

    print('\n[1] Spawn both servers (children will outlive this script on Windows).')
    back_pid = spawn_uvicorn()
    front_pid = spawn_vite()

    print('\n[2] Wait for HTTP probes to come up (max 60s each).')
    back_ok = False
    front_ok = False
    try:
        elapsed = wait_for(back_url, label='backend /version', timeout=60.0)
        print(f'  [backend] /version 200 OK after {elapsed:.1f}s', flush=True)
        back_ok = True
    except TimeoutError as e:
        print(f'  [backend] FAILED: {e}', flush=True)
    try:
        elapsed = wait_for(front_url, label='vite /', timeout=60.0)
        print(f'  [frontend] / 200 OK after {elapsed:.1f}s', flush=True)
        front_ok = True
    except TimeoutError as e:
        print(f'  [frontend] FAILED: {e}', flush=True)

    print('\n[3] Verify-each inspection.')
    if back_ok:
        show_first_json_lines('backend', back_url, want_field_keys=True)
    if front_ok:
        show_first_json_lines('frontend', front_url, want_field_keys=False)

    print('\n[4] Tail of uvicorn log (first 15 lines):')
    try:
        for line in BACK_LOG.read_text(encoding='utf-8', errors='replace').splitlines()[:15]:
            print(f'    {line}', flush=True)
    except Exception as e:
        print(f'    could not read log: {e}', flush=True)

    print('\n[5] Tail of Vite log (first 15 lines):')
    try:
        for line in FRONT_LOG.read_text(encoding='utf-8', errors='replace').splitlines()[:15]:
            print(f'    {line}', flush=True)
    except Exception as e:
        print(f'    could not read log: {e}', flush=True)

    print('\n' + '=' * 70)
    print('STACK STATUS')
    print('=' * 70)
    print(f'  backend  (uvicorn --reload): pid={back_pid:>5d}  port={BACK_PORT}  {"OK" if back_ok else "FAIL"}')
    print(f'  frontend (Vite dev server):  pid={front_pid:>5d}  port={FRONT_PORT}  {"OK" if front_ok else "FAIL"}')
    print()
    print('Servers are LEFT RUNNING so you can hit the UI.')
    print(f'  Browser URLs:  http://127.0.0.1:{FRONT_PORT}/   (Vite, dev UI)')
    print(f'                 http://127.0.0.1:{BACK_PORT}/version  (uvicorn, JSON)')
    print(f'                 http://127.0.0.1:{BACK_PORT}/docs    (uvicorn, OpenAPI)')
    print()
    print('To stop them manually:')
    # /T walks the process tree so the uvicorn worker + npm.cmd's node.exe
    # child also die — taskkill /F alone orphans them, leaving the ports held.
    print(f'  taskkill /F /T /PID {back_pid} {front_pid}')
    print('=' * 70)

    return 0 if (back_ok and front_ok) else 1


if __name__ == '__main__':
    raise SystemExit(main())
