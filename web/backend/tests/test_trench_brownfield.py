"""Trench read-back: the project's OWN trenches feed BF_*_TRENCH on a re-run.

The duct read-back (``test_occupancy_brownfield.py``) closes the capacity loop,
but trenches had none: a re-run could only reuse trench files an uploaded
archive happened to carry. One Berlin project's run was fed a Birmingham
reference archive, so nothing in it was within a kilometre of the work — the
design had no way to reuse the trench network it had planned itself.

``occupancy.write_trench_brownfield`` exports the stored ``gis.trench_layer``
rows one file per tier, whose names the engine's ``_match_brownfield_param``
resolves to BF_FEEDER_TRENCH / BF_DIST_TRENCH / BF_TRENCHES, and
``main._brownfield_args`` feeds them on the run.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_trench_brownfield.py -v
"""

import json

import pytest

import occupancy
import postgis

LINE = {"type": "LineString", "coordinates": [[13.37, 52.44], [13.38, 52.44]]}


def _trench(trench_id, tier, **props):
    return {
        "type": "Feature",
        "geometry": LINE,
        "properties": {
            "TRENCH_ID": trench_id,
            "TRENCH_TIER": tier,
            "WIDTH_MM": 300,
            "DEPTH_MM": 600,
            **props,
        },
    }


def _stored(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def test_every_stored_trench_becomes_a_brownfield_feature(monkeypatch):
    monkeypatch.setattr(
        postgis,
        "get_layer_geojson",
        lambda project_id, layer: _stored(
            _trench("TR-1", "Feeder", VERIFY_STATUS="Designed"),
            _trench("TR-2", "Distribution"),
        ),
    )

    feats = occupancy.trench_brownfield_features("p1")
    assert len(feats) == 2

    props = feats[0]["properties"]
    assert props["ASSET_TYPE"] == "trench"
    assert props["STAGE"] == "brownfield"
    assert props["INFRA_STATUS"] == "Existing"
    # The fields the loader needs to see it as reusable capacity, not as work.
    assert props["capacity_total"] == 1
    assert props["capacity_used"] == 0
    assert props["USE_MODE"] == "brownfield"
    # The design's own attributes survive, so a reviewer can trace the row.
    assert props["TRENCH_ID"] == "TR-1"
    assert props["WIDTH_MM"] == 300
    assert props["trench_type"] == "feeder"
    # A planned trench is not a field-verified asset.
    assert props["VERIFY_STATUS"] == "Designed"


def test_one_file_per_tier_so_each_reaches_its_own_parameter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        postgis,
        "get_layer_geojson",
        lambda project_id, layer: _stored(
            _trench("F-1", "Feeder"),
            _trench("D-1", "Distribution"),
            _trench("D-2", "Distribution"),
        ),
    )

    written = {p.name: p for p in occupancy.write_trench_brownfield("p1", tmp_path)}

    assert sorted(written) == ["bf_distribution_trench.geojson", "bf_feeder_trench.geojson"]
    feeder = json.loads(written["bf_feeder_trench.geojson"].read_text(encoding="utf-8"))
    dist = json.loads(written["bf_distribution_trench.geojson"].read_text(encoding="utf-8"))
    assert [f["properties"]["TRENCH_ID"] for f in feeder["features"]] == ["F-1"]
    assert [f["properties"]["TRENCH_ID"] for f in dist["features"]] == ["D-1", "D-2"]


def test_a_tier_with_no_named_file_falls_back_to_the_merged_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        postgis,
        "get_layer_geojson",
        lambda project_id, layer: _stored(_trench("G-1", "Garden")),
    )

    written = [p.name for p in occupancy.write_trench_brownfield("p1", tmp_path)]

    assert written == ["bf_trenches.geojson"]


def test_the_written_file_declares_wgs84_like_the_duct_read_back(tmp_path, monkeypatch):
    """The gis tables are stored in 4326; a file with no `crs` member would be
    read as lon/lat anyway, but stating it keeps the merge step unambiguous."""
    monkeypatch.setattr(
        postgis, "get_layer_geojson", lambda project_id, layer: _stored(_trench("F-1", "Feeder"))
    )

    path = occupancy.write_trench_brownfield("p1", tmp_path)[0]
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["crs"]["properties"]["name"] == "EPSG:4326"


def test_a_survey_trench_file_is_merged_not_overwritten(tmp_path, monkeypatch):
    """An approved/uploaded trench file and the read-back both reach the param."""
    monkeypatch.setattr(
        postgis, "get_layer_geojson", lambda project_id, layer: _stored(_trench("F-1", "Feeder"))
    )
    survey = _stored(_trench("SRV-1", "Feeder"))
    (tmp_path / "bf_feeder_trench.geojson").write_text(json.dumps(survey), encoding="utf-8")

    path = [p for p in occupancy.write_trench_brownfield("p1", tmp_path)
            if p.name == "bf_feeder_trench.geojson"][0]
    ids = [
        f["properties"].get("TRENCH_ID")
        for f in json.loads(path.read_text(encoding="utf-8"))["features"]
    ]

    assert ids == ["SRV-1", "F-1"]


def test_no_stored_trenches_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        postgis, "get_layer_geojson", lambda project_id, layer: _stored()
    )

    assert occupancy.write_trench_brownfield("p1", tmp_path) == []


def test_a_missing_layer_or_a_dead_database_is_not_fatal(tmp_path, monkeypatch):
    def _boom(project_id, layer):
        raise RuntimeError("no gis schema")

    monkeypatch.setattr(postgis, "get_layer_geojson", _boom)

    assert occupancy.trench_brownfield_features("p1") == []
    assert occupancy.write_trench_brownfield("p1", tmp_path) == []


