"""HLD cable attributes must reach the LLD output unchanged.

The HLD cable stage (``HLDPlanning/algorithms/cable_layer.py``) writes two
different schemas: the feeder cable carries the trunk/branch topology
(``cable_id``, ``TRUNK_NO``, ``BRANCH_NO``, ``BRANCH_ROLE``, ``PDP_IDS``,
``PDP_COUNT``, ``SPLIT_MODULES``, ``LENGTH_LIMIT_M``, ``LENGTH_CAPPED``,
``EXTENDED_FROM_PDP``, ``EXTENSION_LENGTH_M`` …), the distribution cable
carries the per-premise/drop contract (``addr_id``, ``ADDR_IDS``, ``HH_COUNT``,
``CONNECTION_TYPE``, ``SIDE``, ``PDP_ID`` …). Both ride through the Approved
Survey Version into the LLD, where three passes touch cable features:

  * ``_relay_dependents``       re-lays a cable onto an approved reroute
  * ``regroup_distribution_cables`` merges co-routed distribution cables
  * ``_enrich_lld_distribution_cables`` recomputes the capacity fields

None of them may drop the design-topology attributes, or the LLD design loses
the trunk/branch structure the HLD computed and every downstream reader (BOQ
grouping, jointing, capacity reports) sees a flat cable layer.

These tests drive the real ``_run_lld`` and compare the written GeoJSON
against the exact attribute values fed in.

Run (from HLD_Planning_01/web/backend):

    env -u PYTHONPATH -u PYTHONHOME python -m pytest tests/test_lld_cable_attributes.py -v
"""

import json
import pathlib
import uuid

import pytest

import main as engine


PROJECT = "cable-attr-project"
VERSION = "LLD-CABLE01"

# Every field the HLD feeder cable stage writes (cable_layer.py, `fields`).
FEEDER_HLD_SCHEMA = (
    "cable_id", "CABLE_TYPE", "FIBER_COUNT", "SPLIT_MODULES", "UTIL_PCT",
    "SPARE_PCT", "CAPACITY_STATUS", "CAPACITY_WARNING",
    "RESERVED_SPARE_FIBERS", "AVAILABLE_FIBERS", "PDP_IDS", "PDP_COUNT",
    "SPLICE_OF", "SPLICE_POINT", "length_m", "POLYGON_ID", "MFG_ID", "REVIEW",
    "TRUNK_NO", "LENGTH_LIMIT_M", "LENGTH_CAPPED", "BRANCH_ROLE",
    "BRANCH_NO", "START_NODE", "END_NODE", "PARENT_CABLE_ID",
    "EXTENDED_FROM_PDP", "EXTENSION_LENGTH_M",
)

# Every field the HLD distribution cable stage writes (cable_layer.py,
# `out_fields`).
DISTRIBUTION_HLD_SCHEMA = (
    "addr_id", "ADDR_IDS", "hhs", "HH_COUNT", "FIBER_COUNT",
    "RESERVED_SPARE_FIBERS", "AVAILABLE_FIBERS", "ACTIVE_FIBERS",
    "CAPACITY_STATUS", "CAPACITY_WARNING", "REVIEW", "UTIL_PCT", "SPARE_PCT",
    "CONNECTION_TYPE", "CABLE_TYPE", "length_m", "POLYGON_ID", "PDP_ID",
    "MFG_ID", "SIDE",
)

# The LLD is allowed to recompute these on a distribution cable: they are the
# LLD's own capacity contract (48F catalogue, HH re-count after regrouping),
# not HLD topology.
DISTRIBUTION_RECOMPUTED = frozenset(
    {
        "HH_COUNT", "hhs", "FIBER_COUNT", "ACTIVE_FIBERS", "AVAILABLE_FIBERS",
        "UTIL_PCT", "CAPACITY_STATUS", "CAPACITY_WARNING", "REVIEW",
        "RESERVED_SPARE_FIBERS", "CONNECTION_TYPE", "ADDR_IDS", "addr_id",
        # A merged cable is longer than either member, so its length is
        # recomputed — in metres (see the assertion below).
        "length_m",
    }
)

