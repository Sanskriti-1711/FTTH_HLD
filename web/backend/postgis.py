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
    from psycopg2.extras import Json, RealDictCursor, execute_values
    from psycopg2 import sql
except ImportError:  # pragma: no cover - handled by API diagnostics
    psycopg2 = None  # type: ignore
    Json = None  # type: ignore
    RealDictCursor = None  # type: ignore
    execute_values = None  # type: ignore
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
    "mfg_service_areas": "mfg_service_areas",
    # Feeder/Distribution sub-layers share the canonical merged table so the
    # frontend sees one "cables"/"ducts" layer (distinguishable by STAGE).
    "feeder_cable": "cable_layer",
    "distribution_cable": "cable_layer",
    "feeder_ducts": "duct_layer",
    "distribution_ducts": "duct_layer",
    "drop_ducts": "duct_layer",
    "trenches": "trench_layer",
    "chambers": "chambers",
    "coupleurs": "coupleur_layer",
    "poles": "poles",
    "brownfield": "brownfield",
    # Designer structural nodes (HDD pits / junctions / PDPs / bends / pulls):
    # the evidence behind every planned chamber, in its own table so the
    # platform serves them next to the chambers they produced.
    "trench_nodes": "trench_nodes",
    # Overhead spans on poles — NOT an excavation. Renamed from
    # `aerial_drop_trench_layer` / `aerial_drop_trenches` because "trench" in
    # this project means "dug", and these carry EXCAVATION=0. The two old
    # spellings remain aliases so a stored project, a saved URL or a Django
    # FtthLayer row written before the rename still resolves to the same table.
    "aerial_spans": "aerial_span_layer",
    "aerial_drop_trenches": "aerial_span_layer",
    "aerial_trenches": "aerial_span_layer",
    "aerial_drop_trench_layer": "aerial_span_layer",
    "aerial_cable": "aerial_cable_layer",
    # Aerial legs CLASSIFIED by the trench stage (never excavated) — their own
    # table, because they are a design decision, not the aerial drop the
    # pole/aerial stage BUILDS (which lands in aerial_span_layer).
    "aerial_drops": "aerial_drops",
    # Occupancy registry (derived from the duct/cable layers each run).
    "duct_occupancy": "duct_occupancy",
    "cable_occupancy": "cable_occupancy",
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
    "chamber": "chambers",
    "coupler": "coupleur_layer",
    "couplers": "coupleur_layer",
    "coupleur": "coupleur_layer",
    "pole": "poles",
    "existing_infrastructure": "brownfield",
    "existing_infrastructure_points": "brownfield",
    "existing_infra": "brownfield",
    "existing_infra_points": "brownfield",
}

# Maps internal table name (no schema) -> public API name
TABLE_TO_PUBLIC_NAME = {
    "object_layer": "objects",
    "polygon_layer": "polygons",
    "pdps": "pdps",
    "mfg": "mfg",
    "mfg_service_areas": "mfg_service_areas",
    "cable_layer": "cables",
    "duct_layer": "ducts",
    "trench_layer": "trenches",
    "network_layer": "network",
    "chambers": "chambers",
    "coupleur_layer": "coupleurs",
    "poles": "poles",
    "trench_nodes": "trench_nodes",
    "brownfield": "brownfield",
    "aerial_span_layer": "aerial_spans",
    "aerial_cable_layer": "aerial_cable",
    # NOTE: this map is also the list init_schema() creates tables from, so a
    # table named only in LAYER_TABLES is never created — publishing
    # `aerial_drops` without it here raised
    # `relation "gis.aerial_drops" does not exist` and failed the whole
    # ingest of every run that carried the aerial layer.
    "aerial_drops": "aerial_drops",
    "duct_occupancy": "duct_occupancy",
    "cable_occupancy": "cable_occupancy",
}

_TABLES = tuple(TABLE_TO_PUBLIC_NAME.keys())

# Registry tables that are DATA, not design layers: stored for read-back
# (brownfield capacity for a re-run / the LLD) and deliberately never listed
# as public layers, so they stay off the results map and out of the downloads.
DB_ONLY_TABLES = frozenset({"duct_occupancy", "cable_occupancy"})

