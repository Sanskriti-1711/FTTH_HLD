# object_layer/crs_utils.py
from qgis.core import (
    QgsProcessingException, QgsVectorLayer, QgsCoordinateReferenceSystem,
    QgsProject, QgsGeometry, QgsWkbTypes, QgsCoordinateTransformContext
)
from qgis import processing

# Common CRSs used in this project
WGS84   = QgsCoordinateReferenceSystem("EPSG:4326")
WEBMERC = QgsCoordinateReferenceSystem("EPSG:3857")
UTM32   = QgsCoordinateReferenceSystem("EPSG:25832")  # Germany (west)
UTM33   = QgsCoordinateReferenceSystem("EPSG:25833")  # Germany (east)

# Country-scale sanity windows (rough Germany bounding boxes)
#  - 4326 degrees
BBOX_4326  = dict(xmin=5.0, xmax=20.0, ymin=45.0, ymax=60.0)
#  - 3857 meters
BBOX_3857  = dict(xmin=5.0 * 111320,  xmax=20.0 * 111320,
                  ymin=45.0 * 110540, ymax=60.0 * 110540)
#  - 25832 and 25833 meters (wide enough windows for real-world data variance)
BBOX_25832 = dict(xmin=200000.0, xmax=700000.0, ymin=5200000.0, ymax=6150000.0)
BBOX_25833 = dict(xmin=200000.0, xmax=900000.0, ymin=5200000.0, ymax=6200000.0)

_SANITY_BY_AUTHID = {
    "EPSG:4326":  BBOX_4326,
    "EPSG:3857":  BBOX_3857,
    "EPSG:25832": BBOX_25832,
    "EPSG:25833": BBOX_25833,
}

def _extent_ok(ext, box):
    if ext is None or ext.isEmpty():
        return False
    return (
        box["xmin"] <= ext.xMinimum() <= box["xmax"] and
        box["xmin"] <= ext.xMaximum() <= box["xmax"] and
        box["ymin"] <= ext.yMinimum() <= box["ymax"] and
        box["ymin"] <= ext.yMaximum() <= box["ymax"]
    )

def guess_storage_crs(layer: QgsVectorLayer):
    """
    Heuristic guess from extents only. Returns 'EPSG:4326', 'EPSG:3857',
    'EPSG:25832', 'EPSG:25833', or None.
    """
    if not layer or not layer.isValid():
        return None
    ext = layer.extent()
    for authid, bbox in _SANITY_BY_AUTHID.items():
        if _extent_ok(ext, bbox):
            return authid
    return None

def ensure_declared_crs(layer: QgsVectorLayer, expected_authid: str, context, feedback):
    """
    If the layer likely stores coordinates in another CRS than it declares,
    ASSIGN the projection (no reprojection math). We prefer 'expected_authid'
    if the extent matches that CRS; else we fall back to a heuristic guess.
    """
    if not layer or not layer.isValid():
        raise QgsProcessingException("Invalid input layer.")

    # Prefer the expected CRS when it fits the extent sanity window
    expected = QgsCoordinateReferenceSystem(expected_authid) if expected_authid else None
    if expected and expected.isValid() and _SANITY_BY_AUTHID.get(expected_authid):
        if _extent_ok(layer.extent(), _SANITY_BY_AUTHID[expected_authid]) and layer.crs().authid() != expected_authid:
            return processing.run(
                "native:assignprojection",
                {"INPUT": layer, "CRS": expected, "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )["OUTPUT"]

    # Otherwise try heuristic guess
    guess = guess_storage_crs(layer)
    if guess and guess != layer.crs().authid():
        return processing.run(
            "native:assignprojection",
            {"INPUT": layer, "CRS": QgsCoordinateReferenceSystem(guess), "OUTPUT": "memory:"},
            context=context, feedback=feedback
        )["OUTPUT"]

    return layer

def _geom_memory_layer_for_clip(geom: QgsGeometry, authid: str) -> QgsVectorLayer:
    """
    Create a one-feature memory layer suitable as 'OVERLAY' for native:clip.
    Always polygonal (builds a polygon from the geometry's envelope if needed).
    """
    crs = QgsCoordinateReferenceSystem(authid)
    # Use polygon; clip overlay must be polygonal. If input isn't polygonal, use its bounding box.
    if not geom or geom.isEmpty():
        raise QgsProcessingException("Empty geometry passed to clip overlay.")
    poly = QgsGeometry(geom)
    if poly.type() != QgsWkbTypes.PolygonGeometry:
        poly = QgsGeometry.fromPolygonXY([poly.boundingBox().asWktPolygon().geometry().asPolygon()[0]])
        # Fallback in case WKT trick misbehaves; final guard:
        poly = QgsGeometry.fromRect(geom.boundingBox())
    lyr = QgsVectorLayer(f"Polygon?crs={authid}", "aoi_src", "memory")
    pr = lyr.dataProvider()
    from qgis.core import QgsFeature
    f = QgsFeature(); f.setGeometry(poly); pr.addFeature(f)
    lyr.updateExtents()
    return lyr

def reproject_safe(layer: QgsVectorLayer, target_authid: str, context, feedback, fast_clip_extent_geom=None):
    """
    Robust reprojection:
      1) Assign correct CRS if declaration is wrong (prefer target CRS when plausible).
      2) Optional: pre-clip in source CRS by 'fast_clip_extent_geom' to reduce size.
      3) Reproject with native:reprojectlayer.
      4) Sanity-check extents in target CRS if we know a window for it.
    Returns a QgsVectorLayer.
    """
    if not layer or not layer.isValid():
        raise QgsProcessingException("Invalid input layer for reprojection.")
    if not target_authid:
        raise QgsProcessingException("Target CRS is not specified.")

    # Step 1: assign if declared wrong (prefer target CRS)
    layer = ensure_declared_crs(layer, target_authid, context, feedback)

    src_crs = layer.crs()
    tgt_crs = QgsCoordinateReferenceSystem(target_authid)
    if not tgt_crs.isValid():
        raise QgsProcessingException(f"Target CRS '{target_authid}' is invalid.")

    # Step 2: optional pre-clip in source CRS (fast)
    if fast_clip_extent_geom is not None:
        aoi_layer = _geom_memory_layer_for_clip(fast_clip_extent_geom, src_crs.authid())
        layer = processing.run(
            "native:clip",
            {"INPUT": layer, "OVERLAY": aoi_layer, "OUTPUT": "memory:"},
            context=context, feedback=feedback
        )["OUTPUT"]

    # Step 3: reproject using project transform context
    ctx: QgsCoordinateTransformContext = QgsProject.instance().transformContext()
    out = processing.run(
        "native:reprojectlayer",
        {"INPUT": layer, "TARGET_CRS": tgt_crs, "OPERATION": 0, "OUTPUT": "memory:"},
        context=context, feedback=feedback
    )["OUTPUT"]

    # Step 4: sanity check (only if we have a window for this CRS)
    box = _SANITY_BY_AUTHID.get(target_authid)
    if box and not _extent_ok(out.extent(), box):
        raise QgsProcessingException(
            f"Reprojection sanity check failed. Output extent {out.extent().toString()} "
            f"does not match expected range for {target_authid}. "
            "Input CRS may be mis-declared; verify in Layer Properties → Source."
        )

    return out
