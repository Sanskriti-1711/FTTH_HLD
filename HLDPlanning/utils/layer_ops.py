from qgis import processing
from qgis.core import (
    QgsApplication,
    QgsProcessing,
    QgsVectorLayer,
    QgsFeature,
    QgsFeatureSink,
)

def fix_geometries(layer, context, feedback):
    """Fix invalid geometries in a layer."""
    return processing.run(
        "native:fixgeometries",
        {"INPUT": layer, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        context=context,
        feedback=feedback,
    )["OUTPUT"]

def reproject_if_needed(layer, target_crs, context, feedback):
    """Reproject layer if CRS differs from target CRS."""
    if layer.crs() == target_crs:
        return layer
    return processing.run(
        "native:reprojectlayer",
        {"INPUT": layer, "TARGET_CRS": target_crs, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        context=context,
        feedback=feedback,
    )["OUTPUT"]

def subset_by_id(layer, field_name, id_key, normalize_func=None):
    """Return a subset of features matching id_key."""
    from qgis.core import QgsWkbTypes

    norm = normalize_func or (lambda v: str(v).strip().lower() if v else "")
    crs_auth = layer.crs().authid()
    wkb = layer.wkbType()
    out = QgsVectorLayer(f"{QgsWkbTypes.displayString(wkb)}?crs={crs_auth}", "_subset", "memory")
    dp = out.dataProvider()
    dp.addAttributes(layer.fields())
    out.updateFields()
    adds = []
    for f in layer.getFeatures():
        if norm(f[field_name]) == id_key:
            nf = QgsFeature(out.fields())
            nf.setGeometry(f.geometry())
            nf.setAttributes(f.attributes())
            adds.append(nf)
    if adds:
        dp.addFeatures(adds)
    out.updateExtents()
    return out

def find_first_alg(*ids):
    """Return first available algorithm ID from list."""
    reg = QgsApplication.processingRegistry()
    for aid in ids:
        if reg.algorithmById(aid):
            return aid
    return None

def snap_layer(input_layer, ref_layer, tolerance, context, feedback):
    """Snap geometries between input and reference layers."""
    alg = find_first_alg("native:snapgeometries", "qgis:snapgeometries")
    if not alg:
        feedback.pushWarning("Snap algorithm not available; skipping snapping.")
        return input_layer
    return processing.run(
        alg,
        {
            "INPUT": input_layer,
            "REFERENCE_LAYER": ref_layer,
            "TOLERANCE": tolerance,
            "BEHAVIOR": 0,
            "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
        },
        context=context,
        feedback=feedback,
    )["OUTPUT"]

def linemerge_layer(layer, context, feedback):
    """Run line merge on input layer if algorithm available."""
    alg = find_first_alg("native:linemerge", "qgis:linemerge")
    if not alg:
        return layer
    return processing.run(
        alg, {"INPUT": layer, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT}, context=context, feedback=feedback
    )["OUTPUT"]
