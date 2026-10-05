# -*- coding: utf-8 -*-
"""Can the real distribution taps be routed along the trench network?

Takes the reference run's own duct-stage output, finds every piece of the
distribution duct that lies off the trench (these are the chords the tap drew
to reach couplers), and asks DuctLayer._trench_route whether the trench network
can carry each one instead.

Run under the QGIS interpreter (it drives the real QgsGeometry code):

    ./HLD_Planning_01/tools/qgis_python.cmd tmp/ductfix_dist_route.py [run_dir]
"""
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

TOL = 0.5


def layer(run_dir, fn, name):
    path = os.path.join(run_dir, fn).replace("\\", "/")
    lyr = QgsVectorLayer("%s|layername=%s" % (path, os.path.splitext(fn)[0]),
                         name, "ogr")
    if not lyr.isValid():
        raise SystemExit("cannot open %s" % fn)
    return lyr


def parts_of(geom):
    if geom is None or geom.isEmpty():
        return []
    try:
        ml = geom.asMultiPolyline()
    except TypeError:            # a bare LineString, not a multi-line
        ml = None
    if ml:
        return [p for p in ml if len(p) >= 2]
    pl = geom.asPolyline()
    return [pl] if pl and len(pl) >= 2 else []


def main(run_dir):
    trench = layer(run_dir, "Final_Trenches.gpkg", "trench")
    ducts = layer(run_dir, "Distribution_Ducts_Runs.gpkg", "ducts")
    pseudo = layer(run_dir, "Pseudo_HH.gpkg", "pseudo")

    merged = QgsGeometry.unaryUnion([f.geometry() for f in trench.getFeatures()])
    corridor = merged.buffer(TOL, 8)
    print("trench features : %d" % trench.featureCount())
    print("distribution route ducts : %d" % ducts.featureCount())
    print("pseudo object points     : %d" % pseudo.featureCount())

    chords = []
    total_off = 0.0
    for f in ducts.getFeatures():
        off = f.geometry().difference(corridor)
        if off.isEmpty():
            continue
        for p in parts_of(off):
            seg = QgsGeometry.fromPolylineXY(p)
            L = seg.length()
            total_off += L
            if L > 1.0:
                chords.append((L, p[0], p[-1]))
    print()
    print("distribution duct length off the trench : %.1f m" % total_off)
    print("off-trench chords >1 m                  : %d (%.1f m)"
          % (len(chords), sum(c[0] for c in chords)))

    algo = DuctLayer()
    DuctLayer.ROUTE_DETOUR_MAX_X = float(os.environ.get("DETOUR_X", "3"))
    DuctLayer.ROUTE_DETOUR_SLACK_M = float(os.environ.get("DETOUR_SLACK", "30"))
    DuctLayer.ROUTE_DOCK_TOL_M = float(os.environ.get("DOCK_TOL", "0.25"))
    print("[dock] tolerance %.2f m" % DuctLayer.ROUTE_DOCK_TOL_M)
    print("[cap] route <= %.1fx chord + %.0f m"
          % (DuctLayer.ROUTE_DETOUR_MAX_X, DuctLayer.ROUTE_DETOUR_SLACK_M))
    join = float(os.environ.get("ROUTE_JOIN_TOL_M", "0"))
    if join:
        DuctLayer.ROUTE_JOIN_TOL_M = join
        print("[override] ROUTE_JOIN_TOL_M = %g" % join)
    routed = 0
    routed_len = 0.0
    routed_geom = 0.0
    ratios = []
    failed = []
    for L, a, b in chords:
        r = algo._trench_route(trench, (a.x(), a.y()), (b.x(), b.y()))
        if r is not None and not r.isEmpty():
            routed += 1
            routed_len += L
            routed_geom += r.length()
            ratios.append((r.length() / max(1e-9, L), L, r.length()))
        else:
            failed.append((L, a, b))
    print()
    print("ROUTABLE along the trench : %d of %d chords  (%.1f m of %.1f m)"
          % (routed, len(chords), routed_len, sum(c[0] for c in chords)))
    print("routed length %.1f m vs the %.1f m of chord it replaces (%.2fx)"
          % (routed_geom, routed_len, routed_geom / max(1e-9, routed_len)))
    ratios.sort(reverse=True)
    if ratios:
        n = len(ratios)
        print("route/chord ratio: p50 %.1fx | p90 %.1fx | worst %.1fx"
              % (ratios[n // 2][0], ratios[max(0, n // 10)][0], ratios[0][0]))
        print("worst five (ratio, chord m, route m):")
        for rr in ratios[:5]:
            print("   %8.1fx | %7.1f m chord | %9.1f m route" % rr)
    print("=> distribution off-trench would be %.1f m (%.1f %% of %.1f m)"
          % (total_off - routed_len,
             100.0 * (total_off - routed_len) / 6999.6, 6999.6))

    if not failed:
        return
    # Why did the rest fail: unreachable, or reachable only the long way round?
    net = algo._route_network(trench)
    adj = net[0]
    from HLDPlanning.utils.graph import dijkstra_with_parents
    print()
    print("  failed chord | d(a->net) | d(b->net) | shortest network route | verdict")
    for L, a, b in sorted(failed, key=lambda t: -t[0]):
        ka, _pa = algo._attach_to_network(net, (a.x(), a.y()), 1.0)
        kb, _pb = algo._attach_to_network(net, (b.x(), b.y()), 1.0)
        if ka is None or kb is None:
            print("  %7.1f m  |    OFF THE NETWORK (a=%s, b=%s)"
                  % (L, ka is not None, kb is not None))
            continue
        dist, _parent = dijkstra_with_parents(ka, adj)
        if kb not in dist:
            print("  %7.1f m  |  on net     |  on net     | UNREACHABLE            | break" % L)
        else:
            cap = L * 6.0 + 150.0
            print("  %7.1f m  |  on net     |  on net     | %8.1f m (cap %.0f) | %s"
                  % (L, dist[kb], cap,
                     "detour > cap" if dist[kb] > cap else "should route"))


if __name__ == "__main__":
    rd = sys.argv[1] if len(sys.argv) > 1 else \
        "HLD_Planning_01/web/backend/outputs/f0426f446acd4b02ada8595e1bb3e3a9"
    main(os.path.abspath(rd))
