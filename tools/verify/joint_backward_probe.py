# -*- coding: utf-8 -*-
"""Quantify the ends left as ``no_route``: the chamber is UPSTREAM along the trench.

For every labelled end that the forward walk refuses, take the piece of the
trench between the end and the chamber in whichever direction it lies, and
report (a) its length and (b) how much of it lies ON duct already published —
a doubling-back would add redundant geometry and double-billed metres.

    unset PYTHONPATH && python tmp/joint_backward_probe.py <run_dir> [layer_stem]
"""
import os
import sys

from osgeo import ogr
from shapely import wkb
from shapely.geometry import LineString
from shapely.ops import unary_union

sys.path.insert(0, os.path.abspath("HLD_Planning_01"))
from HLDPlanning.utils.attr_enrich import (  # noqa: E402
    _load_line_coords, _measure_along, _substring_coords, _coords_len, _line_parts)

STEM = sys.argv[2] if len(sys.argv) > 2 else "Feeder_Ducts"
EXTEND_MAX = 25.0


def main():
    run = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    trench = _load_line_coords(os.path.join(run, "Final_Trenches.gpkg"))
    ch = {}
    ds = ogr.Open(os.path.join(run, "Chambers.gpkg"))
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        ch[str(f.GetField("STRUCT_ID"))] = (g.GetX(), g.GetY())
    ds = None

    spans = []          # (start_id, end_id, coords, shapely geometry)
    ds = ogr.Open(os.path.join(run, "%s.gpkg" % STEM))
    lyr = ds.GetLayer(0)
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        for part in _line_parts(g):
            spans.append((str(f.GetField("START_CHAMBER") or ""),
                          str(f.GetField("END_CHAMBER") or ""),
                          [(float(p[0]), float(p[1])) for p in part]))
    ds = None
    published = unary_union([LineString(c) for _s, _e, c in spans if len(c) >= 2])

    fwd = back = refused = 0
    lens = []
    overlap_fracs = []
    for start_id, end_id, coords in spans:
        for idx, cid in ((0, start_id), (-1, end_id)):
            if not cid or cid not in ch:
                continue
            ex, ey = coords[idx]
            cx, cy = ch[cid]
            if ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5 <= 1.5:
                continue                        # already at the structure
            near = None
            for cs in trench:
                m = _measure_along(cs, ex, ey)
                if m is None:
                    continue
                if near is None or m[0] < near[0]:
                    near = (m[0], cs, m[1])
            if near is None or near[0] > 1.0:
                continue                        # not on the network — out of scope
            mc = _measure_along(near[1], cx, cy)
            if mc is None or mc[0] > 2.0:
                refused += 1                    # chamber is not on this piece
                continue
            lo, hi = sorted((near[2], mc[1]))
            piece = _substring_coords(near[1], lo, hi)
            if len(piece) < 2:
                refused += 1
                continue
            if near[2] <= mc[1]:
                piece = piece[::-1]             # end -> chamber
            ln = _coords_len(piece)
            if ln > EXTEND_MAX:
                refused += 1
                continue
            if near[2] <= mc[1]:
                fwd += 1
            else:
                back += 1
            lens.append(ln)
            # how much of the patch is duct we have already published?
            try:
                inter = LineString(piece).intersection(published.buffer(0.5)).length
            except Exception:
                inter = 0.0
            overlap_fracs.append(inter / ln if ln else 0.0)

    n = len(lens)
    print("%s: %d end(s) walkable along the trench (%d forward, %d BACKWARD), "
          "%d refused (chamber not on the same piece / over the %.0f m limit)"
          % (STEM, n, fwd, back, refused, EXTEND_MAX))
    if n:
        lens.sort()
        overlap_fracs.sort()
        print("  patch length   : min %.2f p50 %.2f p90 %.2f max %.2f m | total %.1f m"
              % (lens[0], lens[n // 2], lens[int(n * 0.9)], lens[-1], sum(lens)))
        print("  lying on already-published duct: p50 %.0f%% p90 %.0f%% max %.0f%% "
              "| patch(es) fully doubled: %d"
              % (100 * overlap_fracs[n // 2], 100 * overlap_fracs[int(n * 0.9)],
                 100 * overlap_fracs[-1],
                 sum(1 for o in overlap_fracs if o > 0.9)))


if __name__ == "__main__":
    main()
