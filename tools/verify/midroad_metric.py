"""How much trench is drawn down the MIDDLE of a road?  (OGR only — no shapely)

A vertex is "in the middle of the road" when it is within `CAR_TOL` of a
carriageway CENTRELINE and further than `FOOT_MIN` from any footway line — i.e.
the trench is following the carriageway, not a mapped pavement. Sums the length
of consecutive such vertices, per part.

    unset PYTHONPATH && python tmp/midroad_metric.py <Final_Trenches.gpkg> <label>
"""
import math
import sys

from osgeo import ogr

CAR_TOL = 1.0
FOOT_MIN = 2.0
MIN_RUN = 3.0
CELL = 50.0

FOOT = {"footway", "path", "pedestrian", "cycleway", "steps", "bridleway",
        "sidewalk"}

path, label = sys.argv[1], sys.argv[2]


def bucket(geoms):
    """(cell -> geometry indices) index; geometry bounding boxes are recorded."""
    idx, boxes = {}, []
    for i, g in enumerate(geoms):
        env = g.GetEnvelope()          # (minx, maxx, miny, maxy)
        boxes.append(env)
        for gx in range(int(math.floor(env[0] / CELL)),
                        int(math.floor(env[1] / CELL)) + 1):
            for gy in range(int(math.floor(env[2] / CELL)),
                            int(math.floor(env[3] / CELL)) + 1):
                idx.setdefault((gx, gy), []).append(i)
    return idx, boxes


def nearest(index, boxes, geoms, x, y, radius, want_index=False):
    """Distance from (x, y) to the nearest geometry within ``radius``."""
    best = 1e9
    best_i = -1
    cx, cy = int(math.floor(x / CELL)), int(math.floor(y / CELL))
    span = int(radius / CELL) + 1
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint_2D(x, y)
    for gx in range(cx - span, cx + span + 1):
        for gy in range(cy - span, cy + span + 1):
            for i in index.get((gx, gy), ()):
                b = boxes[i]
                if (b[0] - x > best or x - b[1] > best
                        or b[2] - y > best or y - b[3] > best):
                    continue
                d = geoms[i].Distance(pt)
                if d < best:
                    best, best_i = d, i
    return (best, best_i) if want_index else best


ds = ogr.Open("tmp/roads_osm/roads_aoi_25833.gpkg")
lyr = ds.GetLayer(0)
ci = lyr.GetLayerDefn().GetFieldIndex("fclass")
foot, veh, veh_cls = [], [], []
for f in lyr:
    g = f.GetGeometryRef()
    if g is None:
        continue
    c = (f.GetField(ci) if ci >= 0 else "") or ""
    if c in FOOT:
        foot.append(g.Clone())
    else:
        veh.append(g.Clone())
        veh_cls.append(c)
fidx, fbox = bucket(foot)
vidx, vbox = bucket(veh)

_trenches_ds = ogr.Open(path)   # keep the Dataset alive: a temporary one is
                                # garbage-collected and invalidates its Layer
if _trenches_ds is None:
    raise SystemExit("cannot open %s" % path)
tl = _trenches_ds.GetLayer(0)
names = [tl.GetLayerDefn().GetFieldDefn(i).GetName()
         for i in range(tl.GetLayerDefn().GetFieldCount())]
i_id = names.index("TRENCH_ID") if "TRENCH_ID" in names else -1

total, parts, worst = 0.0, 0, []
by_class = {}
for f in tl:
    g = f.GetGeometryRef()
    if g is None:
        continue
    tid = f.GetField(i_id) if i_id >= 0 else "?"
    parts_g = ([g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
               if g.GetGeometryType() == ogr.wkbMultiLineString else [g])
    for p in parts_g:
        n = p.GetPointCount()
        if n < 2:
            continue
        flags, co = [], []
        for i in range(n):
            x, y = p.GetX(i), p.GetY(i)
            co.append((x, y))
            dv, vi = nearest(vidx, vbox, veh, x, y, 40.0, True)
            if dv > CAR_TOL or vi < 0:
                flags.append(None)
                continue
            df = nearest(fidx, fbox, foot, x, y, 40.0)
            flags.append(veh_cls[vi] if df >= FOOT_MIN else None)
        # segment-based on purpose: span splitting moves the part boundaries, so
        # a per-part run total is not comparable between two runs
        run = 0.0
        run_cls = {}
        for i in range(n - 1):
            if flags[i] and flags[i + 1]:
                run_cls[flags[i]] = run_cls.get(flags[i], 0.0) + 1.0
                seg = math.hypot(co[i + 1][0] - co[i][0], co[i + 1][1] - co[i][1])
                run += seg
                by_class[flags[i]] = by_class.get(flags[i], 0.0) + seg
        if run >= MIN_RUN:
            parts += 1
            total += run
            worst.append((round(run, 1), str(tid), co[0],
                          max(run_cls, key=run_cls.get)))
    del g

worst.sort(key=lambda w: -w[0])
print("%-28s middle-of-road stretches >= %.0f m: %3d parts | total %7.1f m"
      % (label, MIN_RUN, parts, total))
print("     by carriageway class: %s"
      % ", ".join("%s %.0f m" % (k, v)
                  for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])))
for run, tid, pt, cls in worst[:8]:
    print("     %-10s %7.1f m  %-12s @ %.0f,%.0f" % (tid, run, cls, pt[0], pt[1]))
