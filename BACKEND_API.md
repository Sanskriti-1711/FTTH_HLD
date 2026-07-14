# FTTH Engine — Backend API Documentation

> **Project:** HLD Planning — Fibre-To-The-Home (FTTH) High-Level Design  
> **Backend:** FastAPI + QGIS `qgis_process` + PostGIS  
> **Frontend:** React (Vite)  
> **API Base URL (local):** `http://localhost:8080`

---

## Table of Contents

1. [Docker Quick Start](#1-docker-quick-start)
2. [Pipeline Overview](#2-pipeline-overview)
3. [API Endpoints](#3-api-endpoints)
4. [Frontend Wiring Guide](#4-frontend-wiring-guide)
5. [Testing & Debugging](#5-testing--debugging)
6. [Project File Structure](#6-project-file-structure)
7. [PostGIS Schema](#7-postgis-schema)
8. [Environment Variables](#8-environment-variables)
9. [Common Recipes](#9-common-recipes)

---

## 1. Docker Quick Start

### Start the Stack

```bash
# From the project root (HLD_Planning_01/)
docker compose up -d

# Wait for both services to become healthy (~20–30 s)
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Expected output:
```
NAME                                      STATUS
hld_planning_01-postgis-1                 Up About a minute (healthy)
hld_planning_01-ftth-engine-1             Up About a minute
```

### Rebuild the Engine Image

Use `--no-cache` only when you change `web/backend/main.py`, `web/backend/Dockerfile`, or any `HLDPlanning/` Python files:

```bash
docker compose build --no-cache ftth-engine
docker compose up -d
```

### View Logs

```bash
# Container logs (API + qgis_process stdout)
docker logs hld_planning_01-ftth-engine-1

# Tail continuously
docker logs -f hld_planning_01-ftth-engine-1

# HLD pipeline log files (inside container)
docker exec hld_planning_01-ftth-engine-1 sh -c \
  "cat /app/HLDPlanning/logs/OC_$(date +%Y%m%d)*.txt | tail -50"
```

### Stop & Clean

```bash
# Stop containers (data persists in volumes)
docker compose down

# Stop + remove volumes (WARNING: deletes PostGIS data)
docker compose down -v

# Clean old pipeline outputs (host-side)
rm -rf web/backend/outputs/*/

# Clean HLD logs inside the container
docker exec hld_planning_01-ftth-engine-1 sh -c \
  "rm -rf /app/HLDPlanning/logs/*.txt"
```

### Run ad-hoc commands inside the container

```bash
# Python (sanity-check Python is installed)
docker exec hld_planning_01-ftth-engine-1 python3 -c "print('hello')"

# List registered algorithms (look for hldplanning:*)
docker exec hld_planning_01-ftth-engine-1 qgis_process list

# Show parameters of the end-to-end pipeline
docker exec hld_planning_01-ftth-engine-1 qgis_process help hldplanning:end_to_end_pipeline

# Interactive shell
docker exec -it hld_planning_01-ftth-engine-1 sh
```

### Run the plugin directly inside the container (bypassing the API)

The backend's `_run_pipeline` builds the following `qgis_process` command. You can invoke it directly via `docker exec` if you want to skip the FastAPI layer — for example, when iterating on plugin code.

**Prerequisite:** the input files must already exist inside the container at the path you reference. The upload directory is `/app/web/backend/outputs/<project_id>/inputs/`.

```bash
docker exec hld_planning_01-ftth-engine-1 qgis_process run \
  hldplanning:end_to_end_pipeline -- \
  EXCEL=/app/web/backend/outputs/<project_id>/inputs/addresses.xlsx \
  ROADS=/app/web/backend/outputs/<project_id>/inputs/roads.gpkg \
  OUTPUT_DIR=/app/web/backend/outputs/<project_id>/ \
  POLY_METHOD=3
```

**Or interactively inside the container:**

```bash
docker exec -it hld_planning_01-ftth-engine-1 sh
qgis_process run hldplanning:end_to_end_pipeline -- \
    EXCEL=/path/to/file.xlsx \
    ROADS=/path/to/roads.gpkg \
    OUTPUT_DIR=/path/to/output/ \
    POLY_METHOD=3
```

**Run a single pipeline stage alone (debugging):**

```bash
# Polygon layer (stage 1) with full help
docker exec hld_planning_01-ftth-engine-1 qgis_process run \
  hldplanning:02_polygon_layer --help

# Object layer (stage 0)
docker exec hld_planning_01-ftth-engine-1 qgis_process run \
  hldplanning:01_object_layer --help
```

### Port mapping

| Host port | Container port | Service |
|-----------|---------------|---------|
| `8080`    | `8000`        | FastAPI backend |
| `5432`    | `5432`        | PostGIS |

> **Note:** Host port 8000 may be occupied by a Docker Desktop ghost socket, so the Docker Compose file maps `8080:8000`.

---

## 2. Pipeline Overview

The HLD pipeline runs **6 sequential stages** inside `qgis_process` via the `hldplanning:end_to_end_pipeline` algorithm:

| # | Stage | Algorithm | Output Files |
|---|-------|-----------|-------------|
| 0 | **Object Layer** | `01_object_layer` | `Objects.gpkg` |
| 1 | **Polygon Layer** | `02_polygon_layer` | `Polygons.gpkg` |
| 2 | **Network Layer** | `03_network_layer` | `PDPs.gpkg`, `MFG.gpkg`, `Network.gpkg` |
| 3 | **Trench Layer** | `04_trench_layer` | `Final_Trenches.gpkg` |
| 4 | **Cable Layer** | `05_cable_layer` | `Feeder_Cable.gpkg`, `Distribution_Cable.gpkg` |
| 5 | **Duct Layer** | `06_duct_layer` | `Feeder_Ducts.gpkg`, `Distribution_Ducts.gpkg` |

### Inputs

- **Excel file** (`.xlsx`) — Address dataset with premise data, including:
  - Geocoded addresses (lat/lon or X/Y columns)
  - Household counts (HH column)
- **Roads file** — OSM road network (`.gpkg`, `.shp`, or `.zip` archive of shapefiles)

If a `.zip` file is uploaded as roads, the backend automatically:
1. Extracts the archive
2. Finds the first usable vector file (priority: `.gpkg` → `.shp` containing "road" → any `.shp` → `.geojson`)
3. Passes the extracted file path to `qgis_process`

### Polygon Generation Method

The `poly_method` parameter controls which algorithm generates service-area polygons:

| Value | Method | Description |
|-------|--------|-------------|
| `0` | Convex Hull | Simple convex hull per cluster |
| `1` | Concave Hull | Alpha-shape hull |
| `2` | Voronoi | Voronoi partition → dissolve by group |
| **`3` (default)** | **Seeded Growth** | Constrained agglomerative clustering with road barriers, capacity caps (32–128 homes), service radius (300 m), and road-access validation |

---

## 3. API Endpoints

### 3.1 Health / Root

```
GET /health
GET /
```

**Purpose:** Health check and service metadata.

**Response:**
```json
{
  "status": "ok",
  "service": "ftth-engine-api",
  "started_at": "2026-07-14T09:00:00+00:00",
  "uptime_seconds": 3600,
  "qgis_process": "/usr/bin/qgis_process",
  "postgis": {
    "available": true,
    "postgis_version": "POSTGIS=\"3.4.2\" [EXTENSION] ..."
  },
  "endpoints": [
    "POST /ftth/hld/run",
    "GET /ftth/hld/results/{project_id}",
    "GET /ftth/hld/results/{project_id}/layers/{layer}",
    "GET /ftth/hld/download/{project_id}/{file_path}",
    "GET /tiles/{layer}/{z}/{x}/{y}.pbf?project_id={project_id}",
    "GET /ftth/projects"
  ]
}
```

---

### 3.2 Run Pipeline

```
POST /ftth/hld/run
POST /run-hld                         ← compatibility alias
```

**Purpose:** Submit a new HLD pipeline run.

**Request:** `multipart/form-data`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `excel` | File | ✅ | — | Address dataset `.xlsx` file |
| `roads` | File | ✅ | — | Roads file (`.gpkg`, `.shp`, or `.zip`) |
| `project_id` | String | ❌ | UUID hex (32 chars) | Optional custom project ID |
| `poly_method` | Integer | ❌ | `3` | Polygon generation method (0–3) |

**Response** (HTTP `202 Accepted`):
```json
{
  "project_id": "a395311b15424d8d85f7bd211c82598f",
  "status": "queued",
  "stage": null,
  "stage_index": 0,
  "stage_count": 6,
  "progress": 0,
  "layers": [],
  "downloads": [],
  "messages": [],
  "created_at": "2026-07-14T09:00:00+00:00",
  "updated_at": "2026-07-14T09:00:00+00:00",
  "results_url": "/ftth/hld/results/a395311b15424d8d85f7bd211c82598f",
  "tile_url_template": "/tiles/{layer}/{z}/{x}/{y}.pbf?project_id=a395311b15424d8d85f7bd211c82598f"
}
```

**cURL example (bash / Git Bash / WSL):**
```bash
curl -X POST http://localhost:8080/ftth/hld/run \
  -F "excel=@/path/to/Main_DataSet.xlsx" \
  -F "roads=@/path/to/berlin-roads.gpkg" \
  -F "poly_method=3"
```

**PowerShell equivalents (Windows):**

Single line (always works):
```powershell
curl.exe -s -X POST http://localhost:8080/ftth/hld/run -F "excel=@D:/…/Main_DataSet.xlsx" -F "roads=@D:/…/berlin-roads.gpkg" -F "poly_method=3"
```

Multi-line backtick continuation:
```powershell
curl.exe -s -X POST http://localhost:8080/ftth/hld/run `
  -F "excel=@D:/…/Main_DataSet.xlsx" `
  -F "roads=@D:/…/berlin-roads.gpkg" `
  -F "poly_method=3"
```

Pure `Invoke-WebRequest` (no curl at all):
```powershell
$form = @{
    excel       = Get-Item "D:\path\to\Main_DataSet.xlsx"
    roads       = Get-Item "D:\path\to\berlin-roads.gpkg"
    poly_method = "3"
}
$ProjectId = (Invoke-WebRequest -Method Post -Uri "http://localhost:8080/ftth/hld/run" -Form $form).Content | ConvertFrom-Json | Select-Object -ExpandProperty project_id
```

> PowerShell aliases `curl` to `Invoke-WebRequest`, which **does not support** `-F` form-data and **does not accept** `\` line continuations. Use `curl.exe` (real GNU curl), backtick `` ` `` continuations, or `Invoke-WebRequest` with `-Form`. To permanently fix the broken alias, run once:
> ```powershell
> Set-Alias -Name curl -Value curl.exe -Option AllScope -Force
> ```

**JavaScript fetch (for frontend):**
```javascript
const form = new FormData();
form.append("excel", excelFile);       // File object
form.append("roads", roadsFile);       // File object
form.append("poly_method", "3");

const res = await fetch("http://localhost:8080/ftth/hld/run", {
  method: "POST",
  body: form,
});
const data = await res.json();
// data.project_id → use to poll results
```

---

### 3.3 Get Results / Status

```
GET /ftth/hld/results/{project_id}
GET /status/{project_id}               ← compatibility alias
```

**Purpose:** Poll pipeline status, messages, and completed layer info.  
**Use case:** Frontend polls this every 1–5 seconds while `status` is `"running"`.

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | String | 32-hex-char project UUID |
| `status` | String | `"queued"`, `"running"`, `"completed"`, `"failed"` |
| `stage` | String | Current pipeline stage name (e.g., `"Polygon Layer"`) |
| `stage_index` | Integer | 0-based index of current stage |
| `stage_count` | Integer | Total stages (always 6) |
| `progress` | Integer | Percentage 0–100 |
| `messages` | Array | Real-time streaming log messages |
| `layers` | Array | Completed layer info (only when `status="completed"`) |
| `downloads` | Array | Available download files |
| `error` | String | Error message (only when `status="failed"`) |
| `results_url` | String | URL to this endpoint |
| `tile_url_template` | String | URL template for vector tiles |

**cURL example:**
```bash
curl -s http://localhost:8080/ftth/hld/results/a395311b15424d8d85f7bd211c82598f | jq .
```

**Polling loop (JavaScript):**
```javascript
async function pollResults(projectId) {
  const delay = (ms) => new Promise(r => setTimeout(r, ms));

  while (true) {
    const res = await fetch(
      `http://localhost:8080/ftth/hld/results/${projectId}`
    );
    const data = await res.json();

    console.log(
      `[${data.stage_index}/${data.stage_count}] ${data.stage} — ${data.progress}%`
    );

    if (data.status === "completed") {
      return data;    // <- layers, downloads available
    }
    if (data.status === "failed") {
      throw new Error(data.error);
    }

    await delay(3000);  // poll every 3 seconds
  }
}
```

**Streaming messages:** Messages are appended in **real-time** as `qgis_process` writes each stdout line (no batching at end). Each message:
```json
{
  "ts": "2026-07-14T09:05:30+00:00",
  "level": "info",
  "text": "[4/6] [Trench Layer] Building sidewalk+tangent graph …"
}
```

---

### 3.4 Get Layer GeoJSON

```
GET /ftth/hld/results/{project_id}/layers/{layer}
GET /layers/{project_id}/{layer}        ← compatibility alias
```

**Purpose:** Retrieve a completed pipeline layer as **GeoJSON FeatureCollection**.

**Layer name values** (case-insensitive):

| Layer name | Table name | Geometry | Description |
|-----------|-----------|----------|-------------|
| `objects` | `object_layer` | Point | Premises / address points |
| `polygons` | `polygon_layer` | Polygon | Service-area polygons (FDP/PDP zones) |
| `network` | `network_layer` | Line | Road network with PDP/MFG assignments |
| `trenches` | `trench_layer` | Line | Trench routes |
| `cables` | `cable_layer` | Line | Feeder & distribution cables |
| `ducts` | `duct_layer` | Line | Feeder & distribution duct routes |

**Response:** GeoJSON FeatureCollection
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "id": 0,
      "geometry": { "type": "Polygon", "coordinates": [...] },
      "properties": {
        "POLYGON_ID": "FDP-001",
        "PDP_ID": "PDP-001",
        "MFG_ID": "MFG-01",
        "SRC_ID": "SRC-001",
        "STAGE": "02_polygon_layer"
      }
    }
  ]
}
```

**cURL example:**
```bash
curl http://localhost:8080/ftth/hld/results/a395311/layers/polygons | jq .
```

**JavaScript (MapLibre / Leaflet):**
```javascript
const res = await fetch(
  `http://localhost:8080/ftth/hld/results/${projectId}/layers/polygons`
);
const geojson = await res.json();

// Use with MapLibre / Mapbox GL
map.addSource("polygons", { type: "geojson", data: geojson });
```

---

### 3.5 Download Files

```
GET /ftth/hld/download/{project_id}/{file_path}
```

**Purpose:** Download any output file (GPKG, GeoJSON, XLSX, CSV, TXT).

**Supported extensions:** `.gpkg`, `.xlsx`, `.csv`, `.json`, `.geojson`, `.txt`

**Available files (from `downloads` array in results):**
```json
[
  {
    "name": "Objects.gpkg",
    "url": "/ftth/hld/download/a395311/Objects.gpkg",
    "size_bytes": 172032
  },
  {
    "name": "Distribution_Ducts.gpkg",
    "url": "/ftth/hld/download/a395311/Distribution_Ducts.gpkg",
    "size_bytes": 266240
  },
  {
    "name": "Feeder_Cable.geojson",
    "url": "/ftth/hld/download/a395311/Feeder_Cable.geojson",
    "size_bytes": 241664
  }
  // ... plus Objects.geojson, Polygons.gpkg, Polygons.geojson, etc.
]
```

**cURL example:**
```bash
curl -o Polygons.gpkg \
  http://localhost:8080/ftth/hld/download/a395311/Polygons.gpkg
```

---

### 3.6 Vector Tiles (MapLibre / MVT)

```
GET /tiles/{layer}/{z}/{x}/{y}.pbf?project_id={project_id}
```

**Purpose:** Serve Mapbox Vector Tiles (MVT) for efficient rendering.  
**Requires:** PostGIS service to be running.

| Param | Type | Description |
|-------|------|-------------|
| `layer` | Path | Layer name (e.g., `polygons`) |
| `z` | Path | Zoom level |
| `x` | Path | Tile column |
| `y` | Path | Tile row |
| `project_id` | Query | Project UUID |

**Response:** `application/vnd.mapbox-vector-tile` (binary PBF)

**MapLibre source config:**
```javascript
map.addSource("polygons_tiles", {
  type: "vector",
  tiles: [
    `http://localhost:8080/tiles/polygons/{z}/{x}/{y}.pbf?project_id=${projectId}`
  ],
  minzoom: 10,
  maxzoom: 16,
});
```

**Note:** Returns HTTP 503 if PostGIS is not available. Set `minzoom` to at least 10 for good performance on FTTH-scale data.

---

### 3.7 List Projects

```
GET /ftth/projects?limit=50
```

**Purpose:** List recent pipeline runs (completed, running, failed).

**Response:**
```json
[
  {
    "project_id": "a395311b...",
    "status": "completed",
    "roads_filename": "berlin-roads.gpkg",
    "runner": "qgis_process",
    "created_at": "2026-07-14T09:00:00+00:00",
    "updated_at": "2026-07-14T09:13:00+00:00",
    "downloads": [...]
  }
]
```

---

## 4. Frontend Wiring Guide

### 4.1 Typical Integration Flow

1. **User uploads** an Excel file and a Roads file → your frontend calls `POST /ftth/hld/run`
2. **Backend returns** a `project_id` (HTTP 202)
3. **Frontend begins polling** `GET /ftth/hld/results/{project_id}` every 2–3 seconds
4. **Messages stream in real-time** — display them as a log/console in the UI
5. **When `status = "completed"`**, the `layers` array has feature counts and geometry types
6. **Render layers** by fetching GeoJSON or configuring MVT tile sources
7. **Offer downloads** from the `downloads` array

### 4.2 MapLibre Integration Example

```javascript
import maplibregl from "maplibre-gl";

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {},
    layers: [],
  },
  center: [13.405, 52.52],   // Berlin
  zoom: 11,
});

// After pipeline completes:
async function loadPipelineLayers(projectId) {
  const res = await fetch(
    `http://localhost:8080/ftth/hld/results/${projectId}/layers/polygons`
  );
  const geojson = await res.json();

  map.addSource("polygons", { type: "geojson", data: geojson });
  map.addLayer({
    id: "polygons-fill",
    type: "fill",
    source: "polygons",
    paint: {
      "fill-color": [
        "match", ["get", "REVIEW"],
        1, "#ff4444",          // red = flagged
        "#00aa88"              // default green
      ],
      "fill-opacity": 0.3,
    },
  });
  map.addLayer({
    id: "polygons-outline",
    type: "line",
    source: "polygons",
    paint: {
      "line-color": "#006644",
      "line-width": 2,
    },
  });
}
```

### 4.3 Complete Frontend Flow

```
┌────────────┐     POST /ftth/hld/run     ┌──────────────┐
│  Frontend  │ ──────────────────────────> │   Backend    │
│  (React)   │ <── HTTP 202 + project_id ─ │  (FastAPI)   │
└────────────┘                             └──────┬───────┘
       │                                          │
       │  ┌─────────────────────────────────┐     │
       │  │  Poll every 3s                  │     │
       │  │  GET /ftth/hld/results/{id}     │     │
       │  │  - Show messages in real-time   │────>│ qgis_process
       │  │  - Update progress bar          │<────│ (pipeline runs)
       │  └─────────────────────────────────┘     │
       │                                          │
       │  When status = "completed":              │
       │  ┌──────────────────────────────┐        │
       │  │ GET /layers/{id}/polygons    │───────>│ PostGIS
       │  │ GET /layers/{id}/trenches    │<───────│ or GPKG
       │  │ ...                          │        │
       │  └──────────────────────────────┘        │
       │                                          │
       │  Download buttons:                       │
       │  GET /ftth/hld/download/{id}/{file}      │
       └──────────────────────────────────────────┘
```

### 4.4 CORS

The backend allows **all origins** (`allow_origins=["*"]`), so no CORS issues during local development. For production, restrict to your frontend domain.

---

## 5. Testing & Debugging

### 5.1 Manual API Testing (cURL)

**Start a pipeline:**
```bash
curl -v -X POST http://localhost:8080/ftth/hld/run \
  -F "excel=@D:/Downloads_D/Q-GIS/Main_DataSet.xlsx" \
  -F "roads=@D:/Downloads_D/Q-GIS/berlin-roads.gpkg"
# → note the project_id from the response
```

**Poll until complete:**
```bash
PROJECT_ID="your-project-id-here"
while true; do
  clear
  curl -s http://localhost:8080/ftth/hld/results/$PROJECT_ID | \
    python -c "
import sys, json
v = json.load(sys.stdin)
print(f\"Status: {v['status']}\")
print(f\"Stage: {v.get('stage_index',0)}/{v['stage_count']} [{v.get('stage','-')}] {v.get('progress',0)}%\")
msgs = v.get('messages', [])
for m in msgs[-5:]:
    txt = m.get('text','')
    if txt: print(f'  >> {txt[:200]}')
if v['status'] == 'completed':
    print()
    print('Layers:')
    for lyr in v.get('layers', []):
        print(f'  {lyr[\"name\"]}: {lyr[\"feature_count\"]} features ({lyr.get(\"geometry_type\",\"?\")})')
if v['status'] in ('completed','failed'):
    exit(0)
"
  sleep 10
done
```

**Get layer data:**
```bash
curl -s http://localhost:8080/ftth/hld/results/$PROJECT_ID/layers/polygons \
  | python -c "import sys, json; d=json.load(sys.stdin); print(len(d['features']), 'features')"
```

### 5.2 Health Check

```bash
curl -s http://localhost:8080/health | python -m json.tool
```

### 5.3 Inside-Container Debugging

```bash
# Check if QGIS plugin is registered
docker exec hld_planning_01-ftth-engine-1 qgis_process list \
  | grep -i hld

# Check pipeline output files
docker exec hld_planning_01-ftth-engine-1 sh -c \
  "ls -la /app/web/backend/outputs/*/*.gpkg 2>&1"

# Get feature counts from output GPKGs
docker exec hld_planning_01-ftth-engine-1 python3 -c "
import os
from osgeo import ogr
base = '/app/web/backend/outputs/a395311/'
for f in sorted(os.listdir(base)):
    if not f.endswith('.gpkg'): continue
    ds = ogr.Open(os.path.join(base, f))
    lyr = ds.GetLayer(0)
    name = lyr.GetLayerDefn().GetName()
    count = lyr.GetFeatureCount()
    print(f'{f}: {count} features ({name})')
"

# Read HLD log file
docker exec hld_planning_01-ftth-engine-1 sh -c \
  "ls -t /app/HLDPlanning/logs/ | head -1 | xargs -I{} tail -50 /app/HLDPlanning/logs/{}"

# Check if qgis_process is still running
docker exec hld_planning_01-ftth-engine-1 sh -c \
  "ps aux | grep '[q]gis_process'"
```

### 5.4 Frontend Testing (fetch)

```javascript
// Test in browser console
async function testAPI() {
  // Health
  let r = await fetch("http://localhost:8080/health");
  console.log("Health:", await r.json());

  // Run pipeline
  const form = new FormData();
  form.append("excel", new File(["..."], "test.xlsx"));
  form.append("roads", new File(["..."], "roads.gpkg"));
  r = await fetch("http://localhost:8080/ftth/hld/run", {
    method: "POST", body: form,
  });
  const { project_id } = await r.json();
  console.log("Project:", project_id);

  // Poll
  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 5000));
    r = await fetch(`http://localhost:8080/ftth/hld/results/${project_id}`);
    const data = await r.json();
    console.log(`${data.stage} — ${data.progress}%`);
    if (data.status === "completed" || data.status === "failed") break;
  }
}
```

---

## 6. Project File Structure

```
HLD_Planning_01/
├── docker-compose.yml                  ← Service definitions
├── web/
│   ├── backend/
│   │   ├── main.py                     ← FastAPI app (ALL endpoints)
│   │   ├── postgis.py                  ← PostGIS storage layer
│   │   ├── Dockerfile                  ← Engine image build
│   │   ├── requirements.txt            ← Python deps (reference)
│   │   └── outputs/                    ← Pipeline output GPKGs/GeoJSONs
│   └── frontend/
│       ├── index.html
│       ├── vite.config.js
│       └── src/
│           ├── main.jsx
│           ├── App.jsx
│           └── App.css
├── HLDPlanning/
│   ├── plugin.py                       ← QGIS plugin entry point
│   ├── provider.py
│   ├── metadata.txt
│   ├── algorithms/
│   │   ├── object_layer.py             ← Stage 0
│   │   ├── polygon_layer.py            ← Stage 1 (seeded growth)
│   │   ├── network_layer.py            ← Stage 2
│   │   ├── trench_layer.py             ← Stage 3
│   │   ├── cable_layer.py              ← Stage 4
│   │   ├── duct_layer.py               ← Stage 5
│   │   └── oneclick.py                 ← Pipeline orchestrator
│   └── utils/
│       ├── fields.py
│       ├── geom_utils.py
│       ├── splitters.py
│       ├── pricing.py
│       ├── network_registry.py
│       └── ...                         ← Various utility modules
```

---

## 7. PostGIS Schema

When PostGIS is available (`postgis` container healthy), the backend stores pipeline data in:

### `ftth_projects` table

| Column | Type | Description |
|--------|------|-------------|
| `project_id` | TEXT PK | UUID |
| `status` | TEXT | queued / running / completed / failed |
| `runner` | TEXT | e.g. "qgis_process" |
| `qgis_version` | TEXT | QGIS version used |
| `roads_filename` | TEXT | Original roads filename |
| `error` | TEXT | Error message if failed |
| `output_dir` | TEXT | Path to output files |
| `downloads` | JSONB | Available download URLs |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | |

### Layer tables (one per stage)

`object_layer`, `polygon_layer`, `network_layer`, `trench_layer`, `cable_layer`, `duct_layer`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PK | Auto-increment |
| `project_id` | TEXT FK → ftth_projects | ON DELETE CASCADE |
| `fid` | INTEGER | Feature ID |
| `geom` | GEOMETRY(Geometry, 4326) | Reprojected to WGS84 |
| `"POLYGON_ID"` | TEXT | Polygon/FDP identifier |
| `"PDP_ID"` | TEXT | PDP identifier |
| `"MFG_ID"` | TEXT | MFG identifier |
| `"SRC_ID"` | TEXT | Source ID |
| `"STAGE"` | TEXT | Stage name |
| `properties` | JSONB | All other attributes |

Indexes: `(project_id)`, `GIST (geom)`

---

## 8. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://ftth:ftth@localhost:5432/ftth` | PostGIS connection string |
| `QGIS_EXECUTABLE` | auto-detected | Path to `qgis_process` binary |
| `QGIS_PLUGINPATH` | `/app` | Directory with the HLDPlanning plugin |
| `QT_QPA_PLATFORM` | `offscreen` | Headless Qt platform (no display needed) |
| `QGIS_PROCESS_TIMEOUT` | `10800` | Pipeline timeout in seconds (3 hours) |
| `PGHOST` | `localhost` | Postgres host (fallback when no DATABASE_URL) |
| `PGPORT` | `5432` | Postgres port |
| `PGDATABASE` | `ftth` | Database name |
| `PGUSER` | `ftth` | Database user |
| `PGPASSWORD` | `ftth` | Database password |
| `PGCONNECT_TIMEOUT` | `2` | Connection timeout |

---

## 9. Common Recipes

### 9.1 Upload and wait for results in a single script

```bash
#!/bin/bash
# run_and_wait.sh — Submit pipeline and poll until done

HOST="http://localhost:8080"
EXCEL="$1"
ROADS="$2"

if [ -z "$EXCEL" ] || [ -z "$ROADS" ]; then
  echo "Usage: $0 <excel.xlsx> <roads.gpkg>"
  exit 1
fi

echo "=== Submitting pipeline ==="
RESP=$(curl -sS -w "\n%{http_code}" -X POST "$HOST/ftth/hld/run" \
  -F "excel=@$EXCEL" -F "roads=@$ROADS" -F "poly_method=3")

PROJECT_ID=$(echo "$RESP" | head -1 | python -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "Project ID: $PROJECT_ID"
echo ""

echo "=== Polling ==="
while true; do
  sleep 10
  STATUS=$(curl -sS "$HOST/ftth/hld/results/$PROJECT_ID" | python -c "
import sys, json
v = json.load(sys.stdin)
s = v['status']
si = v.get('stage_index',0)
st = v.get('stage','-')
p = v.get('progress',0)
print(f'{si}/6 [{st}] {p}% — {s}')
msgs = v.get('messages', [])
for m in msgs[-2:]:
    txt = m.get('text','')
    if txt: print(f'  >> {txt[:150]}')
if s == 'completed':
    print()
    print('Layers:')
    for lyr in v.get('layers', []):
        print(f'  {lyr[\"name\"]}: {lyr[\"feature_count\"]}')
if s in ('completed','failed'):
    print('FINAL:', s)
    exit(0 if s == 'completed' else 1)
")
  echo "$STATUS"
done
```

### 9.2 Diff two pipeline runs

```bash
# Get feature counts for all layers in project A
for f in web/backend/outputs/PROJECT_A/*.gpkg; do
  name=$(basename "$f")
  count=$(ogrinfo -so "$f" 2>&1 | grep "Feature Count" | grep -oP "\d+")
  echo "$name: $count"
done > /tmp/project_a.txt

# Same for project B, then diff
diff /tmp/project_a.txt /tmp/project_b.txt
```

### 9.3 Re-run failed pipeline

The backend does NOT support re-running the same `project_id`. Submit a new request (without `project_id`) to get a fresh UUID:

```bash
curl -X POST http://localhost:8080/ftth/hld/run \
  -F "excel=@Main_DataSet.xlsx" \
  -F "roads=@berlin-roads.gpkg"
```

### 9.4 Check if PostGIS is connected

```bash
curl -s http://localhost:8080/health | python -c "
import sys, json
d = json.load(sys.stdin)
pg = d.get('postgis', {})
if pg.get('available'):
    print('PostGIS: OK')
else:
    print('PostGIS: NOT AVAILABLE')
"
```

---

> **Last updated:** 2026-07-14  
> **Questions?** Check `web/backend/main.py` and `web/backend/postgis.py` for the complete implementation.
