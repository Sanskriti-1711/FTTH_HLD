# -*- coding: utf-8 -*-
"""Replay the duct segmentation with the new joint closure, on a copy.

The published duct layers are already cut, so this rebuilds the state the
segmenter actually consumes: the duct stage's per-run output (`*_Ducts_Runs`,
written beside the published layers) is copied over the published names, then
the real `segment_ducts_at_chambers` runs against it with the trench passed in.

Usage:
    unset PYTHONPATH && PYTHONPATH=HLD_Planning_01 python tmp/joint_test.py <run_dir>
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath("HLD_Planning_01"))

from HLDPlanning.utils.attr_enrich import segment_ducts_at_chambers  # noqa: E402

KEEP = ("Polygons.gpkg", "PDPs.gpkg", "Chambers.gpkg", "Final_Trenches.gpkg",
        "Pseudo_HH.gpkg", "Coupleurs.gpkg", "Objects.gpkg", "MFG.gpkg",
        "Feeder_Ducts.gpkg", "Feeder_Ducts_Runs.gpkg",
        "Distribution_Ducts.gpkg", "Distribution_Ducts_Runs.gpkg",
        "Drop_Ducts.gpkg", "Feeder_Cable.gpkg", "Distribution_Cable.gpkg",
        "Trench_Nodes.gpkg")


class FB(object):
    def pushInfo(self, m):
        print("   " + str(m).strip())

    def pushWarning(self, m):
        print("   WARN " + str(m).strip())


def main():
    src = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    work = tempfile.mkdtemp(prefix="joints_")
    for f in KEEP:
        p = os.path.join(src, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(work, f))
    # the state the segmenter sees: one duct per run, not yet cut
    for pub, runs in (("Feeder_Ducts.gpkg", "Feeder_Ducts_Runs.gpkg"),
                      ("Distribution_Ducts.gpkg", "Distribution_Ducts_Runs.gpkg")):
        rp = os.path.join(work, runs)
        if os.path.isfile(rp):
            shutil.copy2(rp, os.path.join(work, pub))
    print("replay dir: %s" % work)
    print()
    # NO_TRENCH=1 replays the same input WITHOUT the joint closure, so the two
    # runs differ only by that pass (the A/B the numbers are quoted from).
    trench = None if os.environ.get("NO_TRENCH") else os.path.join(
        work, "Final_Trenches.gpkg")
    n = segment_ducts_at_chambers(
        os.path.join(work, "Feeder_Ducts.gpkg"),
        os.path.join(work, "Distribution_Ducts.gpkg"),
        os.path.join(work, "Chambers.gpkg"),
        FB(),
        trench_path=trench,
    )
    print()
    print("features written: %d" % n)
    print("REPLAY_DIR=%s" % work)


if __name__ == "__main__":
    main()
