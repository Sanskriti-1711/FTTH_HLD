"""Publish a manually produced pipeline run into the platform.

A run started by hand (tmp/run_pipeline_engine.py) writes its layers into a
per-run directory, not the project directory the platform ingests from.  This
copies the run's layers over the project directory and then drives the same
ingest the engine uses for a normal run: GeoJSON conversion, PostGIS load,
occupancy registry, downloads, project row.

Usage: python tmp/publish_run.py <run_dir_name> <project_id>
"""
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(r"D:\Downloads_D\Q-GIS\Fibre-FTTH\HLD_Planning_01\web\backend")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import main  # noqa: E402
import postgis  # noqa: E402

PUBLISH_EXTS = {".gpkg", ".xlsx", ".csv"}


def main_(run_name: str, project_id: str) -> int:
    run_dir = BACKEND / "outputs" / run_name
    proj_dir = BACKEND / "outputs" / project_id
    if not run_dir.is_dir():
        print("run dir not found:", run_dir)
        return 2
    proj_dir.mkdir(parents=True, exist_ok=True)

    copied, skipped = 0, 0
    for f in sorted(run_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in PUBLISH_EXTS:
            continue
        # Keep the project's own inputs if a name ever collides.
        if f.name.lower() in ("boq.xlsx", "bom.xlsx"):
            pass
        shutil.copy2(f, proj_dir / f.name)
        copied += 1
    print(f"copied {copied} file(s) from {run_name} -> {project_id}")

    print("postgis available:", postgis.is_available())
    layers = main._ingest_outputs(project_id, proj_dir)
    print(f"ingested {len(layers)} layer(s):")
    for lyr in layers:
        name = lyr.get("name") or lyr.get("layer") or lyr
        cnt = lyr.get("count") or lyr.get("features")
        print(f"   {name}: {cnt}")

    downloads = main._register_downloads(project_id, proj_dir)
    print("downloads:", len(downloads or []))

    if postgis.is_available():
        # roads_filename is NOT NULL on the project row (the engine always has
        # it from the upload).  Reuse whatever the row already carries rather
        # than inventing one.
        existing = postgis.get_project(project_id) or {}
        roads = existing.get("roads_filename")
        if not roads:
            cands = list((proj_dir / "inputs").glob("*roads*")) \
                if (proj_dir / "inputs").is_dir() else []
            roads = cands[0].name if cands else "berlin-roads-bundle.zip"
        postgis.upsert_project(
            project_id, status="completed", roads_filename=roads,
            output_dir=str(proj_dir))
        print("project row updated (roads_filename=%s)" % roads)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1], sys.argv[2]))
