"""Time enrich_all standalone on a fresh copy of a finished run.

Usage: python tmp/time_enrich.py <src_run_dir> [<dest_dir>]
"""
import os
import shutil
import sys
import time

ROOT = os.path.abspath(".")
sys.path.insert(0, os.path.join(ROOT, "HLD_Planning_01"))

from HLDPlanning.utils import attr_enrich  # noqa: E402


class FB:
    def __init__(self):
        self.lines = []

    def pushInfo(self, m):
        self.lines.append(m)
        print("   |", m, flush=True)


def main(src, dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    fb = FB()
    t0 = time.time()
    attr_enrich.enrich_all(dest, fb, roads_lyr=None)
    print("\nENRICH SECONDS: %.1f" % (time.time() - t0))


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]),
         os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "tmp/enrich_time"))