# Tables created before cables/ducts were unified (kept only for clearing
# stale rows on re-runs against an upgraded database).
LEGACY_TABLES = (
    "feeder_cable",
    "distribution_cable",
    "feeder_ducts",
    "distribution_ducts",
)

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
        # Ensure every column the engine writes exists, even when the table
        # was first created by Django migrations (which only know the Django
        # fields — runner/qgis_version/error/output_dir/downloads are engine
        # extras). Without this, upsert_project 500s with UndefinedColumn and
        # every new run leaves a queued orphan row behind.
        _engine_extras = [
            ("runner", "TEXT"),
            ("qgis_version", "TEXT"),
            ("error", "TEXT"),
            ("output_dir", "TEXT"),
            ("downloads", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
            ("pipeline_state", "JSONB"),
            ("progress", "INTEGER"),
            ("stage_name", "TEXT"),
            ("stage_index", "INTEGER"),
            ("stage_count", "INTEGER"),
        ]
        for col, ddl in _engine_extras:
            try:
                cur.execute(
                    sql.SQL("ALTER TABLE {projects} ADD COLUMN IF NOT EXISTS {col} {ddl}").format(
                        projects=_biz_ident("ftth_projects"),
                        col=sql.Identifier(col),
                        ddl=sql.SQL(ddl),
                    )
                )
            except Exception:
                pass  # Race-safe

        # Django declares these NOT NULL without a DB default; the engine's
        # upsert doesn't supply them, so a row insert would violate the
        # constraint. Give them the same defaults as the Django model fields
        # (Django always passes explicit values, so this never changes its
        # behaviour — it only lets the engine create rows in a shared table).
        _django_defaults = [
            ("name", "''"),
            ("status", "'queued'"),
            ("stage_name", "''"),
            ("stage_index", "0"),
            ("stage_count", "6"),
            ("progress", "0"),
            ("error_message", "''"),
            ("excel_filename", "''"),
            ("roads_filename", "''"),
        ]
        for col, default in _django_defaults:
            try:
                cur.execute(
                    sql.SQL(
                        "ALTER TABLE {projects} ALTER COLUMN {col} SET DEFAULT {default}"
                    ).format(
                        projects=_biz_ident("ftth_projects"),
                        col=sql.Identifier(col),
                        default=sql.SQL(default),
                    )
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

        # The aerial-span table was renamed `aerial_drop_trench_layer` ->
        # `aerial_span_layer`. Carry the rows across rather than orphaning every
        # already-published project on an empty new table.
        #
        # This runs AFTER the creation loop on purpose: the copy fallback below
        # writes into `aerial_span_layer`, so that table has to exist first. Run
        # before the loop it failed startup with `relation "gis.aerial_span_layer"
        # does not exist` on the first boot against a database that still had the
        # old name.
        cur.execute("SELECT to_regclass(%s)", (f"{GIS_SCHEMA}.aerial_drop_trench_layer",))
        if cur.fetchone()[0] is not None:
            try:
                cur.execute(
                    sql.SQL("ALTER TABLE {old} RENAME TO {new}").format(
                        old=sql.Identifier(GIS_SCHEMA, "aerial_drop_trench_layer"),
                        new=sql.Identifier(GIS_SCHEMA, "aerial_span_layer"),
                    )
                )
                for suffix in ("project", "geom"):
                    cur.execute(
                        sql.SQL("ALTER INDEX IF EXISTS {idx} RENAME TO {new_idx}").format(
                            idx=sql.Identifier(f"idx_aerial_drop_trench_layer_{suffix}"),
                            new_idx=sql.Identifier(f"idx_aerial_span_layer_{suffix}"),
                        )
                    )
            except Exception:
                # A view or a dependent object blocks RENAME. The rows still
                # have to be readable, so copy them across and leave the old
                # table for a human to drop. Best-effort: a failure here must
                # not stop the engine booting.
                try:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {new} (project_id, fid, geom, properties) "
                            "SELECT o.project_id, o.fid, o.geom, o.properties "
                            "FROM {old} o WHERE NOT EXISTS ("
                            "  SELECT 1 FROM {new} n "
                            "  WHERE n.project_id = o.project_id)"
                        ).format(
                            new=_gis_ident("aerial_span_layer"),
                            old=_gis_ident("aerial_drop_trench_layer"),
                        )
                    )
                except Exception:
                    pass


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
    progress: Optional[int] = None,
    stage_name: Optional[str] = None,
    stage_index: Optional[int] = None,
    stage_count: Optional[int] = None,
) -> None:
    """Insert/update a project row (shared with Django's ftth_projects).

    ``progress`` is persisted when supplied: the platform's project list and
    dashboard read this column (the engine's live in-memory progress only
    exists while it is serving the run), so a finished run must not sit at the
    column default of 0 %.

    ``stage_name`` / ``stage_index`` / ``stage_count`` are the same idea for the
    stage: the engine tracks it in memory while a run streams its output, and
    the row is what survives a restart and what the project list shows.  They go
    in a second statement, so a status-only upsert leaves the recorded stage be.
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {projects} (
                    project_id, status, roads_filename, runner, qgis_version,
                    error, output_dir, downloads, progress, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, 0), now(), now())
                ON CONFLICT (project_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    progress = COALESCE(%s, {projects}.progress),
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
                progress,
                progress,
            ),
        )
        if stage_name is not None or stage_index is not None or stage_count is not None:
            cur.execute(
                sql.SQL(
                    """
                    UPDATE {projects}
                    SET stage_name = COALESCE(%s, stage_name),
                        stage_index = COALESCE(%s, stage_index),
                        stage_count = COALESCE(%s, stage_count),
                        updated_at = now()
                    WHERE project_id = %s
                    """
                ).format(projects=_biz_ident("ftth_projects")),
                (stage_name, stage_index, stage_count, project_id),
            )


def update_project_downloads(
    project_id: str, downloads: List[Dict[str, Any]]
) -> None:
    """Persist the on-disk download manifest for a project."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {projects}
                SET downloads = %s, updated_at = now()
                WHERE project_id = %s
                """
            ).format(projects=_biz_ident("ftth_projects")),
            (Json(downloads), project_id),
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
        # Legacy tables may not exist on fresh databases — drop rows only if
        # the table is present (older databases from before the cable/duct
        # unification still hold rows there).
        for table in tables or (_TABLES + LEGACY_TABLES):
            cur.execute(
                sql.SQL(
                    "SELECT to_regclass(%s)"
                ),
                (f"{GIS_SCHEMA}.{table}",),
            )
            if cur.fetchone()[0] is None:
                continue
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
    sublayer: Optional[str] = None,
) -> int:
    """Insert a layer's features into its GIS table.

    ``sublayer`` tags every feature that does not already carry one. Grouped
    layers (ducts = feeder + distribution + drop, cables = feeder +
    distribution) share a single table, so without the tag the tier is lost on
    ingest and the results map cannot offer them as separate toggles.
    """
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
            if sublayer and not props.get("sublayer"):
                props = dict(props)
                props["sublayer"] = sublayer
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
        # ``executemany`` sends one statement per row: against a remote PostGIS
        # that is one network round-trip per feature (measured 36 ms/row — the
        # objects layer alone cost 49-58 s, and the whole ingest ~6.5 min).
        # ``execute_values`` folds a page of rows into a single multi-row INSERT
        # so the round-trips collapse: the same 1,359 rows went 58 s -> 1.0 s.
        query = sql.SQL(
            """
            INSERT INTO {table} (
                project_id, fid, geom, "POLYGON_ID", "PDP_ID", "MFG_ID",
                "SRC_ID", "STAGE", properties
            )
            VALUES %s
            """
        ).format(table=_gis_ident(table)).as_string(conn)
        template = (
            "(%s, %s,"
            " CASE"
            "  WHEN %s IS NULL THEN NULL"
            "  WHEN %s = 4326 THEN ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)"
            "  ELSE ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), %s), 4326)"
            " END,"
            " %s, %s, %s, %s, %s, %s)"
        )
        execute_values(
            cur,
            query,
            [
                (
                    project_id,
                    fid,
                    # geom appears twice per branch: the CASE tests it for NULL
                    # and then parses it, so the placeholder count is one more
                    # than the number of distinct values.
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
            template=template,
            page_size=500,
        )
    return len(rows)


def load_geojson_file(
    project_id: str,
    layer: str,
    file_path: str,
    *,
    replace: bool = True,
    sublayer: Optional[str] = None,
) -> int:
    with open(file_path, "r", encoding="utf-8") as f:
        return load_geojson(
            project_id, layer, json.load(f), replace=replace, sublayer=sublayer
        )


def store_occupancy(project_id: str, table: str, features: List[Dict[str, Any]]) -> int:
    """Load occupancy registry rows into ``gis.<table>`` (no map layer).

    The occupancy tables reuse the generic GIS table shape (project_id +
    geometry + JSONB properties). They are data for the next run / the LLD —
    brownfield capacity read-back — not design layers, so they are never
    returned by ``list_project_layers`` and never reach the results map or
    the downloads.
    """
    if table not in ("duct_occupancy", "cable_occupancy"):
        raise ValueError(f"unknown occupancy table: {table}")
    conn = get_conn()
    rows = []
    for fid, feature in enumerate(features):
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geom = feature.get("geometry")
        geom_json = json.dumps(geom) if geom else None
        rows.append(
            (
                project_id,
                fid,
                # geom appears twice: the CASE tests it for NULL and then
                # parses it, so the placeholder count is one more than the
                # number of distinct values.
                geom_json,
                geom_json,
                _pick_prop(props, "PDP_ID"),
                _pick_prop(props, "SRC_ID"),
                Json(props),
            )
        )
    if not rows:
        return 0
    with conn.cursor() as cur:
        # One registry per project per run: refresh, never append.
        cur.execute(
            sql.SQL("DELETE FROM {table} WHERE project_id = %s").format(
                table=_gis_ident(table)
            ),
            (project_id,),
        )
        # Batched for the same reason as load_geojson: one round-trip per
        # occupancy row against a remote PostGIS dominated the derivation.
        query = sql.SQL(
            """
            INSERT INTO {table} (
                project_id, fid, geom, "PDP_ID", "SRC_ID", properties
            )
            VALUES %s
            """
        ).format(table=_gis_ident(table)).as_string(conn)
        execute_values(
            cur,
            query,
            rows,
            template=("(%s, %s,"
                      " CASE WHEN %s IS NULL THEN NULL"
                      "  ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) END,"
                      " %s, %s, %s)"),
            page_size=500,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Layer querying
# ---------------------------------------------------------------------------


def get_layer_geojson(
    project_id: str, layer: str
) -> Optional[Dict[str, Any]]:
    requested_layer = (layer or "").strip().lower().replace("-", "_")
    table = normalize_layer_name(requested_layer)

    # Layer predicate.  The cable builder writes BOTH the feeder and the
    # distribution cable into one table and tells them apart with the
    # CABLE_TYPE property ("Feeder" / "Distribution"); the grouped aliases
    # (cables / cable / cable_layer) mean "every cable row".  The predicates
    # used to key off a STAGE property/column that the writer never fills, so
    # every cable request matched nothing and silently fell back to the stale
    # on-disk GeoJSON — which is why a re-ingest never reached the map.
    where = sql.SQL("WHERE project_id = %s")
    params: List[Any] = [project_id]
    if table == "cable_layer":
        cable_type = {
            "feeder_cable": "Feeder",
            "distribution_cable": "Distribution",
        }.get(requested_layer)
        if cable_type is not None:
            where += sql.SQL(" AND properties->>'CABLE_TYPE' = %s")
            params.append(cable_type)
        elif requested_layer not in ("cables", "cable", "cable_layer"):
            # Unrecognised cable sub-layer name — let the caller fall back to
            # the on-disk outputs rather than guessing at a type.
            return None
    elif table == "duct_layer":
        # Ducts share one table too but carry no feeder / distribution marker
        # in their properties, so only the grouped name can be resolved here;
        # the sub-layer names keep falling back to disk as before.
        if requested_layer not in ("ducts", "duct", "duct_layer"):
            return None
    # Every other table (trenches, chambers, pdps, ...) is one layer = one
    # table, so there is nothing to discriminate on: return all its rows.

    conn = get_conn()
    with conn.cursor() as cur:
        # Table may not exist on DBs created before a layer was added
        # (e.g. chambers/poles/brownfield) — treat it as "no data" so the
        # caller can fall back to on-disk outputs instead of 500ing.
        cur.execute(sql.SQL("SELECT to_regclass(%s)"), (f"{GIS_SCHEMA}.{table}",))
        if cur.fetchone()[0] is None:
            return None
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
                {where}
                """
            ).format(table=_gis_ident(table), where=where),
            tuple(params),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    # A table that exists but holds no rows for this project produces an
    # empty FeatureCollection here. Treat it as "no data" so callers fall
    # through to the on-disk outputs — returning an empty collection made
    # clients cache a permanently-empty layer mid-run.
    if not (row[0].get("features") or []):
        return None
    return row[0]


