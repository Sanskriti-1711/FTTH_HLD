"""Unit tests for the per-stage guard that keeps the cascade end-to-end.

The cascade used to abort on the first stage that raised, so a failure in (say)
the duct stage threw away a complete trench and chamber network that was
already written to disk.  ``RunGuard`` contains the failure instead: the stage
is recorded, the stages that need it are skipped with a reason, and every other
stage still runs and still publishes.

These are plain unit tests — no QGIS, no GeoPackage, no network.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_run_guard.py -v
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils.run_guard import (  # noqa: E402
    REPORT_NAME,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    RunGuard,
    summarise,
)


class FakeFeedback:
    """Records what the guard told the user."""

    def __init__(self, cancelled=False):
        self.infos = []
        self.warnings = []
        self._cancelled = cancelled

    def pushInfo(self, message):  # noqa: N802 - QGIS API spelling
        self.infos.append(message)

    def pushWarning(self, message):  # noqa: N802 - QGIS API spelling
        self.warnings.append(message)

    def isCanceled(self):  # noqa: N802 - QGIS API spelling
        return self._cancelled


def _boom():
    raise RuntimeError("duct stage blew up")


# ── containment ───────────────────────────────────────────────────────────


def test_a_failing_stage_is_recorded_and_returns_none():
    guard = RunGuard()
    feedback = FakeFeedback()

    assert guard.run("Duct Layer", _boom, feedback=feedback) is None

    assert guard.status("Duct Layer") == STATUS_FAILED
    assert guard.failed() == ["Duct Layer"]
    assert any("Duct Layer failed" in w for w in feedback.warnings)


def test_the_run_carries_on_after_a_failure():
    """The whole point: later independent stages still run."""
    guard = RunGuard()
    ran = []

    guard.run("Duct Layer", _boom)
    out = guard.run("Cable Layer", lambda: ran.append("cable") or {"ok": 1})

    assert out == {"ok": 1}
    assert ran == ["cable"]
    assert guard.status("Duct Layer") == STATUS_FAILED
    assert guard.status("Cable Layer") == STATUS_OK


def test_a_dependent_stage_is_skipped_without_running():
    guard = RunGuard()
    called = []

    guard.run("Trench Layer", _boom)
    out = guard.run(
        "Chamber Layer",
        lambda: called.append("chamber") or {"OUT_CHAMBERS": "x"},
        needs=("Trench Layer",),
    )

    assert out is None
    assert called == [], "a dependent stage must not run on missing inputs"
    assert guard.status("Chamber Layer") == STATUS_SKIPPED
    assert "Trench Layer" in guard.outcome("Chamber Layer").reason
    assert guard.skipped() == ["Chamber Layer"]


def test_a_skipped_dependent_blocks_the_next_one_too():
    guard = RunGuard()
    guard.run("Trench Layer", _boom)
    guard.run("Duct Layer", _boom, needs=("Trench Layer",))
    guard.run("Cable Layer", _boom, needs=("Duct Layer",))

    assert guard.status("Duct Layer") == STATUS_SKIPPED
    assert guard.status("Cable Layer") == STATUS_SKIPPED
    assert guard.failed() == ["Trench Layer"]


def test_all_stages_ok_is_a_clean_run():
    guard = RunGuard()
    guard.run("Object Layer", lambda: {})
    guard.run("Polygon Layer", lambda: {}, needs=("Object Layer",))

    assert guard.is_clean()
    assert guard.failed() == [] and guard.skipped() == []
    assert guard.report()["ok"] is True


# ── cancellation must never be contained ──────────────────────────────────

def test_a_cancelled_run_re_raises_instead_of_being_contained():
    """Swallowing a cancel would keep a stop request running to completion."""
    feedback = FakeFeedback(cancelled=True)
    guard = RunGuard(cancel_check=feedback.isCanceled)

    with pytest.raises(RuntimeError, match="blew up"):
        guard.run("Duct Layer", _boom, feedback=feedback)

    assert guard.outcomes == [], "a cancelled stage is not a stage failure"


def test_a_cancel_checker_that_raises_is_not_treated_as_a_cancel():
    def broken():
        raise ValueError("no feedback")

    guard = RunGuard(cancel_check=broken)
    assert guard.run("Duct Layer", _boom) is None
    assert guard.status("Duct Layer") == STATUS_FAILED


# ── caller-side validation ────────────────────────────────────────────────

def test_output_validation_can_fail_a_stage_that_returned_cleanly():
    guard = RunGuard()
    guard.run("Network Layer", lambda: {"rows": 0})

    guard.record_failure("Network Layer", "no Final_Object_Layer")

    assert guard.status("Network Layer") == STATUS_FAILED
    assert guard.blocked("Network Layer")
    assert "Final_Object_Layer" in guard.outcome("Network Layer").error


def test_re_recording_a_stage_keeps_its_time_instead_of_losing_it():
    """The runner's seconds survive the validation that follows it."""
    guard = RunGuard()
    guard.record_success("Network Layer", seconds=12.5)
    guard.record_failure("Network Layer", "no MFG point")

    assert guard.status("Network Layer") == STATUS_FAILED
    assert guard.outcome("Network Layer").seconds == pytest.approx(12.5)


