# -*- coding: utf-8 -*-
"""Build the auditable, one-row-per-premise service-status layer."""

from typing import Dict, List, Optional, Set

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterVectorLayer,
    QgsWkbTypes,
)

from ..utils.fields import first_field_case_insensitive


# A drop shorter than this is drafting noise, not a laid connection.
MIN_DROP_LENGTH_M = 0.0001


def is_usable_drop_geometry(geom) -> bool:
    """True when a published drop trace is real constructed connection.

    The SERVED verdict stays ID-based (``_drop_addresses``); this gate only
    removes rows that cannot represent a connection on the ground:

    * empty / null geometry;
    * a geometry that is not a line (the drop parameters are line-typed, so a
      stray point or polygon row is not a drop);
    * a line with no length — the two-identical-vertices stub that a failed
      or collapsed write leaves behind;
    * topologically invalid geometry. This also catches a multi-part drop in
      which any single part collapsed: such a row measures its full length but
      fails GEOS with "Too few points in geometry component", and is rejected
      rather than counted as a partial connection.

    Without it, one stub row carrying a valid ``ADDR_ID`` marks a premise
    SERVED that nothing is actually cabled to.
    """
    if geom is None or geom.isEmpty():
        return False
    if geom.type() != QgsWkbTypes.LineGeometry:
        return False
    if geom.length() <= MIN_DROP_LENGTH_M:
        return False
    return bool(geom.isGeosValid())


class ServedPremisesAlgorithm(QgsProcessingAlgorithm):
    P_OBJECTS = "OBJECTS"
    P_GARDEN = "GARDEN_TRENCH"
    P_AERIAL = "AERIAL_DROPS"
    O_SERVED = "OUTPUT"

    def name(self):
        return "served_premises"

    def displayName(self):
        return "Build Served Premises Register"

    def group(self):
        return "08 Quality Assurance"

    def groupId(self):
        return "08_quality_assurance"

    def createInstance(self):
        return ServedPremisesAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_OBJECTS, "Premises / objects", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_GARDEN, "Published underground premise drops", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_AERIAL, "Published aerial premise drops", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_SERVED, "Served premises register", QgsProcessing.TypeVectorPoint))

    def processAlgorithm(self, parameters, context, feedback):
        objects = self.parameterAsVectorLayer(parameters, self.P_OBJECTS, context)
        garden = self.parameterAsVectorLayer(parameters, self.P_GARDEN, context)
        aerial = self.parameterAsVectorLayer(parameters, self.P_AERIAL, context)
        if objects is None or not objects.isValid():
            raise QgsProcessingException("A valid Objects layer is required.")

        addr_field = first_field_case_insensitive(objects, ["ADDR_ID", "addr_id", "id"])
        if not addr_field:
            raise QgsProcessingException("Objects layer has no premise/address ID field.")
        fields = QgsFields()
        for name, typ in (
            ("ADDR_ID", QMetaType.Type.QString),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("PDP_ID", QMetaType.Type.QString),
            ("MFG_ID", QMetaType.Type.QString),
            ("HH", QMetaType.Type.Double),
            ("SERVICE_STATUS", QMetaType.Type.QString),
            ("SERVICE_METHOD", QMetaType.Type.QString),
            ("SERVICE_REASON", QMetaType.Type.QString),
        ):
            fields.append(QgsField(name, typ))
        sink, dest = self.parameterAsSink(
            parameters, self.O_SERVED, context, fields,
            QgsWkbTypes.Point, objects.crs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.O_SERVED))

        served_ug = self._drop_addresses(garden)
        served_aerial = self._drop_addresses(aerial)
        pdp_field = first_field_case_insensitive(objects, ["PDP_ID", "pdp_id"])
        polygon_field = first_field_case_insensitive(objects, ["POLYGON_ID", "polygon_id"])
        mfg_field = first_field_case_insensitive(objects, ["MFG_ID", "mfg_id"])
        hh_field = first_field_case_insensitive(objects, ["HH", "HHS", "households"])
        written = 0
        linked = 0
        for obj in objects.getFeatures():
            geom = obj.geometry()
            if geom is None or geom.isEmpty():
                continue
            raw = obj[addr_field]
            addr = str(raw).strip() if raw not in (None, "") else ""
            if not addr:
                continue
            status = "UNSERVED"
            method = "NONE"
            reason = "No published underground or aerial drop for premise ID."
            if addr in served_ug:
                status, method, reason = "SERVED", "UNDERGROUND", ""
            elif addr in served_aerial:
                status, method, reason = "SERVED", "AERIAL", ""
            if status == "SERVED":
                linked += 1
            feature = QgsFeature(fields)
            feature.setGeometry(QgsGeometry(geom))
            feature["ADDR_ID"] = addr
            feature["POLYGON_ID"] = str(obj[polygon_field]) if polygon_field and obj[polygon_field] not in (None, "") else None
            feature["PDP_ID"] = str(obj[pdp_field]) if pdp_field and obj[pdp_field] not in (None, "") else None
            feature["MFG_ID"] = str(obj[mfg_field]) if mfg_field and obj[mfg_field] not in (None, "") else None
            try:
                feature["HH"] = float(obj[hh_field]) if hh_field and obj[hh_field] not in (None, "") else 1.0
            except (TypeError, ValueError):
                feature["HH"] = 1.0
            feature["SERVICE_STATUS"] = status
            feature["SERVICE_METHOD"] = method
            feature["SERVICE_REASON"] = reason
            sink.addFeature(feature)
            written += 1
        feedback.pushInfo(
            "Served premises register: {}/{} premises served; {} unserved.".format(
                linked, written, written - linked))
        return {self.O_SERVED: dest}

    @staticmethod
    def _drop_addresses(layer) -> Set[str]:
        if layer is None or not layer.isValid():
            return set()
        field = first_field_case_insensitive(layer, ["ADDR_ID", "addr_id", "id"])
        if not field:
            return set()
        out = set()
        for feature in layer.getFeatures():
            geom = feature.geometry()
            if not is_usable_drop_geometry(geom):
                continue
            value = feature[field]
            if value in (None, ""):
                continue
            # Drop records are expected one premise per row; if a consumer
            # supplied a comma-list, normalize it without treating blank IDs as served.
            out.update(part.strip() for part in str(value).split(",") if part.strip())
        return out
