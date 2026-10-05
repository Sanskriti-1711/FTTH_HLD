# -*- coding: utf-8 -*-
"""Does ONE trench carry both ends of a tap? If so the route is the wrong answer.

_trench_connector() tries the network route first and only falls back to the
single-feature link when no route exists. A route can be a huge detour while one
trench runs right between the two ends — in which case the duct is being sent
the long way round for nothing.
"""
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
    p = os.path.join(RUN, "Final_Trenches.gpkg").replace("\\", "/")
    trench = QgsVectorLayer("%s|layername=Final_Trenches" % p, "t", "ogr")
    p2 = os.path.join(RUN, "Distribution_Ducts_Runs.gpkg").replace("\\", "/")
    dup = QgsVectorLayer("%s|layername=Distribution_Ducts_Runs" % p2, "d", "ogr")

    algo = DuctLayer()
    algo.ROUTE_DETOUR_MAX_X = 1e9
    algo.ROUTE_DETOUR_SLACK_M = 0.0

    corridor = QgsGeometry.unaryUnion(
        [f.geometry() for f in trench.getFeatures()]).buffer(0.5, 8)
    chords = []
    for f in dup.getFeatures():
        off = f.geometry().difference(corridor)
        if off.isEmpty():
            continue
        for pl in parts_of(off):
            if QgsGeometry.fromPolylineXY(pl).length() > 1.0:
                chords.append((pl[0], pl[-1], QgsGeometry.fromPolylineXY(pl).length()))

    shorter_by_link = 0
    link_len = 0.0
    route_len = 0.0
    worst = []
    for a, b, ch in chords:
        r = algo._trench_route(trench, (a.x(), a.y()), (b.x(), b.y()))
        lk = algo._trench_link(trench, (a.x(), a.y()), (b.x(), b.y()), 0.5)
        rl = r.length() if (r is not None and not r.isEmpty()) else None
        ll = lk.length() if (lk is not None and not lk.isEmpty()) else None
        if ll is not None and (rl is None or ll < rl):
            shorter_by_link += 1
            link_len += ll
            route_len += (rl if rl is not None else ch)
            worst.append((rl if rl is not None else ch, ll, ch))
    print("taps where ONE trench carries both ends better than the network route: "
          "%d of %d" % (shorter_by_link, len(chords)))
    if worst:
        worst.sort(key=lambda t: -(t[0] - t[1]))
        print("  their link total %.1f m vs network-route total %.1f m"
              % (link_len, route_len))
        print("  worst five (network route m, link m, chord m):")
        for rl, ll, ch in worst[:5]:
            print("    route %8.1f | link %7.1f | chord %6.1f" % (rl, ll, ch))


if __name__ == "__main__":
    main()
