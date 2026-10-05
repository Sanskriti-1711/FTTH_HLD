"""Read-only real-data check of the trench tier provenance fix.

Runs the exact code path ``processAlgorithm`` uses — ``_final_rows`` →
``_ensure_backbone_reach`` → ``_stamp_tier_provenance`` — against a real run's
*designer* output (``design/Final_Trenches.gpkg``, written before the promotion)
and reports what the promotion moves and what provenance now records.

Nothing is written: the layers are read-only and the run dir is untouched.
"""

from __future__ import annotations

import collections
import json
import os
import sys

from qgis.core import QgsApplication, QgsVectorLayer

sys.path.insert(0, r"D:\Downloads_D\Q-GIS\Fibre-FTTH\HLD_Planning_01")
sys.path.insert(0, r"D:\Downloads_D\Q-GIS\Fibre-FTTH\HLD_Planning_01\web\backend")

QGIS_PREFIX = r"C:\Program Files\QGIS 3.44.6\apps\qgis"
QgsApplication.setPrefixPath(QGIS_PREFIX, True)
app = QgsApplication([], False)
app.initQgis()

from HLDPlanning.algorithms.trench_design_layer import (  # noqa: E402
    TrenchDesignLayerAlgorithm,
)

RUN = r"D:\Downloads_D\Q-GIS\Fibre-FTTH\HLD_Planning_01\web\backend\outputs\39ec0d866dba4e5e80a4b9e7f4101953"
ALGO = TrenchDesignLayerAlgorithm()


def layer(path, name):
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        raise SystemExit("cannot open " + path)
    return lyr


def hist(rows, key):
    return collections.Counter(str(r.get(key) or "") for r in rows)


def show(title, counter):
    total = sum(counter.values())
    parts = ", ".join("%s %d" % (k or "(empty)", v)
                      for k, v in sorted(counter.items()))
    print("  %-26s %4d spans: %s" % (title, total, parts))


design = layer(os.path.join(RUN, "design", "Final_Trenches.gpkg"), "Final_Trenches")
published = layer(os.path.join(RUN, "Final_Trenches.gpkg"), "Final_Trenches")
mfgs = layer(os.path.join(RUN, "MFG.gpkg"), "MFG")
pdps = layer(os.path.join(RUN, "PDPs.gpkg"), "PDPs")

print("CRS design=%s mfg=%s pdp=%s"
      % (design.crs().authid(), mfgs.crs().authid(), pdps.crs().authid()))
print()

# ── What the deployed run actually published (post-promotion) ──────────────
pub_rows = ALGO._final_rows(published)
show("PUBLISHED (deployed)", hist(pub_rows, "TRENCH_TIER"))

# ── Replay the same promotion on the pre-promotion designer output ─────────
rows = ALGO._final_rows(design)
show("designer output (pre)", hist(rows, "TRENCH_TIER"))

anchors = []


def collect(lyr, label, idfield):
    names = lyr.fields().names()
    for f in lyr.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        aid = str(f[idfield]) if idfield in names and f[idfield] else "?"
        anchors.append((label, aid, p.x(), p.y()))


collect(pdps, "PDP", "PDP_ID")
collect(mfgs, "MFG", "MFG_ID")
print("  anchors: %d" % len(anchors))
print()

changed = ALGO._ensure_backbone_reach(rows, anchors, None)
promoted = ALGO._stamp_tier_provenance(rows)

print("  _ensure_backbone_reach changed %d span(s)" % changed)
print("  _stamp_tier_provenance marked  %d span(s) promoted" % promoted)
print()
show("TRENCH_TIER (after)", hist(rows, "TRENCH_TIER"))
show("SERVES_TIER (after)", hist(rows, "SERVES_TIER"))
show("PROMOTED_FROM (after)", hist(rows, "PROMOTED_FROM"))
print()

moved = [r for r in rows if r.get("PROMOTED_FROM")]
if moved:
    lens = sorted(float(r.get("length_m") or 0.0) for r in moved)
    print("  promoted spine length: %.1f m (median span %.1f m, max %.1f m)"
          % (sum(lens), lens[len(lens) // 2], lens[-1]))

algo = ALGO
_ = algo
