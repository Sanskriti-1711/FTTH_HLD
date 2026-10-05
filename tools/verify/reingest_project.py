"""Re-run the engine's own output ingest for a project that is already on disk.

`_ingest_outputs()` is the step that turns the run's GeoJSON exports into the
Platform layers: it clears the project's GIS rows and reloads them, plus the
occupancy registry. A run launched straight through ``qgis_process`` (no HTTP
run) leaves PostGIS holding the *previous* publish, and since the engine serves
PostGIS first, every page keeps showing the old geometry until this runs.

Course: it exports only when missing, but the load clears + replaces, so this is
safe to re-run and never duplicates rows.

Usage:
    cd HLD_Planning_01/web/backend && set -a && . ../../../.env && set +a && \\
        python ../../../tmp/reingest_project.py <project_id>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "HLD_Planning_01" / "web" / "backend"))

import main  # noqa: E402


def main_entry():
    project_id = sys.argv[1]
    output_dir = Path(main.OUTPUT_DIR) / project_id
    if not output_dir.is_dir():
        print(f"!! no output dir for {project_id}")
        return 2
    print(f"postgis available: {main.postgis.is_available()}")
    layers = main._ingest_outputs(project_id, output_dir)
    for layer in layers:
        print(f"  {layer.get('name'):<22} {layer.get('feature_count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