FEEDER_VALUES = {
    "cable_id": 7,
    "CABLE_TYPE": "Feeder",
    "FIBER_COUNT": 48,
    "SPLIT_MODULES": 9,
    "UTIL_PCT": 18.8,
    "SPARE_PCT": 81.2,
    "CAPACITY_STATUS": "OK",
    "CAPACITY_WARNING": "",
    "RESERVED_SPARE_FIBERS": 2,
    "AVAILABLE_FIBERS": 37,
    "PDP_IDS": "PDP00001,PDP00002",
    "PDP_COUNT": 2,
    "SPLICE_OF": "MFG00001/RIGHT",
    "SPLICE_POINT": "CPL-0007",
    "length_m": 123.4,
    "POLYGON_ID": "POLY00001",
    "MFG_ID": "MFG00001",
    "REVIEW": 0,
    "TRUNK_NO": 2,
    "LENGTH_LIMIT_M": 1000.0,
    "LENGTH_CAPPED": 1,
    "BRANCH_ROLE": "Grouped corridor",
    "BRANCH_NO": 3,
    "START_NODE": "MFG00001",
    "END_NODE": "PDP00001",
    "PARENT_CABLE_ID": "MFG00001/RIGHT",
    "EXTENDED_FROM_PDP": "PDP00009",
    "EXTENSION_LENGTH_M": 42.5,
}

DISTRIBUTION_VALUES = {
    "addr_id": "A-1",
    "ADDR_IDS": "A-1",
    "hhs": "5",
    "HH_COUNT": 5,
    "FIBER_COUNT": 288,
    "RESERVED_SPARE_FIBERS": 2,
    "AVAILABLE_FIBERS": 281,
    "ACTIVE_FIBERS": 5,
    "CAPACITY_STATUS": "OK",
    "CAPACITY_WARNING": "",
    "REVIEW": 0,
    "UTIL_PCT": 1.7,
    "SPARE_PCT": 98.3,
    "CONNECTION_TYPE": "Shared trunk + branches",
    "CABLE_TYPE": "Distribution",
    "length_m": 17.5,
    "POLYGON_ID": "POLY00001",
    "PDP_ID": "PDP00001",
    "MFG_ID": "MFG00001",
    "SIDE": "Left",
}


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


def _dataset(*, reroute=False, approved_side=None):
    """A connected AS-Vxx dataset whose cables carry the full HLD schema.

    Corridor: (13.4000, 52.5000) -> (13.4005, 52.5000), one trench with a
    distribution duct, a feeder duct and both cables lying on it, so no
    supporting feature has to be auto-created.

    ``reroute`` makes the trench an APPROVED reroute (survey middle vertex
    dips to 52.49990) so the relay pass re-lays the cables onto it.
    """
    middle = [13.40025, 52.49990] if reroute else [13.40025, 52.5000]
    trench_props = {
        "feature_id": "TR-1",
        "trench_type": "New Trench",
        "CONSTRUCT": "Open Cut",
    }
    if reroute:
        trench_props.update(
            {
                "approved": True,
                "change_id": "chg-trench",
                "original_geometry": _line(
                    [[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]
                ),
            }
        )

    cable_1 = dict(DISTRIBUTION_VALUES)
    if approved_side:
        # An approved survey attribute edit: the planner changed the side.
        cable_1.update({"approved": True, "change_id": "chg-side", "SIDE": approved_side})

    feats = [
        _feature(
            "final_trenches",
            _line([[13.4000, 52.5000], middle, [13.4005, 52.5000]]),
            trench_props,
        ),
        _feature(
            "distribution_ducts",
            _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "DD-1", "DUCT_TYPE": "1-Way HDPE"},
        ),
        _feature(
            "feeder_ducts",
            _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]),
            {"feature_id": "FD-1", "DUCT_TYPE": "4-Way HDPE"},
        ),
        _feature(
            "feeder_cable",
            _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4005, 52.5000]]),
            dict(FEEDER_VALUES, feature_id="FC-1"),
        ),
        # Two co-routed distribution cables with the same PDP, polygon and
        # footway end -> regroup_distribution_cables must merge them.
        _feature(
            "distribution_cable",
            _line([[13.4000, 52.5000], [13.40025, 52.5000], [13.4004, 52.5000]]),
            dict(cable_1, feature_id="DC-1"),
        ),
        _feature(
            "distribution_cable",
            _line([[13.4005, 52.5000], [13.4004, 52.5000]]),
            dict(
                DISTRIBUTION_VALUES,
                feature_id="DC-2",
                addr_id="A-2",
                ADDR_IDS="A-2",
                hhs="7",
                HH_COUNT=7,
                ACTIVE_FIBERS=7,
            ),
        ),
        _feature(
            "polygons",
            _polygon(
                [[13.3999, 52.4999], [13.4006, 52.4999], [13.4006, 52.5001], [13.3999, 52.5001]]
            ),
            {"feature_id": "POLY00001", "SRC_ID": "POLY00001"},
        ),
        _feature(
            "pdps",
            _point(13.40025, 52.50000),
            {"feature_id": "PDP00001", "PDP_ID": "PDP00001", "MFG_ID": "MFG00001",
             "label": "Network_POLY00001"},
        ),
        _feature("mfg", _point(13.4000, 52.49990), {"feature_id": "MFG00001", "MFG_ID": "MFG00001"}),
        # The premise sits on the merged cable's end -> it is already served,
        # so drop planning must not invent a second cable for it.
        _feature("objects", _point(13.4004, 52.5000), {"feature_id": "O-1", "ADDR_ID": "A-1", "households": 5}),
        _feature("chambers", _point(13.4000, 52.5000), {"feature_id": "CH-1", "CHAMBER_ID": "CH00001"}),
    ]
    return {"type": "FeatureCollection", "features": feats}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(engine, "lld_tasks", {})
    return engine


