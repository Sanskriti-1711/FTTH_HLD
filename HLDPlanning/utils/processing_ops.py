# -*- coding: utf-8 -*-
from typing import Optional
from qgis.core import QgsGeometry, QgsVectorLayer, QgsProcessingContext, QgsProcessing
from qgis import processing
from .layer_io import as_layer


def erase_or_difference(input_layer, overlay_layer, context: QgsProcessingContext, feedback, name="erased"):
    """
    SAFE ERASE helper (works on builds without native:erase).
    Prefer difference; fall back to erase.
    """
    params = {"INPUT": input_layer, "OVERLAY": overlay_layer, "OUTPUT": "TEMPORARY_OUTPUT"}
    try:
        out = processing.run("native:difference", params, is_child_algorithm=True,
                             context=context, feedback=feedback)["OUTPUT"]
    except Exception:
        out = processing.run("native:erase", params, is_child_algorithm=True,
                             context=context, feedback=feedback)["OUTPUT"]
    return as_layer(out, context, name)

def try_unary_union(layer: QgsVectorLayer) -> Optional[QgsGeometry]:
    """
    Defensive unary union: returns None on any error, so callers can fall back.
    """
    try:
        geoms = [f.geometry() for f in layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
        if not geoms:
            return None
        return QgsGeometry.unaryUnion(geoms)
    except Exception:
        return None

def reproject_to(layer, crs_authid: str, context: QgsProcessingContext, feedback=None, is_child=True):
    out = processing.run(
        "native:reprojectlayer",
        {"INPUT": layer, "TARGET_CRS": crs_authid, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        is_child_algorithm=is_child, context=context, feedback=feedback
    )["OUTPUT"]
    return as_layer(out, context, "reproj")

def materialize_temp(input_src, context, feedback, name: str):
    """
    Convert a sink id / layer / path into a concrete, temporary QgsVectorLayer.
    Best-effort; returns None on failure.
    """
    try:
        from qgis import processing
        from .layer_io import as_layer
        out = processing.run(
            "native:savefeatures",
            {"INPUT": input_src, "OUTPUT": "TEMPORARY_OUTPUT"},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"]
        return as_layer(out, context, name)
    except Exception:
        return None
