"""Tests for the OSM reference layers → design-constraint wiring.

The upload form has always accepted railways / waterways / water / landuse /
natural and stored them under ``inputs/osm/<key>/`` — and for a long time
nothing consumed them.  They now feed the polygon barrier rule
(``POLY_BARRIER_EXTRA``) and the trench stage's aerial zones
(``AERIAL_ZONES``), with a ``design_inputs.json`` record beside the outputs.

No QGIS needed: the resolver only reads files and builds command-line args.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_osm_constraints.py -v
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder with inputs/osm/<key>/ and PostGIS switched off."""
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.postgis, "is_available", lambda: False)
    main.tasks.clear()
    output_dir = tmp_path / "proj1"
    (output_dir / "inputs" / "osm").mkdir(parents=True)
    yield output_dir
    main.tasks.clear()


def _osm_dir(output_dir: Path) -> Path:
    return output_dir / "inputs" / "osm"


def _write_geojson(path: Path, geom_type: str = "LineString") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": geom_type,
                             "coordinates": [[13.3, 52.5], [13.4, 52.51]]},
                "properties": {"fclass": "rail"},
            }],
        }),
        encoding="utf-8",
    )
    return path


def _write_shp_zip(zip_path: Path, stem: str = "layer") -> Path:
    """A zip containing the four files of a shapefile (contents arbitrary —
    the resolver only extracts; nothing parses the geometry here)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            zf.writestr(f"{stem}{ext}", b"not-really-a-shapefile")
    return zip_path


# ── the resolver ─────────────────────────────────────────────────────────


def test_no_osm_directory_yields_no_constraint_args(project):
    (project / "inputs" / "osm").rmdir()
    assert main._osm_constraint_args(project, "proj1") == []


def test_plain_geojson_barriers_are_passed_through(project):
    _write_geojson(_osm_dir(project) / "railways" / "railways.geojson")

    args = main._osm_constraint_args(project, "proj1")

    assert len(args) == 1
    assert args[0].startswith("POLY_BARRIER_EXTRA=")
    assert args[0].endswith("railways.geojson")


def test_every_barrier_key_aggregates_into_one_arg_each(project):
    for key in main.OSM_BARRIER_KEYS:
        _write_geojson(_osm_dir(project) / key / f"{key}.geojson")

    args = main._osm_constraint_args(project, "proj1")

    barriers = [a for a in args if a.startswith("POLY_BARRIER_EXTRA=")]
    assert len(barriers) == len(main.OSM_BARRIER_KEYS)
    assert not any(a.startswith("AERIAL_ZONES=") for a in args)


def test_zip_uploads_are_extracted_to_a_real_path(project):
    """qgis_process must receive a plain path — a /vsizip/ source is not
    reliably loadable as a processing layer parameter."""
    _write_shp_zip(_osm_dir(project) / "railways" / "railways.zip")

    args = main._osm_constraint_args(project, "proj1")

    assert len(args) == 1
    path = args[0].split("=", 1)[1]
    assert "/vsizip/" not in path
    assert path.endswith(".shp")
    assert Path(path).is_file()
    # The sidecars came along with the .shp.
    shp = Path(path)
    assert shp.with_suffix(".dbf").is_file()

    # Re-running the same project reuses the extraction (idempotent).
    again = main._osm_constraint_args(project, "proj1", log=False)
    assert again == args


def test_landuse_feeds_aerial_zones_and_writes_the_input_map(project):
    _write_geojson(_osm_dir(project) / "railways" / "railways.geojson")
    _write_geojson(_osm_dir(project) / "landuse" / "landuse.geojson",
                   geom_type="Polygon")

    args = main._osm_constraint_args(project, "proj1")

    assert any(a.startswith("AERIAL_ZONES=") and
               a.endswith("landuse.geojson") for a in args)
    record = json.loads((project / "design_inputs.json").read_text(encoding="utf-8"))
    assert record["aerial_zones"].endswith("landuse.geojson")
    assert len(record["poly_barrier_extra"]) == 1


def test_an_unreadable_entry_is_skipped_not_fatal(project):
    """An optional constraint must never fail the run."""
    (_osm_dir(project) / "water").mkdir()
    (_osm_dir(project) / "water" / "notes.txt").write_text("not a layer",
                                                           encoding="utf-8")
    _write_geojson(_osm_dir(project) / "railways" / "railways.geojson")

    args = main._osm_constraint_args(project, "proj1")

    barriers = [a for a in args if a.startswith("POLY_BARRIER_EXTRA=")]
    assert len(barriers) == 1
    # The skip is reported, not swallowed silently.
    logs = main.tasks["proj1"].get("messages") or main.tasks["proj1"].get("log") or []
    texts = [m.get("text", "") if isinstance(m, dict) else str(m) for m in logs]
    assert any("water" in t and "not a readable" in t for t in texts)


def test_an_empty_osm_tree_yields_nothing_and_no_record(project):
    args = main._osm_constraint_args(project, "proj1")
    assert args == []
    assert not (project / "design_inputs.json").exists()
