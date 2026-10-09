"""Unit checks for the shared brownfield reuse rule (utils/reuse.py).

Run with QGIS's Python launcher so qgis.core is importable. The bare
apps/Python312/python.exe cannot import qgis (its site-packages are not on
sys.path); bin/python-qgis.bat sets QGIS_PREFIX_PATH and PYTHONPATH for you:

    "C:/Program Files/QGIS 3.44.6/bin/python-qgis.bat" \
        HLD_Planning_01/tests/test_reuse_rule.py

Checks the three things the rule must get right:
  1. a run that FOLLOWS an existing asset for >= 50 % of its length is reuse
  2. a run that merely brushes past one is NOT reuse (the bug that once
     classified ~96 % of a Berlin run as reuse)
  3. an asset without spare capacity cannot be reused
  4. capacity is committed at most once per asset, however many runs ride it
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_PARENT = os.path.dirname(HERE)  # HLD_Planning_01 (holds the HLDPlanning package)
if PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, PLUGIN_PARENT)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY,
)

QgsApplication.setPrefixPath(r"C:\Program Files\QGIS 3.44.6\apps\qgis", True)
_app = QgsApplication([], False)
_app.initQgis()

from HLDPlanning.utils.brownfield import (  # noqa: E402
    BrownfieldRegistry, AssetType,
)
from HLDPlanning.utils import reuse as R  # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-58s %-18s %s" % (name, repr(got), "OK" if ok else "FAIL want %r" % (want,)))
    if not ok:
        FAILURES.append(name)


def line(pts, crs):
    return QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts])


def make_registry(assets):
    """assets: list of (asset_type, points, capacity_total, capacity_used, id)"""
    crs = QgsCoordinateReferenceSystem("EPSG:25833")
    reg = BrownfieldRegistry(crs)
    for asset_type, pts, cap, used, aid in assets:
        reg._assets[aid] = {
            "asset_type": asset_type,
            "geom": line(pts, crs),
            "capacity_total": cap,
            "capacity_used": used,
            "verify_status": "Verified",
            "use_mode": "",
            "reused": False,
        }
        reg._add_to_index(aid, line(pts, crs))
    return reg


# ── 1. following a 4-way duct (1 used, 3 spare) for 80 % of a run ────────
reg = make_registry([(AssetType.DUCT, [(0, 0), (0, 100)], 4, 1, "BF_DUCT_00001")])
reg.store_registry(reg, True)
idx = R.BrownfieldLineIndex.build()
res = idx.classify_run([line([(0, 0), (0, 85)], None)])
check("80 % along a duct -> Reused", res.status, R.STATUS_REUSED)
check("  reuse length counted", round(res.reuse_len), 85)
check("  asset named", res.sources, ["BF_DUCT_00001"])
check("  capacity consumed once", reg.get_asset("BF_DUCT_00001")["capacity_used"], 2)

# ── 2. brushing past it (20 % of the run) is NOT reuse ───────────────────
reg2 = make_registry([(AssetType.DUCT, [(0, 0), (0, 100)], 4, 1, "BF_DUCT_00002")])
reg2.store_registry(reg2, True)
res2 = R.BrownfieldLineIndex.build().classify_run([line([(0, 95), (0, 120)], None)])
check("20 % overlap -> not reuse", res2.matched, False)
check("  capacity untouched", reg2.get_asset("BF_DUCT_00002")["capacity_used"], 1)

# ── 3. a full duct cannot be reused ──────────────────────────────────────
reg3 = make_registry([(AssetType.DUCT, [(0, 0), (0, 100)], 4, 4, "BF_DUCT_00003")])
reg3.store_registry(reg3, True)
res3 = R.BrownfieldLineIndex.build().classify_run([line([(0, 0), (0, 100)], None)])
check("full duct -> not reuse", res3.matched, False)

# ── 4. many runs riding one asset commit its capacity once ───────────────
reg4 = make_registry([(AssetType.TRENCH, [(0, 0), (0, 100)], 4, 0, "BF_TRENCH_00001")])
reg4.store_registry(reg4, True)
idx4 = R.BrownfieldLineIndex.build()
statuses = []
for k in range(6):
    r = idx4.classify_run([line([(0, 10 * k), (0, 10 * k + 9)], None)])
    statuses.append(r.status)
check("6 runs along one trench all reuse", set(statuses), {R.STATUS_REUSED})
check("  capacity committed once", reg4.get_asset("BF_TRENCH_00001")["capacity_used"], 1)

# ── 5. no registry (brownfield off) -> no reuse, no crash ────────────────
reg5 = make_registry([(AssetType.DUCT, [(0, 0), (0, 100)], 4, 0, "BF_DUCT_00005")])
reg5.set_reuse_enabled(False)
check("reuse disabled -> no index", R.BrownfieldLineIndex.build(), None)

print()
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("all reuse-rule checks passed")
