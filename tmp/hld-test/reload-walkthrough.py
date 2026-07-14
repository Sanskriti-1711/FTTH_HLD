"""Restart uvicorn with --reload, hit /version before and after touching main.py
to trigger uvicorn's reload watcher, and verify the post-restart fields."""
import json, os, time, subprocess, urllib.request, urllib.error
from pathlib import Path

ROOT       = Path(r'D:/Downloads_D/Q-GIS/HLD_Planning_01')
MAIN_PY    = ROOT / 'web' / 'backend' / 'main.py'
LOG_FILE   = ROOT / 'tmp' / 'hld-test' / 'uvicorn-reload.log'
# Use 18742 instead of 8000 so we don't collide with TIME_WAIT residue from
# previous uvicorn restarts on this box. The /version endpoint is the same
# FastAPI handler regardless of port.
API        = 'http://127.0.0.1:18742'
PORT_PID   = '18742'
pids_kill  = []
results    = {
    'pre_pid':   None, 'post_pid':   None,
    'pre_up':    None, 'post_up':    None,
    'pre_size':  None, 'post_size':  None,
    'pre_mtime': None, 'post_mtime': None,
    'build_id_pre':  None, 'build_id_post': None,
    'started_at_pre': None, 'started_at_post': None,
}


def hit():
    with urllib.request.urlopen(API + '/version', timeout=3) as r:
        return json.loads(r.read())


def discover_pids():
    try:
        ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True).stdout
        for line in ns.splitlines():
            if f':{PORT_PID}' in line and 'LISTENING' in line:
                try:
                    p = int(line.split()[-1])
                    if p not in pids_kill:
                        pids_kill.append(p)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass


def reap_pids():
    for pid in pids_kill:
        try:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        except Exception:
            pass


def fmt_snapshot(label, body):
    sysout = (
        f'{label:18s} pid={body["pid"]:>6d}  '
        f'uptime={body["uptime_seconds"]:6.2f}s  '
        f'build_id={body["build_id"]!r:<24}  '
        f'module_size={body["module_size_bytes"]:>6d}B  '
        f'mtime={body["module_mtime"]}  '
        f'started_at={body["started_at"]}'
    )
    print(sysout, flush=True)


