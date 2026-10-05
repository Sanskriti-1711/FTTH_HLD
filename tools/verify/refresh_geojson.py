"""Re-export a project's stale GeoJSON layer files from its newer GeoPackages.

The engine serves the GeoJSON, and (before the _ensure_geojson fix) exported it
only when the file was MISSING. A re-run rewrites the GPKG, so the engine kept
serving the previous run's geometry. This script applies the same conversion
`_convert_gpkg_to_geojson` performs, but decides on mtime freshness.

Usage: python tmp/refresh_geojson.py <project_id> [--dry]
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("HLD_Planning_01/web/backend/outputs")

# (public_layer, gpkg, geojson) — mirrors ONECLICK_OUTPUTS in main.py
PAIRS = [
    ("objects", "Objects.gpkg", "Objects.geojson"),
    ("polygons", "Polygons.gpkg", "Polygons.geojson"),
    ("pdps", "PDPs.gpkg", "PDPs.geojson"),
    ("mfg", "MFG.gpkg", "MFG.geojson"),
    ("trenches", "Final_Trenches.gpkg", "Final_Trenches.geojson"),
    ("cables", "Feeder_Cable.gpkg", "Feeder_Cable.geojson"),
    ("cables", "Distribution_Cable.gpkg", "Distribution_Cable.geojson"),
    ("ducts", "Feeder_Ducts.gpkg", "Feeder_Ducts.geojson"),
    ("ducts", "Distribution_Ducts.gpkg", "Distribution_Ducts.geojson"),
    ("ducts", "Drop_Ducts.gpkg", "Drop_Ducts.geojson"),
    ("coupleurs", "Coupleurs.gpkg", "Coupleurs.geojson"),
    ("chambers", "Chambers.gpkg", "Chambers.geojson"),
    ("poles", "Poles.gpkg", "Poles.geojson"),
    ("aerial_drops", "Aerial_Drops.gpkg", "Aerial_Drops.geojson"),
    ("aerial_drop_trenches", "Aerial_Drop_Trenches.gpkg", "Aerial_Drop_Trenches.geojson"),
    ("aerial_cable", "Aerial_Cable.gpkg", "Aerial_Cable.geojson"),
    ("trench_nodes", "Trench_Nodes.gpkg", "Trench_Nodes.geojson"),
    ("brownfield", "Existing_Infrastructure.gpkg", "Existing_Infrastructure.geojson"),
]


def count(path):
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("features") or [])
    except Exception:
        return None


def main():
    project_id = sys.argv[1]
    dry = "--dry" in sys.argv[2:]
    out = ROOT / project_id
    ogr2ogr = shutil.which("ogr2ogr")
    if not ogr2ogr:
        print("!! ogr2ogr not on PATH (source tmp/qgis_env.sh)")
        return 2

    refreshed = 0
    for _layer, gpkg_name, geojson_name in PAIRS:
        gpkg, geojson = out / gpkg_name, out / geojson_name
        if not gpkg.exists():
            continue
        stale = (not geojson.exists()) or gpkg.stat().st_mtime > geojson.stat().st_mtime
        before = count(geojson) if geojson.exists() else None
        if not stale:
            continue
        if dry:
            print(f"  would refresh {geojson_name}: {before} -> ? features")
            continue
        if geojson.exists():
            geojson.unlink()
        res = subprocess.run(
            [ogr2ogr, "-f", "GeoJSON", "-t_srs", "EPSG:4326", str(geojson), str(gpkg)],
            capture_output=True, text=True, timeout=900,
        )
        if res.returncode != 0 or not geojson.exists():
            print(f"  !! {geojson_name} failed: {res.stderr.strip()[:200]}")
            continue
        after = count(geojson)
        refreshed += 1
        print(f"  {geojson_name}: {before} -> {after} features")
    print(f"{'would refresh' if dry else 'refreshed'} {refreshed} stale export(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
