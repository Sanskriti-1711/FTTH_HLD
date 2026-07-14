# FTTH Engine Backend

This folder is now backend-only. The old React/Vite and vanilla frontend code
has been removed.

## Runtime Shape

```
MapLibre/static client
        |
        v
FTTH Engine API (FastAPI + QGIS + HLDPlanning plugin)
        |
        v
PostGIS tables:
object_layer, polygon_layer, network_layer, trench_layer, duct_layer, cable_layer
```

The API runs the plugin orchestrator:

```
hldplanning:end_to_end_pipeline
```

which executes:

```
Object -> Polygon -> Network -> Trench -> Duct -> Cable
```

## Install

```bash
cd web/backend
pip install -r requirements.txt
```

Set PostGIS connection variables:

```bash
set DATABASE_URL=postgresql://ftth:ftth@localhost:5432/ftth
```

Set QGIS if it is not already on `PATH`:

```bash
set QGIS_EXECUTABLE=C:\Program Files\QGIS 3.44.6\bin\qgis_process-qgis.bat
```

Start:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Docker

From the repository root:

```bash
docker compose up --build
```

This starts:

| Service | Port | Purpose |
|---|---:|---|
| `ftth-engine` | `8000` | FastAPI + QGIS + HLDPlanning plugin |
| `postgis` | `5432` | Spatial storage for all FTTH layers |

The backend image uses `web/backend/Dockerfile`. Pin the QGIS base image there
for production builds if you need deterministic QGIS versions.

## API

### Health

```http
GET /health
```

Returns service status, QGIS executable detection, PostGIS status, and endpoint
list.

### Start HLD Run

```http
POST /ftth/hld/run
Content-Type: multipart/form-data
```

Fields:

| Field | Type | Required | Notes |
|---|---|---:|---|
| `excel` | file | yes | Address list `.xlsx` |
| `roads` | file | yes | Roads layer, usually `.gpkg` |
| `project_id` | form string | no | If omitted, the API creates one |
| `poly_method` | form integer | no | Defaults to `0` convex hull. Use `3` only when the seeded-growth/Voronoi path is fixed. |

Response is `202 Accepted`:

```json
{
  "project_id": "9e2d...",
  "status": "queued",
  "results_url": "/ftth/hld/results/9e2d...",
  "tile_url_template": "/tiles/{layer}/{z}/{x}/{y}.pbf?project_id=9e2d..."
}
```

### Get Results

```http
GET /ftth/hld/results/{project_id}
```

Returns run status, progress, log messages, available layers, downloads, and
tile URL template.

### Get Layer GeoJSON

```http
GET /ftth/hld/results/{project_id}/layers/{layer}
```

Supported layer names:

```text
objects
polygons
network
trenches
ducts
cables
```

Aliases such as `object_layer`, `polygon_layer`, and `trench_layer` also work.

### Vector Tiles

```http
GET /tiles/{layer}/{z}/{x}/{y}.pbf?project_id={project_id}
```

Returns Mapbox Vector Tile bytes from PostGIS using `ST_AsMVT`.

MapLibre source example:

```js
{
  "trenches": {
    "type": "vector",
    "tiles": [
      "http://localhost:8000/tiles/trenches/{z}/{x}/{y}.pbf?project_id=PROJECT_ID"
    ]
  }
}
```

### Downloads

```http
GET /ftth/hld/download/{project_id}/{file_path}
```

Streams plugin-generated outputs such as `.gpkg`, `.xlsx`, `.csv`, `.geojson`,
and logs from the project output directory.

### Projects

```http
GET /ftth/projects
```

Lists recent PostGIS-backed HLD projects.

## Legacy Compatibility Routes

These remain temporarily for older clients:

```http
POST /run-hld
GET  /status/{project_id}
GET  /layers/{project_id}/{layer}
```
