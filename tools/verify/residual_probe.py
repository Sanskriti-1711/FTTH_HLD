"""What is left off the trench after the walk — detour-cap refusals, or breaks?

For every whole duct segment whose midpoint is off the trench, ask the router for
the path between its ends with NO detour cap:

  * a path exists                     -> the walk's cap refused it; the chord is
                                         a line switch a longer trench path can
                                         replace (spike) 
  * no path at any length             -> the two ends really are on separate
                                         pieces of the network; the chord is the
                                         only way across a genuine break

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/residual_probe.py <run_dir> [layer] [tol]
"""
import math
import os
import sys

from qgis.core import QgsGeometry, QgsPointXY, QgsSpatialIndex, QgsVectorLayer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from HLDPlanning.algorithms.duct_layer import DuctLayer  # noqa: E402


def main():
    run_dir = sys.argv[1]
    layer = sys.argv[2] if len(sys.argv) > 2 else "distribution_ducts"
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

    trench = QgsVectorLayer(os.path.join(run_dir, "Final_Trenches.gpkg"),
                            "Final_Trenches", "ogr")
    ducts = QgsVectorLayer(os.path.join(run_dir, "%s.gpkg" % layer), layer, "ogr")
    alg = DuctLayer()
    alg._route_network(trench)

    idx = QgsSpatialIndex(trench.getFeatures())
    tgeoms = {tf.id(): tf.geometry() for tf in trench.getFeatures()
              if tf.geometry() is not None and not tf.geometry().isEmpty()}
    SEARCH_R = 25.0

    def tdist(px, py):
        pg = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        best = None
        for fid in idx.intersects(pg.buffer(SEARCH_R, 8).boundingBox()):
            tg = tgeoms.get(fid)
            if tg is None:
                continue
            near = tg.nearestPoint(pg)
            if near is None or near.isEmpty():
                continue
            np_ = near.asPoint()
            d = math.hypot(np_.x() - px, np_.y() - py)
            if best is None or d < best:
                best = d
        return SEARCH_R if best is None else best

    def nearest(pt_xy):
        pt = QgsPointXY(pt_xy)
        pg = QgsGeometry.fromPointXY(pt)
        best = None
        for fid in idx.intersects(pg.buffer(SEARCH_R, 8).boundingBox()):
            tg = tgeoms.get(fid)
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

    stick_fn = alg._reachable_trench_lookup(trench, DuctLayer.REBASE_STICK_REACH_M)

    chords = []
    for f in ducts.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for part in parts:
            for vi in range(1, len(part)):
                a, b = part[vi - 1], part[vi]
                seg = math.hypot(b.x() - a.x(), b.y() - a.y())
                if seg <= 0.3:
                    continue
                mx, my = (a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0
                if tdist(mx, my) > tol:
                    chords.append((seg, f.id(), vi, len(part), a, b, tdist(mx, my)))

    tot_off = sum(c[0] for c in chords)
    routable = []
    broken = []
    for seg, fid, vi, nv, a, b, off in chords:
        route = alg._trench_route(trench, (a.x(), a.y()), (b.x(), b.y()), 1.0,
                                  detour_ref_m=1e9)
        if route is None or route.isEmpty():
            broken.append((seg, fid, off))
        else:
            routable.append((seg, route.length(), fid, off))
    print("--- residual chords in %s (tol %g) ---" % (layer, tol))
    print("chords: %d  total off-length %.1f m" % (len(chords), tot_off))
    print("  routable if the cap allowed it : %d  (%.1f m of off-trench)"
          % (len(routable), sum(r[0] for r in routable)))
    print("  genuinely separate pieces      : %d  (%.1f m of off-trench)"
          % (len(broken), sum(r[0] for r in broken)))
    if routable:
        lens = sorted(r[1] for r in routable)
        print("  route lengths (m): p50=%.1f p90=%.1f max=%.1f  (chord p50=%.1f)"
              % (lens[len(lens) // 2], lens[int(0.9 * (len(lens) - 1))], lens[-1],
                 sorted(r[0] for r in routable)[len(routable) // 2]))
        ratios = sorted(r[1] / max(r[0], 1e-9) for r in routable)
        print("  route/chord ratio: p50=%.1f p90=%.1f max=%.1f"
              % (ratios[len(ratios) // 2], ratios[int(0.9 * (len(ratios) - 1))],
                 ratios[-1]))
        cap = [r for r in routable if r[1] <= r[0] * 3.0 + 30.0]
        print("  within the shipped cap (3x+30 m): %d" % len(cap))
    print("  worst left chords (len, fid, off):",
          [tuple(round(x, 1) for x in b[:3]) for b in sorted(broken, reverse=True)[:8]])

    # Why did the walk leave them? Replay the shipped decision on each one.
    why = {}
    stuck = []
    for seg, fid, vi, nv, a, b, off in chords:
        stay = stick_fn(a, b)
        if stay is None:
            k = "nothing reachable from a"
        elif stay[0] > DuctLayer.REBASE_STICK_MAX_M:
            k = "reachable line is >%.0f m away" % DuctLayer.REBASE_STICK_MAX_M
        elif not alg._chord_on_trench(a, stay[1], nearest):
            route = alg._trench_path_between(trench, a, stay[1])
            if route:
                k = "stick chord off-trench, route ok"
            else:
                free = alg._trench_route(trench, (a.x(), a.y()),
                                         (stay[1].x(), stay[1].y()), 1.0,
                                         detour_ref_m=1e9)
                k = ("stick chord off-trench, route refused" if free is None
                     else "stick chord off-trench, route refused (free route exists)")
                stuck.append((math.hypot(stay[1].x() - a.x(), stay[1].y() - a.y()),
                              off, fid))
        else:
            k = "stick should have worked"
        why[k] = why.get(k, 0) + 1
    if stuck:
        print("  stick-refused samples (chord a->stay, off, fid):",
              [tuple(round(x, 2) for x in r) for r in stuck[:8]])
    print("  why the walk left them:")
    for k, v in sorted(why.items(), key=lambda x: -x[1]):
        print("    %-40s %d" % (k, v))


if __name__ == "__main__":
    main()
