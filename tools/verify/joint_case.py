# -*- coding: utf-8 -*-
"""Dump every span that names one chamber, plus the trench near it.

    unset PYTHONPATH && python tmp/joint_case.py <run_dir> <layer_stem> <CHAMBER_ID>
"""
import os
import sys

from osgeo import ogr

sys.path.insert(0, os.path.abspath("HLD_Planning_01"))
from HLDPlanning.utils.attr_enrich import (  # noqa: E402
    _load_line_coords, _measure_along, _line_parts)

run = os.path.abspath(sys.argv[1])
STEM = sys.argv[2]
CID = sys.argv[3]


def main():
    ds = ogr.Open(os.path.join(run, "Chambers.gpkg"))
    for f in ds.GetLayer(0):
        if str(f.GetField("STRUCT_ID")) == CID:
            g = f.GetGeometryRef()
            ch = (g.GetX(), g.GetY())
    ds = None
    print("chamber %s at (%.2f, %.2f)" % (CID, ch[0], ch[1]))

    ds = ogr.Open(os.path.join(run, "%s.gpkg" % STEM))
    for f in ds.GetLayer(0):
        s = str(f.GetField("START_CHAMBER") or "")
        e = str(f.GetField("END_CHAMBER") or "")
        if CID not in (s, e):
            continue
        for part in _line_parts(f.GetGeometryRef()):
            pts = [(round(float(p[0]), 2), round(float(p[1]), 2)) for p in part]
            print("  span %s -> %s  len %.2f m  nverts %d"
                  % (s or "-", e or "-", f.GetField("SPAN_LEN_M") or 0, len(pts)))
            print("      %s ... %s" % (pts[:3], pts[-3:]))
    ds = None

    trench = _load_line_coords(os.path.join(run, "Final_Trenches.gpkg"))
    hits = []
    for cs in trench:
        m = _measure_along(cs, ch[0], ch[1])
        if m is None or m[0] > 2.0:
            continue
        hits.append((m[0], cs, m[1]))
    hits.sort(key=lambda h: h[0])
    for d, cs, meas in hits[:4]:
        print("  trench part: %d vert(s), chamber at measure %.2f (%.2f m off), "
              "starts (%.2f, %.2f) ends (%.2f, %.2f), length %.2f m"
              % (len(cs), meas, d, cs[0][0], cs[0][1], cs[-1][0], cs[-1][1],
                 sum(((cs[i + 1][0] - cs[i][0]) ** 2 + (cs[i + 1][1] - cs[i][1]) ** 2) ** 0.5
                     for i in range(len(cs) - 1))))


if __name__ == "__main__":
    main()
