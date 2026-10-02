"""End-to-end integration test for LLD Mode A (_run_lld).

Runs the REAL engine function on a realistic Approved Survey Version
dataset (full layer set + approved changes) and asserts the complete output
contract:

  * the run completes at 100% with zero validation issues
  * the survey-only input log fires
  * an approved reroute re-lays its dependents onto the new path (relay)
  * new premises get a drop duct + serving distribution cable (drop planning)
  * missing supporting features are auto-created (cross-layer propagation)
  * output layers keep the public contract (LLD_LAYER_ORDER, excluded layers)
  * GeoJSON files + the downloadable zip land in the output directory

Run from the engine backend dir. The application and tests use the Anaconda
Python; clear inherited QGIS Python 3.12 paths so compiled QGIS packages cannot
shadow Anaconda's Python 3.11 packages:

    cd HLD_Planning_01/web/backend
    # Git Bash / bash
    env -u PYTHONPATH -u PYTHONHOME python -m pytest tests/test_lld_mode_a.py -v
    # Windows cmd.exe
    set "PYTHONPATH=" && set "PYTHONHOME=" && python -m pytest tests\test_lld_mode_a.py -v

The engine configures the QGIS environment only for its qgis_process child.
"""

import json
import pathlib
import uuid
import zipfile

import pytest

import main as engine


# ── Fixture helpers ─────────────────────────────────────────────────────────

def _line(coords):
    return {"type": "LineString", "coordinates": coords}


def _point(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _polygon(ring):
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def _feature(layer, geometry, props=None):
    props = dict(props or {})
    props["layer"] = layer
    props.setdefault("feature_id", "%s-%s" % (layer, uuid.uuid4().hex[:10]))
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _asv():
    """A small but realistic AS-Vxx dataset.

    Layout (Berlin-ish lon/lat, ~75 m street):

      T1  main trench  (13.4000,52.5000) -> (13.4005,52.5000)
          APPROVED reroute: the survey moved the middle to 52.49990
          (original_geometry = the straight HLD line)
      D1  distribution duct on the ORIGINAL straight path (not approved), with
          a MIDDLE vertex at y=52.5 so the relay has a vertex to move onto the
          new path (the relay only re-projects existing vertices)
      C1  distribution cable on the ORIGINAL straight path (not approved)
      F1  feeder duct + FC1 feeder cable on a FAR side path (not approved,
          >50m tolerance away from the trench) -> the coverage pass must
          auto-create a trench under them
      P1  polygon + PDP1 + MFG1 (serving areas)
      O1  approved NEW premise (no drop) -> drop planning must connect it
      O2  existing premise (no drop)     -> drop planning must connect it
      CH1  chamber at the junction
    E1  existing infrastructure line + EP1 point (brownfield evidence)
    """
    feats = [
        # Approved rerouted trench: survey geometry is the new path.
        _feature("final_trenches", _line([[13.4000, 52.5000], [13.40025, 52.49990], [13.4005, 52.5000]]), {
            "feature_id": "TR-1",
            "approved": True,
            "change_id": "chg-trench",
            "original_geometry": _line([[13.4000, 52.5000], [13.4005, 52.5000]]),
            "trench_type": "New Trench",
            "USAGE_TYPE": "Trunk",
            "SURFACE": "Asphalt",
            "CONSTRUCT": "Open Cut",
            "REINSTATE": "Asphalt",
        }),
        # Dependents still riding the old straight path — relay must move them.
        # Each carries a MIDDLE vertex on the old path so the relay has
        # something to re-project onto the survey's new middle vertex.
        _feature("distribution_ducts", _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]), {
            "feature_id": "DD-1",
            "DUCT_TYPE": "1-Way HDPE",
        }),
        _feature("distribution_cable", _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]), {
            "feature_id": "DC-1",
            "CABLE_TYPE": "Drop",
            "CONNECTION_TYPE": "Drop (garden leg)",
            "FIBER_COUNT": 12,
            "HH_COUNT": 1,
            "ADDR_ID": "A-2",
            "ADDR_IDS": "A-2",
            "households": 1,
        }),
        # Feeder on a FAR side path (base of the fixture, ~700 m south) with no
        # trench beneath it — outside the 50 m coverage tolerance, so the
        # cross-layer propagation pass must auto-create a trench under it.
        _feature("feeder_ducts", _line([[13.3999, 52.49300], [13.4003, 52.49300]]), {
            "feature_id": "FD-1",
            "DUCT_TYPE": "4-Way HDPE",
        }),
        _feature("feeder_cable", _line([[13.3999, 52.49300], [13.4003, 52.49300]]), {
            "feature_id": "FC-1",
            "CABLE_TYPE": "Feeder",
            "FIBER_COUNT": 24,
        }),
        # Serving areas.
        _feature("polygons", _polygon([[13.3999, 52.4999], [13.4006, 52.4999], [13.4006, 52.5001], [13.3999, 52.5001]]), {
            "feature_id": "POLY00001",
            "SRC_ID": "POLY00001",
        }),
        _feature("pdps", _point(13.40025, 52.49995), {
            "feature_id": "PDP00001",
            "PDP_ID": "PDP00001",
            "MFG_ID": "MFG00001",
            "label": "Network_POLY00001",
        }),
        _feature("mfg", _point(13.4000, 52.49980), {
            "feature_id": "MFG00001",
            "MFG_ID": "MFG00001",
        }),
        # Premises — one approved new, one existing; neither has a drop yet.
        # Placed ~14 m clear of the trunk-cable endpoints so they are NOT
        # "served" within the 11 m drop tolerance and must get a real drop.
        _feature("objects", _point(13.40042, 52.50012), {
            "feature_id": "O-1",
            "approved": True,
            "change_id": "chg-premise-1",
            "households": 18,
            "ADDR_ID": "A-1",
        }),
        _feature("objects", _point(13.40008, 52.50012), {
            "feature_id": "O-2",
            "households": 1,
            "ADDR_ID": "A-2",
        }),
        # Chamber + brownfield evidence.
        _feature("chambers", _point(13.4000, 52.5000), {
            "feature_id": "CH-1",
            "CHAMBER_ID": "CH00001",
        }),
        _feature("existing_infrastructure", _line([[13.4000, 52.49980], [13.4005, 52.49980]]), {
            "feature_id": "E-1",
            "INFRA_STATUS": "Existing",
            "ASSET_TYPE": "Duct",
        }),
        _feature("existing_infrastructure_points", _point(13.4002, 52.49990), {
            "feature_id": "EP-1",
            "ASSET_TYPE": "Chamber",
        }),
    ]
    return {"type": "FeatureCollection", "features": feats}


