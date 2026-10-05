# -*- coding: utf-8 -*-
"""Why does a labelled joint still not meet after the along-trench extension?

For every chamber named by two or more published duct spans, measure the gap
between the span ends that claim it, then ask whether the gap is closable on the
trench: are the two ends and the chamber all on ONE trench part, and does the
walk between the measures fit inside the extension limit?

    unset PYTHONPATH && python tmp/joint_diag.py <run_dir> [layer_stem]
"""
import os
import sys

from osgeo import ogr

sys.path.insert(0, os.path.abspath("HLD_Planning_01"))
from HLDPlanning.utils.attr_enrich import (  # noqa: E402
    _load_line_coords, _measure_along, _substring_coords, _coords_len)

STEM = sys.argv[2] if len(sys.argv) > 2 else "Feeder_Ducts"


def ends(path):
    """(chamber, x, y) for every labelled span end, both ends."""
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    idx = {defn.GetFieldDefn(i).GetName(): i for i in range(defn.GetFieldCount())}
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        ls = g.GetGeometryRef(0) if g.GetGeometryName() == "MULTILINESTRING" else g
        if ls is None or ls.GetPointCount() < 2:
            continue
        for field, which in (("START_CHAMBER", 0), ("END_CHAMBER", -1)):
            cid = str(f.GetField(field) or "")
            if not cid:
                continue
            x, y, _z = ls.GetPoint(which if which >= 0 else ls.GetPointCount() - 1)
            out.append((cid, float(x), float(y)))
    ds = None
    return out


def main():
    run = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    trench = _load_line_coords(os.path.join(run, "Final_Trenches.gpkg"))
    ch = {}
    ds = ogr.Open(os.path.join(run, "Chambers.gpkg"))
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        ch[str(f.GetField("STRUCT_ID"))] = (g.GetX(), g.GetY())
    ds = None

    by_chamber = {}
    for cid, x, y in ends(os.path.join(run, "%s.gpkg" % STEM)):
        by_chamber.setdefault(cid, []).append((x, y))

    open_joints = 0
    closable = 0
    print("%s: %d chamber(s) named by 2+ span ends" % (STEM, sum(
        1 for v in by_chamber.values() if len(v) >= 2)))
    for cid, pts in sorted(by_chamber.items()):
        if len(pts) < 2 or cid not in ch:
            continue
        cx, cy = ch[cid]
        far = [p for p in pts if ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 > 1.0]
        if len(far) < 2:
            continue          # the joint already meets (or only one end is open)
        open_joints += 1
        # is there ONE trench part carrying the chamber and both ends?
        diag = []
        for tag, (px, py) in (("chamber", (cx, cy)), ("endA", far[0]), ("endB", far[1])):
            best = None
            for cs in trench:
                m = _measure_along(cs, px, py)
                if m is None:
                    continue
                if best is None or m[0] < best[0]:
                    best = (m[0], cs, m[1])
            diag.append((tag, best))
        shared = None
        for cs in trench:
            ms = {}
            ok = True
            for tag, (px, py) in (("c", (cx, cy)), ("a", far[0]), ("b", far[1])):
                m = _measure_along(cs, px, py)
                if m is None or m[0] > 2.0:
                    ok = False
                    break
                ms[tag] = m[1]
            if ok:
                shared = ms
                break
        if shared:
            closable += 1
        gap = min(((far[0][0] - far[1][0]) ** 2 + (far[0][1] - far[1][1]) ** 2) ** 0.5,
                  max(((far[0][0] - cx) ** 2 + (far[0][1] - cy) ** 2) ** 0.5,
                      ((far[1][0] - cx) ** 2 + (far[1][1] - cy) ** 2) ** 0.5))
        print("  %-10s ends off %.2f/%.2f m  gap %.2f m  same trench part: %s"
              % (cid,
                 ((far[0][0] - cx) ** 2 + (far[0][1] - cy) ** 2) ** 0.5,
                 ((far[1][0] - cx) ** 2 + (far[1][1] - cy) ** 2) ** 0.5,
                 gap, shared if shared else "no"))
    print("  open joint(s): %d | closable on ONE trench part: %d"
          % (open_joints, closable))


if __name__ == "__main__":
    main()
