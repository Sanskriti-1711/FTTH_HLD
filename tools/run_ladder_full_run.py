"""Run one isolated full HLD pipeline from local fixture files.

This harness uses the backend's already-configured qgis_process launcher and
subprocess environment, but does not start the API, ingest to PostGIS, or alter
a prior project output. Usage from the repo root:

    env -u PYTHONPATH -u PYTHONHOME python HLD_Planning_01/tools/run_ladder_full_run.py \
        <addresses.xlsx> <roads.gpkg|roads.geojson|roads.zip> <new-output-dir>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
BACKEND_DIR = ENGINE_ROOT / "web" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("excel", type=Path)
    parser.add_argument("roads", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--poly-method", type=int, default=3)
    args = parser.parse_args()

    excel = args.excel.resolve()
    roads = args.roads.resolve()
    output_dir = args.output_dir.resolve()
    if not excel.is_file():
        parser.error(f"address workbook does not exist: {excel}")
    if not roads.is_file():
        parser.error(f"roads input does not exist: {roads}")
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error(f"output directory must be new or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    qgis = main._find_qgis_process()
    if not qgis:
        parser.error("QGIS process launcher not found")
    command = [
        qgis,
        "run",
        "hldplanning:end_to_end_pipeline",
        "--",
        f"EXCEL={excel.as_posix()}",
        f"ROADS={roads.as_posix()}",
        f"OUTPUT_DIR={output_dir.as_posix()}",
        f"POLY_METHOD={args.poly_method}",
    ]
    if sys.platform == "win32" and qgis.lower().endswith((".bat", ".cmd")):
        command = " ".join(main._quote_cmd_arg(str(part)) for part in command)

    project_id = "local_ladder_audit"
    main._run_command(project_id, command, output_dir)
    expected = ("Polygons.gpkg", "PDPs.gpkg", "MFG.gpkg", "Objects.gpkg",
                "Final_Trenches.gpkg", "Feeder_Cable.gpkg", "Distribution_Cable.gpkg")
    missing = [name for name in expected if not (output_dir / name).is_file()]
    if missing:
        print("HLD run did not produce required audit layers: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("Full HLD run completed into: " + output_dir.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
