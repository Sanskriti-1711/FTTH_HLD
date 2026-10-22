"""The LLD re-plan must survive the engine's non-persistent disk.

Two things are covered:
- the pipeline inputs are archived when a run starts and restored before a
  re-plan reads them (the container's disk is wiped on every redeploy);
- when nothing was archived (a run older than the archive), the inputs are
  rebuilt from the extent that run already designed, rather than failing.
"""

import importlib
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

engine = importlib.import_module("main")


def test_dataset_extent_covers_every_coordinate():
    dataset = {
        "type": "FeatureCollection",
        "features": [
            {"geometry": {"type": "Point", "coordinates": [-1.90, 52.47]},
             "properties": {}},
            {"geometry": {"type": "MultiLineString",
                          "coordinates": [[[-1.92, 52.48], [-1.91, 52.49]]]},
             "properties": {}},
            {"geometry": None, "properties": {}},
        ],
    }
    polygon, bbox = engine._dataset_extent(dataset)
    assert polygon["type"] == "Polygon"
    ring = polygon["coordinates"][0]
    assert ring[0] == ring[-1], "the ring must close"
    minx, miny, maxx, maxy = bbox
    assert minx < -1.92 and maxx > -1.90
    assert miny < 52.47 and maxy > 52.49


def test_dataset_extent_without_geometry_returns_none():
    assert engine._dataset_extent({"features": [{"geometry": None}]}) is None


def test_persist_inputs_archives_the_replan_inputs(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    (inputs / "osm" / "landuse").mkdir(parents=True)
    (inputs / "Main_DataSet.xlsx").write_bytes(b"xlsx")
    (inputs / "roads.geojson").write_text("{}", encoding="utf-8")
    (inputs / "area_resolution.json").write_text("{}", encoding="utf-8")
    (inputs / "osm" / "landuse" / "landuse.geojson").write_text("{}", encoding="utf-8")

    seen = {}
    monkeypatch.setattr(engine.postgis, "is_available", lambda: True)
    monkeypatch.setattr(
        engine.postgis, "store_project_inputs",
        lambda pid, files: seen.update({"pid": pid, "files": dict(files)}) or len(files),
    )
    engine._persist_inputs("proj-1", inputs)

    assert seen["pid"] == "proj-1"
    names = set(seen["files"])
    assert {"Main_DataSet.xlsx", "roads.geojson", "area_resolution.json",
            "osm/landuse/landuse.geojson"} <= names


def test_restore_inputs_puts_the_archive_back(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"

    def fake_restore(pid, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "Main_DataSet.xlsx").write_bytes(b"xlsx")
        return ["Main_DataSet.xlsx"]

    monkeypatch.setattr(engine.postgis, "is_available", lambda: True)
    monkeypatch.setattr(engine.postgis, "restore_project_inputs", fake_restore)
    engine._restore_inputs("proj-1", inputs)

    assert (inputs / "Main_DataSet.xlsx").is_file()


def test_rebuild_inputs_uses_the_surveyed_extent(tmp_path, monkeypatch):
    """A run whose inputs were never archived is rebuilt from its own design."""
    import osm_source

    inputs = tmp_path / "inputs"
    calls = {}

    def fake_build(project_id, area, output_dir, **kwargs):
        calls.update({"area": area, "resolution": kwargs.get("resolution"),
                      "output_dir": output_dir})
        (Path(output_dir) / "inputs").mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "inputs" / "Main_DataSet.xlsx").write_bytes(b"xlsx")
        (Path(output_dir) / "inputs" / "roads.geojson").write_text("{}", encoding="utf-8")
        return {"premises": 12, "roads_km": 3.4}

    monkeypatch.setattr(osm_source, "build_inputs", fake_build)
    monkeypatch.setattr(engine.postgis, "is_available", lambda: False)

    dataset = {
        "features": [
            {"geometry": {"type": "Point", "coordinates": [-1.90, 52.47]}, "properties": {}},
            {"geometry": {"type": "Point", "coordinates": [-1.91, 52.48]}, "properties": {}},
        ]
    }
    assert engine._rebuild_replan_inputs("proj-1", "LLD-V07", dataset, inputs) is None
    assert calls["area"] == "surveyed extent"
    resolution = calls["resolution"]
    assert resolution["polygon_source"] == "survey_extent"
    assert resolution["bbox"][0] < -1.91 and resolution["bbox"][2] > -1.90
    assert (inputs / "Main_DataSet.xlsx").is_file()
    assert (inputs / "roads.geojson").is_file()
