# -*- coding: utf-8 -*-
"""
utils.snap
Snapping and virtual node creation for FTTH layers
"""

from qgis.core import QgsGeometry, QgsPointXY

def nearest_segment_info_pt(pt_xy, seg_index, fid_to_geom, fid_to_len, snap_tol=1.5):
    """Find nearest segment to a point and return (fid, dist_along, sqr_d, proj_pt)."""
    rect = QgsGeometry.fromPointXY(QgsPointXY(pt_xy)).buffer(snap_tol * 6.0, 8).boundingBox()
    best = (None, None, 1e18, None)
    for fid in seg_index.intersects(rect):
        g = fid_to_geom.get(fid)
        if not g:
            continue
        dist2, proj_pt, *_ = g.closestSegmentWithContext(QgsPointXY(pt_xy))
        if dist2 < best[2]:
            da = g.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(proj_pt)))
            best = (fid, da, dist2, proj_pt)
    return best


def snap_point_create_virtual(
    pt_geom,
    seg_index,
    fid_to_geom,
    fid_to_len,
    fid_breaks=None,
    fid_break_xy=None,
    snap_tol=1.5,
    node_tol=0.5,
    end_eps=0.25,
):
    """
    Snap point to nearest line and create virtual node key.
    If snapped mid-segment, also registers a break so the graph will split at that location.
    Returns (node_key, fid) or (None, None) if no snap target.
    """
    pt = pt_geom.asPoint()
    info = nearest_segment_info_pt(pt, seg_index, fid_to_geom, fid_to_len, snap_tol)
    fid, dist_along, sqr_d, proj_pt = info
    if fid is None:
        return None, None

    g = fid_to_geom[fid]
    L = fid_to_len[fid]

    def _rk(x, y):
        return (round(x / node_tol) * node_tol, round(y / node_tol) * node_tol)

    if dist_along <= end_eps:
        p0 = g.interpolate(0.0).asPoint()
        return _rk(p0.x(), p0.y()), fid

    if (L - dist_along) <= end_eps:
        pL = g.interpolate(L).asPoint()
        return _rk(pL.x(), pL.y()), fid

    # Mid-segment: register a break so edge is split at this location
    x, y = proj_pt.x(), proj_pt.y()
    if fid_breaks is not None:
        fid_breaks[fid].append(dist_along)
    if fid_break_xy is not None:
        fid_break_xy[fid][dist_along] = (x, y)

    return _rk(x, y), fid
