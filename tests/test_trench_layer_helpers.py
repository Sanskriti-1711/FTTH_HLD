"""QGIS-level tests for `trench_layer.py`'s module-level helpers.

These are the extraction seams of the 3,015-line `processAlgorithm`: every one
of them is a pure function over `QgsGeometry`, so each can be pinned
independently of the pipeline run that uses it.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import math

import pytest

from HLDPlanning.algorithms.trench_layer import (
    _approx_meters,
    _crossing_between_contacts,
    _dir_on_line_near_point,
    _eval_drop_feasibility,
    _intersection_points,
    _make_tangent_trench,
    _nearest_point_on,
    _safe_sink_fields,
    _weld_crossing_to_network,
)

pytestmark = pytest.mark.usefixtures("qgis_app")


def _pts(*xy):
    """QgsPointXY list — the only form QGIS geometry builders accept."""
    from qgis.core import QgsPointXY

    return [QgsPointXY(x, y) for x, y in xy]


def _line(*xy):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromPolylineXY(_pts(*xy))


def _multi(*lines):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromMultiPolylineXY([_pts(*ln) for ln in lines])


def _point(x, y):
    from qgis.core import QgsGeometry, QgsPointXY

    return QgsGeometry.fromPointXY(QgsPointXY(x, y))


# --- _approx_meters ---------------------------------------------------------

def test_approx_meters_matches_known_degree_distance():
    """0.001 deg of latitude is ~111.3 m; longitude shrinks with cos(lat)."""
    from qgis.core import QgsPointXY

    d_lat = _approx_meters(QgsPointXY(13.0, 52.0), QgsPointXY(13.0, 52.001))
    assert d_lat == pytest.approx(111.32, abs=0.5)

    d_lon = _approx_meters(QgsPointXY(13.0, 52.0), QgsPointXY(13.001, 52.0))
    assert d_lon == pytest.approx(111.32 * math.cos(math.radians(52.0)), abs=0.5)


def test_approx_meters_is_zero_for_identical_points():
    from qgis.core import QgsPointXY

    p = QgsPointXY(13.4, 52.5)
    assert _approx_meters(p, p) == 0.0


# --- _make_tangent_trench ---------------------------------------------------

def test_tangent_trench_is_perpendicular_to_the_road():
    """Edge point sits one radius along `direction`; the line is square to it."""
    from qgis.core import QgsPointXY

    geom = _make_tangent_trench(QgsPointXY(0.0, 0.0), (1.0, 0.0), 5.0, 10.0)
    pl = geom.asPolyline()

    assert len(pl) == 2
    assert geom.length() == pytest.approx(10.0)

    # Both ends are the same distance from the circle edge, straddling it.
    edge = QgsPointXY(5.0, 0.0)
    for p in pl:
        assert math.hypot(p.x() - edge.x(), p.y() - edge.y()) == pytest.approx(5.0)

    # And the tangent runs along +Y, i.e. perpendicular to +X.
    assert pl[0].x() == pytest.approx(5.0)
    assert pl[1].x() == pytest.approx(5.0)
    assert {round(pl[0].y(), 6), round(pl[1].y(), 6)} == {-5.0, 5.0}


# --- _intersection_points ---------------------------------------------------

def test_intersection_points_returns_crossing_point():
    pts = _intersection_points(_line((0, 0), (10, 0)).intersection(_line((5, -5), (5, 5))))

    assert len(pts) == 1
    assert pts[0].x() == pytest.approx(5.0)
    assert pts[0].y() == pytest.approx(0.0)


def test_intersection_points_handles_empty_and_none():
    assert _intersection_points(None) == []
    assert _intersection_points(_line((0, 0), (1, 1)).intersection(_line((5, 5), (6, 6)))) == []


def test_intersection_points_walks_a_multipart_result():
    """A multipart result must be recursed into, not treated as one geometry."""
    multi = _multi([(0, 0), (10, 0)], [(0, 20), (10, 20)])
    pts = _intersection_points(multi)

    assert sorted(round(p.y(), 6) for p in pts) == [0.0, 0.0, 20.0, 20.0]


# --- _dir_on_line_near_point ------------------------------------------------

def test_dir_on_line_near_point_returns_unit_vector_of_nearest_segment():
    d = _dir_on_line_near_point(_line((0, 0), (10, 0)), _point(3.0, 0.2).asPoint())

    assert d == pytest.approx((1.0, 0.0))


def test_dir_on_line_near_point_picks_the_closest_segment():
    """Near the vertical leg, the direction must be the vertical one."""
    geom = _line((0, 0), (10, 0), (10, 10))
    d = _dir_on_line_near_point(geom, _point(10.0, 9.0).asPoint())

    assert d == pytest.approx((0.0, 1.0))


def test_dir_on_line_near_point_returns_none_for_empty_geometry():
    from qgis.core import QgsGeometry

    assert _dir_on_line_near_point(QgsGeometry(), _point(0, 0).asPoint()) is None


# --- _nearest_point_on ------------------------------------------------------

def test_nearest_point_on_lands_on_the_line_with_the_right_distance():
    pt, dist = _nearest_point_on(_line((0, 0), (10, 0)), _point(5.0, 3.0))

    assert pt.x() == pytest.approx(5.0)
    assert pt.y() == pytest.approx(0.0)
    assert dist == pytest.approx(3.0)


def test_nearest_point_on_returns_none_for_empty_input():
    from qgis.core import QgsGeometry

    assert _nearest_point_on(QgsGeometry(), _point(0, 0)) == (None, None)
    assert _nearest_point_on(_line((0, 0), (1, 0)), QgsGeometry()) == (None, None)


# --- _weld_crossing_to_network ----------------------------------------------

def test_weld_pulls_both_ends_onto_the_open_cut():
    welded, ok = _weld_crossing_to_network(_line((0, 0), (10, 0)), _line((0, 5), (10, 5)))

    assert ok is True
    pl = welded.asPolyline()
    assert pl[0].y() == pytest.approx(5.0)
    assert pl[-1].y() == pytest.approx(5.0)
    assert welded.length() == pytest.approx(10.0)


def test_weld_leaves_the_crossing_alone_beyond_the_snap_tolerance():
    """An end further than snap_m from the network keeps the designed geometry."""
    original = _line((0, 0), (10, 0))
    welded, ok = _weld_crossing_to_network(original, _line((0, 5), (10, 5)), snap_m=1.0)

    assert ok is False
    assert welded.asWkt() == original.asWkt()


def test_weld_keeps_intermediate_vertices():
    welded, ok = _weld_crossing_to_network(
        _line((0, 0), (5, 2), (10, 0)), _line((0, 5), (10, 5))
    )

    assert ok is True
    assert len(welded.asPolyline()) == 3


def test_weld_rejects_degenerate_input():
    from qgis.core import QgsGeometry

    original = _line((0, 0), (10, 0))
    assert _weld_crossing_to_network(original, QgsGeometry()) == (original, False)
    assert _weld_crossing_to_network(QgsGeometry(), _line((0, 0), (1, 0)))[1] is False


# --- _crossing_between_contacts ---------------------------------------------

def _open_cut_both_sides():
    """Two parallel runs of open cut that a road-crossing drill passes through."""
    return _multi([(-50, 20), (50, 20)], [(-50, 80), (50, 80)])


def test_crossing_is_the_stretch_between_the_two_open_cuts():
    out, ok = _crossing_between_contacts(_line((0, 0), (0, 100)), _open_cut_both_sides())

    assert ok is True
    pl = out.asPolyline()
    assert pl[0].y() == pytest.approx(20.0)
    assert pl[-1].y() == pytest.approx(80.0)
    assert out.length() == pytest.approx(60.0)


def test_crossing_grows_a_short_drill_out_to_the_open_cut():
    """The drill spans only y=30..70 but the open cut is wider: it must extend."""
    out, ok = _crossing_between_contacts(_line((0, 30), (0, 70)), _open_cut_both_sides())

    assert ok is True
    pl = out.asPolyline()
    assert pl[0].y() == pytest.approx(20.0)
    assert pl[-1].y() == pytest.approx(80.0)


def test_crossing_gives_up_when_the_network_only_touches_one_side():
    """Contact on one side only would leave the stub the fix exists to remove."""
    out, ok = _crossing_between_contacts(_line((0, 0), (0, 100)), _line((-50, 20), (50, 20)))

    assert ok is False
    assert out.asWkt() == _line((0, 0), (0, 100)).asWkt()


def test_crossing_rejects_a_sliver_shorter_than_min_length():
    close_pair = _line((0, 0), (0, 100))
    out, ok = _crossing_between_contacts(close_pair, _open_cut_both_sides(), min_len_m=100.0)

    assert ok is False
    assert out.asWkt() == close_pair.asWkt()


# --- _eval_drop_feasibility -------------------------------------------------

def test_spare_duct_always_prefers_buried():
    ok, reason = _eval_drop_feasibility(_point(0, 0).asPoint(), [], [], "primary", "rock", True)

    assert (ok, reason) == (True, "spare_duct_available")


def test_major_road_blocks_buried_drop():
    ok, reason = _eval_drop_feasibility(_point(0, 0).asPoint(), [], [], "primary", None, False)

    assert (ok, reason) == (False, "major_road_crossing")


def test_terrain_constraint_blocks_buried_drop():
    ok, reason = _eval_drop_feasibility(_point(0, 0).asPoint(), [], [], "residential", "rock", False)

    assert (ok, reason) == (False, "terrain_constraint")


# `_eval_drop_feasibility` reads coordinates as lat/lon: `_approx_meters`
# scales degrees to metres, so these cases use degree-scale offsets (~11 m per
# 0.0001 deg of latitude) rather than projected metres.
_PREMISE = (13.0, 52.0)
_NET_11M = [(13.0, 52.0001), (13.0001, 52.0001)]


def test_far_from_network_blocks_buried_drop():
    ok, reason = _eval_drop_feasibility(
        _point(*_PREMISE).asPoint(), [_line((13.0, 52.01), (13.01, 52.01))], [], None, None, False,
        max_ug_drop_m=100.0,
    )

    assert (ok, reason) == (False, "distance_threshold")


def test_barrier_between_premise_and_network_blocks_buried_drop():
    """A barrier the drop would have to cross forbids the buried route."""
    ok, reason = _eval_drop_feasibility(
        _point(*_PREMISE).asPoint(),
        [_line(*_NET_11M)],
        [_line((13.0, 52.000095), (13.0001, 52.000095))],
        None, None, False,
    )

    assert (ok, reason) == (False, "prohibited_crossing")


def test_barrier_check_buffers_in_degrees_not_metres():
    """Characterises a real unit mismatch, not desired behaviour.

    `_approx_meters` treats the input as lat/lon degrees, but the barrier check
    calls `bg.buffer(5.0, 5)` — five *coordinate units*. On degree data that is
    a ~550 km corridor, so the check fires for a barrier ~4 km away. Fixing it
    means buffering with a degree-equivalent of 5 m; this test then changes.
    """
    ok, reason = _eval_drop_feasibility(
        _point(*_PREMISE).asPoint(),
        [_line(*_NET_11M)],
        [_line((13.0, 52.036), (13.0001, 52.036))],  # ~4 km north of the network
        None, None, False,
    )

    assert (ok, reason) == (False, "prohibited_crossing")


def test_plain_case_is_buried_by_default():
    ok, reason = _eval_drop_feasibility(
        _point(*_PREMISE).asPoint(), [_line(*_NET_11M)], [], "residential", "grass", False
    )

    assert (ok, reason) == (True, "ug_default")


# --- _safe_sink_fields ------------------------------------------------------

def test_safe_sink_fields_renames_the_case_insensitive_collision():
    from qgis.core import QgsField
    from qgis.PyQt.QtCore import QMetaType

    out, alias = _safe_sink_fields([QgsField("pdp_id", QMetaType.Type.Int),
                                    QgsField("PDP_ID", QMetaType.Type.Int)])

    assert [f.name() for f in out] == ["pdp_id", "PDP_ID_NEW"]
    assert alias == {"pdp_id": "pdp_id", "PDP_ID": "PDP_ID_NEW"}


def test_safe_sink_fields_keeps_unrelated_names_untouched():
    from qgis.core import QgsField
    from qgis.PyQt.QtCore import QMetaType

    fields = [QgsField("a", QMetaType.Type.Int), QgsField("b", QMetaType.Type.Int)]
    out, alias = _safe_sink_fields(fields)

    assert [f.name() for f in out] == ["a", "b"]
    assert alias == {"a": "a", "b": "b"}
