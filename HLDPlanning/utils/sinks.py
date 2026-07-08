# -*- coding: utf-8 -*-
from qgis.core import QgsVectorLayer, QgsProcessingContext

def copy_to_sink(alg, p, context: QgsProcessingContext,
                 src_layer: QgsVectorLayer, sink_param: str,
                 name_hint: str, feedback=None):
    """
    Uses alg.parameterAsSink to allocate a sink and copy all features.
    Returns sink_id or None.
    """
    if not src_layer:
        return None
    fields = src_layer.fields()
    wkb    = src_layer.wkbType()
    crs    = src_layer.crs()
    sink, sink_id = alg.parameterAsSink(p, sink_param, context, fields, wkb, crs)
    if sink_id:
        for f in src_layer.getFeatures():
            sink.addFeature(f)
        return sink_id
    if feedback: feedback.reportError(f"Could not allocate sink for {name_hint}.")
    return None
