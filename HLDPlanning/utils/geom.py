# -*- coding: utf-8 -*-
"""
utils.geom
Common geometry helpers for FTTH layers (Trench, Duct, Cable, etc.)
"""

import math
from qgis.core import QgsGeometry, QgsPointXY

def round_key_xy(x: float, y: float, tol: float = 0.5):
    """Rounds coordinates to a tolerance grid (used for node keys)."""
    rx = round(x / tol) * tol
    ry = round(y / tol) * tol
    return (round(rx, 6), round(ry, 6))


def geom_substring(geom: QgsGeometry, start: float, end: float) -> QgsGeometry:
    """Returns a portion of a line geometry between distances start and end (meters)."""
    if not geom or geom.isEmpty():
        return QgsGeometry()

    if hasattr(geom, "lineSubstring"):
        try:
            return geom.lineSubstring(start, end)
        except Exception:
            pass
    if hasattr(geom, "curveSubstring"):
        try:
            return geom.curveSubstring(start, end)
        except Exception:
            pass

    pl = geom.asPolyline()
    if not pl:
        m = geom.asMultiPolyline()
        if m and m[0]:
            pl = m[0]
        else:
            return QgsGeometry()

    cum = [0.0]
    for i in range(1, len(pl)):
        dx = pl[i].x() - pl[i - 1].x()
        dy = pl[i].y() - pl[i - 1].y()
        cum.append(cum[-1] + math.hypot(dx, dy))

    total = cum[-1]
    if end <= 0 or start >= total or start >= end:
        return QgsGeometry()

    def point_at(dist):
        if dist <= 0:
            return QgsPointXY(pl[0].x(), pl[0].y())
        if dist >= total:
            return QgsPointXY(pl[-1].x(), pl[-1].y())
        for i in range(1, len(pl)):
            if dist <= cum[i]:
                seg_len = (cum[i] - cum[i - 1]) or 1.0
                t = (dist - cum[i - 1]) / seg_len
                x = pl[i - 1].x() + (pl[i].x() - pl[i - 1].x()) * t
                y = pl[i - 1].y() + (pl[i].y() - pl[i - 1].y()) * t
                return QgsPointXY(x, y)
        return QgsPointXY(pl[-1].x(), pl[-1].y())

    start_pt = point_at(max(0.0, start))
    end_pt = point_at(min(total, end))
    pts = [start_pt]
    for i in range(1, len(pl)):
        if cum[i] > start and cum[i] < end:
            pts.append(QgsPointXY(pl[i].x(), pl[i].y()))
    pts.append(end_pt)
    return QgsGeometry.fromPolylineXY(pts)


def edges_to_geom(edge_geom: dict, seg_list: list):
    """Merge segment geometries to a MultiLineString."""
    parts = []
    for sid in seg_list:
        g = edge_geom.get(sid)
        if g:
            parts.append(g.asPolyline())
    return QgsGeometry.fromMultiPolylineXY(parts) if parts else QgsGeometry()


def path_len(edge_len: dict, seg_list: list) -> float:
    """Sum length of all segments in a path."""
    return sum(edge_len.get(s, 0.0) for s in seg_list)


def is_prefix(a: list, b: list) -> bool:
    """Return True if path a is prefix of path b."""
    return len(a) <= len(b) and a == b[: len(a)]


def lcp_len(seq_list: list) -> int:
    """Find longest common prefix length among list of paths."""
    if not seq_list:
        return 0
    m = min(len(s) for s in seq_list)
    k = 0
    for i in range(m):
        seg0 = seq_list[0][i]
        if any(s[i] != seg0 for s in seq_list[1:]):
            break
        k += 1
    return k
