"""Verify on-the-map alignment: every layer vs the trench.

Reads the SAME payload the results page draws (GET /api/ftth/hld/results/<id>/
plus .../layers/<name>/), not the on-disk GPKG, so what this reports is what
the map shows.

The API serves WGS84 degrees, so every coordinate is projected to metres with a
local equirectangular projection centred on the trench — a degree of longitude
at Berlin's latitude is only ~0.68 of a degree of latitude, and without that
correction a "0.00002" reading cannot be read as metres at all.

Usage:
    python tmp/verify_map_alignment.py --list
    python tmp/verify_map_alignment.py <project_id> [<project_id> ...]
"""
import json
import math
import os
import sys
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("tmp/token_sub.txt").read().strip()
CACHE = "tmp/apicache"

TRENCH_LAYERS = ("trenches", "final_trenches", "trench_layer", "trench_design",
                 "trench_designer")
ON_TRENCH = ("feeder_ducts", "distribution_ducts", "drop_ducts",
             "feeder_cable", "distribution_cable", "drop_cable", "cables",
             "duits", "ducts", "coupleurs", "couplers", "chambers",
             "trench_nodes", "poles", "aerial_drops", "aerial_drop_trenches",
             "aerial_cable", "pdps", "mfg", "objects")
POINT_LAYERS = ("chambers", "coupleurs", "couplers", "objects", "premises",
                "pdps", "mfg", "poles", "trench_nodes", "aerial_drops")
# Which property each LINE_SPEC bucket keys on (ftth-map.js), so we can say
# whether the served payload actually carries the field the map matches.
BUCKET_FIELDS = {
    "trenches": "trench_type", "aerial_drops": "TRENCH_TYPE",
    "aerial_drop_trenches": "TRENCH_TYPE", "aerial_cable": "CABLE_TYPE",
    "ducts": "DUCT_TYPE", "cables": "CABLE_TYPE",
}

DEG_LAT_M = 111132.0