def list_project_layers(project_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    out: List[Dict[str, Any]] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for table, public_name in TABLE_TO_PUBLIC_NAME.items():
            if table in DB_ONLY_TABLES:
                continue
            # Skip tables that don't exist yet (added after the schema was
            # first created — e.g. chambers/poles/brownfield) so a stale DB
            # never 500s the results endpoint.
            cur.execute(sql.SQL("SELECT to_regclass(%s)"), (f"{GIS_SCHEMA}.{table}",))
            if cur.fetchone()["to_regclass"] is None:
                continue
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
        cur.execute(sql.SQL("SELECT to_regclass(%s)"), (f"{GIS_SCHEMA}.{table}",))
        if cur.fetchone()[0] is None:
            raise KeyError(f"Layer '{layer}' has no GIS table")
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


def delete_project(project_id: str) -> None:
    """
    Delete a project and all its associated data from PostGIS.

    Removes:
      - The project row from business.ftth_projects (CASCADE deletes
        spatial rows from all gis.* layer tables)
    """
    conn = get_conn()
    with conn.cursor() as cur:
        # Django-owned HLD layer metadata references ftth_projects without
        # ON DELETE CASCADE. Remove those rows before deleting the project;
        # spatial GIS rows are cleared separately by clear_project_layers().
        cur.execute(
            "DELETE FROM business.ftth_hld_layers WHERE ftth_project_id = %s",
            (project_id,),
        )
        cur.execute(
            sql.SQL("DELETE FROM {projects} WHERE project_id = %s").format(
                projects=_biz_ident("ftth_projects")
            ),
            (project_id,),
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