# ── report ────────────────────────────────────────────────────────────────


def test_the_report_names_the_failed_and_skipped_stages():
    guard = RunGuard()
    guard.run("Object Layer", lambda: {})
    guard.run("Trench Layer", _boom, needs=("Object Layer",))
    guard.run("Duct Layer", lambda: {}, needs=("Trench Layer",))

    report = guard.report()

    assert report["ok"] is False
    assert report["failed"] == ["Trench Layer"]
    assert report["skipped"] == ["Duct Layer"]
    assert {s["name"] for s in report["stages"]} == {
        "Object Layer", "Trench Layer", "Duct Layer"
    }
    failed = [s for s in report["stages"] if s["name"] == "Trench Layer"][0]
    assert "blew up" in failed["error"]


def test_the_report_ranks_the_slowest_stages():
    guard = RunGuard()
    for name, seconds in (("Object Layer", 0.5), ("Trench Layer", 9.0),
                          ("Cable Layer", 3.0)):
        guard.record_success(name, seconds=seconds)

    slowest = guard.report()["slowest_stages"]

    assert [s["name"] for s in slowest] == [
        "Trench Layer", "Cable Layer", "Object Layer"
    ]
    assert slowest[0]["seconds"] == pytest.approx(9.0)


def test_the_report_is_written_beside_the_design(tmp_path):
    guard = RunGuard()
    guard.run("Object Layer", lambda: {})

    path = guard.write_report(str(tmp_path), extra={"output_dir": str(tmp_path)})

    assert path == str(tmp_path / REPORT_NAME)
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["output_dir"] == str(tmp_path)
    assert payload["stages"][0]["name"] == "Object Layer"
    assert not list(tmp_path.glob("*.tmp")), "report must be replaced atomically"


def test_a_report_that_cannot_be_written_does_not_fail_the_run(tmp_path):
    """A design must never be lost because its bookkeeping could not be saved."""
    guard = RunGuard()
    guard.run("Object Layer", lambda: {})

    assert guard.write_report(str(tmp_path / "missing" / "dir")) is None
    assert guard.write_report(None) is None


def test_summarise_logs_one_verdict_line():
    guard = RunGuard()
    feedback = FakeFeedback()
    guard.run("Object Layer", lambda: {})
    guard.run("Trench Layer", _boom)

    summarise(guard, feedback)

    assert len(feedback.warnings) == 1
    assert "Trench Layer" in feedback.warnings[0]

    clean = RunGuard()
    clean.run("Object Layer", lambda: {})
    ok_feedback = FakeFeedback()
    summarise(clean, ok_feedback)
    assert ok_feedback.warnings == []
    assert any("All stages completed" in m for m in ok_feedback.infos)
