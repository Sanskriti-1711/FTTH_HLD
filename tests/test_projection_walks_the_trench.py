"""The projected distribution line must lie ON the trench, not across it.

The rebase's projection (rule D10) densifies a legacy line and snaps every sample
to its nearest trench point. That snap is discontinuous: consecutive samples can
land on DIFFERENT trench lines, and the chord between them then cuts the corner.
Measured on the CV1 2DE run: every projected vertex sat 0.00 m from the trench
yet 2,770 m of the layer (0.75 %, up to 4.4 m per chord) lay off it — and all of
it came from here, since the router's own edges measure 0.000 m off.

`_walk_off_trench_chords` now follows the trench between two such points, and
stays on the line the duct is already on when the network reaches the other line
only far away (a parallel street is not the same duct).

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py
"""

from __future__ import annotations

import math

import pytest

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.duct_layer import DuctLayer

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS_AUTHID = "EPSG:3857"


def _trench(geoms, name="trench"):
    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("TRENCH_ID", QMetaType.Type.QString)])
    lyr.updateFields()
    feats = []
    for i, coords in enumerate(geoms):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        f["TRENCH_ID"] = f"T{i + 1}"
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _nearest_fn(trench, search_r=50.0):
    """The rebase's own (distance, point) lookup, rebuilt for the test."""
    idx = QgsSpatialIndex(trench.getFeatures())
    geoms = {f.id(): f.geometry() for f in trench.getFeatures()
             if f.geometry() is not None and not f.geometry().isEmpty()}

    def nearest(pt_xy):
        pt = QgsPointXY(pt_xy)
        pg = QgsGeometry.fromPointXY(pt)
        best = None
        for fid in idx.intersects(pg.buffer(search_r, 8).boundingBox()):
            tg = geoms.get(fid)
            if tg is None:
                continue
            near = tg.nearestPoint(pg)
            if near is None or near.isEmpty():
                continue
            np_ = near.asPoint()
            d = math.hypot(np_.x() - pt.x(), np_.y() - pt.y())
            if best is None or d < best[0]:
                best = (d, np_, fid)
        return best

    return nearest


def _off_length(pts, nearest_fn, tol=0.5, step=1.0):
    """Length of the line whose midpoints sit more than ``tol`` off the trench."""
    off = 0.0
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        seg = math.hypot(b.x() - a.x(), b.y() - a.y())
        k = max(1, int(math.ceil(seg / step)))
        for s in range(k):
            t = (s + 0.5) / k
            near = nearest_fn(QgsPointXY(a.x() + (b.x() - a.x()) * t,
                                         a.y() + (b.y() - a.y()) * t))
            if near is None or near[0] > tol:
                off += seg / k
    return off


def _pts(coords):
    return [QgsPointXY(x, y) for x, y in coords]


# ── the chord test ────────────────────────────────────────────────────────────

def test_a_chord_along_the_trench_is_on_it():
    trench = _trench([[(0, 0), (100, 0)]])
    algo = DuctLayer()
    nearest = _nearest_fn(trench)
    assert algo._chord_on_trench(QgsPointXY(0, 0), QgsPointXY(80, 0), nearest)


def test_a_chord_across_the_street_is_not_on_the_trench():
    trench = _trench([[(0, 0), (100, 0)]])
    algo = DuctLayer()
    nearest = _nearest_fn(trench)
    # Both ends are ON the trench (0 m), the line between them is 4 m off it.
    assert not algo._chord_on_trench(QgsPointXY(0, 0), QgsPointXY(0, 4), nearest)


# ── the walk ──────────────────────────────────────────────────────────────────

