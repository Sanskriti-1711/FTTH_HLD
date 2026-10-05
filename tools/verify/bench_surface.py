"""Split verify_surface_geometry into its geometry check vs the advisory AI review.

Copies the produced Final_Trenches + roads into a scratch dir, then times the
check with SURFACE_AI_REVIEW unset (geometry only) and with it set (geometry +
Gemini imagery review).  Read-only for the real outputs.

    "C:/Program Files/QGIS 3.44.6/bin/python-qgis.bat" tmp/bench_surface.py <out_dir>
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from HLDPlanning.utils import attr_enrich  # noqa: E402


def run(out_dir, enabled):
    scratch = tempfile.mkdtemp(prefix="bench_surface_")
    try:
        shutil.copy2(os.path.join(out_dir, "Final_Trenches.gpkg"), scratch)
        os.makedirs(os.path.join(scratch, "inputs"), exist_ok=True)
        shutil.copy2(os.path.join(out_dir, "inputs", "roads.geojson"),
                     os.path.join(scratch, "inputs", "roads.geojson"))
        if enabled:
            os.environ["SURFACE_AI_REVIEW"] = "1"
        else:
            os.environ.pop("SURFACE_AI_REVIEW", None)
        t0 = time.time()
        report = attr_enrich.verify_surface_geometry(
            scratch, None, os.path.join(scratch, "inputs", "roads.geojson"))
        dt = time.time() - t0
        return dt, report
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main():
    out_dir = sys.argv[1]
    dt, report = run(out_dir, enabled=False)
    print("geometry-only : %.1fs  checked=%d agreed=%d contradictions=%d uncertain=%d"
          % (dt, report["checked"], report["agreed"],
             len(report["flags"]), report["uncertain"]))
    dt2, _ = run(out_dir, enabled=True)
    print("with AI review: %.1fs  (advisory Gemini imagery pass adds ~%.1fs)"
          % (dt2, dt2 - dt))


if __name__ == "__main__":
    main()
