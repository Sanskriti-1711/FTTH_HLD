"""Layer inventory for a finished pipeline run — and a diff of two runs.

Answers "what did the run produce?" per layer (feature count, geometry type,
total line length / polygon area in the layer's projected metres) and, with
two directories, "what changed?" — the comparison used to check a clean run
against a reference run after an engine change.

    python tools/layer_inventory.py <run_dir>            # inventory
    python tools/layer_inventory.py <reference_dir> <new_run_dir>   # diff

GPKGs are the source of truth (the .geojson files are derived exports); the
run's ``brownfield/`` input folder is not part of the produced inventory.
Runs under the Anaconda interpreter (osgeo.ogr only — no QGIS needed).
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

from osgeo import ogr


def _inventory(run_dir: str) -> Dict[str, Tuple[str, int, float]]:
    """{layer name: (geom type, feature count, length-or-area)} for one run."""
    out: Dict[str, Tuple[str, int, float]] = {}
    for fname in sorted(os.listdir(run_dir)):
        if not fname.lower().endswith(".gpkg"):
            continue
        path = os.path.join(run_dir, fname)
        ds = ogr.Open(path)
        if ds is None:
            print(f"  (cannot open {fname})", file=sys.stderr)
            continue
        for li in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(li)
            name = lyr.GetName()
            key = f"{fname[:-5]}/{name}"
            gtype = ogr.GeometryTypeToName(lyr.GetGeomType())
            count = 0
            measure = 0.0
            for feat in lyr:
                count += 1
                g = feat.GetGeometryRef()
                if g is None:
                    continue
                flat = ogr.GT_Flatten(g.GetGeometryType())
                if flat in (ogr.wkbLineString, ogr.wkbMultiLineString):
                    measure += g.Length()
                elif flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
                    measure += g.GetArea()
            out[key] = (gtype, count, round(measure, 1))
        ds = None
    return out


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    a = _inventory(argv[1])
    if len(argv) == 2:
        for key, (gtype, count, measure) in sorted(a.items()):
            print(f"{count:6d}  {measure:12.1f}  {gtype:28s}  {key}")
        return 0

    b = _inventory(argv[2])
    unit = "len/area"
    print(f"{'layer':52s} {'reference':>14s} {'new':>14s} {'delta':>12s}")
    for key in sorted(set(a) | set(b)):
        ga, ca, ma = a.get(key, ("-", 0, 0.0))
        gb, cb, mb = b.get(key, ("-", 0, 0.0))
        flag = "" if (ca == cb and abs(ma - mb) < 0.1) else "   <-- changed"
        if key not in b:
            flag = "   <-- only in reference"
        elif key not in a:
            flag = "   <-- only in new"
        print(f"{key:52s} {ca:6d}/{ma:7.1f} {cb:6d}/{mb:7.1f} "
              f"{cb - ca:+6d}/{mb - ma:+7.1f}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
