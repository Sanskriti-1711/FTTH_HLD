"""C3 audit: do the ducts and cables lie ON the designed trench network?

Answers TRENCH_DESIGN.md §6.1 C3's question — "count ducts with ON_TRENCH = 0
and cables not in a duct; both must be 0" — against a finished run's output
directory, with no pipeline re-run.

Rule (per feature): every vertex of the duct/cable must be within TOLERANCE
metres of some Final_Trenches feature. Ducts ride their own side of the
corridor (sidewalk offsets), so the default tolerance is 6 m; pass a tighter
one with --tolerance to see the strict picture. A feature whose ANY vertex is
further away than the tolerance is ON_TRENCH = 0.

Runs under the QGIS Python (qgis.core for the spatial index):

    HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/audit_on_trench.py <output_dir> [--tolerance 6]

Exit code 0 when every audited feature is on a trench, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys

from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex, QgsVectorLayer

DUCT_LAYERS = ("Feeder_Ducts", "Distribution_Ducts", "Drop_Ducts")
CABLE_LAYERS = ("Feeder_Cable", "Distribution_Cable")
AUDIT_LAYERS = DUCT_LAYERS + CABLE_LAYERS


def _layer(path: str, name: str):
    lyr = QgsVectorLayer(path, name, "ogr")
    return lyr if lyr.isValid() else None


def _vertices(geom: QgsGeometry):
    """Every vertex of a (multi)line geometry."""
    if geom is None or geom.isEmpty():
        return []
    parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
    for part in parts:
        for p in part:
            yield p


def audit(output_dir: str, tolerance: float = 6.0) -> dict:
    trenches_path = os.path.join(output_dir, "Final_Trenches.gpkg")
    trenches = _layer(trenches_path, "trenches")
    if trenches is None or trenches.featureCount() == 0:
        raise SystemExit(f"no readable Final_Trenches in {output_dir}")

    index = QgsSpatialIndex()
    tgeoms: dict = {}
    for f in trenches.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        tgeoms[f.id()] = g
        index.addFeature(f)

    report = {"tolerance_m": tolerance, "layers": {}, "on_trench_total": 0,
              "checked_total": 0}
    for name in AUDIT_LAYERS:
        path = os.path.join(output_dir, f"{name}.gpkg")
        lyr = _layer(path, name)
        if lyr is None:
            report["layers"][name] = {"present": False}
            continue
        checked = off = 0
        worst = []
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            checked += 1
            max_d = 0.0
            for v in _vertices(g):
                d_best = float("inf")
                for tid in index.nearestNeighbor(v, int(2 * tolerance + 10)):
                    tg = tgeoms.get(tid)
                    if tg is None:
                        continue
                    d = QgsGeometry.fromPointXY(v).distance(tg)
                    if d < d_best:
                        d_best = d
                max_d = max(max_d, d_best)
            if max_d > tolerance:
                off += 1
                if len(worst) < 5:
                    worst.append({"fid": f.id(),
                                  "max_vertex_offset_m": round(max_d, 1)})
        report["layers"][name] = {
            "present": True, "checked": checked, "off_trench": off,
            "on_trench": checked - off, "worst": worst,
        }
        report["on_trench_total"] += checked - off
        report["checked_total"] += checked
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output_dir", help="a finished run's output directory")
    ap.add_argument("--tolerance", type=float, default=6.0,
                    help="max allowed vertex-to-trench distance (metres)")
    args = ap.parse_args()

    rep = audit(args.output_dir, args.tolerance)
    ok = True
    print(f"ON_TRENCH audit — {args.output_dir} (tolerance {rep['tolerance_m']} m)")
    for name, row in rep["layers"].items():
        if not row.get("present"):
            print(f"  {name:24s} — not produced")
            continue
        status = "OK " if row["off_trench"] == 0 else "OFF"
        ok = ok and row["off_trench"] == 0
        print(f"  {name:24s} — {row['on_trench']}/{row['checked']} on trench"
              f" [{status}]")
        for w in row["worst"]:
            print(f"      worst fid {w['fid']}: {w['max_vertex_offset_m']} m "
                  "from nearest trench")
    print(f"  TOTAL: {rep['on_trench_total']}/{rep['checked_total']} on trench"
          f" — {'ON_TRENCH = 1 everywhere' if ok else 'ON_TRENCH = 0 EXISTS'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
