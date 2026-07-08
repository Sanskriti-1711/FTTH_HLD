# -*- coding: utf-8 -*-
# Generic line network segmentizer for PyQGIS
import math
from typing import List, Optional
from qgis.core import (
    QgsVectorLayer, QgsGeometry, QgsPointXY, QgsProcessing, QgsProcessingContext,
    QgsProcessingFeedback, QgsProcessingException, QgsFeature, QgsFeatureRequest,
    QgsSpatialIndex
)
from qgis import processing

from .geom_basic import (
    geom_ok as _geom_ok,
    line_parts as _line_parts,
    point_of as _point_of,
)

def _interp_on_polyline(pts: List[QgsPointXY], d: float) -> QgsPointXY:
    if d <= 0:
        return QgsPointXY(pts[0])
    total = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = math.hypot(b.x() - a.x(), b.y() - a.y())
        if total + seg >= d:
            t = 0.0 if seg == 0 else (d - total) / seg
            return QgsPointXY(a.x() + t * (b.x() - a.x()), a.y() + t * (b.y() - a.y()))
        total += seg
    return QgsPointXY(pts[-1])

def _slice_polyline_by_distances(base_geom: QgsGeometry, distances: List[float], tol: float) -> List[QgsGeometry]:
    pts = base_geom.asPolyline()
    if not pts or len(pts) < 2:
        return []
    # cumulative distances
    cum = [0.0]
    for i in range(1, len(pts)):
        seg = math.hypot(pts[i].x() - pts[i - 1].x(), pts[i].y() - pts[i - 1].y())
        cum.append(cum[-1] + seg)
    L = cum[-1]
    cuts = [max(0.0, min(L, float(x))) for x in distances]
    cuts = sorted({0.0, *cuts, L})
    out_geoms = []
    for i in range(1, len(cuts)):
        a, b = cuts[i - 1], cuts[i]
        if b - a <= tol:  # guard against tiny slivers
            continue
        pa = _interp_on_polyline(pts, a)
        pb = _interp_on_polyline(pts, b)
        seg_pts = [pa]
        for j in range(1, len(pts) - 1):
            if a < cum[j] < b:
                seg_pts.append(pts[j])
        seg_pts.append(pb)
        # dedupe near-coincident vertices
        cleaned = [seg_pts[0]]
        for q in seg_pts[1:]:
            if math.hypot(q.x() - cleaned[-1].x(), q.y() - cleaned[-1].y()) > 1e-6:
                cleaned.append(q)
        if len(cleaned) >= 2:
            out_geoms.append(QgsGeometry.fromPolylineXY(cleaned))
    return out_geoms

def segmentize_merged_lines(
    all_layers: List[QgsVectorLayer],
    context: QgsProcessingContext,
    feedback: QgsProcessingFeedback,
    crs_fallback: str = "EPSG:25833",
    tol: float = 0.02,
) -> QgsVectorLayer:
    """
    Segments merged lines at each mutual intersection.
    Returns a memory LineString layer with source fields preserved.
    """
    if not all_layers:
        raise QgsProcessingException("No layers supplied for segmentation.")

    # Merge inputs (keep CRS from the first valid layer)
    crs_auth = None
    valid_layers = []
    for lyr in all_layers:
        if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.featureCount() >= 0:
            valid_layers.append(lyr)
            if crs_auth is None:
                crs_auth = lyr.crs().authid()
    if not valid_layers:
        raise QgsProcessingException("All input layers for segmentation are invalid/empty.")

    merged = processing.run(
        "native:mergevectorlayers",
        {"LAYERS": valid_layers, "CRS": crs_auth or crs_fallback, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        is_child_algorithm=True, context=context, feedback=feedback
    )["OUTPUT"]

    if isinstance(merged, QgsVectorLayer):
        merged_lyr = merged
    else:
        merged_lyr = QgsVectorLayer(merged, "merged_for_seg", "ogr")
    if not merged_lyr or not merged_lyr.isValid():
        raise QgsProcessingException("Failed to build merged layer for segmentation.")

    if merged_lyr.featureCount() == 0:
        return QgsVectorLayer(f"LineString?crs={merged_lyr.crs().authid()}", "segmented_mem_empty", "memory")

    # Intersections and dedup
    inter_pts = processing.run(
        "native:lineintersections",
        {"INPUT": merged_lyr, "INTERSECT": merged_lyr, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        is_child_algorithm=True, context=context, feedback=feedback
    )["OUTPUT"]
    if not isinstance(inter_pts, QgsVectorLayer):
        inter_pts = QgsVectorLayer(inter_pts, "intersections_pts", "ogr")

    try:
        inter_pts = processing.run(
            "native:deleteduplicategeometries",
            {"INPUT": inter_pts, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"]
        if not isinstance(inter_pts, QgsVectorLayer):
            inter_pts = QgsVectorLayer(inter_pts, "intersections_pts_dedup", "ogr")
    except Exception:
        pass

    idx_pts = QgsSpatialIndex(inter_pts.getFeatures())

    out = QgsVectorLayer(f"LineString?crs={merged_lyr.crs().authid()}", "segmented_mem", "memory")
    pr = out.dataProvider()
    pr.addAttributes(merged_lyr.fields()); out.updateFields()

    has_lineSubstring = hasattr(QgsGeometry, "lineSubstring")
    pieces = 0

    for f in merged_lyr.getFeatures():
        g = f.geometry()
        if not _geom_ok(g):
            continue
        for ln in _line_parts(g):
            if not ln or len(ln) < 2:
                continue
            base = QgsGeometry.fromPolylineXY(ln)
            L = base.length()
            if L <= tol:
                continue

            ds = [0.0, L]
            bb = base.boundingBox()
            for pid in idx_pts.intersects(bb):
                req = QgsFeatureRequest().setFilterFid(pid)
                pf_it = inter_pts.getFeatures(req)
                pf = next(pf_it, None)
                if not pf:
                    continue
                p = _point_of(pf.geometry())
                if not p:
                    continue
                d = base.lineLocatePoint(QgsGeometry.fromPointXY(p))
                if d is None:
                    continue
                d = float(max(0.0, min(L, d)))
                if all(abs(d - x) > tol for x in ds):
                    ds.append(d)
            ds.sort()

            if has_lineSubstring:
                for i in range(1, len(ds)):
                    a, b = ds[i - 1], ds[i]
                    if b - a <= tol:
                        continue
                    seg = base.lineSubstring(a, b)
                    if _geom_ok(seg) and seg.length() > tol:
                        nf = QgsFeature(out.fields())
                        nf.setAttributes(f.attributes())
                        nf.setGeometry(seg)
                        pr.addFeatures([nf]); pieces += 1
            else:
                for seg in _slice_polyline_by_distances(base, ds, tol):
                    if _geom_ok(seg) and seg.length() > tol:
                        nf = QgsFeature(out.fields())
                        nf.setAttributes(f.attributes())
                        nf.setGeometry(seg)
                        pr.addFeatures([nf]); pieces += 1

    out.updateExtents()
    feedback.pushInfo(f"Segmentation created {pieces} short pieces (memory).")
    return out