def test_a_feature_without_geometry_is_skipped(monkeypatch):
    no_geom = _trench("TR-1", "Feeder")
    no_geom["geometry"] = None
    monkeypatch.setattr(
        postgis,
        "get_layer_geojson",
        lambda project_id, layer: _stored(no_geom, _trench("TR-2", "Feeder")),
    )

    feats = occupancy.trench_brownfield_features("p1")

    assert [f["properties"]["TRENCH_ID"] for f in feats] == ["TR-2"]


def test_brownfield_args_feeds_the_trench_read_back(tmp_path, monkeypatch):
    import main

    def _write(project_id, bf_dir):
        bf_dir.mkdir(parents=True, exist_ok=True)
        path = bf_dir / "bf_feeder_trench.geojson"
        path.write_text(json.dumps(_stored(_trench("F-1", "Feeder"))), encoding="utf-8")
        return [path]

    monkeypatch.setattr(occupancy, "write_duct_brownfield", lambda project_id, bf_dir: None)
    monkeypatch.setattr(occupancy, "write_trench_brownfield", _write)

    args = main._brownfield_args(None, tmp_path, "p1")

    assert "USE_BROWNFIELD=true" in args
    assert any(a.startswith("BF_FEEDER_TRENCH=") for a in args)


def test_brownfield_args_is_empty_when_the_project_has_no_stored_network(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(occupancy, "write_duct_brownfield", lambda project_id, bf_dir: None)
    monkeypatch.setattr(occupancy, "write_trench_brownfield", lambda project_id, bf_dir: [])

    assert main._brownfield_args(None, tmp_path, "p1") == []


def _archive(path, name, features):
    import zipfile

    staging = path.parent / "archive_src"
    staging.mkdir(parents=True, exist_ok=True)
    layer = staging / name
    layer.write_text(json.dumps(_stored(*features)), encoding="utf-8")
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(layer, name)
    return path


# ── an uploaded archive is the brownfield, the read-back only fills gaps ────
#
# The read-backs export the project's WHOLE stored network -- every trench and
# every duct with a spare way, over the whole ward. Merging them into an
# uploaded handover turned one region into a copy of the design: the project
# this fixes had 240 read-back line features beside an 8-layer archive, and the
# "Existing infrastructure" layer on the map covered the entire design.

def test_an_uploaded_archive_is_not_buried_by_the_whole_project_read_back(
        tmp_path, monkeypatch):
    import main

    def _read_back(project_id, bf_dir):
        bf_dir.mkdir(parents=True, exist_ok=True)
        path = bf_dir / "bf_feeder_trench.geojson"
        path.write_text(json.dumps(_stored(_trench("WHOLE-1", "Feeder"))),
                        encoding="utf-8")
        return [path]

    calls = []
    monkeypatch.setattr(occupancy, "write_duct_brownfield", lambda p, d: None)

    def _trench_read_back(project_id, bf_dir):
        calls.append(project_id)
        return _read_back(project_id, bf_dir)

    monkeypatch.setattr(occupancy, "write_trench_brownfield", _trench_read_back)

    archive = _archive(tmp_path / "brownfield.zip", "bf_feeder_trench.geojson",
                       [_trench("HANDOVER-1", "Feeder")])
    args = main._brownfield_args(archive, tmp_path, "p1")

    assert calls == [], "the read-back must not run for a layer the archive carries"
    fed = [a for a in args if a.startswith("BF_FEEDER_TRENCH=")][0]
    rows = json.loads(open(fed.split("=", 1)[1], encoding="utf-8").read())
    assert [f["properties"]["TRENCH_ID"] for f in rows["features"]] == ["HANDOVER-1"]


def test_the_read_back_still_fills_the_layers_the_archive_omits(tmp_path, monkeypatch):
    """An archive may carry only some layers; the rest still come from the project."""
    import main

    def _write(project_id, bf_dir):
        bf_dir.mkdir(parents=True, exist_ok=True)
        path = bf_dir / "bf_feeder_trench.geojson"
        path.write_text(json.dumps(_stored(_trench("STORED-1", "Feeder"))),
                        encoding="utf-8")
        return [path]

    monkeypatch.setattr(occupancy, "write_duct_brownfield", lambda p, d: None)
    monkeypatch.setattr(occupancy, "write_trench_brownfield", _write)

    # The archive names chambers only, so BF_FEEDER_TRENCH is the read-back's.
    archive = _archive(tmp_path / "brownfield.zip", "bf_chambers.geojson", [])
    args = main._brownfield_args(archive, tmp_path, "p1")

    assert any(a.startswith("BF_FEEDER_TRENCH=") for a in args)
    fed = [a for a in args if a.startswith("BF_FEEDER_TRENCH=")][0]
    rows = json.loads(open(fed.split("=", 1)[1], encoding="utf-8").read())
    assert [f["properties"]["TRENCH_ID"] for f in rows["features"]] == ["STORED-1"]


def test_a_first_run_has_nothing_to_read_back(tmp_path, monkeypatch):
    """No stored network and no upload: the run continues greenfield."""
    import main

    monkeypatch.setattr(occupancy, "write_duct_brownfield", lambda project_id, bf_dir: None)
    monkeypatch.setattr(occupancy, "write_trench_brownfield", lambda project_id, bf_dir: [])

    assert main._brownfield_args(None, tmp_path, None) == []
    assert main._brownfield_args(None, tmp_path, "a-fresh-project") == []
