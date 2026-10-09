"""Re-run one project with aerial + brownfield, then leave it ready to publish.

Why this exists
---------------
The user asked for the latest run to be re-published "with all the updated
components, aerial and brownfield regions too".  Two things have to be in
place before `_run_pipeline` is called, and neither is an upload:

* **Aerial zones.**  For an area run the trench stage classifies a non-diggable
  drop leg as aerial from `AERIAL_ZONES`, which `_osm_constraint_args` reads
  from ``inputs/osm/landuse/``.  This project has no such directory, so the
  zones are derived here from the project's OWN OSM landuse — the same layer
  the ``/ftth/hld/input-layers`` route serves — restricted to the classes the
  designer treats as non-diggable.

* **Brownfield.**  The archive this project already holds is the Monument Road
  (Birmingham) reference ZIP, laid on Berlin roads, so it cannot contribute
  reuse: nothing in it is within a kilometre of the design.  What CAN
  contribute is the project's own previous design, and the engine already
  knows how — passing ``project_id`` makes ``_brownfield_args`` export the
  stored duct occupancy as ``bf_ducts.geojson`` (spare ways only) and feed it
  as ``BF_DUCTS``.  That is the capacity read-back, and on this re-run it is
  the brownfield the design actually reuses.

Usage (engine backend dir, Anaconda python):

    python tmp_rerun_publish.py [project_id]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault(
    "QGIS_EXECUTABLE", r"C:\Program Files\QGIS 3.44.6\bin\qgis_process-qgis.bat"
)
os.environ.setdefault("QGIS_PLUGINPATH", str(BACKEND.parent))

import main  # noqa: E402
import osm_source  # noqa: E402

PROJECT_ID = sys.argv[1] if len(sys.argv) > 1 else "24cd3573f4884a578c1b98a80bbd4040"
AREA = "Mariendorf, Berlin, Germany"


def write_aerial_zones(output_dir: Path) -> Path | None:
    """Write inputs/osm/landuse/landuse.geojson from the project's OSM store.

    Returns the path written, or None when the store holds nothing for this
    area (a missing aerial layer must not fail the run — the trench stage then
    simply classifies no leg as aerial, exactly as before).
    """
    target = output_dir / "inputs" / "osm" / "landuse" / "landuse.geojson"
    if target.exists():
        print(f"[aerial] reusing existing {target}")
        return target
    try:
        resolution = osm_source.resolve_area(AREA)
        osm_source.ensure_area_data(AREA, resolution["bbox"])
        payload = osm_source.input_layer_geojson(
            resolution["polygon"],
            "landuse",
            country=resolution.get("country") or "",
            city=resolution.get("city") or "",
            country_code=resolution.get("country_code") or "",
        )
    except Exception as exc:  # noqa: BLE001 - optional constraint
        print(f"[aerial] could not derive landuse zones: {exc!r}")
        return None
    features = payload.get("features") or []
    if not features:
        print("[aerial] OSM landuse returned no features for this area")
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    print(f"[aerial] wrote {len(features)} zone polygon(s) to {target}")
    return target


def main_() -> int:
    output_dir = main.OUTPUT_DIR / PROJECT_ID
    excel = output_dir / "inputs" / "Main_DataSet.xlsx"
    roads = output_dir / "inputs" / "roads_mariendorf.geojson"
    for needed in (excel, roads):
        if not needed.is_file():
            print(f"[fatal] missing input: {needed}")
            return 2

    write_aerial_zones(output_dir)

    print(f"[run] project={PROJECT_ID} poly_method=3")
    print("[run] brownfield = this project's own duct occupancy read-back "
          "(+ any supplied archive)")
    # brownfield_path=None + project_id -> _brownfield_args exports the stored
    # occupancy as bf_ducts.geojson and enables reuse.
    main._run_pipeline(PROJECT_ID, excel, roads, output_dir, 3, None)

    task = main.tasks.get(PROJECT_ID) or {}
    print(f"[run] status={task.get('status')} stage={task.get('stage')} "
          f"progress={task.get('progress')}")
    if task.get("error"):
        print(f"[run] error={task['error']}")
    if task.get("downloads"):
        print(f"[run] {len(task['downloads'])} downloadable file(s)")
    return 0 if task.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main_())
