"""The recorded download list must describe the files that are actually there.

The project row carries the run's downloadable files (name / url / size) and
both packages are built from it: ``generate_survey_package`` and
``generate_design_package`` read the engine's ``downloads`` and fetch each
entry.  That list used to be built only when it was EMPTY, so a run whose last
stages wrote their files after the publish kept the previous design's list for
good — one project's pole and aerial stages wrote two hours after its publish,
and every aerial layer and the poles stayed invisible in the Downloads panel
and in both packages while 20 files, ``Poles.gpkg`` and the three ``Aerial_*``
layers among them, sat on disk.

Existence is not freshness: these tests pin the rule that the directory is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _touch(output_dir: Path, *names: str) -> None:
    for name in names:
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def _recorded(*names: str) -> list:
    return [
        {'name': name, 'url': f'/ftth/hld/download/proj/{name}', 'size_bytes': 1}
        for name in names
    ]


class DownloadsFreshnessTests:
    def test_a_list_that_matches_the_directory_is_not_stale(self, tmp_path):
        _touch(tmp_path, 'Objects.gpkg', 'Poles.geojson')
        assert not main._downloads_are_stale(
            _recorded('Objects.gpkg', 'Poles.geojson'), tmp_path
        )

    def test_a_layer_written_after_the_publish_makes_it_stale(self, tmp_path):
        """The regression: the list already had entries, so it was never rebuilt."""
        _touch(tmp_path, 'Objects.gpkg', 'Poles.geojson', 'Aerial_Spans.geojson')
        assert main._downloads_are_stale(_recorded('Objects.gpkg'), tmp_path)

    def test_a_file_that_vanished_makes_it_stale(self, tmp_path):
        """A recorded name with no file behind it is a download that 404s."""
        _touch(tmp_path, 'Objects.gpkg')
        assert main._downloads_are_stale(
            _recorded('Objects.gpkg', 'surface_ai_review.json'), tmp_path
        )

    def test_an_empty_list_over_files_is_stale(self, tmp_path):
        _touch(tmp_path, 'Objects.gpkg')
        assert main._downloads_are_stale([], tmp_path)

    def test_files_that_are_not_downloads_are_ignored(self, tmp_path):
        """Only the extensions the download route serves are compared."""
        _touch(tmp_path, 'Objects.gpkg', 'internal.geojson.sha256', 'run.log')
        assert not main._downloads_are_stale(_recorded('Objects.gpkg'), tmp_path)

    def test_subdirectories_are_compared_by_relative_path(self, tmp_path):
        _touch(tmp_path, 'brownfield/bf_ducts.geojson')
        assert main._downloads_are_stale(_recorded('Objects.gpkg'), tmp_path)
        assert not main._downloads_are_stale(
            _recorded('brownfield/bf_ducts.geojson'), tmp_path
        )

    def test_an_empty_directory_is_never_stale(self, tmp_path):
        """Nothing to publish is not a reason to wipe the recorded list."""
        assert not main._downloads_are_stale(_recorded('Objects.gpkg'), tmp_path)

    def test_a_missing_directory_is_never_stale(self, tmp_path):
        assert not main._downloads_are_stale(
            _recorded('Objects.gpkg'), tmp_path / 'gone'
        )


class ResultsRefreshTests:
    """The results endpoint is what the UI and both packages read."""

    def test_get_results_replaces_a_stale_list(self, tmp_path, monkeypatch):
        project_id = 'b' * 32
        run_dir = tmp_path / project_id
        _touch(run_dir, 'Objects.gpkg', 'Poles.gpkg', 'Aerial_Spans.gpkg')
        monkeypatch.setattr(main, 'OUTPUT_DIR', tmp_path)
        monkeypatch.setattr(main.postgis, 'is_available', lambda: False)
        main.tasks[project_id] = {
            'project_id': project_id,
            'status': 'completed',
            'progress': 100,
            'layers': [{'name': 'objects'}],
            'downloads': [{'name': 'Objects.gpkg'}],
            'messages': [],
        }
        try:
            payload = main.get_results(project_id)
        finally:
            main.tasks.pop(project_id, None)

        names = {item['name'] for item in payload['downloads']}
        assert names == {'Objects.gpkg', 'Poles.gpkg', 'Aerial_Spans.gpkg'}

    def test_get_results_leaves_a_current_list_alone(self, tmp_path, monkeypatch):
        project_id = 'c' * 32
        run_dir = tmp_path / project_id
        _touch(run_dir, 'Objects.gpkg')
        monkeypatch.setattr(main, 'OUTPUT_DIR', tmp_path)
        monkeypatch.setattr(main.postgis, 'is_available', lambda: False)
        recorded = _recorded('Objects.gpkg')
        main.tasks[project_id] = {
            'project_id': project_id,
            'status': 'completed',
            'progress': 100,
            'layers': [{'name': 'objects'}],
            'downloads': recorded,
            'messages': [],
        }
        try:
            payload = main.get_results(project_id)
        finally:
            main.tasks.pop(project_id, None)

        assert payload['downloads'] == recorded
