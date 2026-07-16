"""PostGIS storage for FTTH HLD pipeline outputs.

The backend keeps one physical table per canonical FTTH layer so QGIS,
MapLibre tile generation, pgRouting, and downloads can address stable names:
object_layer, polygon_layer, network_layer, trench_layer, duct_layer,
cable_layer.

Schema layout (unified PostgreSQL with Django):
  gis schema      — PostGIS spatial layer tables
  business schema — project metadata, pipeline state (shared with Django)
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any, Dict, Iterable, List, Optional

try:
    import psycopg2
    from psycopg2.extras import Json, RealDictCursor
    from psycopg2 import sql
except ImportError:  # pragma: no cover - handled by API diagnostics
    psycopg2 = None  # type: ignore
    Json = None  # type: ignore
    RealDictCursor = None  # type: ignore
    sql = None  # type: ignore


# Schema names shared with Django (settings.py search_path=business,public)
GIS_SCHEMA = "gis"
BUSINESS_SCHEMA = "business"

CANONICAL_COLUMNS = ("POLYGON_ID", "PDP_ID", "MFG_ID", "SRC_ID", "STAGE")

# Maps public API layer names -> PostGIS physical table names (without schema).
# Individual sub-layers (pdps, mfg, feeder_cable, etc.) each get their own
# table so the frontend can address them independently by name.
LAYER_TABLES: Dict[str, str] = {
    # Canonical names (from ONECLICK_OUTPUTS)
    "objects": "object_layer",
    "polygons": "polygon_layer",
    "pdps": "pdps",
    "mfg": "mfg",
    "feeder_cable": "feeder_cable",
    "distribution_cable": "distribution_cable",
    "feeder_ducts": "feeder_ducts",
    "distribution_ducts": "distribution_ducts",
    "trenches": "trench_layer",
    # Backward-compatible aliases
    "object": "object_layer",
    "object_layer": "object_layer",
    "polygon": "polygon_layer",
    "polygon_layer": "polygon_layer",
    "network": "network_layer",
    "network_layer": "network_layer",
    "trench": "trench_layer",
    "trench_layer": "trench_layer",
    "ducts": "duct_layer",
    "duct": "duct_layer",
    "duct_layer": "duct_layer",
    "cables": "cable_layer",
    "cable": "cable_layer",
    "cable_layer": "cable_layer",
}

# Maps internal table name (no schema) -> public API name
TABLE_TO_PUBLIC_NAME = {
    "object_layer": "objects",
    "polygon_layer": "polygons",
    "pdps": "pdps",
    "mfg": "mfg",
    "feeder_cable": "feeder_cable",
    "distribution_cable": "distribution_cable",
    "feeder_ducts": "feeder_ducts",
    "distribution_ducts": "distribution_ducts",
    "trench_layer": "trenches",
    "network_layer": "network",
    "duct_layer": "ducts",
    "cable_layer": "cables",
}

_TABLES = tuple(TABLE_TO_PUBLIC_NAME.keys())
_tl = threading.local()


# ---------------------------------------------------------------------------
# Schema-qualified identifier helpers
# ---------------------------------------------------------------------------


def _gis_ident(table: str) -> sql.Composable:
    """Return a psycopg2 sql.Identifier qualified with the GIS schema."""
    return sql.Identifier(GIS_SCHEMA, table)


def _biz_ident(table: str) -> sql.Composable:
    """Return a psycopg2 sql.Identifier qualified with the business schema."""
    return sql.Identifier(BUSINESS_SCHEMA, table)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def normalize_layer_name(layer: str) -> str:
    key = (layer or "").strip().lower().replace("-", "_")
    table = LAYER_TABLES.get(key)
    if table is None:
        raise KeyError(f"Unknown FTTH layer '{layer}'")
    return table


def _conn_str() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    return " ".join(
        f"{k}={v}"
        for k, v in (
            ("host", os.environ.get("PGHOST", "localhost")),
            ("port", os.environ.get("PGPORT", "5432")),
            ("dbname", os.environ.get("PGDATABASE", "ftth")),
            ("user", os.environ.get("PGUSER", "ftth")),
            ("password", os.environ.get("PGPASSWORD", "ftth")),
            ("connect_timeout", os.environ.get("PGCONNECT_TIMEOUT", "2")),
        )
        if v
    )


def get_conn():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Install psycopg2-binary.")
    conn = getattr(_tl, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(_conn_str())
        conn.autocommit = True
        _tl.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_tl, "conn", None)
    if conn is not None and not conn.closed:
        conn.close()
    _tl.conn = None


def is_available() -> bool:
    if psycopg2 is None:
        return False
    try:
        with get_conn().cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        close_conn()
        return False


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def init_schema() -> None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        # Create schemas (idempotent)
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(
            schema=sql.Identifier(GIS_SCHEMA)
        ))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(
            schema=sql.Identifier(BUSINESS_SCHEMA)
        ))

        # Business schema: project metadata table
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {projects} (
                    project_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'queued',
                    runner TEXT,
                    qgis_version TEXT,
                    roads_filename TEXT,
                    error TEXT,
                    output_dir TEXT,
                    downloads JSONB NOT NULL DEFAULT '[]'::jsonb,
                    pipeline_state JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ).format(projects=_biz_ident("ftth_projects"))
        )
        # Add pipeline_state column if upgrading from older schema
        try:
            cur.execute(
                sql.SQL(
                    """
                    ALTER TABLE {projects}
                    ADD COLUMN IF NOT EXISTS pipeline_state JSONB
                    """
                ).format(projects=_biz_ident("ftth_projects"))
            )
        except Exception:
            pass  # Race-safe

        # GIS schema: spatial layer tables
        for table in _TABLES:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {table} (
                        id BIGSERIAL PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES {projects}(project_id) ON DELETE CASCADE,
                        fid INTEGER,
                        geom GEOMETRY(Geometry, 4326),
                        "POLYGON_ID" TEXT,
                        "PDP_ID" TEXT,
                        "MFG_ID" TEXT,
                        "SRC_ID" TEXT,
                        "STAGE" TEXT,
                        properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                ).format(
                    table=_gis_ident(table),
                    projects=_biz_ident("ftth_projects"),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {idx} ON {table} (project_id)"
                ).format(
                    idx=sql.Identifier(f"idx_{table}_project"),
                    table=_gis_ident(table),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {idx} ON {table} USING GIST (geom)"
                ).format(
                    idx=sql.Identifier(f"idx_{table}_geom"),
                    table=_gis_ident(table),
                )
            )


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def upsert_project(
    project_id: str,
    *,
    status: str,
    roads_filename: Optional[str] = None,
    runner: Optional[str] = None,
    qgis_version: Optional[str] = None,
    error: Optional[str] = None,
    output_dir: Optional[str] = None,
    downloads: Optional[List[Dict[str, Any]]] = None,
) -> None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {projects} (
                    project_id, status, roads_filename, runner, qgis_version,
                    error, output_dir, downloads, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (project_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    roads_filename = COALESCE(
                        EXCLUDED.roads_filename, {projects}.roads_filename
                    ),
                    runner = COALESCE(EXCLUDED.runner, {projects}.runner),
                    qgis_version = COALESCE(
                        EXCLUDED.qgis_version, {projects}.qgis_version
                    ),
                    error = EXCLUDED.error,
                    output_dir = COALESCE(
                        EXCLUDED.output_dir, {projects}.output_dir
                    ),
                    downloads = COALESCE(
                        EXCLUDED.downloads, {projects}.downloads
                    ),
                    updated_at = now()
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (
                project_id,
                status,
                roads_filename,
                runner,
                qgis_version,
                error,
                output_dir,
                Json(downloads or []),
            ),
        )


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {projects} WHERE project_id = %s").format(
                projects=_biz_ident("ftth_projects")
            ),
            (project_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_projects(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT * FROM {projects}
                ORDER BY created_at DESC
                LIMIT %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def clear_project_layers(
    project_id: str, tables: Optional[Iterable[str]] = None
) -> None:
    conn = get_conn()
    with conn.cursor() as cur:
        for table in tables or _TABLES:
            cur.execute(
                sql.SQL("DELETE FROM {table} WHERE project_id = %s").format(
                    table=_gis_ident(table)
                ),
                (project_id,),
            )


# ---------------------------------------------------------------------------
# GeoJSON ingestion
# ---------------------------------------------------------------------------


def _pick_prop(props: Dict[str, Any], name: str) -> Optional[str]:
    for key, value in props.items():
        if key.upper() == name:
            return None if value is None else str(value)
    return None


def _first_coordinate(value: Any) -> Optional[List[float]]:
    if not isinstance(value, list) or not value:
        return None
    if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
        return [float(value[0]), float(value[1])]
    for item in value:
        found = _first_coordinate(item)
        if found is not None:
            return found
    return None


def _guess_source_srid(geojson: Dict[str, Any]) -> int:
    for feature in geojson.get("features") or []:
        geom = feature.get("geometry") if isinstance(feature, dict) else None
        coords = _first_coordinate((geom or {}).get("coordinates"))
        if coords is None:
            continue
        x, y = coords
        if -180 <= x <= 180 and -90 <= y <= 90:
            return 4326
        return 25833
    return 4326


def load_geojson(
    project_id: str,
    layer: str,
    geojson: Dict[str, Any],
    *,
    replace: bool = True,
) -> int:
    table = normalize_layer_name(layer)
    features = geojson.get("features") or []
    source_srid = _guess_source_srid(geojson)
    conn = get_conn()
    with conn.cursor() as cur:
        if replace:
            cur.execute(
                sql.SQL("DELETE FROM {table} WHERE project_id = %s").format(
                    table=_gis_ident(table)
                ),
                (project_id,),
            )
        rows = []
        for fid, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") or {}
            geom = feature.get("geometry")
            rows.append(
                (
                    project_id,
                    fid,
                    json.dumps(geom) if geom else None,
                    _pick_prop(props, "POLYGON_ID"),
                    _pick_prop(props, "PDP_ID"),
                    _pick_prop(props, "MFG_ID"),
                    _pick_prop(props, "SRC_ID"),
                    _pick_prop(props, "STAGE"),
                    Json(props),
                )
            )
        if not rows:
            return 0
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {table} (
                    project_id, fid, geom, "POLYGON_ID", "PDP_ID", "MFG_ID",
                    "SRC_ID", "STAGE", properties
                )
                VALUES (
                    %s, %s,
                    CASE
                        WHEN %s IS NULL THEN NULL
                        WHEN %s = 4326 THEN ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
                        ELSE ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), %s), 4326)
                    END,
                    %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=_gis_ident(table)),
            [
                (
                    project_id,
                    fid,
                    geom,
                    source_srid,
                    geom,
                    geom,
                    source_srid,
                    polygon_id,
                    pdp_id,
                    mfg_id,
                    src_id,
                    stage,
                    props,
                )
                for (
                    project_id,
                    fid,
                    geom,
                    polygon_id,
                    pdp_id,
                    mfg_id,
                    src_id,
                    stage,
                    props,
                ) in rows
            ],
        )
    return len(rows)


def load_geojson_file(
    project_id: str,
    layer: str,
    file_path: str,
    *,
    replace: bool = True,
) -> int:
    with open(file_path, "r", encoding="utf-8") as f:
        return load_geojson(project_id, layer, json.load(f), replace=replace)


# ---------------------------------------------------------------------------
# Layer querying
# ---------------------------------------------------------------------------


def get_layer_geojson(
    project_id: str, layer: str
) -> Optional[Dict[str, Any]]:
    table = normalize_layer_name(layer)
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT jsonb_build_object(
                    'type', 'FeatureCollection',
                    'features', COALESCE(jsonb_agg(jsonb_build_object(
                        'type', 'Feature',
                        'id', fid,
                        'geometry', CASE WHEN geom IS NULL THEN NULL ELSE ST_AsGeoJSON(geom)::jsonb END,
                        'properties', properties
                    ) ORDER BY fid), '[]'::jsonb)
                )
                FROM {table}
                WHERE project_id = %s
                """
            ).format(table=_gis_ident(table)),
            (project_id,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return row[0]


def list_project_layers(project_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    out: List[Dict[str, Any]] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for table, public_name in TABLE_TO_PUBLIC_NAME.items():
            cur.execute(
                sql.SQL(
                    """
                    SELECT COUNT(*) AS feature_count,
                           COALESCE(GeometryType(ST_Collect(geom)), 'NONE') AS geometry_type
                    FROM {table}
                    WHERE project_id = %s
                    """
                ).format(table=_gis_ident(table)),
                (project_id,),
            )
            row = dict(cur.fetchone() or {})
            if int(row.get("feature_count") or 0) > 0:
                out.append(
                    {
                        "name": public_name,
                        "table": f"{GIS_SCHEMA}.{table}",
                        "feature_count": int(row["feature_count"]),
                        "geometry_type": row.get("geometry_type"),
                    }
                )
    return out


def get_vector_tile(
    project_id: str, layer: str, z: int, x: int, y: int
) -> bytes:
    table = normalize_layer_name(layer)
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                WITH bounds AS (
                    SELECT ST_TileEnvelope(%s, %s, %s) AS geom
                ),
                mvtgeom AS (
                    SELECT
                        id,
                        fid,
                        "POLYGON_ID",
                        "PDP_ID",
                        "MFG_ID",
                        "SRC_ID",
                        "STAGE",
                        properties,
                        ST_AsMVTGeom(
                            ST_Transform(t.geom, 3857),
                            bounds.geom,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM {table} t, bounds
                    WHERE t.project_id = %s
                      AND t.geom IS NOT NULL
                      AND ST_Transform(t.geom, 3857) && bounds.geom
                )
                SELECT ST_AsMVT(mvtgeom, %s, 4096, 'geom') FROM mvtgeom
                """
            ).format(table=_gis_ident(table)),
            (z, x, y, project_id, TABLE_TO_PUBLIC_NAME[table]),
        )
        row = cur.fetchone()
    return bytes(row[0]) if row and row[0] is not None else b""


# ---------------------------------------------------------------------------
# Pipeline state (step-by-step progress persistence for resume capability)
# ---------------------------------------------------------------------------


def _make_pipeline_state() -> Dict[str, Any]:
    """Return the default pipeline_state for a fresh project."""
    return {
        "steps": {
            step: {
                "status": "pending",
                "outputs": {},
                "params": {},
                "error": None,
                "started_at": None,
                "completed_at": None,
                "progress": 0,
            }
            for step in ("object", "polygon", "network", "trench", "cable", "duct")
        },
        "inputs": {
            "excel_filename": None,
            "roads_filename": None,
            "excel_uploaded": False,
            "roads_uploaded": False,
        },
    }


def init_pipeline_state(
    project_id: str,
    excel_filename: Optional[str] = None,
    roads_filename: Optional[str] = None,
) -> None:
    """Set the initial pipeline_state for a new project."""
    state = _make_pipeline_state()
    if excel_filename:
        state["inputs"]["excel_filename"] = excel_filename
        state["inputs"]["excel_uploaded"] = True
    if roads_filename:
        state["inputs"]["roads_filename"] = roads_filename
        state["inputs"]["roads_uploaded"] = True
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {projects}
                SET pipeline_state = %s::jsonb, updated_at = now()
                WHERE project_id = %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (Json(state), project_id),
        )


def get_pipeline_state(project_id: str) -> Optional[Dict[str, Any]]:
    """Get the current pipeline_state for a project, or None if not set."""
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT pipeline_state FROM {projects} WHERE project_id = %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or row.get("pipeline_state") is None:
        return None
    return row["pipeline_state"]


def update_step_progress(
    project_id: str,
    step: str,
    status: str,
    *,
    progress: Optional[int] = None,
    error: Optional[str] = None,
    outputs: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """Update a single step's state within the pipeline_state JSONB column.

    Uses PostGIS JSONB merge so concurrent partial updates don't clobber.
    """
    valid_steps = {"object", "polygon", "network", "trench", "cable", "duct"}
    if step not in valid_steps:
        raise ValueError(f"Unknown pipeline step '{step}'. Valid: {valid_steps}")

    # Build the partial update JSON
    patch: Dict[str, Any] = {
        "status": status,
    }
    if progress is not None:
        patch["progress"] = progress
    if error is not None:
        patch["error"] = error
    if outputs is not None:
        patch["outputs"] = outputs
    if params is not None:
        patch["params"] = params

    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
    if status == "running":
        patch["started_at"] = now_ts
    if status in ("completed", "failed"):
        patch["completed_at"] = now_ts

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {projects}
                SET pipeline_state = jsonb_set(
                    COALESCE(pipeline_state, '{{}}'::jsonb),
                    %s,
                    %s::jsonb,
                    true
                ),
                status = CASE
                    WHEN %s IN ('completed', 'failed')
                     AND %s IN ('completed', 'failed')
                    THEN %s  -- propagate terminal status to top-level
                    ELSE {projects}.status
                END,
                updated_at = now()
                WHERE project_id = %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (
                "{steps," + step + "}",  # JSONB path: {steps,object}
                Json(patch),
                status,
                status,
                status,
                project_id,
            ),
        )


def update_inputs_state(
    project_id: str,
    *,
    excel_filename: Optional[str] = None,
    roads_filename: Optional[str] = None,
) -> None:
    """Update the inputs section of pipeline_state."""
    patch: Dict[str, Any] = {}
    if excel_filename is not None:
        patch["excel_filename"] = excel_filename
        patch["excel_uploaded"] = True
    if roads_filename is not None:
        patch["roads_filename"] = roads_filename
        patch["roads_uploaded"] = True

    if not patch:
        return

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {projects}
                SET pipeline_state = jsonb_set(
                    COALESCE(pipeline_state, '{{}}'::jsonb),
                    '{{inputs}}',
                    (pipeline_state->'inputs') || %s::jsonb,
                    true
                ),
                updated_at = now()
                WHERE project_id = %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (Json(patch), project_id),
        )


def db_info() -> Dict[str, Any]:
    if not is_available():
        return {"available": False}
    try:
        with get_conn().cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT postgis_full_version() AS version")
            row = cur.fetchone()
        return {
            "available": True,
            "postgis_version": row["version"] if row else None,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}
