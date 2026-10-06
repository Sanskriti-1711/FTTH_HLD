"""Rebuild the pre-segmentation distribution ducts from the published spans.

The published layer is one feature per chamber-bounded span; `RUN_ID` groups the
spans that were cut from one duct-stage feature. Unioning a RUN_ID's spans gives
back (up to the extra vertices at the cut points) the geometry the duct stage
published, which is what tells us whether an off-trench chord was already there
or was introduced by the chamber segmentation.

Usage (QGIS python):
    qgis_python.cmd HLD_Planning_01/tmp/reconstruct_runs.py <run_dir> <out_dir> [layer]
"""
import os
import shutil
import sys

from qgis.core import (QgsFeature, QgsFields, QgsGeometry, QgsVectorLayer,
                       QgsVectorFileWriter, QgsWkbTypes)


def main():
    run_dir = sys.argv[1]
    out_dir = sys.argv[2]
    layer = sys.argv[3] if len(sys.argv) > 3 else "distribution_ducts"
    os.makedirs(out_dir, exist_ok=True)
    for nm in ("Final_Trenches.gpkg", "Chambers.gpkg", "Pseudo_HH.gpkg"):
        src = os.path.join(run_dir, nm)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out_dir, nm))

    lyr = QgsVectorLayer(os.path.join(run_dir, "%s.gpkg" % layer), layer, "ogr")
    if not lyr.isValid():
        raise SystemExit("cannot open the layer")
    names = lyr.fields().names()
    i_run = names.index("RUN_ID") if "RUN_ID" in names else -1
    if i_run < 0:
        raise SystemExit("layer carries no RUN_ID — nothing to rebuild")

    groups = {}
    for f in lyr.getFeatures():
        key = str(f[i_run] or f.id())
        groups.setdefault(key, []).append(f.geometry())

    fields = QgsFields()
    out_path = os.path.join(out_dir, "distribution_ducts.gpkg")
    if os.path.isfile(out_path):
        os.remove(out_path)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "distribution_ducts"
    writer = QgsVectorFileWriter.create(out_path, fields, QgsWkbTypes.MultiLineString,
                                        lyr.crs(), lyr.transformContext(), opts)
    if writer.hasError() != QgsVectorFileWriter.NoError:
        raise SystemExit("writer error: %s" % writer.errorMessage())

    n = 0
    for key, geoms in groups.items():
        merged = QgsGeometry.unaryUnion([g for g in geoms if g and not g.isEmpty()])
        if merged is None or merged.isEmpty():
            continue
        feat = QgsFeature()
        feat.setGeometry(merged)
        writer.addFeature(feat)
        n += 1
    del writer
    print("rebuilt %d run(s) from %d span(s) -> %s" % (n, sum(len(v) for v in groups.values()),
                                                      out_path))


if __name__ == "__main__":
    main()
