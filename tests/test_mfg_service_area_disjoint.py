"""MFG service areas never cover the same ground twice.

The boundary is a buffered union of each area's member premise polygons; two
areas can overlap where neighbouring groups sit close together. ``_disjoin_service_areas``
trims that overlap, and this pins the rule: the lower MFG id keeps the contested
ground and the result does not depend on input order.

Runs under the QGIS interpreter (see ``tests/conftest.py``).
"""

from __future__ import annotations

import pytest

from HLDPlanning.algorithms.network_layer import _disjoin_service_areas


def _box(x0, y0, x1, y1):
    from qgis.core import QgsGeometry

    return QgsGeometry.fromWkt(
        f"Polygon (({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"
    )


def test_overlapping_areas_are_trimmed_to_zero_overlap(qgis_app):
    """Two areas sharing ground come back with the lower id keeping it."""
    first = _box(0, 0, 100, 100)
    second = _box(50, 0, 150, 100)

    out = _disjoin_service_areas([("MFG00001", first), ("MFG00002", second)])

    overlap = out["MFG00001"].intersection(out["MFG00002"]).area()
    assert overlap == pytest.approx(0.0, abs=1e-9)
    # The lower id keeps its full footprint; the later one loses only the sliver.
    assert out["MFG00001"].area() == pytest.approx(100 * 100)
    assert out["MFG00002"].area() == pytest.approx(50 * 100)


def test_result_is_independent_of_input_order(qgis_app):
    """Reversing the input cannot change which id owns the overlap."""
    forward = _disjoin_service_areas(
        [("MFG00001", _box(0, 0, 100, 100)), ("MFG00002", _box(50, 0, 150, 100))]
    )
    reverse = _disjoin_service_areas(
        [("MFG00002", _box(50, 0, 150, 100)), ("MFG00001", _box(0, 0, 100, 100))]
    )

    assert forward["MFG00001"].equals(reverse["MFG00001"])
    assert forward["MFG00002"].equals(reverse["MFG00002"])


def test_disjoint_areas_are_untouched(qgis_app):
    """Areas that already do not touch are published unchanged."""
    left = _box(0, 0, 40, 40)
    right = _box(100, 0, 140, 40)

    out = _disjoin_service_areas([("MFG00001", left), ("MFG00002", right)])

    assert out["MFG00001"].area() == pytest.approx(40 * 40)
    assert out["MFG00002"].area() == pytest.approx(40 * 40)


def test_boundaries_are_published_as_multipolygons(qgis_app):
    """The sink is MultiPolygon, so each boundary is converted to one."""
    from qgis.core import QgsWkbTypes

    out = _disjoin_service_areas([("MFG00001", _box(0, 0, 10, 10))])
    assert QgsWkbTypes.isMultiType(out["MFG00001"].wkbType())
