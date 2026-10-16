"""A region's pseudo point with no connected trench is reached by a straight
connector, clipped to its own polygon (docs D9, same rule as duct_layer).

Before this, confinement left such points unconnected (28 on Berlin 24cd3573).
The connector must reach the point and must not extend past the region edge.
"""

from osgeo import ogr

from HLDPlanning.utils.attr_enrich import _line_from_coords, _line_parts, _point_geom


def _square(x0, y0, x1, y1):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _reach(region, foot, point, tol_m=1.0):
    # Same steps as _confine_one_layer's straight-connector fallback.
    env = region.Buffer(tol_m)
    link = [foot, point]
    got = _line_parts(_line_from_coords(link).Intersection(env))
    return got


def test_connector_inside_region_reaches_the_point():
    region = _square(0.0, 0.0, 100.0, 100.0)
    got = _reach(region, (10.0, 50.0), (40.0, 50.0))
    assert got, "connector inside the region must survive the clip"
    end = got[-1][-1]
    assert abs(end[0] - 40.0) < 1e-6 and abs(end[1] - 50.0) < 1e-6


def test_connector_is_clipped_at_the_region_edge():
    # The point lies outside the region: only the part inside is kept.
    region = _square(0.0, 0.0, 100.0, 100.0)
    got = _reach(region, (90.0, 50.0), (130.0, 50.0))
    assert got
    for coords in got:
        for x, _y in coords:
            assert x <= 101.0 + 1e-6, "connector must not run past the region"
