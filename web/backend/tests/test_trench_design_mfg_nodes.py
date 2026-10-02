"""Add focused regression checks for the latest designer-node and served-premises edits.

This file is intentionally thin:
- it does not re-run the full engine pipeline
- it only asserts the new public behaviors we changed in this round
"""

import sys
import pathlib

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import trench_design as td


def _run(coords, tier="Feeder", mfg=None):
    r = td.Run(coords=list(coords), tier=tier)
    r.mfg = mfg
    return r


def test_place_nodes_keeps_run_mfg_on_pure_run_nodes():
    run = _run([(0, 0), (10, 0), (20, 0), (300, 0)], mfg="MFG-7")
    nodes = td.place_nodes([run], [], [], td.Params())
    pdp_mfgs = {n["NODE_TYPE"]: n.get("mfg") for n in nodes if n.get("NODE_TYPE") in ("PULL",)}
    assert any(v == "MFG-7" for v in pdp_mfgs.values())
    assert all(v == "MFG-7" for v in pdp_mfgs.values())


def test_place_nodes_propagates_mfg_from_splitter_to_pdp_node():
    run = _run([(0, 0), (100, 0)], mfg="MFG-A")
    pdp = {"x": 70.0, "y": 0.0, "PDP_ID": "PDP-2", "MFG_ID": "MFG-B"}
    nodes = td.place_nodes([run], [], [pdp], td.Params())
    pdp_node = next(n for n in nodes if n["NODE_TYPE"] == "PDP")
    assert pdp_node["mfg"] == "MFG-B"


def test_place_nodes_fills_node_mfg_from_nearest_run_when_run_is_none():
    run = _run([(0, 0), (100, 0)], mfg="MFG-X")
    nodes = td.place_nodes([run], [], [], td.Params(), junction_points=[(100.0, 0.0)])
    junction = next(n for n in nodes if n["NODE_TYPE"] == "JUNCTION")
    assert junction["mfg"] == "MFG-X"


def test_final_node_rows_include_mfg_id_from_node_object():
    run = _run([(0, 0), (10, 0), (20, 0), (300, 0)], mfg="MFG-99")
    nodes = td.place_nodes([run], [], [], td.Params())
    for n in nodes:
        n["NODE_ID"] = "TN-00001"
    node_rows = []
    for n in nodes:
        node_rows.append({
            "NODE_ID": n["NODE_ID"], "NODE_TYPE": n["NODE_TYPE"],
            "PRIORITY": n["PRIORITY"], "PDP_ID": n.get("ref") or None,
            "MFG_ID": n.get("mfg"),
            "X": round(n["x"], 2), "Y": round(n["y"], 2),
            "geom": td.ogr.CreateGeometryFromWkt(f"POINT({n['x']} {n['y']})"),
        })
    assert any(r["MFG_ID"] == "MFG-99" for r in node_rows)
