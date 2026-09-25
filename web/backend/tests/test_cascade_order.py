"""Unit tests for the Phase C cascade order (TRENCH_DESIGN.md §6.1).

The pipeline used to run trench → cable → duct → chamber, which placed the
civil structures AFTER the routes that are supposed to end at them.  The
approved order is trench → chambers → ducts → cables: chambers realise the
designer's Trench_Nodes while the trench evidence is fresh, ducts are then
laid chamber-to-chamber inside the spans, and a cable can only be planned
where the civil work already exists.

These tests pin the load-bearing facts of that reorder from source (the
algorithm module cannot be imported without QGIS):

  * the physical stage order inside ``execute_pipeline``;
  * the step indices and the stage lists the status page reads from disk;
  * the C4 wiring — Trench_Nodes saved by the trench stage and bound into
    the chamber stage as INPUT_TRENCH_NODES;
  * the two normalization passes (trenches before ducts, cables/ducts after
    the cable stage).

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_cascade_order.py -v
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

ONECLICK_SRC = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "oneclick.py").read_text(
    encoding="utf-8"
)

import main  # noqa: E402  (engine backend app — same import the recovery tests use)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _execute_pipeline_src() -> str:
    """The body of execute_pipeline only — stage order lives there."""
    start = ONECLICK_SRC.index("def execute_pipeline")
    # The next top-level def after it bounds the method body.
    m = re.search(r"\n    def ", ONECLICK_SRC[start:])
    end = start + m.start() if m else len(ONECLICK_SRC)
    return ONECLICK_SRC[start:end]


def _stage_offset(body: str, header: str) -> int:
    idx = body.index(header)
    assert idx >= 0, f"stage header {header!r} missing from execute_pipeline"
    return idx


# ── physical order inside execute_pipeline ───────────────────────────────


def test_the_cascade_runs_trench_chamber_duct_cable():
    body = _execute_pipeline_src()
    trench = _stage_offset(body, "# --- Trench Layer ---")
    chamber = _stage_offset(body, "# --- Chamber Layer (civil structures) ---")
    duct = _stage_offset(body, "# --- Duct Layer ---")
    cable = _stage_offset(body, "# --- Cable Layer (planned last")
    assert trench < chamber < duct < cable, (
        "cascade order must be trench → chambers → ducts → cables"
    )


def test_step_indices_follow_the_new_order():
    """QgsProcessingMultiStepFeedback reports progress by step index — they
    must count up in the order the stages actually run."""
    body = _execute_pipeline_src()
    steps = [
        int(m.group(1))
        for m in re.finditer(r"steps\.setCurrentStep\((\d+)\)", body)
    ]
    # 0 brownfield … 4 trench, 5 chamber, 6 duct, 7 cable, 8+ pole/aerial.
    assert steps == sorted(steps), f"step indices out of order: {steps}"
    for header, expected in (
        ("# --- Trench Layer ---", 4),
        ("# --- Chamber Layer (civil structures) ---", 5),
        ("# --- Duct Layer ---", 6),
        ("# --- Cable Layer (planned last", 7),
    ):
        seg = body[_stage_offset(body, header):]
        m = re.search(r"steps\.setCurrentStep\((\d+)\)", seg)
        assert m and int(m.group(1)) == expected, header


# ── the status page reads the same order from disk ───────────────────────


def _marker_index(name: str) -> int:
    names = [n for n, _ in main._STAGE_OUTPUT_MARKERS]
    assert name in names, f"{name} missing from _STAGE_OUTPUT_MARKERS"
    return names.index(name)


def test_stage_markers_put_duct_files_before_cable_files():
    """A restarted engine infers the run's position by walking the marker
    files in write order — ducts are now written first."""
    assert _marker_index("Duct Layer") < _marker_index("Cable Layer")
    assert main.PIPELINE_STAGES.index("Duct Layer") < \
        main.PIPELINE_STAGES.index("Cable Layer")
    assert main.PIPELINE_STAGES == [n for n, _ in main._STAGE_OUTPUT_MARKERS], (
        "PIPELINE_STAGES and _STAGE_OUTPUT_MARKERS must list the same stages "
        "in the same order — the status page mixes them"
    )


# ── C4: the designer's nodes reach the chamber stage ─────────────────────


def test_the_trench_stage_publishes_its_structural_nodes():
    body = _execute_pipeline_src()
    save = body[body.index("# The structural nodes the designer placed"):]
    assert 'results["trench_nodes"] = self._save_layer_to_gpkg(' in save
    assert '"Trench_Nodes.gpkg"' in save
    # …and the save happens while the trench stage is current (step 4), i.e.
    # before the chamber stage consumes it.
    trench_to_chamber = body[
        _stage_offset(body, "# --- Trench Layer ---"):
        _stage_offset(body, "# --- Chamber Layer (civil structures) ---")
    ]
    assert 'results["trench_nodes"]' in trench_to_chamber


def test_the_chamber_stage_binds_the_designer_nodes_as_its_primary_source():
    run_chamber = ONECLICK_SRC[
        ONECLICK_SRC.index("def run_chamber_layer"):]
    run_chamber = run_chamber[:run_chamber.index("\n    def ")]
    assert '"INPUT_TRENCH_NODES"' in run_chamber
    assert 'results.get("trench_nodes")' in run_chamber
    # The duct evidence rules cannot fire before the duct stage — the binding
    # must still be passed (as an optional input) rather than dropped.
    assert "INPUT_FEEDER_DUCTS" in run_chamber


# ── normalization passes ─────────────────────────────────────────────────


def test_trenches_are_normalized_at_chambers_before_the_duct_stage():
    """One chamber-to-chamber component: the trench layers are cut at the
    chambers BEFORE ducts/cables are built on them."""
    body = _execute_pipeline_src()
    chamber_seg = body[
        _stage_offset(body, "# --- Chamber Layer (civil structures) ---"):
        _stage_offset(body, "# --- Duct Layer ---")
    ]
    assert '"Trench layers normalized at chambers' in chamber_seg
    assert '"Final_Trenches.gpkg"' in chamber_seg
    # The trench pass must NOT try to split cables/ducts that do not exist yet.
    assert "Feeder_Cable.gpkg" not in chamber_seg
    assert "Feeder_Ducts.gpkg" not in chamber_seg


def test_cables_and_ducts_get_their_normalization_pass_after_the_cable_stage():
    body = _execute_pipeline_src()
    cable_seg = body[_stage_offset(body, "# --- Cable Layer (planned last"):]
    assert '"Chamber span normalization complete' in cable_seg
    for name in ("Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
                 "Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg",
                 "Drop_Ducts.gpkg"):
        assert name in cable_seg, f"{name} not normalized after the cable stage"


def test_cable_stage_still_requires_the_trench_route_tree():
    """Cables route on the (chamber-split) trench network — the ducts lie
    inside those spans, so preflight must keep demanding them."""
    body = _execute_pipeline_src()
    cable_seg = body[_stage_offset(body, "# --- Cable Layer (planned last"):]
    assert "_preflight_cable" in cable_seg
    assert "_preflight_duct" in body[
        _stage_offset(body, "# --- Duct Layer ---"):
        _stage_offset(body, "# --- Cable Layer (planned last")
    ]
