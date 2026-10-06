"""Where are the off-trench duct chords? Chamber joints, or mid-run?

For every duct sub-segment whose midpoint is off the trench network, classify
it by how far that midpoint is from the nearest chamber:

  * within a chamber-joint distance  -> the geometry was re-cut to reach a
    chamber (attr_enrich `_extend_ends` / `_snap_ends`), so the fix lives there
  * far from every chamber          -> the chord came out of the duct stage's
    own routing / projection

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/duct_chord_context.py <run_dir> [layer] [tol] [step]
"""
import math
import os
import sys

from qgis.core import QgsGeometry, QgsPointXY, QgsSpatialIndex, QgsVectorLayer


def load(path, name):
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        raise SystemExit("cannot open %s" % path)
    return lyr


def main():
    run_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    layer = sys.argv[2] if len(sys.argv) > 2 else "distribution_ducts"
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    step = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0

    trench = load(os.path.join(run_dir, "Final_Trenches.gpkg"), "Final_Trenches")
    ducts = load(os.path.join(run_dir, "%s.gpkg" % layer), layer)
    ch_path = os.path.join(run_dir, "Chambers.gpkg")
    chambers = load(ch_path, "Chambers") if os.path.isfile(ch_path) else None

    tidx = QgsSpatialIndex(trench.getFeatures())
    tgeoms = {tf.id(): tf.geometry() for tf in trench.getFeatures()
              if tf.geometry() is not None and not tf.geometry().isEmpty()}
    SEARCH_R = 25.0

    def tdist(px, py):
        pg = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        best = None
        for fid in tidx.intersects(pg.buffer(SEARCH_R, 8).boundingBox()):
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

    cidx = QgsSpatialIndex(chambers.getFeatures()) if chambers is not None else None
    cgeoms = {c.id(): c.geometry() for c in chambers.getFeatures()} if chambers else {}
    cxy = {}
    if chambers is not None:
        for c in chambers.getFeatures():
            g = c.geometry()
            if g is None or g.isEmpty():
                continue
            cxy[c.id()] = g.asPoint()
    CIDX = QgsSpatialIndex(chambers.getFeatures()) if chambers is not None else None
    CGRID = None
    CELL = 25.0
    if chambers is not None:
        CGRID = {}
        for cid, p in cxy.items():
            CGRID.setdefault((int(p.x() // CELL), int(p.y() // CELL)), []).append(cid)

    def cdist(px, py):
        if CGRID is None:
            return None
        gx, gy = int(px // CELL), int(py // CELL)
        best = None
        for r in range(0, 4):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for cid in CGRID.get((gx + dx, gy + dy), ()):
                        p = cxy[cid]
                        d = math.hypot(p.x() - px, p.y() - py)
                        if best is None or d < best:
                            best = d
            if best is not None:
                break
        return best

    buckets = [(0.5, 0.0), (2.0, 0.5), (5.0, 2.0), (10.0, 5.0), (25.0, 10.0),
               (1e9, 25.0)]
    acc = [0.0] * len(buckets)
    tot = 0.0
    off_tot = 0.0
    chords = 0
    chord_rows = []
    for f in ducts.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        for part in (g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]):
            for vi in range(1, len(part)):
                a, b = part[vi - 1], part[vi]
                seg = math.hypot(b.x() - a.x(), b.y() - a.y())
                tot += seg
                k = max(1, int(math.ceil(seg / step)))
                for s in range(k):
                    t = (s + 0.5) / k
                    mx = a.x() + (b.x() - a.x()) * t
                    my = a.y() + (b.y() - a.y()) * t
                    d = tdist(mx, my)
                    if d <= tol:
                        continue
                    w = seg / k
                    off_tot += w
                    dc = cdist(mx, my)
                    dc = 1e9 if dc is None else dc
                    for bi, (hi, lo) in enumerate(buckets):
                        if lo <= dc < hi:
                            acc[bi] += w
                            break
                # A chord: a whole segment whose midpoint is off the trench.
                mx = (a.x() + b.x()) / 2.0
                my = (a.y() + b.y()) / 2.0
                if tdist(mx, my) > tol and seg > 0.3:
                    chords += 1
                    dc = cdist(mx, my)
                    chord_rows.append((seg, tdist(mx, my), f.id(),
                                       1e9 if dc is None else dc,
                                       vi, len(part)))
    print("--- %s vs Final_Trenches ---" % ducts.name())
    print("total length %.1f m; off-trench %.1f m (%.3f %%)"
          % (tot, off_tot, 100.0 * off_tot / max(tot, 1e-9)))
    print("off-trench length by distance to nearest chamber:")
    for bi, (hi, lo) in enumerate(buckets):
        if hi > 1e8:
            label = ">= %g m" % lo
        elif lo == 0.0:
            label = "< %g m" % hi
        else:
            label = "%g - %g m" % (lo, hi)
        print("  %-12s %9.1f m  %5.1f %% of off-trench" % (
            label, acc[bi], 100.0 * acc[bi] / max(off_tot, 1e-9)))
    print("off-trench whole segments (chords): %d" % chords)
    chord_rows.sort(reverse=True)
    print("--- worst 15 chords (seg_len, mid_off, fid, cham_dist, vpos, nverts) ---")
    for row in chord_rows[:15]:
        print("  seg=%.2f  off=%.2f  fid=%s  cham=%.1f  v%d/%d" % row)


if __name__ == "__main__":
    main()