def test_a_chord_around_a_corner_is_walked_along_the_trench():
    """The two ends are joined by the trench — so the duct goes the way it goes."""
    # Two features: a single-feature union comes back as a plain LineString,
    # which the router's `asMultiPolyline` cannot read (production is always
    # multipart), and the network would then be None.
    trench = _trench([[(0, 0), (0, 10)], [(0, 10), (4, 10)]])
    algo = DuctLayer()
    nearest = _nearest_fn(trench)
    stats = {"walked": 0, "stuck": 0, "gap": 0}
    out = algo._walk_off_trench_chords(
        trench, _pts([(0, 0), (4, 10)]), nearest, stats,
        stick_fn=algo._reachable_trench_lookup(trench, 30.0))

    assert stats["walked"] == 1 and stats["gap"] == 0 and stats["stuck"] == 0
    # The corner is now in the line and nothing is off the trench.
    assert any(abs(p.x()) < 1e-6 and abs(p.y() - 10) < 1e-6 for p in out)
    assert _off_length(out, nearest) == pytest.approx(0.0, abs=1e-6)


def test_a_sample_on_a_parallel_line_far_along_the_network_is_left_alone():
    """Stay on the line the duct is on: the other line is 180 m away by trench.

    The two streets are 4 m apart but only join at x=100, so crossing to the
    other one is not a 4 m move — it is a 180 m detour, and a straight chord
    across it is the one thing a duct may never be (rule D10).
    """
    trench = _trench([[(0, 0), (100, 0)],                       # the duct's line
                      [(100, 0), (100, 4)],                      # the only link
                      [(0, 4), (100, 4)]])                       # the parallel line
    algo = DuctLayer()
    nearest = _nearest_fn(trench)
    stats = {"walked": 0, "stuck": 0, "gap": 0}
    out = algo._walk_off_trench_chords(
        trench, _pts([(10, 0), (40, 4)]), nearest, stats,
        stick_fn=algo._reachable_trench_lookup(trench, 30.0))

    assert stats["stuck"] == 1 and stats["gap"] == 0
    assert out[-1].y() == pytest.approx(0.0, abs=1e-6)      # stayed on its line
    assert out[-1].x() == pytest.approx(40.0, abs=1e-6)     # advanced along it
    assert _off_length(out, nearest) == pytest.approx(0.0, abs=1e-6)


def test_a_chord_no_line_can_replace_is_kept_and_counted():
    """Two severed pieces: the line across the break is the only way over it."""
    trench = _trench([[(0, 0), (100, 0)], [(100, 4), (200, 4)]])
    algo = DuctLayer()
    nearest = _nearest_fn(trench)
    stats = {"walked": 0, "stuck": 0, "gap": 0}
    out = algo._walk_off_trench_chords(
        trench, _pts([(50, 0), (150, 4)]), nearest, stats,
        stick_fn=algo._reachable_trench_lookup(trench, 30.0))

    assert stats["gap"] == 1 and stats["walked"] == 0 and stats["stuck"] == 0
    assert out == _pts([(50, 0), (150, 4)])


def test_the_walk_reports_what_it_did_to_the_log(tmp_path):
    """The rebase's log line carries the walk's counts, never silently."""
    from qgis.core import QgsProcessingContext

    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", "distribution", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("REVIEW", QMetaType.Type.Int)])
    lyr.updateFields()
    f = QgsFeature(lyr.fields())
    # A courtyard walk between two severed trench pieces: projected, not routed.
    f.setGeometry(QgsGeometry.fromPolylineXY(_pts([(50, 1), (150, 3)])))
    f["REVIEW"] = 0
    pr.addFeatures([f])
    lyr.updateExtents()
    from qgis.core import QgsProject, QgsVectorFileWriter

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    path = str(tmp_path / "dist.gpkg")
    err, _m, _n, _l = QgsVectorFileWriter.writeAsVectorFormatV3(
        lyr, path, QgsProject.instance().transformContext(), opts)
    assert err == QgsVectorFileWriter.NoError

    algo = DuctLayer()
    trench = _trench([[(0, 0), (100, 0)], [(100, 4), (200, 4)]])

    class _Fb:
        def __init__(self):
            self.info = []

        def pushInfo(self, msg):
            self.info.append(str(msg))

        def text(self):
            return "\n".join(self.info)

    ctx = QgsProcessingContext()
    fb = _Fb()
    algo._rebase_distribution_output(path, trench, ctx, fb)
    text = fb.text()
    assert "projected onto the trench" in text
    assert "off-trench chord(s) back along the trench" in text
