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
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple, Union

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

# Individual pipeline steps — maps step name → (alg_id, user-facing label)
PIPELINE_STEPS: Dict[str, Tuple[str, str]] = {
    "object":  ("hldplanning:01_object_layer",  "Object Layer"),
    "polygon": ("hldplanning:02_polygon_layer", "Polygon Layer"),
    "network": ("hldplanning:03_network_layer", "Network Layer"),
    "trench":  ("hldplanning:04_trench_layer",  "Trench Layer"),
    "cable":   ("hldplanning:06_cable_layer",   "Cable Layer"),
    "duct":    ("hldplanning:05_duct_layer",    "Duct Layer"),
}

# Which ONECLICK_OUTPUTS entries belong to each pipeline step
STEP_LAYER_MAP: Dict[str, List[str]] = {
    "object":  ["objects"],
    "polygon": ["polygons"],
    "network": ["pdps", "mfg"],
    "trench":  ["trenches"],
    "cable":   ["feeder_cable", "distribution_cable"],
    "duct":    ["feeder_ducts", "distribution_ducts"],
}

# Step dependency chain (which step must be completed before this one)
STEP_DEPENDENCIES: Dict[str, Optional[str]] = {
    "object":  None,
    "polygon": "object",
    "network": "polygon",
    "trench":  "network",
    "cable":   "trench",
    "duct":    "trench",
}

