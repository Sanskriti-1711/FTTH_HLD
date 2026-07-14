"""
Synthesize demo fixtures for the multipart shp+shx+dbf upload demo.

The frontend's "Road Network" dropzone accepts a multi-file upload: the user
selects the .shp + .shx + .dbf (and optionally .prj) and they are POSTed as
one multipart field per file via `roadsFiles.forEach((f) => fd.append('roads', f))`.
The backend groups them by basename stem on the server side and returns a
bundle summary chip in the UI:

  \u2713 <code>{shp}</code> \u00b7 {N} sidecars \u00b7 {KB} KB \u00b7 CRS detected

This script produces that bundle by shelling out to `ogr2ogr` (GDAL), which
ships in the active anaconda env (verified via `/version` -> has_ogr2ogr=True).
No pip-installs required, no manual binary laying-out.

OUTPUT folder:  tmp/hld-test/fixtures/
  - test_roads.shp        (geometry)
  - test_roads.shx        (shape index)
  - test_roads.dbf        (attribute table; fclass, name, highway)
  - test_roads.prj        (WGS84 .prj so the UI shows "CRS detected")
  - test_addresses.xlsx   (copied from tmp/hld-test/test_addresses.xlsx)

Then this script also probes the live backend at :8000 by POSTing the bundle
to /upload-roads and printing the response. (Not a substitute for the UI
demo \u2014 that's done via browser-use \u2014 but a fast sanity check.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # D:/Downloads_D/Q-GIS/HLD_Planning_01
SRC_GEOJSON = ROOT / "tmp" / "hld-test" / "test_roads.geojson"
SRC_XLSX = ROOT / "tmp" / "hld-test" / "test_addresses.xlsx"
OUT_DIR = ROOT / "tmp" / "hld-test" / "fixtures"
SHAPEFILE_BASENAME = "test_roads"

BACKEND_HOST = "http://127.0.0.1:8000"
UPLOAD_URL = f"{BACKEND_HOST}/upload-roads"


def find_ogr2ogr() -> str:
    """Cross-platform resolve of the ogr2ogr executable (handles .exe append)."""
    exe = shutil.which("ogr2ogr")
    if not exe:
        raise RuntimeError(
            "ogr2ogr not on PATH. The active Python env must have GDAL "
            "(e.g., anaconda3's GDAL install). Re-activate the env or install "
            "the 'libgdal' wheel."
        )
    return exe


def synthesize_shapefile_bundle(src_geojson: Path, out_dir: Path, basename: str) -> list[Path]:
    """Run ogr2ogr to write the ESRI Shapefile bundle (.shp/.shx/.dbf/.prj)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # -f "ESRI Shapefile"           target driver
    # {out_dir / basename + ".shp"}  output (basename drives the stem)
    # -nln {basename}                layer name in the .dbf (matches stem)
    # -a_srs EPSG:4326               force WGS84 so .prj is written
    # -overwrite                     idempotent at the layer level
    #
    # Wipe ALL stale sidecars first: ogr2ogr will only cleanly overwrite what
    # the layer creation recreates — a leftover .prj from a prior run with a
    # different SRS, or a dangling .shx whose offsets no longer correspond,
    # can survive an otherwise-clean invocation. Idempotent across stems.
    for stale in out_dir.glob(f"{basename}.*"):
        stale.unlink()
    target_shp = out_dir / f"{basename}.shp"

    cmd = [
        find_ogr2ogr(),
        "-f", "ESRI Shapefile",
        str(target_shp),
        str(src_geojson),
        "-nln", basename,
        "-a_srs", "EPSG:4326",
        "-overwrite",
    ]
    print(f"[fixture] $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ogr2ogr failed (rc={proc.returncode}):\n  stdout: {proc.stdout}\n  stderr: {proc.stderr}"
        )

    produced = sorted(p for p in out_dir.iterdir() if p.name.startswith(f"{basename}."))
    print(f"[fixture] ogr2ogr produced: {[p.name for p in produced]}", flush=True)
    return produced


