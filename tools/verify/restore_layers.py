"""Put saved snapshot layers back into their ``gis.*`` tables.

The map reads PostGIS, not the GeoPackages on disk, so restoring a baseline
means re-ingesting it — and grouped layers (ducts = feeder + distribution +
drop, cables = feeder + distribution) share ONE table, so every sublayer has to
go in together: ``replace`` on the first, ``replace=False`` after, or the first
load would delete the sublayers loaded before it.

Run under the engine's own interpreter (it has the DB driver):

    set -a; source .env; set +a
    ./tmp/qgis_python.cmd tmp/restore_layers.py <plan.json>

``plan.json``: {"project": "<id>", "groups": [{"table": "duct_layer",
"sublayers": [{"name": "Feeder_Ducts", "file": "..."}, ...]}]}
"""
import json
import os
import sys

sys.path.insert(0, os.path.join("HLD_Planning_01", "web", "backend"))
import postgis                                            # noqa: E402

plan = json.load(open(sys.argv[1], "r", encoding="utf-8"))
pid = plan["project"]
for group in plan["groups"]:
    table = group["table"]
    for i, sub in enumerate(group["sublayers"]):
        n = postgis.load_geojson_file(
            pid, table, sub["file"],
            replace=(i == 0),                    # wipe the table ONCE, then append
            sublayer=sub["name"],
        )
        print("  %-22s -> %-14s %5d feature(s)  [%s]"
              % (sub["name"], table, n, sub["file"]))
print("done")
