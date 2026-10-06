"""Does the duct router's network hold any edge that is off the trench?

`_route_network` unions the trench and docks vertices onto passing spans; every
edge it hands the pathfinder is supposed to be a piece of trench. If a routed
duct is off the trench, the culprit is here; if every edge measures 0 m, the
off-trench chords can only come from the projection branch of the rebase.

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/net_edge_probe.py <run_dir> [tol]
"""
import math
import os
import sys
import time

from qgis.core import QgsGeometry, QgsPointXY, QgsSpatialIndex, QgsVectorLayer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from HLDPlanning.algorithms.duct_layer import DuctLayer  # noqa: E402


def main():
    run_dir = sys.argv[1]
    tol = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    trench = QgsVectorLayer(os.path.join(run_dir, "Final_Trenches.gpkg"),
                            "Final_Trenches", "ogr")
    if not trench.isValid():
        raise SystemExit("cannot open the trench layer")

    t0 = time.time()
    net = DuctLayer()._route_network(trench)
    print("network built in %.1fs" % (time.time() - t0))
    if net is None:
        raise SystemExit("no network")
    adj, edge_geom, edge_len, node_xy, segs = net
    print("nodes=%d edges=%d segments_seen=%d" % (len(node_xy), len(edge_geom), len(segs)))

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

    off_edges = []
    off_len = tot_len = 0.0
    for eid, g in edge_geom.items():
        if g is None or g.isEmpty():
            continue
        pl = g.asPolyline() if not g.isMultipart() else g.asMultiPolyline()[0]
        if len(pl) < 2:
            continue
        a, b = pl[0], pl[1]
        seg = math.hypot(b.x() - a.x(), b.y() - a.y())
        tot_len += seg
        d = tdist((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
        if d > tol:
            off_len += seg
            off_edges.append((seg, d, eid))
    print("edge length %.1f m; off-trench (>%g m) %.1f m = %.3f %%"
          % (tot_len, tol, off_len, 100.0 * off_len / max(tot_len, 1e-9)))
    print("off-trench edges: %d of %d" % (len(off_edges), len(edge_geom)))
    off_edges.sort(reverse=True)
    for row in off_edges[:15]:
        print("  seg=%.2f off=%.2f edge=%s" % row)


if __name__ == "__main__":
    main()
