# -*- coding: utf-8 -*-
from typing import Optional, Tuple
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes

def nearest_point_and_distance(target_geom: QgsGeometry, ref_geom: QgsGeometry) -> Tuple[Optional[QgsPointXY], Optional[float]]:
    """
    Returns (nearest_point_as_XY, distance) from target_geom to ref_geom.
    Returns (None, None) on any error or if nearest point is not a point geometry.
    """
    try:
        if not target_geom or target_geom.isEmpty() or not ref_geom or ref_geom.isEmpty():
            return None, None
        np = target_geom.nearestPoint(ref_geom)
        if not np or np.isEmpty() or QgsWkbTypes.geometryType(np.wkbType()) != QgsWkbTypes.PointGeometry:
            return None, None
        pt = np.asMultiPoint()[0] if QgsWkbTypes.isMultiType(np.wkbType()) else np.asPoint()
        return QgsPointXY(pt), float(np.distance(ref_geom))
    except Exception:
        return None, None
