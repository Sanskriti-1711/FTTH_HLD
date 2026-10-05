"""What does each detached feeder stub actually TOUCH, and on which tier?"""
import sys
from collections import defaultdict
from osgeo import ogr

RUN = sys.argv[1]
TOL = 0.05


def load(path, tag):
    ds = ogr.Open(path)
    if ds is None:
        return []
    out = []
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        out.append((tag, g.Clone()))
    ds = None
    return out


feeder = load(f"{RUN}/Feeder_Trench.gpkg", "Feeder")
dist = load(f"{RUN}/Distribution_Trench.gpkg", "Distribution")
garden = load(f"{RUN}/Garden_Trench.gpkg", "Garden")
final = load(f"{RUN}/Final_Trenches.gpkg", "Final")
print(f"feeder={len(feeder)} dist={len(dist)} garden={len(garden)} final={len(final)}")

# feeder components
par = list(range(len(feeder)))


def find(i):
    while par[i] != i:
        par[i] = par[par[i]]
        i = par[i]
    return i


for i in range(len(feeder)):
    gi = feeder[i][1]
    ei = gi.GetEnvelope()
    for j in range(i + 1, len(feeder)):
        gj = feeder[j][1]
        ej = gj.GetEnvelope()
        if ei[1] + TOL < ej[0] or ej[1] + TOL < ei[0] or ei[3] + TOL < ej[2] or ej[3] + TOL < ei[2]:
            continue
        if find(i) == find(j):
            continue
        if gi.Distance(gj) <= TOL:
            par[find(j)] = find(i)

comps = defaultdict(list)
for i in range(len(feeder)):
    comps[find(i)].append(i)
ranked = sorted(comps.values(), key=len, reverse=True)
print(f"{len(ranked)} feeder component(s); main {len(ranked[0])}")

others = dist + garden + final
for c in ranked[1:]:
    g, feat = feeder[c[0]][1], None
    length = sum(feeder[i][1].Length() for i in c)
    best = []
    for tag, og in others:
        d = g.Distance(og)
        if d <= 1.0:
            best.append((round(d, 3), tag))
    best.sort()
    print(f"  island {len(c)} feat, {length:.1f} m -> touches: {best[:6]}")
