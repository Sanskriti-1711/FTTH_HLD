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
    # Brownfield / existing infrastructure
    INFRA_STATUS = "INFRA_STATUS"       # Existing | Reused | Proposed | Removed
    VERIFY_STATUS = "VERIFY_STATUS"     # Verified | Assumed | Survey Required
    CAPACITY_USED = "CAPACITY_USED"     # sub-ducts/strands currently occupied
    CAPACITY_TOTAL = "CAPACITY_TOTAL"   # total sub-ducts/strands available
    REUSE_SOURCE = "REUSE_SOURCE"       # brownfield asset_id that was reused
    ASSET_TYPE = "ASSET_TYPE"           # duct | chamber | pole | fibre | cabinet | trench

    # HLD_attr.docx civil-infrastructure catalogue
    USAGE_TYPE = "USAGE_TYPE"           # Feeder | Distribution | Garden (trench usage)
    CONSTRUCT = "CONSTRUCT"             # Open Cut | Micro Trench | HDD
    WIDTH_MM = "WIDTH_MM"
    DEPTH_MM = "DEPTH_MM"
    SURFACE = "SURFACE"                 # Asphalt | Concrete | Footpath
    REINSTATE = "REINSTATE"             # Road | Sidewalk
    PARENT_TRENCH = "PARENT_TRENCH"     # Trench ID the duct runs in
    DUCT_TYPE = "DUCT_TYPE"             # e.g. "4-Way HDPE"
    DIAMETER_MM = "DIAMETER_MM"
    WAYS = "WAYS"                       # number of ways / sub-ducts
    START_CHAMBER = "START_CHAMBER"
    END_CHAMBER = "END_CHAMBER"
    OCCUPANCY_PCT = "OCCUPANCY_PCT"
    SPARE_PCT = "SPARE_PCT"

    # HLD_attr.docx cable catalogue
    CABLE_TYPE = "CABLE_TYPE"           # Feeder | Distribution | Garden
    FIBER_COUNT = "FIBER_COUNT"
    LENGTH_M = "LENGTH_M"
    UTIL_PCT = "UTIL_PCT"
    SOURCE_NODE = "SOURCE_NODE"         # MFG id / PDP id the cable originates from

    # HLD_attr.docx equipment catalogue
    EQUIP_TYPE = "EQUIP_TYPE"           # MFG | PDP | Splitter | FAT | ONT
    EQUIP_NAME = "EQUIP_NAME"
    LOCATION = "LOCATION"               # Central Office | Street Cabinet | Building
    EQUIP_CAPACITY = "EQUIP_CAPACITY"
    SPLIT_RATIO = "SPLIT_RATIO"
    VENDOR = "VENDOR"
    POWER_REQ = "POWER_REQ"
    MAINT_ZONE = "MAINT_ZONE"

    # HLD_attr.docx chamber / pole catalogue
    STRUCT_ID = "STRUCT_ID"             # MH-0001 / CH-0001 / HH-0001
    CHAMBER_TYPE = "CHAMBER_TYPE"       # Manhole | Chamber | Handhole
    CONN_DUCTS = "CONN_DUCTS"
    SIZE = "SIZE"
    EQUIPMENT = "EQUIPMENT"             # associated equipment id (PDP/FAT)
    POLE_ID = "POLE_ID"                 # PL-0001
    POLE_TYPE = "POLE_TYPE"             # Utility | Telecom
    MATERIAL = "MATERIAL"               # Concrete
    HEIGHT_M = "HEIGHT_M"
    CABLE_CNT = "CABLE_CNT"

    # Aerial drop trench / cable
    AERIAL_TRENCH_ID = "AERIAL_TRENCH_ID"  # AT-0001
    AERIAL_REASON = "AERIAL_REASON"        # why aerial was chosen
    FROM_POLE = "FROM_POLE"                # PL-0001
    TO_PREMISE = "TO_PREMISE"              # ADDR_ID

    # Trench catalogue (used by multiple algorithms)
    TRENCH_TYPE = "TRENCH_TYPE"            # Feeder | Distribution | Garden | Aerial_Drop
    CONSTRUCTION_METHOD = "CONSTRUCTION_METHOD"  # Open Cut | Micro Trench | HDD | Overhead
    FIBER_COUNT = "FIBER_COUNT"
    POLE_SPACING_M = "POLE_SPACING_M"
    CROSSINGS = "CROSSINGS"
    PERMIT_REQUIRED = "PERMIT_REQUIRED"


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
    "EXISTING_INFRA": [
        COMMON_FIELDS.ASSET_TYPE,
        COMMON_FIELDS.INFRA_STATUS,
        COMMON_FIELDS.VERIFY_STATUS,
        COMMON_FIELDS.CAPACITY_USED,
        COMMON_FIELDS.CAPACITY_TOTAL,
        COMMON_FIELDS.SRC_ID,
    ],
    "CHAMBER": [
        COMMON_FIELDS.STRUCT_ID,
        COMMON_FIELDS.CHAMBER_TYPE,
        COMMON_FIELDS.PARENT_TRENCH,
        COMMON_FIELDS.CONN_DUCTS,
        COMMON_FIELDS.SIZE,
        COMMON_FIELDS.EQUIPMENT,
        COMMON_FIELDS.CAPACITY_USED,
        COMMON_FIELDS.CAPACITY_TOTAL,
        COMMON_FIELDS.INFRA_STATUS,
        COMMON_FIELDS.VERIFY_STATUS,
        COMMON_FIELDS.STAGE,
    ],
    "POLE": [
        COMMON_FIELDS.POLE_ID,
        COMMON_FIELDS.POLE_TYPE,
        COMMON_FIELDS.MATERIAL,
        COMMON_FIELDS.HEIGHT_M,
        COMMON_FIELDS.CABLE_CNT,
        COMMON_FIELDS.EQUIPMENT,
        COMMON_FIELDS.CAPACITY_USED,
        COMMON_FIELDS.CAPACITY_TOTAL,
        COMMON_FIELDS.INFRA_STATUS,
        COMMON_FIELDS.VERIFY_STATUS,
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
