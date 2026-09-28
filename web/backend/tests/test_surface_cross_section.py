"""Tests for the lateral road cross-section surface model."""

import math
import pathlib
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import surface_cross_section as sx  # noqa: E402


def _line(coords):
    return list(coords)


def test_footway_cross_section_is_all_footway():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="footway"))
    assert len(cs.bounds) == 1
    assert cs.bounds[0][1] == sx.SURFACE_FOOTWAY


def test_residential_cross_section_has_three_bands_when_sidewalk():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    bands = [b for _, b in cs.bounds]
    assert bands == [sx.SURFACE_CARRIAGEWAY, sx.SURFACE_VERGE, sx.SURFACE_FOOTWAY]


def test_residential_without_sidewalk_is_just_carriageway():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="no"))
    bands = [b for _, b in cs.bounds]
    assert bands == [sx.SURFACE_CARRIAGEWAY]


def test_classify_center_of_road_is_carriageway():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    cls, conf = sx.classify_point(cs, 50, 0)
    assert cls == sx.SURFACE_CARRIAGEWAY
    assert conf == 1.0


def test_classify_kerbside_is_footway():
    # residential default width 6.5 → half 3.25; verge 1.0; footway starts at 4.25
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    cls, _ = sx.classify_point(cs, 50, 5.0)
    assert cls == sx.SURFACE_FOOTWAY


def test_classify_between_carr_and_foot_is_verge():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    cls, _ = sx.classify_point(cs, 50, 3.75)
    assert cls == sx.SURFACE_VERGE


def test_classify_far_from_road_is_unknown():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    cls, conf = sx.classify_point(cs, 50, 40.0)
    assert cls == sx.SURFACE_UNKNOWN
    assert conf == 0.0


def test_classify_line_splits_at_band_boundaries():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both"))
    # A line running from the centre to the footway.
    line = _line([(0, 0), (0, 5.5)])
    intervals = sx.classify_line(cs, line, step_m=1.0)
    classes = [c for _, _, c, _ in intervals]
    assert sx.SURFACE_CARRIAGEWAY in classes
    assert sx.SURFACE_VERGE in classes
    assert sx.SURFACE_FOOTWAY in classes
    # Intervals should cover the whole line.
    assert intervals[0][0] == pytest.approx(0.0)
    assert intervals[-1][1] == pytest.approx(5.5)


def test_surface_tag_overrides_carriageway_name():
    cs = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both", surface="concrete"))
    cls, _ = sx.classify_point(cs, 50, 0)
    assert cls == "Concrete"


def test_width_tag_overrides_default():
    cs_narrow = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both", width=4.0))
    cs_wide = sx.build_cross_section(
        _line([(0, 0), (100, 0)]),
        sx.RoadTags(highway="residential", sidewalk="both", width=12.0))
    # Narrow road: 4.0/2=2.0 carr, verge 1.0, footway starts at 3.0
    assert sx.classify_point(cs_narrow, 50, 3.5)[0] == sx.SURFACE_FOOTWAY
    # Wide road: 12.0/2=6.0 carr, verge 1.0, footway starts at 7.0
    assert sx.classify_point(cs_wide, 50, 3.5)[0] == sx.SURFACE_CARRIAGEWAY