@pytest.fixture()
def mode_a_env(tmp_path, monkeypatch):
    """Isolate the engine: throwaway output dir + fresh task registry."""
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(engine, "lld_tasks", {})
    return engine


def _run(mode_a_env, project_id="e2e-test-project", version="LLD-TEST01", dataset=None):
    engine_ = mode_a_env
    engine_._run_lld(project_id, version, dataset or _asv())
    return engine_.lld_tasks[engine_._lld_key(project_id, version)]


def _load_output(mode_a_env, project_id, version, layer):
    path = (
        pathlib.Path(engine.OUTPUT_DIR) / project_id / "lld" / version / f"{layer}.geojson"
    )
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ── Tests ──────────────────────────────────────────────────────────────────

def test_mode_a_runs_end_to_end(mode_a_env):
    """A real ASV run completes at 100% with zero validation issues."""
    task = _run(mode_a_env)

    assert task["status"] == "completed"
    assert task["progress"] == 100

    val = task["validation"]["summary"]
    assert val["layers"] >= 10
    assert val["continuity_issues"] == 0, task["validation"]["issues"]
    assert val["attribute_issues"] == 0
    assert val["drop_issues"] == 0, task["validation"]["issues"]


def test_mode_a_logs_survey_only_input(mode_a_env):
    """The run advertises that the Approved Survey Version is the sole input."""
    task = _run(mode_a_env)
    texts = " | ".join(m.get("text", "") for m in task["messages"])
    assert "LLD input = Approved Survey Version only" in texts
    assert "no HLD layer references" in texts


