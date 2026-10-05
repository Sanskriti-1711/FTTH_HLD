"""Occupancy read-back: the last run's spare duct ways feed BF_DUCTS.

The duct stage records ``WAYS_TOTAL`` / ``WAYS_USED`` on every corridor and the
engine stores that under ``gis.duct_occupancy`` — but nothing carried it
forward, so a re-run re-planned the network with no idea which ducts still had
spare ways (docs/GLOBAL_TODO.md). The read-back closes that loop:

* ``occupancy.duct_brownfield_features`` turns the stored rows into brownfield
  DUCT features whose capacity fields are the names the loader AUTO-DETECTS
  (``capacity_total`` / ``capacity_used`` — the one-click pipeline exposes no
  capacity-field parameter, so ``WAYS_TOTAL`` alone would be ignored), dropping
  full ducts entirely;
* ``occupancy.write_duct_brownfield`` merges them into ``bf_ducts.geojson``
  beside any survey file, and
* ``main._brownfield_args`` feeds that file as BF_DUCTS on the run.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_occupancy_brownfield.py -v
"""

import json

import occupancy
import postgis


def _occ_row(duct_id, total, used, geom=True):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}
        if geom else None,
        "properties": {
            "DUCT_ID": duct_id,
            "WAYS_TOTAL": total,
            "WAYS_USED": used,
            "VERIFY_STATUS": "Verified",
        },
    }


def test_duct_brownfield_features_renames_fields_and_drops_full_ducts(monkeypatch):
    rows = [
        _occ_row("D1", 4, 1),
        _occ_row("D2", 2, 2),      # full -> never reusable
        _occ_row("D3", 4, 0, geom=False),   # no geometry -> unusable
    ]
    seen = {}

    def _load(pid, table):
        seen["table"] = table
        return rows

    monkeypatch.setattr(postgis, "load_occupancy", _load)

    feats = occupancy.duct_brownfield_features("p1")
    assert seen["table"] == "duct_occupancy"

    assert len(feats) == 1
    props = feats[0]["properties"]
    assert props["DUCT_ID"] == "D1"
    # The names the brownfield loader auto-detects.
    assert props["capacity_total"] == 4
    assert props["capacity_used"] == 1
    assert props["USE_MODE"] == "brownfield"
    assert props["verify_status"] == "Verified"
    # Kept for a reviewer / the map.
    assert props["WAYS_TOTAL"] == 4 and props["WAYS_USED"] == 1


def test_duct_brownfield_features_handles_an_unavailable_registry(monkeypatch):
    monkeypatch.setattr(postgis, "load_occupancy", lambda pid, table: [])
    assert occupancy.duct_brownfield_features("p1") == []


def test_write_duct_brownfield_merges_beside_a_survey_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        occupancy, "duct_brownfield_features",
        lambda pid: [{"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[5, 5], [6, 5]]},
                      "properties": {"DUCT_ID": "OCC1",
                                     "capacity_total": 4,
                                     "capacity_used": 1}}],
    )
    bf_dir = tmp_path / "brownfield"
    bf_dir.mkdir()
    survey = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
         "properties": {"DUCT_ID": "SRV1", "capacity_total": 2,
                        "capacity_used": 0}},
    ]}
    (bf_dir / "bf_ducts.geojson").write_text(json.dumps(survey), encoding="utf-8")

    path = occupancy.write_duct_brownfield("p1", bf_dir)

    assert path == bf_dir / "bf_ducts.geojson"
    doc = json.loads(path.read_text(encoding="utf-8"))
    ids = [f["properties"]["DUCT_ID"] for f in doc["features"]]
    # Survey first (its field reading wins on a duplicate id), occupancy last.
    assert ids == ["SRV1", "OCC1"]


def test_write_duct_brownfield_is_a_noop_without_spare_ways(tmp_path, monkeypatch):
    monkeypatch.setattr(occupancy, "duct_brownfield_features", lambda pid: [])
    assert occupancy.write_duct_brownfield("p1", tmp_path / "brownfield") is None


def test_brownfield_args_feeds_the_occupancy_read_back(tmp_path, monkeypatch):
    import main

    def _write(project_id, bf_dir):
        bf_dir.mkdir(parents=True, exist_ok=True)
        path = bf_dir / "bf_ducts.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": []}),
                        encoding="utf-8")
        return path

    monkeypatch.setattr(occupancy, "write_duct_brownfield", _write)

    args = main._brownfield_args(None, tmp_path, "p1")

    assert "USE_BROWNFIELD=true" in args
    assert any(a.startswith("BF_DUCTS=") for a in args)


def test_brownfield_args_is_empty_with_no_inputs(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(occupancy, "write_duct_brownfield",
                        lambda project_id, bf_dir: None)
    assert main._brownfield_args(None, tmp_path, "p1") == []
