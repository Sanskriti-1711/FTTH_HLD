# -*- coding: utf-8 -*-
"""Which identity attributes actually reach each layer of a run?

For the region-confinement work we need, on every layer that a duct, cable,
chamber or joint is filtered or grouped by: POLYGON_ID, a PDP link, the duct and
drop ids, the chamber anchors and the run id. This prints, per layer, whether the
field exists and what fraction of rows carry a value.

Usage:
    unset PYTHONPATH && python tmp/attr_coverage.py <run_dir>
"""
import os
import sys
from collections import Counter

from osgeo import ogr

WANT = [
    ("POLYGON_ID", ("POLYGON_ID", "polygon_id")),
    ("PDP", ("PDP_ID", "pdp_id", "PDP_IDS", "pdp_ids", "PDP")),
    ("MFG", ("MFG_ID", "MFG")),
    ("CHAMBERS", ("START_CHAMBER", "END_CHAMBER")),
    ("RUN_ID", ("RUN_ID", "RUN")),
    ("SPAN_KIND", ("SPAN_KIND",)),
    ("DIST_DUCT", ("DIST_DUCT_ID",)),
    ("DROP_DUCT", ("DROP_DUCT", "DROP_DUCT_UID")),
    ("PREMISE", ("PREMISE_ID", "premise_id", "ADDR_ID", "addr_id")),
    ("DUCT_UID", ("DUCT_UID", "DUCT_ID")),
    ("TIER", ("TRENCH_TIER", "SERVES_TIER", "trench_tier")),
    ("PARENT_TRENCH", ("PARENT_TRENCH",)),
]


def scan(path):
    ds = ogr.Open(path)
    if ds is None:
        return None
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    rows = 0
    filled = Counter()
    for ft in lyr:
        rows += 1
        for _label, aliases in WANT:
            for a in aliases:
                if a in names:
                    v = ft.GetField(a)
                    if v not in (None, "", 0):
                        filled[a] += 1
                        break
    return names, rows, filled


def main():
    run_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = sorted(f for f in os.listdir(run_dir) if f.endswith(".gpkg"))
    print("field presence / fill on each layer  (run: %s)" % os.path.basename(run_dir))
    print()
    header = "%-28s %6s %s" % ("layer", "rows", "  ".join("%-13s" % w[0][:13] for w in WANT))
    print(header)
    print("-" * len(header))
    notes = []
    for fn in files:
        got = scan(os.path.join(run_dir, fn))
        if got is None:
            continue
        names, rows, filled = got
        cells = []
        for label, aliases in WANT:
            present = [a for a in aliases if a in names]
            if not present:
                cells.append("%-13s" % "-")
                continue
            got_n = max(filled.get(a, 0) for a in present)
            cells.append("%-13s" % ("%d/%d" % (got_n, rows)))
        print("%-28s %6d %s" % (os.path.splitext(fn)[0][:28], rows, "  ".join(cells)))
        for label, aliases in WANT:
            present = [a for a in aliases if a in names]
            if not present:
                notes.append((os.path.splitext(fn)[0], label, "ABSENT"))
            else:
                got_n = max(filled.get(a, 0) for a in present)
                if rows and got_n < rows:
                    notes.append((os.path.splitext(fn)[0], label,
                                  "partial %d/%d" % (got_n, rows)))
    print()
    print("gaps (absent or partial):")
    for layer, label, what in notes:
        print("   %-28s %-13s %s" % (layer, label, what))


if __name__ == "__main__":
    main()