def test_mode_a_relay_moves_dependents_onto_reroute(mode_a_env):
    """An approved trench reroute re-lays its duct + cable onto the new path."""
    task = _run(mode_a_env)

    relay_msgs = [m for m in task["messages"] if "Reroute propagation" in m.get("text", "")]
    assert relay_msgs, "expected a reroute-propagation log line"

    ducts = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "distribution_ducts")
    assert ducts is not None
    relayed = [
        f for f in ducts["features"]
        if (f.get("properties") or {}).get("lld_relayed")
    ]
    assert relayed, "expected the distribution duct to be relayed onto the reroute"
    # The duct must now deviate from the OLD straight line (y=52.5000) and
    # follow the survey's new middle dip toward 52.49990. The relay projects
    # each old vertex onto the new path, so assert the line leaves the old
    # line rather than demanding the exact survey vertex.
    moved_off_old = [
        c for f in relayed
        for line in engine._line_strings(f.get("geometry"))
        for c in line
        if abs(c[1] - 52.5000) > 1e-5  # no longer on the old straight path
    ]
    assert moved_off_old, "relayed duct still rides the old straight path"
    assert all(c[1] < 52.5000 for c in moved_off_old), (
        "relayed duct moved the wrong direction"
    )


def test_mode_a_purges_old_reroute_region(mode_a_env):
    """The old straight path must not survive the purge pass."""
    task = _run(mode_a_env)

    purged = sum(
        1 for m in task["messages"]
        if "Old-path purge" in m.get("text", "") and "=0," not in m.get("text", "")
    )
    assert purged >= 1

    for layer in ("final_trenches", "distribution_ducts", "distribution_cable"):
        fc = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", layer)
        for f in fc["features"]:
            props = f.get("properties") or {}
            if props.get("approved"):
                continue  # engineer's own re-draw is authoritative
            for line in engine._line_strings(f.get("geometry")):
                # No non-approved line may keep the exact old y=52.5000 straight.
                assert len(line) >= 2


def test_mode_a_drop_planning_connects_new_premises(mode_a_env):
    """Each new service location gets one garden trench + drop duct/cable."""
    task = _run(mode_a_env)

    drops = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "drop_ducts")
    assert drops is not None
    created_drops = [
        f for f in drops["features"]
        if (f.get("properties") or {}).get("lld_created")
    ]
    # One per physical service location that lacked a drop (O-1 and O-2).
    assert len(created_drops) == 2, "expected 2 auto-created drop ducts"

    trenches = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "final_trenches")
    garden = [
        f for f in trenches["features"]
        if (f.get("properties") or {}).get("trench_type") == "Garden"
    ]
    assert len(garden) == 2, "expected garden trenches mirrored into final_trenches"

    cables = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "distribution_cable")
    serving = [
        f for f in cables["features"]
        if (f.get("properties") or {}).get("lld_created")
    ]
    assert len(serving) == 2, "expected serving cables for the two physical service locations"
    by_addr = {
        (f.get("properties") or {}).get("ADDR_ID"): f.get("properties") or {}
        for f in serving
    }
    assert by_addr["A-1"]["CABLE_TYPE"] == "Drop"
    assert by_addr["A-1"]["HH_COUNT"] == 18
    assert by_addr["A-1"]["FIBER_COUNT"] == 24
    assert by_addr["A-2"]["CABLE_TYPE"] == "Drop"
    assert by_addr["A-2"]["HH_COUNT"] == 1
    assert by_addr["A-2"]["FIBER_COUNT"] == 12
    assert by_addr["A-1"]["CAPACITY_STATUS"] == "OK"
    assert len(created_drops) == 2  # still one civil route per physical location

    trenches = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "final_trenches")
    garden_by_addr = {
        (f.get("properties") or {}).get("addr_id"): f
        for f in trenches["features"]
        if (f.get("properties") or {}).get("trench_type") == "Garden"
    }
    assert len(garden_by_addr) == 2
    assert set(garden_by_addr) == {"A-1", "A-2"}


