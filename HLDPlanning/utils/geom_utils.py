from qgis.core import QgsWkbTypes

def geom_str_from_wkb(wkb_type):
    """Return geometry string (Point/LineString/Polygon/Multi*) from WKB type."""
    base_type = QgsWkbTypes.geometryType(wkb_type)
    base = {
        QgsWkbTypes.PointGeometry: "Point",
        QgsWkbTypes.LineGeometry: "LineString",
        QgsWkbTypes.PolygonGeometry: "Polygon",
    }.get(base_type, "Unknown")
    if QgsWkbTypes.isMultiType(wkb_type):
        base = "Multi" + base
    return base
