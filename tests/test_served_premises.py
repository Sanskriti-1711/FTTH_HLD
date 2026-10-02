"""Served-premises register: which drop traces may count as service.

`ServedPremisesAlgorithm` links each Objects `ADDR_ID` against the published
Garden / Aerial drop layers and writes one auditable row per premise. The
SERVED verdict is deliberately ID-based (that is the register's contract), but
an ID alone is not enough: a drop row whose geometry is empty, degenerate or a
zero-length stub is not a constructed connection, and counting it claims
service that does not exist on the ground.

These tests pin that gate. They run under the QGIS interpreter because
`algorithms/served_premises.py` imports `qgis.core`:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py HLD_Planning_01/tests
"""

from __future__ import annotations

import pytest

from HLDPlanning.algorithms.served_premises import (
    ServedPremisesAlgorithm,
    is_usable_drop_geometry,
)

pytestmark = pytest.mark.usefixtures("qgis_app")

ALGO = ServedPremisesAlgorithm()


def _pts(coords):
    """(x, y) tuples -> QgsPointXY.

    This QGIS build's geometry factories require QgsPointXY, not raw tuples.
    """
    from qgis.core import QgsPointXY

    return [QgsPointXY(x, y) for x, y in coords]


def _line(*coords):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromPolylineXY(_pts(coords))


def _multiline(*parts):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromMultiPolylineXY([_pts(p) for p in parts])


# --- geometry gate -----------------------------------------------------------

def test_empty_geometry_is_not_usable():
    from qgis.core import QgsGeometry

    assert not is_usable_drop_geometry(QgsGeometry())
    assert not is_usable_drop_geometry(None)


def test_zero_length_line_is_not_usable():
    """A stub of two identical vertices has no laid cable on it."""
    assert not is_usable_drop_geometry(_line((1.0, 1.0), (1.0, 1.0)))


def test_short_but_real_line_is_usable():
    """Sub-metre connectors are real construction and must still count."""
    assert is_usable_drop_geometry(_line((0.0, 0.0), (0.4, 0.0)))


def test_normal_drop_line_is_usable():
    assert is_usable_drop_geometry(_line((0.0, 0.0), (12.0, 0.0)))


def test_multiline_drop_is_usable():
    """Designer drops are published as MultiLineString."""
    geom = _multiline([(0.0, 0.0), (6.0, 0.0)], [(6.0, 0.0), (6.0, 9.0)])
    assert is_usable_drop_geometry(geom)


def test_multiline_with_one_stub_part_is_rejected():
    """A collapsed part fails GEOS validity ("Too few points in geometry
    component") even though the row's total length is fine.

    Measured rather than assumed: length() reports 5.0 for this geometry, so
    the rejection comes from the validity check. That is the outcome we want —
    a row containing a degenerate part is a bad write, not a real drop.
    """
    geom = _multiline([(1.0, 1.0), (1.0, 1.0)], [(0.0, 0.0), (5.0, 0.0)])
    assert geom.length() == pytest.approx(5.0)   # the length check alone passes
    assert not geom.isGeosValid()                # GEOS is what rejects it
    assert not is_usable_drop_geometry(geom)


def test_all_stub_parts_is_not_usable():
    geom = _multiline([(1.0, 1.0), (1.0, 1.0)], [(2.0, 2.0), (2.0, 2.0)])
    assert not is_usable_drop_geometry(geom)


def test_point_is_not_a_drop_trace():
    """A bare point is not a connection, whatever its id says."""
    from qgis.core import QgsGeometry, QgsPointXY

    assert not is_usable_drop_geometry(
        QgsGeometry.fromPointXY(QgsPointXY(1.0, 1.0)))


def test_polygon_is_not_a_drop_trace():
    from qgis.core import QgsGeometry

    ring = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]
    geom = QgsGeometry.fromPolygonXY([_pts(ring)])
    assert not is_usable_drop_geometry(geom)


# --- the drop-address index --------------------------------------------------

def _drop_layer(rows):
    """A memory MultiLineString layer of (addr, drop_id, coords) drop rows."""
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer
    from qgis.PyQt.QtCore import QMetaType

    lyr = QgsVectorLayer("MultiLineString?crs=EPSG:25833", "drops", "memory")
    lyr.dataProvider().addAttributes([
        QgsField("ADDR_ID", QMetaType.Type.QString),
        QgsField("DROP_ID", QMetaType.Type.QString),
    ])
    lyr.updateFields()
    features = []
    for addr, drop_id, coords in rows:
        feat = QgsFeature(lyr.fields())
        feat.setGeometry(QgsGeometry.fromMultiPolylineXY([_pts(coords)]))
        feat["ADDR_ID"] = addr
        feat["DROP_ID"] = drop_id
        features.append(feat)
    lyr.dataProvider().addFeatures(features)
    lyr.updateExtents()
    return lyr


def test_address_index_collects_every_id():
    lyr = _drop_layer([
        ("A1", "D1", [(0.0, 0.0), (5.0, 0.0)]),
        ("A2", "D2", [(5.0, 0.0), (5.0, 5.0)]),
    ])

    assert ALGO._drop_addresses(lyr) == {"A1", "A2"}


def test_address_index_splits_a_comma_list():
    """Spine rows fan out one address per row, but a joined value must not
    silently drop the extra premises."""
    lyr = _drop_layer([("A1,A2", "D1", [(0.0, 0.0), (5.0, 0.0)])])

    assert ALGO._drop_addresses(lyr) == {"A1", "A2"}


def test_address_index_ignores_blank_ids():
    lyr = _drop_layer([
        ("", "D1", [(0.0, 0.0), (5.0, 0.0)]),
        ("A2", "D2", [(5.0, 0.0), (5.0, 5.0)]),
    ])

    assert ALGO._drop_addresses(lyr) == {"A2"}


def test_address_index_skips_a_degenerate_drop_row():
    """The regression these tests exist for: a stub row carrying a real address
    used to mark the premise SERVED."""
    lyr = _drop_layer([
        ("A1", "D1", [(1.0, 1.0), (1.0, 1.0)]),   # zero length
        ("A2", "D2", [(5.0, 0.0), (5.0, 5.0)]),   # real
    ])

    assert ALGO._drop_addresses(lyr) == {"A2"}


def test_address_index_on_empty_or_missing_layer():
    from qgis.core import QgsVectorLayer

    assert ALGO._drop_addresses(None) == set()
    empty = QgsVectorLayer("MultiLineString?crs=EPSG:25833", "empty", "memory")
    assert ALGO._drop_addresses(empty) == set()
