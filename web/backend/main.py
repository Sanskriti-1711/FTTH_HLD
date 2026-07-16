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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple, Union

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
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
    ("feeder_cable", "Feeder_Cable.gpkg", "Feeder_Cable.geojson"),
    ("distribution_cable", "Distribution_Cable.gpkg", "Distribution_Cable.geojson"),
    ("feeder_ducts", "Feeder_Ducts.gpkg", "Feeder_Ducts.geojson"),
    ("distribution_ducts", "Distribution_Ducts.gpkg", "Distribution_Ducts.geojson"),
    ("reports", "BOQ.xlsx", "BOQ.xlsx"),
    ("reports", "BOM.xlsx", "BOM.xlsx"),
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
    timeout = int(os.environ.get("QGIS_PROCESS_TIMEOUT", "10800"))
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            process.kill()
        try:
            stdout, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout = ""
        for line in (stdout or "").splitlines()[-80:]:
            _append(project_id, "info", line)
        raise RuntimeError(f"qgis_process timed out after {timeout} seconds") from exc

    for line in (stdout or "").splitlines():
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

    rc = process.returncode
    if rc != 0:
        raise RuntimeError(f"qgis_process exited with code {rc}")
    _append(project_id, "info", f"qgis_process finished; outputs in {output_dir}")


def _convert_gpkg_to_geojson(gpkg_path: Path, geojson_path: Path) -> bool:
    if not gpkg_path.exists():
        return False
    if geojson_path.exists():
        geojson_path.unlink()
    ogr2ogr = shutil.which("ogr2ogr")
    if not ogr2ogr:
        return False
    result = subprocess.run(
        [ogr2ogr, "-f", "GeoJSON", str(geojson_path), str(gpkg_path)],
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


def _run_pipeline(project_id: str, excel_path: Path, roads_path: Path, output_dir: Path) -> None:
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
        ]
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
    project_id: Optional[str] = Form(None),
) -> Dict[str, Any]:
    project_id = project_id or uuid.uuid4().hex
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    excel_path = _save_upload(excel, upload_dir, "addresses.xlsx")
    roads_path = _save_upload(roads, upload_dir, "roads.gpkg")

    task = _task(project_id)
    task.update(
        {
            "status": "queued",
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

    background_tasks.add_task(_run_pipeline, project_id, excel_path, roads_path, output_dir)
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
        raise HTTPException(status_code=404, detail="Project not found")
    return _public_task(project_id)


@app.get("/ftth/hld/results/{project_id}/layers/{layer}")
def get_layer(project_id: str, layer: str) -> Dict[str, Any]:
    if postgis.is_available():
        try:
            data = postgis.get_layer_geojson(project_id, layer)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if data is not None:
            return data

    task = tasks.get(project_id)
    if task:
        for file_path in (task.get("files") or {}).get(layer, []):
            if file_path.lower().endswith((".geojson", ".json")) and os.path.isfile(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
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


# Compatibility aliases
@app.post("/run-hld", status_code=202)
async def run_hld_compat(
    background_tasks: BackgroundTasks,
    excel: UploadFile = File(...),
    roads: UploadFile = File(...),
    project_id: Optional[str] = Form(None),
) -> Dict[str, Any]:
    return await run_hld(background_tasks, excel, roads, project_id)


@app.get("/status/{project_id}")
def status_compat(project_id: str) -> Dict[str, Any]:
    return get_results(project_id)


@app.get("/layers/{project_id}/{layer}")
def layer_compat(project_id: str, layer: str) -> Dict[str, Any]:
    return get_layer(project_id, layer)
