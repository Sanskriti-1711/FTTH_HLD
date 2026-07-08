# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsVectorLayer, QgsWkbTypes, QgsVectorLayer, QgsFields, QgsField, QgsFeature

def tag_simple(layer: QgsVectorLayer, tag_value: str) -> QgsVectorLayer:
    """Return a memory layer with same features + trench_type = tag_value."""
    if not layer:
        return None
    gtype = "MultiLineString" if QgsWkbTypes.isMultiType(layer.wkbType()) else "LineString"
    mem = QgsVectorLayer(f"{gtype}?crs={layer.crs().authid()}", "mem", "memory")
    prov = mem.dataProvider()
    fields = QgsFields(layer.fields())
    if "trench_type" not in fields.names():
        fields.append(QgsField("trench_type", QVariant.String))
    prov.addAttributes(fields); mem.updateFields()

    trench_idx = mem.fields().indexFromName("trench_type")
    for f in layer.getFeatures():
        nf = QgsFeature(fields)
        nf.setGeometry(f.geometry())
        attrs = f.attributes()
        if len(attrs) == len(fields):
            attrs[trench_idx] = tag_value
        else:
            attrs = attrs + [tag_value]
        nf.setAttributes(attrs)
        prov.addFeatures([nf])
    mem.updateExtents()
    return mem