def get(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def layer_geojson(pid, name):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    os.makedirs(os.path.join(CACHE, pid), exist_ok=True)
    fp = os.path.join(CACHE, pid, safe + ".json")
    if os.path.exists(fp) and os.path.getsize(fp) > 0:
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    try:
        gj = get("/api/ftth/hld/results/%s/layers/%s/" % (pid, name))
    except Exception as exc:
        gj = {"__error__": str(exc)}
    json.dump(gj, open(fp, "w", encoding="utf-8"))
    return gj


def raw_parts(geom):
    """GeoJSON geometry -> (points, line_parts) in raw coordinate order."""
    if not geom:
        return [], []
    t = (geom.get("type") or "").lower()
    c = geom.get("coordinates")
    if t == "point":
        return [(c[0], c[1])], [[(c[0], c[1])]]
    if t in ("linestring", "multilinestring"):
        lines = [c] if t == "linestring" else c
        pts, out = [], []
        for ln in lines or []:
            seg = [(p[0], p[1]) for p in ln or []]
            pts.extend(seg)
            out.append(seg)
        return pts, out
    if t in ("polygon", "multipolygon"):
        polys = [c] if t == "polygon" else c
        pts, out = [], []
        for poly in polys or []:
            for ring in poly or []:
                seg = [(p[0], p[1]) for p in ring or []]
                pts.extend(seg)
                out.append(seg)
        return pts, out
    return [], []


class Proj(object):
    """Local equirectangular projection to metres about a WGS84 origin."""

    def __init__(self, lon0, lat0):
        self.lon0 = lon0
        self.lat0 = lat0
        self.kx = DEG_LAT_M * math.cos(math.radians(lat0))

    def __call__(self, p):
        return ((p[0] - self.lon0) * self.kx, (p[1] - self.lat0) * DEG_LAT_M)


def seg_segments(lines):
    segs = []
    for ln in lines:
        for i in range(len(ln) - 1):
            segs.append((ln[i], ln[i + 1]))
    return segs


def pt_seg_dist(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        projects = get("/api/ftth/hld/projects/")
        for p in projects:
            print("%s  %s  [%s]" % (p.get("project_id"), p.get("name"),
                                    p.get("status")))
        return

    union = "--union" in sys.argv
    strict = "--strict" in sys.argv
    pids = [a for a in sys.argv[1:] if not a.startswith("--")]
    for pid in pids:
        print("=" * 80)
        print("PROJECT", pid)
        status = get("/api/ftth/hld/results/%s/" % pid)
        layers = status.get("layers") or []
        if isinstance(layers, dict):
            layers = [dict(v, name=k) for k, v in layers.items()]
        names = [l.get("name") or l.get("layer") for l in layers]
        print("payload layers (%d): %s" % (len(names), ", ".join(names)))

        trench_name = next((n for n in names if n in TRENCH_LAYERS), None)
        if not trench_name:
            print("  !! no trench layer in the payload — cannot verify")
            continue

        tj = layer_geojson(pid, trench_name)
        tfeats = tj.get("features") or []
        tpts = []
        for f in tfeats:
            pts, _ = raw_parts(f.get("geometry"))
            tpts.extend(pts)
        if not tpts:
            print("  !! trench payload empty")
            continue
        proj = Proj(sum(p[0] for p in tpts) / len(tpts),
                    sum(p[1] for p in tpts) / len(tpts))
        tlines = []
        for f in tfeats:
            _, ln = raw_parts(f.get("geometry"))
            tlines.extend([[proj(p) for p in part] for part in ln])
        tsegs = seg_segments(tlines)
        ref_names = [trench_name]
        if union:
            # An aerial leg is never dug: it is a span between poles, so it is
            # published as its own layer and is NOT expected to touch a trench.
            # Measuring against the union answers "is every layer on the
            # network", which is the alignment question that matters.
            aj = layer_geojson(pid, "aerial_drop_trenches")
            extra = 0
            for f in aj.get("features") or []:
                _, ln = raw_parts(f.get("geometry"))
                for part in ln:
                    seg = [proj(p) for p in part]
                    tlines.append(seg)
                    extra += len(seg) - 1
            tsegs = seg_segments(tlines)
            ref_names.append("aerial_drop_trenches")
            print("aerial spans added to the reference: %d segment(s)" % extra)
        print("trench reference: %s  features=%d  vertices=%d  segments=%d"
              % (" + ".join(ref_names), len(tfeats), len(tpts), len(tsegs)))
        print("")

        rows = []
        for name in names:
            if name == trench_name or name not in ON_TRENCH:
                continue
            lj = layer_geojson(pid, name)
            feats = lj.get("features") or []
            is_pt = name in POINT_LAYERS
            dists = []
            for f in feats:
                pts, lines = raw_parts(f.get("geometry"))
                # EVERYTHING is projected first: the trench segments are already
                # in metres, so a raw-degree candidate would be measured against
                # the wrong space entirely.
                if is_pt:
                    cand = [proj(pts[0])] if pts else []
                elif strict:
                    # EVERY vertex: a long L-shaped run whose endpoints and
                    # midpoint happen to lie on the trench would otherwise
                    # report 0.00 while most of it is off.
                    cand = [proj(p) for ln in lines for p in ln]
                else:
                    cand = []
                    for ln in lines:
                        if not ln:
                            continue
                        seg = [proj(p) for p in ln]
                        cand.append(seg[0])
                        cand.append(seg[-1])
                        if len(seg) > 2:
                            cand.append(seg[len(seg) // 2])
                if not cand:
                    continue
                dists.append(min(pt_seg_dist(c, a, b)
                                 for c in cand for a, b in tsegs))
            rows.append((name, len(feats), dists))

        print("%-22s %6s %9s %9s %9s %8s" %
              ("layer", "n", "p50 m", "p90 m", "max m", ">5m"))
        for name, n, dists in rows:
            if not dists:
                print("%-22s %6d %9s %9s %9s %8s" % (name, n, "-", "-", "-", "-"))
                continue
            s = sorted(dists)
            p50 = s[len(s) // 2]
            p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
            over = sum(1 for d in s if d > 5.0)
            print("%-22s %6d %9.2f %9.2f %9.2f %8d" %
                  (name, len(s), p50, p90, max(s), over))
        worst = [(max(d) if d else -1, nm) for nm, _, d in rows]
        worst.sort(reverse=True)
        print("")
        print("worst-aligned layer: %s at %.2f m" % (worst[0][1], worst[0][0]))
        if worst[0][0] > 5.0:
            print("  !! %s is NOT on the trench on the map" % worst[0][1])

        # The map colours a line by a bucket field; a missing field falls back
        # to the layer-name alias (still amber for aerial), so report which.
        print("")
        print("bucket field present in the served payload:")
        for name, _, _ in rows:
            field = BUCKET_FIELDS.get(name)
            if not field:
                continue
            lj = layer_geojson(pid, name)
            vals = {}
            for f in lj.get("features") or []:
                v = (f.get("properties") or {}).get(field)
                key = "(missing)" if v is None or str(v).strip() == "" else str(v)
                vals[key] = vals.get(key, 0) + 1
            print("  %-22s %-12s %s" % (name, field, vals))


if __name__ == "__main__":
    main()
