#!/usr/bin/env python3
"""Stdlib-only E2E smoke test for the running FastAPI backend.

Expected: pid 18240 holding build_id "1.0.0+stream-logs+utf8+ff".
Steps:
  [0] /version liveness
  [1] write a tiny geojson if missing
  [2] /upload-roads -> get scratch_id
  [3] /run-hld -> get task_id + auth_token
  [4] /events/{tid} SSE subscribe for 25s, count typed frames
  [5] summary
  [6] tail web/backend/logs/qgis_run.log
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from io import BytesIO

URL = "http://localhost:8000"
FIX_DIR = r"D:/Downloads_D/Q-GIS/HLD_Planning_01/tmp/hld-test/fixtures"
FIX = os.path.join(FIX_DIR, "tiny-roads.geojson")
EXCEL = r"D:/Downloads_D/Q-GIS/HLD_Planning_01/tmp/hld-test/test_addresses.xlsx"
LOG = r"D:/Downloads_D/Q-GIS/HLD_Planning_01/web/backend/logs/qgis_run.log"


def post_multipart(url, files, fields=None, timeout=60):
    """files: dict[name] = (filename, bytes).  fields: dict[name] = str."""
    fields = fields or {}
    boundary = "----PyBnd" + str(int(time.time()))
    out = []
    for k, v in fields.items():
        out.append(f"--{boundary}".encode())
        out.append(f'Content-Disposition: form-data; name="{k}"'.encode())
        out.append(b"")
        out.append(v.encode("utf-8") if isinstance(v, str) else v)
    for k, (fname, data) in files.items():
        out.append(f"--{boundary}".encode())
        out.append(
            f'Content-Disposition: form-data; name="{k}"; filename="{fname}"'.encode()
        )
        out.append(b"Content-Type: application/octet-stream")
        out.append(b"")
        out.append(data if isinstance(data, bytes) else data.encode("utf-8"))
    out.append(f"--{boundary}--".encode())
    out.append(b"")
    payload = b"\r\n".join(out)
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def main():
    # [0] /version
    print("=== [0] /version diagnostic ===")
    try:
        with urllib.request.urlopen(f"{URL}/version", timeout=5) as r:
            v = json.loads(r.read())
        print(
            f"  http=200 pid={v.get('pid')} build_id={v.get('build_id')} "
            f"uptime={v.get('uptime_seconds', 0):.1f}s "
            f"module_size_bytes={v.get('module_size_bytes')} "
            f"qgis_available={v.get('qgis_available')} "
            f"has_ogr2ogr={v.get('has_ogr2ogr')} "
            f"started_at={v.get('started_at')}"
        )
    except urllib.error.URLError as e:
        print(f"  SERVER UNREACHABLE: {e.reason}")
        return

    # [1] tiny fixture
    print("\n=== [1] tiny roads.geojson fixture ===")
    os.makedirs(FIX_DIR, exist_ok=True)
    if not os.path.exists(FIX):
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[13.405, 52.515], [13.41, 52.51], [13.42, 52.52]],
                    },
                    "properties": {"fclass": "primary"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[13.42, 52.52], [13.43, 52.53]],
                    },
                    "properties": {"fclass": "secondary"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[13.43, 52.53], [13.44, 52.54]],
                    },
                    "properties": {"fclass": "primary"},
                },
            ],
        }
        with open(FIX, "w", encoding="utf-8") as f:
            json.dump(gj, f)
    print(f"  fixture_path={FIX}")
    print(f"  fixture_size={os.path.getsize(FIX)} bytes")
    print(f"  excel_size={os.path.getsize(EXCEL)} bytes")

    # [2] /upload-roads
    print("\n=== [2] /upload-roads ===")
    rdata = open(FIX, "rb").read()
    status, body = post_multipart(
        f"{URL}/upload-roads", {"roads": ("tiny-roads.geojson", rdata)}
    )
    print(f"  http={status}")
    print(f"  response (first 500 chars): {body[:500]}")
    try:
        rr = json.loads(body)
        scratch = rr["scratch_id"]
    except Exception as e:
        print(f"  PARSE ERROR: {e}")
        return
    print(
        f"  parsed: scratch_id={scratch} filename={rr.get('filename')} "
        f"size_bytes={rr.get('size_bytes')} feature_count={rr.get('feature_count')} "
        f"is_lines={rr.get('is_lines')} geometry_types={rr.get('geometry_types')}"
    )

    # [3] /run-hld
    print("\n=== [3] /run-hld ===")
    xdata = open(EXCEL, "rb").read()
    status, body = post_multipart(
        f"{URL}/run-hld",
        {"excel": ("test_addresses.xlsx", xdata)},
        fields={"roads_scratch_id": scratch},
        timeout=60,
    )
    print(f"  http={status}")
    print(f"  response (first 400 chars): {body[:400]}")
    try:
        rr2 = json.loads(body)
        tid = rr2["task_id"]
        token = rr2.get("auth_token", "")
    except Exception as e:
        print(f"  PARSE ERROR: {e}")
        return
    print(f"  parsed: task_id={tid} auth_token_chars={len(token)}")

    # [4] /events/{tid} SSE
    print(f"\n=== [4] /events/{tid} SSE subscribe for 25s ===")
    req = urllib.request.Request(
        f"{URL}/events/{tid}", headers={"Accept": "text/event-stream"}
    )
    start = time.time()
    total_lines = 0
    types = {}
    snapshots = 0
    progresses = 0
    messages = 0
    done = 0
    stage_text = 0
    first_data_lines = []
    stages_in_order = []
    buf = ""
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            while True:
                if time.time() - start > 25:
                    break
                chunk = r.read(512)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    total_lines += 1
                    if line.startswith("event:"):
                        et = line[6:].strip()
                        types[et] = types.get(et, 0) + 1
                        if et == "snapshot":
                            snapshots += 1
                        elif et == "progress":
                            progresses += 1
                        elif et == "message":
                            messages += 1
                        elif et == "stage_text":
                            stage_text += 1
                        elif et == "done":
                            done += 1
                    elif line.startswith("data:"):
                        if len(first_data_lines) < 12:
                            first_data_lines.append(line[:240])
                        try:
                            obj = json.loads(line[5:].strip())
                            s = obj.get("stage")
                            if (
                                isinstance(s, str)
                                and s
                                and (not stages_in_order or stages_in_order[-1] != s)
                            ):
                                stages_in_order.append(s)
                        except Exception:
                            pass
    except urllib.error.HTTPError as e:
        print(f"  HTTPError during SSE: {e.code} {e.reason}")
    except Exception as e:
        print(f"  ERROR during SSE: {type(e).__name__}: {e}")

    # [5] summary
    print("\n=== [5] SSE summary ===")
    print(f"  total_sse_framing_lines    = {total_lines}")
    print(f"  event_type_counts          = {types}")
    print(f"  snapshot={snapshots}  progress={progresses}  message={messages}  stage_text={stage_text}  done={done}")
    print(f"  stages in order            = {' -> '.join(stages_in_order)}")
    print(f"  first 12 SSE data lines:")
    for ln in first_data_lines:
        print(f"    - {ln}")

    # [6] tee log
    print("\n=== [6] tee log ===")
    print(f"  path={LOG}")
    print(f"  exists={os.path.exists(LOG)}")
    if os.path.exists(LOG):
        sz = os.path.getsize(LOG)
        print(f"  size_bytes={sz}")
        if sz > 0:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                lines_out = f.read().splitlines()
            print(f"  lines={len(lines_out)}")
            print("  last 8 lines:")
            for ln in lines_out[-8:]:
                print(f"    {ln[:240]}")
        else:
            print("  (zero bytes — mock runner did not tee: only qgis_process runner writes to this path)")


if __name__ == "__main__":
    main()
