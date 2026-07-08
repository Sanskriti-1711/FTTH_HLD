# -*- coding: utf-8 -*-
import math
from typing import Optional
from qgis.core import QgsVectorLayer, QgsGeometry, QgsPointXY
from .geom_basic import geom_ok as _geom_ok

def add_lines_to_graph(G, layer: Optional[QgsVectorLayer], step_m: float, eps_val: float, qkey_func):
    """
    Densifies each line in 'layer' by 'step_m' and adds segments to graph 'G'.
    'qkey_func' must return a quantized key tuple for a QgsPointXY (x, y).
    """
    if not layer or layer.featureCount() == 0:
        return
    for feat in layer.getFeatures():
        g = feat.geometry()
        if not _geom_ok(g):
            continue
        g = g.densifyByDistance(step_m)
        lines = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for ln in lines:
            for i in range(len(ln) - 1):
                p1, p2 = QgsPointXY(ln[i]), QgsPointXY(ln[i + 1])
                a, b = qkey_func(p1, eps_val), qkey_func(p2, eps_val)
                if a == b:
                    continue
                dist = math.hypot(p1.x() - p2.x(), p1.y() - p2.y())
                if dist > 0:
                    G.add_edge(a, b, weight=dist)

def snap_to_nodes(pt: QgsPointXY, nodes, max_dist: float):
    """Return (nearest_node, distance) within 'max_dist', or (None, inf) if none."""
    nearest, best = None, float("inf")
    for n in nodes:
        d = math.hypot(pt.x() - n[0], pt.y() - n[1])
        if d < best and d <= max_dist:
            best, nearest = d, n
    return nearest, best
