"""Survey database layer — shares the existing PostGIS instance.

All survey tables are created in the `business` schema alongside the FTTH
pipeline tables. The connection management mirrors web/backend/postgis.py."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import psycopg2
    from psycopg2.extras import Json, RealDictCursor
except ImportError:
    psycopg2 = None  # type: ignore
    Json = None  # type: ignore
    RealDictCursor = None  # type: ignore


BUSINESS_SCHEMA = "business"


# ── Connection management ──


def _conn_str() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    # Fall back to environment variables (same defaults as FTTH engine)
    parts = [
        f"host={os.environ.get('PGHOST', 'localhost')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"dbname={os.environ.get('PGDATABASE', 'ftth')}",
        f"user={os.environ.get('PGUSER', 'ftth')}",
        f"password={os.environ.get('PGPASSWORD', 'ftth')}",
    ]
    return " ".join(parts)


def get_conn():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")
    import threading
    _tl = threading.local()
    conn = getattr(_tl, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(_conn_str())
        conn.autocommit = True
        _tl.conn = conn
    return conn


def is_available() -> bool:
    if psycopg2 is None:
        return False
    try:
        with get_conn().cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


# ── Schema initialisation ──


def init_survey_schema() -> None:
    """Create all survey-related tables if they don't exist."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {BUSINESS_SCHEMA}")

        # Survey projects — extends FTTH project metadata with survey-specific fields
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.survey_projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                region TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','ACTIVE','ASSIGNED','ACCEPTED',
                                      'IN_PROGRESS','SURVEY_COMPLETE','COMPLIANCE_DONE','CLOSED')),
                assigned_to TEXT REFERENCES {BUSINESS_SCHEMA}.users(id),
                assigned_user_name TEXT,
                survey_package_name TEXT,
                total_assets INTEGER DEFAULT 0,
                completion INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        # Survey entries — per-feature survey data synced from mobile
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.survey_entries (
                id TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.survey_projects(project_id) ON DELETE CASCADE,
                layer_name TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started','in_progress','complete','flagged')),
                form_data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                notes TEXT DEFAULT '',
                surveyed_by TEXT,
                surveyed_at TIMESTAMPTZ,
                gps_lat DOUBLE PRECISION,
                gps_lng DOUBLE PRECISION,
                gps_accuracy DOUBLE PRECISION,
                photo_count INTEGER DEFAULT 0,
                measurement_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (id, project_id)
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_survey_entries_project
            ON {BUSINESS_SCHEMA}.survey_entries (project_id)
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_survey_entries_status
            ON {BUSINESS_SCHEMA}.survey_entries (project_id, status)
        """)

        # Survey photos — uploaded image records
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.survey_photos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.survey_projects(project_id) ON DELETE CASCADE,
                feature_id TEXT,
                layer_name TEXT,
                category TEXT DEFAULT 'Evidence',
                file_path TEXT,
                storage_key TEXT,
                original_filename TEXT,
                latitude DOUBLE PRECISION DEFAULT 0,
                longitude DOUBLE PRECISION DEFAULT 0,
                altitude DOUBLE PRECISION,
                accuracy DOUBLE PRECISION,
                heading DOUBLE PRECISION,
                captured_at TIMESTAMPTZ,
                uploaded BOOLEAN DEFAULT FALSE,
                uploaded_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_survey_photos_project
            ON {BUSINESS_SCHEMA}.survey_photos (project_id)
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_survey_photos_feature
            ON {BUSINESS_SCHEMA}.survey_photos (feature_id)
        """)

        # Team members
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.team_members (
                project_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.survey_projects(project_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.users(id) ON DELETE CASCADE,
                role TEXT DEFAULT 'viewer',
                joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (project_id, user_id)
            )
        """)

        # Activities
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.activities (
                id BIGSERIAL PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.survey_projects(project_id) ON DELETE CASCADE,
                user_id TEXT REFERENCES {BUSINESS_SCHEMA}.users(id),
                user_name TEXT DEFAULT '',
                action TEXT NOT NULL,
                description TEXT DEFAULT '',
                metadata JSONB DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_activities_project
            ON {BUSINESS_SCHEMA}.activities (project_id, created_at DESC)
        """)

        # Messages
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.messages (
                id BIGSERIAL PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES {BUSINESS_SCHEMA}.survey_projects(project_id) ON DELETE CASCADE,
                user_id TEXT REFERENCES {BUSINESS_SCHEMA}.users(id),
                user_name TEXT DEFAULT '',
                content TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_messages_project
            ON {BUSINESS_SCHEMA}.messages (project_id, created_at DESC)
        """)

        # Fused measurements
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.measurements (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                feature_id TEXT,
                label TEXT,
                fused_value DOUBLE PRECISION,
                unit TEXT DEFAULT 'm',
                confidence DOUBLE PRECISION,
                status TEXT DEFAULT 'pending',
                sources_json JSONB DEFAULT '[]'::jsonb,
                device_fingerprint TEXT,
                photo_uri TEXT,
                timestamp TIMESTAMPTZ,
                synced BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Add updated_at column if upgrading from older schema
        cur.execute(f"""
            ALTER TABLE {BUSINESS_SCHEMA}.measurements
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """)

        # Audit records
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BUSINESS_SCHEMA}.audit_records (
                id BIGSERIAL PRIMARY KEY,
                measurement_id TEXT,
                photo_hash TEXT,
                depth_map_hash TEXT,
                measurement_hash TEXT,
                certificate_hash TEXT,
                device_fingerprint TEXT,
                sources_json JSONB DEFAULT '[]'::jsonb,
                timestamp TIMESTAMPTZ,
                uploaded BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


# ── Survey Projects ──


def list_survey_projects(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT sp.*, u.name as assigned_user_name
            FROM {BUSINESS_SCHEMA}.survey_projects sp
            LEFT JOIN {BUSINESS_SCHEMA}.users u ON u.id = sp.assigned_to
            ORDER BY sp.updated_at DESC
            LIMIT %s
        """, (limit,))
        return [_row_to_project(dict(r)) for r in cur.fetchall()]


