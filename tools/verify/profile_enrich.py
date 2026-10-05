"""Profile the attribute-enrichment step alone.

`enrich_all` is pure OGR, so it runs outside QGIS.  The step is 523 s of a
583 s pipeline, so it can be studied on a copy of a finished run while other
work continues.

Usage: python tmp/profile_enrich.py <src_run_dir> [<dest_dir>]
"""
import cProfile
import io
import os
import pstats
import shutil
import sys
import time

ROOT = os.path.abspath(".")
sys.path.insert(0, os.path.join(ROOT, "HLD_Planning_01"))

from HLDPlanning.utils import attr_enrich  # noqa: E402


def main(src, dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print("profiling enrich_all on", dest, flush=True)

    prof = cProfile.Profile()
    t0 = time.time()
    prof.enable()
    n = attr_enrich.enrich_all(dest, None, roads_lyr=None)
    prof.disable()
    dt = time.time() - t0
    print("enrich_all did %s pass(es) in %.1f s" % (n, dt), flush=True)

    out = os.path.join(os.path.dirname(dest), "enrich_profile.txt")
    st = pstats.Stats(prof)
    st.sort_stats("tottime")
    buf = io.StringIO()
    st.stream = buf
    st.print_stats(45)
    st.sort_stats("cumulative")
    st.print_stats(45)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    print("stats ->", out, flush=True)

    # A readable short list too.
    st2 = pstats.Stats(prof)
    entries = sorted(st2.stats.items(),
                     key=lambda kv: kv[1][3], reverse=True)[:35]
    print("\n=== top by tottime ===", flush=True)
    for (fn, _ln, name), (_cc, nc, tt, ct, _callers) in entries:
        print("  %8.2fs  %8.2fs cum  %6dx  %s" % (tt, ct, nc, name), flush=True)


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]),
         os.path.abspath(sys.argv[2] if len(sys.argv) > 2
                         else "tmp/enrich_prof"))
