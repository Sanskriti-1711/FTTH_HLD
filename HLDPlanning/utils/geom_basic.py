# -*- coding: utf-8 -*-
from typing import Iterable, List, Optional
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
import math

def geom_ok(g: Optional[QgsGeometry]) -> bool:
    try:
        return bool(g and not g.isEmpty() and (g.isGeosValid() if hasattr(g, "isGeosValid") else True))
    except Exception:
        return False

def line_parts(g: QgsGeometry) -> Iterable[List[QgsPointXY]]:
    try:
        return g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
    except Exception:
        return []

def point_of(g: QgsGeometry) -> Optional[QgsPointXY]:
    if not g:
        return None
    try:
        if QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.PointGeometry:
            return None
        if QgsWkbTypes.isMultiType(g.wkbType()):
            pts = g.asMultiPoint()
            return QgsPointXY(pts[0]) if pts else None
        pt = g.asPoint()
        return QgsPointXY(pt)
    except Exception:
        return None


def to_multiline(geom: QgsGeometry) -> QgsGeometry:
    """
    Coerce LineString → MultiLineString for sinks that expect multi.
    Returns the geometry unchanged if it's already multi or not a line.
    """
    if not geom or geom.isEmpty():
        return geom
    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
        return geom
    if QgsWkbTypes.isMultiType(geom.wkbType()):
        return geom
    pts = geom.asPolyline()
    return QgsGeometry.fromMultiPolylineXY([pts]) if pts else geom

def safe_collect(g1: QgsGeometry, g2: Optional[QgsGeometry]) -> QgsGeometry:
    """
    Collect g1 and g2 robustly; falls back to combine if collect fails.
    Returns g1 if g2 is None/invalid.
    """
    if not g2 or g2.isEmpty():
        return g1
    try:
        mg = QgsGeometry.collectGeometry([g1, g2])
        return mg if mg and not mg.isEmpty() else g1.combine(g2)
    except Exception:
        return g1.combine(g2)