def _run(env, name="run", dataset=None):
    engine._run_lld(PROJECT, name, dataset or _dataset())
    return env.lld_tasks[env._lld_key(PROJECT, name)]


def _layer(env, name, layer):
    path = pathlib.Path(engine.OUTPUT_DIR) / PROJECT / "lld" / name / f"{layer}.geojson"
    assert path.exists(), f"{layer} was not written"
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def _by_id(features, feature_id):
    for f in features:
        if (f.get("properties") or {}).get("feature_id") == feature_id:
            return f
    return None


def _assert_kept(props, expected, ignored=()):
    missing = [k for k in expected if k not in props]
    assert not missing, "attributes lost in the LLD output: %s" % sorted(missing)
    wrong = {
        k: (v, props[k])
        for k, v in expected.items()
        if k not in ignored and props[k] != v
    }
    assert not wrong, "attributes changed in the LLD output: %s" % wrong


def test_feeder_cable_keeps_every_hld_topology_attribute(env):
    """The whole HLD feeder schema survives the LLD run, value for value."""
    task = _run(env)
    assert task["status"] == "completed", task.get("error")

    cable = _by_id(_layer(env, "run", "feeder_cable"), "FC-1")
    assert cable is not None, "the feeder cable disappeared from the LLD output"
    _assert_kept(cable["properties"], FEEDER_VALUES)
    # Every schema field is present, not just the ones we sampled.
    assert set(FEEDER_HLD_SCHEMA) <= set(cable["properties"])


def test_merged_distribution_cable_keeps_its_design_attributes(env):
    """Regrouping two co-routed cables keeps the HLD attributes of the base."""
    task = _run(env, "run-regroup")
    assert task["status"] == "completed", task.get("error")

    cables = _layer(env, "run-regroup", "distribution_cable")
    survivors = [f for f in cables if (f.get("properties") or {}).get("feature_id") in {"DC-1", "DC-2"}]
    assert len(survivors) == 1, "co-routed cables were not regrouped into one feature"
    merged = survivors[0]
    props = merged["properties"]
    assert props["feature_id"] == "DC-1"

    # Topology/design attributes of the base cable are untouched.
    _assert_kept(props, DISTRIBUTION_VALUES, ignored=DISTRIBUTION_RECOMPUTED)
    assert set(DISTRIBUTION_HLD_SCHEMA) <= set(props)

    # The capacity contract is recomputed from the combined member set.
    assert props["HH_COUNT"] == 12
    assert props["ADDR_IDS"] == "A-1,A-2"
    assert merged["geometry"]["type"] == "MultiLineString"

    # The recomputed length must stay in metres: the two members are ~27 m and
    # ~7 m long, and a degree sum would come out as ~0.0005.
    assert 29.0 < props["length_m"] < 39.0, props["length_m"]


def test_relayed_cable_keeps_its_hld_attributes(env):
    """An approved reroute re-lays the cable without rewriting its attributes."""
    task = _run(env, "run-relay", _dataset(reroute=True))
    assert task["status"] == "completed", task.get("error")

    cables = _layer(env, "run-relay", "feeder_cable")
    cable = _by_id(cables, "FC-1")
    assert cable is not None
    assert cable["properties"].get("lld_relayed") is True, (
        "expected the feeder cable to follow the approved reroute"
    )
    _assert_kept(cable["properties"], FEEDER_VALUES)


def test_approved_attribute_edit_reaches_the_cable(env):
    """A planner-approved attribute change wins over the HLD value."""
    task = _run(env, "run-attr", _dataset(approved_side="Right"))
    assert task["status"] == "completed", task.get("error")

    cables = _layer(env, "run-attr", "distribution_cable")
    cable = _by_id(cables, "DC-1")
    assert cable is not None
    assert cable["properties"]["SIDE"] == "Right"
    # The rest of the schema is still intact after the overlay.
    _assert_kept(cable["properties"], DISTRIBUTION_VALUES, ignored=DISTRIBUTION_RECOMPUTED | {"SIDE"})
    assert cable["properties"].get("approved") is True
