"""Does SERVES_TIER fix the tier mis-attribution the docs recorded as D11?

For every network layer, find the nearest Final_Trenches span and tally that
span's tier two ways: by `TRENCH_TIER` (what the label says) and by
`SERVES_TIER` (what the span actually serves, i.e. `PROMOTED_FROM or
TRENCH_TIER`).

The recorded D11 measurement was: "of the 154 distribution ducts, 126 are
nearest to a Feeder span" — a feature is on the network, but the tier it is
attributed to is the promoted label rather than the tier it serves.

Read-only. Usage: python tmp/verify_serves_tier.py <run_dir>
"""
import os
import sys
from collections import Counter

from osgeo import ogr

# layer file, label, the tier the layer belongs to
LAYERS = [
    ("Feeder_Ducts.gpkg", "feeder_ducts", "Feeder"),
    ("Distribution_Ducts.gpkg", "distribution_ducts", "Distribution"),
    ("Drop_Ducts.gpkg", "drop_ducts", "Garden"),
    ("Feeder_Cable.gpkg", "feeder_cable", "Feeder"),
    ("Distribution_Cable.gpkg", "distribution_cable", "Distribution"),
]


def load(path, with_props=False):
    if not os.path.exists(path):
        return []
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        try:
            gc = g.Clone()
        except Exception:
            gc = g
        if with_props:
            props = {}
            try:
                keys = [f.GetDefnRef().GetFieldDefn(i).GetName() for i in range(f.GetDefnRef().GetFieldCount())]
            except Exception:
                keys = []
            for k in keys:
                val = f.GetField(k)
                if k == 'ogr_geometry':
                    val = gc
                elif val is not None:
                    try:
                        val = float(val) if isinstance(val, (int, float)) else str(val)
                    except Exception:
                        val = str(val) if val is not None else ''
                else:
                    val = ''
                props[str(k)] = val
            out.append((gc, props))
        else:
            out.append((gc, None))
    ds = None
    return out


def _closest_trench_row(g, trench):
    best = float("inf")
    best_props = None
    for t_geom, _props in trench:
        try:
            d = float(g.Distance(t_geom))
        except Exception:
            d = float("inf")
        if d < best:
            best = d
            best_props = _props
    return best, best_props


def main():
    run = sys.argv[1].rstrip("/\\")
    trench = load(os.path.join(run, "Final_Trenches.gpkg"), with_props=True)
    print("trenches: %d" % len(trench))
    print("  TRENCH_TIER : %s" % dict(Counter(str(t.get('TRENCH_TIER') or '') for _g, t in trench)))
    print("  SERVES_TIER : %s" % dict(Counter(str(t.get('SERVES_TIER') or '') for _g, t in trench)))
    print()
    print("nearest trench's tier, counted two ways "
          "(a feature should be attributed to the tier it belongs to)")
    print()
    print("%-20s %5s | %-34s | %-34s" % ("layer", "n", "by TRENCH_TIER (label)", "by SERVES_TIER (real)"))
    print("-" * 100)

    for fname, label, only in LAYERS:
        feats = load(os.path.join(run, fname), with_props=False)
        if not feats:
            print("%-20s   -   (missing)" % label)
            continue
        by_label, by_serves = Counter(), Counter()
        by_label, by_serves = Counter(), Counter()
        for g in feats:
            _best, props = _closest_trench_row(g, trench)
            if props is not None:
                by_label[str(props.get('TRENCH_TIER') or '')] += 1
                by_serves[str(props.get('SERVES_TIER') or '')] += 1
            else:
                by_label['_no_trench'] += 1
                by_serves['_no_trench'] += 1
        n = len(feats)
        ok_l = by_label.get(only, 0)
        ok_s = by_serves.get(only, 0)
        print("%-20s %5d | %-34s | %-34s" % (
            label, n,
            "%d/%d %s" % (ok_l, n, dict(by_label)),
            "%d/%d %s" % (ok_s, n, dict(by_serves))))
        print("%-20s %5s | %-34s | %-34s" % ("", "", "  correct tier: %.0f%%" % (100.0 * ok_l / n),
            "  correct tier: %.0f%%" % (100.0 * ok_s / n)))
        print()
        print("%-20s %5s | %-34s | %-34s" % ("", "", "  label[sum]: %s" % dict(by_label), "  serves[sum]: %s" % dict(by_serves)))
        n = len(feats)
        ok_l = by_label.get(only, 0)
        ok_s = by_serves.get(only, 0)
        print("%-20s %5d | %-34s | %-34s" % (
            label, n,
            "%d/%d %s" % (ok_l, n, dict(by_label)),
            "%d/%d %s" % (ok_s, n, dict(by_serves))))
        print("%-20s %5s | %-34s | %-34s" % ("", "", "  correct tier: %.0f%%" % (100.0 * ok_l / n),
            "  correct tier: %.0f%%" % (100.0 * ok_s / n)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
