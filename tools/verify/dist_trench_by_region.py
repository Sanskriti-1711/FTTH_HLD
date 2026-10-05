# -*- coding: utf-8 -*-
"""Which regions have a distribution SPINE (Distribution_Trench) at all?

The distribution duct is grouped per POLYGON_ID, but it can only group what the
cable/trench tier publishes: a region with no distribution spine line gets no
trunk cable and therefore no duct — its pseudo points have nothing of their own
to sit on. This splits the 31 regions into "has a spine" and "has none".

    unset PYTHONPATH && python tmp/dist_trench_by_region.py <run_dir>
"""
import sys
from collections import defaultdict

from osgeo import ogr

RUN = sys.argv[1]


def rows(name):
    ds = ogr.Open(f"{RUN}/{name}.gpkg")
    if ds is None:
        return []
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    out = []
    for f in lyr:
        out.append({n: f.GetField(n) for n in names})
    ds = None
    return out


def norm(v):
    return str(v or "").strip().upper()


spine = defaultdict(int)
spine_len = defaultdict(float)
for r in rows("Distribution_Trench"):
    key = norm(r.get("POLYGON_ID") or r.get("polygon_id"))
    spine[key or "(blank)"] += 1
    try:
        spine_len[key or "(blank)"] += float(r.get("length_m") or 0)
    except Exception:
        pass

pdp_by_region = {}
for r in rows("Polygons"):
    pid = norm(r.get("POLYGON_ID") or r.get("polygon_id"))
    for k in ("pdp_id", "PDP_ID", "PDP"):
        if r.get(k):
            pdp_by_region[pid] = norm(r[k])
            break

cable_kind = defaultdict(lambda: defaultdict(int))
for r in rows("Distribution_Cable"):
    kind = norm(r.get("CABLE_TYPE"))
    key = norm(r.get("POLYGON_ID") or r.get("polygon_id"))
    cable_kind[key][kind] += 1

regions = sorted({norm(r.get("POLYGON_ID") or r.get("polygon_id"))
                  for r in rows("Polygons")} - {""})

print("region        pdp        spine_rows  spine_m   cables")
none = []
for pid in regions:
    n = spine.get(pid, 0)
    kinds = dict(cable_kind.get(pid, {}))
    if not n:
        none.append(pid)
    print("%-13s %-10s %10d %8.1f   %s"
          % (pid, pdp_by_region.get(pid, "-"), n, spine_len.get(pid, 0.0), kinds))

print()
print("regions with NO distribution spine line: %d of %d  %s"
      % (len(none), len(regions), ", ".join(none)))
print("regions with no spine but a Drop cable:  %d"
      % sum(1 for p in none if cable_kind.get(p, {}).get("DROP")))
