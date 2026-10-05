"""Put a saved trench GeoJSON back into ``gis.trench_layer``.

The map reads PostGIS, not the GeoPackage on disk, so restoring a baseline means
re-ingesting it. Run under the engine's own interpreter (it has the DB driver):

    set -a; source .env; set +a
    ./tmp/qgis_python.cmd tmp/restore_trench.py <geojson> [project_id]
"""
import os
import sys

sys.path.insert(0, os.path.join("HLD_Planning_01", "web", "backend"))
import postgis                                            # noqa: E402

path = sys.argv[1]
pid = sys.argv[2] if len(sys.argv) > 2 else "f0426f446acd4b02ada8595e1bb3e3a9"
n = postgis.load_geojson_file(pid, "trenches", path)
print("restored %d feature(s) from %s into gis.trench_layer for %s"
      % (n, path, pid))
