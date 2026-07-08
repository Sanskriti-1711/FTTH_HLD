# -*- coding: utf-8 -*-
import math
from typing import Tuple, Optional
from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayer, QgsSpatialIndex, QgsFeatureRequest

def nearest_point_on_lines(lyr: QgsVectorLayer, sidx: QgsSpatialIndex, ref_pt: QgsPointXY, search_m: float) -> Tuple[Optional[QgsPointXY], float, Optional[int]]:
    """
    Return (closest_point, distance, feature_id) on 'lyr' to 'ref_pt', using 'sidx'.
    Distance is in layer CRS units. If none found, returns (None, inf, None).
    """
    ref_geom = QgsGeometry.fromPointXY(ref_pt)
    bb = ref_geom.buffer(max(1.0, search_m), 8).boundingBox()
    best_pt, best_d, best_fid = None, float("inf"), None
    for fid in sidx.intersects(bb):
        f = next(lyr.getFeatures(QgsFeatureRequest(fid)), None)
        if not f:
            continue
        sqr_dist, q, *_ = f.geometry().closestSegmentWithContext(ref_pt)
        if q is None:
            continue
        dist = math.sqrt(sqr_dist) if sqr_dist >= 0 else float("inf")
        if dist < best_d:
            best_pt, best_d, best_fid = QgsPointXY(q), float(dist), int(fid)
    return best_pt, best_d, best_fid
