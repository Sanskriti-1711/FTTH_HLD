"""An off-region duct piece that HOLDS THE SAME DUCT TOGETHER is kept.

Region confinement clips a duct corridor to its own polygon. Measured on Berlin
24cd3573, that cut the same duct (same duct_uid) into disconnected pieces inside
one polygon, 2-56 m apart, with a continuous trench under each gap. The pieces
are restored only when they join two in-region pieces of the SAME duct, and only
when they are short enough to be a crossing rather than a detour.
"""

from osgeo import ogr

from HLDPlanning.utils.attr_enrich import (
    BRIDGE_RESTORE_MAX_M,
    _restore_bridges,
)


class _Feat:
    """The only surface `_restore_bridges` uses on a feature."""

    def __init__(self, geom, uid):
        self._geom = geom
        self._uid = uid

    def GetGeometryRef(self):
        return self._geom

    def GetFieldIndex(self, name):
        return 0 if name == "duct_uid" else -1

    def GetField(self, index):
        return self._uid


def _line(*pts):
    line = ogr.Geometry(ogr.wkbLineString)
    for x, y in pts:
        line.AddPoint_2D(x, y)
    return line


def _square(x0, y0, x1, y1):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


REGION = _square(0.0, 0.0, 100.0, 100.0)


def test_a_short_off_region_piece_that_holds_the_duct_together_is_kept():
    # The corridor leaves the region at y=100 and comes straight back, 20 m
    # apart: the removed piece is 40 m long and both ends land on the two
    # in-region pieces of the same duct.
    geom = _line((10.0, 50.0), (30.0, 50.0), (30.0, 110.0),
                 (50.0, 110.0), (50.0, 50.0))
    f = _Feat(geom, "D1")
    parts_of = {}

    kept = geom.Intersection(REGION)
    from HLDPlanning.utils.attr_enrich import _line_parts
    parts_of[1] = _line_parts(kept)
    before = len(parts_of[1])
    assert before == 2, "the clip should have cut this duct in two"

    restored = _restore_bridges([(1, f, ("POLY00001",), [])], REGION, parts_of)
    assert restored == 1
    assert len(parts_of[1]) == before + 1


def test_a_long_off_region_piece_is_not_kept():
    # 120 m outside the region is a detour, not a crossing.
    geom = _line((10.0, 50.0), (30.0, 50.0), (30.0, 150.0),
                 (50.0, 150.0), (50.0, 50.0))
    f = _Feat(geom, "D1")
    parts_of = {1: [
        [(10.0, 50.0), (30.0, 50.0), (30.0, 100.0)],
        [(50.0, 100.0), (50.0, 50.0)],
    ]}
    assert BRIDGE_RESTORE_MAX_M < 120.0
    restored = _restore_bridges([(1, f, ("POLY00001",), [])], REGION, parts_of)
    assert restored == 0


def test_a_piece_of_a_different_duct_is_not_kept():
    # The removed piece belongs to duct B and only ONE of its ends lands on a
    # piece of duct B; the other lands on duct A. Joining them would merge two
    # different ducts, so nothing is restored.
    gone = _line((30.0, 100.0), (30.0, 110.0), (50.0, 110.0), (50.0, 100.0))
    a = _line((10.0, 50.0), (30.0, 100.0))       # duct A, in region
    b = _line((50.0, 100.0), (50.0, 50.0))       # duct B, in region
    group = [(1, _Feat(a, "A"), ("POLY00001",), []),
             (2, _Feat(b, "B"), ("POLY00001",), []),
             (3, _Feat(gone, "B"), ("POLY00001",), [])]
    parts_of = {1: [[(10.0, 50.0), (30.0, 100.0)]],
                2: [[(50.0, 100.0), (50.0, 50.0)]]}

    restored = _restore_bridges(group, REGION, parts_of)
    assert restored == 0
    assert len(parts_of[1]) == 1 and len(parts_of[2]) == 1


def test_a_piece_that_touches_nothing_is_not_kept():
    # Entirely outside the region and touching no in-region piece.
    geom = _line((200.0, 200.0), (230.0, 200.0))
    f = _Feat(geom, "D1")
    parts_of = {}
    restored = _restore_bridges([(1, f, ("POLY00001",), [])], REGION, parts_of)
    assert restored == 0
    assert parts_of == {}
