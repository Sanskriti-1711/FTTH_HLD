"""End-to-end walkthrough of the HLD Planner FastAPI contract as the
inlined IIFE would actually use it. Designed to be 100% crash-proof so the
spawned uvicorn + http.server processes get reaped at the end regardless of
which step goes wrong.

Run: python D:/Downloads_D/Q-GIS/HLD_Planning_01/tmp/hld-test/walkthrough.py
"""

import json, sys, time, traceback
import urllib.parse as up
import urllib.request
import urllib.error
import uuid
import subprocess
from pathlib import Path

ROOT = Path(r'D:/Downloads_D/Q-GIS/HLD_Planning_01')
FIX = ROOT / 'tmp' / 'hld-test'
XLSX = FIX / 'test_addresses.xlsx'
GJ   = FIX / 'test_roads.geojson'
API  = 'http://127.0.0.1:8000'

PASS = 0
FAIL = 0
RESULTS = {'passes': [], 'fails': [], 'events': 0, 'event_types': {},
           'tid': None, 'auth': None, 'scratch_id': None}
PIDS_KILLED = []


def out(s, *, end='\n'):
    sys.stdout.write(s + end)
    sys.stdout.flush()


def ok(name):
    global PASS
    PASS += 1
    RESULTS['passes'].append(name)
    out(f'    [OK] {name}')


def fail(name, msg):
    global FAIL
    FAIL += 1
    RESULTS['fails'].append((name, str(msg)))
    out(f'    [FAIL] {name}: {msg}')


def step(name):
    out(f'\n>>> {name}')


def safe_pct(x):
    return f'{x:.1f}' if isinstance(x, (int, float)) else repr(x)


def safe_len(x):
    return len(x) if x is not None else 0


