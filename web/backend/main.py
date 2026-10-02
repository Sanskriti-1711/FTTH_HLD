"""FTTH Engine API.

Runs the HLDPlanning QGIS plugin through oneclick.py/qgis_process, stores the
canonical outputs in PostGIS, and exposes GeoJSON, downloads, and optional MVT
tiles for MapLibre or any other client.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple, Union

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

import countries
import design
import occupancy
import osm_source
import postgis


APP_STARTED_AT = datetime.now(timezone.utc)
ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BACKEND_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_MESSAGES = 5000
# ADDR_ID truncation warnings flood the log (one per feature) and bury the
# real Cable Layer error under 500 identical messages — collapse them to a
# single counted entry so the failure stays visible.
_ADR_WARN_MARKERS = ("ADDR_ID", "exceeds maximum field length", "Value of field")
tasks: Dict[str, Dict[str, Any]] = {}

PIPELINE_STAGES = [
    "Object Layer",
    "Polygon Layer",
    "Network Layer",
    "Trench Layer",
    "Duct Layer",
    "Cable Layer",
]

ONECLICK_OUTPUTS: List[Tuple[str, str, str]] = [
    ("objects", "Objects.gpkg", "Objects.geojson"),
    ("polygons", "Polygons.gpkg", "Polygons.geojson"),
    ("pdps", "PDPs.gpkg", "PDPs.geojson"),
    ("mfg", "MFG.gpkg", "MFG.geojson"),
    ("mfg_service_areas", "MFG_Service_Areas.gpkg", "MFG_Service_Areas.geojson"),
    # ONE trench layer (user spec): the published trench network is a single
    # Final_Trenches layer whose features carry the construction sub-category
    # (Open Cut / HDD / Garden). The old per-tier Feeder/Distribution/Garden
    # sub-layer publications are gone — ducts and cables (which do keep their
    # tiers) ride inside the one trench per route.
    ("trenches", "Final_Trenches.gpkg", "Final_Trenches.geojson"),
    ("cables", "Feeder_Cable.gpkg", "Feeder_Cable.geojson"),
    ("cables", "Distribution_Cable.gpkg", "Distribution_Cable.geojson"),
    ("ducts", "Feeder_Ducts.gpkg", "Feeder_Ducts.geojson"),
    ("ducts", "Distribution_Ducts.gpkg", "Distribution_Ducts.geojson"),
    ("ducts", "Drop_Ducts.gpkg", "Drop_Ducts.geojson"),
    ("coupleurs", "Coupleurs.gpkg", "Coupleurs.geojson"),
    ("chambers", "Chambers.gpkg", "Chambers.geojson"),
    ("poles", "Poles.gpkg", "Poles.geojson"),
    # Aerial legs the trench stage classified: NOT excavated, so they are not
    # part of Final_Trenches. Their own layer carries TRENCH_TYPE="Aerial",
    # AERIAL_REASON (zone / length / chain) and EXCAVATION=0.
    ("aerial_drops", "Aerial_Drops.gpkg", "Aerial_Drops.geojson"),
    # What the pole + aerial stages BUILD from that classification: the drop
    # trench (pole -> premise) and its cable. They are a different thing from
    # Aerial_Drops (the designer's decision not to dig), so they publish
    # separately — before this the aerial chain ran and its output was
    # invisible on the platform because nothing served those two layers.
    # Overhead spans the pole + aerial stage BUILDS for the aerial legs the
    # trench stage classified. Renamed from `aerial_drop_trenches` /
    # `Aerial_Drop_Trenches.gpkg`: a trench is an excavation and these are not
    # (EXCAVATION=0, CONSTRUCTION_METHOD=Overhead), so the old name invited
    # reading them as civil trench. The old public name is still accepted as an
    # alias in postgis.LAYER_TABLES so a stored project keeps resolving.
    ("aerial_spans", "Aerial_Spans.gpkg",
     "Aerial_Spans.geojson"),
    ("aerial_cable", "Aerial_Cable.gpkg", "Aerial_Cable.geojson"),
    # NOTE: Trench_Nodes (the trench designer's STRUCTURAL NODES — the HDD
    # drill openings, junctions, splitter positions, bends and pull points) is
    # deliberately NOT published as a layer. It is an intermediate of the
    # design: every one of those points is realised by a feature in the
    # `chambers` layer below, which is what a reader of the HLD panel actually
    # needs. Serving it put ~125 undifferentiated dots on the map next to the
    # chambers they had already become, which is noise, not evidence. The file
    # is still produced and still ships in the Design Package ZIP.
    # (`design` in the LLD keeps it: there it is the designer's own working
    # output, not a finished layer.)
    ("brownfield", "Existing_Infrastructure.gpkg", "Existing_Infrastructure.geojson"),
    ("brownfield", "Existing_Infrastructure_Points.gpkg", "Existing_Infrastructure_Points.geojson"),
    # NOTE: the duct/cable occupancy registry is derived by occupancy.store()
    # during ingest and lives ONLY in the gis.duct_occupancy / gis.cable_occupancy
    # tables (brownfield-capacity read-back for a re-run / the LLD). It is not a
    # design layer, so it is not published as one — no map toggle, no download.
    # NOTE: BOQ.xlsx / BOM.xlsx are intentionally NOT listed here as layers —
    # they surface in the Downloads section via _register_downloads() instead.
]

DOWNLOAD_EXTS = {".gpkg", ".xlsx", ".csv", ".json", ".geojson", ".txt"}

# Which files prove each pipeline stage ran, in stage order.  The pipeline's
# own stage reporting comes from matching its stdout, which lives in memory --
# so an engine restart mid-run loses it, and the project row is left saying
# "running" forever with no stage.  The output files do not: they say exactly
# how far the design got.  This is what the status page falls back to.
_STAGE_OUTPUT_MARKERS: List[Tuple[str, List[str]]] = [
    ("Object Layer", ["Objects.gpkg"]),
    ("Polygon Layer", ["Polygons.gpkg"]),
    ("Network Layer", ["PDPs.gpkg", "MFG.gpkg"]),
    ("Trench Layer", ["Final_Trenches.gpkg"]),
    # Phase C cascade order (TRENCH_DESIGN.md §6.1): trench → chambers →
    # ducts → cables, so the duct files land before the cable files on disk
    # and the recovery walk below reads the run's real position.
    ("Duct Layer", ["Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg", "Drop_Ducts.gpkg"]),
    ("Cable Layer", ["Feeder_Cable.gpkg", "Distribution_Cable.gpkg"]),
]

# How long an orphaned run's files must be quiet before it is declared dead.
# An engine restart does NOT kill the qgis_process child: it kept writing this
# project's ducts for two hours afterwards, so "the owner is gone" cannot mean
# "failed" on sight -- the files are the only evidence either way.  Generous,
# because a single HLD stage can compute for a long time without writing.
ORPHAN_DEAD_SECONDS = int(os.environ.get("HLD_ORPHAN_DEAD_SECONDS", "1800"))
# A broken output is conclusive much faster: a GeoPackage that fails its
# integrity check cannot become valid by waiting, and a run whose last write is
# minutes old is not mid-write on it.
ORPHAN_BROKEN_SECONDS = int(os.environ.get("HLD_ORPHAN_BROKEN_SECONDS", "300"))

# Guards the recovery of one project: two status polls arriving together must
# not both ingest the outputs.
_recover_lock = threading.Lock()

app = FastAPI(
    title="FTTH Engine API",
    version="2.0.0",
    description="FastAPI backend for HLDPlanning one-click FTTH pipeline outputs.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _task(project_id: str) -> Dict[str, Any]:
    return tasks.setdefault(
        project_id,
        {
            "project_id": project_id,
            "status": "queued",
            "stage": None,
            "stage_index": 0,
            "stage_count": len(PIPELINE_STAGES),
            "progress": 0,
            "layers": [],
            "downloads": [],
            "messages": deque(maxlen=MAX_MESSAGES),
            "created_at": _now(),
            "updated_at": _now(),
        },
    )


def _public_task(project_id: str) -> Dict[str, Any]:
    task = dict(_task(project_id))
    messages = task.get("messages")
    task["messages"] = list(messages) if isinstance(messages, deque) else []
    task["results_url"] = f"/ftth/hld/results/{project_id}"
    task["tile_url_template"] = f"/tiles/{{layer}}/{{z}}/{{x}}/{{y}}.pbf?project_id={project_id}"
    # A run is only 100% when it is actually completed.  Anything else
    # (queued/running/failed/unknown, or a restored task) is capped at 99%
    # so the UI progress bar can never show a finished bar for an in-flight
    # pipeline — regardless of how the in-memory state was built.  Conversely
    # a genuinely completed run always reports 100 (in-memory rebuilds from
    # PostGIS don't carry the final progress value).
    if task.get("status") == "completed":
        task["progress"] = 100
    else:
        task["progress"] = min(int(task.get("progress") or 0), 99)
    return task


def _append(project_id: str, level: str, text: str) -> None:
    task = _task(project_id)
    # Collapse the per-feature ADDR_ID / ogr field-width warnings: they fire
    # once per feature (2-3k times) and push the real error out of the 500-
    # message deque. Keep the first occurrence and count the rest.
    if "ADDR_ID" in text and any(m in text for m in _ADR_WARN_MARKERS):
        cnt = int(task.get("_addr_warn_count") or 0) + 1
        task["_addr_warn_count"] = cnt
        if cnt == 1:
            task["messages"].append({"ts": _now(), "level": level, "text": text + " (further identical ADDR_ID warnings collapsed)"})
        else:
            # update the first warning in place to show the running total
            for msg in task["messages"]:
                if "ADDR_ID" in msg["text"] and "collapsed" in msg["text"]:
                    msg["text"] = f"ADDR_ID field-width warnings collapsed: {cnt} occurrences (first: {text[:140]})"
                    break
        task["updated_at"] = _now()
        return
    task["messages"].append({"ts": _now(), "level": level, "text": text})
    task["updated_at"] = _now()


def _safe_filename(name: str, fallback: str) -> str:
    clean = os.path.basename(name or fallback).strip()
    return clean or fallback


def _save_upload(upload: UploadFile, dest_dir: Path, fallback: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / _safe_filename(upload.filename or fallback, fallback)
    with path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return path


def _find_qgis_process() -> Optional[str]:
    override = os.environ.get("QGIS_EXECUTABLE", "").strip()
    if override and os.path.isfile(override):
        return os.path.abspath(override)
    # Windows: only the official QGIS launcher BATs are safe. They call
    # o4w_env.bat, which sets every DLL search path qgis_process.exe needs;
    # launching the .exe directly (or via a hand-rolled env) crashes at
    # startup with 0xC0000135 (STATUS_DLL_NOT_FOUND). The engine's
    # _run_command prepends Python312's site-packages to PYTHONPATH and the
    # official BAT appends (never replaces) it, so pandas stays importable.
    if os.name == "nt":
        prog_files = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                      r"C:\Program Files (x86)"]
        bases: List[str] = []
        for prog in prog_files:
            try:
                bases.extend(
                    os.path.join(prog, d) for d in os.listdir(prog)
                    if d.lower().startswith(("qgis", "osgeo4w"))
                )
            except OSError:
                continue
        bases.append(r"C:\OSGeo4W64")
        launcher_names = ("qgis_process-qgis.bat", "qgis_process.bat", "qgis_process.cmd")
        for base in bases:
            for sub in ("bin", os.path.join("apps", "qgis", "bin")):
                for name in launcher_names:
                    cand = os.path.join(base, sub, name)
                    if os.path.isfile(cand):
                        return cand
        # Last resort: a bounded walk that only accepts launcher BATs.
        for base in bases:
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for filename in files:
                    lower = filename.lower()
                    if lower.startswith("qgis_process") and lower.endswith((".bat", ".cmd")):
                        return os.path.join(root, filename)
        return None
    for name in ("qgis_process-qgis", "qgis_process"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _quote_cmd_arg(arg: str) -> str:
    if not any(ch.isspace() for ch in arg) and not any(ch in arg for ch in ['"', "&", "(", ")", "^"]):
        return arg
    return '"' + arg.replace('"', r'\"') + '"'


def _run_command(project_id: str, cmd: Union[List[str], str], output_dir: Path) -> None:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("QGIS_PLUGINPATH", str(ROOT_DIR))
    # Ensure qgis_process can find pandas/numpy/etc. from QGIS's Python312
    _qgispython = str(ROOT_DIR / "HLDPlanning" / "python")  # fallback
    _py312site = r"C:\Program Files\QGIS 3.44.6\apps\Python312\Lib\site-packages"
    if os.path.isdir(_py312site) and _py312site not in env.get("PYTHONPATH", ""):
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = _py312site + (";" + existing if existing else "")
    # Line-buffer stdout so plugin progress reaches the API logger
    # instead of waiting for the kernel buffer to fill or process exit.
    env.setdefault("PYTHONUNBUFFERED", "1")

    _append(project_id, "info", "$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    process = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        shell=isinstance(cmd, str),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    # Stream stdout line-by-line so log messages appear in real-time
    # (Nominatim geocoding takes 1.2s per address — without streaming,
    # the frontend sees 0% until the entire pipeline finishes.)
    for line in iter(process.stdout.readline, ""):
        text = line.strip()
        if not text:
            continue
        _append(project_id, "info", text)
        for idx, stage in enumerate(PIPELINE_STAGES):
            if stage.lower() in text.lower():
                task = _task(project_id)
                if task.get("stage") == stage and task.get("stage_index") == idx:
                    continue
                task["stage"] = stage
                task["stage_index"] = idx
                task["progress"] = int((idx / len(PIPELINE_STAGES)) * 100)
                task["updated_at"] = _now()
                # Persisted, not just in memory: this is the only record of
                # where the run was when the engine restarts, and it is what the
                # platform's project list shows while the run is in flight.
                _persist_project_state(project_id, task)

    process.stdout.close()
    timeout = int(os.environ.get("QGIS_PROCESS_TIMEOUT", "10800"))
    try:
        rc = process.wait(timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"qgis_process exited with code {rc}")
        _append(project_id, "info", f"qgis_process finished; outputs in {output_dir}")
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            process.kill()
        raise RuntimeError(f"qgis_process timed out after {timeout} seconds")


def _convert_gpkg_to_geojson(gpkg_path: Path, geojson_path: Path) -> bool:
    if not gpkg_path.exists():
        return False
    if geojson_path.exists():
        geojson_path.unlink()
    ogr2ogr = shutil.which("ogr2ogr")
    if not ogr2ogr:
        return False
    # Reproject to WGS84 (EPSG:4326): MapLibre/Leaflet render GeoJSON as
    # lon/lat, so serving projected (e.g. EPSG:25833) coordinates puts every
    # feature off the map. The PostGIS path already transforms on ingest.
    result = subprocess.run(
        [ogr2ogr, "-f", "GeoJSON", "-t_srs", "EPSG:4326", str(geojson_path), str(gpkg_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode == 0 and geojson_path.exists()


def _ensure_geojson(gpkg_path: Path, geojson_path: Path) -> bool:
    """Export the GeoJSON unless an up-to-date one already exists.

    A pipeline stage rewrites its GPKG in place AFTER the per-stage GeoJSON
    export (the attribute-enrichment pass in particular), and a re-run rewrites
    the GPKG again. So existence is NOT freshness: an old GeoJSON next to a
    newer GPKG is the previous run's geometry, and serving it means the map
    shows the previous design no matter what the new run produced
    (project 0dc85304 served its pre-fix feeder ducts for hours after the
    re-run because this check only asked whether the file existed).
    """
    if not gpkg_path.exists():
        return geojson_path.exists()
    if geojson_path.exists():
        try:
            if geojson_path.stat().st_mtime >= gpkg_path.stat().st_mtime:
                return True
        except OSError:
            pass
    return _convert_gpkg_to_geojson(gpkg_path, geojson_path)


def _register_downloads(project_id: str, output_dir: Path) -> List[Dict[str, Any]]:
    downloads: List[Dict[str, Any]] = []
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in DOWNLOAD_EXTS:
            continue
        rel = path.relative_to(output_dir).as_posix()
        downloads.append(
            {
                "name": rel,
                "url": f"/ftth/hld/download/{project_id}/{rel}",
                "size_bytes": path.stat().st_size,
            }
        )
    return sorted(downloads, key=lambda item: item["name"])


def _gpkg_is_intact(path: Path) -> bool:
    """Is this GeoPackage a finished, readable file?

    A GeoPackage is a SQLite file, so this needs no GDAL and no new dependency.
    A pipeline killed mid-write leaves a journal (or WAL) beside the file --
    SQLite has to roll it back before the file can be read, and a read-only
    open cannot -- or a file whose pages are malformed.  Both are what a
    half-written output looks like, and both are cheap to detect: a full
    ``PRAGMA quick_check`` on every output was slow enough to time out the
    project list.
    """
    if not path.is_file():
        return False
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-journal", "-wal")):
        return False
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        # Touches the real pages: a truncated or malformed file raises here.
        conn.execute("SELECT count(*) FROM gpkg_contents").fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _stage_from_outputs(output_dir: Path) -> Optional[Dict[str, Any]]:
    """How far the pipeline got, according to the files it wrote.

    Returns the stage being worked on, the last stage that finished, whether
    every stage's files are there, which files are missing or damaged, and when
    the run last wrote anything.  ``None`` when the directory holds no output.
    """
    if not output_dir.is_dir():
        return None
    files = [p for p in output_dir.iterdir() if p.is_file()]
    if not files:
        return None
    newest = max(p.stat().st_mtime for p in files)
    stages: List[Dict[str, Any]] = []
    last_complete = -1
    missing: List[str] = []
    damaged: List[str] = []
    for idx, (name, markers) in enumerate(_STAGE_OUTPUT_MARKERS):
        gone = [m for m in markers if not (output_dir / m).is_file()]
        stages.append({"index": idx, "name": name, "complete": not gone, "missing": gone})
        if gone:
            missing.extend(gone)
        else:
            last_complete = idx
    for name in _marker_names():
        path = output_dir / name
        if path.is_file() and not _gpkg_is_intact(path):
            damaged.append(name)
    # The stage to report: the one in progress, or -- when every stage's files
    # are present -- the last stage, because that is what the run was on.
    current = min(last_complete + 1, len(_STAGE_OUTPUT_MARKERS) - 1)
    complete = last_complete == len(_STAGE_OUTPUT_MARKERS) - 1 and not missing
    return {
        "stage_index": current,
        "stage_name": _STAGE_OUTPUT_MARKERS[current][0],
        "progress": int(round(((last_complete + 1) / len(_STAGE_OUTPUT_MARKERS)) * 100)),
        "last_complete_stage": (
            _STAGE_OUTPUT_MARKERS[last_complete][0] if last_complete >= 0 else None
        ),
        "all_stages_written": complete,
        "missing": sorted(set(missing)),
        "damaged": sorted(set(damaged)),
        "newest_write": newest,
        "stages": stages,
    }


def _marker_names() -> List[str]:
    return [name for _stage, markers in _STAGE_OUTPUT_MARKERS for name in markers]


def _recover_orphan_run(
    project_id: str, task: Dict[str, Any], publish: bool = True
) -> None:
    """Settle a run whose owner is gone, from the files it left behind.

    A project row says "running"; no thread in this process is running it.  That
    happens when the engine restarts mid-run -- the background task dies with
    it, the row keeps its last write, and the status page then shows a run that
    is neither alive nor finished.  The qgis_process child usually SURVIVES the
    restart, so "the owner is gone" is not on its own proof of failure: the
    outputs are.  So:

      * files still changing  -> keep reporting the stage they show, as running
      * every stage written and every file intact -> complete it for real, so
        the outputs page can show it
      * otherwise -> failed, naming the stage it reached and what is damaged

    ``publish=False`` settles the row without the expensive ingest; the results
    endpoint then restores the layers from disk.  The project list uses that,
    because a list of twenty projects must not ingest twenty designs.
    """
    if task.get("status") not in ("running", "queued"):
        return
    output_dir = OUTPUT_DIR / project_id
    info = _stage_from_outputs(output_dir)
    if info is None:
        # Nothing on disk: the run never got as far as writing an output.
        _settle_orphan(
            project_id, task, "failed",
            "The engine restarted before this run wrote any output. Run it again.",
            stage_name="Resolve area", stage_index=0, progress=0,
        )
        return

    # Always report the stage the FILES show, not the one the lost thread
    # claimed: that is the whole point of the status page after a restart.
    task.update({
        "stage": info["stage_name"],
        "stage_index": info["stage_index"],
        "progress": info["progress"],
        "updated_at": _now(),
    })
    quiet_for = max(0.0, time.time() - info["newest_write"])

    if info["damaged"] and quiet_for > ORPHAN_BROKEN_SECONDS:
        # Name the stage that OWNS the damaged file — the run broke while
        # writing that stage's output. The file walk reports the furthest
        # stage whose files merely EXIST, which since the duct/cable cascade
        # reorder can be a LATER stage than the broken file's owner (duct
        # files land before cable files, so a broken duct file coexists with
        # a complete cable set).
        broke_stage, broke_index = info["stage_name"], info["stage_index"]
        for idx, (name, markers) in enumerate(_STAGE_OUTPUT_MARKERS):
            if any(m in info["damaged"] for m in markers):
                broke_stage, broke_index = name, idx
                break
        _settle_orphan(
            project_id, task, "failed",
            f"This run was interrupted during the {broke_stage} and its output "
            f"is incomplete ({', '.join(info['damaged'])}). The layers that were built "
            "are shown below; run the area again for a complete design.",
            stage_name=broke_stage, stage_index=broke_index,
            progress=info["progress"],
            damaged=info["damaged"], publish=publish,
        )
        return

    if quiet_for <= ORPHAN_DEAD_SECONDS:
        # The pipeline process may still be alive and writing (it outlives an
        # engine restart), so this is a run in progress as far as the evidence
        # goes -- reported with the stage its files show.
        return

    if info["all_stages_written"] and not info["damaged"]:
        if publish:
            _complete_orphan(project_id, task, info)
        else:
            # Row only: the results endpoint restores the layers from disk when
            # this project is opened, so the list stays cheap.
            _settle_orphan(
                project_id, task, "completed", None,
                stage_name="Complete", stage_index=len(PIPELINE_STAGES), progress=100,
            )
        return

    reached = info["last_complete_stage"] or "no"
    missing = (", " + ", ".join(info["missing"])) if info["missing"] else ""
    _settle_orphan(
        project_id, task, "failed",
        f"This run was interrupted after the {reached}{missing}. The engine restarted "
        "while the design was still being built, so it never finished. The layers "
        "that were built are shown below; run the area again for a complete design.",
        stage_name=info["stage_name"], stage_index=info["stage_index"],
        progress=info["progress"],
        damaged=info["damaged"], publish=publish,
    )


def _complete_orphan(project_id: str, task: Dict[str, Any], info: Dict[str, Any]) -> None:
    """Publish a finished run whose completion write was lost with the thread."""
    with _recover_lock:
        if task.get("status") not in ("running", "queued"):
            return
        output_dir = OUTPUT_DIR / project_id
        _append(
            project_id, "info",
            "Every pipeline output is on disk and intact, so this run was completed "
            "after the engine restarted mid-run.",
        )
        layers = _ingest_outputs(project_id, output_dir)
        downloads = _register_downloads(project_id, output_dir)
        task.update({
            "status": "completed",
            "stage": "Complete",
            "stage_index": len(PIPELINE_STAGES),
            "progress": 100,
            "layers": layers,
            "downloads": downloads,
            "runner": task.get("runner") or "qgis_process",
            "recovered_from_disk": True,
            "updated_at": _now(),
        })
        _persist_project_state(project_id, task, downloads=downloads)


def _settle_orphan(
    project_id: str,
    task: Dict[str, Any],
    status: str,
    error: Optional[str],
    *,
    stage_name: str,
    stage_index: int,
    progress: int,
    damaged: Optional[List[str]] = None,
    publish: bool = False,
) -> None:
    """Put an orphaned run into a terminal state, in memory and in PostGIS.

    An interrupted run's INTACT layers are still published, so the outputs page
    shows the design as far as it got instead of an empty map.  The damaged
    files are excluded and named, rather than served and crashed on.
    """
    with _recover_lock:
        if task.get("status") not in ("running", "queued"):
            return
        task.update({
            "status": status,
            "error": error,
            "stage": stage_name,
            "stage_index": stage_index,
            "progress": progress,
            "recovered_from_disk": True,
            "updated_at": _now(),
        })
        if damaged:
            task["damaged_outputs"] = sorted(set(damaged))
        if publish and status == "failed":
            try:
                task["layers"] = _ingest_outputs(
                    project_id, OUTPUT_DIR / project_id, skip=set(damaged or [])
                )
            except Exception as exc:  # noqa: BLE001 - the status is what matters
                _append(project_id, "warning", f"Partial layers not published: {exc}")
        if error:
            _append(project_id, "error" if status == "failed" else "info", error)
        _persist_project_state(project_id, task)


def _persist_project_state(
    project_id: str,
    task: Dict[str, Any],
    downloads: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write a run's status/stage/progress to PostGIS, best-effort.

    The platform's project list and the outputs page read the row, so a stage
    that only exists in this process's memory is invisible everywhere else --
    and invisible again after the next restart.
    """
    if not postgis.is_available():
        return
    status = str(task.get("status") or "unknown")
    progress = int(task.get("progress") or 0)
    # Only a completed run is 100 %: a failed one that had written every stage
    # before it broke would otherwise sit in the project list looking finished.
    if status != "completed":
        progress = min(progress, 99)
    try:
        postgis.upsert_project(
            project_id,
            status=status,
            roads_filename=task.get("roads_filename"),
            runner=task.get("runner"),
            error=task.get("error"),
            output_dir=task.get("output_dir") or str(OUTPUT_DIR / project_id),
            downloads=downloads,
            progress=progress,
            stage_name=task.get("stage") or "",
            stage_index=int(task.get("stage_index") or 0),
            stage_count=int(task.get("stage_count") or len(PIPELINE_STAGES)),
        )
    except Exception:  # noqa: BLE001 - never fail a status read on a write
        pass


