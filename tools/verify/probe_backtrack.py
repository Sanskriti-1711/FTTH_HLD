# -*- coding: utf-8 -*-
"""Are the 19x-route taps back-tracking along a single segment?

If both ends of a tap project onto the SAME segment of the network, the path
along the trench between them is just the distance between the two projections.
A graph that never subdivides that segment sends the route out to one end and
back — hundreds of metres for a few metres of tap. This separates that artifact
from a route that genuinely has to go round.
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
    def layer(fn):
        p = os.path.join(RUN, fn).replace("\\", "/")
        return QgsVectorLayer("%s|layername=%s" % (p, os.path.splitext(fn)[0]),
                              fn, "ogr")

    trench = layer("Final_Trenches.gpkg")
    dup = layer("Distribution_Ducts_Runs.gpkg")
    algo = DuctLayer()
    algo.ROUTE_DETOUR_MAX_X = 1e9          # accept every route; we judge here
    algo.ROUTE_DETOUR_SLACK_M = 0.0
    net = algo._route_network(trench)
    segs = net[4]

    corridor = QgsGeometry.unaryUnion(
        [f.geometry() for f in trench.getFeatures()]).buffer(0.5, 8)
    chords = []
    for f in dup.getFeatures():
        off = f.geometry().difference(corridor)
        if off.isEmpty():
            continue
        for p in parts_of(off):
            if QgsGeometry.fromPolylineXY(p).length() > 1.0:
                chords.append((p[0], p[-1]))

    same_seg = []
    other = []
    unmatched = 0
    for a, b in chords:
        r = algo._trench_route(trench, (a.x(), a.y()), (b.x(), b.y()))
        if r is None or r.isEmpty():
            unmatched += 1
            continue
        best = [None, None]
        for idx, (p, q, _kp, _kq) in enumerate(segs):
            for which, pt in ((0, a), (1, b)):
                d, fx, fy = DuctLayer._pt_to_segment(pt.x(), pt.y(), p, q)
                if best[which] is None or d < best[which][0]:
                    best[which] = (d, idx, fx, fy)
        if best[0][1] == best[1][1]:
            sub = math.hypot(best[0][2] - best[1][2], best[0][3] - best[1][3])
            ch = math.hypot(a.x() - b.x(), a.y() - b.y())
            same_seg.append((ch, r.length(), sub))
        else:
            ch = math.hypot(a.x() - b.x(), a.y() - b.y())
            other.append((ch, r.length()))

    print("chords routed for inspection: %d (unroutable %d)"
          % (len(same_seg) + len(other), unmatched))
    print()
    print("both ends on ONE segment (route out-and-back is pure artifact): %d"
          % len(same_seg))
    if same_seg:
        art = sum(max(0.0, rl - sub) for _c, rl, sub in same_seg)
        print("  real along-trench distance there : %.1f m" % sum(s for _c, _r, s in same_seg))
        print("  route the graph actually takes   : %.1f m"
              % sum(rl for _c, rl, _s in same_seg))
        print("  metres that are back-tracking    : %.1f m" % art)
        worst = sorted(same_seg, key=lambda t: -(t[1] - t[2]))[:5]
        for ch, rl, sub in worst:
            print("    chord %6.1f m | route %8.1f m | along-trench %6.1f m"
                  % (ch, rl, sub))
    print()
    print("ends on DIFFERENT segments (a route has to go round): %d" % len(other))
    if other:
        worst = sorted(other, key=lambda t: -t[1] / max(1e-9, t[0]))[:5]
        print("  worst ratios (chord m, route m):")
        for ch, rl in worst:
            print("    chord %6.1f m | route %8.1f m | %.1fx" % (ch, rl, rl / max(1e-9, ch)))


if __name__ == "__main__":
    main()
