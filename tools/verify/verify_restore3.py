"""Authoritative check: measure the restored rows in metres inside PostGIS
(ST_Length on geography) and compare with each snapshot's own metre total."""
import os, psycopg2
PID = "f0426f446acd4b02ada8595e1bb3e3a9"
conn = psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"],
                        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
                        password=os.environ["PGPASSWORD"])
cur = conn.cursor()
targets = [
    ("Feeder_Ducts", "duct_layer", "AND properties->>'sublayer'='Feeder_Ducts'"),
    ("Distribution_Ducts", "duct_layer", "AND properties->>'sublayer'='Distribution_Ducts'"),
    ("Drop_Ducts", "duct_layer", "AND properties->>'sublayer'='Drop_Ducts'"),
    ("Feeder_Cable", "cable_layer", "AND properties->>'sublayer'='Feeder_Cable'"),
    ("Distribution_Cable", "cable_layer", "AND properties->>'sublayer'='Distribution_Cable'"),
    ("Coupleurs", "coupleur_layer", ""),
]
for name, table, where in targets:
    cur.execute(f"""
        SELECT count(*),
               round(sum(ST_Length(geom::geography))::numeric, 1),
               round(min(ST_Length(geom::geography))::numeric, 2),
               round(max(ST_Length(geom::geography))::numeric, 1)
        FROM gis.{table} WHERE project_id=%s {where}""", (PID,))
    n, tot, lo, hi = cur.fetchone()
    print(f"{name:20s} n={n:<5d} total={tot:>10} m   min={lo:>8} max={hi:>9}")
conn.close()
