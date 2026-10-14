# -*- coding: utf-8 -*-
"""Re-run one project with the planner's brownfield ZIP and the aerial fix.

What this run does differently from a portal run
-----------------------------------------------
1. **The brownfield is the planner's archive.** ``main._brownfield_args`` no
   longer buries an uploaded archive under the project's whole stored network:
   the read-backs (``occupancy.write_duct_brownfield`` /
   ``write_trench_brownfield``) run only for the asset types the archive does
   not name. That is the production path, so both stay ON here.

2. **The aerial zone layer is WRITTEN from the area's own OSM store**, through
   ``osm_source.write_landuse_geojson`` -- the same writer the area-run path
   uses. The helper used to hand the designer the ``/ftth/hld/input-layers``
   REVIEW payload (``osm_id`` / ``landuse`` / ``natural`` / ``leisure`` /
   ``boundary``, no ``fclass``). ``_read_polygons_geom`` filters a layer by
   ``fclass`` and honours a layer WITHOUT it AS IS, so that payload became a
   hand-drawn no-dig mask over the whole ward: 714 polygons, 1089 ha, and
   **284 of 285 drop legs classified aerial** -- every house on its own pole and
   overhead span, which is what "not every drop cable is supposed to be aerial"
   looks like from the map. The fix is at the producer: write the layer with
   ``fclass``, and refuse a payload that has none.

Usage (engine backend dir):

    python tmp_rerun_publish.py [project_id] [brownfield_zip]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault(
    "QGIS_EXECUTABLE", r"C:\Program Files\QGIS 3.44.6\bin\qgis_process-qgis.bat"
)
os.environ.setdefault("QGIS_PLUGINPATH", str(BACKEND.parent))

import main  # noqa: E402
import occupancy  # noqa: E402
import osm_source  # noqa: E402
import postgis  # noqa: E402

# A run started from this helper runs in whatever environment the shell has, and
# with no PG* variables `postgis.is_available()` is False. The pipeline still
# writes every layer to disk — but `_ingest_outputs` in `_run_pipeline` then
# skips the whole publish half: nothing reaches `gis.*`, the platform rows keep
# the PREVIOUS design, and the run reports `completed`. That happened on
# 2026-10-13: the re-run produced 184 feeder ducts while the map and the permit
# matrix still read the old rows (0 feeder ducts), with nothing in the output
# saying so. Say it up front instead.
if not postgis.is_available():
    print("[warn] PostGIS is NOT reachable from this process, so this run will "
          "NOT publish: layers land on disk and the engine serves them, but "
          "gis.* and the platform rows keep the previous design. Source .env "
          "(PGDATABASE/PGUSER/PGHOST/PGPORT) before running this helper.")

PROJECT_ID = sys.argv[1] if len(sys.argv) > 1 else "24cd3573f4884a578c1b98a80bbd4040"
HLD_ROOT = BACKEND.parents[1]
BROWNFIELD_ZIP = Path(sys.argv[2]) if len(sys.argv) > 2 else (
    HLD_ROOT / "brownfield_layers.zip"
)


def _area_label(output_dir: Path) -> str:
    """The area this run planned, for the OSM store lookup.

    The running task records it; a re-run started fresh has to read it back, and
    an area run names its roads file after the area (``roads_<area>.geojson``).
    """
    task = main.tasks.get(PROJECT_ID) or {}
    area = str(task.get("area") or "").strip()
    if area:
        return area
    for roads in sorted((output_dir / "inputs").glob("roads*.geojson")):
        stem = roads.stem
        if stem.startswith("roads_"):
            return stem[len("roads_"):].replace("_", " ")
    return ""


def _zone_fclass_count(path: Path) -> Optional[int]:
    """Rows carrying ``fclass``, or None when the layer cannot be read."""
    try:
        features = (json.loads(path.read_text(encoding="utf-8"))
                    .get("features") or [])
    except (OSError, ValueError):
        return None
    return sum(
        1 for f in features
        if str((f.get("properties") or {}).get("fclass") or "").strip()
    )


def write_aerial_zones(output_dir: Path) -> Optional[Path]:
    """Write the AERIAL_ZONES layer the designer reads, from the area's OSM store.

    The layer goes to ``inputs/osm/landuse/landuse.geojson`` -- the path
    ``design.design_inputs`` looks in, so a layer written anywhere else is never
    read -- and it is produced by ``osm_source.write_landuse_geojson``, which
    resolves ``fclass`` from the store's own tags. The pre-run REVIEW payload
    (``osm_source.input_layer_geojson``) is never used for this: it is shaped
    for the map, carries no ``fclass``, and a layer without that field is
    honoured BY THE DESIGNER as a hand-drawn no-dig mask -- which is how a whole
    ward came out aerial.

    Returns the path, or None when there is no usable layer. A run with no
    zones is still valid (drop legs are then classified on length and chain
    only), so nothing here fails the run.
    """
    target = output_dir / "inputs" / "osm" / "landuse" / "landuse.geojson"
    resolution: Dict[str, Any] = {}
    try:
        resolution = osm_source.resolve_area(_area_label(output_dir))
    except Exception as exc:  # noqa: BLE001 - zone derivation is optional
        print(f"[aerial] the area could not be resolved ({exc!r}) - the run "
              "continues without zones")
        return None
    polygon = (resolution or {}).get("polygon")
    if not polygon:
        print("[aerial] the area resolved to no polygon - the run continues "
              "without zones")
        return None
    try:
        written = osm_source.write_landuse_geojson(str(target), polygon)
    except Exception as exc:  # noqa: BLE001
        print(f"[aerial] the zone layer could not be written ({exc!r})")
        return None
    if not written:
        print("[aerial] the area's OSM store has no landuse - the run continues "
              "without zones")
        return None
    path = Path(written)
    with_class = _zone_fclass_count(path)
    if not with_class:
        # Refusing it leaves a valid run; honouring it would declare the whole
        # ward non-diggable and put every house on a pole.
        print(f"[aerial] REFUSING {path}: no 'fclass' property, so the trench "
              "designer would honour the whole layer as a hand-drawn no-dig "
              "mask and classify every drop leg aerial")
        return None
    print(f"[aerial] {with_class} landuse polygon(s) with fclass -> AERIAL_ZONES")
    return path


def main_() -> int:
    output_dir = main.OUTPUT_DIR / PROJECT_ID
    excel = output_dir / "inputs" / "Main_DataSet.xlsx"
    roads = output_dir / "inputs" / "roads_mariendorf.geojson"
    for needed in (excel, roads):
        if not needed.is_file():
            print(f"[fatal] missing input: {needed}")
            return 2
    if not BROWNFIELD_ZIP.is_file():
        print(f"[fatal] missing brownfield archive: {BROWNFIELD_ZIP}")
        return 2

    write_aerial_zones(output_dir)

    # A previous run's extracted archive is still in brownfield/. It is an INPUT
    # directory: leaving it there made a run that supplied no archive inherit the
    # last one, and its stale files take part in the BF_* matching.
    bf_dir = output_dir / "brownfield"
    if bf_dir.is_dir():
        for stale in bf_dir.iterdir():
            if stale.is_file():
                stale.unlink()
    print(f"[brownfield] cleared {bf_dir}")

    # ── the planner's archive is the brownfield for the layers it names ─────
    # Both read-backs stay ON, which is the production path: `_brownfield_args`
    # now runs them only for the asset types the archive does NOT carry, so an
    # uploaded region is never buried under the project's whole stored network.
    # They are logged here so a run that DOES fall back to them says so.
    merged: List[str] = []
    real_ducts, real_trench = occupancy.write_duct_brownfield, occupancy.write_trench_brownfield

    def _note_ducts(project_id, bf_dir):
        out = real_ducts(project_id, bf_dir)
        if out is not None:
            merged.append("ducts")
        return out

    def _note_trench(project_id, bf_dir):
        out = real_trench(project_id, bf_dir)
        if out:
            merged.append("trenches")
        return out

    occupancy.write_duct_brownfield = _note_ducts
    occupancy.write_trench_brownfield = _note_trench
    print(f"[brownfield] archive = {BROWNFIELD_ZIP.name} (read-back fills gaps only)")

    print(f"[run] project={PROJECT_ID} poly_method=3")
    main._run_pipeline(PROJECT_ID, excel, roads, output_dir, 3, BROWNFIELD_ZIP)

    task = main.tasks.get(PROJECT_ID) or {}
    # The pipeline's own log lives in this process's message deque, so without
    # this dump the run leaves no trace of what the stages decided -- and the
    # stage lines ("aerial drops: N leg(s) ... zone/chain", "Brownfield:
    # loaded ...") are the evidence a reviewer needs.
    log_path = BACKEND / "logs" / f"rerun_{PROJECT_ID[:8]}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as fh:
            for msg in task.get("messages") or []:
                text = msg.get("text") if isinstance(msg, dict) else str(msg)
                level = msg.get("level") if isinstance(msg, dict) else "info"
                fh.write(f"[{level}] {text}\n")
        print(f"[run] log -> {log_path}")
    except OSError as exc:
        print(f"[run] could not write {log_path}: {exc!r}")

    print(f"[run] status={task.get('status')} stage={task.get('stage')} "
          f"progress={task.get('progress')}")
    if merged:
        print(f"[brownfield] read-back ran for {', '.join(sorted(set(merged)))} "
              "(the archive did not name them)")
    else:
        print("[brownfield] read-back ran for nothing: the archive named every "
              "layer it could have filled")
    if task.get("error"):
        print(f"[run] error={task['error']}")
    return 0 if task.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main_())
