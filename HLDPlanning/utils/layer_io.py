# -*- coding: utf-8 -*-
"""
Lightweight I/O helpers for resolving/saving layers inside QGIS Processing.
"""

import os
import tempfile
import uuid
from typing import Optional

from qgis.core import (
    QgsVectorLayer,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingUtils,
    QgsProcessing,
)
from qgis import processing

__all__ = [
    "as_layer",
    "_as_layer",            # kept for backward compatibility
    "normalize_gpkg_path",
    "materialize_layer",
]


def _as_layer(obj, context: Optional[QgsProcessingContext] = None, hint: str = "layer") -> QgsVectorLayer:
    """
    Tolerant conversion of various processing outputs into a QgsVectorLayer.

    Accepts:
      - QgsVectorLayer (validated)
      - any QgsMapLayer (validated & cast to vector)
      - layer id / layer URI (resolved via QgsProcessingUtils.mapLayerFromString)
      - filesystem path (OGR-opened)

    Raises QgsProcessingException if it cannot resolve a valid vector layer.
    """
    # 1) Already a vector layer
    if isinstance(obj, QgsVectorLayer):
        if obj.isValid():
            return obj
        raise QgsProcessingException(f"{hint}: provided QgsVectorLayer is invalid")

    # 2) Any map layer (ensure it is vector)
    if isinstance(obj, QgsMapLayer):
        if obj.isValid() and isinstance(obj, QgsVectorLayer):
            return obj
        raise QgsProcessingException(f"{hint}: provided QgsMapLayer is not a valid vector layer")

    # 3) String: try layer id/URI first, then path
    if isinstance(obj, str):
        # layer id / URI
        try:
            lyr = QgsProcessingUtils.mapLayerFromString(obj, context) if context else None
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                return lyr
        except Exception:
            pass

        # filesystem path (supports 'path|layername=xxx' too)
        try:
            lyr = QgsVectorLayer(obj, hint, "ogr")
            if lyr and lyr.isValid():
                return lyr
        except Exception:
            pass

    # If we reach here, resolution failed
    raise QgsProcessingException(f"Expected a valid vector {hint}, got {type(obj).__name__}: {obj!r}")


def as_layer(obj, context: Optional[QgsProcessingContext] = None, hint: str = "layer") -> QgsVectorLayer:
    """
    Public alias for _as_layer, so callers can import a stable API name.
    """
    return _as_layer(obj, context, hint)


def normalize_gpkg_path(path: str, context: Optional[QgsProcessingContext] = None, prefix: str = "tmp") -> str:
    """
    Ensure we have a usable .gpkg file path.

    - Treat '', 'TEMPORARY_OUTPUT', or '*.file' as a cue to create a temp .gpkg in the
      processing temp directory if available (else the system temp dir).
    - Append .gpkg if missing.
    - Create parent directory if needed.
    """
    needs_tmp = (not path) or (str(path).strip().upper() == "TEMPORARY_OUTPUT") or str(path).endswith(".file")
    if needs_tmp:
        try:
            tmpdir = context.temporaryDirectory() if context and hasattr(context, "temporaryDirectory") else None
        except Exception:
            tmpdir = None
        if not tmpdir:
            tmpdir = tempfile.gettempdir()
        path = os.path.join(tmpdir, f"{prefix}_{uuid.uuid4().hex}.gpkg")

    if not path.lower().endswith(".gpkg"):
        path = path + ".gpkg"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def materialize_layer(source, context: QgsProcessingContext, feedback=None, hint: str = "layer") -> QgsVectorLayer:
    """
    Force a 'source' (id/path/layer/sink) into a concrete QgsVectorLayer by saving to a
    temporary output and resolving it back.

    Typical use when an upstream algo returns a sink or when we need a stable layer object.
    """
    try:
        # First try to resolve directly (fast path)
        return _as_layer(source, context, hint)
    except Exception:
        # Fall back to savefeatures → TEMPORARY_OUTPUT, then resolve
        try:
            out = processing.run(
                "native:savefeatures",
                {"INPUT": source, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True,
                context=context,
                feedback=feedback,
            )["OUTPUT"]
            return _as_layer(out, context, hint)
        except Exception as e:
            if feedback:
                try:
                    feedback.reportError(f"⚠️ Could not materialize source for {hint}: {e}")
                except Exception:
                    pass
            raise QgsProcessingException(f"Could not materialize {hint}: {e}")