def copy_xlsx(src_xlsx: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / src_xlsx.name
    shutil.copy2(src_xlsx, dst)
    print(f"[fixture] copied xlsx: {dst.name} ({dst.stat().st_size} bytes)", flush=True)
    return dst


def probe_upload(paths: list[Path], url: str) -> dict:
    """POST the bundle to /upload-roads and print the response."""
    boundary = "------HLDDemoBoundary7c2f3a"
    body_chunks: list[bytes] = []
    for p in paths:
        body_chunks.append(f"--{boundary}\r\n".encode())
        body_chunks.append(
            f'Content-Disposition: form-data; name="roads"; filename="{p.name}"\r\n'.encode()
        )
        body_chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        body_chunks.append(p.read_bytes())
        body_chunks.append(b"\r\n")
    body_chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(body_chunks)

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
            print(f"[probe] POST {url} -> HTTP {r.status}", flush=True)
            print(f"[probe] response keys: {sorted(payload.keys())}", flush=True)
            if payload.get("ok") is False:
                print(f"[probe] structured error: code={payload.get('code')!r} error={payload.get('error')!r}", flush=True)
                for k in ("missing", "offenders", "files", "sidecars", "found", "shps", "zips", "bundle", "hint"):
                    if payload.get(k) is not None:
                        print(f"[probe]   {k}: {payload[k]!r}", flush=True)
            else:
                if "bundle" in payload:
                    print(f"[probe] bundle: {payload['bundle']}", flush=True)
                if "feature_count" in payload:
                    print(f"[probe] feature_count: {payload['feature_count']}", flush=True)
            return payload
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"[probe] POST {url} -> HTTP {e.code}", flush=True)
        try:
            err_payload = json.loads(body_text)
            print(f"[probe] structured error: code={err_payload.get('code')!r} error={err_payload.get('error')!r}", flush=True)
            for k in ("missing", "offenders", "files", "sidecars", "hint"):
                if err_payload.get(k) is not None:
                    print(f"[probe]   {k}: {err_payload[k]!r}", flush=True)
            return err_payload
        except Exception:
            print(f"[probe] non-JSON body: {body_text[:400]!r}", flush=True)
            return {"http_error": e.code, "body_text": body_text}
    except urllib.error.URLError as e:
        print(f"[probe] connection failed: {e}", flush=True)
        return {"connection_error": str(e)}


def main() -> int:
    if not SRC_GEOJSON.exists():
        print(f"[fixture] missing source geojson: {SRC_GEOJSON}", flush=True)
        return 2
    if not SRC_XLSX.exists():
        print(f"[fixture] missing source xlsx: {SRC_XLSX}", flush=True)
        return 2

    bundle = synthesize_shapefile_bundle(SRC_GEOJSON, OUT_DIR, SHAPEFILE_BASENAME)
    xlsx = copy_xlsx(SRC_XLSX, OUT_DIR)

    # Sanity check: assert the 4 mandatory sidecars exist.
    required = {".shp", ".shx", ".dbf", ".prj"}
    have = {p.suffix for p in bundle}
    missing = required - have
    if missing:
        print(f"[fixture] WARNING: missing sidecars: {sorted(missing)}", flush=True)

    # Probe the bundle against the live backend so we know the green-chip
    # path works BEFORE we spend a browser session on it.
    print(f"\n[probe] live backend probe via direct curl-style POST ...", flush=True)
    probe_upload(sorted(bundle), UPLOAD_URL)

    print(f"\n[floor] demo fixtures ready at: {OUT_DIR}", flush=True)
    print(f"[floor] {chr(10).join(f'  - {p.name}  ({p.stat().st_size} bytes)' for p in sorted(OUT_DIR.iterdir()))}", flush=True)
    print(f"\n[floor] browser-use will next feed these paths into the UI dropzones.", flush=True)
    print(f"[floor] absolute paths (no spaces, JSON-safe) for browser-use file inputs:", flush=True)
    for p in sorted(bundle):
        print(f"  '{p.as_posix()}'", flush=True)
    print(f"  '{xlsx.as_posix()}'", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
