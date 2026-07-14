import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get('HLD_API_BASE', 'http://127.0.0.1:8080')
RESULTS_URL = f'{API_BASE}/ftth/hld/results'
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 30 * 60
TEXT_TRUNC = 200


def resolve_pid() -> str:
    pid = os.environ.get('LAST_PID') or (sys.argv[1] if len(sys.argv) > 1 else '')
    pid = pid.strip()
    if not pid:
        print(f'usage: {sys.argv[0]} <project_id>  (or set LAST_PID env)', file=sys.stderr)
        sys.exit(2)
    return pid


def truncate(text: str, cap: int = TEXT_TRUNC) -> str:
    return text if len(text) <= cap else text[: cap - 1] + '\u2026'


def main() -> int:
    pid = resolve_pid()
    last_msg_count = 0
    t0 = time.time()
    final = None
    timed_out = False
    os.makedirs('/tmp/hld', exist_ok=True)
    print(f'# polling project_id={pid}', flush=True)
    while True:
        try:
            with urllib.request.urlopen(f'{RESULTS_URL}/{pid}', timeout=30) as r:
                v = json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f'# poll err: {e}', flush=True); time.sleep(POLL_INTERVAL_S); continue
        status = v.get('status'); stage = v.get('stage') or '-'
        si = v.get('stage_index', 0); sc = v.get('stage_count', 6); prog = v.get('progress', 0)
        elapsed = int(time.time() - t0)
        msgs = v.get('messages', []); new = msgs[last_msg_count:]
        last_msg_count = len(msgs)
        print(f'# [{elapsed:4d}s] status={status:9s} stage={si}/{sc} [{stage}] progress={prog}%', flush=True)
        for m in new:
            txt = (m.get('text') or '').replace('\u2014', '-').replace('\u2192', '->')
            if txt:
                print(f'   {truncate(txt)}', flush=True)
        if status in ('completed', 'failed'):
            final = v; break
        if elapsed > POLL_TIMEOUT_S:
            print(f'# giving up after {POLL_TIMEOUT_S//60} min', flush=True)
            timed_out = True; break
        time.sleep(POLL_INTERVAL_S)

    out = {
        'project_id': pid,
        'timed_out': timed_out,
        'status': (final or {}).get('status'),
        'error': (final or {}).get('error'),
        'stage_index': (final or {}).get('stage_index'),
        'stage_count': (final or {}).get('stage_count'),
        'stage': (final or {}).get('stage'),
        'progress': (final or {}).get('progress'),
        'layers': (final or {}).get('layers', []),
        'downloads': (final or {}).get('downloads', []),
        'messages': (final or {}).get('messages', []),
        'elapsed_s': int(time.time() - t0),
    }
    with open('/tmp/hld/final.json', 'w') as f:
        json.dump(out, f, indent=2)
    with open('/tmp/hld/last_pid.txt', 'w') as f:
        f.write(pid + '\n')

    print()
    print('=== FINAL ===')
    print('status :', out['status'])
    print('timed  :', timed_out)
    print('error  :', out['error'] if out['error'] else '(none)')
    print('layers :')
    for lyr in out['layers'] or []:
        print(f"   {lyr.get('name','?'):28s} features={lyr.get('feature_count','?')}  geom={lyr.get('geometry_type','?')}")
    print('downloads:')
    for d in out['downloads'] or []:
        print(f"   {d.get('name','?'):32s}  {d.get('size_bytes','?')} bytes")
    return 0 if (out['status'] == 'completed' or timed_out) else 1


if __name__ == '__main__':
    sys.exit(main())
