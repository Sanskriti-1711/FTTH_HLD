# -*- coding: utf-8 -*-
"""Run stamp_region_identity on a COPY of a run and report before/after coverage.

Usage:
    unset PYTHONPATH && PYTHONPATH=HLD_Planning_01 python tmp/identity_test.py <run_dir>
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath("HLD_Planning_01"))

from HLDPlanning.utils import attr_enrich  # noqa: E402
from HLDPlanning.utils.attr_enrich import stamp_region_identity  # noqa: E402

FILES = ("Polygons.gpkg", "PDPs.gpkg", "MFG.gpkg", "Chambers.gpkg",
         "Trench_Nodes.gpkg", "Final_Trenches.gpkg", "Feeder_Trench.gpkg",
         "Distribution_Trench.gpkg", "Garden_Trench.gpkg", "Feeder_Ducts.gpkg",
         "Feeder_Ducts_Runs.gpkg", "Feeder_Cable.gpkg",
         "Distribution_Cable.gpkg")


class FB(object):
    def __init__(self):
        self.lines = []

    def pushInfo(self, m):
        self.lines.append(str(m))

    def pushWarning(self, m):
        self.lines.append("WARN " + str(m))


def coverage(path):
    from osgeo import ogr
    ds = ogr.Open(path)
    if ds is None:
        return None
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    if "POLYGON_ID" not in names:
        return (0, 0)
    i = defn.GetFieldIndex("POLYGON_ID")
    tot = ok = 0
    for ft in lyr:
        tot += 1
        if str(ft.GetField(i) or "").strip():
            ok += 1
    return (ok, tot)


def main():
    src = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    work = tempfile.mkdtemp(prefix="identity_")
    for f in FILES:
        p = os.path.join(src, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(work, f))
    print("copied to %s" % work)
    print()
    print("layer                          before      after")
    before = {}
    for f in FILES:
        p = os.path.join(work, f)
        if os.path.isfile(p):
            before[f] = coverage(p)

    fb = FB()
    n = stamp_region_identity(work, fb)
    print("values written: %d" % n)
    for line in fb.lines:
        print("   " + line.strip())
    print()
    for f in FILES:
        p = os.path.join(work, f)
        if not os.path.isfile(p):
            continue
        b = before.get(f)
        a = coverage(p)
        if b is None or a is None:
            continue
        print("%-30s %6s      %6s" % (f, "%d/%d" % b, "%d/%d" % a))
    print()
    print("temp dir left at %s" % work)


if __name__ == "__main__":
    main()
