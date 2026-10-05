"""What the HLD page is actually served for `ducts`, split by tier.

The map reads `/api/ftth/hld/results/<id>/layers/<name>/`, which serves the
`FtthLayer` row verbatim — and `sync_project_layers()` skips layers that already
exist, so a re-run leaves the platform on the PREVIOUS run's geometry until
`tmp/republish_layers.py --apply` overwrites the rows. This is the check that
the row the page reads is the new one.

    cd fiber-backend && set -a && . ../.env && set +a && \\
        PYTHONPATH=. python ../tmp/served_dist_check.py <project_id>
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from ftth_hld.models import FtthLayer  # noqa: E402

project_id = sys.argv[1]

row = FtthLayer.objects.filter(
    ftth_project__project_id=project_id, name="ducts"
).first()
if row is None:
    print("no 'ducts' row for", project_id)
    raise SystemExit(1)

feats = (row.geojson or {}).get("features") or []
print("served 'ducts' row: %d feature(s), feature_count=%s" % (len(feats), row.feature_count))

tier_keys = ("DUCT_TIER", "TIER", "PARENT_TRENCH", "CABLE_TYPE", "DUCT_TYPE")
sample = (feats[0].get("properties") or {}) if feats else {}
print("first feature keys:", sorted(sample)[:22])

for key in tier_keys:
    if key in sample:
        values = Counter(str((f.get("properties") or {}).get(key) or "(blank)")
                         for f in feats)
        print("  %-14s %s" % (key, dict(values.most_common(6))))

# The merged layer carries no tier column: the distribution ducts are the
# 2-Way profile, the feeder is 4-Way and the per-premise drop legs 1-Way.
tier_key = "DUCT_TYPE"
if tier_key:
    dist = [f for f in feats
            if str((f.get("properties") or {}).get(tier_key) or "").startswith("2-Way")]
    print("\ndistribution subset: %d feature(s)" % len(dist))
    total = 0.0
    multi = 0
    blanks = 0
    for f in dist:
        props = f.get("properties") or {}
        try:
            total += float(props.get("length_m") or 0)
        except Exception:
            pass
        pid = str(props.get("POLYGON_ID") or "").strip()
        if not pid:
            blanks += 1
        elif "," in pid or ";" in pid:
            multi += 1
    print("  length_m total : %.1f m" % total)
    print("  blank POLYGON_ID: %d" % blanks)
    print("  >1 POLYGON_ID   : %d" % multi)