ONECLICK_OUTPUTS: List[Tuple[str, str, str]] = [
    ("objects", "Objects.gpkg", "Objects.geojson"),
    ("polygons", "Polygons.gpkg", "Polygons.geojson"),
    # Network layer outputs — each a separate public_layer for the frontend
    ("pdps", "PDPs.gpkg", "PDPs.geojson"),
    ("mfg", "MFG.gpkg", "MFG.geojson"),
    # All trench types merged under "trenches" (single frontend toggle)
    ("trenches", "Feeder_Trench.gpkg", "Feeder_Trench.geojson"),
    ("trenches", "Distribution_Trench.gpkg", "Distribution_Trench.geojson"),
    ("trenches", "Garden_Trench.gpkg", "Garden_Trench.geojson"),
    ("trenches", "Drill_Trench.gpkg", "Drill_Trench.geojson"),
    ("trenches", "Final_Trenches.gpkg", "Final_Trenches.geojson"),
    # Cable layers — individual names for frontend color mapping
    ("feeder_cable", "Feeder_Cable.gpkg", "Feeder_Cable.geojson"),
    ("distribution_cable", "Distribution_Cable.gpkg", "Distribution_Cable.geojson"),
    # Duct layers — individual names for frontend color mapping
    ("feeder_ducts", "Feeder_Ducts.gpkg", "Feeder_Ducts.geojson"),
    ("distribution_ducts", "Distribution_Ducts.gpkg", "Distribution_Ducts.geojson"),
    # Reports (non-vector, appear in downloads only)
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


# ---------------------------------------------------------------------------
# Pipeline state helpers (server-side progress persistence)
# ---------------------------------------------------------------------------


def _init_pipeline_state(
    project_id: str,
    excel_filename: Optional[str] = None,
    roads_filename: Optional[str] = None,
) -> None:
    """Initialise the pipeline_state JSONB in PostGIS for a fresh project."""
    if postgis.is_available():
        postgis.init_pipeline_state(
            project_id, excel_filename=excel_filename, roads_filename=roads_filename
        )


def _update_step_progress(
    project_id: str,
    step: str,
    status: str,
    *,
    progress: Optional[int] = None,
    error: Optional[str] = None,
    outputs: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """Update a single step's state in PostGIS pipeline_state."""
    if postgis.is_available():
        postgis.update_step_progress(
            project_id, step, status,
            progress=progress, error=error, outputs=outputs, params=params,
        )
    # Also update in-memory task for polling
    if error:
        _append(project_id, "error", f"[{step}] {error}")
    elif status == "completed":
        _append(project_id, "success", f"[{step}] Completed")
    elif status == "running":
        _append(project_id, "info", f"[{step}] Running...")


def _step_output_name(step: str) -> List[str]:
    """Return the list of output GPKG filenames produced by a step."""
    layer_map = {
        "object":  ["Objects.gpkg"],
        "polygon": ["Polygons.gpkg"],
        "network": ["PDPs.gpkg", "MFG.gpkg", "Objects.gpkg"],  # network re-writes Objects.gpkg with PDP IDs
        "trench":  ["Feeder_Trench.gpkg", "Distribution_Trench.gpkg",
                     "Garden_Trench.gpkg", "Drill_Trench.gpkg", "Final_Trenches.gpkg"],
        "cable":   ["Feeder_Cable.gpkg", "Distribution_Cable.gpkg"],
        "duct":    ["Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg"],
    }
    return layer_map.get(step, [])


def _check_dependency(project_id: str, step: str) -> None:
    """Check that the dependency step has been completed before this step."""
    dep = STEP_DEPENDENCIES.get(step)
    if dep is None:
        return
    if not postgis.is_available():
        return  # Can't verify without PostGIS — let it proceed
    state = postgis.get_pipeline_state(project_id)
    if state is None:
        raise HTTPException(
            status_code=400,
            detail=f"Project has no pipeline state. Run '{dep}' step first.",
        )
    dep_status = (state.get("steps") or {}).get(dep, {}).get("status")
    if dep_status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot run '{step}' — dependency '{dep}' has status '{dep_status}'. Complete '{dep}' first.",
        )


# ---------------------------------------------------------------------------
# Step-level qgis_process runners
# ---------------------------------------------------------------------------


def _build_qgis_step_cmd(
    qgis: str,
    step: str,
    output_dir: Path,
    step_params: Dict[str, str],
) -> Union[List[str], str]:
    """Build the qgis_process run command for an individual pipeline step."""
    alg_id = PIPELINE_STEPS[step][0]
    cmd_parts = [qgis, "run", alg_id, "--"]
    for key, value in step_params.items():
        cmd_parts.append(f"{key}={value}")
    # When QGIS executable is a Windows batch script, convert to a single string
    if os.name == "nt" and qgis.lower().endswith((".bat", ".cmd")):
        return " ".join(_quote_cmd_arg(p) for p in cmd_parts)
    return cmd_parts


def _run_step(
    project_id: str,
    step: str,
    output_dir: Path,
    step_params: Dict[str, str],
) -> None:
    """Run a single pipeline step via qgis_process and update pipeline_state.

    Args:
        project_id: The project UUID.
        step: Step name (object, polygon, network, trench, cable, duct).
        output_dir: Directory storing inputs and receiving outputs.
        step_params: KEY=VALUE parameters for the qgis_process algorithm.
    """
    # Validate step
    if step not in PIPELINE_STEPS:
        raise HTTPException(status_code=400, detail=f"Unknown step '{step}'.")

    _update_step_progress(project_id, step, "running", progress=0)

    qgis = _find_qgis_process()
    if not qgis:
        raise RuntimeError(
            "qgis_process was not found. Set QGIS_EXECUTABLE or add QGIS bin to PATH."
        )

    cmd = _build_qgis_step_cmd(qgis, step, output_dir, step_params)
    try:
        _run_command(project_id, cmd, output_dir)
    except Exception as exc:
        _update_step_progress(project_id, step, "failed", error=str(exc))
        raise

    # Collect outputs — find which GPKGs were produced
    outputs: Dict[str, str] = {}
    for fname in _step_output_name(step):
        gpkg = output_dir / fname
        if gpkg.exists():
            outputs[fname] = str(gpkg)

    _update_step_progress(
        project_id, step, "completed",
        progress=100,
        outputs=outputs if outputs else None,
    )


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
        # Initialize pipeline_state and mark all steps as running (full pipeline)
        _init_pipeline_state(project_id, excel_filename=excel_path.name, roads_filename=roads_path.name)

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

        # Mark all steps as completed in pipeline_state
        for step_key in ("object", "polygon", "network", "trench", "cable", "duct"):
            outputs = {}
            for fname in _step_output_name(step_key):
                gpkg = output_dir / fname
                if gpkg.exists():
                    outputs[fname] = str(gpkg)
            _update_step_progress(
                project_id, step_key, "completed",
                progress=100,
                outputs=outputs if outputs else None,
            )

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


@app.get("/ftth/projects")
def projects(limit: int = 50) -> List[Dict[str, Any]]:
    if postgis.is_available():
        return postgis.list_projects(limit=limit)
    return [
        _public_task(project_id)
        for project_id in sorted(tasks, key=lambda pid: tasks[pid].get("created_at", ""), reverse=True)
    ][:limit]


# ---------------------------------------------------------------------------
# Individual step endpoints & progress/resume API
# ---------------------------------------------------------------------------


@app.get("/ftth/hld/progress/{project_id}")
def get_progress(project_id: str) -> Dict[str, Any]:
    """
    Get the current pipeline_state for a project — shows which steps are
    completed, pending, or failed, along with output files and parameters.
    Useful for resuming a partially-complete pipeline.
    """
    if postgis.is_available():
        state = postgis.get_pipeline_state(project_id)
        if state is not None:
            project = postgis.get_project(project_id)
            return {
                "project_id": project_id,
                "status": (project or {}).get("status", "unknown"),
                "pipeline_state": state,
            }
    # Fallback to in-memory task
    task = tasks.get(project_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found")
    # Build a basic state from the in-memory task
    steps = {}
    for step_key in ("object", "polygon", "network", "trench", "cable", "duct"):
        step_status = "pending"
        if task.get("status") == "completed":
            step_status = "completed"
        elif task.get("status") == "failed":
            step_status = "failed"
        steps[step_key] = {
            "status": step_status,
            "outputs": {},
            "params": {},
            "error": task.get("error") if step_status == "failed" else None,
            "started_at": task.get("created_at"),
            "completed_at": task.get("updated_at") if step_status in ("completed", "failed") else None,
        }
    return {
        "project_id": project_id,
        "status": task.get("status", "unknown"),
        "pipeline_state": {
            "steps": steps,
            "inputs": {
                "excel_filename": task.get("roads_filename"),
                "roads_filename": task.get("roads_filename"),
            },
        },
    }


@app.post("/ftth/hld/run/step/object", status_code=202)
async def run_step_object(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    excel: UploadFile = File(...),
    sheet: Optional[str] = Form(None),
    email: Optional[str] = Form("you@example.com"),
    out_crs: Optional[str] = Form("EPSG:25833"),
    thin: bool = Form(False),
) -> Dict[str, Any]:
    """Run the Object Layer step individually. Produces Objects.gpkg."""
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize project if new
    _task(project_id)
    if postgis.is_available():
        postgis.init_schema()
        postgis.upsert_project(project_id, status="queued", output_dir=str(output_dir))
        _init_pipeline_state(project_id, excel_filename=excel.filename)
        postgis.update_inputs_state(project_id, excel_filename=excel.filename)

    excel_path = _save_upload(excel, upload_dir, "addresses.xlsx")
    out_gpkg = str(output_dir / "Objects.gpkg")

    step_params = {
        "EXCEL": str(excel_path),
        "SHEET": sheet or "",
        "EMAIL": email or "you@example.com",
        "OUT_CRS": out_crs or "EPSG:25833",
        "OUT_GPKG": out_gpkg,
        "THIN_EXPORT": "1" if thin else "0",
    }

    def _run() -> None:
        try:
            _run_step(project_id, "object", output_dir, step_params)
            _append(project_id, "info", "Object Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Object Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Object Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


@app.post("/ftth/hld/run/step/polygon", status_code=202)
async def run_step_polygon(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    input_objects: UploadFile = File(...),
    method: int = Form(3),
    planning_first: bool = Form(False),
    min_hh: int = Form(32),
    max_hh: int = Form(128),
    neighbor_dist: float = Form(150.0),
    service_radius: float = Form(300.0),
    road_access_dist: float = Form(100.0),
    buffer: float = Form(0.0),
    barrier_roads: Optional[UploadFile] = File(None),
    barrier_classes: Optional[str] = Form("motorway,trunk,primary,secondary"),
) -> Dict[str, Any]:
    """Run the Polygon Layer step individually. Requires Objects.gpkg."""
    _check_dependency(project_id, "polygon")
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    _task(project_id)
    objects_path = _save_upload(input_objects, upload_dir, "Objects.gpkg")
    out_gpkg = str(output_dir / "Polygons.gpkg")

    step_params: Dict[str, str] = {
        "INPUT": str(objects_path),
        "METHOD": str(method),
        "PLANNING_FIRST": "1" if planning_first else "0",
        "MIN_HH_PER_POLYGON": str(min_hh),
        "MAX_HH_PER_POLYGON": str(max_hh),
        "NEIGHBOR_DIST": str(neighbor_dist),
        "SERVICE_RADIUS": str(service_radius),
        "ROAD_ACCESS_DIST": str(road_access_dist),
        "BUFFER": str(buffer),
        "THIN_EXPORT": "0",
        "OUT": out_gpkg,
    }
    if barrier_classes:
        step_params["BARRIER_MAIN_CLASSES"] = barrier_classes

    def _run() -> None:
        try:
            _run_step(project_id, "polygon", output_dir, step_params)
            _append(project_id, "info", "Polygon Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Polygon Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Polygon Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


@app.post("/ftth/hld/run/step/network", status_code=202)
async def run_step_network(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    input_polygons: UploadFile = File(...),
    input_objects: UploadFile = File(...),
    roads: UploadFile = File(...),
    mfg_override: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    """Run the Network Layer step individually. Produces PDPs.gpkg and MFG.gpkg."""
    _check_dependency(project_id, "network")
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    _task(project_id)
    polys_path = _save_upload(input_polygons, upload_dir, "Polygons.gpkg")
    objs_path = _save_upload(input_objects, upload_dir, "Objects.gpkg")
    roads_path = _save_upload(roads, upload_dir, "roads.gpkg")

    step_params: Dict[str, str] = {
        "INPUT_POLY": str(polys_path),
        "INPUT_OBJECTS": str(objs_path),
        "INPUT_ROADS": str(roads_path),
        "OUT_EDGES": "TEMPORARY_OUTPUT",
        "OUT_CAND": "TEMPORARY_OUTPUT",
        "OUT_REMOVED": "TEMPORARY_OUTPUT",
        "OUT_CLEAN": "TEMPORARY_OUTPUT",
        "OUT_ASSIGNED": str(output_dir / "PDPs.gpkg"),
        "OUT_MFG_POINT": str(output_dir / "MFG.gpkg"),
        "OUT_FINAL_OBJECTS": str(objs_path),  # updates objects with PDP IDs
    }

    # If user provided an MFG point override, save it and pass to the algorithm.
    # The network layer uses INPUT_MFG to skip MFG generation and use this point.
    mfg_override_path: Optional[Path] = None
    if mfg_override is not None:
        mfg_override_path = _save_upload(mfg_override, upload_dir, "MFG_override.gpkg")
        step_params["INPUT_MFG"] = str(mfg_override_path)

    def _run() -> None:
        try:
            _run_step(project_id, "network", output_dir, step_params)
            _append(project_id, "info", "Network Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Network Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Network Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


@app.post("/ftth/hld/run/step/trench", status_code=202)
async def run_step_trench(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    input_polygons: UploadFile = File(...),
    input_roads: UploadFile = File(...),
    input_pdp: UploadFile = File(...),
    input_objects: UploadFile = File(...),
    input_mfg: UploadFile = File(...),
    buildings: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    """Run the Trench Layer step individually. Produces all trench GPKGs."""
    _check_dependency(project_id, "trench")
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    _task(project_id)
    polys_path = _save_upload(input_polygons, upload_dir, "Polygons.gpkg")
    roads_path = _save_upload(input_roads, upload_dir, "roads.gpkg")
    pdp_path = _save_upload(input_pdp, upload_dir, "PDPs.gpkg")
    objs_path = _save_upload(input_objects, upload_dir, "Objects.gpkg")
    mfg_path = _save_upload(input_mfg, upload_dir, "MFG.gpkg")

    step_params: Dict[str, str] = {
        "INPUT_POLY": str(polys_path),
        "INPUT_ROADS": str(roads_path),
        "INPUT_PDP": str(pdp_path),
        "INPUT_HOUSEHOLDS": str(objs_path),
        "INPUT_MFG": str(mfg_path),
        "OUT_FINAL_TRENCHES": str(output_dir / "Final_Trenches.gpkg"),
        "OUT_SIDEWALK_LEFT": "TEMPORARY_OUTPUT",
        "OUT_SIDEWALK_RIGHT": "TEMPORARY_OUTPUT",
        "OUT_MERGED_PDP": "TEMPORARY_OUTPUT",
        "OUT_FEEDER_FINAL": "TEMPORARY_OUTPUT",
        "OUT_GARDEN_TRENCHES": "TEMPORARY_OUTPUT",
        "OUT_DISTRIBUTION_LINES": "TEMPORARY_OUTPUT",
        "OUT_DISTRIBUTION_DISS": "TEMPORARY_OUTPUT",
        "OUT_FINAL_TANGENT_TRENCHES": "TEMPORARY_OUTPUT",
    }

    def _run() -> None:
        try:
            _run_step(project_id, "trench", output_dir, step_params)
            _append(project_id, "info", "Trench Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Trench Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Trench Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


@app.post("/ftth/hld/run/step/cable", status_code=202)
async def run_step_cable(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    feeder_trench: UploadFile = File(...),
    garden_trench: UploadFile = File(...),
    distribution_trench: UploadFile = File(...),
) -> Dict[str, Any]:
    """Run the Cable Layer step individually. Produces Feeder_Cable and Distribution_Cable."""
    _check_dependency(project_id, "cable")
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    _task(project_id)
    feeder_path = _save_upload(feeder_trench, upload_dir, "Feeder_Trench.gpkg")
    garden_path = _save_upload(garden_trench, upload_dir, "Garden_Trench.gpkg")
    dist_path = _save_upload(distribution_trench, upload_dir, "Distribution_Trench.gpkg")

    step_params: Dict[str, str] = {
        "FEEDER_TRENCH": str(feeder_path),
        "GARDEN_TRENCHES": str(garden_path),
        "DISTR_TRENCHES": str(dist_path),
        "OUT_FEEDER_CABLE": str(output_dir / "Feeder_Cable.gpkg"),
        "OUT_DISTRIBUTION_CABLE": str(output_dir / "Distribution_Cable.gpkg"),
    }

    def _run() -> None:
        try:
            _run_step(project_id, "cable", output_dir, step_params)
            _append(project_id, "info", "Cable Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Cable Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Cable Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


@app.post("/ftth/hld/run/step/duct", status_code=202)
async def run_step_duct(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    network_lines: UploadFile = File(...),
    mfg_points: UploadFile = File(...),
    pdp_points: UploadFile = File(...),
    object_points: UploadFile = File(...),
) -> Dict[str, Any]:
    """Run the Duct Layer step individually. Produces Feeder_Ducts and Distribution_Ducts."""
    _check_dependency(project_id, "duct")
    output_dir = OUTPUT_DIR / project_id
    upload_dir = output_dir / "inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    _task(project_id)
    nl_path = _save_upload(network_lines, upload_dir, "Final_Trenches.gpkg")
    mfg_path = _save_upload(mfg_points, upload_dir, "MFG.gpkg")
    pdp_path = _save_upload(pdp_points, upload_dir, "PDPs.gpkg")
    obj_path = _save_upload(object_points, upload_dir, "Objects.gpkg")

    step_params: Dict[str, str] = {
        "NETWORK_LINES": str(nl_path),
        "MFG_POINTS": str(mfg_path),
        "PDP_POINTS": str(pdp_path),
        "OBJECT_POINTS": str(obj_path),
        "OUT_FEEDER_DUCTS": str(output_dir / "Feeder_Ducts.gpkg"),
        "OUT_DISTRIBUTION_DUCTS": str(output_dir / "Distribution_Ducts.gpkg"),
    }

    def _run() -> None:
        try:
            _run_step(project_id, "duct", output_dir, step_params)
            _append(project_id, "info", "Duct Layer step complete.")
        except Exception as exc:
            _append(project_id, "error", f"Duct Layer failed: {exc}")

    background_tasks.add_task(_run)

    task = _task(project_id)
    task.update({"status": "queued", "stage": "Duct Layer", "output_dir": str(output_dir), "updated_at": _now()})
    return _public_task(project_id)


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