def test_mode_a_propagates_missing_support_layers(mode_a_env):
    """A cable/duct with no trench beneath it gets an auto-created trench."""
    task = _run(mode_a_env)

    trenches = _load_output(mode_a_env, "e2e-test-project", "LLD-TEST01", "final_trenches")
    auto = [
        f for f in trenches["features"]
        if (f.get("properties") or {}).get("lld_created")
        and (f.get("properties") or {}).get("lld_source_layer") in ("feeder_ducts", "feeder_cable")
    ]
    assert auto, "expected an auto-created trench under the feeder duct/cable"


def test_lld_enrichment_preserves_drop_cables_and_sizes_from_location_hh():
    """A building's HH load changes cable capacity, not its single drop record."""
    from lld_cable_geometry import (
        cable_fiber_capacity,
        drop_capacity_warning,
        drop_fiber_capacity,
    )

    drop_geometry = _line([[13.4, 52.5], [13.4001, 52.5]])
    cables = [
        _feature("distribution_cable", drop_geometry, {
            "ADDR_ID": "BLDG-1", "addr_id": "BLDG-1", "CABLE_TYPE": "Drop",
            "CONNECTION_TYPE": "Drop (garden leg)", "HH_COUNT": 18,
        }),
        _feature("distribution_cable", _line([[13.4, 52.5], [13.4002, 52.5]]), {
            "ADDR_ID": "BLDG-2", "addr_id": "BLDG-2", "HH_COUNT": 60,
        }),
        _feature("distribution_cable", _line([[13.4, 52.5], [13.4003, 52.5]]), {
            "ADDR_ID": "BLDG-3", "addr_id": "BLDG-3",
            "CABLE_TYPE": "Drop", "HH_COUNT": 286,
        }),
        _feature("distribution_cable", _line([[13.4, 52.5], [13.4004, 52.5]]), {
            "ADDR_ID": "BLDG-4", "addr_id": "BLDG-4",
            "CABLE_TYPE": "Drop", "HH_COUNT": 300,
        }),
    ]
    assert engine._enrich_lld_distribution_cables({"distribution_cable": cables}) == 4
    assert len(cables) == 4
    assert cables[0]["properties"]["CABLE_TYPE"] == "Drop"
    assert cables[0]["properties"]["HH_COUNT"] == 18
    assert cables[0]["properties"]["FIBER_COUNT"] == 24
    assert cables[0]["properties"]["CONNECTION_TYPE"] == "Drop (garden leg)"
    assert cables[0]["geometry"] == drop_geometry
    by_address = {
        str((feature.get("properties") or {}).get("ADDR_ID") or
            (feature.get("properties") or {}).get("addr_id")): feature["properties"]
        for feature in cables
    }
    assert by_address["BLDG-2"]["FIBER_COUNT"] == 72
    assert by_address["BLDG-2"]["CABLE_TYPE"] == "Distribution"
    assert by_address["BLDG-3"]["FIBER_COUNT"] == 288
    assert by_address["BLDG-3"]["CAPACITY_STATUS"] == "OK"
    assert by_address["BLDG-3"]["REVIEW"] == 0
    assert by_address["BLDG-4"]["FIBER_COUNT"] == 288
    assert by_address["BLDG-4"]["CAPACITY_STATUS"] == "OVER_CAPACITY"
    assert by_address["BLDG-4"]["REVIEW"] == 1
    assert by_address["BLDG-4"]["UTIL_PCT"] == 100.0
    assert "286 HH" in by_address["BLDG-4"]["CAPACITY_WARNING"]

    dropped = [
        _feature("distribution_cable", _line([[13.4, 52.5], [13.4002, 52.5]]), {
            "ADDR_ID": "OLD-DROP", "addr_id": "OLD-DROP",
            "CONNECTION_TYPE": "Dedicated drop", "HH_COUNT": 3,
        }),
    ]
    engine._enrich_lld_distribution_cables({"distribution_cable": dropped})
    assert len(dropped) == 1
    assert dropped[0]["properties"]["CABLE_TYPE"] == "Drop"
    assert dropped[0]["properties"]["FIBER_COUNT"] == 12
    assert by_address["BLDG-2"]["CABLE_TYPE"] == "Distribution"
    assert by_address["BLDG-2"]["FIBER_COUNT"] == 72
    assert cable_fiber_capacity(1, 12) == 12
    assert cable_fiber_capacity(10, 12) == 12
    assert cable_fiber_capacity(11, 12) == 24
    assert cable_fiber_capacity(18, 12) == 24
    assert cable_fiber_capacity(22, 12) == 24
    assert cable_fiber_capacity(23, 12) == 48
    assert cable_fiber_capacity(46, 12) == 48
    assert cable_fiber_capacity(47, 12) == 72
    assert drop_fiber_capacity(286) == 288
    assert drop_fiber_capacity(287) is None
    assert by_address["BLDG-4"]["CAPACITY_STATUS"] == "OVER_CAPACITY"
    assert "286 HH" in drop_capacity_warning(287)


