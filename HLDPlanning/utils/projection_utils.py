# -*- coding: utf-8 -*-
from qgis import processing
from qgis.core import QgsVectorLayer, QgsProcessingContext
from .layer_io import as_layer  # <-- add this

def reproject_to(layer, crs_authid: str, context: QgsProcessingContext, feedback=None) -> QgsVectorLayer:
    """
    Reproject to CRS authid (e.g. 'EPSG:25833').
    Always returns a QgsVectorLayer (or None if input is falsy).
    Accepts QgsVectorLayer, layer id/URI, or filesystem path.
    """
    if not layer:
        return None

    # Normalize input to a real layer first (avoids passing a bare string to Processing)
    lyr_in = as_layer(layer, context=context, hint="reproject_to:INPUT")

    try:
        out = processing.run(
            "native:reprojectlayer",
            {"INPUT": lyr_in, "TARGET_CRS": crs_authid, "OUTPUT": "TEMPORARY_OUTPUT"},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"]
    except Exception:
        if feedback:
            try:
                nm = lyr_in.name() if isinstance(lyr_in, QgsVectorLayer) else str(layer)
            except Exception:
                nm = str(layer)
            feedback.reportError(f"Reprojection to {crs_authid} failed for layer {nm}; passing through.")
        out = lyr_in

    # Normalize the OUTPUT (can be a URI) to a real vector layer
    return as_layer(out, context=context, hint="reproject_to:OUTPUT")
