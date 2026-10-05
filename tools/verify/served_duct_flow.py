"""Is the feeder duct chain whole on the payload the map actually draws?

Fetches `ducts`, `mfg` and `pdps` from the platform API for every HLD project
and, for the feeder rows (4-Way HDPE), reports the connected parts and how many
PDPs the MFG reaches. Coordinates are WGS84 degrees, so everything is projected
to metres about the project centre first.

Usage: python tmp/served_duct_flow.py [project_id ...]
"""
import collections
import json
import math
import subprocess
import sys

BASE = "http://localhost:8000/api/ftth"
TOKEN = open("tmp/token_sub.txt").read().strip()


def get(path):
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {TOKEN}",
                          f"{BASE}{path}"], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        return None


def lines(feat):
    g = feat["geometry"]
    if g["type"] == "LineString":
        return [g["coordinates"]]
    return g["coordinates"]


def parts_of(feats, mfg_pt, pdp_pts):
    pts = [mfg_pt] + pdp_pts
    lat0 = sum(p[1] for p in pts) / len(pts)
    mx = 111320 * math.cos(math.radians(lat0))

    def P(c):
        return (c[0] * mx, c[1] * 110540)

    nodes, parent = [], {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def node_at(p):
        for i, q in enumerate(nodes):
            if math.hypot(p[0] - q[0], p[1] - q[1]) <= 0.5:
                return i
        nodes.append(p)
        parent[len(nodes) - 1] = len(nodes) - 1
        return len(nodes) - 1

    for f in feats:
        for line in lines(f):
            xy = [P(c) for c in line]
            for i in range(len(xy) - 1):
                ia, ib = node_at(xy[i]), node_at(xy[i + 1])
                if ia != ib:
                    union(ia, ib)
    if not nodes:
        return 0, 0, []
    groups = collections.defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(i)

    def nearest_node(p):
        return min(range(len(nodes)),
                   key=lambda i: math.hypot(p[0] - nodes[i][0], p[1] - nodes[i][1]))

    mf = P(mfg_pt)
    mi = nearest_node(mf)
    root = next(r for r, m in groups.items() if mi in m)
    reached, stranded = 0, []
    for pid, pt in pdp_pts:
        pass
    return groups, root, mf


def main():
    projects = sys.argv[1:]
    if not projects:
        data = get("/hld/results/") or {}
        projects = [p["project_id"] for p in (data.get("projects") or [])]
    print(f"{'project':<36} {'feeder rows':>11} {'parts':>6} {'MFG->PDP':>9}")
    for pid in projects:
        ducts = get(f"/hld/results/{pid}/layers/ducts/")
        mfg = get(f"/hld/results/{pid}/layers/mfg/")
        pdps = get(f"/hld/results/{pid}/layers/pdps/")
        if not ducts or not mfg or not pdps:
            print(f"{pid:<36} {'-':>11} {'-':>6} {'-':>9}  (no layers)")
            continue
        mfeat = (mfg.get("features") or [])
        pfeats = (pdps.get("features") or [])
        if not mfeat or not pfeats:
            print(f"{pid:<36} {'-':>11} {'-':>6} {'-':>9}  (no MFG/PDP)")
            continue
        feeder = [f for f in (ducts.get("features") or [])
                  if f["properties"].get("DUCT_TYPE") == "4-Way HDPE"]
        mpt = mfeat[0]["geometry"]["coordinates"]
        ppts = [(f["properties"].get("PDP_ID"),
                 f["geometry"]["coordinates"]) for f in pfeats
                if f["geometry"]["type"] == "Point"]
        if not feeder:
            print(f"{pid:<36} {0:>11} {'-':>6} {'-':>9}  (no feeder rows)")
            continue
        pts = [mpt] + [p for _i, p in ppts]
        lat0 = sum(p[1] for p in pts) / len(pts)
        mx = 111320 * math.cos(math.radians(lat0))

        def P(c):
            return (c[0] * mx, c[1] * 110540)

        nodes, parent = [], {}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        def node_at(p):
            for i, q in enumerate(nodes):
                if math.hypot(p[0] - q[0], p[1] - q[1]) <= 0.5:
                    return i
            nodes.append(p)
            parent[len(nodes) - 1] = len(nodes) - 1
            return len(nodes) - 1

        for f in feeder:
            for line in lines(f):
                xy = [P(c) for c in line]
                for i in range(len(xy) - 1):
                    ia, ib = node_at(xy[i]), node_at(xy[i + 1])
                    if ia != ib:
                        union(ia, ib)
        groups = collections.defaultdict(list)
        for i in range(len(nodes)):
            groups[find(i)].append(i)

        def nearest(p):
            return min(range(len(nodes)),
                       key=lambda i: math.hypot(p[0] - nodes[i][0], p[1] - nodes[i][1]))

        mi = nearest(P(mpt))
        root = next(r for r, m in groups.items() if mi in m)
        reached, stranded = 0, []
        for pid_, pt in ppts:
            rr = next((r for r, m in groups.items() if nearest(P(pt)) in m), None)
            if rr == root:
                reached += 1
            else:
                stranded.append(pid_)
        flag = "" if not stranded else f"   stranded: {stranded[:6]}"
        print(f"{pid:<36} {len(feeder):>11} {len(groups):>6} "
              f"{reached:>4}/{len(ppts):<4}{flag}")


if __name__ == "__main__":
    main()
