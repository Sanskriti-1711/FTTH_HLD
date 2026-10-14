"""Every layer name the API advertises must resolve, in every project.

`ONECLICK_OUTPUTS` publishes the ducts as three files and the cables as two,
all under ONE grouped public name each, so ``_ingest_outputs`` keys the task's
file map by the group (``ducts``, ``cables``).  A request for a tier
(``feeder_ducts``) therefore found no entry and answered **404** — while the
file it wanted sat in the very same list — and the "fall back to disk" that
``postgis.get_layer_geojson`` relied on could never fire either, because that
map is keyed by the group name and never by a tier.

Measured on project ``24cd3573…`` before the fix: ``feeder_ducts``,
``distribution_ducts`` and ``drop_ducts`` were all 404 through the API while the
run had published 638 duct rows, and ``distribution_cable`` answered 56 where
the same name holds 280 in the store.  A layer that cannot be fetched is a
layer that cannot be drawn, downloaded or counted, however correct the design
is behind it.

These are module-level tests on purpose: this suite's pytest configuration
collects `def test_*` functions (and `Test*` classes), so a class named
`SomethingTests` is never run at all -- see the note in `tests/README.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
import postgis  # noqa: E402


def _write_fc(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[13.37, 52.45], [13.38, 52.46]],
                        },
                        "properties": {"fid": i},
                    }
                    for i in range(rows)
                ],
            }
        ),
        encoding="utf-8",
    )


def _task_with_grouped_files(tmp_path: Path, project_id: str) -> None:
    """A finished run's task, files keyed the way `_ingest_outputs` keys them."""
    feeder_ducts = tmp_path / "Feeder_Ducts.geojson"
    dist_ducts = tmp_path / "Distribution_Ducts.geojson"
    drop_ducts = tmp_path / "Drop_Ducts.geojson"
    _write_fc(feeder_ducts, 3)
    _write_fc(dist_ducts, 5)
    _write_fc(drop_ducts, 7)
    main.tasks[project_id] = {
        "project_id": project_id,
        "status": "completed",
        "progress": 100,
        "messages": [],
        "files": {
            "ducts": [str(feeder_ducts), str(dist_ducts), str(drop_ducts)],
        },
        "layers": [
            {"name": "ducts", "feature_count": 15, "files": [str(feeder_ducts)]},
        ],
    }


@pytest.fixture
def no_postgis(monkeypatch):
    """Serve from the run's files, so the test needs no database."""
    monkeypatch.setattr(main.postgis, "is_available", lambda: False)


def _get(project_id: str, layer: str) -> dict:
    try:
        return main.get_layer(project_id, layer)
    finally:
        main.tasks.pop(project_id, None)


def test_a_duct_tier_is_served_from_the_group_it_belongs_to(tmp_path, no_postgis):
    project_id = "a" * 32
    _task_with_grouped_files(tmp_path, project_id)
    assert len(_get(project_id, "feeder_ducts")["features"]) == 3


def test_the_tier_does_not_hand_back_the_whole_group(tmp_path, no_postgis):
    """Serving the group under a tier name is the failure this avoids."""
    project_id = "b" * 32
    _task_with_grouped_files(tmp_path, project_id)
    assert len(_get(project_id, "distribution_ducts")["features"]) == 5


def test_the_served_tier_states_which_tier_it_is(tmp_path, no_postgis):
    """The results map splits the group by this property, so it must be set."""
    project_id = "c" * 32
    _task_with_grouped_files(tmp_path, project_id)
    payload = _get(project_id, "drop_ducts")
    assert {f["properties"]["sublayer"] for f in payload["features"]} == {"Drop_Ducts"}


def test_the_grouped_name_still_returns_every_tier(tmp_path, no_postgis):
    project_id = "d" * 32
    _task_with_grouped_files(tmp_path, project_id)
    assert len(_get(project_id, "ducts")["features"]) == 15


def test_a_name_that_matches_no_file_is_still_a_404(tmp_path, no_postgis):
    project_id = "e" * 32
    _task_with_grouped_files(tmp_path, project_id)
    with pytest.raises(main.HTTPException) as excinfo:
        try:
            main.get_layer(project_id, "garden_ducts")
        finally:
            main.tasks.pop(project_id, None)
    assert excinfo.value.status_code == 404


def test_every_shared_table_name_is_a_group_alias_or_a_known_tier():
    """The invariant whose absence produced the 404s.

    `postgis.LAYER_TABLES` accepts a name for a tier of a grouped layer, and the
    table it maps to holds every tier.  Any such name must therefore be either
    the group's own alias or a tier the serving code knows how to filter on --
    otherwise the map advertises a layer that can only ever answer 404.
    """
    group_aliases = {"ducts", "duct", "duct_layer", "cables", "cable", "cable_layer"}
    shared = {
        name
        for name, table in postgis.LAYER_TABLES.items()
        if table in ("duct_layer", "cable_layer")
    }
    unresolved = shared - group_aliases - set(main._LAYER_TIER_FILES)
    assert not unresolved, (
        f"{sorted(unresolved)} map to a grouped table but the serving code has no "
        "tier filter for them, so they can only 404"
    )


def test_the_tier_map_points_at_files_the_pipeline_really_publishes():
    """A tier must name a published stem and the group it is published under, so
    a rename in `ONECLICK_OUTPUTS` cannot silently strand it again."""
    published_stems = {
        Path(gpkg).stem.lower() for _pub, gpkg, _js in main.ONECLICK_OUTPUTS
    }
    published_groups = {pub for pub, _g, _j in main.ONECLICK_OUTPUTS}
    for name, (group, stem) in main._LAYER_TIER_FILES.items():
        assert stem.lower() in published_stems, (name, stem)
        assert group in published_groups, (name, group)
