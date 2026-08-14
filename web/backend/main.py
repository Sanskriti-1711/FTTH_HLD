"""FTTH Engine API.

Runs the HLDPlanning QGIS plugin through oneclick.py/qgis_process, stores the
canonical outputs in PostGIS, and exposes GeoJSON, downloads, and optional MVT
tiles for MapLibre or any other client.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple, Union

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

import postgis


APP_STARTED_AT = datetime.now(timezone.utc)
ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BACKEND_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_MESSAGES = 500
tasks: Dict[str, Dict[str, Any]] = {}

PIPELINE_STAGES = [
    "Object Layer",
    "Polygon Layer",
    "Network Layer",
    "Trench Layer",
    "Cable Layer",
    "Duct Layer",
]

ONECLICK_OUTPUTS: List[Tuple[str, str, str]] = [
    ("objects", "Objects.gpkg", "Objects.geojson"),
    ("polygons", "Polygons.gpkg", "Polygons.geojson"),
    ("pdps", "PDPs.gpkg", "PDPs.geojson"),
    ("mfg", "MFG.gpkg", "MFG.geojson"),
    ("trenches", "Feeder_Trench.gpkg", "Feeder_Trench.geojson"),
    ("trenches", "Distribution_Trench.gpkg", "Distribution_Trench.geojson"),
    ("trenches", "Garden_Trench.gpkg", "Garden_Trench.geojson"),
    ("trenches", "Drill_Trench.gpkg", "Drill_Trench.geojson"),
    ("trenches", "Final_Trenches.gpkg", "Final_Trenches.geojson"),
    ("cables", "Feeder_Cable.gpkg", "Feeder_Cable.geojson"),
    ("cables", "Distribution_Cable.gpkg", "Distribution_Cable.geojson"),
    ("ducts", "Feeder_Ducts.gpkg", "Feeder_Ducts.geojson"),
    ("ducts", "Distribution_Ducts.gpkg", "Distribution_Ducts.geojson"),
    ("ducts", "Drop_Ducts.gpkg", "Drop_Ducts.geojson"),
    ("chambers", "Chambers.gpkg", "Chambers.geojson"),
    ("poles", "Poles.gpkg", "Poles.geojson"),
    ("brownfield", "Existing_Infrastructure.gpkg", "Existing_Infrastructure.geojson"),
    ("brownfield", "Existing_Infrastructure_Points.gpkg", "Existing_Infrastructure_Points.geojson"),
    # NOTE: BOQ.xlsx / BOM.xlsx are intentionally NOT listed here as layers —
    # they surface in the Downloads section via _register_downloads() instead.
]

DOWNLOAD_EXTS = {".gpkg", ".xlsx", ".csv", ".json", ".geojson", ".txt"}

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
    for name in ("qgis_process-qgis", "qgis_process"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for base in (r"C:\Program Files", r"C:\OSGeo4W64\bin"):
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for filename in files:
                    lower = filename.lower()
                    if lower.startswith("qgis_process") and lower.endswith((".bat", ".cmd", ".exe")):
                        return os.path.join(root, filename)
    return None


def _quote_cmd_arg(arg: str) -> str:
    if not any(ch.isspace() for ch in arg) and not any(ch in arg for ch in ['"', "&", "(", ")", "^"]):
        return arg
    return '"' + arg.replace('"', r'\"') + '"'


def _run_command(project_id: str, cmd: Union[List[str], str], output_dir: Path) -> None:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("QGIS_PLUGINPATH", str(ROOT_DIR))
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
                task["stage"] = stage
                task["stage_index"] = idx
                task["progress"] = int((idx / len(PIPELINE_STAGES)) * 100)

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

    layer_files: Dict[str, List[str]] = {}
    for public_layer, gpkg_name, geojson_name in ONECLICK_OUTPUTS:
        if gpkg_name.lower().endswith(".xlsx"):
            path = output_dir / gpkg_name
            if path.is_file():
                layer_files.setdefault(public_layer, []).append(str(path))
            continue
        geojson_path = output_dir / geojson_name
        gpkg_path = output_dir / gpkg_name
        if not geojson_path.exists() and gpkg_path.exists():
            _convert_gpkg_to_geojson(gpkg_path, geojson_path)
        if geojson_path.exists():
            layer_files.setdefault(public_layer, []).append(str(geojson_path))
        elif gpkg_path.exists():
            layer_files.setdefault(public_layer, []).append(str(gpkg_path))

    if not layer_files:
        return None

    task = _task(project_id)
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


def _ingest_outputs(project_id: str, output_dir: Path) -> List[Dict[str, Any]]:
    has_postgis = postgis.is_available()
    if has_postgis:
        postgis.init_schema()
        postgis.clear_project_layers(project_id)
    layer_files: Dict[str, List[str]] = {}

    for public_layer, gpkg_name, geojson_name in ONECLICK_OUTPUTS:
        gpkg_path = output_dir / gpkg_name
        geojson_path = output_dir / geojson_name
        # Handle report files (.xlsx) that aren't vector layers
        if gpkg_name.lower().endswith(".xlsx"):
            if gpkg_path.exists():
                layer_files.setdefault(public_layer, []).append(str(gpkg_path))
            continue
        if not geojson_path.exists():
            _convert_gpkg_to_geojson(gpkg_path, geojson_path)
        if geojson_path.exists():
            layer_files.setdefault(public_layer, []).append(str(geojson_path))
            if has_postgis:
                inserted = postgis.load_geojson_file(
                    project_id,
                    public_layer,
                    str(geojson_path),
                    replace=False,
                )
                _append(project_id, "info", f"Loaded {inserted} features into {public_layer}.")
        elif gpkg_path.exists():
            layer_files.setdefault(public_layer, []).append(str(gpkg_path))

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


def _run_pipeline(
    project_id: str,
    excel_path: Path,
    roads_path: Path,
    output_dir: Path,
    poly_method: int = 3,
    brownfield_path: Optional[Path] = None,
) -> None:
    task = _task(project_id)
    task.update({"status": "running", "stage": PIPELINE_STAGES[0], "updated_at": _now()})
    if postgis.is_available():
        postgis.init_schema()
        postgis.upsert_project(
            project_id,
            status="running",
            roads_filename=roads_path.name,
            output_dir=str(output_dir),
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
        if os.name == "nt" and qgis.lower().endswith((".bat", ".cmd")):
            cmd = " ".join(_quote_cmd_arg(part) for part in cmd)

        _run_command(project_id, cmd, output_dir)
        layers = _ingest_outputs(project_id, output_dir)
        downloads = _register_downloads(project_id, output_dir)

        task.update(
            {
                "status": "completed",
                "stage": "Complete",
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
        "endpoints": [
            "POST /ftth/hld/run",
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

    task = _task(project_id)
    task.update(
        {
            "status": "queued",
            "project_name": name or "",
            "poly_method": poly_method,
            "roads_filename": roads_path.name,
            "output_dir": str(output_dir),
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
    if task is None:
        task = _restore_task_from_disk(project_id)
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
    if postgis.is_available():
        postgis.clear_project_layers(project_id)
        postgis.delete_project(project_id)
    return {
        "deleted": True,
        "project_id": project_id,
        "had_in_memory_task": removed_task is not None,
    }


@app.get("/ftth/projects")
def projects(limit: int = 50) -> List[Dict[str, Any]]:
    if postgis.is_available():
        return postgis.list_projects(limit=limit)
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
    "final_trenches", "feeder_trench", "distribution_trench",
    "garden_trench", "drill_trench",
    "feeder_cable", "distribution_cable",
    "feeder_ducts", "distribution_ducts", "drop_ducts",
    "chambers", "poles",
    "existing_infrastructure", "existing_infrastructure_points",
    "brownfield",
]

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

# Coverage / creation tolerance in degrees (~50 m). Must stay consistent with
# the network-connectivity tolerance used by _validate_network().
LLD_COVERAGE_TOL = 0.0005


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

    return {
        "issues": issues,
        "summary": {
            "layers": len(by_layer),
            "total_features": sum(len(v) for v in by_layer.values()),
            "continuity_issues": sum(1 for i in issues if i["type"] == "continuity"),
            "attribute_issues": attr_issues,
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
        # ── 1. Group the approved dataset (HLD + approved survey changes) by
        #       its `layer` property, preserving the HLD layer structure. ────
        by_layer: Dict[str, List[Dict[str, Any]]] = {}
        for feat in dataset.get("features") or []:
            props = feat.get("properties") or {}
            layer = props.get("layer") or "unknown"
            by_layer.setdefault(layer, []).append(feat)

        if not by_layer:
            raise RuntimeError("Approved dataset contains no features")

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

        task.update({"stage": "Validating network", "progress": 30, "updated_at": _now()})
        validation = _validate_network(by_layer)
        _lld_append(
            project_id, lld_version, "info",
            "Validation: %d layers, %d features, %d continuity issue(s), %d attribute issue(s)."
            % (
                validation["summary"]["layers"],
                validation["summary"]["total_features"],
                validation["summary"]["continuity_issues"],
                validation["summary"]["attribute_issues"],
            ),
        )

        # ── 2. Write one GeoJSON file per layer. ────────────────────────────
        task.update({"stage": "Writing layers", "progress": 60, "updated_at": _now()})
        output_dir = OUTPUT_DIR / project_id / "lld" / lld_version
        output_dir.mkdir(parents=True, exist_ok=True)

        layer_files: Dict[str, List[str]] = {}
        order = {name: i for i, name in enumerate(LLD_LAYER_ORDER)}
        for layer in sorted(by_layer.keys(), key=lambda n: (order.get(n, 999), n)):
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
    if _lld_key(project_id, lld_version) not in lld_tasks:
        raise HTTPException(status_code=404, detail="LLD run not found")
    task = _lld_task(project_id, lld_version)
    output_dir = Path(task.get("output_dir")) if task.get("output_dir") else OUTPUT_DIR / project_id / "lld" / lld_version
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
