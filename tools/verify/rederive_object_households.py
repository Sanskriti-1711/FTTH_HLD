"""Put the household fields on a project's served Objects layer, under the new names.

The design pipeline's object layer now writes `households` (the BUILDING's total,
on every one of its service-location rows), `premises` and `household_method`,
and no longer writes `HH` / `HH_METHOD`.  Projects published before that carry
the old per-location `HH` column instead, so their served Objects layer has no
household count a reader can trust per building.

The aggregate is a pure function of columns the layer already holds
(`OSM_ID`, `HH`, `HH_METHOD`), so this derives it from the served file and
rebuilds the outputs the platform serves from -- no design re-run.

The GPKG cannot be updated row by row with sqlite3: its spatialite triggers call
ST_IsEmpty, which the Python sqlite module does not register, so any UPDATE
fails.  Instead the fields are rewritten on the GeoJSON and the GPKG is rebuilt
from it with ogr2ogr (which owns the spatial functions).

It writes, in order:
  1. Objects.geojson  -- HH/HH_METHOD replaced by households/premises/household_method
  2. Objects.gpkg     -- rebuilt from the geojson, reprojected to EPSG:25833
  3. gis.object_layer -- refreshed from the geojson, so the running engine serves
     the new fields rather than the JSONB it ingested before

Usage:
    set -a; . ./.env; set +a && PYTHONPATH= PYTHONHOME= /c/Users/HP/anaconda3/python.exe \
        tmp/rederive_object_households.py <project_id> [--apply]

Without --apply it reports what it would do and touches nothing.
"""
import json
import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "HLD_Planning_01"))

import pandas as pd  # noqa: E402

from HLDPlanning.utils.sheet_utils import (  # noqa: E402
    HOUSEHOLD_AGGREGATE_COLUMNS,
    add_household_aggregates,
    households_by_object,
)

OUTPUTS = os.path.join(_ROOT, "HLD_Planning_01", "web", "backend", "outputs")
LAYER = "Objects"
OUT_CRS = "EPSG:25833"
LEGACY = ("HH", "HH_METHOD")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project_id = sys.argv[1]
    apply_changes = "--apply" in sys.argv
    use_register = "--register" in sys.argv
    out_dir = os.path.join(OUTPUTS, project_id)
    gpkg = os.path.join(out_dir, "Objects.gpkg")
    geojson = os.path.join(out_dir, "Objects.geojson")
    if not os.path.isfile(geojson):
        print("!! no Objects.geojson at %s" % geojson)
        return 1

    data = json.load(open(geojson, encoding="utf-8"))
    features = data.get("features") or []
    props = pd.DataFrame([f.get("properties") or {} for f in features])
    if not features:
        print("!! nothing to do")
        return 1
    print("Objects.geojson: %d features; has %s"
          % (len(features), sorted(c for c in props.columns if c in
                                   HOUSEHOLD_AGGREGATE_COLUMNS + LEGACY)))

    # Idempotent: `households` already holds the BUILDING total on every row, so
    # feeding it back through the aggregator would sum the repeats and inflate
    # every multi-location block.  Derive only from the legacy per-location
    # columns; otherwise the values already in the file are the answer.
    already_renamed = ("households" in props.columns
                       and not any(c in props.columns for c in LEGACY))

    if use_register:
        # The served layer was published before the register was loaded, so its
        # counts are the OSM heuristic.  Re-apply the register here: the row
        # estimate is the apportionment WEIGHT, and the register fixes each
        # postcode's TOTAL (see household_register.apply_register).
        sys.path.insert(0, os.path.join(_ROOT, "HLD_Planning_01", "web", "backend"))
        import household_register as hr  # noqa: E402
        records = props.to_dict("records")
        postcodes = [r.get("Postcode") for r in records]
        by_pc, by_uprn, meta = hr.load_register("GB", postcodes)
        print("register: %d postcode(s) matched of %d requested; source=%s"
              % (len(by_pc), meta.get("requested"), meta.get("source")))
        if not by_pc and not by_uprn:
            print("!! register matched nothing — run with `set -a; . ./.env; set +a`")
            return 1
        before = sum(households_by_object(records).values())
        flat = [{"Postcode": r.get("Postcode"),
                 "households": int(r.get("households") or 1),
                 "household_method": str(r.get("household_method") or ""),
                 "UPRN": str(r.get("UPRN") or "")} for r in records]
        resolved, stats = hr.apply_register(flat, by_pc, by_uprn)
        for row, out in zip(records, resolved):
            row["households"] = int(out["households"])
            row["household_method"] = str(out["household_method"])
        props = pd.DataFrame(records)
        # Collapse the per-location spread back into the BUILDING total.
        add_household_aggregates(props)
        after = sum(households_by_object(props.to_dict("records")).values())
        print("register applied: %d premise(s) registered; homes %d -> %d"
              % (stats["premises_registered"], before, after))
    elif already_renamed:
        print("already renamed — values left as they are (nothing to derive)")
    else:
        add_household_aggregates(props)
    homes = households_by_object(props.to_dict("records"))
    print("derived: %d service locations, %d buildings, %d homes (counted once per "
          "building)" % (len(props), len(homes), sum(homes.values())))
    print("household_method: %s"
          % json.dumps(props["household_method"].value_counts().to_dict(), sort_keys=True))
    print("rows repeating a building's total (multi-location blocks): %d"
          % int((props["premises"] > 1).sum()))

    if not apply_changes:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    for feat, hh, prem, meth in zip(
        features, props["households"], props["premises"], props["household_method"]
    ):
        p = feat.setdefault("properties", {})
        for old in LEGACY:
            p.pop(old, None)
        p["households"] = int(hh)
        p["premises"] = int(prem)
        p["household_method"] = str(meth)
    with open(geojson, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print("geojson updated")

    ogr2ogr = shutil.which("ogr2ogr")
    if not ogr2ogr:
        print("!! ogr2ogr not on PATH — geojson updated but Objects.gpkg not rebuilt")
        return 1
    tmp_gpkg = os.path.join(out_dir, "Objects.rebuild.gpkg")
    if os.path.exists(tmp_gpkg):
        os.remove(tmp_gpkg)
    result = subprocess.run(
        [ogr2ogr, "-f", "GPKG", "-nln", LAYER, "-t_srs", OUT_CRS, tmp_gpkg, geojson, LAYER],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0 or not os.path.exists(tmp_gpkg):
        print("!! ogr2ogr failed: %s" % (result.stderr or result.stdout))
        return 1
    os.replace(tmp_gpkg, gpkg)
    print("gpkg rebuilt at %s" % gpkg)

    # Refresh the engine's own copy, or the running engine keeps serving the
    # JSONB it ingested before the rename.
    if os.environ.get("PGHOST"):
        sys.path.insert(0, os.path.join(_ROOT, "HLD_Planning_01", "web", "backend"))
        import postgis  # noqa: E402
        if postgis.is_available():
            n = postgis.load_geojson_file(project_id, "objects", geojson, replace=True)
            print("engine gis.object_layer refreshed: %d features" % n)
        else:
            print("postgis unavailable — engine would fall back to the file")
    else:
        print("PGHOST not set — run with `set -a; . ./.env; set +a` to refresh the engine copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
