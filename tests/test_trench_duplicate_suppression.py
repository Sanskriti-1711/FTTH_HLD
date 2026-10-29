"""Duplicate trench runs on one side of a street must collapse to one line.

Sidewalk offsets are applied more than once, so one street comes out as two
parallel trenches a metre or two apart. The suppression corridor used to be
0.75 m — narrower than the offset it was meant to catch — so those pairs
scored 0 % overlap and both lines were published. Measured on the 2026-10-29
Mariendorf run, the duplicate pairs sit 0.94-3.11 m apart.

Runs under the QGIS interpreter, like the rest of the suite:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import pytest
from qgis.core import QgsGeometry

from HLDPlanning.algorithms.trench_layer import (
    DUP_FRAC,
    DUP_TOL_M,
    _is_duplicate_run,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

OPEN_CUT = "Open Cut"
HDD = "HDD"


def _line(wkt):
    return QgsGeometry.fromWkt(wkt)


def test_corridor_is_wider_than_the_sidewalk_offset():
    assert DUP_TOL_M >= 2.0
    assert DUP_FRAC == 0.8


def test_parallel_twin_one_point_eight_metres_away_is_dropped():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    twin = _line("LineString(0 1.8, 100 1.8)")
    assert _is_duplicate_run(twin, OPEN_CUT, kept) is True


def test_parallel_twin_two_and_a_half_metres_away_is_dropped():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    twin = _line("LineString(0 2.2, 100 2.2)")
    assert _is_duplicate_run(twin, OPEN_CUT, kept) is True


def test_separate_trench_three_and_a_half_metres_away_is_kept():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    other = _line("LineString(0 3.5, 100 3.5)")
    assert _is_duplicate_run(other, OPEN_CUT, kept) is False


def test_same_line_of_another_construction_class_is_kept():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    bore = _line("LineString(0 1.5, 100 1.5)")
    assert _is_duplicate_run(bore, HDD, kept) is False


def test_crossing_spur_is_kept():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    spur = _line("LineString(50 -4, 50 4)")
    assert _is_duplicate_run(spur, OPEN_CUT, kept) is False


def test_short_run_lying_along_a_long_trench_is_dropped():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    fragment = _line("LineString(40 1.2, 52 1.2)")
    assert _is_duplicate_run(fragment, OPEN_CUT, kept) is True


def test_longer_twin_is_dropped_when_the_short_one_came_first():
    # The published 2026-10-29 run kept pairs like this: a 12 m run emitted
    # first, then its 25 m twin. Measuring the candidate against its own length
    # kept the long one, so both shipped.
    kept = [(_line("LineString(0 0, 12 0)"), OPEN_CUT)]
    twin = _line("LineString(0 2.1, 12 2.1)")
    assert _is_duplicate_run(twin, OPEN_CUT, kept) is True


def test_long_trench_brushing_a_short_kept_fragment_is_kept():
    kept = [(_line("LineString(0 0, 5 0)"), OPEN_CUT)]
    long_run = _line("LineString(0 1.2, 120 1.2)")
    assert _is_duplicate_run(long_run, OPEN_CUT, kept) is False


def test_empty_run_is_not_a_duplicate():
    kept = [(_line("LineString(0 0, 100 0)"), OPEN_CUT)]
    empty = QgsGeometry()
    assert _is_duplicate_run(empty, OPEN_CUT, kept) is False
