# -*- coding: utf-8 -*-
"""Replay the published-duct passes from the duct stage output.

Rebuilds Feeder_Ducts.gpkg / Distribution_Ducts.gpkg exactly as the pipeline
does — starting from the duct stage's own output (_Runs), which is the state
the enrichment stage actually sees — then measures how much of each duct
leaves the trench network.

Run with the plain interpreter (attr_enrich is OGR-only):
    unset PYTHONPATH && python tmp/ductfix_replay.py <run_dir> [work_dir]
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath("HLD_Planning_01/HLDPlanning"))

from osgeo import ogr                                        # noqa: E402
from shapely import wkb                                      # noqa: E402
from shapely.ops import unary_union                          # noqa: E402

import importlib.util                                        # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "attr_enrich",
    os.path.abspath("HLD_Planning_01/HLDPlanning/utils/attr_enrich.py"))
enrich = importlib.util.module_from_spec(_spec)
sys.modules["attr_enrich"] = enrich
_spec.loader.exec_module(enrich)

NEEDED = (
    "Feeder_Ducts_Runs.gpkg",
    "Distribution_Ducts_Runs.gpkg",
    "Drop_Ducts.gpkg",
    "Final_Trenches.gpkg",
    "Chambers.gpkg",
)


def gpkgs(d):
    ds = ogr.Open(os.path.join(d, "Final_Trenches.gpkg"))
    return ds


def load_lines(path):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        out.append(wkb.loads(bytes(g.ExportToWkb())))
    return out


def off_report(work_dir, tol=0.5):
    tl = load_lines(os.path.join(work_dir, "Final_Trenches.gpkg"))
    corr = unary_union(tl).buffer(tol, cap_style=2, join_style=2)
    for fn in ("Feeder_Ducts.gpkg", "Distribution_Ducts.gpkg"):
        path = os.path.join(work_dir, fn)
        if not os.path.exists(path):
            continue
        rows = load_lines(path)
        tot = sum(g.length for g in rows)
        off = sum(g.difference(corr).length for g in rows)
        print("    %-28s %4d rows  %8.1f m  off %7.1f m (%.1f%%)"
              % (fn, len(rows), tot, off, 100 * off / tot if tot else 0))


def main(run_dir, work_dir):
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir)
    for fn in NEEDED:
        shutil.copy2(os.path.join(run_dir, fn), os.path.join(work_dir, fn))
    # The duct stage writes the route layer; the enrichment stage then rewrites
    # it into the published chamber-to-chamber layer. That is our starting state.
    for run, pub in (("Feeder_Ducts_Runs.gpkg", "Feeder_Ducts.gpkg"),
                     ("Distribution_Ducts_Runs.gpkg", "Distribution_Ducts.gpkg")):
        ds = ogr.Open(os.path.join(work_dir, run))
        drv = ogr.GetDriverByName("GPKG")
        src = ds.GetLayer(0)
        out = drv.CreateDataSource(os.path.join(work_dir, pub))
        out.CopyLayer(src, os.path.splitext(pub)[0])
        ds = None
        out = None
    p = lambda n: os.path.join(work_dir, n)                  # noqa: E731

    # A/B the end-snap without editing the source: DUCT_SNAP_M=0 disables it.
    snap_override = os.environ.get("DUCT_SNAP_M")
    if snap_override is not None:
        real = enrich._segment_layer_at_chambers

        def _wrapped(*a, **kw):
            kw["snap_ends_m"] = float(snap_override)
            return real(*a, **kw)
        enrich._segment_layer_at_chambers = _wrapped
        print("  [override] snap_ends_m = %s" % snap_override)

    print("  stage output (pre-publish):")
    off_report(work_dir)

    print("  publishing (segment -> enrich -> merge -> absorb) ...")
    enrich.segment_ducts_at_chambers(
        p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"),
        p("Chambers.gpkg"), None)
    enrich.enrich_ducts(
        p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"), p("Drop_Ducts.gpkg"),
        p("Final_Trenches.gpkg"), p("Chambers.gpkg"), None)
    enrich.merge_ducts_per_chamber_span(p("Feeder_Ducts.gpkg"), None)
    enrich.absorb_chamber_stubs(p("Feeder_Ducts.gpkg"), None, "Feeder ducts")
    enrich.absorb_chamber_stubs(p("Distribution_Ducts.gpkg"), None,
                                "Distribution ducts", mode="coincident", floor=2)
    enrich.propagate_duct_pdp_id(p("Distribution_Ducts.gpkg"), None)
    print("  PUBLISHED:")
    off_report(work_dir)


if __name__ == "__main__":
    rd = sys.argv[1] if len(sys.argv) > 1 else \
        "HLD_Planning_01/web/backend/outputs/f0426f446acd4b02ada8595e1bb3e3a9"
    wd = sys.argv[2] if len(sys.argv) > 2 else "tmp/ductfix_work"
    main(rd, wd)