def _owned_in_process(project_id: str) -> bool:
    """Is a pipeline thread in THIS process running this project?

    The task registry is memory-only, so a task that exists here is only proof
    of a live run when THIS process put it there: a task rebuilt from the
    project row (after a restart) is a run whose thread died with the engine.
    ``_from_db_row`` is that distinction.
    """
    task = tasks.get(project_id)
    if task is None or task.get("_from_db_row"):
        return False
    return not task.get("recovered_from_disk")


def _recover_orphan_from_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Settle an orphaned run described by a PostGIS project row."""
    project_id = str(row.get("project_id") or "")
    if not project_id or _owned_in_process(project_id):
        return None
    task = _task(project_id)
    task.update({
        "status": row.get("status"),
        "runner": row.get("runner"),
        "roads_filename": row.get("roads_filename"),
        "error": row.get("error"),
        "output_dir": row.get("output_dir") or str(OUTPUT_DIR / project_id),
        "downloads": row.get("downloads") or [],
        "_from_db_row": True,
    })
    # The list must stay cheap: settle the row, leave the ingest to the results
    # endpoint (which restores the layers from disk).
    _recover_orphan_run(project_id, task, publish=False)
    return task


def _restore_task_from_disk(project_id: str) -> Optional[Dict[str, Any]]:
    """Rebuild an in-memory task from output files already on disk.

    The task registry is memory-only, so an engine restart loses every run.
    PostGIS reloads them when available; this is the disk fallback for local
    dev / deployments without a database. Returns the restored task or None
    when no outputs exist for the project.
    """
    output_dir = OUTPUT_DIR / project_id
    if not output_dir.is_dir():
        return None

    # Files an interrupted run left half-written are NOT published: serving one
    # to the results map breaks the page instead of showing the rest of the
    # design.  Cheap now -- a journal beside the file, or an unreadable page.
    info = _stage_from_outputs(output_dir)
    damaged = set(info["damaged"]) if info else set()

    layer_files: Dict[str, List[str]] = {}
    for public_layer, gpkg_name, geojson_name in ONECLICK_OUTPUTS:
        if gpkg_name in damaged:
            continue
        if gpkg_name.lower().endswith(".xlsx"):
            path = output_dir / gpkg_name
            if path.is_file():
                layer_files.setdefault(public_layer, []).append(str(path))
            continue
        geojson_path = output_dir / geojson_name
        gpkg_path = output_dir / gpkg_name
        # Freshness, not existence: a restored project must serve the CURRENT
        # run's geometry, never a stale GeoJSON left beside a newer GPKG.
        _ensure_geojson(gpkg_path, geojson_path)
        if geojson_path.exists():
            layer_files.setdefault(public_layer, []).append(str(geojson_path))
        elif gpkg_path.exists():
            layer_files.setdefault(public_layer, []).append(str(gpkg_path))

    if not layer_files:
        return None

    # A task that already existed in the registry is owned by a pipeline thread
    # (or was queued by one) — a task created right here is a project known only
    # from its files (engine restart / no DB).
    existed = project_id in tasks
    task = _task(project_id)
    # NEVER override an in-flight run. The results map fetches its layers while
    # the pipeline is still working, and at that moment none of the output files
    # are registered on the task yet — so a restore would force the run to
    # "completed / 100 % / Complete" the instant anyone opened a layer, while the
    # project row (and the real pipeline) were still running. A live task only
    # receives the on-disk file paths it is missing; status, stage, progress and
    # the message log stay exactly as the pipeline reported them.
    if existed and task.get("status") in ("running", "queued"):
        merged_files = dict(layer_files)
        merged_files.update(task.get("files") or {})
        task["files"] = merged_files
        if not (task.get("downloads") or []):
            task["downloads"] = _register_downloads(project_id, output_dir)
        task.setdefault("output_dir", str(output_dir))
        return task
    if existed and task.get("status") == "failed":
        # A FAILED run keeps its status here. The restore used to force it to
        # "completed / 100 %", which turned an interrupted run into a finished
        # one the moment the outputs page was opened — the one place a planner
        # looks to find out what happened.  Its layers are still attached, so
        # the design that was built stays visible.
        task["files"] = layer_files
        task["layers"] = [
            {"name": layer, "feature_count": None, "geometry_type": None, "files": files}
            for layer, files in sorted(layer_files.items())
        ]
        if damaged:
            task["damaged_outputs"] = sorted(damaged)
        task.setdefault("output_dir", str(output_dir))
        task.setdefault("restored_from_disk", True)
        return task
    task.update(
        {
            "status": "completed",
            "stage": "Complete",
            "stage_index": len(PIPELINE_STAGES),
            "stage_count": len(PIPELINE_STAGES),
            "progress": 100,
            "files": layer_files,
            "layers": [
                {"name": layer, "feature_count": None, "geometry_type": None, "files": files}
                for layer, files in sorted(layer_files.items())
            ],
            "downloads": _register_downloads(project_id, output_dir),
            "output_dir": str(output_dir),
            "restored_from_disk": True,
            "updated_at": _now(),
        }
    )
    return task


def _ingest_outputs(
    project_id: str,
    output_dir: Path,
    skip: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    has_postgis = postgis.is_available()
    if has_postgis:
        postgis.init_schema()
        postgis.clear_project_layers(project_id)
    # Grouped public layers (ducts = feeder + distribution + drop, cables =
    # feeder + distribution) are published as several files. Tag each file's
    # features with its own sub-layer so the tier survives the single shared
    # GIS table and the results map can expose the tiers as separate toggles.
    _grouped_counts = Counter(public for public, _g, _j in ONECLICK_OUTPUTS)
    layer_files: Dict[str, List[str]] = {}

    for public_layer, gpkg_name, geojson_name in ONECLICK_OUTPUTS:
        # `skip` carries files an interrupted run left half-written: they are
        # published by every other route, and serving one to the results map
        # would 500 the page instead of showing the rest of the design.
        if skip and gpkg_name in skip:
            continue
        gpkg_path = output_dir / gpkg_name
        geojson_path = output_dir / geojson_name
        # Handle report files (.xlsx) that aren't vector layers
        if gpkg_name.lower().endswith(".xlsx"):
            if gpkg_path.exists():
                layer_files.setdefault(public_layer, []).append(str(gpkg_path))
            continue
        # Re-convert when the GPKG is newer than the GeoJSON (see
        # _ensure_geojson): stages like the attribute-enrichment pass rewrite
        # the GPKGs in place AFTER the per-stage GeoJSON exports, so a re-run
        # must not ingest the stale previous run's GeoJSON.
        _ensure_geojson(gpkg_path, geojson_path)
        if geojson_path.exists():
            layer_files.setdefault(public_layer, []).append(str(geojson_path))
            if has_postgis:
                sublayer = (
                    geojson_path.stem
                    if _grouped_counts.get(public_layer, 0) > 1
                    else None
                )
                inserted = postgis.load_geojson_file(
                    project_id,
                    public_layer,
                    str(geojson_path),
                    replace=False,
                    sublayer=sublayer,
                )
                _append(project_id, "info", f"Loaded {inserted} features into {public_layer}.")
        elif gpkg_path.exists():
            layer_files.setdefault(public_layer, []).append(str(gpkg_path))

    # ── occupancy registry (DB-only, after the layers are on disk) ────────
    # Stored at the END: the registry is derived from the duct/cable outputs,
    # and the loop above is what turns each stage's GeoPackage into the GeoJSON
    # the derivation reads — running it first found no files and stored 0 rows.
    # Best-effort: a missing registry must never fail a completed run.
    try:
        occ = occupancy.store(output_dir, project_id)
        if occ.get("ducts") or occ.get("cables"):
            _append(
                project_id,
                "info",
                "Occupancy registry stored: "
                f"{occ.get('ducts', 0)} duct row(s), "
                f"{occ.get('cables', 0)} cable row(s).",
            )
    except Exception as exc:  # noqa: BLE001
        _append(project_id, "warning", f"Occupancy registry skipped: {exc}")

    task = _task(project_id)
    task["files"] = layer_files
    if has_postgis:
        return postgis.list_project_layers(project_id)
    return [
        {"name": layer, "feature_count": None, "geometry_type": None, "files": files}
        for layer, files in sorted(layer_files.items())
    ]


_BF_VECTOR_EXTS = {".geojson", ".gpkg", ".shp", ".json"}


def _match_brownfield_param(filename: str) -> Optional[str]:
    """Map an uploaded brownfield file to its plugin BF_* parameter by name.

    Specific patterns are matched first so e.g. ``bf_feeder_trench.geojson``
    hits BF_FEEDER_TRENCH and not the generic BF_TRENCHES.
    """
    lower = filename.lower()
    if "duct" in lower:
        return "BF_DUCTS"
    if "chamber" in lower:
        return "BF_CHAMBERS"
    if "pole" in lower:
        return "BF_POLES"
    if "fibre" in lower or "fiber" in lower:
        return "BF_FIBRE"
    if "cabinet" in lower:
        return "BF_CABINETS"
    if "feeder" in lower and "trench" in lower:
        return "BF_FEEDER_TRENCH"
    if "dist" in lower and "trench" in lower:
        return "BF_DIST_TRENCH"
    if "pdp" in lower:
        return "BF_EXISTING_PDP"
    if "mfg" in lower:
        return "BF_EXISTING_MFG"
    if "trench" in lower:
        return "BF_TRENCHES"
    return None


def _brownfield_args(brownfield_path: Optional[Path], output_dir: Path) -> List[str]:
    """Unzip an uploaded brownfield archive and build the plugin BF_* params.

    Returns qgis_process ``--`` style args (e.g. ``USE_BROWNFIELD=true``,
    ``BF_DUCTS=<path>``) or an empty list when no archive was supplied.
    """
    if not brownfield_path or not brownfield_path.exists():
        return []
    bf_dir = output_dir / "brownfield"
    bf_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(brownfield_path):
        with zipfile.ZipFile(brownfield_path) as zf:
            base = str(bf_dir.resolve())
            for member in zf.namelist():
                dest = (bf_dir / member).resolve()
                if not str(dest).startswith(base):
                    continue  # skip path-traversal entries
                zf.extract(member, bf_dir)
    else:
        shutil.copy2(brownfield_path, bf_dir / brownfield_path.name)

    matches: Dict[str, str] = {}
    # Prefer GeoJSON > GPKG > JSON > SHP when a zip ships the same asset
    # in several formats.
    priority = {".geojson": 3, ".gpkg": 2, ".json": 1, ".shp": 0}
    for fp in bf_dir.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in _BF_VECTOR_EXTS:
            continue
        param = _match_brownfield_param(fp.name)
        if not param:
            continue
        prev = matches.get(param)
        if prev is None or priority[fp.suffix.lower()] > priority[Path(prev).suffix.lower()]:
            matches[param] = str(fp)

    args: List[str] = []
    if matches:
        args.append("USE_BROWNFIELD=true")
        for param in sorted(matches):
            args.append(f"{param}={matches[param]}")
    return args


# ── OSM reference layers → design constraints ─────────────────────────────
# The platform's upload form has always ACCEPTED railways / waterways / water /
# landuse / natural and stored them under inputs/osm/<key>/ — but nothing
# consumed them ("NOT consumed by the design algorithm yet"). They now feed
# the two places that can act on them:
#
#   POLY_BARRIER_EXTRA  → the polygon growth stage's barrier rule (every
#                         feature of the extra layers is a barrier the service
#                         polygons must not grow across: railways, rivers,
#                         water, natural)
#   AERIAL_ZONES        → the trench stage's aerial classification (restricted
#                         landuse the designer derives zones from — the same
#                         derivation the on-demand designer runs)
#
# A DESIGN_INPUT_MAP is also written next to the design so a reviewer (and the
# log line above) can see exactly which constraints the run carried.
OSM_BARRIER_KEYS = ("railways", "waterways", "water", "natural")
OSM_AERIAL_KEY = "landuse"


def _vector_layer_arg(path: Path) -> Optional[str]:
    """A qgis_process-ready path for an uploaded OSM layer (zip or file).

    qgis_process resolves processing layer parameters as plain paths, so a
    shapefile inside an archive is EXTRACTED to ``<key>/extracted/`` first
    (sidecars included) and its .shp path returned — /vsizip/ works for
    ogr.Open but is not reliably loadable as a QgsProcessingParameterVectorLayer.
    A plain GeoJSON/GPKG passes through; None means the upload is unreadable.
    """
    if not path or not path.exists():
        return None
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return _extract_zip_layer(path)
    if suffix in {".geojson", ".json", ".gpkg"}:
        return path.as_posix()
    return None


def _extract_zip_layer(zip_path: Path, prefer: Optional[str] = None) -> Optional[str]:
    """Extract the vector layer from an archive next to it and return its path.

    Idempotent: re-running a project reuses the previously extracted copy.
    Returns None when the archive holds no readable vector layer.
    """
    target = zip_path.parent / "extracted"
    # Already extracted once?
    prior = sorted(target.glob("*.geojson")) + sorted(target.glob("*.gpkg")) \
        + sorted(target.glob("*.shp"))
    if prior:
        if prefer:
            for p in prior:
                if prefer in p.name.lower():
                    return p.as_posix()
        return prior[0].as_posix()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            base = str(zip_path.resolve())
            wanted = [n for n in names if n.lower().endswith(
                (".shp", ".dbf", ".shx", ".prj", ".cpg", ".qpj",
                 ".geojson", ".json", ".gpkg"))]
            if not wanted:
                return None
            target.mkdir(parents=True, exist_ok=True)
            for member in wanted:
                dest = (target / Path(member).name)
                with zf.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    out_files = sorted(target.glob("*.geojson")) + sorted(target.glob("*.gpkg")) \
        + sorted(target.glob("*.shp"))
    if not out_files:
        return None
    if prefer:
        for p in out_files:
            if prefer in p.name.lower():
                return p.as_posix()
    return out_files[0].as_posix()


def _osm_constraint_args(
    output_dir: Path, project_id: str, log: bool = True
) -> List[str]:
    """POLY_BARRIER_EXTRA=… / AERIAL_ZONES=… args from inputs/osm/.

    Every entry the platform accepted is considered; missing or unreadable
    ones are logged and skipped (an optional constraint must never fail the
    run — the layers are evidence the planner supplied, not required input).
    """
    osm_dir = output_dir / "inputs" / "osm"
    if not osm_dir.is_dir():
        return []
    barrier_paths: List[str] = []
    for key in OSM_BARRIER_KEYS:
        if not (osm_dir / key).is_dir():
            continue
        resolved = None
        for candidate in sorted((osm_dir / key).iterdir()):
            if candidate.is_file() and candidate.name != "design_inputs.json":
                resolved = _vector_layer_arg(candidate)
                if resolved:
                    break
        if resolved:
            barrier_paths.append(resolved)
            if log:
                _append(project_id, "info",
                       f"OSM barrier input '{key}' feeding POLY_BARRIER_EXTRA.")
        elif log:
            _append(project_id, "warn",
                    f"OSM input '{key}' is not a readable vector layer — "
                    "skipped (constraints unaffected).")

    aerial_path: Optional[str] = None
    landuse_dir = osm_dir / OSM_AERIAL_KEY
    if landuse_dir.is_dir():
        for candidate in sorted(landuse_dir.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() == ".zip":
                aerial_path = _extract_zip_layer(candidate, prefer=OSM_AERIAL_KEY)
                if aerial_path:
                    break
                continue
            if candidate.suffix.lower() in {".geojson", ".json", ".gpkg"}:
                aerial_path = candidate.as_posix()
                break

    args: List[str] = []
    if barrier_paths:
        for p in barrier_paths:
            args.append(f"POLY_BARRIER_EXTRA={p}")
    if aerial_path:
        args.append(f"AERIAL_ZONES={aerial_path}")
        if log:
            _append(project_id, "info",
                    "OSM landuse input feeding AERIAL_ZONES — restricted "
                    "landuse classifies non-diggable drop legs aerial.")

    if args:
        _write_design_input_map(output_dir, barrier_paths, aerial_path)
    elif log:
        _append(project_id, "info",
                "No OSM constraint inputs (or none readable) — polygon "
                "barrier rule uses the road classes only.")
    return args


def _write_design_input_map(
    output_dir: Path, barrier_paths: List[str], aerial_path: Optional[str]
) -> None:
    """Record which constraints a run carried — beside the outputs, where a
    reviewer looking at the finished design can find it."""
    try:
        payload = {
            "poly_barrier_extra": list(barrier_paths),
            "aerial_zones": aerial_path,
            "note": (
                "OSM reference layers bound to the design: barriers gate the "
                "polygon growth rule; landuse derives aerial zones for the "
                "trench stage's non-diggable drop classification."
            ),
        }
        (output_dir / "design_inputs.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _run_pipeline(
    project_id: str,
    excel_path: Path,
    roads_path: Path,
    output_dir: Path,
    poly_method: int = 3,
    brownfield_path: Optional[Path] = None,
) -> None:
    task = _task(project_id)
    task.update({
        "status": "running", "stage": PIPELINE_STAGES[0], "stage_index": 0,
        "progress": 0, "updated_at": _now(),
    })
    if postgis.is_available():
        postgis.init_schema()
        postgis.upsert_project(
            project_id,
            status="running",
            roads_filename=roads_path.name,
            output_dir=str(output_dir),
            stage_name=PIPELINE_STAGES[0],
            stage_index=0,
            stage_count=len(PIPELINE_STAGES),
            progress=0,
        )

    try:
        qgis = _find_qgis_process()
        if not qgis:
            raise RuntimeError(
                "qgis_process was not found. Set QGIS_EXECUTABLE or add QGIS bin to PATH."
            )
        cmd = [
            qgis,
            "run",
            "hldplanning:end_to_end_pipeline",
            "--",
            f"EXCEL={excel_path}",
            f"ROADS={roads_path}",
            f"OUTPUT_DIR={output_dir}",
            f"POLY_METHOD={int(poly_method or 3)}",
        ]
        bf_args = _brownfield_args(brownfield_path, output_dir)
        if bf_args:
            cmd.extend(bf_args)
            _append(project_id, "info", "Brownfield upload detected; enabling reuse.")
        osm_args = _osm_constraint_args(output_dir, project_id)
        if osm_args:
            cmd.extend(osm_args)
        if os.name == "nt" and qgis.lower().endswith((".bat", ".cmd")):
            cmd = " ".join(_quote_cmd_arg(part) for part in cmd)

        _run_command(project_id, cmd, output_dir)
        layers = _ingest_outputs(project_id, output_dir)
        downloads = _register_downloads(project_id, output_dir)

        task.update(
            {
                "status": "completed",
                "stage": "Complete",
                "stage_index": len(PIPELINE_STAGES),
                "progress": 100,
                "layers": layers,
                "downloads": downloads,
                "runner": "qgis_process",
                "updated_at": _now(),
            }
        )
        if postgis.is_available():
            postgis.upsert_project(
                project_id,
                status="completed",
                roads_filename=roads_path.name,
                runner="qgis_process",
                output_dir=str(output_dir),
                downloads=downloads,
                progress=100,
                stage_name="Complete",
                stage_index=len(PIPELINE_STAGES),
                stage_count=len(PIPELINE_STAGES),
            )
    except Exception as exc:
        task.update({"status": "failed", "error": str(exc), "updated_at": _now()})
        _append(project_id, "error", str(exc))
        if postgis.is_available():
            postgis.upsert_project(
                project_id,
                status="failed",
                roads_filename=roads_path.name,
                error=str(exc),
                output_dir=str(output_dir),
            )


@app.on_event("startup")
def startup() -> None:
    if postgis.is_available():
        postgis.init_schema()


@app.get("/")
@app.get("/health")
def health() -> Dict[str, Any]:
    qgis = _find_qgis_process()
    return {
        "status": "ok",
        "service": "ftth-engine-api",
        "started_at": APP_STARTED_AT.isoformat(timespec="seconds"),
        "uptime_seconds": int((datetime.now(timezone.utc) - APP_STARTED_AT).total_seconds()),
        "qgis_process": qgis,
        "postgis": postgis.db_info(),
        "osm": _osm_health(),
        "endpoints": [
            "POST /ftth/hld/run",
            "POST /ftth/hld/resolve-area",
            "GET /ftth/hld/area-fetch?area=...&bbox=lon_w,lon_e,lat_s,lat_n",
            "POST /ftth/hld/run-from-area",
            "POST /ftth/hld/input-layers",
            "GET /ftth/hld/countries",
            "GET /ftth/hld/places",
            "GET /ftth/hld/osm-status",
            "GET /ftth/hld/results/{project_id}",
            "GET /ftth/hld/results/{project_id}/layers/{layer}",
            "GET /ftth/hld/download/{project_id}/{file_path}",
            "GET /tiles/{layer}/{z}/{x}/{y}.pbf?project_id={project_id}",
            "GET /ftth/projects",
            "DELETE /ftth/hld/projects/{project_id}",
        ],
    }


@app.post("/ftth/hld/run", status_code=202)
async def run_hld(
    background_tasks: BackgroundTasks,
    excel: UploadFile = File(...),
    roads: UploadFile = File(...),
    brownfield: Optional[UploadFile] = File(None),
    # Optional OSM reference layers — stored in inputs/ AND consumed as design
    # constraints: railways/waterways/water/natural feed the polygon barrier
    # rule (POLY_BARRIER_EXTRA) and landuse feeds the trench stage's aerial
    # zones (AERIAL_ZONES) via _osm_constraint_args when the run starts.
    railways: Optional[UploadFile] = File(None),
    waterways: Optional[UploadFile] = File(None),
    water: Optional[UploadFile] = File(None),
    landuse: Optional[UploadFile] = File(None),
    natural: Optional[UploadFile] = File(None),
    project_id: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    poly_method: Optional[int] = Form(3),
) -> Dict[str, Any]:
    project_id = project_id or uuid.uuid4().hex
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    excel_path = _save_upload(excel, upload_dir, "addresses.xlsx")
    roads_path = _save_upload(roads, upload_dir, "roads.gpkg")
    brownfield_path: Optional[Path] = None
    if brownfield and brownfield.filename:
        brownfield_path = _save_upload(brownfield, upload_dir, "brownfield.zip")

    # Optional OSM reference layers: save each under inputs/osm/<key>/ so they
    # travel with the project but never touch the pipeline parameters.
    osm_inputs: Dict[str, Path] = {}
    for key, upload in (
        ("railways", railways),
        ("waterways", waterways),
        ("water", water),
        ("landuse", landuse),
        ("natural", natural),
    ):
        if upload and upload.filename:
            osm_inputs[key] = _save_upload(upload, upload_dir / "osm" / key, f"{key}.zip")

    task = _task(project_id)
    task.update(
        {
            "status": "queued",
            "project_name": name or "",
            "poly_method": poly_method,
            "roads_filename": roads_path.name,
            "output_dir": str(output_dir),
            "osm_inputs": {k: str(v) for k, v in osm_inputs.items()},
            "updated_at": _now(),
        }
    )
    if postgis.is_available():
        postgis.init_schema()
        postgis.upsert_project(
            project_id,
            status="queued",
            roads_filename=roads_path.name,
            output_dir=str(output_dir),
        )

    background_tasks.add_task(
        _run_pipeline,
        project_id,
        excel_path,
        roads_path,
        output_dir,
        poly_method,
        brownfield_path,
    )
    return _public_task(project_id)


# ---------------------------------------------------------------------------
# Area-driven runs: an area name instead of hand-prepared input files.
#
# The area path CONVERGES ON THE FILE PATH: _run_area_pipeline writes the same
# two files a manual upload would, then calls _run_pipeline unchanged.  Nothing
# downstream — ingest, layers, tiles, downloads, BOQ, permits, LLD — can tell
# the difference, which is what keeps this from becoming a second pipeline.
# ---------------------------------------------------------------------------


# The two files an area run writes, and therefore the names the platform's
# project row carries (it has no uploaded filename to fall back on).
AREA_ROADS_FILENAME = "roads.geojson"


def _osm_health() -> Dict[str, Any]:
    """Light OSM-store block for /health.  A DB hiccup must not fail the probe."""
    try:
        status = osm_source.osm_status()
    except Exception as exc:  # noqa: BLE001
        return {"loaded": False, "error": str(exc)}
    return {
        "loaded": status.get("loaded"),
        "tables": status.get("tables") or {},
        "extract": status.get("extract"),
        # The register is an off-by-default operator step, so "loaded but not
        # in use" is a state a probe has to be able to see: without it, an
        # operator who has loaded ONSPD and still gets heuristic household
        # counts has no way to tell that from never having loaded it.
        "household_register": status.get("household_register"),
    }


def _area_error(exc: Exception) -> HTTPException:
    """Map an osm_source failure onto an honest, distinguishable response.

    "OpenStreetMap is unreachable" and "no such area" must never be
    confusable: one is a retry, the other is a different area name.
    """
    message = str(exc)
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=404,
            detail=("No area matched that name. Try adding the city and country, "
                    "e.g. 'Mariendorf, Berlin, Germany'."),
        )
    if isinstance(exc, osm_source.TooManyPremises):
        # Quote the cap that actually applied.  The run's cap and a caller-set
        # preview cap are different numbers, and the old text always printed the
        # run's -- so a preview refused by its own cap told the planner to look
        # for a setting that would not have helped.
        return HTTPException(
            status_code=422,
            detail=osm_source.oversize_detail(exc.count, exc.cap, exc.hint),
        )
    if message.startswith("too_many_premises:"):
        # A cap that reached us as text (a persisted failure, say).  The cap
        # travels in the token when the engine raised it; older rows carry only
        # the count, and for those the run's cap is the only one it can have been.
        parts = message.split(":")
        try:
            count = int(parts[1])
            cap = int(parts[2]) if len(parts) > 2 else osm_source.MAX_PREMISES
        except (IndexError, ValueError):
            count, cap = 0, osm_source.MAX_PREMISES
        return HTTPException(
            status_code=422,
            detail=osm_source.oversize_detail(count, cap),
        )
    if message == "no_premises":
        return HTTPException(
            status_code=422,
            detail=("No premises were found inside this boundary — the area may have "
                    "resolved to a building-free area, or the boundary is wrong."),
        )
    if message == "no_roads":
        return HTTPException(
            status_code=422,
            detail=("No roads were found inside this boundary, so there is nothing for "
                    "the design to route along."),
        )
    if message == "postgis_unavailable":
        return HTTPException(
            status_code=503,
            detail=("PostGIS is unavailable, so OSM data cannot be stored or read. "
                    "Start the database and retry."),
        )
    # Anything else is reported with its CAUSE.  An opaque "could not reach the
    # OSM services" standing in front of a database or geometry error is worse
    # than no message: it sends the reader to check the network for an hour.
    return HTTPException(
        status_code=502,
        detail=(f"Area lookup failed — {type(exc).__name__}: {exc}"),
    )


@app.get("/ftth/hld/osm-status")
def get_osm_status() -> Dict[str, Any]:
    """What the local OSM store holds.  An empty store is NOT an error.

    The store fills itself on the first resolve-area call for an area, so an
    empty store is simply "nothing fetched yet" — no setup step is required.
    """
    status = osm_source.osm_status()
    if not status.get("postgis"):
        status["hint"] = "PostGIS is unavailable; area runs cannot store or read OSM data."
    elif not status.get("loaded"):
        status["hint"] = ("Nothing fetched yet. The first resolve-area call for an "
                          "area fetches its OSM data into this store automatically.")
    return status


def _area_request(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    """Structured area inputs -> (search label, country code, input type).

    Accepts the structured fields (country / city / postcode / area_name) and a
    pre-composed `area` label, so an existing client keeps working.  The country
    is never inferred: a country on its own is not an area, and a postcode on its
    own is searched as typed rather than assumed to be German.
    """
    payload = payload or {}
    given = str(payload.get("area") or "").strip()
    code = countries.normalize_country_code(
        payload.get("country_code") or payload.get("country")
    )
    city = str(payload.get("city") or "").strip()
    postcode = str(payload.get("postcode") or "").strip()
    name = str(payload.get("area_name") or payload.get("street") or "").strip()

    if given and not (city or postcode or name):
        return given, code, osm_source.input_type_for(area=given)
    if not (city or postcode or name):
        raise HTTPException(
            status_code=400,
            detail="Give a postcode and/or a place, street or city name.",
        )
    label = osm_source.compose_area(
        area_name=name, postcode=postcode, city=city, country_code=code
    )
    input_type = osm_source.input_type_for(area=label, area_name=name, postcode=postcode)
    return label, code, input_type


@app.get("/ftth/hld/countries")
def get_countries() -> Dict[str, Any]:
    """Country options for the area input's country dropdown.

    A static ISO 3166-1 list: Nominatim has no "list all countries" endpoint,
    and a dropdown should not depend on a network round trip.
    """
    return {"countries": osm_source.country_list()}


@app.get("/ftth/hld/places")
def get_places(q: str = "", country: str = "", limit: int = 8) -> Dict[str, Any]:
    """City/town suggestions for the area input's city combobox.

    Best-effort by design: an empty list is a normal answer ("nothing matched"),
    and an unreachable Nominatim comes back as ``reason: unavailable`` rather
    than a 5xx on a page the planner is still filling in.
    """
    try:
        limit_int = max(1, min(int(limit or 8), 20))
    except (TypeError, ValueError):
        limit_int = 8
    return osm_source.suggest_places(q, country_code=country, limit=limit_int)


@app.post("/ftth/hld/resolve-area")
def resolve_area_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Resolve an area to its boundary, premises and household mix.

    Read-only and pipeline-free: this is the preview a planner checks before a
    design gets built on these numbers.
    """
    area, country_code, input_type = _area_request(payload)
    # No default cap: the preview exists to report the size of any area, and a
    # refusal here used to hide the sub-area breakdown that says where to
    # narrow.  A caller may still impose a cap, and if it is exceeded the error
    # names that cap rather than the run's.
    raw_cap = (payload or {}).get("max_premises")
    try:
        max_premises = int(raw_cap) if str(raw_cap or "").strip() else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="'max_premises' must be an integer.") from exc
    # boundary_only: Nominatim only, so the map can draw the boundary before
    # paying for the OSM fetch that the counts need.
    boundary_only = bool((payload or {}).get("boundary_only"))
    try:
        result = osm_source.preview_area(
            area,
            max_premises=max_premises,
            boundary_only=boundary_only,
            country_code=country_code,
            input_type=input_type,
            postcode=str((payload or {}).get("postcode") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - mapped by _area_error
        raise _area_error(exc) from exc
    # The boundary call also STARTS the download, so the counts the page asks
    # for next are already in progress.  Measured on a cold city the fetch is
    # 10-16 min, and the page used to sit on a single blocking call for all of
    # it -- which the gateway then cut, leaving a boundary and nothing else.
    result["osm_fetch"] = osm_source.ensure_area_data_background(
        area, result.get("bbox") or []
    )
    return result


@app.get("/ftth/hld/area-fetch")
def get_area_fetch(
    area: str = "",
    bbox: str = "",
) -> Dict[str, Any]:
    """Progress of the OSM download for an area (read-only, no DB writes).

    The page polls this while it waits, so a ten-minute download says what it
    is doing instead of looking like a hang.  ``bbox`` is ``lon_w,lon_e,lat_s,
    lat_n`` as returned by resolve-area; without it the area name is resolved
    first, which costs one Nominatim call.
    """
    box: Optional[List[float]] = None
    if bbox.strip():
        try:
            box = [float(v) for v in bbox.split(",")][:4]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="'bbox' must be four numbers.") from exc
        if len(box) != 4:
            raise HTTPException(status_code=400, detail="'bbox' must be four numbers.")
    if box is None:
        try:
            resolution = osm_source.resolve_area(area)
            box = list(resolution["bbox"])
        except Exception as exc:  # noqa: BLE001 - mapped by _area_error
            raise _area_error(exc) from exc
    return osm_source.area_fetch_state(area, box)


def _run_area_pipeline(
    project_id: str,
    area: str,
    output_dir: Path,
    poly_method: int = 3,
    country_code: str = "",
    input_type: str = "",
    postcode: str = "",
) -> None:
    """Generate an area's input files, then run the UNCHANGED pipeline on them."""
    task = _task(project_id)
    task.update({
        "status": "running",
        "stage": "Resolve area",
        "stage_index": 0,
        "progress": 0,
        "updated_at": _now(),
    })
    _append(project_id, "info", f"Resolving area: {area}")
    # Marked running (with its stage) the moment it starts: until now the row
    # said "queued" for the whole resolve, and a restart during it left no trace
    # that this run had ever begun.
    task["output_dir"] = str(output_dir)
    if postgis.is_available():
        postgis.upsert_project(
            project_id,
            status="running",
            roads_filename=AREA_ROADS_FILENAME,
            output_dir=str(output_dir),
            stage_name="Resolve area",
            stage_index=0,
            stage_count=len(PIPELINE_STAGES),
            progress=0,
        )

    try:
        built = osm_source.build_inputs(
            project_id, area, str(output_dir),
            country_code=country_code, input_type=input_type, postcode=postcode,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        # A refused area is a decision about the design, not an internal error,
        # so it is said in the same words the preview would use -- including the
        # cap that applied and where to narrow.  `too_many_premises:257127:20000`
        # is a token, and it was being shown to planners as it stood.
        if isinstance(exc, osm_source.TooManyPremises):
            detail = osm_source.oversize_detail(exc.count, exc.cap, exc.hint)
        else:
            detail = str(exc)
        task.update({"status": "failed", "error": detail, "updated_at": _now()})
        _append(project_id, "error", detail)
        if postgis.is_available():
            postgis.upsert_project(
                project_id,
                status="failed",
                roads_filename=AREA_ROADS_FILENAME,
                error=detail,
                output_dir=str(output_dir),
            )
        return

    households = built["households"]
    extract = built.get("extract") or {}
    _append(project_id, "info", f"Area resolved to {built.get('matched') or area}.")
    _append(
        project_id, "info",
        f"OSM data: {extract.get('source') or 'local store'}"
        f"{(' (' + str(extract.get('detail'))) + ')' if extract.get('detail') else ''}.",
    )
    _append(
        project_id, "info",
        f"Premises: {built['premises']}; households: {households['total']} "
        f"({int(round(households.get('estimated_share', 0) * 100))}% estimated).",
    )
    if built.get("polygon_source") != "nominatim":
        _append(
            project_id, "warning",
            "The area resolved to a bounding box, not a polygon — it is a rectangle and "
            "may take in neighbouring areas.",
        )
    _append(
        project_id, "info",
        f"Roads: {built['roads_km']} km. Wrote inputs/Main_DataSet.xlsx and "
        "inputs/roads.geojson.",
    )

    # Provenance: the run records what OSM it was built from, so a re-run is a
    # decision rather than "whatever the services returned that day".
    try:
        provenance = output_dir / "inputs" / "area_resolution.json"
        with provenance.open("w", encoding="utf-8") as f:
            json.dump(built, f, indent=2, default=str)
    except Exception:
        pass

    task["area"] = area
    task["area_meta"] = {
        "matched": built.get("matched"),
        "polygon_source": built.get("polygon_source"),
        "premises": built["premises"],
        "households": households,
        "roads_km": built["roads_km"],
        "extract": extract,
    }

    _run_pipeline(
        project_id,
        Path(built["excel_path"]),
        Path(built["roads_path"]),
        output_dir,
        poly_method,
    )


@app.post("/ftth/hld/run-from-area", status_code=202)
async def run_from_area(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """Start a full HLD run from an area — no files to prepare."""
    area, country_code, input_type = _area_request(payload)
    postcode = str((payload or {}).get("postcode") or "")
    project_id = str((payload or {}).get("project_id") or uuid.uuid4().hex)
    name = (payload or {}).get("name")
    try:
        poly_method = int((payload or {}).get("poly_method") or 3)
    except (TypeError, ValueError):
        poly_method = 3

    output_dir = OUTPUT_DIR / project_id
    output_dir.mkdir(parents=True, exist_ok=True)

    task = _task(project_id)
    task.update({
        "status": "queued",
        "project_name": name or area,
        "area": area,
        "poly_method": poly_method,
        "output_dir": str(output_dir),
        "updated_at": _now(),
    })
    if postgis.is_available():
        postgis.init_schema()
        # roads_filename is NOT NULL in Django's ftth_projects table, and an
        # area run has no uploaded file to name — so name the file this run is
        # about to write, which is exactly what the column holds for a manual
        # upload too.
        postgis.upsert_project(
            project_id,
            status="queued",
            roads_filename=AREA_ROADS_FILENAME,
            output_dir=str(output_dir),
        )

    background_tasks.add_task(
        _run_area_pipeline, project_id, area, output_dir, poly_method,
        country_code, input_type, postcode,
    )
    return _public_task(project_id)


@app.post("/ftth/hld/input-layers")
def get_input_layer(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Return one complete generated OSM/HLD input layer before execution."""
    layer = str((payload or {}).get("layer") or "").strip()
    if not layer:
        raise HTTPException(status_code=400, detail="'layer' is required.")
    area, country_code, _input_type = _area_request(payload)
    try:
        resolution = osm_source.resolve_area(area, country_code=country_code)
        osm_source.ensure_area_data(area, resolution["bbox"])
        return osm_source.input_layer_geojson(
            resolution["polygon"],
            layer,
            country=resolution.get("country") or "",
            city=resolution.get("city") or "",
            country_code=resolution.get("country_code") or "",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _area_error(exc) from exc


@app.get("/ftth/hld/results/{project_id}")
def get_results(project_id: str) -> Dict[str, Any]:
    if project_id not in tasks and postgis.is_available():
        project = postgis.get_project(project_id)
        if project:
            task = _task(project_id)
            task.update(
                {
                    "status": project.get("status"),
                    "runner": project.get("runner"),
                    "roads_filename": project.get("roads_filename"),
                    "error": project.get("error"),
                    "output_dir": project.get("output_dir") or str(OUTPUT_DIR / project_id),
                    # Rebuilt from a row, NOT started by this process: no thread
                    # here is running it, so a "running" status is an orphan
                    # left by the last engine restart.
                    "_from_db_row": True,
                    # The stage the row recorded, when it has one: without it a
                    # restored run reports stage=None and the page shows a
                    # progress bar with nothing to label it.
                    "stage": project.get("stage_name") or None,
                    "stage_index": int(project.get("stage_index") or 0),
                    "progress": int(project.get("progress") or 0),
                    "downloads": project.get("downloads") or [],
                    "layers": postgis.list_project_layers(project_id),
                }
            )
    if project_id not in tasks:
        _restore_task_from_disk(project_id)
    elif (
        tasks[project_id].get("status") != "running"
        and tasks[project_id].get("status") != "queued"
        and not (tasks[project_id].get("layers") or [])
    ):
        # The project row exists in PostGIS but no layer rows were ever
        # ingested (e.g. the run predates the GIS wiring, or PostGIS was
        # unavailable at run time). Fall back to the on-disk outputs so the
        # results stay fetchable; _restore_task_from_disk merges into the
        # existing task (keeps roads_filename/runner, adds layers/downloads).
        # NEVER restore over an in-flight run — _restore_task_from_disk
        # force-marks the task completed, which would make the UI report a
        # running pipeline as done at 0 layers.
        _restore_task_from_disk(project_id)
    if project_id not in tasks:
        raise HTTPException(status_code=404, detail="Project not found")
    task = tasks[project_id]
    # A run this process does not own (the row says running, but no thread here
    # is running it -- the engine restarted mid-run) is settled from the files
    # it left behind, so the page shows a stage that is real and a status that
    # is terminal instead of "running" forever.
    if task.get("status") in ("running", "queued") and not _owned_in_process(project_id):
        _recover_orphan_run(project_id, task)
    # Downloads may be empty in the DB row (older runs) even though the
    # design package files exist on disk — surface them and persist back.
    if not (task.get("downloads") or []):
        output_dir = OUTPUT_DIR / project_id
        if output_dir.is_dir():
            downloads = _register_downloads(project_id, output_dir)
            if downloads:
                task["downloads"] = downloads
                try:
                    if postgis.is_available():
                        postgis.update_project_downloads(project_id, downloads)
                except Exception:
                    pass
    return _public_task(project_id)


@app.get("/ftth/hld/results/{project_id}/layers/{layer}")
def get_layer(project_id: str, layer: str) -> Dict[str, Any]:
    if postgis.is_available():
        try:
            data = postgis.get_layer_geojson(project_id, layer)
        except KeyError:
            # Layer name isn't a PostGIS table (e.g. chambers/poles/brownfield
            # on a DB created before those tables) — fall through to the disk
            # outputs below instead of 404ing.
            data = None
        if data is not None:
            return data

    task = tasks.get(project_id)
    if task is None or not (task.get("files") or {}).get(layer):
        # A task restored from PostGIS (engine restart) carries layer counts
        # but no on-disk file paths, so grouped layers (cables/ducts) that
        # PostGIS resolves to None would 404 here. Fall back to disk output
        # paths whenever the current task has none for the requested layer.
        restored = _restore_task_from_disk(project_id)
        if restored is not None:
            task = restored
    if task:
        files = (task.get("files") or {}).get(layer, [])
        geojson_files = [
            fp for fp in files
            if fp.lower().endswith((".geojson", ".json")) and os.path.isfile(fp)
        ]
        if not geojson_files:
            raise HTTPException(status_code=404, detail="Layer not found")
        if len(geojson_files) == 1:
            with open(geojson_files[0], "r", encoding="utf-8") as f:
                return json.load(f)
        # Group with multiple sub-layers (e.g. ducts: feeder + distribution + drop):
        # merge them into one FeatureCollection and tag each feature with its
        # originating sub-layer so the frontend can style them differently.
        merged: Dict[str, Any] = {"type": "FeatureCollection", "features": []}
        for fp in geojson_files:
            with open(fp, "r", encoding="utf-8") as f:
                sub = json.load(f)
            sub_name = os.path.splitext(os.path.basename(fp))[0]
            for feat in sub.get("features", []):
                props = dict(feat.get("properties") or {})
                props.setdefault("sublayer", sub_name)
                feat["properties"] = props
                merged["features"].append(feat)
        return merged
    raise HTTPException(status_code=404, detail="Layer not found")


# ======================================================================
# TRENCH DESIGN (Phase A of TRENCH_DESIGN.md)
#
# POST /ftth/hld/design/{project_id}          - (re)run the designer
# GET  /ftth/hld/results/{project_id}/design  - status + report + layers
#
# Inputs are read from the project itself (HLD outputs + roads + optional
# aerial zones / OSM landuse), never re-uploaded.
# ======================================================================

@app.post("/ftth/hld/design/{project_id}", status_code=202)
def run_trench_design(project_id: str, force: bool = False) -> Dict[str, Any]:
    if not (OUTPUT_DIR / project_id).is_dir():
        raise HTTPException(status_code=404, detail="Unknown project")
    return design.start_design(project_id, force=force)


@app.get("/ftth/hld/results/{project_id}/design")
def get_trench_design(project_id: str, layers: bool = True) -> Dict[str, Any]:
    return design.payload(project_id, include_layers=layers)


def _project_output_dir(project_id: str) -> Path:
    """Resolve one project's output directory, rejecting traversal attempts."""
    if not project_id or project_id in (".", "..") or "/" in project_id or "\\" in project_id:
        raise HTTPException(status_code=404, detail="Project not found")
    output_dir = (OUTPUT_DIR / project_id).resolve()
    if OUTPUT_DIR.resolve() not in output_dir.parents:
        raise HTTPException(status_code=404, detail="Project not found")
    return output_dir


@app.get("/ftth/hld/results/{project_id}/surface-ai-review")
def get_surface_ai_review(project_id: str) -> Dict[str, Any]:
    """Read the optional surface AI review artifact without changing the design."""
    output_dir = _project_output_dir(project_id)
    review_path = output_dir / "surface_ai_review.json"
    if not review_path.is_file():
        raise HTTPException(status_code=404, detail="Surface AI review is not available")
    try:
        with review_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Surface AI review is unreadable") from exc


@app.post("/ftth/hld/results/{project_id}/surface-ai-review/classify")
def classify_surface_at_point(project_id: str,
                              payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Suggest the surface at one clicked point or along one span (advisory only).

    Suggestion-only, exactly like the batch review artifact: it fetches fresh
    imagery for the geometry and asks the configured vision model. Two shapes are
    accepted — a clicked point as ``[x, y]`` with ``crs``, or a span the reader
    opted into as its own route ``[[x, y], ...]`` with ``coordinates_crs`` and a
    ``span_id``. It never edits ``Final_Trenches.gpkg`` or any other design output
    and never writes the review artifact, so one reader choice costs one model
    call. The call is synchronous, so a slow local model is bounded by
    SURFACE_AI_OLLAMA_TIMEOUT rather than by the HTTP layer. ``include_imagery``
    returns the patch the model saw, as base64, for display.
    """
    output_dir = _project_output_dir(project_id)
    if not output_dir.is_dir():
        raise HTTPException(status_code=404, detail="Unknown project")
    try:
        from HLDPlanning.design import surface_ai_review as surface_review
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail="Surface review module is unavailable"
        ) from exc

    coordinates = payload.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise HTTPException(
            status_code=400,
            detail="coordinates must be [x, y] or a span route [[x, y], ...]",
        )
    include_imagery = bool(payload.get("include_imagery"))

    # A span detect carries the span's own route, so its first element is a
    # point rather than a number. That single shape test picks the path without
    # needing the caller to declare a mode.
    if isinstance(coordinates[0], (list, tuple)):
        try:
            return surface_review.classify_span(
                payload.get("span_id"), coordinates,
                payload.get("coordinates_crs") or payload.get("crs") or "EPSG:4326",
                claimed_surface=payload.get("claimed_surface"),
                geometry_reason=payload.get("geometry_reason"),
                geometry_confidence=payload.get("geometry_confidence"),
                known_share=payload.get("known_share"),
                include_imagery=include_imagery,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        point = [float(coordinates[0]), float(coordinates[1])]
    except (TypeError, ValueError, IndexError):
        raise HTTPException(
            status_code=400, detail="Point coordinates must be numeric"
        ) from None
    options: Dict[str, Any] = {"include_imagery": include_imagery}
    if payload.get("length_m") is not None:
        options["length_m"] = payload["length_m"]
    if payload.get("bearing") is not None:
        options["bearing"] = payload["bearing"]

    try:
        return surface_review.classify_point(
            point, payload.get("crs") or "EPSG:4326", **options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/ftth/hld/results/{project_id}/surface-ai-review/imagery")
def preview_surface_imagery(project_id: str,
                            payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Return the imagery patch a detect would send, without calling the model.

    Imagery-only and free: it resolves the same provider and patch the review
    uses and hands back the JPEG as base64 plus its source, date and whether the
    planned route was drawn over it. No vision-model call is made, so a reader can
    look at exactly what the model would be given before spending a call on it.
    Accepts the same dual input shape as the classify route: a point as ``[x, y]``
    with ``crs``, or a span's route ``[[x, y], ...]`` with ``coordinates_crs``.
    """
    output_dir = _project_output_dir(project_id)
    if not output_dir.is_dir():
        raise HTTPException(status_code=404, detail="Unknown project")
    try:
        from HLDPlanning.design import surface_ai_review as surface_review
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail="Surface review module is unavailable"
        ) from exc

    coordinates = payload.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise HTTPException(
            status_code=400,
            detail="coordinates must be [x, y] or a span route [[x, y], ...]",
        )
    options: Dict[str, Any] = {"span_id": payload.get("span_id")}
    if payload.get("length_m") is not None:
        options["length_m"] = payload["length_m"]
    if payload.get("bearing") is not None:
        options["bearing"] = payload["bearing"]
    crs = payload.get("coordinates_crs") or payload.get("crs") or "EPSG:4326"

    try:
        return surface_review.preview_imagery(coordinates, crs, **options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/ftth/hld/download/{project_id}/{file_path:path}")
def download(project_id: str, file_path: str) -> FileResponse:
    task = tasks.get(project_id)
    output_dir = Path(task["output_dir"]) if task and task.get("output_dir") else OUTPUT_DIR / project_id
    candidate = (output_dir / file_path).resolve()
    base = output_dir.resolve()
    if base not in candidate.parents and candidate != base:
        raise HTTPException(status_code=404, detail="File not found")
    if not candidate.is_file() or candidate.suffix.lower() not in DOWNLOAD_EXTS:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(candidate), filename=candidate.name)


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf")
def tiles(layer: str, z: int, x: int, y: int, project_id: str) -> Response:
    if not postgis.is_available():
        raise HTTPException(status_code=503, detail="PostGIS is not available")
    try:
        tile = postgis.get_vector_tile(project_id, layer, z, x, y)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=tile,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.delete("/ftth/hld/projects/{project_id}", status_code=200)
def delete_project(project_id: str) -> Dict[str, Any]:
    """Delete a project and all its associated data (disk + PostGIS)."""
    removed_task = tasks.pop(project_id, None)
    output_dir = OUTPUT_DIR / project_id
    if output_dir.exists() and output_dir.is_dir():
        shutil.rmtree(str(output_dir), ignore_errors=True)
    # PostGIS cleanup is best-effort.  The engine's project row is referenced
    # by Django's permit matrix, so if the caller has not dropped its own rows
    # yet we get a ForeignKeyViolation — that is a caller-ordering problem,
    # not a reason to 500 and hide the fact that the disk output was removed.
    postgis_error: Optional[str] = None
    if postgis.is_available():
        try:
            postgis.clear_project_layers(project_id)
            postgis.delete_project(project_id)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            postgis_error = f"{type(exc).__name__}: {exc}"
            print(
                f"[delete] PostGIS cleanup failed for {project_id}: {postgis_error}",
                flush=True,
            )
    return {
        "deleted": True,
        "project_id": project_id,
        "had_in_memory_task": removed_task is not None,
        "postgis_cleaned": postgis_error is None,
        "postgis_error": postgis_error,
    }


@app.get("/ftth/projects")
def projects(limit: int = 50) -> List[Dict[str, Any]]:
    if postgis.is_available():
        rows = postgis.list_projects(limit=limit)
        # Overlay the live in-memory state for runs the engine is executing
        # right now. PostGIS only records status at start/finish and has no
        # progress/stage columns, so a running project would otherwise be
        # listed with the status/progress of its LAST write — leaving the
        # project list (and the dashboard) unable to tell whether an
        # in-flight run is still working or already finished, while the
        # results page reports the real stage.
        for row in rows:
            live = tasks.get(row.get("project_id"))
            if not live and row.get("status") in ("running", "queued"):
                # A row left "running" by an engine restart. Settle it from the
                # files on disk before the list is rendered, so the projects /
                # outputs page does not show a run that stopped hours ago as
                # if it were still going.
                live = _recover_orphan_from_row(row)
            if not live:
                # No live task (engine restart) — the stored row is the truth,
                # but rows written before the progress column was persisted
                # carry the 0 % default, which made a finished run list as
                # "completed · 0 %". Derive it from the terminal status.
                if row.get("status") == "completed" and not row.get("progress"):
                    row["progress"] = 100
                # Same key the live overlay uses, so a caller does not have to
                # know whether the stage came from the row or from this process.
                row["stage"] = row.get("stage_name") or None
                continue
            # A live in-memory task is always the more recent truth — it also
            # covers the window where the pipeline has finished but its final
            # status write to PostGIS has not landed yet.
            if live.get("status") not in ("running", "queued", "completed", "failed"):
                continue
            row["status"] = live.get("status")
            # Same completion guarantee as _public_task: only a completed run
            # reports 100 %, anything in flight is capped at 99 %.
            if live.get("status") == "completed":
                row["progress"] = 100
            else:
                row["progress"] = min(int(live.get("progress") or 0), 99)
            row["stage"] = live.get("stage")
            row["stage_name"] = live.get("stage")
            row["stage_index"] = live.get("stage_index")
            row["stage_count"] = live.get("stage_count")
            row["updated_at"] = live.get("updated_at") or row.get("updated_at")
        return rows
    return [
        _public_task(project_id)
        for project_id in sorted(tasks, key=lambda pid: tasks[pid].get("created_at", ""), reverse=True)
    ][:limit]


# ======================================================================
# LLD — apply approved survey changes to the HLD output, validate the
# network (path continuity + attribute consistency), and emit the final
# LLD layers + a downloadable zip.
# ======================================================================

LLD_LAYER_ORDER = [
    "objects", "polygons", "pdps", "mfg",
    "final_trenches",
    "feeder_cable", "distribution_cable", "aerial_cable",
    "feeder_ducts", "distribution_ducts", "drop_ducts",
    "coupleurs",
    "chambers", "poles", "trench_nodes",
    "aerial_spans", "aerial_drops",
    "existing_infrastructure", "existing_infrastructure_points",
    "brownfield",
]

# Layer names the LLD engine should NOT emit as standalone outputs. Garden
# trenches are mirrored into final_trenches (trench_type=Garden) like the HLD
# pipeline does, so a separate garden_trench layer would be redundant and
# confusing in the LLD results window.
LLD_EXCLUDED_LAYERS = {"garden_trench"}

lld_tasks: Dict[str, Dict[str, Any]] = {}


def _lld_key(project_id: str, lld_version: str) -> str:
    return f"{project_id}:{lld_version}"


def _lld_task(project_id: str, lld_version: str) -> Dict[str, Any]:
    key = _lld_key(project_id, lld_version)
    return lld_tasks.setdefault(
        key,
        {
            "project_id": project_id,
            "lld_version": lld_version,
            "status": "queued",
            "stage": None,
            "progress": 0,
            "layers": [],
            "downloads": [],
            "messages": deque(maxlen=MAX_MESSAGES),
            "created_at": _now(),
            "updated_at": _now(),
        },
    )


def _lld_public_task(project_id: str, lld_version: str) -> Dict[str, Any]:
    task = dict(_lld_task(project_id, lld_version))
    messages = task.get("messages")
    task["messages"] = list(messages) if isinstance(messages, deque) else []
    task["results_url"] = f"/ftth/lld/results/{project_id}/{lld_version}"
    # Same guarantee as HLD: never advertise 100% while the run is not
    # actually completed; always advertise 100% once it is.
    if task.get("status") == "completed":
        task["progress"] = 100
    else:
        task["progress"] = min(int(task.get("progress") or 0), 99)
    return task


def _lld_append(project_id: str, lld_version: str, level: str, text: str) -> None:
    task = _lld_task(project_id, lld_version)
    task["messages"].append({"ts": _now(), "level": level, "text": text})
    task["updated_at"] = _now()


def _geom_type(feats: List[Dict[str, Any]]) -> Optional[str]:
    for f in feats:
        g = f.get("geometry")
        if g and g.get("type"):
            return g["type"]
    return None


def _line_strings(g: Optional[Dict[str, Any]]) -> List[Any]:
    if not g:
        return []
    t = g.get("type")
    c = g.get("coordinates") or []
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def _points(g: Optional[Dict[str, Any]]) -> List[Any]:
    if not g:
        return []
    t = g.get("type")
    c = g.get("coordinates") or []
    if t == "Point":
        return [c]
    if t == "MultiPoint":
        return c
    return []


def _near_point_counter(points: List[Any], tol: float):
    """Grid-hashed near-neighbour counter (pure Python, no shapely needed)."""
    cell = max(tol, 1e-9)
    index: Dict[Tuple[int, int], List[Any]] = {}
    for p in points:
        key = (int(p[0] / cell), int(p[1] / cell))
        index.setdefault(key, []).append(p)

    def count_near(p: Any) -> int:
        kx, ky = int(p[0] / cell), int(p[1] / cell)
        n = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in index.get((kx + dx, ky + dy), []):
                    if abs(q[0] - p[0]) < tol and abs(q[1] - p[1]) < tol:
                        n += 1
        return n

    return count_near


# ---------------------------------------------------------------------------
# Cross-layer support propagation — approved survey changes in one layer must
# be reflected in the layers that depend on it:
#
#   * a cable can only be laid where a trench AND a duct already exist, so
#     every cable path must be covered by final_trenches + a duct;
#   * a duct can only be laid where a trench exists, so every duct path must
#     be covered by final_trenches.
#
# Where the approved dataset lacks the supporting layer on the cable/duct
# path (e.g. the engineer added a cable in survey without drawing a trench),
# the engine creates the missing supporting feature along that path and tags
# it ``lld_created`` so reviewers can see exactly what was auto-generated.
# ---------------------------------------------------------------------------

TRENCH_LAYERS = {"final_trenches"}
CABLE_LAYERS = {"feeder_cable", "distribution_cable"}
DUCT_LAYERS = {"feeder_ducts", "distribution_ducts", "drop_ducts"}
# A cable must ride on the duct of the same tier (feeder cable → feeder ducts,
# distribution cable → distribution ducts). Drop ducts serve premises only.
CABLE_TO_DUCT = {"feeder_cable": "feeder_ducts", "distribution_cable": "distribution_ducts"}

# LLD distribution-cable capacity contract. The physical catalogue remains
# unchanged; these fields describe grouped HH usage and reserve two fibres.
LLD_DISTRIBUTION_FIBERS = 48
LLD_RESERVED_SPARE_FIBERS = 2
# One feeder duct may serve up to four PDPs; this is the enforced capacity
# contract used by both verify and full-replan output enrichment.
LLD_MAX_PDPS_PER_DUCT = 4

# Coverage / creation tolerance in degrees (~50 m). Must stay consistent with
# the network-connectivity tolerance used by _validate_network().
LLD_COVERAGE_TOL = 0.0005

# Reroute detection tolerance in degrees (~1 m). A reroute is a deliberate
# change of the engineer's chosen path — even a small nudge (a few metres)
# must be detected and propagated. The coverage tolerance (50 m) is far too
# coarse for this: a 10 m reroute would look "unchanged" and the duct/cable
# would stay on the old route.
LLD_REROUTE_TOL = 1e-5

# Relay/ride tolerance in degrees (~8 m at Berlin latitude). A dependent line
# "rides on" the moved region when its vertices are within this distance of
# the old path — co-located corridor lines (a duct drawn a few metres beside
# its trench) still count as riding it and must follow the reroute. Matches
# the survey app's 8 m co-location tolerance so app-side and engine-side
# propagation agree.
LLD_RELAY_TOL = 1e-4


def _seg_point_dist(p: Any, a: Any, b: Any) -> float:
    """Euclidean distance from point p to segment [a, b] (degrees)."""
    px, py = p[0], p[1]
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _sample_polyline(line: Any, step: float) -> List[Any]:
    """Sample points along a polyline every ``step`` degrees (incl. joints)."""
    pts: List[Any] = []
    for a, b in zip(line, line[1:]):
        d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        n = max(1, int(d / step)) if step > 0 else 1
        for i in range(n + 1):
            t = i / n
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def _build_segment_index(lines: List[Any], tol: float) -> Tuple[Dict[Tuple[int, int], List[Any]], float]:
    """Grid-hashed index of line segments for fast point-coverage queries."""
    cell = max(tol, 1e-9)
    index: Dict[Tuple[int, int], List[Any]] = {}
    for line in lines:
        for a, b in zip(line, line[1:]):
            xmin, xmax = sorted((a[0], b[0]))
            ymin, ymax = sorted((a[1], b[1]))
            for cx in range(int(xmin / cell) - 1, int(xmax / cell) + 2):
                for cy in range(int(ymin / cell) - 1, int(ymax / cell) + 2):
                    index.setdefault((cx, cy), []).append((a, b))
    return index, cell


def _uncovered_points(line: Any, index: Dict[Tuple[int, int], List[Any]], cell: float, tol: float) -> List[Any]:
    """Sample points of ``line`` not covered by any indexed segment."""
    uncovered: List[Any] = []
    step = max(tol * 0.5, 1e-6)
    for p in _sample_polyline(line, step):
        kx, ky = int(p[0] / cell), int(p[1] / cell)
        found = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for a, b in index.get((kx + dx, ky + dy), []):
                    if _seg_point_dist(p, a, b) <= tol:
                        found = True
                        break
                if found:
                    break
            if found:
                break
        if not found:
            uncovered.append(p)
    return uncovered


def _runs(points: List[Any]) -> List[List[Any]]:
    """Group consecutive sample points into contiguous runs (min 2 points)."""
    if not points:
        return []
    runs: List[List[Any]] = []
    cur = [points[0]]
    for p in points[1:]:
        d = ((p[0] - cur[-1][0]) ** 2 + (p[1] - cur[-1][1]) ** 2) ** 0.5
        if d <= LLD_COVERAGE_TOL * 2.0:
            cur.append(p)
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [p]
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _propagate_support_layers(by_layer: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Ensure every cable lies on a trench + duct and every duct lies on a
    trench. Creates missing supporting features along the cable/duct path.

    The coverage checks make this a no-op wherever the HLD (or an approved
    survey change) already provided the supporting layer — so unchanged HLD
    networks are never duplicated. Created features carry ``lld_created``.

    Returns a count of created features per layer.
    """
    created: Dict[str, int] = {layer: 0 for layer in (*TRENCH_LAYERS, *DUCT_LAYERS)}
    tol = LLD_COVERAGE_TOL

    def all_lines(layers: set) -> List[Any]:
        out: List[Any] = []
        for layer in layers:
            for f in by_layer.get(layer, []):
                for ln in _line_strings(f.get("geometry")):
                    if len(ln) >= 2:
                        out.append(ln)
        return out

    def add_feature(layer: str, geometry: Dict[str, Any], source: Dict[str, Any], kind: str) -> None:
        props = dict(source.get("properties") or {})
        props["feature_id"] = "LLD-%s-%s" % (layer, uuid.uuid4().hex[:10])
        props["layer"] = layer
        props["lld_created"] = True
        props["lld_source_layer"] = (source.get("properties") or {}).get("layer") or "unknown"
        props["lld_source_feature_id"] = (source.get("properties") or {}).get("feature_id") or ""
        props["lld_reason"] = kind
        by_layer.setdefault(layer, []).append({
            "type": "Feature",
            "geometry": geometry,
            "properties": props,
        })
        created[layer] = created.get(layer, 0) + 1

    # ── 1. Trench coverage: every cable and duct must lie on final_trenches. ──
    trench_index, cell = _build_segment_index(all_lines(TRENCH_LAYERS), tol)
    for layer in (*CABLE_LAYERS, *DUCT_LAYERS):
        for f in by_layer.get(layer, []):
            for ln in _line_strings(f.get("geometry")):
                if len(ln) < 2:
                    continue
                uncovered = _uncovered_points(ln, trench_index, cell, tol)
                for run in _runs(uncovered):
                    add_feature(
                        "final_trenches",
                        {"type": "LineString", "coordinates": run},
                        f,
                        "auto-created trench along approved %s change (cable/duct must lie on a trench)" % layer,
                    )

    # ── 2. Duct coverage: every cable must lie on a duct of its tier. ──
    duct_index, dcell = _build_segment_index(all_lines(DUCT_LAYERS), tol)
    for layer in CABLE_LAYERS:
        duct_layer = CABLE_TO_DUCT[layer]
        for f in by_layer.get(layer, []):
            for ln in _line_strings(f.get("geometry")):
                if len(ln) < 2:
                    continue
                uncovered = _uncovered_points(ln, duct_index, dcell, tol)
                for run in _runs(uncovered):
                    add_feature(
                        duct_layer,
                        {"type": "LineString", "coordinates": run},
                        f,
                        "auto-created %s along approved %s change (cable must lie on a duct)" % (duct_layer, layer),
                    )

    return created


# Reroute propagation — an approved survey reroute of ANY corridor line
# (trench, duct or cable) REPLACES the old path for the rerouted region: the
# LLD must never keep both paths. The whole corridor follows — every other
# line layer (feeder / distribution / drop, trench / duct / cable) that rides
# on the changed region is re-laid onto the new path THERE ONLY; vertices
# outside the changed region (shared endpoints, parts running on other
# unchanged lines) stay exactly where they are. This mirrors the survey app's
# bundle-reroute propagation (Plan B: app + engine both amend), so the engine
# works correctly with any survey dataset, even one where only a single layer
# was re-drawn.
#
# "Editing the layers altogether": reroute the feeder -> distribution and
# garden (drop) riding that region follow; reroute a cable -> the trench and
# ducts under it follow. Already-approved features are never re-laid (the
# engineer's own re-draw wins).
# Trench sub-layers are the components of final_trenches. The single-trench
# redesign removed the per-tier feeder/distribution publications — the trench
# network is final_trenches alone, whose features carry the construction class
# (Open Cut / HDD / Garden). Kept as a set so legacy survey imports that still
# carry the old sub-layers degrade gracefully.
TRENCH_SUB_LAYERS: set = set()

# ---------------------------------------------------------------------------
CORRIDOR_LINE_LAYERS = sorted(
    set(TRENCH_LAYERS) | set(DUCT_LAYERS) | set(CABLE_LAYERS) | TRENCH_SUB_LAYERS
)


# ---------------------------------------------------------------------------
# Hierarchy-aware reroute propagation. Network tiers: feeder (0) feeds the
# distribution (1) which feeds the garden/drop (2). A reroute of a feature at
# tier T may only re-lay/purge features at tier >= T (same tier or downstream):
# rerouting the feeder carries the distribution/garden riding that region —
# but rerouting a DISTRIBUTION trench must NEVER drag the FEEDER backbone,
# which merely runs near the same corridor (that bug purged 29/30 feeder
# trenches in one run). ``None`` = tier-unknown (HDD/road-crossing and other
# generic construction classes) — follows any reroute as before.


def _reroute_tier(layer: str, props: Dict[str, Any]) -> Optional[int]:
    """Network tier of a corridor feature (0 feeder / 1 distribution /
    2 drop-garden), or None when the tier cannot be determined."""
    if layer in ("feeder_cable", "feeder_ducts"):
        return 0
    if layer in ("distribution_cable", "distribution_ducts"):
        return 1
    if layer == "drop_ducts":
        return 2
    if layer in TRENCH_LAYERS or layer in TRENCH_SUB_LAYERS:
        tt = str(props.get("trench_type") or props.get("USAGE_TYPE") or "").strip().lower()
        if "feeder" in tt:
            return 0
        if "distribution" in tt or "dist" in tt:
            return 1
        if "garden" in tt or "drop" in tt:
            return 2
        return None  # HDD / unknown construction class
    return None


def _tier_blocks_reroute(changed_tier: Optional[int], dep_tier: Optional[int]) -> bool:
    """True when ``dep_tier`` is UPSTREAM of ``changed_tier`` (both known) and
    must therefore never follow (be re-laid/purged by) that reroute."""
    return changed_tier is not None and dep_tier is not None and dep_tier < changed_tier


def _nearest_point_on_path(p: Any, path: List[Any]) -> List[float]:
    """Project ``p`` onto the nearest segment of ``path`` (Euclidean)."""
    best = [p[0], p[1]]
    best_d = float("inf")
    for a, b in zip(path, path[1:]):
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 == 0:
            q = (ax, ay)
        else:
            t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg2
            t = max(0.0, min(1.0, t))
            q = (ax + t * dx, ay + t * dy)
        d = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
        if d < best_d:
            best_d = d
            best = [q[0], q[1]]
    return best


def _point_near_segments(p: Any, index: Dict[Tuple[int, int], List[Any]], cell: float, tol: float) -> bool:
    """True when ``p`` lies within ``tol`` of any segment in the grid index."""
    kx, ky = int(p[0] / cell), int(p[1] / cell)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for a, b in index.get((kx + dx, ky + dy), []):
                if _seg_point_dist(p, a, b) <= tol:
                    return True
    return False


def _project_path_point(p: Any, path: List[Any]) -> Tuple[List[float], int, float]:
    """Project ``p`` onto ``path``. Returns (point, segment_index, t) so a
    sub-arc of the path can be extracted later."""
    best = ([p[0], p[1]], 0, 0.0)
    best_d = float("inf")
    for i in range(len(path) - 1):
        ax, ay = path[i][0], path[i][1]
        bx, by = path[i + 1][0], path[i + 1][1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 == 0:
            continue
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        q = [ax + dx * t, ay + dy * t]
        d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
        if d < best_d:
            best_d, best = d, (q, i, t)
    return best


def _arc_len_between(
    pa: List[float], ia: int, ta: float,
    pb: List[float], ib: int, tb: float,
    run: List[Any],
) -> float:
    """Arc length along ``run`` between two projected points."""
    if ia > ib or (ia == ib and ta > tb):
        pa, pb, ia, ib, ta, tb = pb, pa, ib, ia, tb, ta
    if ia == ib:
        return ((pb[0] - pa[0]) ** 2 + (pb[1] - pa[1]) ** 2) ** 0.5
    length = ((run[ia + 1][0] - pa[0]) ** 2 + (run[ia + 1][1] - pa[1]) ** 2) ** 0.5
    for j in range(ia + 1, ib):
        a2, b2 = run[j], run[j + 1]
        length += ((b2[0] - a2[0]) ** 2 + (b2[1] - a2[1]) ** 2) ** 0.5
    length += ((pb[0] - run[ib][0]) ** 2 + (pb[1] - run[ib][1]) ** 2) ** 0.5
    return length


def _segment_rides_region(a: Any, b: Any, region_lines: List[Any], tol: float) -> bool:
    """True when segment ``a->b`` runs ALONG a rerouted region (not merely
    crosses it): both endpoints lie within ``tol`` of the SAME region run and
    the region arc between their projections is a real run (> 2*tol)
    comparable to the chord. Perpendicular cross-streets project both ends to
    ~the same point (arc ~ 0) and are left untouched."""
    for run in region_lines:
        if len(run) < 2:
            continue
        pa, ia, ta = _project_path_point(a, run)
        pb, ib, tb = _project_path_point(b, run)
        da = ((a[0] - pa[0]) ** 2 + (a[1] - pa[1]) ** 2) ** 0.5
        db = ((b[0] - pb[0]) ** 2 + (b[1] - pb[1]) ** 2) ** 0.5
        # Small slack so a point sitting exactly at the tolerance boundary
        # (e.g. a relayed line 1e-4 from the old path) still counts.
        if da > tol * 1.01 or db > tol * 1.01:
            continue
        arc = _arc_len_between(pa, ia, ta, pb, ib, tb, run)
        chord = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        if arc > 2 * tol and chord >= 0.3 * arc:
            return True
    return False


def _splice_new_path(a: Any, b: Any, new_path: List[Any]) -> List[List[float]]:
    """Coordinates of ``new_path`` between the projections of ``a`` and ``b``:
    the interpolated endpoints plus the new path's own intermediate vertices
    (deduplicated). Replaces the segment a->b with the reroute geometry."""
    pa, ia, ta = _project_path_point(a, new_path)
    pb, ib, tb = _project_path_point(b, new_path)
    if ia > ib or (ia == ib and ta > tb):
        pa, pb, ia, ib, ta, tb = pb, pa, ib, ia, tb, ta
    if ia == ib:
        out = [pa]
        if (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 > 1e-18:
            out.append(pb)
        return out
    out = [pa]
    for j in range(ia + 1, ib + 1):
        out.append(list(new_path[j]))
    if (out[-1][0] - pb[0]) ** 2 + (out[-1][1] - pb[1]) ** 2 > 1e-18:
        out.append(pb)
    return out


def _dedup_coords(coords: List[List[float]]) -> List[List[float]]:
    """Drop consecutive exact-duplicate coordinates (splice boundaries)."""
    out: List[List[float]] = []
    for p in coords:
        if out and (out[-1][0] - p[0]) ** 2 + (out[-1][1] - p[1]) ** 2 < 1e-18:
            continue
        out.append(p)
    return out


def _snap_vertex_to_new_path(
    p: Any,
    region_lines: List[Any],
    new_path: List[Any],
    tol: float,
) -> Optional[List[float]]:
    """If vertex ``p`` lies within ``tol`` of an OLD rerouted region, return
    its projection onto the NEW path (the vertex rides the moved corridor and
    must follow). Returns None when the vertex is not near any region run —
    used as a fallback for dense polylines whose individual segments are
    shorter than the splice arc threshold."""
    for run in region_lines:
        if len(run) < 2:
            continue
        q, _, _ = _project_path_point(p, run)
        d = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
        if d <= tol * 1.01:
            proj, _, _ = _project_path_point(p, new_path)
            return proj
    return None


def _relay_dependents(by_layer: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Re-lay the rerouted REGION of every other corridor line layer onto the
    rerouted line — the whole corridor follows any reroute (trench -> ducts
    + cables; feeder -> distribution + drop; cable -> trench + ducts). Only
    the moved region moves; shared endpoints and parts on other (unmoved)
    lines stay exactly where they are. Returns counts per relayed layer."""
    relayed: Dict[str, int] = {}

    # Approved-changed lines whose geometry genuinely differs from the BEFORE
    # state. The before/after pair is carried INSIDE the approved dataset (the
    # survey change's own frozen original_geometry) — never a live HLD lookup.
    changed: List[Tuple[str, List[Any], List[Any], Dict[str, Any]]] = []
    for layer, feats in by_layer.items():
        for f in feats:
            props = f.get("properties") or {}
            if not props.get("approved"):
                continue
            orig_lines = _line_strings(props.get("original_geometry"))
            cur_lines = _line_strings(f.get("geometry"))
            if not orig_lines or not cur_lines:
                continue
            if orig_lines == cur_lines:
                continue
            changed.append((layer, orig_lines[0], cur_lines[0], props))

    # Trenches re-lay first so their ducts/cables follow the trench; then a
    # rerouted duct re-lays its cable onto the duct; a rerouted cable re-lays
    # the trench/ducts under it (physical precedence: cable rides in the
    # duct, duct rides in the trench).
    changed.sort(
        key=lambda c: (
            0 if (c[0] in TRENCH_LAYERS or c[0] in TRENCH_SUB_LAYERS)
            else (1 if c[0] in DUCT_LAYERS else 2),
            c[0],
        )
    )

    for layer, orig_path, new_path, cprops in changed:
        # Hierarchy guard: a downstream reroute (distribution/garden) must
        # never drag the upstream feeder backbone that merely runs near the
        # same corridor (see _reroute_tier above).
        changed_tier = _reroute_tier(layer, cprops)
        # Every other corridor line layer follows this reroute.
        dependents = [d for d in CORRIDOR_LINE_LAYERS if d != layer]
        if not dependents:
            continue

        # ── The changed REGION of the original path = the runs of the
        #    original that the new path no longer covers. Endpoints are
        #    shared, so only the moved middle is uncovered. Uses the tight
        #    reroute tolerance so even a small (few-metre) reroute counts. ──
        rtol = LLD_REROUTE_TOL
        new_index, new_cell = _build_segment_index([new_path], rtol)
        orig_uncovered = _uncovered_points(orig_path, new_index, new_cell, rtol)
        if not orig_uncovered:
            continue  # structurally different but spatially identical
        region_lines = _runs(orig_uncovered)
        if not region_lines:
            continue
        for dep_layer in dependents:
            for f in by_layer.get(dep_layer, []):
                props = f.get("properties") or {}
                if props.get("approved"):
                    continue  # engineer already re-drew this dependent
                if _tier_blocks_reroute(changed_tier, _reroute_tier(dep_layer, props)):
                    continue  # upstream tier (e.g. feeder) never follows a downstream reroute
                geom = f.get("geometry") or {}
                lines = _line_strings(geom)
                if not lines:
                    continue
                new_lines = []
                moved_any = 0
                for ln in lines:
                    if len(ln) < 2:
                        new_lines.append([list(p) for p in ln])
                        continue
                    new_coords: List[List[float]] = []
                    moved = 0
                    for i in range(len(ln) - 1):
                        a, b = ln[i], ln[i + 1]
                        if _segment_rides_region(a, b, region_lines, LLD_RELAY_TOL):
                            # The segment runs ALONG the moved region — splice
                            # the reroute's own geometry in (this also covers
                            # sparse polylines whose crossing happens BETWEEN
                            # two vertices, which vertex-projection missed).
                            splice = _splice_new_path(a, b, new_path)
                            if len(splice) >= 2:
                                if new_coords and (
                                    (new_coords[-1][0] - splice[0][0]) ** 2
                                    + (new_coords[-1][1] - splice[0][1]) ** 2
                                ) < 1e-18:
                                    new_coords.extend(splice[1:])
                                else:
                                    new_coords.extend(splice)
                                moved += 1
                            else:
                                new_coords.append(list(b))
                        else:
                            # Dense polyline: the segment is too short to
                            # qualify for a splice, but its far vertex rides
                            # the moved region — snap it onto the new path so
                            # the line follows vertex by vertex.
                            snapped = _snap_vertex_to_new_path(
                                b, region_lines, new_path, LLD_RELAY_TOL
                            )
                            if snapped is not None:
                                new_coords.append(snapped)
                                moved += 1
                            else:
                                new_coords.append(list(b))
                    new_coords = _dedup_coords(new_coords)
                    new_lines.append(new_coords)
                    moved_any += moved
                if not moved_any:
                    continue
                if geom.get("type") == "MultiLineString":
                    f["geometry"] = {"type": "MultiLineString", "coordinates": new_lines}
                else:
                    f["geometry"] = {"type": "LineString", "coordinates": new_lines[0] if new_lines else []}
                props["lld_relayed"] = True
                props["lld_relay_reason"] = "followed rerouted %s (%d segments re-laid)" % (layer, moved_any)
                relayed[dep_layer] = relayed.get(dep_layer, 0) + 1
    return relayed


def _purge_old_region(by_layer: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Hard guarantee: after the relay and coverage passes, no corridor line
    may still ride the OLD region of an approved reroute. Anything the relay
    missed (e.g. a dependent that was itself approved-and-re-drawn, or a line
    added by the coverage pass along the old footprint) is snapped onto the
    new path. This enforces the invariant "the old path is never used" no
    matter what the input dataset contains.

    Returns a count of purged features per layer.
    """
    purged: Dict[str, int] = {}

    # Old regions of approved reroutes.
    old_regions: List[Tuple[str, List[Any], List[Any]]] = []
    for layer, feats in by_layer.items():
        for f in feats:
            props = f.get("properties") or {}
            if not props.get("approved"):
                continue
            orig_lines = _line_strings(props.get("original_geometry"))
            cur_lines = _line_strings(f.get("geometry"))
            if not orig_lines or not cur_lines:
                continue
            if orig_lines == cur_lines:
                continue
            rtol = LLD_REROUTE_TOL
            new_index, new_cell = _build_segment_index(cur_lines, rtol)
            uncovered = _uncovered_points(orig_lines[0], new_index, new_cell, rtol)
            region_lines = _runs(uncovered)
            if region_lines:
                old_regions.append(
                    (layer, region_lines, cur_lines[0], _reroute_tier(layer, props))
                )

    if not old_regions:
        return purged

    for layer in CORRIDOR_LINE_LAYERS:
        for f in by_layer.get(layer, []):
            props = f.get("properties") or {}
            if props.get("approved"):
                continue  # the engineer's own re-draw is authoritative
            dep_tier = _reroute_tier(layer, props)
            geom = f.get("geometry") or {}
            lines = _line_strings(geom)
            if not lines:
                continue
            new_lines = []
            moved_any = 0
            for ln in lines:
                if len(ln) < 2:
                    new_lines.append([list(p) for p in ln])
                    continue
                new_coords: List[List[float]] = []
                moved = 0
                for i in range(len(ln) - 1):
                    a, b = ln[i], ln[i + 1]
                    # Splice the segment onto the reroute's new path when it
                    # rides an old region. Covers sparse polylines whose
                    # crossing lies BETWEEN two vertices (vertex-projection
                    # could never bend those). Perpendicular cross-streets are
                    # left untouched (_segment_rides_region rejects them).
                    splice = None
                    snapped = None
                    best_d = float("inf")
                    for (_ol, region_lines, new_path, rtier) in old_regions:
                        if _tier_blocks_reroute(rtier, dep_tier):
                            continue  # upstream tier: exempt from this reroute
                        if _segment_rides_region(a, b, region_lines, LLD_RELAY_TOL):
                            mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
                            q = _nearest_point_on_path(mid, new_path)
                            d = (mid[0] - q[0]) ** 2 + (mid[1] - q[1]) ** 2
                            if d < best_d:
                                best_d = d
                                splice = _splice_new_path(a, b, new_path)
                        elif snapped is None:
                            # Dense polyline fallback: far vertex rides an
                            # old region — snap it onto that reroute's path.
                            snapped = _snap_vertex_to_new_path(
                                b, region_lines, new_path, LLD_RELAY_TOL
                            )
                    if splice is not None and len(splice) >= 2:
                        if new_coords and (
                            (new_coords[-1][0] - splice[0][0]) ** 2
                            + (new_coords[-1][1] - splice[0][1]) ** 2
                        ) < 1e-18:
                            new_coords.extend(splice[1:])
                        else:
                            new_coords.extend(splice)
                        moved += 1
                    elif snapped is not None:
                        new_coords.append(snapped)
                        moved += 1
                    else:
                        new_coords.append(list(b))
                new_coords = _dedup_coords(new_coords)
                new_lines.append(new_coords)
                moved_any += moved
            if not moved_any:
                continue
            if geom.get("type") == "MultiLineString":
                f["geometry"] = {"type": "MultiLineString", "coordinates": new_lines}
            else:
                f["geometry"] = {"type": "LineString", "coordinates": new_lines[0] if new_lines else []}
            props["lld_purged"] = True
            props["lld_purge_reason"] = "old rerouted region no longer used (%d segments re-laid)" % moved_any
            purged[layer] = purged.get(layer, 0) + 1
    return purged


def _nearest_same_layer_props(
    features: List[Dict[str, Any]],
    approved: Dict[str, Any],
) -> Dict[str, Any]:
    """Inherit design attributes from the nearest NON-approved generated
    feature of the same layer that rides the approved corridor (within relay
    tolerance). Used when a fresh pipeline assigns new feature ids, so the
    survey-authoritative feature is appended rather than matched: pipeline
    fields (trench_type, USAGE_TYPE, lengths, INFRA_STATUS ...) survive
    instead of being replaced by the survey attrs alone."""
    ap = approved.get("properties") or {}
    alines = _line_strings(approved.get("geometry"))
    if not alines:
        return {}
    p = alines[0][0]
    best: Dict[str, Any] = {}
    best_d = float("inf")
    for f in features:
        props = f.get("properties") or {}
        if props.get("approved") or props.get("lld_created"):
            continue
        for ln in _line_strings(f.get("geometry")):
            q = _nearest_point_on_path(p, ln)
            d = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
            if d < best_d:
                best_d, best = d, props
    if best_d > LLD_RELAY_TOL:
        return {}
    out = {k: v for k, v in best.items()}
    out.pop("feature_id", None)  # the survey id stays authoritative
    return out


# Canonical construction classes. The trench network is published with a
# CLOSED 3-value set (Open Cut / HDD / Garden) so BOQ, permits and the field
# app can rely on it. HLD baselines produced before that redesign — and legacy
# survey imports — still carry the tier labels (Feeder / Distribution), which
# the LLD would otherwise pass straight through into its own outputs.
_TRENCH_CLASS_KEYS = ("trench_type", "USAGE_TYPE", "CONSTRUCT")


def _canonical_trench_class(value: Any) -> Optional[str]:
    """Map any historical trench label onto Open Cut / HDD / Garden / Aerial."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    # AERIAL IS TESTED FIRST. It used to fall through to the Open Cut default,
    # so every aerial span the LLD republished was labelled as excavated civil
    # work — billed and permitted as a trench it is not. "Aerial_Drop" and
    # "Aerial Drop" additionally contain "drop", which the garden branch below
    # would otherwise claim as Garden, so the order matters twice over.
    if "aerial" in s or "overhead" in s:
        return "Aerial"
    if "garden" in s or "drop" in s:
        return "Garden"
    if "hdd" in s or "drill" in s or "bore" in s or "trenchless" in s:
        return "HDD"
    # Feeder / Distribution / Open Cut / micro-trench / anything else is civil
    # open-cut excavation.
    return "Open Cut"


def _is_aerial_props(props: Dict[str, Any]) -> bool:
    """True when a feature's own attributes already say it is aerial.

    Two independent signals, because a published aerial row may carry either:
    ``EXCAVATION = 0`` (the contract the BOQ reads) or an aerial construction
    class. Mirrors ``utils/attr_enrich.is_aerial_row`` on the backend side.
    """
    for key in ("EXCAVATION",):
        v = str(props.get(key) if props.get(key) is not None else "").strip().lower()
        if v in ("0", "false", "no"):
            return True
    for key in ("trench_type", "TRENCH_TYPE", "CONSTRUCT", "USAGE_TYPE",
                "CONSTRUCTION_METHOD"):
        v = str(props.get(key) or "").strip().lower()
        if v and ("aerial" in v or "overhead" in v):
            return True
    return False


def _normalize_trench_construction_class(
    by_layer: Dict[str, List[Dict[str, Any]]],
) -> int:
    """Force trench_type / USAGE_TYPE / CONSTRUCT to the closed 3-value set.

    Runs on the LLD's trench layers only (``final_trenches`` plus any legacy
    sub-layers). The baseline label is preserved in ``TRENCH_TIER`` when it was
    a tier (Feeder / Distribution) so nothing is silently lost, and the network
    tier used for reroute propagation is read earlier in the pipeline — this is
    a publication-time normalisation, not a routing input.

    ``aerial_spans`` is deliberately excluded: it keeps its own
    ``Aerial_Drop`` class. Returns the number of normalised features.
    """
    names = ["final_trenches"] + sorted(TRENCH_SUB_LAYERS)
    changed = 0
    for name in names:
        for f in by_layer.get(name) or []:
            props = f.setdefault("properties", {})
            raw = None
            for key in _TRENCH_CLASS_KEYS:
                if props.get(key) not in (None, ""):
                    raw = props.get(key)
                    break
            if raw is None:
                continue
            raw_s = str(raw).strip()
            canon = _canonical_trench_class(raw_s)
            if canon is None:
                continue
            if raw_s.lower() != canon.lower():
                # Remember what the baseline called it (Feeder/Distribution/…).
                if not props.get("TRENCH_TIER"):
                    props["TRENCH_TIER"] = raw_s
                changed += 1
            # Write the canonical class to all three aliases (filling the ones
            # the baseline left empty) so every trench feature carries the same
            # uniform construction class as the HLD publication.
            for key in _TRENCH_CLASS_KEYS:
                props[key] = canon
    return changed


def _inherit_trench_sub_layer_attributes(by_layer: Dict[str, List[Dict[str, Any]]]) -> int:
    """final_trenches is the combination of feeder + distribution (+ garden,
    mirrored into it by the pipeline) — every final trench feature should
    therefore carry the design attributes of its component layer. A survey-
    appended authoritative feature may lack them (its props are the survey
    attrs alone), so inherit trench_type / USAGE_TYPE / SURFACE / CONSTRUCT /
    DEPTH_MM / WIDTH_MM / INFRA_STATUS from the nearest sub-layer feature
    riding the same corridor. Returns the number of enriched features."""
    finals = by_layer.get("final_trenches") or []
    if not finals:
        return 0
    subs: List[Tuple[List[Any], Dict[str, Any]]] = []
    for sub in sorted(TRENCH_SUB_LAYERS):
        for f in by_layer.get(sub, []) or []:
            lines = _line_strings(f.get("geometry"))
            if lines:
                subs.append((lines[0], f.get("properties") or {}))
    if not subs:
        return 0
    inherited = 0
    for tf in finals:
        props = tf.setdefault("properties", {})
        if props.get("trench_type"):
            continue  # already carries the component attribute
        # An aerial span is never re-classified as an excavated class by
        # inheriting from a neighbouring trench it happens to ride over.
        if _is_aerial_props(props):
            continue
        lines = _line_strings(tf.get("geometry"))
        if not lines:
            continue
        line = lines[0]
        samples = [line[0], line[-1]]
        if len(line) > 2:
            samples.append(line[len(line) // 2])
        best: Dict[str, Any] = {}
        best_d = float("inf")
        for slines, sprops in subs:
            total = 0.0
            for p in samples:
                q = _nearest_point_on_path(p, slines)
                total += ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
            if total < best_d:
                best_d, best = total, sprops
        if best_d > LLD_RELAY_TOL:
            continue
        for key in ("trench_type", "USAGE_TYPE", "SURFACE", "CONSTRUCT",
                    "REINSTATE", "DEPTH_MM", "WIDTH_MM", "INFRA_STATUS",
                    "VERIFY_STATUS"):
            if key in props or best.get(key) is None:
                continue
            # The construction class is a CLOSED set (Open Cut / HDD / Garden /
            # Aerial). Sub-layers carry legacy tier labels (Feeder /
            # Distribution) — inheriting one of those would corrupt the
            # classification the engineer and BOQ rely on, so only the
            # canonical values are accepted. Aerial belongs in the list: it was
            # missing, so an aerial span could not inherit "Aerial" and any
            # neighbour offering "Open Cut" won instead.
            if key in ("trench_type", "USAGE_TYPE", "CONSTRUCT"):
                if str(best.get(key)).strip() not in ("Open Cut", "HDD", "Garden",
                                                      "Aerial"):
                    continue
            props[key] = best[key]
        props["lld_attr_source"] = "inherited from sub-layer"
        inherited += 1
    return inherited


# ---------------------------------------------------------------------------
# LLD — drop-connection planning for new/moved premises (Phase 1)
#
# Approved survey changes that ADD a premise (object) only copy the point
# into the objects layer — the LLD then emits it with no serving drop duct,
# no garden trench and no distribution cable, so the premise is never
# connected to the network. This stage gives every orphan premise a proper
# layout: polygon/PDP assignment, a garden trench + drop duct routed from
# the nearest distribution-duct network point to the object, and the
# serving distribution cable along the same path. Created features carry
# ``lld_created`` + a human-readable reason so the run is auditable.
# ---------------------------------------------------------------------------

# A premise is considered unserved when its nearest drop-duct endpoint is
# farther than this (~10 m at survey latitudes; a real drop ends AT the
# object, so existing premises sit at ~0).
DROP_CONNECT_TOL = 0.0001


def _project_point_on_segment(p: Any, a: Any, b: Any) -> List[float]:
    """Foot of the perpendicular from p onto segment [a, b] (nearest endpoint
    when the projection falls outside the segment)."""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return [ax, ay]
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return [ax + t * dx, ay + t * dy]


def _point_in_ring(p: Any, ring: List[Any]) -> bool:
    """Ray-casting point-in-polygon test for a single ring."""
    x, y = p[0], p[1]
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(p: Any, geom: Optional[Dict[str, Any]]) -> bool:
    """True if point lies inside a Polygon / MultiPolygon GeoJSON geometry.
    Only the outer ring of each polygon is tested (premises never sit in
    holes for the purpose of serving-polygon assignment)."""
    if not geom:
        return False
    coords = geom.get("coordinates") or []
    polys = coords if geom.get("type") == "MultiPolygon" else [coords]
    for poly in polys:
        if not poly:
            continue
        ring = poly[0]
        if ring and len(ring) >= 3 and _point_in_ring(p, ring):
            return True
    return False


def _polygon_centroid(geom: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    """Average of the outer-ring vertices (fallback for polygon assignment
    when the premise sits just outside the polygon boundary)."""
    if not geom:
        return None
    coords = geom.get("coordinates") or []
    polys = coords if geom.get("type") == "MultiPolygon" else [coords]
    for poly in polys:
        ring = poly[0] if poly else None
        if not ring:
            continue
        xs = sum(v[0] for v in ring) / len(ring)
        ys = sum(v[1] for v in ring) / len(ring)
        return [xs, ys]
    return None


def _approx_meters(pts: List[Any]) -> float:
    """Approximate length in metres of a polyline of lon/lat points."""
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dy = (b[1] - a[1]) * 111_320.0
        dx = (b[0] - a[0]) * 111_320.0 * max(0.1, math.cos(math.radians((a[1] + b[1]) / 2)))
        total += (dx * dx + dy * dy) ** 0.5
    return round(total, 1)


def _plan_drop_connections(by_layer: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Ensure every premise (object) has a serving drop connection.

    Returns per-layer counts of created features (drop_ducts /
    distribution_cable / final_trenches). Idempotent per run: an object is
    only touched when something is genuinely missing — a serving drop duct
    (its garden trench is mirrored into final_trenches like the HLD does)
    and/or a serving distribution cable.

    Key invariant: the distribution cable flows THROUGH the drop duct — both
    must share the exact same geometry. When the survey engineer already drew
    a cable into the premise, the duct is routed along that cable's final
    segment; when a drop duct already exists, the missing cable is built along
    the duct's exact path. Everything created is tagged ``lld_created`` so
    downstream validation and the design package can distinguish it from
    HLD-derived features.
    """
    created: Dict[str, int] = {
        "drop_ducts": 0,
        "distribution_cable": 0,
        "final_trenches": 0,
        "aerial_spans": 0,
        "aerial_cable": 0,
    }
    objects = by_layer.get("objects") or []
    if not objects:
        return created

    from lld_cable_geometry import drop_capacity_warning, drop_fiber_capacity

    # Existing drop-duct endpoints — a drop duct ends AT its premise.
    drop_ends: List[Any] = []
    for f in by_layer.get("drop_ducts") or []:
        for ln in _line_strings(f.get("geometry")):
            if len(ln) >= 2:
                drop_ends.append(ln[0])
                drop_ends.append(ln[-1])

    # Existing distribution-cable endpoints — a serving cable ends AT its
    # premise too. Checking these (in addition to drop ducts) stops the
    # planner from duplicating a cable the survey engineer already drew.
    cable_ends: List[Any] = []
    for f in by_layer.get("distribution_cable") or []:
        for ln in _line_strings(f.get("geometry")):
            if len(ln) >= 2:
                cable_ends.append(ln[0])
                cable_ends.append(ln[-1])

    # Distribution-duct network segments — the tap point for the drop.
    dist_segs: List[Tuple[Any, Any]] = []
    for f in by_layer.get("distribution_ducts") or []:
        for ln in _line_strings(f.get("geometry")):
            for a, b in zip(ln, ln[1:]):
                dist_segs.append((a, b))

    polygons = by_layer.get("polygons") or []
    pdps = by_layer.get("pdps") or []

    # Map polygon id -> PDP properties. PDPs are named after their serving
    # polygon (label 'Network_POLY00001' -> PDP00001).
    pdp_by_poly: Dict[str, Dict[str, Any]] = {}
    for p in pdps:
        props = p.get("properties") or {}
        label = str(props.get("label") or props.get("SRC_ID") or "")
        m = re.search(r"(POLY\d+)", label, re.IGNORECASE)
        if m:
            pdp_by_poly.setdefault(m.group(1).upper(), props)

    def nearest_polygon(p: Any) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        best_d = float("inf")
        for poly in polygons:
            geom = poly.get("geometry")
            props = poly.get("properties") or {}
            if _point_in_polygon(p, geom):
                return props
            c = _polygon_centroid(geom)
            if c:
                d = ((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best = props
        return best

    def make_feature(layer: str, geom: Dict[str, Any], props: Dict[str, Any], kind: str) -> None:
        props = dict(props)
        props["feature_id"] = "LLD-%s-%s" % (layer, uuid.uuid4().hex[:10])
        props["layer"] = layer
        props["lld_created"] = True
        props["lld_reason"] = kind
        by_layer.setdefault(layer, []).append(
            {"type": "Feature", "geometry": geom, "properties": props}
        )
        created[layer] = created.get(layer, 0) + 1

    max_uid = 0
    for f in by_layer.get("drop_ducts") or []:
        try:
            max_uid = max(max_uid, int((f.get("properties") or {}).get("DUCT_UID") or 0))
        except (TypeError, ValueError):
            pass

    def served_by(ends: List[Any], o: Any) -> bool:
        return any(
            ((o[0] - e[0]) ** 2 + (o[1] - e[1]) ** 2) ** 0.5 <= DROP_CONNECT_TOL
            for e in ends
        )

    def serving_feature(layer: str, o: Any) -> Optional[Dict[str, Any]]:
        """Return the feature in `layer` that has an endpoint AT the premise."""
        for f in by_layer.get(layer) or []:
            for ln in _line_strings(f.get("geometry")):
                if len(ln) >= 2 and (
                    ((ln[0][0] - o[0]) ** 2 + (ln[0][1] - o[1]) ** 2) ** 0.5 <= DROP_CONNECT_TOL
                    or ((ln[-1][0] - o[0]) ** 2 + (ln[-1][1] - o[1]) ** 2) ** 0.5 <= DROP_CONNECT_TOL
                ):
                    return f
        return None

    def path_ending_at(ln: List[Any], o: Any) -> List[Any]:
        """Return a path along `ln` that ENDS at the premise."""
        if len(ln) >= 2 and ((ln[0][0] - o[0]) ** 2 + (ln[0][1] - o[1]) ** 2) <= DROP_CONNECT_TOL:
            return [ln[1], ln[0]]
        return ln

    def _nearest_pole_id(o: Any) -> Optional[str]:
        poles = by_layer.get("poles") or []
        best_d, best_id = float("inf"), None
        for pole in poles:
            for pt in _points(pole.get("geometry")):
                d = ((o[0] - pt[0]) ** 2 + (o[1] - pt[1]) ** 2) ** 0.5
                if d < best_d:
                    best_d, best_id = d, (pole.get("properties") or {}).get("POLE_ID") or pole.get("properties", {}).get("pole_id")
        return best_id

    def _pole_point_by_id(pole_id: str, by_layer: Dict[str, List[Dict[str, Any]]]) -> Optional[Any]:
        poles = by_layer.get("poles") or []
        for pole in poles:
            props = pole.get("properties") or {}
            if str(props.get("POLE_ID") or props.get("pole_id") or "") == pole_id:
                pts = _points(pole.get("geometry"))
                if pts:
                    return pts[0]
        return None

    def _route_aerial_drop(pole_pt: Any, premise_pt: Any, by_layer: Dict[str, List[Dict[str, Any]]]) -> List[Any]:
        dist = ((pole_pt[0] - premise_pt[0]) ** 2 + (pole_pt[1] - premise_pt[1]) ** 2) ** 0.5
        if dist > 0.0001:
            return [pole_pt, premise_pt]
        return []

    for obj in objects:
        pts = _points(obj.get("geometry"))
        if not pts:
            continue
        o = pts[0]
        has_drop = served_by(drop_ends, o)
        has_cable = served_by(cable_ends, o)
        if has_drop and has_cable:
            continue  # fully served — nothing to do
        props = obj.get("properties") or {}
        obj_ref = props.get("feature_id") or ("%s,%s" % (round(o[0], 6), round(o[1], 6)))

        # 1. Assign serving polygon + PDP/MFG.
        poly_props = nearest_polygon(o)
        poly_id = (poly_props or {}).get("SRC_ID") or props.get("POLYGON_ID")
        pdp = pdp_by_poly.get(str(poly_id).upper()) if poly_id else None
        pdp_id = (pdp or {}).get("PDP_ID") or props.get("PDP_ID")
        mfg_id = (pdp or {}).get("MFG_ID") or props.get("MFG_ID")
        if not pdp_id:
            # Fallback: nearest PDP point.
            best_d, best_p = float("inf"), None
            for pdp_f in pdps:
                for pp in _points(pdp_f.get("geometry")):
                    d = ((o[0] - pp[0]) ** 2 + (o[1] - pp[1]) ** 2) ** 0.5
                    if d < best_d:
                        best_d, best_p = d, pdp_f.get("properties") or {}
            if best_p:
                pdp_id = best_p.get("PDP_ID") or best_p.get("SRC_ID")
                mfg_id = mfg_id or best_p.get("MFG_ID")

        # 2. Determine the serving path. The distribution cable must flow
        #    THROUGH the drop duct, so cable and duct share one geometry:
        #      a) cable already drawn by the survey engineer → route the new
        #         drop duct along the cable's final segment into the premise
        #      b) drop duct already exists → build the missing cable along the
        #         duct's exact path
        #      c) neither → route from the nearest distribution-network point
        serving_cable = serving_feature("distribution_cable", o)
        serving_duct = serving_feature("drop_ducts", o)
        path: List[Any] = []
        if not has_drop and has_cable and serving_cable is not None:
            ln = path_ending_at(
                _line_strings(serving_cable.get("geometry"))[0], o
            )
            path = ln[-2:] if len(ln) >= 2 else ln
        elif not has_cable and has_drop and serving_duct is not None:
            path = path_ending_at(_line_strings(serving_duct.get("geometry"))[0], o)
        else:
            # Both missing — route from the nearest distribution-duct network
            # point (footway side).
            best_seg, best_d = None, float("inf")
            for a, b in dist_segs:
                d = _seg_point_dist(o, a, b)
                if d < best_d:
                    best_d, best_seg = d, (a, b)
            if best_seg is not None:
                tap = _project_point_on_segment(o, best_seg[0], best_seg[1])
            else:
                tap = o  # no distribution network — validation will flag
            if (tap[0] - o[0]) ** 2 + (tap[1] - o[1]) ** 2 < 1e-14:
                continue  # degenerate zero-length connection
            path = [tap, o]

        if len(path) < 2:
            continue
        length_m = _approx_meters(path)
        max_uid += 1
        common = {
            "PDP_ID": str(pdp_id or ""),
            "POLYGON_ID": str(poly_id).upper() if poly_id else "",
            "MFG_ID": str(mfg_id or ""),
            "ADDR_ID": str(props.get("ADDR_ID") or props.get("addr_id") or ""),
            "INFRA_STATUS": "Proposed",
        }
        try:
            hh_count = max(1, int(float(props.get("households") or props.get("HH") or props.get("hhs") or 1)))
        except (TypeError, ValueError):
            hh_count = 1
        hh = str(hh_count)
        capacity_warning = drop_capacity_warning(hh_count)
        drop_fibers = drop_fiber_capacity(hh_count) or 288
        addr = str(props.get("ADDR_ID") or props.get("addr_id") or props.get("SRC_ID") or "")

        # ── Aerial drop: engineer explicitly flagged aerial_required ──────────
        aerial_required = str(props.get("aerial_required") or props.get("AERIAL_REQUIRED") or "").lower()
        if aerial_required in ("true", "1", "yes", "y") and not has_drop and not has_cable:
            pole_id = str(props.get("assigned_pole") or props.get("POLE_ID") or "")
            if not pole_id:
                pole_id = _nearest_pole_id(o)
            if pole_id:
                pole_pt = _pole_point_by_id(pole_id, by_layer)
                if pole_pt:
                    aerial_path = _route_aerial_drop(pole_pt, o, by_layer)
                    if aerial_path and len(aerial_path) >= 2:
                        length_m = _approx_meters(aerial_path)
                        make_feature(
                            "aerial_spans",
                            {"type": "LineString", "coordinates": aerial_path},
                            {
                                **common,
                                "TRENCH_TYPE": "Aerial_Drop",
                                "CONSTRUCTION_METHOD": "Overhead",
                                "CABLE_TYPE": "Aerial",
                                "FIBRE_COUNT": drop_fibers,
                                "FIBER_COUNT": drop_fibers,
                                "HH_COUNT": hh_count,
                                "RESERVED_SPARE_FIBERS": LLD_RESERVED_SPARE_FIBERS,
                                "AVAILABLE_FIBERS": max(0, drop_fibers - hh_count - LLD_RESERVED_SPARE_FIBERS),
                                "UTIL_PCT": min(100.0, round(((hh_count + LLD_RESERVED_SPARE_FIBERS) / float(drop_fibers)) * 100.0, 1)),
                                "CAPACITY_STATUS": "OVER_CAPACITY" if capacity_warning else "OK",
                                "CAPACITY_WARNING": capacity_warning or "",
                                "REVIEW": 1 if capacity_warning else 0,
                                "FROM_POLE": pole_id,
                                "TO_PREMISE": addr,
                                "POLE_SPACING_M": 50.0,
                                "LENGTH_M": length_m,
                                "AERIAL_REASON": "survey_flagged",
                            },
                            "aerial drop trench for %s (engineer flagged)" % obj_ref,
                        )
                        make_feature(
                            "aerial_cable",
                            {"type": "LineString", "coordinates": aerial_path},
                            {
                                **common,
                                "CABLE_TYPE": "Aerial",
                                "FIBER_COUNT": drop_fibers,
                                "HH_COUNT": hh_count,
                                "RESERVED_SPARE_FIBERS": LLD_RESERVED_SPARE_FIBERS,
                                "ACTIVE_FIBERS": hh_count,
                                "AVAILABLE_FIBERS": max(0, drop_fibers - hh_count - LLD_RESERVED_SPARE_FIBERS),
                                "CAPACITY_STATUS": "OVER_CAPACITY" if capacity_warning else "OK",
                                "CAPACITY_WARNING": capacity_warning or "",
                                "REVIEW": 1 if capacity_warning else 0,
                                "UTIL_PCT": min(100.0, round(((hh_count + LLD_RESERVED_SPARE_FIBERS) / float(drop_fibers)) * 100.0, 1)),
                                "SOURCE_NODE": pole_id,
                                "hhs": hh,
                                "length_m": length_m,
                                "CONNECTION_TYPE": "Aerial drop",
                            },
                            "aerial drop cable for %s" % obj_ref,
                        )
                        props["AERIAL_TRENCH_ID"] = "AT-%s" % uuid.uuid4().hex[:8]
                        drop_ends.append(o)
                        cable_ends.append(o)
                        continue
            feedback.pushWarning(
                "Premise %s: aerial_required but no pole found — falling back to UG." % obj_ref
            )

        if not has_drop:
            # 3a. One physical-location drop duct + its garden trench. The garden
            #     trench is mirrored into final_trenches exactly like the HLD
            #     pipeline does (trench_type=Garden, micro trench specs) so the construction
            #     plan + BOQ include the garden digging — the standalone
            #     garden_trench layer is NOT emitted in the LLD output.
            make_feature(
                "final_trenches",
                {"type": "LineString", "coordinates": path},
                {
                    **common,
                    "trench_type": "Garden",
                    "USAGE_TYPE": "Garden",
                    "SURFACE": "Footpath",
                    "CONSTRUCT": "Garden",
                    "REINSTATE": "Sidewalk",
                    "DEPTH_MM": 450,
                    "WIDTH_MM": 150,
                    "obj_id": addr,
                    "addr_id": addr,
                    "hhs": hh,
                    "length_m": length_m,
                },
                "auto-created garden trench (mirrored into final_trenches) for %s" % obj_ref,
            )
            make_feature(
                "drop_ducts",
                {"type": "LineString", "coordinates": path},
                {
                    **common,
                    "DUCT_TYPE": "1-Way HDPE",
                    "DIAMETER_MM": 32,
                    "WAYS": 1,
                    "SIDE": "left",
                    "DUCT_UID": max_uid,
                    "HH_ID": hh,
                    "HH_COUNT": hh_count,
                    "LENGTH_M": length_m,
                    "SPARE_PCT": 0.0,
                    "OCCUPANCY_PCT": 100.0,
                },
                "auto-created drop duct to serve approved new premise %s" % obj_ref,
            )

        if not has_cable:
            # 3b. Serving physical-location drop cable when no existing cable
            #     ends at the service entry; preserve one route per location.
            #     Same geometry as the drop duct: the cable flows THROUGH it.
            make_feature(
                "distribution_cable",
                {"type": "LineString", "coordinates": path},
                {
                    **common,
                    "CABLE_TYPE": "Drop",
                    "CONNECTION_TYPE": "Drop (garden leg)",
                    "FIBER_COUNT": drop_fibers,
                    "HH_COUNT": hh_count,
                    "RESERVED_SPARE_FIBERS": LLD_RESERVED_SPARE_FIBERS,
                    "ACTIVE_FIBERS": hh_count,
                    "AVAILABLE_FIBERS": max(0, drop_fibers - hh_count - LLD_RESERVED_SPARE_FIBERS),
                    "UTIL_PCT": min(100.0, round(((hh_count + LLD_RESERVED_SPARE_FIBERS) / float(drop_fibers)) * 100.0, 1)),
                    "CAPACITY_STATUS": "OVER_CAPACITY" if capacity_warning else "OK",
                    "CAPACITY_WARNING": capacity_warning or "",
                    "REVIEW": 1 if capacity_warning else 0,
                    "UTIL_PCT": min(100.0, round(((hh_count + LLD_RESERVED_SPARE_FIBERS) / float(drop_fibers)) * 100.0, 1)),
                    "SOURCE_NODE": str(pdp_id or ""),
                    "hhs": hh,
                    "length_m": length_m,
                },
                "auto-created serving distribution cable for approved new premise %s" % obj_ref,
            )

        # 4. Link the premise itself to its serving polygon / PDP / MFG.
        if poly_id:
            props["POLYGON_ID"] = str(poly_id).upper()
        if pdp_id:
            props["PDP_ID"] = pdp_id
        if mfg_id:
            props["MFG_ID"] = mfg_id

        drop_ends.append(o)
        cable_ends.append(o)

    return created


def _enrich_lld_distribution_cables(by_layer: Dict[str, List[Dict[str, Any]]]) -> int:
    """Rebuild grouped cable geometry and apply HH-based trunk/drop capacity."""
    from lld_cable_geometry import (
        distribution_fiber_capacity,
        drop_capacity_warning,
        drop_fiber_capacity,
        regroup_distribution_cables,
    )

    cables = by_layer.get("distribution_cable") or []
    regroup_distribution_cables(cables)
    updated = 0
    for feature in cables:
        props = feature.setdefault("properties", {})
        members = [v.strip() for v in str(props.get("ADDR_IDS") or props.get("addr_id") or "").split(",") if v.strip()]
        try:
            hh_count = int(float(props.get("HH_COUNT") or props.get("hhs") or len(members) or 1))
        except (TypeError, ValueError):
            hh_count = max(1, len(members))
        is_drop = (
            str(props.get("CABLE_TYPE") or "").strip().lower() == "drop"
            or str(props.get("CONNECTION_TYPE") or "").strip().lower().startswith("drop")
            or str(props.get("CONNECTION_TYPE") or "").strip().lower() == "dedicated drop"
        )
        capacity_warning = drop_capacity_warning(hh_count) if is_drop else None
        fiber_count = (
            drop_fiber_capacity(hh_count) if is_drop
            else distribution_fiber_capacity(hh_count)
        )
        if fiber_count is None:
            fiber_count = 288
        props["ADDR_IDS"] = ",".join(members)
        props["HH_COUNT"] = hh_count
        if is_drop or str(props.get("CABLE_TYPE") or "").strip().lower() in ("", "distribution"):
            props["CABLE_TYPE"] = "Drop" if is_drop else "Distribution"
        props["FIBER_COUNT"] = fiber_count
        props["CAPACITY_STATUS"] = "OVER_CAPACITY" if capacity_warning else "OK"
        props["CAPACITY_WARNING"] = capacity_warning or ""
        props["REVIEW"] = 1 if capacity_warning else 0
        props["RESERVED_SPARE_FIBERS"] = LLD_RESERVED_SPARE_FIBERS
        props["ACTIVE_FIBERS"] = hh_count
        props["AVAILABLE_FIBERS"] = max(
            0, fiber_count - LLD_RESERVED_SPARE_FIBERS - hh_count
        )
        props["UTIL_PCT"] = round(
            min(100.0, (hh_count / float(fiber_count)) * 100.0), 1
        )
        if is_drop:
            props["CONNECTION_TYPE"] = "Drop (garden leg)"
        elif not str(props.get("CONNECTION_TYPE") or "").strip() or \
                str(props.get("CONNECTION_TYPE") or "").strip().lower() == "dedicated drop":
            props["CONNECTION_TYPE"] = "Shared trunk + branches"
        updated += 1
    return updated


def _validate_network(by_layer: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Path-continuity + attribute-consistency validation.

    Reports issues (does not modify the dataset) so the reviewer can see where
    the approved survey edits left gaps or mismatched attributes.
    """
    tol = 0.0005  # ~50m in degrees — connectivity tolerance
    line_layers: List[str] = []
    node_points: List[Any] = []
    line_endpoints: List[Any] = []

    for layer, feats in by_layer.items():
        t = _geom_type(feats)
        if t in ("LineString", "MultiLineString"):
            line_layers.append(layer)
            for f in feats:
                for ln in _line_strings(f.get("geometry")):
                    if len(ln) >= 2:
                        line_endpoints.append(ln[0])
                        line_endpoints.append(ln[-1])
        elif t in ("Point", "MultiPoint"):
            for f in feats:
                node_points.extend(_points(f.get("geometry")))

    count_near = _near_point_counter(node_points + line_endpoints, tol)
    issues: List[Dict[str, Any]] = []

    for layer in line_layers:
        for f in by_layer[layer]:
            fid = (f.get("properties") or {}).get("feature_id")
            for ln in _line_strings(f.get("geometry")):
                if len(ln) < 2:
                    continue
                for ep in (ln[0], ln[-1]):
                    if count_near(ep) <= 1:
                        issues.append({
                            "type": "continuity",
                            "layer": layer,
                            "feature_id": fid,
                            "message": "Disconnected endpoint at (%.5f, %.5f)" % (ep[0], ep[1]),
                        })

    attr_issues = 0
    for layer, feats in by_layer.items():
        for f in feats:
            props = f.get("properties") or {}
            if not props.get("feature_id"):
                issues.append({
                    "type": "attribute",
                    "layer": layer,
                    "message": "Missing feature_id",
                })
                attr_issues += 1

    # Drop connectivity: every premise must have a drop duct ending at it.
    drop_ends = []
    for f in by_layer.get("drop_ducts") or []:
        for ln in _line_strings(f.get("geometry")):
            if len(ln) >= 2:
                drop_ends.append(ln[0])
                drop_ends.append(ln[-1])
    drop_issues = 0
    for f in by_layer.get("objects") or []:
        for o in _points(f.get("geometry")):
            if not any(
                ((o[0] - e[0]) ** 2 + (o[1] - e[1]) ** 2) ** 0.5 <= DROP_CONNECT_TOL
                for e in drop_ends
            ):
                drop_issues += 1
                issues.append({
                    "type": "drop",
                    "layer": "objects",
                    "feature_id": (f.get("properties") or {}).get("feature_id"),
                    "message": "Premise has no serving drop duct",
                })

    return {
        "issues": issues,
        "summary": {
            "layers": len(by_layer),
            "total_features": sum(len(v) for v in by_layer.values()),
            "continuity_issues": sum(1 for i in issues if i["type"] == "continuity"),
            "attribute_issues": attr_issues,
            "drop_issues": drop_issues,
        },
    }


def _run_lld(
    project_id: str,
    lld_version: str,
    dataset: Dict[str, Any],
) -> None:
    task = _lld_task(project_id, lld_version)
    task.update({"status": "running", "stage": "Grouping layers", "updated_at": _now()})
    try:
        # ── 0. Input contract: the ONLY input is the Approved Survey Version
        #       (full layer set + approved survey changes - approved removals).
        #       Every feature in it is survey / survey-confirmed ground truth.
        #       No HLD layers, routing or topology are consulted anywhere in
        #       this pipeline — the LLD is built from the survey dataset alone.
        approved_count = sum(
            1 for f in dataset.get("features") or []
            if (f.get("properties") or {}).get("approved")
        )
        _lld_append(
            project_id, lld_version, "info",
            "LLD input = Approved Survey Version only (%d features, %d approved survey change(s)); no HLD layer references."
            % (len(dataset.get("features") or []), approved_count),
        )

        # ── 1. Group the approved dataset by its `layer` property, preserving
        #       the HLD layer structure. ────────────────────────────────────
        by_layer: Dict[str, List[Dict[str, Any]]] = {}
        for feat in dataset.get("features") or []:
            props = feat.get("properties") or {}
            layer = props.get("layer") or "unknown"
            by_layer.setdefault(layer, []).append(feat)

        if not by_layer:
            raise RuntimeError("Approved dataset contains no features")

        # ── 1a. Reroute propagation: an approved reroute of a trench/duct must
        #       carry the duct/cable laid in it onto the new path, so the LLD
        #       follows the engineer's reroute instead of keeping the old path.
        task.update({"stage": "Applying approved reroutes", "progress": 15, "updated_at": _now()})
        relayed = _relay_dependents(by_layer)
        relay_summary = ", ".join("%s=%d" % (k, v) for k, v in relayed.items() if v)
        if relay_summary:
            _lld_append(
                project_id, lld_version, "info",
                "Reroute propagation: re-laid %s onto the rerouted support path." % relay_summary,
            )
        else:
            _lld_append(
                project_id, lld_version, "info",
                "Reroute propagation: no approved reroutes required re-laying duct/cable.",
            )

        # ── 1a2. Old-path purge: hard guarantee that nothing still rides the
        #       OLD region of an approved reroute. Any line the relay missed is
        #       snapped onto the new path, so the coverage pass below can never
        #       re-create a trench along the old route. "Old path never used."
        task.update({"stage": "Purging old rerouted regions", "progress": 18, "updated_at": _now()})
        purged = _purge_old_region(by_layer)
        purge_summary = ", ".join("%s=%d" % (k, v) for k, v in purged.items() if v)
        if purge_summary:
            _lld_append(
                project_id, lld_version, "info",
                "Old-path purge: snapped %s off the old rerouted region onto the new path." % purge_summary,
            )

        # final_trenches is the combination of feeder + distribution (+ garden):
        # inherit the component attributes (trench_type etc.) onto any final
        # feature that lacks them.
        inherited = _inherit_trench_sub_layer_attributes(by_layer)
        if inherited:
            _lld_append(
                project_id, lld_version, "info",
                "Final-trench attributes: %d feature(s) inherited trench_type/design attrs from feeder/distribution sub-layers."
                % inherited,
            )

        # ── 1b. Cross-layer propagation: every cable must lie on a trench + duct,
        #       every duct must lie on a trench. Missing supporting features are
        #       auto-created along the changed path (tagged ``lld_created``). ────
        task.update({"stage": "Propagating approved changes across layers", "progress": 20, "updated_at": _now()})
        created = _propagate_support_layers(by_layer)
        created_summary = ", ".join("%s=%d" % (k, v) for k, v in created.items() if v)
        if created_summary:
            _lld_append(
                project_id, lld_version, "info",
                "Cross-layer propagation created missing supporting features: %s." % created_summary,
            )
        else:
            _lld_append(
                project_id, lld_version, "info",
                "Cross-layer propagation: all cables lie on a trench + duct and all ducts lie on a trench — no supporting features needed.",
            )

        # ── 1c. Drop-connection planning: new/moved premises added by the
        #       approved survey get a polygon/PDP assignment plus a garden
        #       trench + drop duct + serving distribution cable, so the whole
        #       layout (not just the point) reflects the change. ────────────
        task.update({"stage": "Planning drop connections", "progress": 25, "updated_at": _now()})
        drops = _plan_drop_connections(by_layer)
        drop_summary = ", ".join("%s=%d" % (k, v) for k, v in drops.items() if v)
        if drop_summary:
            _lld_append(
                project_id, lld_version, "info",
                "Drop-connection planning created: %s." % drop_summary,
            )
        else:
            _lld_append(
                project_id, lld_version, "info",
                "Drop-connection planning: all premises already served by a drop duct.",
            )

        for duct_feature in by_layer.get("feeder_ducts") or []:
            duct_props = duct_feature.setdefault("properties", {})
            try:
                pdp_ids = [v.strip() for v in str(duct_props.get("pdp_ids") or "").split(",") if v.strip()]
            except Exception:
                pdp_ids = []
            duct_props["capacity_total"] = LLD_MAX_PDPS_PER_DUCT
            duct_props["capacity_used"] = len(pdp_ids)
            duct_props["capacity_spare"] = max(0, LLD_MAX_PDPS_PER_DUCT - len(pdp_ids))
            duct_props["MAX_PDPS_PER_DUCT"] = LLD_MAX_PDPS_PER_DUCT

        cable_count = _enrich_lld_distribution_cables(by_layer)
        _lld_append(
            project_id, lld_version, "info",
            "LLD cable enrichment: %d distribution cable feature(s), 48-fibre catalogue, 2 reserved spare fibres." % cable_count,
        )

        # Publication-time trench classification: the LLD's own output must
        # speak the same closed 3-value construction class as the HLD, even
        # when the HLD baseline predates the single-trench redesign.
        normalized = _normalize_trench_construction_class(by_layer)
        if normalized:
            _lld_append(
                project_id, lld_version, "info",
                "Trench classification: %d feature(s) normalised to Open Cut / HDD / Garden "
                "(baseline tier kept in TRENCH_TIER)." % normalized,
            )

        task.update({"stage": "Validating network", "progress": 30, "updated_at": _now()})
        validation = _validate_network(by_layer)
        _lld_append(
            project_id, lld_version, "info",
            "Validation: %d layers, %d features, %d continuity issue(s), %d attribute issue(s), %d drop issue(s)."
            % (
                validation["summary"]["layers"],
                validation["summary"]["total_features"],
                validation["summary"]["continuity_issues"],
                validation["summary"]["attribute_issues"],
                validation["summary"].get("drop_issues", 0),
            ),
        )

        # ── 2. Write one GeoJSON file per layer. ────────────────────────────
        task.update({"stage": "Writing layers", "progress": 60, "updated_at": _now()})
        output_dir = OUTPUT_DIR / project_id / "lld" / lld_version
        output_dir.mkdir(parents=True, exist_ok=True)

        layer_files: Dict[str, List[str]] = {}
        order = {name: i for i, name in enumerate(LLD_LAYER_ORDER)}
        for layer in sorted(by_layer.keys(), key=lambda n: (order.get(n, 999), n)):
            if layer in LLD_EXCLUDED_LAYERS:
                continue  # e.g. garden_trench — mirrored into final_trenches
            fc = {"type": "FeatureCollection", "features": by_layer[layer]}
            geojson_path = output_dir / f"{layer}.geojson"
            geojson_path.write_text(json.dumps(fc), encoding="utf-8")
            layer_files[layer] = [str(geojson_path)]

        # ── 3. Build the downloadable LLD zip (all layers, like HLD). ──────
        task.update({"stage": "Packaging", "progress": 85, "updated_at": _now()})
        zip_name = f"{project_id}_{lld_version}_lld.zip"
        zip_path = output_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for layer, files in layer_files.items():
                for fp in files:
                    zf.write(fp, arcname=f"{layer}.geojson")

        downloads = [
            {
                "name": zip_name,
                "url": f"/ftth/lld/download/{project_id}/{lld_version}",
                "size_bytes": zip_path.stat().st_size,
            }
        ]
        for layer, files in layer_files.items():
            for fp in files:
                p = Path(fp)
                downloads.append({
                    "name": f"{layer}.geojson",
                    "url": f"/ftth/lld/results/{project_id}/{lld_version}/layers/{layer}",
                    "size_bytes": p.stat().st_size,
                })

        task.update(
            {
                "status": "completed",
                "stage": "Complete",
                "progress": 100,
                "files": layer_files,
                "layers": [
                    {"name": layer, "feature_count": len(by_layer[layer]), "files": files}
                    for layer, files in layer_files.items()
                ],
                "downloads": downloads,
                "validation": validation,
                "output_dir": str(output_dir),
                "updated_at": _now(),
            }
        )
    except Exception as exc:
        task.update({"status": "failed", "error": str(exc), "updated_at": _now()})
        _lld_append(project_id, lld_version, "error", str(exc))


# ---------------------------------------------------------------------------
# LLD Phase 2 — Mode B: full re-plan (re-run the HLD oneclick pipeline with
# the Approved Survey Version as input)
#
# Mode A (the _run_lld path above) patches approved changes onto the HLD
# baseline — right for attribute fixes, nudges and reroutes, which only change
# the physical path ("the muscle"). Mode B re-derives the whole design when the
# change is structural (PDP moved, polygon/premise regrouping, UG<->aerial)
# — the logical network ("the brain") changed and must be re-planned.
#
# Mode B feeds the approved survey segments back into the shortest-route
# algorithm as BROWNFIELD input (preferred reuse, weight 0.1x) while the
# original roads + address Excel drive the routing graph — so the fresh
# design still follows shortest-path logic, but respects every approved
# survey recommendation. The old path is never part of the input, so it
# cannot reappear.
# ---------------------------------------------------------------------------

# oneclick output filename -> LLD layer name (public layer set).
_REPLAN_OUTPUT_MAP = [
    ("Objects", "objects"),
    ("Polygons", "polygons"),
    ("PDPs", "pdps"),
    ("MFG", "mfg"),
    ("Final_Trenches", "final_trenches"),
    ("Feeder_Cable", "feeder_cable"),
    ("Distribution_Cable", "distribution_cable"),
    ("Aerial_Spans", "aerial_spans"),
    ("Aerial_Drops", "aerial_drops"),
    ("Aerial_Cable", "aerial_cable"),
    ("Feeder_Ducts", "feeder_ducts"),
    ("Distribution_Ducts", "distribution_ducts"),
    ("Drop_Ducts", "drop_ducts"),
    ("Coupleurs", "coupleurs"),
    ("Chambers", "chambers"),
    ("Poles", "poles"),
    ("Trench_Nodes", "trench_nodes"),
    ("Existing_Infrastructure", "existing_infrastructure"),
    ("Existing_Infrastructure_Points", "existing_infrastructure_points"),
]


# Approved survey layer -> oneclick brownfield param (+ the GeoJSON filename
# the engine's _match_brownfield_param resolver recognises). Multiple approved
# layers can merge into one brownfield param (e.g. all duct tiers -> BF_DUCTS).
_REPLAN_BF_GROUPS = [
    ("final_trenches", "BF_TRENCHES", "bf_trenches.geojson"),
    ("feeder_ducts", "BF_DUCTS", "bf_ducts.geojson"),
    ("distribution_ducts", "BF_DUCTS", "bf_ducts.geojson"),
    ("drop_ducts", "BF_DUCTS", "bf_ducts.geojson"),
    ("feeder_cable", "BF_FIBRE", "bf_fibre.geojson"),
    ("distribution_cable", "BF_FIBRE", "bf_fibre.geojson"),
    ("pdps", "BF_EXISTING_PDP", "bf_pdps.geojson"),
    ("mfg", "BF_EXISTING_MFG", "bf_mfg.geojson"),
    ("chambers", "BF_CHAMBERS", "bf_chambers.geojson"),
]


# LLD layers the replan should NOT emit standalone (mirror LLD_EXCLUDED_LAYERS
# + layers the LLD results page never shows).
_REPLAN_EXCLUDED = {"garden_trench"}


# Survey duct_type / condition / spare_capacity -> pipeline capacity fields.
# The engineer records these in the field; without translation the pipeline
# assumes every existing duct is empty (capacity_used=0, total=default 2) and
# would route through a full or blocked duct.
_DUCT_TYPE_TOTAL = {
    "single": 1, "1-way": 1, "1way": 1,
    "twin": 2, "2-way": 2, "2way": 2,
    "quad": 4, "4-way": 4, "4way": 4,
}


def _survey_capacity(props: Dict[str, Any]) -> Tuple[int, int, str]:
    """Map survey duct attributes to (capacity_total, capacity_used, verify_status).

    - total: ``duct_type`` (single/twin/quad) or an explicit capacity attr;
      falls back to the pipeline default (2).
    - used: ``spare_capacity`` % is inverted against total; ``occupied`` or a
      blocked/collapsed condition forces the duct full (excluded from routing).
    - verify: condition/occupancy recorded on-site -> Verified, else Survey Required.
    """
    total = None
    for key in ("capacity_total", "capacity", "subducts", "sub_ducts",
                "n_subducts", "cap"):
        raw = props.get(key)
        if raw is not None:
            try:
                total = int(raw)
                break
            except (TypeError, ValueError):
                continue
    if total is None:
        d = str(props.get("duct_type") or props.get("DUCT_TYPE") or "").lower()
        total = _DUCT_TYPE_TOTAL.get(d) or 2
    total = max(1, total)

    used = 0
    occupied = props.get("occupied")
    if occupied in (True, "true", "True", "1", 1, "yes"):
        used = total
    else:
        spare = props.get("spare_capacity")
        if spare is not None:
            try:
                pct = int(spare)
                used = round(total * (100 - pct) / 100)
            except (TypeError, ValueError):
                pass

    condition = str(props.get("condition") or "").lower()
    if condition in ("blocked", "collapsed"):
        used = total  # unusable -> exclude from routing graph

    if used > total:
        used = total

    if condition in ("excellent", "good", "verified"):
        verify = "Verified"
    elif condition in ("blocked", "collapsed", "damaged"):
        verify = "Verified"  # on-site confirmed (as unusable)
    elif occupied in (True, "true", "True", "1", 1, "yes") or spare is not None:
        verify = "Verified"  # capacity/occupancy recorded on-site
    else:
        verify = "Survey Required"
    return total, used, verify


def _write_replan_brownfield(bf_dir: Path, dataset: Dict[str, Any]) -> List[str]:
    """Write approved survey segments as brownfield GeoJSON files for the
    oneclick pipeline. Returns the qgis_process ``--`` BF_* args.

    Two-tier brownfield model:
    - **USE_MODE=survey** (mandatory): the engineer's approved path is the
      field truth. The routing algorithm MUST follow it (weight=0, forced).
    - **USE_MODE=brownfield** (preferred): existing HLD infrastructure is
      available for reuse but the algorithm may choose a better path
      (weight=0.1, preferred).

    Survey duct fields are translated into the pipeline's capacity model
    (``capacity_total`` / ``capacity_used`` / ``verify_status``) so the
    engineer's field observation of spare capacity actually gates routing.
    """
    bf_dir.mkdir(parents=True, exist_ok=True)
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for feat in dataset.get("features") or []:
        props = feat.get("properties") or {}
        layer = props.get("layer") or ""
        if not props.get("approved"):
            continue
        for (src_layer, _param, filename) in _REPLAN_BF_GROUPS:
            if layer == src_layer:
                # ── Mandatory: the survey-approved path is the field truth ──
                props["USE_MODE"] = "survey"

                # Translate survey duct capacity/condition into pipeline fields.
                if filename == "bf_ducts.geojson":
                    total, used, verify = _survey_capacity(props)
                    props["capacity_total"] = total
                    props["capacity_used"] = used
                    props["verify_status"] = verify
                elif filename == "bf_trenches.geojson":
                    # Field-confirmed reuse of existing corridor -> Verified.
                    ct = str(props.get("construction_type") or "").lower()
                    if ct.startswith("existing"):
                        props["verify_status"] = "Verified"
                by_file.setdefault(filename, []).append(feat)
                break

    args: List[str] = []
    for (src_layer, param, filename) in _REPLAN_BF_GROUPS:
        feats = by_file.get(filename)
        if not feats:
            continue
        fc = {"type": "FeatureCollection", "features": feats}
        path = bf_dir / filename
        path.write_text(json.dumps(fc), encoding="utf-8")
        if f"{param}={path}" not in args:
            args.append(f"{param}={path}")
    if args:
        args.insert(0, "USE_BROWNFIELD=true")
    return args


def _drop_generated_corridor_duplicates(
    by_layer: Dict[str, List[Dict[str, Any]]],
    approved_by_layer: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, int]:
    """Remove generated corridor features that DUPLICATE an approved survey
    route. The re-plan algorithm is brownfield-aware, so it often re-creates
    a trench/duct/cable along the very corridor the engineer rerouted. That
    generated twin is a whole-feature copy of the superseded path — snapping
    its vertices (relay/purge) only produces a hybrid old+new line, so it
    must be DELETED, not moved. Only features lying ENTIRELY inside the
    approved corridor region (old path ∪ new path, within relay tolerance)
    are removed — long trunk lines that merely pass through the region are
    kept and are snapped onto the approved path by the purge pass. Returns
    counts per layer of removed features.
    """
    removed: Dict[str, int] = {}
    for layer, approved_features in approved_by_layer.items():
        regions: List[Any] = []
        approved_tiers: List[Optional[int]] = []
        for approved in approved_features:
            aprop = approved.get("properties") or {}
            approved_tiers.append(_reroute_tier(layer, aprop))
            for geom in (approved.get("geometry"), aprop.get("original_geometry")):
                for ln in _line_strings(geom):
                    if len(ln) >= 2:
                        regions.append(ln)
        if not regions:
            continue
        rindex, rcell = _build_segment_index(regions, LLD_RELAY_TOL)
        kept: List[Dict[str, Any]] = []
        cnt = 0
        for f in by_layer.get(layer, []):
            props = f.get("properties") or {}
            if props.get("approved") or props.get("lld_created"):
                kept.append(f)
                continue
            gen_tier = _reroute_tier(layer, props)
            if (
                gen_tier is not None
                and approved_tiers
                and all(_tier_blocks_reroute(t, gen_tier) for t in approved_tiers)
            ):
                kept.append(f)
                continue  # upstream tier: never a duplicate of a downstream reroute
            lines = _line_strings(f.get("geometry"))
            if not lines:
                kept.append(f)
                continue
            all_inside = True
            for ln in lines:
                if len(ln) < 2:
                    continue
                # Dense sampling: every sample point must ride the approved
                # corridor region for the feature to count as a duplicate.
                for p in _sample_polyline(ln, LLD_RELAY_TOL * 0.5):
                    if not _point_near_segments(p, rindex, rcell, LLD_RELAY_TOL):
                        all_inside = False
                        break
                if not all_inside:
                    break
            if all_inside:
                cnt += 1
            else:
                kept.append(f)
        if cnt:
            by_layer[layer] = kept
            removed[layer] = removed.get(layer, 0) + cnt
    return removed


def _run_lld_replan(
    project_id: str,
    lld_version: str,
    dataset: Dict[str, Any],
) -> None:
    """Mode B: re-run the HLD oneclick pipeline with the approved survey
    segments as brownfield, emitting a genuinely fresh design as the LLD
    output. Uses the project's original roads + address Excel so the
    shortest-route algorithm runs on the same routing graph."""
    task = _lld_task(project_id, lld_version)
    task.update({"status": "running", "stage": "Preparing re-plan inputs", "updated_at": _now()})
    try:
        # ── 1. Original HLD inputs (roads + excel) drive the routing graph ──
        inputs_dir = OUTPUT_DIR / project_id / "inputs"
        excel = inputs_dir / "Main_DataSet.xlsx"
        if not excel.exists():
            xlsx = sorted(inputs_dir.glob("*.xlsx"))
            excel = xlsx[0] if xlsx else None
        roads = inputs_dir / "berlin-roads-bundle.zip"
        if not roads.exists():
            candidates = sorted(inputs_dir.glob("roads*")) + sorted(inputs_dir.glob("*.gpkg"))
            roads = candidates[0] if candidates else None
        if not excel or not excel.exists():
            raise RuntimeError(
                "Full re-plan needs the original address Excel (inputs/Main_DataSet.xlsx) — missing on disk."
            )
        if not roads or not roads.exists():
            raise RuntimeError(
                "Full re-plan needs the original roads file (inputs/roads*) — missing on disk."
            )

        # ── 2. Brownfield: approved survey segments ONLY ───────────────────
        # The routing graph is the original roads + address Excel (network
        # base data, NOT HLD design output). The brownfield input is the
        # approved survey dataset alone — no HLD feeder/distribution/cable
        # reference layers are ever fed in. Every approved segment is forced
        # (USE_MODE=survey, weight 0) so the router MUST follow survey paths.
        task.update({"stage": "Writing approved survey as brownfield", "updated_at": _now()})
        replan_root = OUTPUT_DIR / project_id / "replan" / lld_version
        bf_args = _write_replan_brownfield(replan_root / "brownfield", dataset)
        if bf_args:
            _lld_append(project_id, lld_version, "info",
                        "Mode B re-plan: brownfield = approved survey segments only (%s); no HLD design layers fed."
                        % ", ".join(bf_args))
        else:
            _lld_append(project_id, lld_version, "warn",
                        "Mode B re-plan: no approved survey segments found to feed as brownfield.")

        # ── 3. Run the oneclick pipeline (shortest-route algo, survey-aware) ──
        task.update({"stage": "Running full design pipeline", "updated_at": _now()})
        design_dir = replan_root / "design"
        design_dir.mkdir(parents=True, exist_ok=True)
        qgis = _find_qgis_process()
        if not qgis:
            raise RuntimeError("qgis_process not found — full re-plan requires QGIS.")
        cmd = [
            qgis, "run", "hldplanning:end_to_end_pipeline", "--",
            f"EXCEL={excel}",
            f"ROADS={roads}",
            f"OUTPUT_DIR={design_dir}",
            "POLY_METHOD=3",
        ]
        cmd.extend(bf_args)
        if os.name == "nt" and qgis.lower().endswith((".bat", ".cmd")):
            cmd = " ".join(_quote_cmd_arg(part) for part in cmd)
        _run_command(project_id, cmd, design_dir)

        # ── 4. Map fresh outputs to LLD layers + build the by_layer set ──
        task.update({"stage": "Ingesting fresh design", "progress": 80, "updated_at": _now()})
        by_layer: Dict[str, List[Dict[str, Any]]] = {}
        for oneclick_name, lld_layer in _REPLAN_OUTPUT_MAP:
            if lld_layer in _REPLAN_EXCLUDED:
                continue
            gpkg = design_dir / f"{oneclick_name}.gpkg"
            geojson = design_dir / f"{oneclick_name}.geojson"
            _ensure_geojson(gpkg, geojson)
            if not geojson.exists():
                _lld_append(project_id, lld_version, "warn",
                            f"Mode B: fresh design has no {oneclick_name} layer — skipping.")
                continue
            try:
                fc = json.loads(geojson.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            feats = fc.get("features") or []
            for f in feats:
                props = dict(f.get("properties") or {})
                props["layer"] = lld_layer
                props.setdefault("feature_id", "LLD-%s-%s" % (lld_layer, uuid.uuid4().hex[:10]))
                f["properties"] = props
            by_layer.setdefault(lld_layer, []).extend(feats)
            _lld_append(project_id, lld_version, "info",
                        f"Mode B: fresh {lld_layer} layer — {len(feats)} features.")

        if not by_layer:
            raise RuntimeError("Mode B pipeline produced no layers.")

        # ── 5. Enforce approved survey routes as authoritative ────────────
        # The re-plan's generated layers are only a routing scaffold. For each
        # approved survey corridor feature, replace the generated feature's
        # geometry in the affected region with the engineer's geometry. This
        # deliberately does not require sidewalk/road alignment: field truth
        # wins. Then relay dependent layers and purge the superseded footprint.
        task.update({"stage": "Enforcing approved survey routes", "progress": 84, "updated_at": _now()})
        approved_by_layer: Dict[str, List[Dict[str, Any]]] = {}
        for feat in dataset.get("features") or []:
            props = feat.get("properties") or {}
            layer = props.get("layer")
            geom = feat.get("geometry")
            if props.get("approved") and layer in CORRIDOR_LINE_LAYERS and _line_strings(geom):
                approved_by_layer.setdefault(layer, []).append(feat)

        # Remove generated twins that fully duplicate an approved corridor
        # BEFORE overlaying the authoritative route, so the engineer's path
        # is the ONLY line in that region (old path never coexists with the
        # new path). Long trunk lines passing through are kept for the purge.
        dropped = _drop_generated_corridor_duplicates(by_layer, approved_by_layer)
        dropped_summary = ", ".join("%s=%d" % (k, v) for k, v in dropped.items() if v) or "none"

        authoritative = 0
        for layer, approved_features in approved_by_layer.items():
            for approved in approved_features:
                aprop = approved.get("properties") or {}
                target_id = aprop.get("feature_id")
                target = next(
                    (f for f in by_layer.get(layer, [])
                     if (f.get("properties") or {}).get("feature_id") == target_id),
                    None,
                )
                if target is None:
                    # The fresh algorithm assigns new ids, so the approved
                    # route is appended as the authoritative feature. Inherit
                    # the pipeline's design attributes from the nearest
                    # generated feature riding the corridor so the survey
                    # overlay never blanks trench_type / lengths / INFRA_STATUS.
                    base_props = _nearest_same_layer_props(by_layer.get(layer, []), approved)
                    props = dict(base_props)
                    props.update(aprop)
                    target = {
                        "type": "Feature",
                        "geometry": approved.get("geometry"),
                        "properties": props,
                    }
                    by_layer.setdefault(layer, []).append(target)
                else:
                    target["geometry"] = approved.get("geometry")
                    target["properties"] = {**(target.get("properties") or {}), **aprop}
                target.setdefault("properties", {})["survey_authoritative"] = True
                authoritative += 1

        relayed = _relay_dependents(by_layer)
        purged = _purge_old_region(by_layer)
        relay_summary = ", ".join("%s=%d" % (k, v) for k, v in relayed.items() if v) or "none"
        purge_summary = ", ".join("%s=%d" % (k, v) for k, v in purged.items() if v) or "none"

        # final_trenches is the combination of feeder + distribution (+ garden):
        # any final feature missing the component attributes inherits them from
        # the nearest sub-layer so the final trench always carries trench_type
        # / USAGE_TYPE / SURFACE ... like the three component layers.
        inherited = _inherit_trench_sub_layer_attributes(by_layer)
        if inherited:
            _lld_append(
                project_id, lld_version, "info",
                "Final-trench attributes: %d feature(s) inherited trench_type/design attrs from feeder/distribution sub-layers."
                % inherited,
            )

        _lld_append(
            project_id, lld_version, "info",
            "Survey route enforcement: %d approved route(s) authoritative; "
            "field geometry retained regardless of sidewalk alignment; "
            "generated_duplicates_removed=%s; relay=%s; old_path_purge=%s."
            % (authoritative, dropped_summary, relay_summary, purge_summary),
        )

        # ── 6. Apply the same final cable/duct contract as Verify mode ────
        # Full re-plan has fresh HLD output, but it must still pass through
        # the shared grouped-cable normalization before LLD files are written.
        from lld_cable_geometry import regroup_distribution_cables
        regroup_distribution_cables(by_layer.get("distribution_cable") or [])
        for duct_feature in by_layer.get("feeder_ducts") or []:
            duct_props = duct_feature.setdefault("properties", {})
            pdp_ids = [v.strip() for v in str(duct_props.get("pdp_ids") or "").split(",") if v.strip()]
            duct_props["capacity_total"] = LLD_MAX_PDPS_PER_DUCT
            duct_props["capacity_used"] = len(pdp_ids)
            duct_props["capacity_spare"] = max(0, LLD_MAX_PDPS_PER_DUCT - len(pdp_ids))
            duct_props["MAX_PDPS_PER_DUCT"] = LLD_MAX_PDPS_PER_DUCT

        # ── 6. Validate + write layers + zip (same contract as _run_lld) ──
        normalized = _normalize_trench_construction_class(by_layer)
        if normalized:
            _lld_append(
                project_id, lld_version, "info",
                "Trench classification: %d feature(s) normalised to Open Cut / HDD / Garden "
                "(baseline tier kept in TRENCH_TIER)." % normalized,
            )

        task.update({"stage": "Validating fresh design", "progress": 90, "updated_at": _now()})
        validation = _validate_network(by_layer)
        _lld_append(
            project_id, lld_version, "info",
            "Validation: %d layers, %d features, %d continuity issue(s), %d attribute issue(s), %d drop issue(s)."
            % (
                validation["summary"]["layers"],
                validation["summary"]["total_features"],
                validation["summary"]["continuity_issues"],
                validation["summary"]["attribute_issues"],
                validation["summary"].get("drop_issues", 0),
            ),
        )

        output_dir = OUTPUT_DIR / project_id / "lld" / lld_version
        output_dir.mkdir(parents=True, exist_ok=True)
        layer_files: Dict[str, List[str]] = {}
        order = {name: i for i, name in enumerate(LLD_LAYER_ORDER)}
        for layer in sorted(by_layer.keys(), key=lambda n: (order.get(n, 999), n)):
            fc = {"type": "FeatureCollection", "features": by_layer[layer]}
            geojson_path = output_dir / f"{layer}.geojson"
            geojson_path.write_text(json.dumps(fc), encoding="utf-8")
            layer_files[layer] = [str(geojson_path)]

        zip_name = f"{project_id}_{lld_version}_lld.zip"
        zip_path = output_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for layer, files in layer_files.items():
                for fp in files:
                    zf.write(fp, arcname=f"{layer}.geojson")

        downloads = [
            {
                "name": zip_name,
                "url": f"/ftth/lld/download/{project_id}/{lld_version}",
                "size_bytes": zip_path.stat().st_size,
            }
        ]
        for layer, files in layer_files.items():
            for fp in files:
                p = Path(fp)
                downloads.append({
                    "name": f"{layer}.geojson",
                    "url": f"/ftth/lld/results/{project_id}/{lld_version}/layers/{layer}",
                    "size_bytes": p.stat().st_size,
                })

        task.update(
            {
                "status": "completed",
                "stage": "Complete",
                "progress": 100,
                "files": layer_files,
                "layers": [
                    {"name": layer, "feature_count": len(by_layer[layer]), "files": files}
                    for layer, files in layer_files.items()
                ],
                "downloads": downloads,
                "validation": validation,
                "output_dir": str(output_dir),
                "mode": "replan",
                "updated_at": _now(),
            }
        )
    except Exception as exc:
        task.update({"status": "failed", "error": str(exc), "updated_at": _now()})
        _lld_append(project_id, lld_version, "error", str(exc))


@app.post("/ftth/lld/replan", status_code=202)
async def run_lld_replan(
    background_tasks: BackgroundTasks,
    request: Request,
) -> Dict[str, Any]:
    """Start a Mode B LLD run: re-run the HLD oneclick pipeline with the
    approved survey dataset as brownfield input, producing a genuinely fresh
    design. Same request shape as /ftth/lld/run.

    Body (JSON):
        {
          "project_id": "<hld-project-id>",
          "lld_version": "LLD-V01",
          "dataset": {"type": "FeatureCollection", "features": [...]}
        }
    """
    body = None
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    project_id = (body or {}).get("project_id") or ""
    lld_version = (body or {}).get("lld_version") or ""
    dataset = (body or {}).get("dataset") or {}

    if not project_id or not lld_version:
        raise HTTPException(status_code=400, detail="project_id and lld_version are required")
    if not isinstance(dataset.get("features"), list):
        raise HTTPException(status_code=400, detail="dataset.features must be a list")

    task = _lld_task(project_id, lld_version)
    task.update({
        "status": "queued",
        "stage": "Queued (Mode B re-plan)",
        "progress": 0,
        "mode": "replan",
        "updated_at": _now(),
    })
    background_tasks.add_task(_run_lld_replan, project_id, lld_version, dataset)
    return _lld_public_task(project_id, lld_version)


@app.post("/ftth/lld/run", status_code=202)
async def run_lld(
    background_tasks: BackgroundTasks,
    request: Request,
) -> Dict[str, Any]:
    """Start an LLD run: apply approved survey changes to the HLD output,
    validate the network, and emit final LLD layers + a zip.

    Body (JSON):
        {
          "project_id": "<hld-project-id>",
          "lld_version": "LLD-V01",
          "dataset": {"type": "FeatureCollection", "features": [...]}
        }
    """
    body = None
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    project_id = (body or {}).get("project_id") or ""
    lld_version = (body or {}).get("lld_version") or ""
    dataset = (body or {}).get("dataset") or {}

    if not project_id or not lld_version:
        raise HTTPException(status_code=400, detail="project_id and lld_version are required")
    if not isinstance(dataset.get("features"), list):
        raise HTTPException(status_code=400, detail="dataset.features must be a list")

    task = _lld_task(project_id, lld_version)
    task.update({
        "status": "queued",
        "stage": "Queued",
        "progress": 0,
        "updated_at": _now(),
    })
    background_tasks.add_task(_run_lld, project_id, lld_version, dataset)
    return _lld_public_task(project_id, lld_version)


@app.get("/ftth/lld/results/{project_id}/{lld_version}")
def get_lld_results(project_id: str, lld_version: str) -> Dict[str, Any]:
    if _lld_key(project_id, lld_version) not in lld_tasks:
        raise HTTPException(status_code=404, detail="LLD run not found")
    return _lld_public_task(project_id, lld_version)


@app.get("/ftth/lld/results/{project_id}/{lld_version}/layers/{layer}")
def get_lld_layer(project_id: str, lld_version: str, layer: str) -> Dict[str, Any]:
    if _lld_key(project_id, lld_version) not in lld_tasks:
        raise HTTPException(status_code=404, detail="LLD run not found")
    task = _lld_task(project_id, lld_version)
    files = (task.get("files") or {}).get(layer) or []
    for fp in files:
        if fp.lower().endswith((".geojson", ".json")) and os.path.isfile(fp):
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
    raise HTTPException(status_code=404, detail="Layer not found")


@app.get("/ftth/lld/download/{project_id}/{lld_version}")
def download_lld(project_id: str, lld_version: str) -> FileResponse:
    # The in-memory task registry is lost on engine restart, but the output
    # zip persists on disk — fall back to the on-disk file so downloads keep
    # working for runs completed in a previous engine process.
    task = lld_tasks.get(_lld_key(project_id, lld_version))
    if task and task.get("output_dir"):
        output_dir = Path(task["output_dir"])
    else:
        output_dir = OUTPUT_DIR / project_id / "lld" / lld_version
    zip_name = f"{project_id}_{lld_version}_lld.zip"
    candidate = output_dir / zip_name
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="LLD zip not found")
    return FileResponse(str(candidate), filename=candidate.name)



# Compatibility aliases
@app.post("/run-hld", status_code=202)
async def run_hld_compat(
    background_tasks: BackgroundTasks,
    excel: UploadFile = File(...),
    roads: UploadFile = File(...),
    project_id: Optional[str] = Form(None),
) -> Dict[str, Any]:
    # NOTE: brownfield (4th positional) is intentionally None here — pass
    # project_id by keyword so it does not land in the brownfield slot.
    return await run_hld(background_tasks, excel, roads, None, project_id)


@app.get("/status/{project_id}")
def status_compat(project_id: str) -> Dict[str, Any]:
    return get_results(project_id)


@app.get("/layers/{project_id}/{layer}")
def layer_compat(project_id: str, layer: str) -> Dict[str, Any]:
    return get_layer(project_id, layer)