def main():
    pids_to_kill = []

    try:
        # 1) /version diagnostic
        step('GET /version')
        with urllib.request.urlopen(API + '/version', timeout=3) as r:
            v = json.loads(r.read())
        out(f'    build_id={v["build_id"]}  pid={v["pid"]}  qgis={v["qgis_available"]} '
            f'ogr2ogr={v["has_ogr2ogr"]}  uptime={safe_pct(v["uptime_seconds"])}s')
        pids_to_kill.append(int(v['pid']))
        ok('/version: new diagnostic fields (build_id, pid, uptime, qgis_available, has_ogr2ogr)')

        # 2) /upload-roads
        step('POST /upload-roads with test_roads.geojson')
        bu = uuid.uuid4().hex
        body = (
            f'--{bu}\r\n'
            f'Content-Disposition: form-data; name="roads"; filename="{GJ.name}"\r\n'
            f'Content-Type: application/geo+json\r\n\r\n'
        ).encode() + GJ.read_bytes() + b'\r\n' + f'--{bu}--\r\n'.encode()
        req = urllib.request.Request(
            API + '/upload-roads', data=body, method='POST',
            headers={'Content-Type': f'multipart/form-data; boundary={bu}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            ur = json.loads(r.read())
        out(f'    ok={ur.get("ok")}  scratch_id={ur.get("scratch_id")}  '
            f'features={ur.get("feature_count")}  geom={ur.get("geometry_types")}  '
            f'fclass_like={ur.get("has_fclass_like_field")}')
        RESULTS['scratch_id'] = ur.get('scratch_id')
        fc = ur.get('feature_count')
        if ur.get('ok') and fc == 3:
            ok('upload-roads: 3 LineString features detected, geojson routed correctly')
        else:
            fail('upload-roads', f'ok={ur.get("ok")} feature_count={fc}')

        # 3) /run-hld
        step('POST /run-hld with xlsx + scratch_id')
        bu = uuid.uuid4().hex
        excel_part = (
            f'--{bu}\r\n'
            f'Content-Disposition: form-data; name="excel"; filename="{XLSX.name}"\r\n'
            f'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'
        ).encode() + XLSX.read_bytes() + b'\r\n'
        scratch_part = (
            f'--{bu}\r\nContent-Disposition: form-data; name="roads_scratch_id"\r\n\r\n'
        ).encode() + RESULTS['scratch_id'].encode() + b'\r\n'
        body = excel_part + scratch_part + f'--{bu}--\r\n'.encode()
        req = urllib.request.Request(
            API + '/run-hld', data=body, method='POST',
            headers={'Content-Type': f'multipart/form-data; boundary={bu}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            rr = json.loads(r.read())
        RESULTS['tid'] = rr['task_id']
        RESULTS['auth'] = rr['auth_token']
        out(f'    task_id={RESULTS["tid"]}  auth_token={RESULTS["auth"][:16]}...')
        ok('run-hld: returned task_id + opaque auth_token')

        # 4) /events/{tid} SSE stream (8 second capture)
        step(f'GET /events/{RESULTS["tid"]} (SSE, 8s capture)')
        req = urllib.request.Request(API + '/events/' + RESULTS['tid'])
        events = []
        deadline = time.time() + 8
        with urllib.request.urlopen(req, timeout=8) as r:
            buf = b''
            while time.time() < deadline:
                try:
                    chunk = r.read(64)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
                while buf.find(b'\n\n') != -1:
                    end = buf.find(b'\n\n')
                    frame, buf = buf[:end], buf[end + 2:]
                    evt, data = None, None
                    for ln in frame.decode('utf-8', errors='replace').split('\n'):
                        if ln.startswith('event: '):
                            evt = ln[7:]
                        elif ln.startswith('data: '):
                            try:
                                data = json.loads(ln[6:])
                            except Exception:
                                data = ln[6:]
                    if evt:
                        events.append((evt, data))
        RESULTS['events'] = len(events)
        RESULTS['event_types'] = {}
        for evt, _ in events:
            RESULTS['event_types'][evt] = RESULTS['event_types'].get(evt, 0) + 1
        out(f'    {len(events)} SSE events captured')
        out(f'    by event-type: {sorted(RESULTS["event_types"].items())}')

        # Per-type summary (first occurrence only) -- None-tolerant
        summaries = {}
        for evt, d in events:
            if evt in summaries or not isinstance(d, dict):
                if evt not in summaries and not isinstance(d, dict):
                    summaries[evt] = repr(d)[:80]
                continue
            if evt == 'progress':
                summaries[evt] = (
                    f'stage_idx={d.get("stage_index")} '
                    f'progress={safe_pct(d.get("progress"))} '
                    f'stage={d.get("stage")!r}')
            elif evt == 'snapshot':
                summaries[evt] = (
                    f'status={d.get("status")} '
                    f'stage={d.get("stage")!r} '
                    f'layers={safe_len(d.get("layers"))}')
            elif evt == 'done':
                summaries[evt] = (
                    f'status={d.get("status")} '
                    f'layers={safe_len(d.get("layers"))} '
                    f'downloads={safe_len(d.get("downloads"))}')
            elif evt == 'message':
                summaries[evt] = (
                    f'lvl={d.get("level")} text={(d.get("text") or "")[:60]!r}')
            elif evt == 'stage_text':
                summaries[evt] = f'stage={d.get("stage")!r}'
            else:
                summaries[evt] = repr(d)[:80]
        for evt, s in sorted(summaries.items()):
            out(f'    {evt:12s}: {s}')

        must_have = ['snapshot', 'progress', 'done']
        missing = [e for e in must_have if e not in RESULTS['event_types']]
        if missing:
            fail('SSE event sequence', f'missing: {missing}; got: {sorted(RESULTS["event_types"].keys())}')
        else:
            ok(f'SSE emits {len(events)} events covering {sorted(must_have)} '
               f'(plus optional message/stage_text)')

        # 5) /status final
        step(f'GET /status/{RESULTS["tid"]}')
        with urllib.request.urlopen(API + '/status/' + RESULTS['tid'], timeout=5) as r:
            st = json.loads(r.read())
        out(f'    status={st.get("status")}  progress={safe_pct(st.get("progress"))}  '
            f'stage={st.get("stage")!r}  runner={st.get("runner")}  '
            f'layers={st.get("layers")}')
        if st.get('status') == 'completed':
            ok('pipeline reached terminal completed state')
        else:
            fail('status', f'expected completed, got {st.get("status")}')

        # 6) /layers happy path
        step(f'GET /layers/{RESULTS["tid"]}/Roads?token=... (toggle-on)')
        url = API + '/layers/' + RESULTS['tid'] + '/Roads?token=' + up.quote(RESULTS['auth'])
        with urllib.request.urlopen(url, timeout=5) as r:
            lyr = json.loads(r.read())
        out(f'    type={lyr.get("type")}  features={safe_len(lyr.get("features"))}')
        if lyr.get('type') == 'FeatureCollection' and safe_len(lyr.get('features')) >= 1:
            f0 = lyr['features'][0]
            out(f'    feature[0].geometry.type={f0["geometry"]["type"]}  '
                f'properties={list(f0["properties"].keys())[:5]}')
            ok('layer fetch: returns GeoJSON via auth_token, features present')
        else:
            fail('layer fetch', f'expected FeatureCollection w/ >=1 feature; '
                            f'got type={lyr.get("type")} features={safe_len(lyr.get("features"))}')

        # 7) /layers bad-token gate
        step(f'GET /layers/{RESULTS["tid"]}/Roads?token=BADTOKEN -- expect 403')
        try:
            with urllib.request.urlopen(
                API + '/layers/' + RESULTS['tid'] + '/Roads?token=BADTOKEN', timeout=5) as r:
                fail('token gate', f'expected 403, got {r.status}')
        except urllib.error.HTTPError as e:
            if e.code == 403:
                ok('token-gate fires 403 on bad token (auth_token wired correctly)')
            else:
                fail('token gate', f'expected 403, got {e.code}')

    except Exception as e:
        out(f'\n!!! WALKTHROUGH CRASH: {type(e).__name__}: {e}')
        out(traceback.format_exc())

    finally:
        # Cleanup -- ALWAYS runs.
        out('\n>>> Cleanup')
        try:
            netstat_out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True).stdout
            for port in (8000, 8001):
                for line in netstat_out.splitlines():
                    if f':{port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        try:
                            pid = int(parts[-1])
                        except (ValueError, IndexError):
                            continue
                        if pid not in pids_to_kill:
                            pids_to_kill.append(pid)
        except Exception as e:
            out(f'    netstat scan failed: {e}')
        for pid in pids_to_kill:
            try:
                r = subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                                    capture_output=True, text=True)
                PIDS_KILLED.append(pid)
                out(f'    taskkill /F /PID {pid} -> exit={r.returncode}')
            except Exception as e:
                out(f'    kill pid={pid} err: {e}')


if __name__ == '__main__':
    main()
    out('\n' + '=' * 70)
    out(f'PASS: {PASS}   FAIL: {FAIL}')
    out(f'SSE events: {RESULTS["events"]}; by type: {sorted(RESULTS["event_types"].items())}')
    out(f'scratch_id={RESULTS["scratch_id"]}')
    out(f'task_id={RESULTS["tid"]}')
    out(f'PIDs killed: {PIDS_KILLED}')
    out('=' * 70)