def list_assigned_projects(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT sp.*, u.name as assigned_user_name
            FROM {BUSINESS_SCHEMA}.survey_projects sp
            LEFT JOIN {BUSINESS_SCHEMA}.users u ON u.id = sp.assigned_to
            WHERE sp.assigned_to = %s
            ORDER BY sp.updated_at DESC
            LIMIT %s
        """, (user_id, limit))
        return [_row_to_project(dict(r)) for r in cur.fetchall()]


def get_survey_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT sp.*, u.name as assigned_user_name
            FROM {BUSINESS_SCHEMA}.survey_projects sp
            LEFT JOIN {BUSINESS_SCHEMA}.users u ON u.id = sp.assigned_to
            WHERE sp.project_id = %s
        """, (project_id,))
        row = cur.fetchone()
    return _row_to_project(dict(row)) if row else None


def upsert_survey_project(project_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_conn()
    fields = []
    values = []
    for key in ("name", "region", "status", "assigned_to", "survey_package_name",
                 "total_assets", "completion"):
        if key in data:
            fields.append(key)
            values.append(data[key])

    if not fields:
        return get_survey_project(project_id) or {}

    set_clause = ", ".join(f"{f} = %s" for f in fields)
    set_clause += ", updated_at = now()"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Upsert: create stub if not exists, then update fields
        cur.execute(f"""
            INSERT INTO {BUSINESS_SCHEMA}.survey_projects (project_id, name, status)
            VALUES (%s, %s, 'DRAFT')
            ON CONFLICT (project_id) DO NOTHING
        """, (project_id, data.get("name", "")))

        cur.execute(f"""
            UPDATE {BUSINESS_SCHEMA}.survey_projects
            SET {set_clause}
            WHERE project_id = %s
        """, (*values, project_id))

    return get_survey_project(project_id) or {}


def accept_project(project_id: str, user_id: str, user_name: str) -> Dict[str, Any]:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {BUSINESS_SCHEMA}.survey_projects
            SET status = 'ACCEPTED', assigned_to = %s, assigned_user_name = %s,
                updated_at = now()
            WHERE project_id = %s
        """, (user_id, user_name, project_id))
    return get_survey_project(project_id) or {}


def update_project_status(project_id: str, status: str) -> Dict[str, Any]:
    VALID_STATUSES = ("DRAFT", "ACTIVE", "ASSIGNED", "ACCEPTED", "IN_PROGRESS",
                      "SURVEY_COMPLETE", "COMPLIANCE_DONE", "CLOSED")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {BUSINESS_SCHEMA}.survey_projects
            SET status = %s, updated_at = now()
            WHERE project_id = %s
        """, (status, project_id))
    return get_survey_project(project_id) or {}


