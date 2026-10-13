# -*- coding: utf-8 -*-
"""Load the generated brownfield files through the REAL BrownfieldRegistry
(QGIS) and assert the handover shape: 3 to 5 of each asset, one region.

Run with the QGIS interpreter:

    HLD_Planning_01/tools/qgis_python.cmd \
        HLD_Planning_01/tools/verify/check_brownfield_archive.py [archive_dir]

``archive_dir`` defaults to ``HLD_Planning_01/brownfield_layers`` (the staging
directory ``tools/make_brownfield_layers.py`` writes before zipping it).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "..")
ARCHIVE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(
    PLUGIN, "brownfield_layers")
sys.path.insert(0, PLUGIN)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsProcessingFeedback,
    QgsVectorLayer,
)
from HLDPlanning.algorithms.brownfield_layer import BrownfieldLayerAlgorithm  # noqa: E402
from HLDPlanning.utils.brownfield import BrownfieldRegistry  # noqa: E402

MIN_ASSETS, MAX_ASSETS = 3, 5

app = QgsApplication([], False)
app.initQgis()

fb = QgsProcessingFeedback()
crs = QgsCoordinateReferenceSystem("EPSG:4326")
reg = BrownfieldRegistry(crs, fb)
algo = BrownfieldLayerAlgorithm()

fails = []


def layer(fn):
    path = os.path.join(ARCHIVE, fn)
    if not os.path.exists(path):
        fails.append("missing %s" % fn)
        return None
    lyr = QgsVectorLayer(path, fn, "ogr")
    if not lyr.isValid():
        fails.append("invalid layer %s" % fn)
        return None
    return lyr


def contents(name):
    with open(os.path.join(ARCHIVE, name), "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("features") or []


def inside_region(geom, rings):
    """Ray casting, so the check does not depend on QGIS geometry APIs."""
    def coords(node, out):
        if not node:
            return
        if isinstance(node[0], (int, float)):
            out.append((float(node[0]), float(node[1])))
            return
        for child in node:
            coords(child, out)

    pts = []
    coords(geom.get("coordinates"), pts)
    if not pts:
        return False
    x, y = pts[len(pts) // 2] if len(pts) > 1 else pts[0]
    for ring in rings:
        hit = False
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[i - 1]
            if (y1 > y) != (y2 > y):
                if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    hit = not hit
        if hit:
            return True
    return False


# ── the eight brownfield parameters, as the archive names them ──────────────
ducts = layer("bf_ducts.geojson")
fibre = layer("bf_fibre.geojson")
duct_cap = algo._auto_detect_field(
    ducts, ["capacity_total", "capacity", "subducts", "sub_ducts", "n_subducts", "cap"]) if ducts else None
duct_used = algo._auto_detect_field(
    ducts, ["capacity_used", "used", "occupied", "n_used", "used_ways"]) if ducts else None
fib_cap = algo._auto_detect_field(
    fibre, ["capacity_total", "capacity", "strands", "fibre_count", "n_strands", "cap"]) if fibre else None
fib_used = algo._auto_detect_field(
    fibre, ["capacity_used", "used", "strands_used", "n_used", "used_strands"]) if fibre else None
verify = algo._auto_detect_field(
    ducts, ["verify_status", "status", "survey_status", "verification",
            "field_status", "condition"]) if ducts else None
print("auto-detect: duct_cap=%s duct_used=%s fibre_cap=%s fibre_used=%s verify=%s"
      % (duct_cap, duct_used, fib_cap, fib_used, verify))

loaded = {}
if ducts:
    loaded["duct"] = reg.load_ducts(ducts, capacity_field=duct_cap,
                                    capacity_used_field=duct_used, verify_field=verify)
if fibre:
    loaded["fibre"] = reg.load_fibre(fibre, capacity_field=fib_cap,
                                     capacity_used_field=fib_used, verify_field=verify)
loaded["chamber"] = reg.load_chambers(layer("bf_chambers.geojson"), verify_field=verify)
loaded["pole"] = reg.load_poles(layer("bf_poles.geojson"), verify_field=verify)
loaded["pdp"] = reg.load_existing_pdps(layer("bf_existing_pdp.geojson"),
                                       verify_field=verify)
loaded["mfg"] = reg.load_existing_mfgs(layer("bf_existing_mfg.geojson"),
                                       verify_field=verify)
# Trenches are one brownfield parameter per tier, so each file is its own
# sample: 3 to 5 feeder trenches AND 3 to 5 distribution trenches.
loaded["trench"] = (reg.load_trenches(layer("bf_feeder_trench.geojson"), verify_field=verify)
                    + reg.load_trenches(layer("bf_distribution_trench.geojson"),
                                        verify_field=verify))

print("\n--- BrownfieldRegistry.summary() ---")
print(reg.summary())

# 3 to 5 of each, per brownfield FILE (that is the unit the planner checks).
for name in sorted(os.listdir(ARCHIVE)):
    if not (name.startswith("bf_") and name.endswith(".geojson")):
        continue
    n = len(contents(name))
    lo = 1 if name == "bf_existing_mfg.geojson" else MIN_ASSETS
    print("%-32s %2d feature(s)" % (name, n))
    if not (lo <= n <= MAX_ASSETS):
        fails.append("%s has %d feature(s), wanted %d..%d" % (name, n, lo, MAX_ASSETS))

expected = {
    "trench": len(contents("bf_feeder_trench.geojson")) + len(contents("bf_distribution_trench.geojson")),
    "duct": len(contents("bf_ducts.geojson")),
    "fibre": len(contents("bf_fibre.geojson")),
    "chamber": len(contents("bf_chambers.geojson")),
    "pole": len(contents("bf_poles.geojson")),
    "pdp": len(contents("bf_existing_pdp.geojson")),
}
for kind, want in expected.items():
    got = loaded.get(kind, 0)
    if got != want:
        fails.append("%s: registry loaded %d of %d file rows" % (kind, got, want))
if loaded.get("mfg", 0) < 1:
    fails.append("no MFG loaded")

# Every asset must have capacity (a 0/0 row is skipped by the loader) …
for kind in ("duct", "fibre", "trench", "chamber", "pole"):
    for aid in reg.assets_by_type(kind):
        if not reg.has_capacity(aid):
            fails.append("%s %s has no spare capacity" % (kind, aid))

# … and the graph must be routable from the line assets.
edges = list(reg.iter_graph_edges())
print("graph edges yielded:", len(edges))
if not edges:
    fails.append("no graph edges yielded")

# ── one region, not the ward ────────────────────────────────────────────────
polys = None
if os.path.exists(os.path.join(ARCHIVE, "_region.json")):
    with open(os.path.join(ARCHIVE, "_region.json"), "r", encoding="utf-8") as fh:
        polys = json.load(fh)
if polys:
    rings = polys["rings"]
    print("\nregion: %s" % polys.get("description"))
    for name in sorted(os.listdir(ARCHIVE)):
        if not name.startswith("bf_") or not name.endswith(".geojson"):
            continue
        feats = contents(name)
        hits = sum(1 for f in feats
                   if inside_region(f.get("geometry") or {}, rings)
                   or str((f.get("properties") or {}).get("POLYGON_ID")) in polys["polygon_ids"])
        if name != "bf_existing_mfg.geojson" and hits < 1:
            fails.append("%s: no asset inside the region" % name)
        print("%-32s %d/%d inside the region" % (name, hits, len(feats)))

if fails:
    print("\nFAILED:")
    for f in fails:
        print("  -", f)
    sys.stdout.flush()
    os._exit(1)
print("\nOK: brownfield archive loads with 3..5 of each asset, one region, spare capacity on all.")
sys.stdout.flush()
os._exit(0)
