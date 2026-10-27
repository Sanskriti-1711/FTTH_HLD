"""The run form's existing-infrastructure toggle has to actually switch it off.

A project's own published network is fed back into any re-run as ``BF_*`` (the
duct occupancy and the trench network), and the trench stage prices an existing
edge at **0.1x** a new one. So a re-run replays the design it is replacing — on
the Leeds project that was 92 % of the trench metres and 14.4 km of duct — and a
change to the design rules cannot be seen through a re-run at all.

``use_brownfield=false`` is the way out of that loop, so what is pinned here is
that it short-circuits ``_brownfield_args`` completely: no archive is unpacked,
no read-back runs, and the pipeline gets no ``USE_BROWNFIELD`` at all.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402


class _Counter:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("maybe", True),      # unreadable keeps the previous behaviour
        ("1", True),
    ],
)
def test_a_flag_reads_as_a_bool_and_an_absent_one_keeps_the_default(value, expected):
    assert main._as_bool(value) is expected


def test_the_toggle_off_never_reads_the_project_back(tmp_path, monkeypatch):
    ducts, trench = _Counter(), _Counter()
    monkeypatch.setattr(main.occupancy, "write_duct_brownfield", ducts)
    monkeypatch.setattr(main.occupancy, "write_trench_brownfield", trench)

    args = main._brownfield_args(None, tmp_path, "some-project", use_brownfield=False)

    assert args == []
    assert ducts.calls == [] and trench.calls == []
    assert not (tmp_path / "brownfield").exists()


def test_the_toggle_off_also_ignores_an_uploaded_archive(tmp_path):
    archive = tmp_path / "brownfield.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "bf_ducts.geojson",
            json.dumps({"type": "FeatureCollection", "features": []}),
        )
    output_dir = tmp_path / "project"
    output_dir.mkdir()

    assert main._brownfield_args(
        archive, output_dir, "some-project", use_brownfield=False) == []
    assert not (output_dir / "brownfield").exists()


def test_the_toggle_on_still_unpacks_the_archive(tmp_path):
    archive = tmp_path / "brownfield.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "bf_ducts.geojson",
            json.dumps({"type": "FeatureCollection", "features": []}),
        )
    output_dir = tmp_path / "project"
    output_dir.mkdir()

    args = main._brownfield_args(archive, output_dir, None, use_brownfield=True)

    assert args, "with the toggle on the archive must still become BF_* params"
    assert any("USE_BROWNFIELD" in part for part in args)
    assert (output_dir / "brownfield").is_dir()
