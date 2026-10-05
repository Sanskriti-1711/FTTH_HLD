"""Which enrich pass breaks the feeder duct chain?

Copies a run's layers into tmp/ductprobe/, then replays the published-layer
passes one at a time (segment -> enrich -> merge -> absorb) and prints the
feeder duct components / MFG->PDP reachability after each step.

Usage: python tmp/replay_duct_passes.py <run_dir>
"""
import shutil
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "HLD_Planning_01"))

from HLDPlanning.utils import attr_enrich as AE  # noqa: E402

PROBE = HERE / "ductprobe"


def load(name, path):
    from osgeo import ogr
    ogr.UseExceptions()
    ds = ogr.Open(str(path))
    out = []
    for f in ds.GetLayer(0):
        g = f.GetGeometryRef()
        if g is None:
            continue
        gg = g.Clone()
        try:
            gg = gg.GetLinearGeometry()
        except Exception:
            pass
        parts = [gg] if gg.GetGeometryName() == "LINESTRING" else \
            ([gg.GetGeometryRef(i) for i in range(gg.GetGeometryCount())]
             if gg.GetGeometryName() == "MULTILINESTRING" else [])
        for p in parts:
            xy = [(p.GetX(i), p.GetY(i)) for i in range(p.GetPointCount())]
            for i in range(len(xy) - 1):
                out.append((xy[i], xy[i + 1]))
    return out


def d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def report(label, segs, mfg, pdps, snap=0.5):
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
            if d(p, q) <= snap:
                return i
        nodes.append(p)
        parent[len(nodes) - 1] = len(nodes) - 1
        return len(nodes) - 1

    for a, b in segs:
        ia, ib = node_at(a), node_at(b)
        if ia != ib:
            union(ia, ib)
    groups = defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(i)
    mi = min(range(len(nodes)), key=lambda i: d(mfg, nodes[i]))
    root = next(r for r, mem in groups.items() if mi in mem)
    ok = 0
    for p, _id in pdps:
        pi = min(range(len(nodes)), key=lambda i: d(p, nodes[i]))
        rr = next((r for r, mem in groups.items() if pi in mem), None)
        if rr == root:
            ok += 1
    print(f"  {label:<44} components {len(groups):>3}   MFG->PDP {ok}/{len(pdps)}")


def main():
    run = Path(sys.argv[1])
    if PROBE.exists():
        shutil.rmtree(PROBE)
    PROBE.mkdir(parents=True)
    for n in ("Feeder_Ducts", "Distribution_Ducts", "Drop_Ducts", "Chambers",
              "Final_Trenches", "MFG", "PDPs", "Coupleurs", "Objects"):
        src = run / f"{n}.gpkg"
        if src.exists():
            shutil.copy2(src, PROBE / f"{n}.gpkg")
    # Start from the duct builder's own (pre-enrich) output — the "runs" layer.
    shutil.copy2(run / "Feeder_Ducts_Runs.gpkg", PROBE / "Feeder_Ducts.gpkg")

    from osgeo import ogr
    ogr.UseExceptions()
    ds = ogr.Open(str(PROBE / "MFG.gpkg"))
    ml = ds.GetLayer(0)
    mfeat = ml.GetNextFeature()
    g = mfeat.GetGeometryRef()
    mfg = (g.GetX(), g.GetY())
    dsp = ogr.Open(str(PROBE / "PDPs.gpkg"))
    pl = dsp.GetLayer(0)
    pdps = []
    for f in pl:
        gg = f.GetGeometryRef()
        pdps.append(((gg.GetX(), gg.GetY()), f.GetFID()))

    p = lambda n: str(PROBE / f"{n}.gpkg")  # noqa: E731
    report("Feeder_Ducts_Runs (builder output)", load("x", p("Feeder_Ducts")), mfg, pdps)

    AE.segment_ducts_at_chambers(p("Feeder_Ducts"), p("Distribution_Ducts"),
                                 p("Chambers"), None)
    report("after segment_ducts_at_chambers", load("x", p("Feeder_Ducts")), mfg, pdps)

    AE.enrich_ducts(p("Feeder_Ducts"), p("Distribution_Ducts"), p("Drop_Ducts"),
                    p("Final_Trenches"), p("Chambers"), None)
    report("after enrich_ducts", load("x", p("Feeder_Ducts")), mfg, pdps)

    # The real published layer is the duct builder's *bin* layer, which carries
    # these run-level columns; the _Runs layer does not. Add them so the merge
    # below sees the same schema the pipeline feeds it.
    from osgeo import ogr as _ogr
    src = _ogr.Open(p("Feeder_Ducts"), 1)
    sl = src.GetLayer(0)
    have = {sl.GetLayerDefn().GetFieldDefn(i).GetName()
            for i in range(sl.GetLayerDefn().GetFieldCount())}
    extra = ["N_DUCTS", "WAYS_TOTAL", "WAYS", "CLUBS", "BUNDLE_LEN_M"]
    for nm in extra:
        if nm not in have:
            sl.CreateField(_ogr.FieldDefn(nm, _ogr.OFTInteger if nm != "BUNDLE_LEN_M"
                                          else _ogr.OFTReal))

    def count():
        d = _ogr.Open(p("Feeder_Ducts"))
        l = d.GetLayer(0)
        n = l.GetFeatureCount()
        d = None
        return n

    n_before = sl.GetFeatureCount()
    print("  rows after segmentation:", n_before)
    sl = None
    AE.merge_ducts_per_chamber_span(p("Feeder_Ducts"), None)
    n_after = count()
    print(f"  merge removed {n_before - n_after} row(s) "
          f"({n_before} -> {n_after})")
    report("after merge_ducts_per_chamber_span", load("x", p("Feeder_Ducts")), mfg, pdps)

    AE.absorb_chamber_stubs(p("Feeder_Ducts"), None, "Feeder ducts")
    n_after2 = count()
    print(f"  absorb removed {n_after - n_after2} row(s) "
          f"({n_after} -> {n_after2})")
    report("after absorb_chamber_stubs", load("x", p("Feeder_Ducts")), mfg, pdps)


if __name__ == "__main__":
    main()