# ── Survey Entries ──


def upsert_survey_entries(project_id: str, features: List[Dict[str, Any]]) -> int:
    """Batch upsert survey entry data. Returns number of rows affected."""
    conn = get_conn()
    count = 0
    with conn.cursor() as cur:
        for f in features:
            cur.execute(f"""
                INSERT INTO {BUSINESS_SCHEMA}.survey_entries
                    (id, project_id, layer_name, display_name, status, form_data,
                     notes, surveyed_by, surveyed_at, gps_lat, gps_lng, gps_accuracy,
                     photo_count, measurement_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id, project_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    form_data = EXCLUDED.form_data,
                    notes = EXCLUDED.notes,
                    surveyed_by = EXCLUDED.surveyed_by,
                    surveyed_at = EXCLUDED.surveyed_at,
                    gps_lat = EXCLUDED.gps_lat,
                    gps_lng = EXCLUDED.gps_lng,
                    gps_accuracy = EXCLUDED.gps_accuracy,
                    photo_count = GREATEST({BUSINESS_SCHEMA}.survey_entries.photo_count, EXCLUDED.photo_count),
                    measurement_count = GREATEST({BUSINESS_SCHEMA}.survey_entries.measurement_count, EXCLUDED.measurement_count),
                    updated_at = now()
            """, (
                f["feature_id"],
                project_id,
                f.get("layer_name", ""),
                f.get("display_name", ""),
                f.get("status", "not_started"),
                Json(f.get("form_data", {})),
                f.get("notes"),
                f.get("surveyed_by"),
                f.get("surveyed_at"),
                f.get("gps_lat"),
                f.get("gps_lng"),
                f.get("gps_accuracy"),
                f.get("photo_count", 0),
                f.get("measurement_count", 0),
            ))
            count += cur.rowcount
    return count


def get_survey_entries(project_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT * FROM {BUSINESS_SCHEMA}.survey_entries
            WHERE project_id = %s
            ORDER BY layer_name, id
        """, (project_id,))
        return _rows_to_dicts(cur.fetchall())


# ── Survey Photos ──


def save_photo(photo_data: Dict[str, Any]) -> str:
    """Insert a photo record. Returns the photo id."""
    conn = get_conn()
    photo_id = photo_data.get("id", f"photo_{uuid4_hex()}")
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {BUSINESS_SCHEMA}.survey_photos
                (id, project_id, feature_id, layer_name, category, file_path,
                 storage_key, original_filename, latitude, longitude, altitude,
                 accuracy, heading, captured_at, uploaded)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                uploaded = TRUE,
                uploaded_at = now()
        """, (
            photo_id,
            photo_data.get("project_id", ""),
            photo_data.get("feature_id"),
            photo_data.get("layer_name"),
            photo_data.get("category", "Evidence"),
            photo_data.get("file_path"),
            photo_data.get("storage_key"),
            photo_data.get("original_filename"),
            photo_data.get("latitude", 0),
            photo_data.get("longitude", 0),
            photo_data.get("altitude"),
            photo_data.get("accuracy"),
            photo_data.get("heading"),
            photo_data.get("captured_at"),
            photo_data.get("uploaded", False),
        ))
    return photo_id


