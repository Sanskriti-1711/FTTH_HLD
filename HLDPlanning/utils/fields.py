# -*- coding: utf-8 -*-
from typing import List, Optional
from qgis.core import QgsVectorLayer, QgsFields, QgsField
from qgis.PyQt.QtCore import QMetaType


# Common/core columns that can be shared across multiple algorithm outputs.
class COMMON_FIELDS:
    SRC_ID = "SRC_ID"
    STAGE = "STAGE"
    POLYGON_ID = "POLYGON_ID"
    PDP_ID = "PDP_ID"
    MFG_ID = "MFG_ID"
    NODE_TYPE = "NODE_TYPE"


# Thin profile by output role (keep only what is needed operationally).
THIN_PROFILES = {
    "INTERMEDIATE_POINT": [COMMON_FIELDS.SRC_ID, COMMON_FIELDS.STAGE],
    "INTERMEDIATE_LINE": [COMMON_FIELDS.SRC_ID, COMMON_FIELDS.STAGE],
    "INTERMEDIATE_POLYGON": [COMMON_FIELDS.SRC_ID, COMMON_FIELDS.STAGE],
    "PDP": [
        COMMON_FIELDS.POLYGON_ID,
        COMMON_FIELDS.PDP_ID,
        COMMON_FIELDS.MFG_ID,
        COMMON_FIELDS.NODE_TYPE,
        COMMON_FIELDS.SRC_ID,
        COMMON_FIELDS.STAGE,
    ],
    "MFG": [
        COMMON_FIELDS.MFG_ID,
        COMMON_FIELDS.NODE_TYPE,
        COMMON_FIELDS.SRC_ID,
        COMMON_FIELDS.STAGE,
    ],
}


def build_fields(names: List[str]) -> QgsFields:
    """Build a QgsFields schema from column names using String type by default."""
    out = QgsFields()
    for n in names:
        out.append(QgsField(n, QMetaType.Type.QString))
    return out

def first_field_case_insensitive(layer: QgsVectorLayer, candidates: List[str]) -> Optional[str]:
    """
    Finds the first existing field among 'candidates', case-insensitive,
    returning the actual case-correct field name.
    """
    names = layer.fields().names()
    lower_map = {n.lower(): n for n in names}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None

def expr_in_ci(fieldname: str, values):
    """
    Case-insensitive equality expression: lower("field") IN ('v1','v2',...)
    NOTE: values are expected to be clean strings.
    """
    vals = ",".join([f"'{str(v).lower()}'" for v in values])
    return f"lower(\"{fieldname}\") IN ({vals})"
