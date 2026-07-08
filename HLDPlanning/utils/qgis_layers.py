# ----------------------------------------------
# utils/qgis_layers.py
# ----------------------------------------------
from __future__ import annotations

from typing import Optional, Any

from qgis.core import (
    QgsVectorLayer,
    QgsGeometry,
    QgsWkbTypes,
)


__all__ = [
    "open_layer",
    "safe_len",
    "total_length",
    "sum_length_by_field",
    "count_features",
]


def open_layer(uri_or_path: str, name_hint: str = "lyr") -> Optional[QgsVectorLayer]:
    """
    Open a vector layer via OGR provider and validate it.

    Returns:
        QgsVectorLayer or None if invalid/missing.
    """
    if not uri_or_path:
        return None
    lyr = QgsVectorLayer(uri_or_path, name_hint, "ogr")
    # featureCount() >= 0 is a cheap validity-ish check without iterating features
    return lyr if (lyr and lyr.isValid() and lyr.featureCount() >= 0) else None


def safe_len(g: Optional[QgsGeometry], geometry_type_hint: Optional[QgsWkbTypes.GeometryType] = None) -> float:
    """
    Robustly compute linear length for a geometry; returns 0.0 for None/empty/non-linear types.

    Notes:
      - If `geometry_type_hint` is provided and not LINEGeometry, returns 0.0 immediately.
      - Uses geometry.length() which measures in layer units; callers are expected to provide
        projected layers when they need metres.
    """
    if g is None:
        return 0.0
    try:
        if g.isEmpty():
            return 0.0
    except Exception:
        # Some invalid geometries may throw on isEmpty
        pass

    # Reject obviously non-line geometry if a hint is given
    if geometry_type_hint is not None and geometry_type_hint != QgsWkbTypes.LineGeometry:
        return 0.0

    try:
        # length() works for line and multi-line; for other types it returns 0
        val = float(g.length())
        # guard against NaN/inf
        return val if (val == val and abs(val) != float("inf")) else 0.0
    except Exception:
        return 0.0


def _layer_geometry_type(layer: QgsVectorLayer) -> Optional[QgsWkbTypes.GeometryType]:
    try:
        return QgsWkbTypes.geometryType(layer.wkbType())
    except Exception:
        return None


def total_length(layer: Optional[QgsVectorLayer]) -> float:
    """
    Sum lengths of all features in a layer (only line layers contribute).
    Returns a value rounded to 2 decimals (layer units).
    """
    if not layer:
        return 0.0

    gtype = _layer_geometry_type(layer)
    if gtype is not None and gtype != QgsWkbTypes.LineGeometry:
        # Non-line layers have no meaningful "total length" here.
        return 0.0

    total = 0.0
    try:
        for f in layer.getFeatures():
            # getattr to be safe even if f.geometry isn't callable in some mock contexts
            geom = getattr(f, "geometry", None)
            geom = geom() if callable(geom) else None
            total += safe_len(geom, geometry_type_hint=QgsWkbTypes.LineGeometry)
    except Exception:
        # swallow iteration/geometry errors; we prefer a best-effort sum
        pass
    return round(total, 2)


def sum_length_by_field(layer: Optional[QgsVectorLayer], field: str, value: Any) -> float:
    """
    Sum lengths of features where feature[field] == value.
    Only line layers contribute.
    """
    if not layer or field not in layer.fields().names():
        return 0.0

    gtype = _layer_geometry_type(layer)
    if gtype is not None and gtype != QgsWkbTypes.LineGeometry:
        return 0.0

    total = 0.0
    try:
        for f in layer.getFeatures():
            try:
                if f[field] == value:
                    geom = f.geometry()
                    total += safe_len(geom, geometry_type_hint=QgsWkbTypes.LineGeometry)
            except Exception:
                # skip corrupt rows gracefully
                continue
    except Exception:
        pass

    return round(total, 2)


def count_features(layer: Optional[QgsVectorLayer]) -> int:
    """
    Safe feature count (0 if layer is None/invalid).
    """
    try:
        return int(layer.featureCount()) if layer else 0
    except Exception:
        return 0
