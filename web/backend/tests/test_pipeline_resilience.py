"""Source-level tests that every cascade stage runs under the guard.

``oneclick.py`` cannot be imported without QGIS, so — like
``test_cascade_order.py`` — these pin the facts by reading the source.  What
they protect is the promise that made the cascade survivable: a stage failure
is contained and recorded, and the design already built is still published.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_pipeline_resilience.py -v
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

ONECLICK_SRC = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "oneclick.py").read_text(
    encoding="utf-8"
)

import main  # noqa: E402


def _execute_pipeline_src() -> str:
    start = ONECLICK_SRC.index("def execute_pipeline")
    m = re.search(r"\n    def ", ONECLICK_SRC[start:])
    end = start + m.start() if m else len(ONECLICK_SRC)
    return ONECLICK_SRC[start:end]


def _guarded_calls(body: str):
    """Every ``_guarded_stage(...)`` call, flattened to one string each."""
    calls = []
    for m in re.finditer(r"self\._guarded_stage\(", body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        calls.append(body[m.start():i])
    return calls


# ── every stage goes through the guard ───────────────────────────────────


def test_the_driver_never_calls_run_directly():
    """``self._run`` raises by design; only the guard may use it."""
    body = _execute_pipeline_src()
    assert "self._run(" not in body, (
        "execute_pipeline must call stages through _guarded_stage, otherwise a "
        "failing stage aborts the whole design again"
    )


def test_every_status_page_stage_is_guarded():
    body = _execute_pipeline_src()
    guarded = " ".join(_guarded_calls(body))
    for name in main.PIPELINE_STAGES:
        assert name in guarded, f"{name} is not run under the guard"


def test_the_guard_is_created_and_its_report_written():
    body = _execute_pipeline_src()
    assert "guard = RunGuard(cancel_check=feedback.isCanceled)" in body, (
        "the guard must be cancellable, or a stop request gets contained"
    )
    assert "_finish_run(guard" in body, "the run report must be published"


def test_the_report_is_written_before_the_one_hard_failure():
    """No object layer means nothing to publish — but the reason must survive."""
    body = _execute_pipeline_src()
    missing = body.index('Object Layer produced no GeoPackage')
    assert body.rindex("_finish_run(guard", 0, missing) > 0


# ── the dependency graph the guard enforces ──────────────────────────────


@pytest.mark.parametrize(
    "stage, needs",
    [
        ("Polygon Layer", ("Object Layer",)),
        ("Network Layer", ("Object Layer", "Polygon Layer")),
        ("Trench Layer", ("Network Layer",)),
        ("Chamber Layer", ("Trench Layer",)),
        ("Duct Layer", ("Trench Layer",)),
        ("Cable Layer", ("Trench Layer", "Duct Layer")),
        ("Aerial Drop Layer", ("Pole Layer",)),
    ],
)
def test_stage_dependencies(stage, needs):
    guarded = " ".join(_guarded_calls(_execute_pipeline_src()))
    call = [c for c in _guarded_calls(_execute_pipeline_src()) if f'"{stage}"' in c]
    assert call, f"{stage} is not guarded"
    for need in needs:
        assert f'"{need}"' in call[0], (
            f"{stage} must skip when {need} did not complete"
        )
    # sanity: the flattened view is what the assertion above searched
    assert stage in guarded


def test_stage_preflights_run_inside_the_guard():
    """A preflight refusal is an input failure, not a reason to lose the run."""
    body = _execute_pipeline_src()
    for call in _guarded_calls(body):
        if "_preflight_duct" in call or "_preflight_cable" in call:
            assert "preflight=" in call
            break
    else:  # pragma: no cover - the preflights are stage prerequisites
        pytest.fail("no guarded preflight found")


# ── failures that used to abort the run ─────────────────────────────────


def test_the_served_premises_register_no_longer_aborts_the_design():
    body = _execute_pipeline_src()
    served = body[body.index("Served Premises Register") - 400:][:1200]
    assert "raise PipelineStageError" not in served
    assert "guard.run(" in served


def test_the_network_validation_is_contained():
    """A network stage that produced nothing usable must not kill the run."""
    body = _execute_pipeline_src()
    assert "def _validate_network()" in body
    start = body.index("validated = guard.run(")
    assert '"Network Layer"' in body[start:start + 200]
