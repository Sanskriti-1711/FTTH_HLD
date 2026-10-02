"""Unit tests for the LLD trench construction-class normalisation.

The trench network is published with a CLOSED 3-value construction class
(Open Cut / HDD / Garden). HLD baselines produced before the single-trench
redesign — and legacy survey imports — still carry the old tier labels
(Feeder / Distribution), which used to leak straight into the LLD outputs.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/ -v
"""

from main import (
    _canonical_trench_class,
    _normalize_trench_construction_class,
)


def _trench(props):
    return {"type": "Feature", "geometry": None, "properties": dict(props)}


# ── _canonical_trench_class ────────────────────────────────────────────────

def test_canonical_class_maps_every_known_label():
    assert _canonical_trench_class("Open Cut") == "Open Cut"
    assert _canonical_trench_class("open cut") == "Open Cut"
    assert _canonical_trench_class("HDD") == "HDD"
    assert _canonical_trench_class("Drill crossing") == "HDD"
    assert _canonical_trench_class("Garden") == "Garden"
    assert _canonical_trench_class("garden trench") == "Garden"
    assert _canonical_trench_class("Drop leg") == "Garden"


def test_canonical_class_maps_legacy_tier_labels_to_open_cut():
    assert _canonical_trench_class("Feeder") == "Open Cut"
    assert _canonical_trench_class("Distribution") == "Open Cut"
    assert _canonical_trench_class("Duct") == "Open Cut"
    assert _canonical_trench_class("") is None
    assert _canonical_trench_class(None) is None


# ── _normalize_trench_construction_class ───────────────────────────────────

def test_normalize_rewrites_tier_labels_and_keeps_the_tier():
    layer = {
        "final_trenches": [
            _trench({"trench_type": "Feeder", "USAGE_TYPE": "Feeder", "CONSTRUCT": "Feeder"}),
            _trench({"trench_type": "Distribution"}),
            _trench({"trench_type": "Garden", "USAGE_TYPE": "Garden", "CONSTRUCT": "Garden"}),
        ]
    }
    changed = _normalize_trench_construction_class(layer)
    assert changed == 2

    feeder, dist, garden = layer["final_trenches"]
    assert feeder["properties"]["trench_type"] == "Open Cut"
    assert feeder["properties"]["USAGE_TYPE"] == "Open Cut"
    assert feeder["properties"]["CONSTRUCT"] == "Open Cut"
    assert feeder["properties"]["TRENCH_TIER"] == "Feeder"

    assert dist["properties"]["trench_type"] == "Open Cut"
    assert dist["properties"]["TRENCH_TIER"] == "Distribution"

    # already-canonical features are untouched and get no TRENCH_TIER
    assert garden["properties"]["trench_type"] == "Garden"
    assert "TRENCH_TIER" not in garden["properties"]


def test_normalize_fills_missing_class_aliases_on_canonical_features():
    layer = {"final_trenches": [_trench({"trench_type": "HDD"})]}
    assert _normalize_trench_construction_class(layer) == 0
    props = layer["final_trenches"][0]["properties"]
    assert props["trench_type"] == "HDD"
    assert props["USAGE_TYPE"] == "HDD"


def test_normalize_leaves_aerial_spans_alone():
    """The civil-trench normaliser must not touch overhead spans.

    `aerial_spans` (formerly `aerial_drop_trenches`) keeps its own class: it is
    a span on a pole, never dug, so forcing it into the closed 3-value trench
    set would relabel overhead fibre as an excavation.
    """
    layer = {
        "final_trenches": [_trench({"trench_type": "Feeder"})],
        "aerial_spans": [_trench({"trench_type": "Aerial_Drop"})],
    }
    _normalize_trench_construction_class(layer)
    assert layer["final_trenches"][0]["properties"]["trench_type"] == "Open Cut"
    assert layer["aerial_spans"][0]["properties"]["trench_type"] == "Aerial_Drop"


def test_normalize_handles_missing_layers_and_empty_props():
    assert _normalize_trench_construction_class({}) == 0
    layer = {"final_trenches": [_trench({})]}
    assert _normalize_trench_construction_class(layer) == 0


# ── _restore_task_from_disk must never force-complete a live run ───────────

def test_restore_from_disk_keeps_a_running_task_running(tmp_path, monkeypatch):
    """The results map fetches layers while the pipeline is still working.

    No output file is registered on the task yet at that point, so the disk
    fallback used to rewrite the task as completed / 100 % / Complete the
    moment anyone opened a layer — making an in-flight run look finished.
    """
    import main as engine

    project = "restore-running-test"
    out_dir = tmp_path / project
    out_dir.mkdir()
    (out_dir / "Objects.geojson").write_text(
        '{"type": "FeatureCollection", "features": []}', encoding="utf-8"
    )
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)

    task = engine._task(project)
    task.update({"status": "running", "progress": 42, "stage": "Duct Layer"})
    try:
        restored = engine._restore_task_from_disk(project)
        assert restored is not None
        assert restored["status"] == "running"
        assert restored["progress"] == 42
        assert restored["stage"] == "Duct Layer"
        # the on-disk paths are still merged in, so layer fetches work
        assert restored["files"]
    finally:
        engine.tasks.pop(project, None)


def test_restore_from_disk_completes_an_unknown_task(tmp_path, monkeypatch):
    """A project known only from its files (engine restart) restores as done."""
    import main as engine

    project = "restore-idle-test"
    out_dir = tmp_path / project
    out_dir.mkdir()
    (out_dir / "Objects.geojson").write_text(
        '{"type": "FeatureCollection", "features": []}', encoding="utf-8"
    )
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)

    try:
        restored = engine._restore_task_from_disk(project)
        assert restored is not None
        assert restored["status"] == "completed"
        assert restored["progress"] == 100
        assert restored["restored_from_disk"] is True
    finally:
        engine.tasks.pop(project, None)
