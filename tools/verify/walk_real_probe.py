"""Run the REAL `_walk_off_trench_chords` on the projected ducts and measure it.

The reconstructed runs are the pre-segmentation duct geometry, i.e. what the
rebase published: every vertex on the trench, chords cutting the corners. This
feeds those vertices through the shipped walk and measures the off-trench length
before and after with an independent distance function.

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/walk_real_probe.py <run_dir> [layer] [tol]
"""
import math
import os
import shutil
import sys
import time

from qgis.core import (QgsFeature, QgsFields, QgsGeometry, QgsPointXY,
                       QgsSpatialIndex, QgsVectorFileWriter, QgsVectorLayer,
                       QgsWkbTypes)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from HLDPlanning.algorithms.duct_layer import DuctLayer  # noqa: E402


def main():
    run_dir = sys.argv[1]
    layer = sys.argv[2] if len(sys.argv) > 2 else "distribution_ducts"
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    out_dir = os.environ.get("WALK_OUT", "")

    trench = QgsVectorLayer(os.path.join(run_dir, "Final_Trenches.gpkg"),
                            "Final_Trenches", "ogr")
    ducts = QgsVectorLayer(os.path.join(run_dir, "%s.gpkg" % layer), layer, "ogr")
    if not trench.isValid() or not ducts.isValid():
        raise SystemExit("cannot open the layers")

    alg = DuctLayer()
    idx = QgsSpatialIndex(trench.getFeatures())
    tgeoms = {tf.id(): tf.geometry() for tf in trench.getFeatures()
              if tf.geometry() is not None and not tf.geometry().isEmpty()}
    SEARCH_R = 50.0

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

    stick_fn = (alg._reachable_trench_lookup(
        trench, float(os.environ.get("WALK_REACH", str(DuctLayer.REBASE_STICK_REACH_M))))
        if os.environ.get("WALK_STICK", "1") == "1" else None)

    def measure(part):
        tot = off = 0.0
        for vi in range(1, len(part)):
            a, b = part[vi - 1], part[vi]
            seg = math.hypot(b.x() - a.x(), b.y() - a.y())
            tot += seg
            k = max(1, int(math.ceil(seg / 2.0)))
            for s in range(k):
                t = (s + 0.5) / k
                near = nearest(QgsPointXY(a.x() + (b.x() - a.x()) * t,
                                          a.y() + (b.y() - a.y()) * t))
                if near is None or near[0] > tol:
                    off += seg / k
        return tot, off

    tot_all = off_before = off_after = 0.0
    n_feat = n_off_feat = 0
    fixes = []
    t0 = time.time()
    for f in ducts.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        n_feat += 1
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        new_parts = []
        f_before = f_after = 0.0
        part_lens = []
        for part in parts:
            t_, o_ = measure(part)
            tot_all += t_
            f_before += o_
            part_lens.append(t_)
        off_before += f_before
        if f_before > 1e-9:
            n_off_feat += 1
            stats = {"walked": 0, "stuck": 0, "gap": 0}
            for part in parts:
                walked = alg._walk_off_trench_chords(trench, part, nearest, stats,
                                                     stick_fn=stick_fn)
                new_parts.append(walked)
            for part in new_parts:
                _t, o_ = measure(part)
                f_after += o_
            off_after += f_after
            fixes.append((stats["walked"], stats["stuck"], stats["gap"]))
            print("  fid=%-6s before=%6.2f after=%6.2f  walked=%-3d stuck=%-3d gap=%-3d"
                  % (f.id(), f_before, f_after, stats["walked"], stats["stuck"],
                     stats["gap"]))
        else:
            new_parts = parts
        if out_dir:
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromMultiPolylineXY(new_parts))
            _out_features.append(feat)

    print("--- real walk on %d features (%.1fs) ---" % (n_feat, time.time() - t0))
    print("total length        : %.1f m" % tot_all)
    print("off-trench before   : %.1f m (%.3f %%)"
          % (off_before, 100.0 * off_before / max(tot_all, 1e-9)))
    print("off-trench after    : %.1f m (%.3f %%)"
          % (off_after, 100.0 * off_after / max(tot_all, 1e-9)))
    print("features off-trench : %d of %d" % (n_off_feat, n_feat))
    print("chords walked       : %d" % sum(s[0] for s in fixes))
    print("chords stuck to line: %d" % sum(s[1] for s in fixes))
    print("chords left (gaps)  : %d" % sum(s[2] for s in fixes))

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for nm in ("Final_Trenches.gpkg", "Chambers.gpkg"):
            src = os.path.join(run_dir, nm)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(out_dir, nm))
        out_path = os.path.join(out_dir, "distribution_ducts.gpkg")
        if os.path.isfile(out_path):
            os.remove(out_path)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = "distribution_ducts"
        writer = QgsVectorFileWriter.create(out_path, QgsFields(),
                                            QgsWkbTypes.MultiLineString,
                                            ducts.crs(), ducts.transformContext(),
                                            opts)
        for feat in _out_features:
            writer.addFeature(feat)
        del writer
        print("wrote %s (%d features)" % (out_path, len(_out_features)))


_out_features = []

if __name__ == "__main__":
    main()