try:
    # 0) Reap anything left over. We MUST iterate until :8000 is fully free,
    # not just do one pass — stale uvicorn workers from prior test runs in
    # this directory will keep the port listening even after their supervisor
    # is reaped, and a fresh uvicorn cannot bind :8000 until the port is
    # actually released by the kernel (TIME_WAIT / closed FD propagation).
    for attempt in range(8):
        discover_pids()
        if not pids_kill:
            print(f'[0] :8000 is free after {attempt} reap pass(es)', flush=True)
            break
        print(f'[0] Reap pass {attempt}: killing {pids_kill}', flush=True)
        reap_pids()
        pids_kill.clear()
        time.sleep(2)  # give the kernel time to release the listening socket
    else:
        # Still something on the port after 8 * 2s = 16s. Bail noisily.
        discover_pids()
        print(f'[0] WARNING: port :8000 still held by {pids_kill} after 8 reap '
              f'passes — proceeding anyway; the new uvicorn may fail to bind.', flush=True)

    # 1) Start uvicorn with --reload (Windows-detached so the supervisor survives).
    print('\n[1] Starting uvicorn with --reload', flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_FILE, 'wb')
    # IMPORTANT: do NOT pass DETACHED_PROCESS here.
    # uvicorn --reload uses os.kill(pid, signal.CTRL_C_EVENT) to stop the old
    # worker before forking a new one. On Windows that signal requires the
    # sender and target to share a console; DETACHED_PROCESS breaks the link
    # and yields "WinError 6: The handle is invalid" when main.py changes.
    # Plain inheritance keeps the console chain intact; stdio redirect to a
    # log file is still enough to keep things tidy.
    proc = subprocess.Popen(
        ['python', '-m', 'uvicorn', 'main:app',
         '--host', '127.0.0.1', '--port', '18742',
         '--log-level', 'info', '--reload'],
        cwd=str(ROOT / 'web' / 'backend'),
        stdin=subprocess.DEVNULL,
        stdout=log_handle, stderr=subprocess.STDOUT,
    )
    pids_kill.append(proc.pid)
    print(f'    supervisor pid={proc.pid} (no DETACHED_PROCESS); logs -> {LOG_FILE}', flush=True)

    # 2) Wait for bind (with --reload, the first boot can take longer due to file-watcher import).
    deadline = time.time() + 30
    print('[2] Polling /version until bound (max 30s)...', flush=True)
    while time.time() < deadline:
        try:
            urllib.request.urlopen(API + '/version', timeout=1).read()
            print('    bound OK', flush=True)
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    else:
        print('    FAILED to bind within 30s — see uvicorn log:', flush=True)
        # Print last 40 lines of the log
        log_text = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        for line in log_text[-40:]:
            print(f'    | {line}', flush=True)
        raise SystemExit('uvicorn --reload never became reachable')

    # 3) First /version — record fields.
    pre = hit()
    results['pre_pid']        = pre['pid']
    results['pre_up']         = pre['uptime_seconds']
    results['pre_size']       = pre['module_size_bytes']
    results['pre_mtime']      = pre['module_mtime']
    results['build_id_pre']   = pre['build_id']
    results['started_at_pre'] = pre['started_at']
    fmt_snapshot('PRE-RELOAD  ', pre)

    # Wait 2s and re-hit — confirm uptime grows within the SAME worker.
    time.sleep(2)
    pre2 = hit()
    grew_within = pre2['uptime_seconds'] > pre['uptime_seconds']
    grew_amount = pre2['uptime_seconds'] - pre['uptime_seconds']
    print(f'[3] +2s later: pid={pre2["pid"]} (same={pre2["pid"]==pre["pid"]}), '
          f'uptime={pre2["uptime_seconds"]:.2f}s, '
          f'grew by {grew_amount:.2f}s (worker alive?).', flush=True)
    if not grew_within:
        print('    WARNING: uptime did not grow within 2s; worker may not be stable.', flush=True)

    # 4) Modify main.py so the watcher fires the reload.
    #
    # Two layers of belt-and-braces for Windows + NTFS:
    #   (a) read_text + write_text with byte-identical content — forces the
    #       directory entry cache to flush (Path.touch() alone won't).
    #   (b) os.utime(MAIN_PY, None) — explicit mtime bump to wall-clock now,
    #       in case NTFS short-circuits a write of identical bytes.
    pre_touch_mtime = pre['module_mtime']
    print(f'\n[4] Updating {MAIN_PY} to fire --reload (mtime was {pre_touch_mtime})', flush=True)
    original = MAIN_PY.read_text(encoding='utf-8')
    MAIN_PY.write_text(original, encoding='utf-8')
    os.utime(MAIN_PY, None)  # belt + braces
    time.sleep(0.5)  # let the kernel publish the directory entry update
    post_touch_mtime_body = hit()
    print(f'    after rewrite + utime: mtime={post_touch_mtime_body["module_mtime"]}', flush=True)

    # Wait for the reload to happen (uvicorn's watcher polls; on Windows stat polls by default).
    print('[5] Waiting up to 15s for the reload watcher to fire...', flush=True)
    deadline = time.time() + 15
    post = None
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = hit()
        except Exception:
            continue
        # Restart signature: new pid OR uptime reset to < a couple of seconds.
        if cur['pid'] != pre['pid'] or cur['uptime_seconds'] < 1.5:
            post = cur
            break
    if post is None:
        print('    NO RELOAD DETECTED within 15s. Last seen pid/uptime:', flush=True)
        last = hit()
        print(f'    pid={last["pid"]}  uptime={last["uptime_seconds"]:.2f}s', flush=True)
        post = last  # fall through, verification will mark failures

    # 6) Verify the restart.
    results['post_pid']        = post['pid']
    results['post_up']         = post['uptime_seconds']
    results['post_size']       = post['module_size_bytes']
    results['post_mtime']      = post['module_mtime']
    results['build_id_post']   = post['build_id']
    results['started_at_post'] = post['started_at']
    fmt_snapshot('POST-RELOAD ', post)

    pid_changed      = post['pid'] != pre['pid']
    started_at_diff  = post['started_at'] != pre['started_at']
    uptime_reset     = post['uptime_seconds'] < 5.0
    mtime_at_or_after = post['module_mtime'] >= pre['module_mtime']
    size_stable      = post['module_size_bytes'] == pre['module_size_bytes']
    build_id_stable  = post['build_id'] == pre['build_id']

    print()
    print('[6] Verification matrix:')
    print(f'    pid changed      : {pre["pid"]} -> {post["pid"]}      -> {"OK" if pid_changed else "FAIL"}')
    print(f'    started_at moved : {pre["started_at"]} -> {post["started_at"]}')
    print(f'                       -> {"OK" if started_at_diff else "FAIL"}')
    print(f'    uptime reset <5s : {post["uptime_seconds"]:.2f}s                   -> {"OK" if uptime_reset else "FAIL"}')
    print(f'    module_mtime >=  : {pre["module_mtime"]} -> {post["module_mtime"]}')
    print(f'                       -> {"OK" if mtime_at_or_after else "FAIL"}')
    print(f'    module_size unchanged (touch only, no edit): {pre["module_size_bytes"]} == {post["module_size_bytes"]}')
    print(f'                       -> {"OK" if size_stable else "FAIL"}')
    print(f'    build_id constant: {post["build_id"]!r}          -> {"OK" if build_id_stable else "FAIL"}')

    overall = pid_changed and started_at_diff and uptime_reset and size_stable and build_id_stable
    print(f'\nOVERALL: {"RESTART CONFIRMED" if overall else "RESTART FAILED"}')

    if overall:
        # Show first ~30 lines of uvicorn log so the user sees the reload trace.
        log_text = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        print()
        print('[uvicorn --reload log tail (last 25 lines)]')
        for line in log_text[-25:]:
            print(f'    {line}')

except Exception as e:
    import traceback
    print('\n!!! CRASH:', type(e).__name__, e)
    print(traceback.format_exc())

finally:
    print('\n--- Cleanup ---', flush=True)
    discover_pids()
    reap_pids()
    print(f'    killed: {pids_kill}', flush=True)
    print(f'    uvicorn log: {LOG_FILE}', flush=True)
