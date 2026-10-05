# -*- coding: utf-8 -*-
"""Do the two spans at a long-route tap touch? A segment-to-segment touch would
be a junction my dock pass cannot see (it docks vertices onto spans, not spans
onto spans), and would explain a 193 m route for a 7.5 m tap."""
import math
import os
import sys

from qgis.core import QgsApplication, QgsGeometry, QgsVectorLayer

QGIS_PREFIX = r"C:\Program Files\QGIS 3.44.6\apps\qgis"
if os.path.isdir(QGIS_PREFIX):
    QgsApplication.setPrefixPath(QGIS_PREFIX, True)
_app = QgsApplication([], False)
_app.initQgis()
sys.path.insert(0, os.path.abspath("HLD_Planning_01"))
from HLDPlanning.algorithms.duct_layer import DuctLayer   # noqa: E402

RUN = ("HLD_Planning_01/web/backend/outputs/"
       "f0426f446acd4b02ada8595e1bb3e3a9")


def parts_of(g):
    if g is None or g.isEmpty():
        return []
    try:
        ml = g.asMultiPolyline()
    except TypeError:
        ml = None
    if ml:
        return [p for p in ml if len(p) >= 2]
    try:
        pl = g.asPolyline()
    except TypeError:
        return []
    return [pl] if pl and len(pl) >= 2 else []


def main():
    def layer(fn):
        q = os.path.join(RUN, fn).replace("\\", "/")
        return QgsVectorLayer("%s|layername=%s" % (q, os.path.splitext(fn)[0]),
                              fn, "ogr")

    trench = layer("Final_Trenches.gpkg")
    dup = layer("Distribution_Ducts_Runs.gpkg")
    names = trench.fields().names()
    feats = list(trench.getFeatures())

    algo = DuctLayer()
    algo.ROUTE_DETOUR_MAX_X = 1e9
    algo.ROUTE_DETOUR_SLACK_M = 0.0

    corridor = QgsGeometry.unaryUnion(
        [f.geometry() for f in feats]).buffer(0.5, 8)
    chords = []
    for f in dup.getFeatures():
        off = f.geometry().difference(corridor)
        if off.isEmpty():
            continue
        for pl in parts_of(off):
            if QgsGeometry.fromPolylineXY(pl).length() > 1.0:
                chords.append((pl[0], pl[-1],
                               QgsGeometry.fromPolylineXY(pl).length()))

    def nearest_feature(pt):
        best = None
        for f in feats:
            d = f.geometry().distance(QgsGeometry.fromPointXY(pt))
            if best is None or d < best[0]:
                best = (d, f)
        return best

    rows = []
    for a, b, ch in chords:
        r = algo._trench_route(trench, (a.x(), a.y()), (b.x(), b.y()))
        rl = r.length() if (r is not None and not r.isEmpty()) else None
        if rl is None or rl <= ch * 3.0 + 30.0:
            continue
        _da, fa = nearest_feature(a)
        _db, fb = nearest_feature(b)
        touch = fa.geometry().distance(fb.geometry()) if fa.id() != fb.id() else 0.0
        rows.append((ch, rl, touch, fa, fb))

    rows.sort(key=lambda t: -(t[1] - t[0]))
    print("long-route taps: %d" % len(rows))
    print()
    print("  chord |  route |  the two spans' own min distance | features")
    for ch, rl, touch, fa, fb in rows[:10]:
        def tag(f):
            return "%s/%s" % (f["TRENCH_ID"] if "TRENCH_ID" in names else "?",
                              f["SRC"] if "SRC" in names else "?")
        print("  %5.1f | %6.1f | %6.2f m | %-22s vs %-22s"
              % (ch, rl, touch, tag(fa), tag(fb)))
    if rows:
        n_touch = sum(1 for _c, _r, t, _a, _b in rows if t <= 0.05)
        n_close = sum(1 for _c, _r, t, _a, _b in rows if t <= 2.0)
        print()
        print("of %d: %d touch (<=5 cm) and %d come within 2 m"
              % (len(rows), n_touch, n_close))


if __name__ == "__main__":
    main()
