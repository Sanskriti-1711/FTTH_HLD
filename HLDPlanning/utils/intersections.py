# -*- coding: utf-8 -*-
from typing import List, Tuple, Optional
from qgis.PyQt.QtCore import QVariant
from qgis.core import (QgsVectorLayer, QgsGeometry, QgsFields, QgsField, QgsFeature,
                       QgsPointXY, QgsWkbTypes)
from qgis import processing

def build_intersection_buffers(veh_roads: QgsVectorLayer,
                               tol_cluster: float,
                               ibufr: float,
                               context,
                               feedback) -> Tuple[Optional[QgsVectorLayer], List[QgsPointXY], Optional[QgsVectorLayer]]:
    """
    Returns (buffer_polygons_mem, centers_points_list, exploded_roads_layer).
    buffer_polygons_mem is a memory layer with 'id' field, or None.
    """
    if not veh_roads or veh_roads.featureCount() == 0:
        return None, [], None

    roadsExp = processing.run(
        "native:explodelines",
        {"INPUT": veh_roads, "OUTPUT": "TEMPORARY_OUTPUT"},
        is_child_algorithm=True, context=context, feedback=feedback
    )["OUTPUT"]

    inter_raw = processing.run(
        "native:lineintersections",
        {"INPUT": roadsExp, "INTERSECT": roadsExp, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [],
         "OUTPUT": "TEMPORARY_OUTPUT"},
        is_child_algorithm=True, context=context, feedback=feedback
    )["OUTPUT"]

    # Child-algorithm TEMPORARY_OUTPUTs come back as context layer IDs (memory
    # layers), not file paths — resolve through the context first; the OGR
    # constructor only works for actual on-disk outputs.
    def _resolve(v, name):
        if isinstance(v, QgsVectorLayer):
            return v
        try:
            from qgis.core import QgsProcessingUtils
            lyr = QgsProcessingUtils.mapLayerFromString(str(v), context)
            if lyr is not None and lyr.isValid():
                return lyr
        except Exception:
            pass
        return QgsVectorLayer(str(v), name, "ogr")

    roadsExp = _resolve(roadsExp, "exploded")
    inter_raw = _resolve(inter_raw, "intersections")

    # cluster
    centers = []
    clusters = {}
    for f in inter_raw.getFeatures():
        g = f.geometry()
        if not g or g.isEmpty(): continue
        pt = g.asPoint() if not g.isMultipart() else (g.asMultiPoint()[0] if g.asMultiPoint() else None)
        if not pt: continue
        key = (round(pt.x()/tol_cluster)*tol_cluster, round(pt.y()/tol_cluster)*tol_cluster)
        clusters.setdefault(key, []).append(pt)
    for pts in clusters.values():
        if len(pts) >= 3:  # degmin left to caller? keep >2 as safe default
            x = sum(p.x() for p in pts)/len(pts)
            y = sum(p.y() for p in pts)/len(pts)
            centers.append(QgsPointXY(x, y))

    # memory buffer layer
    if not centers:
        return None, [], roadsExp
    crs = veh_roads.crs().authid()
    mem = QgsVectorLayer(f"Polygon?crs={crs}", "inter_buf", "memory")
    prov = mem.dataProvider()
    flds = QgsFields(); flds.append(QgsField("id", QVariant.Int))
    prov.addAttributes(flds); mem.updateFields()
    for i, c in enumerate(centers, 1):
        bf = QgsFeature(flds); bf["id"] = i
        bf.setGeometry(QgsGeometry.fromPointXY(c).buffer(max(0.1, ibufr), 16))
        prov.addFeatures([bf])
    mem.updateExtents()
    return mem, centers, roadsExp
