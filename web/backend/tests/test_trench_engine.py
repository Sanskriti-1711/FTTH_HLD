"""Unit tests for the pipeline's trench-engine selection.

The pipeline can run either trench stage — the legacy sidewalk/graph layer or
the civil trench designer — behind one environment variable.  These tests pin
the resolution rules (which the graders of a wrong run depend on) and the
wiring that makes the designer reachable as a processing algorithm.

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


def test_env_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "legacy")
    assert TRENCH_ENGINE.resolve() == (TRENCH_ENGINE.LEGACY, None)
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "design")
    assert TRENCH_ENGINE.resolve() == (TRENCH_ENGINE.DESIGN, None)


@pytest.mark.parametrize("raw", ["legacy", "LEGACY", "  Legacy "])
def test_legacy_escape_hatch_is_honoured(raw):
    assert TRENCH_ENGINE.resolve(raw) == (TRENCH_ENGINE.LEGACY, None)


@pytest.mark.parametrize("raw", ["design", "DESIGN", " Design\t"])
def test_design_is_honoured(raw):
    assert TRENCH_ENGINE.resolve(raw) == (TRENCH_ENGINE.DESIGN, None)


def test_unrecognised_value_falls_back_but_is_reported():
    """A typo must not silently change the design without saying so."""
    assert TRENCH_ENGINE.resolve("trench_design") == (
        TRENCH_ENGINE.DESIGN, "trench_design")
    assert TRENCH_ENGINE.resolve(" legacy ") == (TRENCH_ENGINE.LEGACY, None)


# ── algorithm id ─────────────────────────────────────────────────────────


def test_algorithm_ids_are_distinct_and_stable():
    assert TRENCH_ENGINE.algorithm_id(TRENCH_ENGINE.LEGACY) == ALG.TRENCH
    assert TRENCH_ENGINE.algorithm_id(TRENCH_ENGINE.DESIGN) == ALG.TRENCH_DESIGN
    assert ALG.TRENCH == "hldplanning:04_trench_layer"
    assert ALG.TRENCH_DESIGN == "hldplanning:04_trench_design_layer"


def test_algorithm_id_defaults_to_the_resolved_engine(monkeypatch):
    assert TRENCH_ENGINE.algorithm_id() == ALG.TRENCH_DESIGN
    monkeypatch.setenv(TRENCH_ENGINE.ENV, "legacy")
    assert TRENCH_ENGINE.algorithm_id() == ALG.TRENCH


# ── registration / wiring ────────────────────────────────────────────────


def test_adapter_is_registered_and_named_like_its_id():
    """The designer must stay addressable by the id the pipeline selects.

    ``04_trench_design_layer`` is derived from the module name by QGIS's
    provider, so a rename in one place without the other silently makes
    TRENCH_ENGINE=design unrunnable.
    """
    init_src = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "__init__.py").read_text(
        encoding="utf-8")
    assert '_register("trench_design_layer", "TrenchDesignLayerAlgorithm")' in init_src

    adapter = (_HLD_ROOT / "HLDPlanning" / "algorithms" /
               "trench_design_layer.py").read_text(encoding="utf-8")
    assert "class TrenchDesignLayerAlgorithm" in adapter
    assert "04_trench_design_layer" in adapter


def test_pipeline_runs_the_selected_engine():
    """The trench stage must dispatch through the resolver, not hardcode one."""
    src = (_HLD_ROOT / "HLDPlanning" / "algorithms" / "oneclick.py").read_text(
        encoding="utf-8")
    assert "alg_id = TRENCH_ENGINE.algorithm_id(engine)" in src
    assert "TRENCH_ENGINE.resolve()" in src