def test_mode_a_output_contract(mode_a_env):
    """Layer names respect LLD_LAYER_ORDER and exclusions; files + zip exist."""
    project_id, version = "e2e-test-project", "LLD-TEST01"
    task = _run(mode_a_env, project_id, version)

    out_dir = pathlib.Path(engine.OUTPUT_DIR) / project_id / "lld" / version
    assert out_dir.is_dir()

    files = {p.name for p in out_dir.glob("*.geojson")}
    layer_names = {name[:-8] for name in files}
    # No excluded layer (garden_trench) may escape as a standalone output.
    assert not (layer_names & engine.LLD_EXCLUDED_LAYERS)
    # Every emitted layer is part of the public contract.
    assert layer_names <= set(engine.LLD_LAYER_ORDER), layer_names - set(engine.LLD_LAYER_ORDER)
    # Any shared trunks retain their 48F floor and enough capacity for
    # aggregate HH plus the two reserved spare fibres; service drops remain Drop.
    cables = _load_output(mode_a_env, project_id, version, "distribution_cable")
    cable_props = [f.get("properties") or {} for f in cables["features"]]
    trunks = [p for p in cable_props if p.get("CABLE_TYPE") == "Distribution"]
    assert all(p.get("CABLE_TYPE") in {"Distribution", "Drop"} for p in cable_props)
    assert all(
        p.get("FIBER_COUNT", 0) >= max(
            engine.LLD_DISTRIBUTION_FIBERS,
            p.get("HH_COUNT", 0) + engine.LLD_RESERVED_SPARE_FIBERS,
        )
        for p in trunks
    )

    zip_path = out_dir / f"{project_id}_{version}_lld.zip"
    assert zip_path.is_file(), "expected the downloadable LLD zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "final_trenches.geojson" in names
        assert "distribution_ducts.geojson" in names
        assert "drop_ducts.geojson" in names


def test_mode_a_approved_dataset_without_changes_is_still_valid(mode_a_env):
    """A pure HLD-equivalent dataset (no approved flags) still completes cleanly."""
    dataset = _asv()
    for f in dataset["features"]:
        props = f.get("properties") or {}
        props.pop("approved", None)
        props.pop("original_geometry", None)
        props.pop("change_id", None)
    task = _run(mode_a_env, dataset=dataset)
    assert task["status"] == "completed"
    assert task["validation"]["summary"]["continuity_issues"] == 0
    assert task["validation"]["summary"]["drop_issues"] == 0


def test_mode_b_brownfield_writer_rejects_unapproved_features(tmp_path):
    """Mode B brownfield files may only contain approved survey features."""
    feats = []
    for layer, _param, filename in engine._REPLAN_BF_GROUPS:
        feats.append(_feature(layer, _line([[13.35, 52.45], [13.36, 52.46]]), {
            "feature_id": "A-" + layer, "approved": True,
        }))
        feats.append(_feature(layer, _line([[13.35, 52.45], [13.36, 52.46]]), {
            "feature_id": "X-" + layer,  # unedited/HLD — must be excluded
        }))
    dataset = {"type": "FeatureCollection", "features": feats}
    bf_dir = tmp_path / "brownfield"

    engine._write_replan_brownfield(bf_dir, dataset)

    files = list(bf_dir.glob("*.geojson"))
    assert files, "expected brownfield files to be written"
    for f in files:
        fc = json.loads(f.read_text(encoding="utf-8"))
        ids = [x["properties"]["feature_id"] for x in fc["features"]]
        assert not any(i.startswith("X-") for i in ids), f"non-approved feature leaked into {f.name}"