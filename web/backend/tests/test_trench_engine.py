"""Unit tests for the single production trench-engine selection.

The end-to-end pipeline always uses the civil trench designer. The legacy
sidewalk/graph stage remains directly available for comparison, but no
configuration value should route production runs into that known-failing path.
Tests pin the fixed resolver and the wiring to the registered designer.

No QGIS required: the resolver lives in ``utils/params.py``, which is pure, and
the registration is asserted from source.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_trench_engine.py -v
"""

import pathlib
import sys

import pytest

# HLDPlanning/ lives one level above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils.params import ALG, TRENCH_ENGINE  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(TRENCH_ENGINE.ENV, raising=False)


# ── resolution ───────────────────────────────────────────────────────────


def test_unset_env_selects_the_designer():
    """The shipped default is the designer, not the legacy trench layer."""
    assert TRENCH_ENGINE.DEFAULT == TRENCH_ENGINE.DESIGN
    assert TRENCH_ENGINE.resolve() == (TRENCH_ENGINE.DESIGN, None)


def test_empty_env_selects_the_default():
    assert TRENCH_ENGINE.resolve("") == (TRENCH_ENGINE.DESIGN, None)
    assert TRENCH_ENGINE.resolve("   ") == (TRENCH_ENGINE.DESIGN, None)


def test_environment_cannot_reenable_the_legacy_pipeline(monkeypatch):
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "legacy")
    assert TRENCH_ENGINE.resolve() == (TRENCH_ENGINE.DESIGN, "legacy")
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "design")
    assert TRENCH_ENGINE.resolve() == (TRENCH_ENGINE.DESIGN, None)


@pytest.mark.parametrize("raw", ["legacy", "LEGACY", "  Legacy "])
def test_legacy_selection_is_rejected_for_pipeline_runs(raw):
    assert TRENCH_ENGINE.resolve(raw) == (TRENCH_ENGINE.DESIGN, raw.strip())


@pytest.mark.parametrize("raw", ["trench_design", "bad", "unknown"])
def test_unrecognised_engine_is_reported_and_uses_designer(raw):
    assert TRENCH_ENGINE.resolve(raw) == (TRENCH_ENGINE.DESIGN, raw)


@pytest.mark.parametrize("raw", ["design", "DESIGN", " Design\t"])
def test_design_is_honoured(raw):
    assert TRENCH_ENGINE.resolve(raw) == (TRENCH_ENGINE.DESIGN, None)



# ── algorithm id ─────────────────────────────────────────────────────────


def test_algorithm_id_always_selects_the_designer(monkeypatch):
    assert TRENCH_ENGINE.algorithm_id(TRENCH_ENGINE.DESIGN) == ALG.TRENCH_DESIGN
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "legacy")
    assert TRENCH_ENGINE.algorithm_id() == ALG.TRENCH_DESIGN
    assert ALG.TRENCH == "hldplanning:04_trench_layer"
    assert ALG.TRENCH_DESIGN == "hldplanning:04_trench_design_layer"


# ── registration / wiring ────────────────────────────────────────────────


def test_adapter_is_registered_and_named_like_its_id():
    """The designer must stay addressable by the fixed production algorithm id.

    ``04_trench_design_layer`` is derived from the module name by QGIS's
    provider, so a rename in one place without the other silently makes the
    production trench stage unrunnable.
    """
    init_src = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "__init__.py").read_text(
        encoding="utf-8")
    assert '_register("trench_design_layer", "TrenchDesignLayerAlgorithm")' in init_src

    adapter = (_HLD_ROOT / "HLDPlanning" / "algorithms" /
               "trench_design_layer.py").read_text(encoding="utf-8")
    assert "class TrenchDesignLayerAlgorithm" in adapter
    assert "04_trench_design_layer" in adapter


def test_pipeline_dispatches_to_the_designer_algorithm():
    """The trench stage must use the single supported pipeline engine."""
    src = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "oneclick.py").read_text(
        encoding="utf-8")
    assert "alg_id = TRENCH_ENGINE.algorithm_id(engine)" in src
    assert "TRENCH_ENGINE.resolve()" in src
