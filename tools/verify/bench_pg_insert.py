"""Is the PostGIS ingest slow because each row commits on its own?

Loads the same feature rows three ways into a scratch table and times them:

  A. autocommit=True  + executemany   (current engine path)
  B. autocommit=False + executemany   + one commit
  C. autocommit=False + execute_values + one commit

Run clean:  unset PYTHONPATH PYTHONHOME; python tmp/bench_pg_insert.py <geojson>
"""
import json
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "web" / "backend"
sys.path.insert(0, str(BACKEND))

import postgis  # noqa: E402
from psycopg2.extras import Json, execute_values  # noqa: E402

geojson = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
features = geojson.get("features") or []
source_srid = postgis._guess_source_srid(geojson)
print(f"features={len(features)} source_srid={source_srid}")

rows = []
for fid, feature in enumerate(features):
    if not isinstance(feature, dict):
        continue
    props = feature.get("properties") or {}
    geom = feature.get("geometry")
    rows.append((0, fid, json.dumps(geom) if geom else None,
                 postgis._pick_prop(props, "POLYGON_ID"),
                 postgis._pick_prop(props, "PDP_ID"),
                 postgis._pick_prop(props, "MFG_ID"),
                 postgis._pick_prop(props, "SRC_ID"),
                 postgis._pick_prop(props, "STAGE"),
                 Json(props)))

INSERT = """
    INSERT INTO {table} (project_id, fid, geom, "POLYGON_ID", "PDP_ID", "MFG_ID",
                         "SRC_ID", "STAGE", properties)
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


def values_template(row):
    project_id, fid, geom, poly, pdp, mfg, src, stage, props = row
    return (project_id, fid, geom, source_srid, geom, geom, source_srid,
            poly, pdp, mfg, src, stage, props)


def run(label, mode):
    conn = postgis.get_conn()
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_pg_insert")
        cur.execute(
            "CREATE TABLE bench_pg_insert (project_id text, fid int, geom geometry,"
            ' "POLYGON_ID" text, "PDP_ID" text, "MFG_ID" text, "SRC_ID" text,'
            ' "STAGE" text, properties jsonb)')
    old_autocommit = conn.autocommit
    t = time.perf_counter()
    if mode == "A":
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.executemany(INSERT.format(table="bench_pg_insert"),
                            [values_template(r) for r in rows])
    elif mode == "B":
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.executemany(INSERT.format(table="bench_pg_insert"),
                            [values_template(r) for r in rows])
        conn.commit()
    else:
        conn.autocommit = False
        cols = ('INSERT INTO bench_pg_insert (project_id, fid, geom, "POLYGON_ID",'
                ' "PDP_ID", "MFG_ID", "SRC_ID", "STAGE", properties) VALUES %s')
        template = ("(%s, %s, CASE WHEN %s IS NULL THEN NULL WHEN %s = 4326 THEN"
                    " ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) ELSE"
                    " ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), %s), 4326) END,"
                    " %s, %s, %s, %s, %s, %s)")
        with conn.cursor() as cur:
            execute_values(cur, cols, [values_template(r) for r in rows],
                           template=template, page_size=500)
        conn.commit()
    d = time.perf_counter() - t
    conn.autocommit = old_autocommit
    with conn.cursor() as cur:
        cur.execute("DROP TABLE bench_pg_insert")
    print(f"  {label}: {d:.1f}s")


run("A autocommit+executemany", "A")
run("B single txn + executemany", "B")
run("C single txn + execute_values", "C")
