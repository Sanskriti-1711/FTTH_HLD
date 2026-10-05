"""Time the engine ingest against an existing output directory.

Run with the engine's own interpreter (Anaconda python), in a clean env:

  unset PYTHONPATH PYTHONHOME
  python tmp/bench_ingest.py web/backend/outputs/<run_dir> <existing_project_id>

Wraps ``_ensure_geojson`` (ogr2ogr reprojection) and ``load_geojson_file``
(PostGIS insert) so the split per layer is visible, then times occupancy.store.
"""
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "web" / "backend"
sys.path.insert(0, str(BACKEND))

import main  # noqa: E402
import occupancy  # noqa: E402
import postgis  # noqa: E402

out_dir = pathlib.Path(sys.argv[1]).resolve()
project_id = sys.argv[2]

_convert = main._convert_gpkg_to_geojson
_load = postgis.load_geojson_file
_totals = {"convert": 0.0, "load": 0.0}


def convert(gpkg_path, geojson_path):
    t = time.perf_counter()
    r = _convert(gpkg_path, geojson_path)
    d = time.perf_counter() - t
    _totals["convert"] += d
    if d > 0.05:
        print(f"    convert {gpkg_path.name}: {d:.1f}s", flush=True)
    return r


# _ensure_geojson calls the module-global name, so patch there.
main._convert_gpkg_to_geojson = convert


def load(*args, **kwargs):
    t = time.perf_counter()
    r = _load(*args, **kwargs)
    d = time.perf_counter() - t
    _totals["load"] += d
    print(f"    load {args[1]}: {d:.1f}s ({r} features)", flush=True)
    return r


postgis.load_geojson_file = load

t0 = time.perf_counter()
layers = main._ingest_outputs(project_id, out_dir)
t1 = time.perf_counter()
print(f"ingest: {t1 - t0:.1f}s  convert={_totals['convert']:.1f}s "
      f"load={_totals['load']:.1f}s  layers={len(layers)}", flush=True)

t2 = time.perf_counter()
occ = occupancy.store(out_dir, project_id)
t3 = time.perf_counter()
print(f"occupancy.store: {t3 - t2:.1f}s  {occ}", flush=True)
print(f"TOTAL: {t3 - t0:.1f}s", flush=True)