def get_photos_for_project(project_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT * FROM {BUSINESS_SCHEMA}.survey_photos
            WHERE project_id = %s
            ORDER BY created_at DESC
        """, (project_id,))
        return _rows_to_dicts(cur.fetchall())


# ── Team ──


def get_team_members(project_id: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT tm.*, u.name, u.email
            FROM {BUSINESS_SCHEMA}.team_members tm
            JOIN {BUSINESS_SCHEMA}.users u ON u.id = tm.user_id
            WHERE tm.project_id = %s
            ORDER BY tm.joined_at
        """, (project_id,))
        return _rows_to_dicts(cur.fetchall())


# ── Activities ──


def log_activity(project_id: str, user_id: str, user_name: str,
                 action: str, description: str = "",
                 metadata: Optional[Dict] = None) -> int:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {BUSINESS_SCHEMA}.activities
                (project_id, user_id, user_name, action, description, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (project_id, user_id, user_name, action, description, Json(metadata or {})))
        return cur.fetchone()[0]


def get_activities(project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT * FROM {BUSINESS_SCHEMA}.activities
            WHERE project_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (project_id, limit))
        return _rows_to_dicts(cur.fetchall())


# ── Messages ──


def send_message(project_id: str, user_id: str, user_name: str,
                 content: str, message_type: str = "text") -> int:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {BUSINESS_SCHEMA}.messages
                (project_id, user_id, user_name, content, message_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (project_id, user_id, user_name, content, message_type))
        return cur.fetchone()[0]


def get_messages(project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            SELECT * FROM {BUSINESS_SCHEMA}.messages
            WHERE project_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (project_id, limit))
        return _rows_to_dicts(cur.fetchall())


# ── Measurements ──


def upsert_measurements(measurements: List[Dict[str, Any]]) -> int:
    conn = get_conn()
    count = 0
    with conn.cursor() as cur:
        for m in measurements:
            mid = m.get("id", f"meas_{uuid4_hex()}")
            cur.execute(f"""
                INSERT INTO {BUSINESS_SCHEMA}.measurements
                    (id, project_id, feature_id, label, fused_value, unit,
                     confidence, status, sources_json, device_fingerprint,
                     photo_uri, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    fused_value = EXCLUDED.fused_value,
                    status = EXCLUDED.status,
                    synced = TRUE,
                    updated_at = now()
            """, (
                mid,
                m.get("project_id"),
                m.get("feature_id"),
                m.get("label"),
                m.get("fused_value") or m.get("final_value"),
                m.get("unit", "m"),
                m.get("confidence"),
                m.get("status", "pending"),
                Json(m.get("sources_json", [])),
                m.get("device_fingerprint"),
                m.get("photo_uri"),
                m.get("timestamp"),
            ))
            count += cur.rowcount
    return count


# ── Audit Records ──


def save_audit_record(record: Dict[str, Any]) -> int:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {BUSINESS_SCHEMA}.audit_records
                (measurement_id, photo_hash, depth_map_hash, measurement_hash,
                 certificate_hash, device_fingerprint, sources_json, timestamp,
                 uploaded)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
        """, (
            record.get("measurement_id"),
            record.get("photo_hash"),
            record.get("depth_map_hash"),
            record.get("measurement_hash"),
            record.get("certificate_hash"),
            record.get("device_fingerprint"),
            Json(record.get("sources_json", [])),
            record.get("timestamp"),
        ))
        return cur.fetchone()[0]


# ── Helpers ──


def _to_str(value: Any) -> str:
    """Convert a value to string, handling datetime objects."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_project(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("project_id", ""),
        "name": row.get("name", ""),
        "region": row.get("region", ""),
        "status": row.get("status", "DRAFT"),
        "assigned_to": row.get("assigned_to"),
        "assigned_user_name": row.get("assigned_user_name"),
        "survey_package_name": row.get("survey_package_name"),
        "completion": row.get("completion", 0),
        "total_assets": row.get("total_assets", 0),
        "lastSync": _to_str(row.get("updated_at")),
        "createdAt": _to_str(row.get("created_at")),
    }


def _rows_to_dicts(rows: List[Any]) -> List[Dict[str, Any]]:
    """Convert RealDictCursor rows to plain dicts with string datetimes."""
    result = []
    for row in rows:
        d = dict(row)
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        result.append(d)
    return result


def uuid4_hex() -> str:
    import uuid
    return uuid.uuid4().hex
