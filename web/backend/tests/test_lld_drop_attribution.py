"""The LLD validator must judge drop service by attribution, not distance.

A drop duct that ends within tolerance of a premise is not necessarily *that*
premise's service: a neighbour's garden leg can sit on top of it. Continuity
only needs the geometry, but the served-premises register reads ``ADDR_ID``, so
the validator reports a premise whose only reaching drop belongs to another
building as its own issue (``drop_attribution``) instead of passing it.

These tests drive the real ``_run_lld`` on a dataset where two premises stand
4 m apart, so they are both inside the 11 m drop tolerance of a single drop:

  * misattributed  — one drop + cable for BLDG-A, nothing for BLDG-B
                     -> 1 drop_attribution issue, 0 drop issues
  * attributed     — BLDG-B gets its own drop + cable
                     -> 0 drop_attribution issues
  * shared         — one drop carrying ADDR_IDS "BLDG-A,BLDG-B"
                     -> 0 drop_attribution issues (one garden leg serves both)

Run (from HLD_Planning_01/web/backend):

    env -u PYTHONPATH -u PYTHONHOME python -m pytest tests/test_lld_drop_attribution.py -v
"""

import json
import pathlib
import uuid

import pytest

import main as engine


PROJECT = "drop-attr-project"

# The two premises: 4 m apart, both within DROP_CONNECT_TOL (11 m) of a drop
# that ends at P-1.
P1 = [13.40020, 52.50006]
P2 = [13.40020, 52.50010]
TAP_1 = [13.40020, 52.50000]
TAP_2 = [13.40025, 52.50000]


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


def _drop(prefix, tap, premise, addr_field, addr_value):
    """A garden drop duct + the serving cable that flows through it."""
    return [
        _feature(
            "drop_ducts",
            _line([tap, premise]),
            {
                "feature_id": "%s-DUCT" % prefix,
                "DUCT_TYPE": "Micro 7/3.5",
                "ADDR_ID": addr_value,
                "addr_id": addr_value,
                "ADDR_IDS": addr_value if addr_field == "ADDR_IDS" else "",
            },
        ),
        _feature(
            "distribution_cable",
            _line([tap, premise]),
            {
                "feature_id": "%s-CABLE" % prefix,
                "CABLE_TYPE": "Drop",
                "CONNECTION_TYPE": "Drop (garden leg)",
                "FIBER_COUNT": 12,
                "HH_COUNT": 1,
                "addr_id": addr_value,
                "ADDR_IDS": addr_value,
            },
        ),
    ]


def _dataset(kind="misattributed"):
    feats = [
        _feature(
            "final_trenches",
            _line([[13.4000, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "TR-1", "trench_type": "New Trench", "CONSTRUCT": "Open Cut"},
        ),
        _feature(
            "distribution_ducts",
            _line([[13.4000, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "DD-1", "DUCT_TYPE": "1-Way HDPE"},
        ),
        _feature(
            "feeder_ducts",
            _line([[13.4000, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "FD-1", "DUCT_TYPE": "4-Way HDPE"},
        ),
        _feature(
            "feeder_cable",
            _line([[13.4000, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "FC-1", "CABLE_TYPE": "Feeder", "FIBER_COUNT": 48},
        ),
        _feature(
            "polygons",
            _polygon([[13.3999, 52.4999], [13.4006, 52.4999], [13.4006, 52.5002], [13.3999, 52.5002]]),
            {"feature_id": "POLY00001", "SRC_ID": "POLY00001"},
        ),
        _feature(
            "pdps",
            _point(13.40025, 52.49995),
            {"feature_id": "PDP00001", "PDP_ID": "PDP00001", "MFG_ID": "MFG00001",
             "label": "Network_POLY00001"},
        ),
        _feature("mfg", _point(13.4000, 52.49980), {"feature_id": "MFG00001", "MFG_ID": "MFG00001"}),
        _feature("chambers", _point(13.4000, 52.5000), {"feature_id": "CH-1", "CHAMBER_ID": "CH00001"}),
        _feature("objects", _point(*P1), {"feature_id": "O-1", "ADDR_ID": "BLDG-A", "households": 1}),
        _feature("objects", _point(*P2), {"feature_id": "O-2", "ADDR_ID": "BLDG-B", "households": 1}),
    ]
    if kind == "shared":
        # One garden leg for a shared service location: it names both premises.
        feats += _drop("D1", TAP_1, P1, "ADDR_IDS", "BLDG-A,BLDG-B")
    else:
        feats += _drop("D1", TAP_1, P1, "ADDR_IDS", "BLDG-A")
    if kind == "attributed":
        feats += _drop("D2", TAP_2, P2, "ADDR_IDS", "BLDG-B")
    return {"type": "FeatureCollection", "features": feats}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(engine, "lld_tasks", {})
    return engine


def _run(env, name, kind):
    engine._run_lld(PROJECT, name, _dataset(kind))
    task = env.lld_tasks[env._lld_key(PROJECT, name)]
    assert task["status"] == "completed", task.get("error")
    return task


def _attribution_issues(task):
    return [i for i in task["validation"]["issues"] if i["type"] == "drop_attribution"]


def test_a_separate_premise_reached_by_a_neighbours_drop_is_reported(env):
    """BLDG-B is 4 m from BLDG-A's drop: continuity is fine, service is not."""
    task = _run(env, "run-misattributed", "misattributed")
    validation = task["validation"]

    # Geometry is continuous and every premise is "within reach" — the old
    # distance-only check passed this dataset.
    assert validation["summary"]["drop_issues"] == 0
    assert validation["summary"]["continuity_issues"] == 0

    issues = _attribution_issues(task)
    assert len(issues) == 1, validation["issues"]
    assert validation["summary"]["drop_attribution_issues"] == 1
    issue = issues[0]
    assert issue["feature_id"] == "O-2"
    assert "BLDG-B" in issue["message"]
    assert "BLDG-A" in issue["message"]


def test_a_premise_with_its_own_drop_is_not_reported(env):
    """A drop attributed to BLDG-B serves BLDG-B, however close the other is."""
    task = _run(env, "run-attributed", "attributed")
    assert task["validation"]["summary"]["drop_attribution_issues"] == 0, task["validation"]["issues"]
    assert task["validation"]["summary"]["drop_issues"] == 0


def test_one_drop_naming_both_premises_serves_both(env):
    """A shared service location attributes one drop to several premises."""
    task = _run(env, "run-shared", "shared")
    assert task["validation"]["summary"]["drop_attribution_issues"] == 0, task["validation"]["issues"]
    # ADDR_IDS carries both ids, so the second premise is attributed too.
    path = (
        pathlib.Path(engine.OUTPUT_DIR) / PROJECT / "lld" / "run-shared" / "drop_ducts.geojson"
    )
    ducts = json.loads(path.read_text(encoding="utf-8"))["features"]
    serviced = [
        f for f in ducts if (f.get("properties") or {}).get("feature_id") == "D1-DUCT"
    ][0]
    assert serviced["properties"]["ADDR_IDS"] == "BLDG-A,BLDG-B"
