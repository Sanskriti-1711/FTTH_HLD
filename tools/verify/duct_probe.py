"""Measure how far the duct layers sit off the trench network.

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/duct_probe.py <run_dir> [layer] [tol] [step]

Off-trench is measured per sub-segment MIDPOINT (a vertex can sit exactly on
the trench while the chord between two vertices cuts across a gap), weighted by
sub-segment length. A vertex pass is reported next to it so the two failure
modes stay distinguishable:
    - vertices off the trench      -> a snapping problem
    - vertices on, midpoints off   -> a chord problem (geometry cuts corners)

Reports the worst features and, for a single feature, the per-vertex and
per-midpoint detail.
"""
import math
import os
import sys

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsSpatialIndex, QgsVectorLayer


def load(path, name):
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        raise SystemExit("cannot open %s" % path)
    return lyr


def pct(values, q):
    if not values:
        return 0.0
    vals = sorted(values)
    i = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[i]


def main():
    run_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    layer = sys.argv[2] if len(sys.argv) > 2 else "distribution_ducts"
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    step = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
    detail = os.environ.get("DUCT_DETAIL_FID")

    trench = load(os.path.join(run_dir, "Final_Trenches.gpkg"), "Final_Trenches")
    ducts = load(os.path.join(run_dir, "%s.gpkg" % layer), layer) if os.path.isfile(
        os.path.join(run_dir, "%s.gpkg" % layer)) else None
    if ducts is None:
        for nm in os.listdir(run_dir):
            if nm.lower() == "%s.gpkg" % layer.lower() or layer.lower() in nm.lower():
                ducts = load(os.path.join(run_dir, nm), layer)
                break
    if ducts is None:
        raise SystemExit("no duct layer matching %r in %s" % (layer, run_dir))
    print("layer  : %s (%d features) crs=%s" % (ducts.name(), ducts.featureCount(),
                                                ducts.crs().authid()))
    print("trench : %d features" % trench.featureCount())

    idx = QgsSpatialIndex(trench.getFeatures())
    tgeoms = {}
    tverts = set()
    for tf in trench.getFeatures():
        g = tf.geometry()
        if g is None or g.isEmpty():
            continue
        tgeoms[tf.id()] = g
        for part in (g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]):
            for p in part:
                tverts.add((round(p.x(), 3), round(p.y(), 3)))

    SEARCH_R = 25.0

    def nearest_pair(px, py):
        """(distance, trench point) capped at SEARCH_R."""
        pg = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        rect = pg.buffer(SEARCH_R, 8).boundingBox()
        best = None
        for fid in idx.intersects(rect):
            tg = tgeoms.get(fid)
            if tg is None:
                continue
            near = tg.nearestPoint(pg)
            if near is None or near.isEmpty():
                continue
            np_ = near.asPoint()
            d = math.hypot(np_.x() - px, np_.y() - py)
            if best is None or d < best[0]:
                best = (d, np_)
        if best is None:
            return SEARCH_R, None
        return best

    tot_len = off_len = 0.0
    vert_d = []
    off_feats = []
    nfeat = 0
    n_projected_like = 0
    for f in ducts.getFeatures(QgsFeatureRequest()):
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        nfeat += 1
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        f_len = f_off = 0.0
        f_max = 0.0
        f_verts = 0
        f_vert_off = 0
        for part in parts:
            for vi, p in enumerate(part):
                d = nearest_pair(p.x(), p.y())[0]
                vert_d.append(d)
                f_verts += 1
                if d > tol:
                    f_vert_off += 1
                if vi == 0:
                    continue
                a = part[vi - 1]
                seg = math.hypot(p.x() - a.x(), p.y() - a.y())
                f_len += seg
                k = max(1, int(math.ceil(seg / step)))
                for s in range(k):
                    t = (s + 0.5) / k
                    mx = a.x() + (p.x() - a.x()) * t
                    my = a.y() + (p.y() - a.y()) * t
                    d = nearest_pair(mx, my)[0]
                    f_max = max(f_max, d)
                    if d > tol:
                        f_off += seg / k
        # A projected line is a densified source line snapped point-wise; its
        # vertices land mid-span on the trench, not on trench vertices.
        on_vert = sum(1 for p in [q for part in parts for q in part]
                      if (round(p.x(), 3), round(p.y(), 3)) in tverts)
        n_all = sum(len(part) for part in parts)
        if n_all and on_vert / float(n_all) < 0.5 and f_verts > 4:
            n_projected_like += 1
        tot_len += f_len
        off_len += f_off
        if f_off > 0.0 or f_max > tol:
            off_feats.append((f_off, f_max, f.id(), f_len, n_all, on_vert))

    print("--- %s ---" % ducts.name())
    print("features with geometry : %d" % nfeat)
    print("total length           : %.1f m" % tot_len)
    print("off-trench (>%g m)      : %.1f m  = %.3f %%" % (tol, off_len,
                                                          100.0 * off_len / max(tot_len, 1e-9)))
    print("vertex dist (m)        : p50=%.3f p90=%.3f max=%.3f  verts=%d off=%d"
          % (pct(vert_d, 0.5), pct(vert_d, 0.9),
             max(vert_d) if vert_d else 0.0, len(vert_d),
             sum(1 for d in vert_d if d > tol)))
    print("midpoint-off features  : %d" % len(off_feats))
    print("projection-like feats  : %d (vertices mostly NOT trench vertices)" % n_projected_like)
    off_feats.sort(reverse=True)
    print("--- worst 15 (off_len, max_mid, fid, len, nverts, nverts_on_trench) ---")
    for row in off_feats[:15]:
        print("  %.1f  %.2f  fid=%s  len=%.1f  nverts=%d  on_vert=%d" % row)

    if detail:
        want = str(detail)
        for f in ducts.getFeatures():
            if str(f.id()) != want:
                continue
            g = f.geometry()
            print("--- detail fid=%s len=%.1f ---" % (f.id(), g.length()))
            print("fields: %s" % [fld.name() for fld in ducts.fields()])
            print("attrs : %s" % [f[nm] for nm in ducts.fields().names()][:24])
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            for pi, part in enumerate(parts):
                print("  part %d: %d vertices" % (pi, len(part)))
                for vi, p in enumerate(part):
                    d, np_ = nearest_pair(p.x(), p.y())
                    mark = "VERT" if (round(p.x(), 3), round(p.y(), 3)) in tverts else "    "
                    extra = ""
                    if vi:
                        a = part[vi - 1]
                        seg = math.hypot(p.x() - a.x(), p.y() - a.y())
                        mx = (p.x() + a.x()) / 2.0
                        my = (p.y() + a.y()) / 2.0
                        dmid = nearest_pair(mx, my)[0]
                        extra = "  seg=%.2f  mid_off=%.2f" % (seg, dmid)
                    print("    %s v%-4d %.2f,%.2f  off=%.3f%s"
                          % (mark, vi, p.x(), p.y(), d, extra))
            break


if __name__ == "__main__":
    main()
