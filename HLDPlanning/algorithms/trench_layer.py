# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false
"""
Trench Layer — existing MFG; FOOTWAY network; crossings-only over roads.

- Footway/Service graph first; vehicular roads only for drill crossings.
- Erase footway bits inside intersection buffers (prevents mid-road crossing).
- Feeder/Distribution route on (footway ∪ service ∪ drills) only.
- Use existing MFG (selected/first feature).
- Pure PyQGIS segmentation to short pieces.
- SAFE erase helper: tries native:difference, falls back to native:erase.

QGIS 3.44 / EPSG:25833.
"""
import re
import os, math, uuid, tempfile
from typing import Tuple, Optional, Iterable, List, Dict, Set
from collections import defaultdict
try:
    import networkx as nx
except Exception:
    nx = None
from qgis.PyQt.QtCore import QVariant, QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingFeedback,
    QgsProcessingException, QgsProcessingParameterFileDestination, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterNumber,QgsExpression,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterField,QgsProject,
    QgsField, QgsFields, QgsFeature, QgsFeatureRequest, QgsProcessingParameterString,
    QgsVectorLayer, QgsGeometry, QgsPointXY, QgsWkbTypes, QgsSpatialIndex,
    QgsProcessingOutputLayerDefinition, QgsVectorFileWriter, QgsCoordinateTransformContext,
    QgsProcessingParameterBoolean
)
from qgis import processing
# inside HLDPlanning/algorithms/trench_layer.py

# fallback for script execution outside plugin (optional)
from ..utils.layer_ops import fix_geometries, reproject_if_needed, snap_layer, linemerge_layer
from ..utils.string_utils import normalize_key
from ..utils.geom_utils import geom_str_from_wkb

# ---- shared utils (from utils/ package)
from ..utils.layer_io import as_layer, normalize_gpkg_path as _normalize_gpkg_path, materialize_layer as _materialize_layer
from ..utils.geometry_ops import unary_union_geoms as _unary_union_geoms
from ..utils.segmentizer import segmentize_merged_lines
from ..utils.processing_ops import erase_or_difference as _erase_or_difference
from ..utils.geom_basic import geom_ok as _geom_ok, point_of as _point_of, to_multiline as _to_multiline, safe_collect as _safe_collect
from ..utils.spatial_grid import qkey_point as _qkey
from ..utils.fields import first_field_case_insensitive as _first_field_case_insensitive
from ..utils.style_utils import colorize_mem_line_layer as _colorize_mem_line_layer
from ..utils.expressions import swap_canonical_field, expr_ci_in, expr_area_not_true
from ..utils.sinks import copy_to_sink
from ..utils.intersections import build_intersection_buffers
from ..utils.nearest import nearest_point_on_lines
from ..utils.graph_ops import add_lines_to_graph as _add_lines_to_graph, snap_to_nodes as _snap_to_nodes
from ..utils.geom_ops import nearest_point_and_distance as _nearest_point_and_distance
from ..utils.graph_nodes import build_node_index_from_graph as _build_node_index_from_graph, nearest_node as _nearest_node
from ..utils.projection_utils import reproject_to

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def _tr(s: str) -> str:
    return QCoreApplication.translate("TrenchLayer", s)

# ---- trench-specific helpers (keep local here)
def _dir_on_line_near_point(line_geom: QgsGeometry, pt: QgsPointXY):
    """Unit direction vector of nearest segment to point."""
    if not _geom_ok(line_geom):
        return None
    lines = line_geom.asMultiPolyline() if line_geom.isMultipart() else [line_geom.asPolyline()]
    q = QgsGeometry.fromPointXY(pt)
    best = (float("inf"), None)
    for ln in lines:
        for i in range(len(ln) - 1):
            a, b = ln[i], ln[i + 1]
            mid = QgsPointXY((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
            d = q.distance(QgsGeometry.fromPointXY(mid))
            if d < best[0]:
                dx, dy = (b.x() - a.x()), (b.y() - a.y())
                L = math.hypot(dx, dy)
                if L > 0:
                    best = (d, (dx / L, dy / L))
    return best[1]

def _make_tangent_trench(center: QgsPointXY, direction, radius_m: float, length_m: float) -> QgsGeometry:
    """Perpendicular line through the circle edge at 'center'."""
    edge_x = center.x() + direction[0] * radius_m
    edge_y = center.y() + direction[1] * radius_m
    # perpendicular to road direction
    dxp, dyp = -direction[1], direction[0]
    half = length_m / 2.0
    p1 = QgsPointXY(edge_x - dxp * half, edge_y - dyp * half)
    p2 = QgsPointXY(edge_x + dxp * half, edge_y + dyp * half)
    return QgsGeometry.fromPolylineXY([p1, p2])


# ------------------------------------------------------------
# Schema safety: GPKG (SQLite/OGR) treats column names
# case-INSENSITIVELY for uniqueness, so ``pdp_id`` and ``PDP_ID``
# collide on layer creation, producing
#   "Cannot create field PDP_ID. A field with the same name
#    already exists."
# This helper de-duplicates field names and returns an alias_map so
# feature writes can still target the canonical column.
# ------------------------------------------------------------

def _safe_sink_fields(input_fields):
    """
    De-duplicate field names case-insensitively for GPKG sinks.
    On collision the LATER occurrence is renamed with a ``_NEW`` suffix.
    Returns:
        (cleaned_qgs_fields, alias_map)
    where ``alias_map`` maps original-name -> final-name.
    """
    out = []
    taken_lower = set()
    alias = {}
    for f in input_fields:
        nm = f.name()
        if nm.lower() in taken_lower:
            cand = f"{nm}_NEW"
            i = 2
            while cand.lower() in taken_lower:
                cand = f"{nm}_NEW_v{i}"
                i += 1
            out.append(QgsField(cand, f.type()))
            taken_lower.add(cand.lower())
            alias[nm] = cand
        else:
            out.append(f)
            taken_lower.add(nm.lower())
            alias[nm] = nm
    return out, alias


# ------------------------------------------------------------
# Algorithm
# ------------------------------------------------------------

class TrenchLayerAlgorithm(QgsProcessingAlgorithm):
    # Inputs
    P_POLY = "INPUT_POLY"
    P_ROADS = "INPUT_ROADS"
    P_PDP  = "INPUT_PDP"
    P_HH   = "INPUT_HOUSEHOLDS"     # optional
    P_BLDG = "INPUT_BUILDINGS"      # optional (mask)
    P_MFG  = "INPUT_MFG"            # existing MFG

    # Field params
    P_PDP_ID = "PDP_ID_FIELD"
    P_HH_ID  = "HH_ID_FIELD"
    P_HH_PDP = "HH_PDP_FIELD"       # Households: PDP ID field (optional, for strict Distribution)
    P_HH_HHS = "HH_HHS_FIELD"       # optional: household size/count

    # Tunables / numeric controls
    P_DENSIFY     = "DENSIFY"
    P_HEAL        = "HEAL_TOL"
    P_SNAP        = "SNAP_TOL"
    P_INT_BUFFER  = "INTERSECTION_R"
    P_FINAL_SNAP  = "FINAL_SNAP_TOL"
    P_CROSS_STEP  = "CROSS_STEP"
    P_CROSS_LEN   = "CROSS_LEN"
    P_PDP_SEARCH  = "PDP_SEARCH_RADIUS_M"
    P_MIN_DEGREE  = "MIN_NODE_DEGREE"
    P_GARDEN_K    = "GARDEN_K_NEIGH"
    P_INTERSECT_DISTINCT_TOL = "INTERSECT_DISTINCT_TOL_M"
    P_AOI_BUFFER_M = "AOI_BUFFER_M"

    # Flags / expressions
    P_PREFER_INTERSECTION = "PREFER_INTERSECTION"
    P_ROADS_FILTER_EXPR   = "ROADS_FILTER_EXPR"

    # Sidewalk/offset params
    P_SIDE_OFFSET   = "SIDEWALK_OFFSET_M"
    P_SIDE_WIDTH    = "SIDEWALK_BUFFER_W_M"
    P_SIDE_JOIN     = "SIDEWALK_JOIN_STYLE"
    P_SIDE_MITER    = "SIDEWALK_MITER_LIMIT"
    P_SIDE_SEGMENTS = "SIDEWALK_SEGMENTS"

    # Outputs (finals + intermediates)
    O_SIDE_L = "OUT_SIDEWALK_LEFT"
    O_SIDE_R = "OUT_SIDEWALK_RIGHT"
    O_SIDE_MERGED = "OUT_SIDEWALK_MERGED"
    O_SIDE_BUF_L = "OUT_SIDEWALK_BUFFERED_LEFT"
    O_SIDE_BUF_R = "OUT_SIDEWALK_BUFFERED_RIGHT"

    O_PDP_PROJ = "OUT_PDP_TO_SIDE"
    O_PSEUDO_PDP = "OUT_PSEUDO_PDP"
    O_MERGED_PDP = "OUT_MERGED_PDP"
    O_MFG = "OUT_MFG_POINT"
    O_INTER_BUFF = "OUT_VALID_INTERSECTIONS"
    O_TANGENTS = "OUT_TANGENT_TRENCHES"
    O_TANGENTS_USED = "OUT_TANGENT_TRENCHES_USED"
    O_MFG_PDP = "OUT_TRENCHES_MFG_TO_PDP"
    O_FEEDER = "OUT_FEEDER_TRENCH"
    O_GARDEN = "OUT_GARDEN_TRENCHES"
    O_PSEUDO_HH = "OUT_PSEUDO_HH"
    O_DIST_LINES = "OUT_DISTRIBUTION_LINES"
    O_DIST_DISS = "OUT_DISTRIBUTION_DISS"
    O_FINAL = "OUT_FINAL_TRENCHES"
    O_FEEDER_FINAL = "OUT_FEEDER_FINAL"
    O_FINAL_TAN = "OUT_FINAL_TANGENT_TRENCHES"
    # --- Stage 1 outputs (intermediates) ---
    O_S1_AOI_BUF_DISS   = "OUT_S1_AOI_BUFFER_DISSOLVED"
    O_S1_AOI_OUTLINE    = "OUT_S1_AOI_OUTLINE_LINES"
    O_S1_ROADS_NEAR     = "OUT_S1_ROADS_NEAR"
    O_S1_ROADS_FILTERED = "OUT_S1_ROADS_FILTERED"   # optional (only if expr provided)

    def name(self): return "04_trench_layer"
    def displayName(self): return _tr("Generate Trenches")
    def group(self): return _tr("04 Trench Layer")
    def groupId(self): return "04_trench_layer"
    def createInstance(self): return TrenchLayerAlgorithm()
    def shortHelpString(self):
        return _tr(
            "Build network from OSM footways/paths/service; cross vehicular roads with perpendicular drills; "
            "route Feeder/Distribution on that graph. Uses selected MFG. Safe erase/difference fallback."
        )

    def initAlgorithm(self, config=None):
        # ---------- Stage 1: AOI + roads filter ----------
        self.P_AOI_BUF   = "AOI_BUFFER_M"
        self.P_ROAD_EXPR = "ROADS_FILTER_EXPR"

        # ---------- Stage 2: sidewalks / offsets ----------
        self.P_SW_OFFSET = "SIDEWALK_OFFSET_M"
        self.P_SW_BUF_W  = "SIDEWALK_BUFFER_W_M"
        self.P_SW_SEG    = "SIDEWALK_SEGMENTS"
        self.P_SW_JOIN   = "SIDEWALK_JOIN_STYLE"   # 0=Round, 1=Miter, 2=Bevel
        self.P_SW_MITER  = "SIDEWALK_MITER_LIMIT"

        # Optional Stage 2 outputs (debug/inspection)
        self.O_SIDE_BUF_L  = "OUT_SIDEWALK_BUFFERED_LEFT"
        self.O_SIDE_BUF_R  = "OUT_SIDEWALK_BUFFERED_RIGHT"
        self.O_SIDE_MERGED = "OUT_SIDEWALK_MERGED"

        # ---------- Other tunables ----------
        self.P_GARDEN_K  = "GARDEN_K_NEIGH"
        self.P_HH_HHS    = "HH_HHS_FIELD"          # households field (optional)
        self.P_PREF_INT  = "PREFER_INTERSECTION"   # prefer exact intersection snaps
        self.P_PDP_SRCH  = "PDP_SEARCH_RADIUS_M"
        self.P_INT_CLUSTER = "INTERSECT_DISTINCT_TOL_M"
        self.P_NODE_DEG    = "MIN_NODE_DEGREE"
        self.P_HH_ID       = "HH_ID_FIELD"

        # ---------- Inputs ----------
        

        self.addParameter(QgsProcessingParameterVectorLayer(self.P_POLY,  _tr("Polygons / AOI"), [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_ROADS, _tr("Roads (OSM lines; includes footways)"), [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_PDP,   _tr("PDPs (points; PDP_ID auto-detected)"), [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_HH, _tr("Households / Objects [optional] (ADDR_ID / PDP_ID auto-detected)"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BLDG, _tr("Buildings (optional) — trim inside"),
            [QgsProcessing.TypeVectorPolygon], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_MFG, _tr("MFG (existing point layer) [optional]"),
            [QgsProcessing.TypeVectorPoint],
            optional=True
        ))
        # Parameter surface slimmed 2026-07-03: the graph/sidewalk/drill tunables,
        # roads filter expression, field pickers (auto-detected instead), the dead
        # OUT_GPKG destination and the never-read INPUT_FOOTWAY_SERVICE /
        # HH_HHS_FIELD inputs were removed — fixed defaults live in DEFAULTS below.

        # ---------- Feature sinks (created dynamically in processAlgorithm) ----------
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_MERGED_PDP,   _tr("Merged Trenches per PDP (multi-lines)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_FEEDER_FINAL, _tr("Feeder_Trench (multi-lines, id only)")))

        self.addParameter(QgsProcessingParameterFeatureSink(self.O_SIDE_MERGED, _tr("Merged Sidewalks (buffered polygons)"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_SIDE_BUF_L,  _tr("Buffered Left (debug)"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_SIDE_BUF_R,  _tr("Buffered Right (debug)"), optional=True))

        self.addParameter(QgsProcessingParameterFeatureSink(self.O_SIDE_L, _tr("Sidewalk Left (lines)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_SIDE_R, _tr("Sidewalk Right (lines)")))

        self.addParameter(QgsProcessingParameterFeatureSink(self.O_PDP_PROJ,    _tr("PDP→Footway/Service Projections")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_PSEUDO_PDP,  _tr("Pseudo PDP Points")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_MFG,         _tr("MFG Point (pass-through)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_INTER_BUFF,  _tr("Valid Intersection Buffers")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_TANGENTS,    _tr("Drill Crossings (designed)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_TANGENTS_USED, _tr("Drill Crossings (used)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_MFG_PDP,     _tr("Trenches: MFG→PDP")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_FEEDER,      _tr("Feeder Trench (merged)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_GARDEN,      _tr("Garden Trenches (HH→Footway/Service)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_PSEUDO_HH,   _tr("Pseudo HH Points on Footway/Service")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_DIST_LINES,  _tr("Distribution Lines (MFG→Pseudo HH)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_DIST_DISS,   _tr("Distribution Dissolved")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_FINAL_TAN, _tr("Final Tangent Trenches"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_FINAL,       _tr("Final Trenches (merged + tagged)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_S1_AOI_BUF_DISS, _tr("Buffered polygon (dissolved)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_S1_AOI_OUTLINE,  _tr("Buffered outline (lines)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_S1_ROADS_NEAR,   _tr("Roads near polygon (within buffer)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_S1_ROADS_FILTERED, _tr("Filtered roads (by expression)"), optional=True))


    def segmentize_network(self, all_layers, context, feedback):
        """
        Thin wrapper to the shared segmentizer.
        """
        return segmentize_merged_lines(all_layers, context, feedback, crs_fallback="EPSG:25833", tol=0.02)


    # ------------------------------------------------------------
    # Main
    # ------------------------------------------------------------
    def processAlgorithm(self, p, context: QgsProcessingContext, feedback: QgsProcessingFeedback):
        if nx is None:
            raise QgsProcessingException(
                "networkx is required for the Trench Layer. "
                "Install networkx in the QGIS Python environment."
            )

        idTan = None


        def _empty_layer(geom: str, crs) -> QgsVectorLayer:
            # geom: "Point", "LineString", "Polygon" or Multi* variants
            return QgsVectorLayer(f"{geom}?crs={crs.authid()}", "empty", "memory")

        # === tolerant layer fetch (must be defined BEFORE first use) ===
        def _param_vec(name: str, hint: str, required: bool = True) -> Optional[QgsVectorLayer]:
            """
            Prefer parameterAsVectorLayer (Processing-native), then fall back to utils.layer_io.as_layer
            for strings/URIs/paths. Returns QgsVectorLayer or None (if required=False).
            """
            lyr = self.parameterAsVectorLayer(p, name, context)
            if lyr and lyr.isValid():
                return lyr

            raw = p.get(name)
            if raw is not None:
                try:
                    lyr = as_layer(raw, context=context, hint=hint)
                    if lyr and lyr.isValid():
                        return lyr
                except Exception:
                    pass

            if required:
                raise QgsProcessingException(f"Required {hint} layer '{name}' is missing or invalid.")
            return None

        # ---------- Inputs (robust against strings/URIs/paths) ----------
        polys  = _param_vec(self.P_POLY,  "polygons",   required=True)
        roads  = _param_vec(self.P_ROADS, "roads",      required=True)
        bldg   = _param_vec(self.P_BLDG,  "buildings",  required=False)
        pdps   = _param_vec(self.P_PDP,   "PDP",        required=True)
        # MFG and HH can be optional in some runs — make them tolerant
        mfg_in = _param_vec(self.P_MFG,   "MFG",        required=False)
        hh     = _param_vec(self.P_HH,    "households", required=False)

        # ---------- Reproject everything to target CRS ----------
        target_auth = "EPSG:25833"
        polys  = reproject_to(polys,  target_auth, context, feedback)
        roads  = reproject_to(roads,  target_auth, context, feedback)
        if bldg: bldg = reproject_to(bldg,   target_auth, context, feedback)
        pdps   = reproject_to(pdps,   target_auth, context, feedback)
        if mfg_in: mfg_in = reproject_to(mfg_in, target_auth, context, feedback)
        if hh:     hh     = reproject_to(hh,     target_auth, context, feedback)

        # add this:
        polys = as_layer(polys, context=context, hint="polygons")
        roads = as_layer(roads, context=context, hint="roads")
        if bldg: bldg = as_layer(bldg,  context=context, hint="buildings")
        pdps  = as_layer(pdps,  context=context, hint="PDP")
        if mfg_in: mfg_in = as_layer(mfg_in, context=context, hint="MFG")
        if hh:     hh     = as_layer(hh,     context=context, hint="households")
        
        # ---------- Spatial index on roads (best-effort) ----------
        try:
            res = processing.run(
                "native:createspatialindex",
                {"INPUT": roads},
                is_child_algorithm=True, context=context, feedback=feedback
            )
            roads_idx = res.get("OUTPUT") or res.get("INPUT") or roads
            roads = as_layer(roads_idx, context=context, hint="roads_indexed")
        except Exception:
            pass  # continue without index if provider doesn't support it

        # ---------- Re-validate after reprojection / indexing ----------
        for nm, lyr in (("polygons", polys), ("roads", roads), ("PDP", pdps)):
            if not lyr or not lyr.isValid():
                raise QgsProcessingException(f"{nm} layer became invalid after reprojection/indexing.")
        if bldg and not bldg.isValid():
            bldg = None  # treat as absent
        if mfg_in and not mfg_in.isValid():
            mfg_in = None  # treat as absent
        if hh and not hh.isValid():
            hh = None      # treat as absent

        # ---------- Fixed numeric & text defaults (parameter surface slimmed) ----------
        aoi_buf_dist  = 100.0  # AOI buffer distance (m) for pre-clipping roads
        # Roads filter (formerly ROADS_FILTER_EXPR parameter default)
        road_expr_raw = (
            "("
            "\"fclass\" IN ('residential','unclassified','living_street','service',"
            "              'tertiary','tertiary_link','secondary','secondary_link',"
            "              'primary','primary_link','trunk','trunk_link')"
            " OR (\"fclass\"='pedestrian' AND COALESCE(\"area\",'F')<>'T')"
            ") "
            "AND \"fclass\" NOT IN ('motorway','motorway_link','track','path','footway','steps') "
            "AND COALESCE(\"bridge\",'F') <> 'T' AND COALESCE(\"tunnel\",'F') <> 'T'"
        )

        # Normalize the field name to whatever the input roads actually have
        road_expr = swap_canonical_field(
            road_expr_raw, roads, canonical="fclass", candidates=("fclass", "highway", "class")
        )

        sw_off   = 3.0   # sidewalk offset distance (m), clamp kept below
        sw_off   = max(0.5, min(sw_off, 12.0))  # keep realistic offsets
        sw_buf_w = 0.5   # sidewalk buffer width (m)
        sw_seg   = 8     # offset segments (curve smoothing)
        sw_join  = 1     # offset join style (miter)
        sw_miter = 2.0   # offset miter limit

        prefer_intersection = True   # prefer exact sidewalk intersection snaps
        garden_k            = 12     # k nearest sidewalk candidates per side
        pdp_search_r        = 200.0  # PDP→sidewalk search radius (m)
        tol_cluster         = 0.5    # distinct/cluster tolerance for intersections (m)
        degmin              = 3      # minimum road connections at node

        # ---------- Optional field selections (with auto-detect) ----------
        def _pick(layer: Optional[QgsVectorLayer], given: Optional[str], candidates: list[str]) -> Optional[str]:
            # if user chose a field in the UI and it exists, keep it; else auto-detect by aliases
            if layer and given and given in layer.fields().names():
                return given
            return _first_field_case_insensitive(layer, candidates) if layer else None

        # Field pickers removed from the UI — always auto-detect from the
        # canonical candidate lists below (value-validated further down).
        pdp_id_field_in = None
        hh_id_field_in = None
        hh_pdp_field_in = None

        # PDP id on PDP layer (accept common aliases)
        pdp_id_field = _pick(
            pdps, pdp_id_field_in,
            ["pdp_id", "PDP_ID", "PDP", "pdp", "pdp_pol_id", "PDP_POL_ID", "pDp_POL_ID"]
        )
        # HH id on HH layer (addr-like fields first — 'HH' is a household COUNT,
        # not an identifier, so it is deliberately not auto-detected)
        hh_id_field = _pick(
            hh, hh_id_field_in,
            ["addr_id", "ADDR_ID", "address_id", "ADDRESS_ID", "hh_id", "HH_ID", "name", "NAME", "id", "ID"]
        )
        # HH→PDP linkage on HH layer
        hh_pdp_field = _pick(
            hh, hh_pdp_field_in,
            ["pdp_id", "PDP_ID", "PDP", "pdp", "pdp_pol_id", "PDP_POL_ID", "pDp_POL_ID"]
        )

        # ---------- Validate user field choices by their VALUES ----------
        # A field can exist and still be the wrong one (e.g. 'HH' = household
        # count, or 'pDp_POL_ID' = source group ids that don't match PDP_ID).
        # These mistakes silently produce 0 distribution paths, so detect them.
        def _field_vals(layer, fld, limit=10000):
            out = set()
            if not (layer and fld):
                return out
            for _i, _f in enumerate(layer.getFeatures()):
                if _i >= limit:
                    break
                _v = _f[fld]
                if _v is not None:
                    _s = str(_v).strip()
                    if _s and _s.upper() != "NULL":
                        out.add(_s)
            return out

        _COUNT_LIKE = ("hh", "hhs", "household", "household_s", "households", "sum_homes", "sum_object")
        if hh is not None and hh_id_field and hh_id_field.strip().lower() in _COUNT_LIKE:
            _better = _first_field_case_insensitive(hh, ["ADDR_ID", "address_id", "HH_ID", "id"])
            if _better and _better.lower() != hh_id_field.lower():
                feedback.pushWarning(
                    f"⚠️ 'HH/Address ID' field '{hh_id_field}' looks like a household COUNT, "
                    f"not an identifier — using '{_better}' instead."
                )
                hh_id_field = _better

        if hh is not None and pdps is not None and pdp_id_field and hh_pdp_field:
            _pdp_ids = _field_vals(pdps, pdp_id_field)
            if _pdp_ids and not (_field_vals(hh, hh_pdp_field) & _pdp_ids):
                # Chosen linkage field shares no values with the PDP ids.
                # Scan the HH layer for the field with the best value overlap.
                _best_f, _best_n = None, 0
                for _fld in hh.fields().names():
                    _n = len(_field_vals(hh, _fld) & _pdp_ids)
                    if _n > _best_n:
                        _best_f, _best_n = _fld, _n
                if _best_f:
                    feedback.pushWarning(
                        f"⚠️ 'Households: PDP ID field' = '{hh_pdp_field}' has no values matching the "
                        f"PDP layer's '{pdp_id_field}' ids — using '{_best_f}' instead "
                        f"({_best_n} matching values)."
                    )
                    hh_pdp_field = _best_f
                else:
                    _hh_sample = next(iter(_field_vals(hh, hh_pdp_field)), "<empty>")
                    _pdp_sample = next(iter(_pdp_ids))
                    feedback.reportError(
                        f"❌ No field on the Households layer links to the PDPs: "
                        f"'{hh_pdp_field}' holds values like '{_hh_sample}' but PDP '{pdp_id_field}' holds "
                        f"'{_pdp_sample}'. Distribution will be empty. Run '03 Build Network Layer' first so "
                        f"the Objects layer gets PDP_ID written back, then select that field."
                    )

        # ---------- Tunables (fixed defaults) ----------
        dens       = 2.0   # densify interval (m)
        heal       = 3.0   # micro-heal on footway (m)
        snap       = 25.0  # snap tolerance (m)
        # Head-room for disconnected / fragmented OSM line work +
        # the requested +50 m tolerance bump. Floor at 75 m so even
        # a 25 m default gets enough slack to snap MFG / pseudo-PDPs.
        snap       = max(snap + 50.0, 75.0)
        feedback.pushInfo(f"Effective snap tolerance (with head-room): {snap:g} m")
        ibufr      = 20.0  # road-intersection buffer (m)
        final_snap = 0.20  # final snap (m)
        cross_step = 25.0  # mid-block drill spacing (m)
        cross_len  = 60.0  # mid-block drill total length (m)

        # Small epsilon used in later graph steps (keep tiny but > 0)
        eps = min(0.05, max(0.001, dens / 5.0))

        # ---------- Output GPKG (parameter removed; only the dead _save_to_gpkg
        # helper below ever referenced it — kept pointing at a temp path) ----------
        out_gpkg     = _normalize_gpkg_path("", context)

        # ---------- Helper: persist a layer/table into GPKG (creates placeholders when empty/missing) ----------
        def _save_to_gpkg(source, layer_name: str):
            # sanitized table name
            safe = re.sub(r"[^A-Za-z0-9_]+", "_", layer_name).strip("_") or "layer"

            # Decide a sensible geometry type for known trench layers (fallback = LineString)
            geom_map = {
                "Final_Tangent_Trenches": "LineString",
                "Tangent_Used":           "LineString",
                "Tangent_All":            "LineString",
                "OUT_VALID_INTERSECTIONS":"Polygon",     # if you use this exact name elsewhere
                "Intersection_Buffers":   "Polygon",
                "Feeder_Trench":          "MultiLineString",
                "Distribution_Trench":    "MultiLineString",
                "Feeder_Trench_Final":    "MultiLineString",
                "Final_Trenches":         "MultiLineString",
                "Sidewalk_Left":          "LineString",
                "Sidewalk_Right":         "LineString",
                "PDP_to_Sidewalk":        "LineString",
                "Merged_PDP_Trenches":    "MultiLineString",
                "Pseudo_PDP":             "Point",
                "Pseudo_HH":              "Point",
                "MFG_Point":              "Point",
                "MFG_to_PDP":             "LineString",
                "AOI_Buffer":             "Polygon",
                "AOI_Outline":            "LineString",
                "Roads_Near":             "MultiLineString",
                "Roads_Filtered":         "MultiLineString",
            }
            geom_type = geom_map.get(layer_name, "LineString")

            # Preferred CRS: explicit OUT_CRS param, else project CRS
            try:
                out_crs = self.parameterAsCrs(p, "OUT_CRS", context)
            except Exception:
                out_crs = QgsProject.instance().crs()

            # If we have a source, try to materialize it
            lyr = None
            if source:
                lyr = _materialize_layer(source, context, feedback, hint=f"tmp_{layer_name}")

            # If invalid or empty, create an EMPTY placeholder layer in the same GPKG
            needs_placeholder = (not lyr) or (not lyr.isValid())
            if not needs_placeholder:
                try:
                    _ = lyr.featureCount()  # count can raise on some virtual layers
                except Exception:
                    needs_placeholder = True

            if needs_placeholder or (lyr and lyr.featureCount() == 0):
                try:
                    processing.run(
                        "native:createvectorlayer",
                        {
                            "EXTENT": None,
                            "CRS": out_crs,
                            "GEOMETRY": geom_type,
                            "FIELDS": [],
                            "FILE_NAME": out_gpkg,
                            "LAYER_NAME": safe,
                            "OUTPUT": f"{out_gpkg}|layername={safe}",
                        },
                        is_child_algorithm=True, context=context, feedback=feedback
                    )
                    feedback.pushInfo(f"Created empty '{safe}' layer (no features).")
                    return
                except Exception as e:
                    feedback.reportError(f"❌ Could not create empty '{safe}' layer: {e}")
                    return

            # Otherwise write the actual features to GPKG
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = safe
            opts.fileEncoding = "UTF-8"
            opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
            opts.layerOptions = ["SPATIAL_INDEX=YES"]

            err, _ = QgsVectorFileWriter.writeAsVectorFormatV2(
                lyr, out_gpkg, QgsCoordinateTransformContext(), opts
            )
            if err != QgsVectorFileWriter.NoError:
                feedback.reportError(f"❌ Failed to save '{safe}' to GeoPackage (error code {err}).")

        

        # ---------- Building buffers (defensive) ----------

        avoid_m = 0.30 if bldg else 0.0  # keep trenches away from buildings by (m)
        bldg_bufL = None
        if bldg and bldg.featureCount() and avoid_m > 0:
            try:
                bldg_bufL = as_layer(
                    processing.run(
                        "native:buffer",
                        {
                            "INPUT": bldg,
                            "DISTANCE": avoid_m,
                            "SEGMENTS": 8,
                            "END_CAP_STYLE": 0,
                            "JOIN_STYLE": 1,
                            "MITER_LIMIT": 2.0,
                            "DISSOLVE": True,
                            "SEPARATE_DISJOINT": False,
                            "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT
                        },
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"],
                    context=context, hint="bldg_bufL"
                )
            except Exception:
                bldg_bufL = None  # keep pipeline robust



        # ------------------------------
        # Stage 1: AOI buffer & roads pre-filter (match UI)
        # ------------------------------

        # 1A) Buffer polygons and dissolve
        aoi_buf = as_layer(processing.run(
            "native:buffer",
            {
                "INPUT": polys, "DISTANCE": max(0.0, aoi_buf_dist), "SEGMENTS": 16,
                "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2.0,
                "DISSOLVE": False, "SEPARATE_DISJOINT": False,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT
            },
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context, "aoi_buffer_raw")

        aoi_buf_dis = as_layer(processing.run(
            "native:dissolve",
            {"INPUT": aoi_buf, "FIELD": [], "SEPARATE_DISJOINT": False, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context, "Buffered polygon (dissolved)")

        # 1B) Outline (lines)
        aoi_outline = as_layer(processing.run(
            "native:polygonstolines",
            {"INPUT": aoi_buf_dis, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context, "Buffered outline (lines)")

        # 1C) Keep only roads within buffer
        roads_near = as_layer(processing.run(
            "native:extractbylocation",
            {"INPUT": roads, "PREDICATE": [0], "INTERSECT": aoi_buf_dis, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context, "Roads near polygon (within buffer)")

        # 1D) Optional attribute filter expression (auto-swap fclass->actual field)
        # ---- insert begin: sanitize road filter expr ----
        def _sanitize_expr(expr: str) -> str:
            if not expr:
                return ""
            expr = expr.strip()
            # NOTE: keep canonical "fclass" references intact — swap_canonical_field()
            # below maps them to whatever class field actually exists on the layer
            # (fclass for Geofabrik shapefiles, highway for raw OSM extracts).
            # Balance parentheses: if more closing than opening, trim extras at the end
            opens = expr.count('(')
            closes = expr.count(')')
            if closes > opens:
                trim = closes - opens
                while trim and expr.endswith(')'):
                    expr = expr[:-1]
                    trim -= 1
            # Remove dangling boolean operators (AND/OR) at the end
            expr = re.sub(r'\s+(AND|OR)\s*$', '', expr, flags=re.IGNORECASE)
            return expr
        # ---- insert end ----

        roads_work = roads_near
        idS1_roads_fl = None

        # sanitize and validate raw input
        road_expr_raw = _sanitize_expr(road_expr_raw)
        if road_expr_raw:
            expr_check_raw = QgsExpression(road_expr_raw)
            if expr_check_raw.hasParserError():
                feedback.pushWarning(
                    f"Roads filter expression ignored (parse error: {expr_check_raw.parserErrorString()}). "
                    "Using unfiltered near-AOI roads."
                )
                road_expr_raw = ""

        # Only proceed if there is a usable expression
        if road_expr_raw:
            # Map canonical field name to whatever exists on this roads layer
            road_expr = swap_canonical_field(
                road_expr_raw, roads_near, "fclass", ("fclass", "highway", "class")
            )

            expr_check = QgsExpression(road_expr)
            if expr_check.hasParserError():
                feedback.pushWarning(
                    f"Roads filter expression ignored (parse error: {expr_check.parserErrorString()}). "
                    "Using unfiltered near-AOI roads."
                )
            else:
                try:
                    filtered = processing.run(
                        "native:extractbyexpression",
                        {
                            "INPUT": roads_near,
                            "EXPRESSION": road_expr,
                            "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
                        },
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"]

                    roads_work = as_layer(filtered, context, "Filtered roads (by expression)")

                    # Guard: a filter matching nothing (e.g. wrong field name) would
                    # silently empty the whole trench graph — fall back instead.
                    if (roads_work is None or roads_work.featureCount() == 0) and roads_near.featureCount() > 0:
                        feedback.pushWarning(
                            "Roads filter matched 0 of "
                            f"{roads_near.featureCount()} near-AOI roads; check the expression/field names. "
                            "Falling back to unfiltered roads."
                        )
                        roads_work = roads_near

                    # Persist the filtered roads as an optional artifact
                    idS1_roads_fl = copy_to_sink(
                        self, p, context, roads_work, self.O_S1_ROADS_FILTERED,
                        "Filtered roads (by expression)", feedback
                    )
                except Exception as e:
                    feedback.reportError(f"Roads filter expression ignored: {e} — double-check parentheses and field names.")
                    feedback.pushWarning("Falling back to unfiltered roads for trench building.")
                    roads_work = roads_near


        # Continue the pipeline with the pre-filtered roads
        roads = roads_work


        idS1_buf_diss = copy_to_sink(self, p, context, aoi_buf_dis, self.O_S1_AOI_BUF_DISS, "Buffered polygon (dissolved)", feedback)
        idS1_outline  = copy_to_sink(self, p, context, aoi_outline,  self.O_S1_AOI_OUTLINE,  "Buffered outline (lines)", feedback)
        idS1_roads_nr = copy_to_sink(self, p, context, roads_near,   self.O_S1_ROADS_NEAR,   "Roads near polygon (within buffer)", feedback)
        idS1_roads_fl = None
        if road_expr_raw:
            idS1_roads_fl = copy_to_sink(self, p, context, roads_work, self.O_S1_ROADS_FILTERED, "Filtered roads (by expression)", feedback)


        # --------------------------------------------------------
        # Roads split: FOOTWAYS / SERVICE / VEHICULAR (robust)
        # --------------------------------------------------------
        fieldname = _first_field_case_insensitive(roads, ["fclass", "highway", "class"])

        if not fieldname:
            feedback.pushWarning(
                "Road class field not found (expected fclass/highway/class). "
                "Auto-promoting all lines to WALKABLE (footway) to keep trench routing alive; "
                "verify your input carries OSM tags."
            )
            footways = roads         # all roads treated as walkable
            service  = roads.clone(); service.dataProvider().truncate()
            veh_roads = roads.clone(); veh_roads.dataProvider().truncate()
        else:
            footway_codes = ('footway','path','pedestrian','cycleway','steps','bridleway','sidewalk')
            footway_expr  = expr_ci_in(fieldname, footway_codes)
            service_expr  = expr_ci_in(fieldname, ('service',))

            # exclude polygonal footway features if an 'area' field exists
            not_area_poly = expr_area_not_true(roads)
            if not_area_poly:
                footway_expr = f"({footway_expr}) AND ({not_area_poly})"

            footways = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": roads, "EXPRESSION": footway_expr, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context, "footways")

            service = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": roads, "EXPRESSION": service_expr, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context, "service_roads")

            not_foot_service = f"NOT ({footway_expr}) AND NOT ({service_expr})"
            veh_roads = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": roads, "EXPRESSION": not_foot_service, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context, "vehicular_roads")

        feedback.pushInfo(
            "Roads split: footways={}, service={}, vehicular={}".format(
                footways.featureCount() if footways else 0,
                service.featureCount() if service else 0,
                veh_roads.featureCount() if veh_roads else 0,
            )
        )

        # --------------------------------------------------------
        # Intersection buffers (for later erase of footways) — NO drills yet
        # --------------------------------------------------------

        idIB = None
        centers = []
        ibuf_mem = None
        veh_roads_exploded = None

        if veh_roads and veh_roads.featureCount() > 0:
            ibuf_mem, centers, veh_roads_exploded = build_intersection_buffers(
                veh_roads, tol_cluster=tol_cluster, ibufr=ibufr, context=context, feedback=feedback
            )
            # allocate sink from memory layer (for UI / outputs)
            if ibuf_mem:
                idIB = copy_to_sink(self, p, context, ibuf_mem, self.O_INTER_BUFF, "Intersection buffers", feedback)
        else:
            # Fallback: attempt intersections on broader walkable network
            try:
                # Prefer the same base used for sidewalks; if not yet defined, use 'roads'
                _fallback = locals().get("base_for_sidewalks") or locals().get("walkable") or roads
                if _fallback and _fallback.featureCount() > 0:
                    ibuf_mem, centers, veh_roads_exploded = build_intersection_buffers(
                        _fallback, tol_cluster=tol_cluster, ibufr=ibufr, context=context, feedback=feedback
                    )
                    if ibuf_mem:
                        idIB = copy_to_sink(self, p, context, ibuf_mem, self.O_INTER_BUFF, "Intersection buffers", feedback)
                # If still empty after fallback, allocate a schema-valid empty sink
                if not idIB:
                    empty_fields = QgsFields(); empty_fields.append(QgsField("id", QVariant.Int))
                    _, idIB = self.parameterAsSink(p, self.O_INTER_BUFF, context, empty_fields, QgsWkbTypes.Polygon, roads.crs())
            except Exception:
                # Last-resort: create a schema-valid empty sink to keep downstream safe
                empty_fields = QgsFields(); empty_fields.append(QgsField("id", QVariant.Int))
                _, idIB = self.parameterAsSink(p, self.O_INTER_BUFF, context, empty_fields, QgsWkbTypes.Polygon, roads.crs())


        # --------------------------------------------------------
        # Clean footways: erase inside intersection buffers (avoid mid-road sneaks)
        # --------------------------------------------------------
        # Prefer in-memory layer when available, else fall back to the sink layer.
        footways_clean = footways

        ibuf_source_for_dissolve = None
        if ibuf_mem:
            ibuf_source_for_dissolve = ibuf_mem
        elif idIB:
            try:
                ibuf_source_for_dissolve = as_layer(idIB, context, "Intersection buffers")
            except Exception:
                ibuf_source_for_dissolve = None

        if ibuf_source_for_dissolve and footways and footways.featureCount():
            try:
                ibuf_union2 = as_layer(processing.run(
                    "native:dissolve",
                    {"INPUT": ibuf_source_for_dissolve, "FIELD": [],
                     "SEPARATE_DISJOINT": False, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                    is_child_algorithm=True, context=context, feedback=feedback
                )["OUTPUT"], context, "ibuf_union")

                if ibuf_union2:
                    footways_clean = _erase_or_difference(
                        footways, ibuf_union2, context, feedback, "footways_clean"
                    )
            except Exception:
                # best-effort; keep original footways if dissolve/erase fails
                pass

        # NOTE: Do NOT build drills here; Stage-2 will create sidewalks (left/right),
        # then we will design drill lines (perpendiculars) using those sidewalks as net_hits.

        # Build footway expression dynamically, excluding polygonal 'area'='T' if present
        # We already produced `footways_clean` above; merge that with roads
        walkable = as_layer(processing.run(
            "native:mergevectorlayers",
            {"LAYERS": [roads, footways_clean], "CRS": roads.crs(), "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context, "roads_with_footways")

        # ------------------------------------------------------------
        # Stage 2: Generate sidewalks by offsetting filtered roads (± sw_off)
        # ------------------------------------------------------------

        # Use vehicular roads for sidewalks when available (fallback = all roads)
        base_for_sidewalks = veh_roads if (veh_roads and veh_roads.featureCount() > 0) else roads

        # 0) Fix + dissolve (offsets behave better on dissolved, valid lines)
        roads_fixed = as_layer(
            processing.run(
                "native:fixgeometries",
                {"INPUT": base_for_sidewalks, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "roads_fixed"
        )

        # Keep as a *layer* (don’t materialize to features here) so later
        # algs can use the provider’s spatial index efficiently.
        roads_fixed = as_layer(
            processing.run(
                "native:createspatialindex",
                {"INPUT": roads_fixed},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "roads_fixed_idx"
        )

        roads_diss = as_layer(
            processing.run(
                "native:dissolve",
                {"INPUT": roads_fixed, "FIELD": [], "SEPARATE_DISJOINT": False,
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "roads_diss"
        )

        # 1) Offset to both sides (convention: left = negative, right = positive)
        left_sw = as_layer(
            processing.run(
                "native:offsetline",
                {"INPUT": roads_diss, "DISTANCE": -sw_off, "SEGMENTS": sw_seg,
                 "JOIN_STYLE": sw_join, "MITER_LIMIT": sw_miter,
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "sidewalk_left"
        )

        right_sw = as_layer(
            processing.run(
                "native:offsetline",
                {"INPUT": roads_diss, "DISTANCE":  sw_off, "SEGMENTS": sw_seg,
                 "JOIN_STYLE": sw_join, "MITER_LIMIT": sw_miter,
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "sidewalk_right"
        )

        # 2) Small buffers around each sidewalk (used later for intersections & trims)
        bufL = as_layer(
            processing.run(
                "native:buffer",
                {"INPUT": left_sw, "DISTANCE": sw_buf_w, "SEGMENTS": 5,
                 "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2.0,
                 "DISSOLVE": False, "SEPARATE_DISJOINT": False,
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "buffered_left"
        )

        bufR = as_layer(
            processing.run(
                "native:buffer",
                {"INPUT": right_sw, "DISTANCE": sw_buf_w, "SEGMENTS": 5,
                 "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2.0,
                 "DISSOLVE": False, "SEPARATE_DISJOINT": False,
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "buffered_right"
        )
        # Guard: prefer presence of offset lines; buffers can be empty in tight spaces
        sidewalks_ok = ((left_sw and left_sw.featureCount() > 0) or
                        (right_sw and right_sw.featureCount() > 0))
        if not sidewalks_ok:
            feedback.pushInfo("ℹ️ No sidewalks could be offset; skipping merge and tangent drill generation.")

        if sidewalks_ok:
            merged_sw = as_layer(
                processing.run(
                    "native:mergevectorlayers",
                    {"LAYERS": [bufL, bufR],  # CRS can be omitted; layers share CRS
                     "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                    is_child_algorithm=True, context=context, feedback=feedback
                )["OUTPUT"],
                context, "merged_sidewalks"
            )
        else:
            # Create an empty polygon memory layer to keep outputs consistent
            merged_sw = QgsVectorLayer(f"Polygon?crs={roads.crs().authid()}", "merged_sidewalks", "memory")

        # Build fast indexes for nearest searches that follow
        idx_L = QgsSpatialIndex(left_sw.getFeatures())
        idx_R = QgsSpatialIndex(right_sw.getFeatures())
        feedback.pushInfo(
            f"Sidewalks: left={left_sw.featureCount()} feats, right={right_sw.featureCount()} feats"
        )
        
        # 3) Write sinks (Left/Right lines always; merged + debug only if connected)
        # Keep this (single creation)
        sinkSL, idSL = self.parameterAsSink(p, self.O_SIDE_L, context, left_sw.fields(), left_sw.wkbType(), left_sw.crs())
        sinkSR, idSR = self.parameterAsSink(p, self.O_SIDE_R, context, right_sw.fields(), right_sw.wkbType(), right_sw.crs())

        # Add features ONCE here
        if idSL:
            sinkSL.addFeatures(list(left_sw.getFeatures()))
        if idSR:
            sinkSR.addFeatures(list(right_sw.getFeatures()))
        
        if sidewalks_ok:
            # --------------------------------------------------------
            # Stage 2b: Tangent drill trenches at ≥3-way intersections
            #   Reuses:
            #     • 'centers' computed earlier from vehicular road intersections
            #     • 'veh_roads_exploded' (segments) for local direction
            #   Uses tunables:
            #     • ibufr      (intersection circle radius)
            #     • tol_cluster (already used when forming 'centers')
            #     • degmin
            #     • cross_len  (trench total length)
            # --------------------------------------------------------

            tan_fields = QgsFields()
            tan_fields.append(QgsField("id", QVariant.Int))
            sinkTan, idTan = self.parameterAsSink(
                p, self.O_TANGENTS, context, tan_fields, QgsWkbTypes.LineString, roads.crs()
            )

            added_tan = 0
            tid = 1

            # quick spatial indexes
            idx_roads_exp = QgsSpatialIndex(veh_roads_exploded.getFeatures()) if 'veh_roads_exploded' in locals() and veh_roads_exploded else None

            def _nearest_road_feat(pt: QgsPointXY):
                if not idx_roads_exp:
                    return None
                rect = QgsGeometry.fromPointXY(pt).buffer(max(1.0, ibufr), 8).boundingBox()
                best = (float("inf"), None)
                for fid in idx_roads_exp.intersects(rect):
                    rf = next(veh_roads_exploded.getFeatures(QgsFeatureRequest(fid)), None)
                    if not rf:
                        continue
                    dist, _, *_ = rf.geometry().closestSegmentWithContext(pt)
                    if dist < best[0]:
                        best = (dist, rf)
                return best[1]

            if centers and sinkTan:
                for c in centers:
                    cpt = QgsPointXY(c)
                    roadf = _nearest_road_feat(cpt)
                    if not roadf:
                        continue
                    dvec = _dir_on_line_near_point(roadf.geometry(), cpt)
                    if not dvec:
                        continue

                    trench = _make_tangent_trench(cpt, dvec, ibufr, cross_len)
                    if not _geom_ok(trench):
                        continue

                    # must hit both sidewalks
                    box = trench.boundingBox()
                    left_hits  = any(trench.intersects(f.geometry()) for f in (left_sw.getFeature(fid)  for fid in idx_L.intersects(box)))
                    right_hits = any(trench.intersects(f.geometry()) for f in (right_sw.getFeature(fid) for fid in idx_R.intersects(box)))
                    if not (left_hits and right_hits):
                        continue

                    tf = QgsFeature(tan_fields)
                    tf.setGeometry(trench)
                    tf["id"] = tid
                    sinkTan.addFeature(tf)
                    tid += 1
                    added_tan += 1

                feedback.pushInfo(f"✅ Tangent drills created: {added_tan} (radius={ibufr} m, length={cross_len} m).")
            else:
                feedback.pushInfo("ℹ️ No valid centers/roads for tangent drills.")

            # --- Materialize and index tangent drills for later intersection tests ---
            tan_tmp = None
            self._tan_layer = None
            self._tan_idx = None
            self._tan_idfld = "id"

        else:
            # Ensure tangent-related members exist, but indicate none generated
            self._tan_layer = None
            self._tan_idx = None
            self._tan_idfld = "id"

            # Also allocate an empty "designed tangents" sink so downstream never sees 'missing'
            tan_fields = QgsFields()
            tan_fields.append(QgsField("id", QVariant.Int))
            sinkTan, idTan = self.parameterAsSink(
                p, self.O_TANGENTS, context, tan_fields, QgsWkbTypes.LineString, roads.crs()
            )


        if idTan:
            try:
                tan_tmp = as_layer(
                    processing.run(
                        "native:savefeatures",
                        {"INPUT": idTan, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"],
                    context, "tan_all"
                )
                self._tan_layer = tan_tmp
                self._tan_idx   = QgsSpatialIndex(tan_tmp.getFeatures())
            except Exception:
                tan_tmp = None
                self._tan_layer = None
                self._tan_idx = None

        
        idSideMerged = idBufL = idBufR = None
        sinkMerged, idSideMerged = self.parameterAsSink(p, self.O_SIDE_MERGED, context, merged_sw.fields(), merged_sw.wkbType(), merged_sw.crs())
        if idSideMerged:
            sinkMerged.addFeatures(list(merged_sw.getFeatures()))

        sinkBufL, idBufL = self.parameterAsSink(p, self.O_SIDE_BUF_L, context, bufL.fields(), bufL.wkbType(), bufL.crs())
        if idBufL:
            sinkBufL.addFeatures(list(bufL.getFeatures()))

        sinkBufR, idBufR = self.parameterAsSink(p, self.O_SIDE_BUF_R, context, bufR.fields(), bufR.wkbType(), bufR.crs())
        if idBufR:
            sinkBufR.addFeatures(list(bufR.getFeatures()))

        # --------------------------------------------------------
        # MFG passthrough (selected or first)
        # --------------------------------------------------------
        mfg_fields = QgsFields(); mfg_fields.append(QgsField("mfg_id", QVariant.Int))
        # MFG is optional: fall back to roads.crs() so the sink can still be created,
        # and skip the MFG-derived steps downstream.
        sinkMFG, idMFG = self.parameterAsSink(
            p, self.O_MFG, context, mfg_fields, QgsWkbTypes.Point,
            mfg_in.crs() if mfg_in else roads.crs()
        )
        mfg_pt = None
        mfg_id_val = None
        if mfg_in:
            feats = list(mfg_in.selectedFeatures()) or list(mfg_in.getFeatures())
            if not feats:
                raise QgsProcessingException("MFG layer has no features.")
            mf = feats[0]
            if not _geom_ok(mf.geometry()):
                raise QgsProcessingException("MFG geometry is invalid.")
            mf_out = QgsFeature(mfg_fields); mf_out.setGeometry(mf.geometry()); mf_out["mfg_id"] = 1
            sinkMFG.addFeature(mf_out)
            mfg_pt = _point_of(mf.geometry())
            if not mfg_pt:
                raise QgsProcessingException("MFG must be a point geometry.")
            # Canonical MFG_ID from the input layer (NetworkManager convention)
            _mfg_idf = _first_field_case_insensitive(mfg_in, ["MFG_ID", "mfg_id"])
            mfg_id_val = str(mf[_mfg_idf]) if (_mfg_idf and mf[_mfg_idf] is not None) else "MFG00001"
        else:
            feedback.pushInfo("MFG omitted: MFG->PDP feeder / distribution routing will be skipped.")
        
        # --------------------------------------------------------
        # PDP → Sidewalk projections (lines) + Pseudo PDP points
        # --------------------------------------------------------
        proj_fields = QgsFields()
        proj_fields.append(QgsField("pdp_id",     QVariant.String))
        proj_fields.append(QgsField("pdp_pol_id", QVariant.String))   # optional field from PDPs if present
        proj_fields.append(QgsField("dist_m",     QVariant.Double))
        sinkProj, idProj = self.parameterAsSink(p, self.O_PDP_PROJ, context, proj_fields, QgsWkbTypes.LineString, pdps.crs())
        
        p_fields = QgsFields()
        for nm, t in (
            ("pdp_id",     QVariant.String),
            ("pdp_pol_id", QVariant.String),
            ("sidewalk",   QVariant.String),   # 'left' | 'right'
            ("method",     QVariant.String),   # 'nearest'
            ("dist_m",     QVariant.Double),
            ("seg_fid",    QVariant.Int),
        ):
            p_fields.append(QgsField(nm, t))
        sinkPseudo, idPseudo = self.parameterAsSink(p, self.O_PSEUDO_PDP, context, p_fields, QgsWkbTypes.Point, pdps.crs())
        
        
        # Hard sanity check: nothing to project if the PDP layer is empty.
        if pdps.featureCount() == 0:
            feedback.reportError(
                "❌ 0 PDPs found on input. Did you forget to run Object/Polygon Layout first? "
                "Trench layer cannot be built without PDPs."
            )
            raise QgsProcessingException(
                "Empty PDP input. Run Network Layer (PDP) before Generate Trenches."
            )

        pdp_attr_field = pdp_id_field if (pdp_id_field and pdp_id_field in pdps.fields().names()) else None
        pdp_attr_copy = None  # set to another field name if you want to copy an extra attribute

        # Detect POLYGON_ID and PDP_ID fields from NetworkManager on the PDP layer
        poly_id_field = _first_field_case_insensitive(
            pdps, ["POLYGON_ID", "POLY_ID", "polygon_id", "poly_id"]
        )
        net_pdp_id_field = _first_field_case_insensitive(
            pdps, ["PDP_ID", "pdp_id"]
        )
        # Lookup: pdp_id (as used in trench) -> {polygon_id, pdp_id}
        pdp_id_lookup = {}
        # Device location per pdp_id, so feeder routes can terminate AT the PDP
        pdp_pt_by_pid = {}

        created_proj = created_pp = 0
        for pf in pdps.getFeatures():
            g = pf.geometry()
            if not _geom_ok(g):
                continue
            pt = _point_of(g)
            if pt is None:
                continue
            
            Lp, Ld, _ = nearest_point_on_lines(left_sw,  idx_L, pt, pdp_search_r)
            Rp, Rd, _ = nearest_point_on_lines(right_sw, idx_R, pt, pdp_search_r)

            # pick the better side
            if Lp and (Ld <= (Rd if Rp else float("inf"))):
                side, hit_pt, dist = "left", Lp, Ld
            else:
                side, hit_pt, dist = "right", Rp, Rd
        
            if not hit_pt or math.isinf(dist):
                continue
            
            pid = str(pf[pdp_attr_field]) if (pdp_attr_field and pf[pdp_attr_field] is not None) else str(pf.id())
            pid_att = None

            # Store polygon_id / pdp_id from NetworkManager for later propagation
            poly_val = str(pf[poly_id_field]) if (poly_id_field and pf[poly_id_field] is not None) else None
            net_pdp_val = str(pf[net_pdp_id_field]) if (net_pdp_id_field and pf[net_pdp_id_field] is not None) else None
            if pid and (poly_val or net_pdp_val):
                pdp_id_lookup[pid] = (poly_val, net_pdp_val)
            if pid:
                pdp_pt_by_pid[pid] = QgsPointXY(pt)
            if pdp_attr_copy and pdp_attr_copy in pdps.fields().names():
                vv = pf[pdp_attr_copy]
                pid_att = "" if vv is None else str(vv)
        
            # projection line
            lf = QgsFeature(proj_fields)
            lf.setGeometry(QgsGeometry.fromPolylineXY([pt, hit_pt]))
            lf["pdp_id"]     = pid
            lf["pdp_pol_id"] = pid_att
            lf["dist_m"]     = round(dist, 3)
            sinkProj.addFeature(lf)
            created_proj += 1
        
            # pseudo PDP at the sidewalk touch point
            pp = QgsFeature(p_fields)
            pp.setGeometry(QgsGeometry.fromPointXY(hit_pt))
            pp["pdp_id"]     = pid
            pp["pdp_pol_id"] = pid_att
            pp["sidewalk"]   = side
            pp["method"]     = "nearest"
            pp["dist_m"]     = round(dist, 3)
            pp["seg_fid"]    = int(pf.id())
            sinkPseudo.addFeature(pp)
            created_pp += 1
        
        feedback.pushInfo(f"Projected {created_proj} PDP(s) and created {created_pp} pseudo PDP points (search ≤ {pdp_search_r} m).")
        

        # --------------------------------------------------------
        # Build graph (match standalone Feeder on sidewalks)
        #   • Sidewalk Left + Right (+ tangent drills if any)
        #   • densify before adding to graph
        #   • add L↔R bridges at their intersections, then heal
        #   • snap MFG / PDPs to graph nodes and route with Dijkstra
        # --------------------------------------------------------
        # Collect drill layer (may be empty if not designed yet)
        tan_tmp = None
        if 'idTan' in locals() and idTan:
            try:
                tan_tmp = as_layer(
                    processing.run(
                        "native:savefeatures",
                        {"INPUT": idTan, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"],
                    context, "tan_all"
                )
            except Exception:
                tan_tmp = None  # keep going without drills



        def _add_lr_bridges(G, left: QgsVectorLayer, right: QgsVectorLayer, heal_m: float, eps_val: float):
            # L↔R intersections become nodes; connect each intersection to nearest
            # points on each side (short spokes). Then heal close nodes.
            if not left or not right or left.featureCount() == 0 or right.featureCount() == 0:
                return
            r_cache = [(rf.id(), rf.geometry()) for rf in right.getFeatures()]
            for lf in left.getFeatures():
                lg = lf.geometry()
                if not _geom_ok(lg):
                    continue
                box = lg.boundingBox()
                for rid, rg in r_cache:
                    if not _geom_ok(rg):
                        continue
                    if not box.intersects(rg.boundingBox()):
                        continue
                    if not lg.intersects(rg):
                        continue
                    inter = lg.intersection(rg)
                    if not _geom_ok(inter) or QgsWkbTypes.geometryType(inter.wkbType()) != QgsWkbTypes.PointGeometry:
                        continue
                    pts = inter.asMultiPoint() if QgsWkbTypes.isMultiType(inter.wkbType()) else [inter.asPoint()]
                    for pt in pts:
                        node = _qkey(QgsPointXY(pt), eps_val)
                        G.add_node(node)
                        # spokes to each side
                        lnpt = lg.closestSegmentWithContext(pt)[1]
                        rnpt = rg.closestSegmentWithContext(pt)[1]
                        lnode = _qkey(QgsPointXY(lnpt), eps_val)
                        rnode = _qkey(QgsPointXY(rnpt), eps_val)
                        dl = QgsGeometry.fromPointXY(QgsPointXY(pt)).distance(QgsGeometry.fromPointXY(lnpt))
                        dr = QgsGeometry.fromPointXY(QgsPointXY(pt)).distance(QgsGeometry.fromPointXY(rnpt))
                        if dl > 0:
                            G.add_edge(node, lnode, weight=dl)
                        if dr > 0:
                            G.add_edge(node, rnode, weight=dr)

            # heal close nodes (connect within heal_m)
            if heal_m > 0 and G.number_of_nodes() > 0:
                nodes = list(G.nodes)
                cell = heal_m
                buckets = {}
                for n in nodes:
                    key = (int(n[0] // cell), int(n[1] // cell))
                    buckets.setdefault(key, []).append(n)
                nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 0), (0, 1), (1, -1), (1, 0), (1, 1)]
                for k, group in buckets.items():
                    for dx, dy in nbrs:
                        other = buckets.get((k[0] + dx, k[1] + dy), [])
                        for a in group:
                            for b in other:
                                if a >= b:
                                    continue
                                d = math.hypot(a[0] - b[0], a[1] - b[1])
                                if 0 < d <= heal_m and not G.has_edge(a, b):
                                    G.add_edge(a, b, weight=d)


        # ---- Build graph from sidewalks (+ optional tangents). Ensure sidewalks exist. ----
        if (not left_sw or left_sw.featureCount() == 0) and (not right_sw or right_sw.featureCount() == 0):
            feedback.reportError("No sidewalks available to build the graph.")
            G = nx.Graph()
        else:
            G = nx.Graph()
            # use previously computed eps
            _add_lines_to_graph(G, left_sw,  dens, eps, _qkey)
            _add_lines_to_graph(G, right_sw, dens, eps, _qkey)
            if tan_tmp:
                _add_lines_to_graph(G, tan_tmp, dens, eps, _qkey)


            _add_lr_bridges(G, left_sw, right_sw, heal, eps)


        # --------------------------------------------------------
        # Feeder MFG→pseudo PDP (same logic as standalone)
        # --------------------------------------------------------
        # NOTE: dropped lowercase ``pdp_id`` — GPKG is case-insensitive
        # for column uniqueness and would collide with canonical ``PDP_ID``.
        # Downstream readers should use ``first_field_case_insensitive``
        # (utils.fields) to look up either case.
        mfgpdp_fields = QgsFields()
        mfgpdp_fields.append(QgsField("dist_m", QVariant.Double))
        mfgpdp_fields.append(QgsField("POLYGON_ID", QVariant.String))
        mfgpdp_fields.append(QgsField("PDP_ID", QVariant.String))
        sinkMP, idMP = self.parameterAsSink(
            p, self.O_MFG_PDP, context, mfgpdp_fields, QgsWkbTypes.LineString, pdps.crs()
        )

        # Feeder_Trench schema + sink
        feeder_fields = QgsFields()
        feeder_fields.append(QgsField("id", QVariant.Int))          # serial ID
        feeder_fields.append(QgsField("dist_m", QVariant.Double))   # cached trench length
        feeder_fields.append(QgsField("POLYGON_ID", QVariant.String))
        feeder_fields.append(QgsField("PDP_ID", QVariant.String))
        feeder_fields.append(QgsField("MFG_ID", QVariant.String))   # routing source device

        sinkFD, idFD = self.parameterAsSink(
            p, self.O_FEEDER, context, feeder_fields, QgsWkbTypes.MultiLineString, pdps.crs()
        )

        serial = 1  # running ID counter
        try:
            fd_vis = as_layer(idFD, context, "Feeder_vis")
            _colorize_mem_line_layer(fd_vis, "#ff6d00", 1.0)
        except Exception:
            # Styling only — a file-destination sink can't be resolved while still open.
            pass

        used_tan_ids: Set[int] = set()
        tan_index = QgsSpatialIndex(tan_tmp.getFeatures()) if (tan_tmp and tan_tmp.featureCount() > 0) else None


        def _mark_used_tangents(geom: QgsGeometry):
            """Mark tangent drill features intersected by a feeder/distribution line.
               Counts both line overlaps and point touches."""
            if not tan_tmp or not tan_index or not _geom_ok(geom):
                return
            for fid in tan_index.intersects(geom.boundingBox()):
                tf = next(tan_tmp.getFeatures(QgsFeatureRequest(fid)), None)
                if not tf:
                    continue
                tg = tf.geometry()
                if not _geom_ok(tg):
                    continue
                
                # Fast reject: if not intersects at all, skip
                if not geom.intersects(tg):
                    continue
                
                inter = geom.intersection(tg)
                if not _geom_ok(inter):
                    continue
                
                gtype = QgsWkbTypes.geometryType(inter.wkbType())

                hit = False
                if gtype == QgsWkbTypes.LineGeometry:
                    # real overlap (non-zero length)
                    hit = inter.length() > 0.0
                elif gtype == QgsWkbTypes.PointGeometry:
                    # point touch (node/bridge)
                    hit = True
                else:
                    # in weird cases (e.g., collections), be permissive
                    try:
                        hit = inter.length() > 0.0 or inter.area() > 0.0
                    except Exception:
                        hit = True

                if hit:
                    try:
                        used_tan_ids.add(int(tf["id"]) if "id" in tan_tmp.fields().names() else int(tf.id()))
                    except Exception:
                        used_tan_ids.add(int(tf.id()))


        # Snap MFG to graph (skipped when MFG was omitted: mfg_pt is None)
        mfg_node = None
        if mfg_pt and G and G.number_of_nodes() > 0:
            mfg_node, _ = _snap_to_nodes(mfg_pt, G.nodes, snap)

        if mfg_pt and not mfg_node:
            feedback.reportError("Could not snap MFG to graph (try increasing Snap distance).")
            

        # Route MFG→each pseudo PDP
        pseudo_pdp_layer = as_layer(
            processing.run(
                "native:savefeatures",
                {"INPUT": idPseudo, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "pseudo_PDP"
        )

        ok_paths = 0
        if mfg_node and pseudo_pdp_layer.featureCount() > 0:
            for tf in pseudo_pdp_layer.getFeatures():
                pt = _point_of(tf.geometry())
                if not pt:
                    continue
                
                pdp_node, _ = _snap_to_nodes(pt, G.nodes, snap)
                if not pdp_node:
                    continue
                
                try:
                    path = nx.shortest_path(G, source=mfg_node, target=pdp_node, weight="weight")
                except Exception:
                    continue
                
                if len(path) < 2:
                    continue

                pid_val = str(tf["pdp_id"]) if "pdp_id" in tf.fields().names() else str(tf.id())
                coords = [QgsPointXY(x, y) for (x, y) in path]
                # Extend the routed sidewalk path to the physical devices so the
                # feeder feature spans MFG → PDP, not graph node → graph node:
                # MFG point at the head; pseudo-PDP + PDP device point at the tail.
                if mfg_pt is not None:
                    coords.insert(0, QgsPointXY(mfg_pt))
                coords.append(QgsPointXY(pt))              # pseudo-PDP on the sidewalk
                _dev_pt = pdp_pt_by_pid.get(pid_val)
                if _dev_pt is not None:
                    coords.append(QgsPointXY(_dev_pt))     # actual PDP device point
                geom = QgsGeometry.fromPolylineXY(coords)
                if not _geom_ok(geom) or geom.length() < 0.05:
                    continue

                # Create MFG→PDP feature
                of = QgsFeature(mfgpdp_fields)
                of.setGeometry(geom)
                of["dist_m"] = round(geom.length(), 2)
                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                poly_lu, pdp_lu = (pdp_id_lookup.get(pid_val, (None, None)) if pid_val and pdp_id_lookup else (None, None))
                of["POLYGON_ID"] = poly_lu
                # Canonical id only (lowercase "pdp_id" removed to avoid
                # GPKG case-insensitive column-collision; prefer NM value
                # but fall back to the pseudo-pdp local id).
                of["PDP_ID"] = pdp_lu or pid_val
                sinkMP.addFeature(of)

                # Add to Feeder trenches
                ff = QgsFeature(feeder_fields)
                ff.setGeometry(geom)
                ff["id"] = serial
                ff["dist_m"] = of["dist_m"]
                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                poly_lu, pdp_lu = (pdp_id_lookup.get(pid_val, (None, None)) if pid_val and pdp_id_lookup else (None, None))
                ff["POLYGON_ID"] = poly_lu
                ff["PDP_ID"] = pdp_lu or pid_val
                ff["MFG_ID"] = mfg_id_val
                sinkFD.addFeature(ff)

                serial += 1
                ok_paths += 1

                # Register any tangent drills intersected
                _mark_used_tangents(geom)

        feedback.pushInfo(f"✅ Created {ok_paths} feeder trenches on sidewalks.")


        # --- 8) Merge Feeder Trenches with PDP Connectors (like the standalone tool) ---

        # materialize the inputs we just wrote
        _feeder_lines = as_layer(
            processing.run(
                "native:savefeatures",
                {"INPUT": idFD, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "feeder_step7"
        )
        _connectors = as_layer(
            processing.run(
                "native:savefeatures",
                {"INPUT": idProj, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"],
            context, "pdp_connectors"
        )

        # --- Optional safety trim: keep feeder lines away from buildings ---
        if bldg_bufL and _feeder_lines and _feeder_lines.featureCount():
            try:
                _feeder_lines = _erase_or_difference(_feeder_lines, bldg_bufL, context, feedback, "feeder_trim")
            except Exception as _e:
                feedback.reportError(f"Feeder trim vs buildings failed; using untrimmed. Error: {_e}")
        # --- guards: ensure optional outputs always exist ---
        # Make sure idMergedPDP exists as an empty layer if not assigned
        if 'idMergedPDP' not in locals() or not idMergedPDP:
            empty = _empty_layer("MultiLineString", roads.crs())
            idMergedPDP = processing.run(
                "native:savefeatures",
                {
                    "INPUT": empty,
                    "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT  # temp first, we just need a valid handle
                },
                context=context, feedback=feedback, is_child_algorithm=True
            )["OUTPUT"]

        # Same idea for Feeder_Final if you reference it later
        if 'idFeederFinal' not in locals() or not idFeederFinal:
            empty = _empty_layer("MultiLineString", roads.crs())
            idFeederFinal = processing.run(
                "native:savefeatures",
                {
                    "INPUT": empty,
                    "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT
                },
                context=context, feedback=feedback, is_child_algorithm=True
            )["OUTPUT"]
        # --- end guards ---

        # Short-circuit if nothing to merge
        if not _feeder_lines or _feeder_lines.featureCount() == 0:
            feedback.pushInfo("Step 8: no feeder lines to merge.")
            # still allocate empty sinks for downstream safety
            merged_fields = QgsFields()
            merged_fields.append(QgsField("dist_m", QVariant.Double))
            merged_fields.append(QgsField("POLYGON_ID", QVariant.String))
            merged_fields.append(QgsField("PDP_ID", QVariant.String))
            sinkMerged, idMergedPDP = self.parameterAsSink(
                p, self.O_MERGED_PDP, context, merged_fields, QgsWkbTypes.MultiLineString, roads.crs()
            )

            final_fields = QgsFields()
            final_fields.append(QgsField("id", QVariant.Int))
            final_fields.append(QgsField("POLYGON_ID", QVariant.String))
            final_fields.append(QgsField("PDP_ID", QVariant.String))
            sinkFinal,  idFeederFinal = self.parameterAsSink(
                p, self.O_FEEDER_FINAL, context, final_fields, QgsWkbTypes.MultiLineString, roads.crs()
            )
        else:
            # outputs (use source layer CRS for safety)
            merged_fields = QgsFields()
            merged_fields.append(QgsField("dist_m", QVariant.Double))
            merged_fields.append(QgsField("POLYGON_ID", QVariant.String))
            merged_fields.append(QgsField("PDP_ID", QVariant.String))
            sinkMerged, idMergedPDP = self.parameterAsSink(
                p, self.O_MERGED_PDP, context, merged_fields, QgsWkbTypes.MultiLineString, _feeder_lines.crs()
            )

            final_fields = QgsFields()
            final_fields.append(QgsField("id", QVariant.Int))
            final_fields.append(QgsField("POLYGON_ID", QVariant.String))
            final_fields.append(QgsField("PDP_ID", QVariant.String))
            sinkFinal, idFeederFinal = self.parameterAsSink(
                p, self.O_FEEDER_FINAL, context, final_fields, QgsWkbTypes.MultiLineString, _feeder_lines.crs()
            )

            # Index connectors by pdp_id
            conn_by_id = {}
            if _connectors and _connectors.featureCount() > 0:
                conn_pid_field = _first_field_case_insensitive(_connectors, ["PDP_ID", "pdp_id", "pdp_pol_id"])
                if conn_pid_field:
                    for f in _connectors.getFeatures():
                        pid = "" if f[conn_pid_field] is None else str(f[conn_pid_field])
                        if not pid:
                            continue
                        g = f.geometry()
                        if _geom_ok(g):
                            conn_by_id.setdefault(pid, []).append(g)

            feed_names = _feeder_lines.fields().names()
            length_field = "dist_m" if "dist_m" in feed_names else None
            feed_pid_field = _first_field_case_insensitive(_feeder_lines, ["PDP_ID", "pdp_id", "pdp_pol_id"])

            added_merged = added_final = skipped = 0
            serial = 1

            for tr in _feeder_lines.getFeatures():
                tg = tr.geometry()
                if not _geom_ok(tg):
                    continue
                if not feed_pid_field:
                    skipped += 1
                    continue
                pid = "" if tr[feed_pid_field] is None else str(tr[feed_pid_field])
                if not pid:
                    skipped += 1
                    continue

                # trench length from attribute if present, else geometry length
                dist_val = float(tr[length_field]) if (length_field and tr[length_field] is not None) else float(tg.length())

                # choose the closest connector for this pdp_id (if any)
                best_g = None
                best_d = float("inf")
                for cg in conn_by_id.get(pid, []):
                    d = tg.distance(cg)
                    if d < best_d:
                        best_d, best_g = d, cg

                merged_geom = _safe_collect(tg, best_g)
                if not _geom_ok(merged_geom):
                    merged_geom = tg  # fallback

                # coerce to MultiLineString for sinks
                merged_multi = _to_multiline(merged_geom)

                # write “Merged Trenches per PDP”
                mf = QgsFeature(merged_fields)
                mf.setGeometry(merged_multi)
                mf["dist_m"] = round(dist_val, 2)
                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                poly_lu, pdp_lu = (pdp_id_lookup.get(pid, (None, None)) if pid and pdp_id_lookup else (None, None))
                mf["POLYGON_ID"] = poly_lu
                mf["PDP_ID"] = pdp_lu or pid
                sinkMerged.addFeature(mf)
                added_merged += 1

                # write “Feeder_Trench (multi-lines, id only)”
                ff = QgsFeature(final_fields)
                ff.setGeometry(merged_multi)
                ff["id"] = serial
                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                poly_lu, pdp_lu = (pdp_id_lookup.get(pid, (None, None)) if pid and pdp_id_lookup else (None, None))
                ff["POLYGON_ID"] = poly_lu
                ff["PDP_ID"] = pdp_lu or pid
                sinkFinal.addFeature(ff)
                serial += 1
                added_final += 1

            feedback.pushInfo(f"✅ Step 8: merged {added_merged} trenches (skipped {skipped}).")

        # -------------------------------------------------------------------------
        # --- Garden trenches: project HH to Sidewalk Left/Right (best of both) ---
        # -------------------------------------------------------------------------
        idGarden = idPseudoHH = None  # for Garden + pseudo HH
        idDist = idDistD = None       # for Distribution

        if hh and hh.featureCount():

            def _nearest_on_layer(idx: QgsSpatialIndex, layer: QgsVectorLayer, ref_geom: QgsGeometry, k: int):
                """Return nearest point and distance on layer."""
                qpt = ref_geom.asPoint() if QgsWkbTypes.geometryType(ref_geom.wkbType()) == QgsWkbTypes.PointGeometry else ref_geom.centroid().asPoint()
                ids = idx.nearestNeighbor(QgsPointXY(qpt), k)
                best = (None, float("inf"))
                for fid in ids:
                    g = layer.getFeature(fid).geometry()
                    if not g or g.isEmpty():
                        continue
                    np = g.nearestPoint(ref_geom)
                    if not np or np.isEmpty():
                        continue
                    d = float(np.distance(ref_geom))
                    if d < best[1]:
                        pt = np.asPoint() if not np.isMultipart() else np.asMultiPoint()[0]
                        best = (QgsPointXY(pt), d)
                return best

            g_fields = QgsFields()
            for nm, t in (
                ("pdp_pol_id", QVariant.String),
                ("addr_id",    QVariant.String),
                ("hhs",        QVariant.String),
                ("sidewalk",   QVariant.String),
                ("method",     QVariant.String),
                ("distance_m", QVariant.Double),
                ("hh_fid",     QVariant.Int),
                ("POLYGON_ID", QVariant.String),
                ("PDP_ID",     QVariant.String),
                ("MFG_ID",     QVariant.String),
            ):
                g_fields.append(QgsField(nm, t))

            sinkG, idG = self.parameterAsSink(
                p, self.O_GARDEN, context, g_fields, QgsWkbTypes.LineString, roads.crs()
            )

            # Spatial indexes for sidewalks (already in EPSG:25833)
            idx_left  = QgsSpatialIndex(left_sw.getFeatures())
            idx_right = QgsSpatialIndex(right_sw.getFeatures())

            # Find best HH ID field
            hh_names = hh.fields().names()
            addr_field = hh_id_field if (hh_id_field and hh_id_field in hh_names) else \
                _first_field_case_insensitive(
                    hh,
                    ["ADDR_ID", "address_id", "HH_ID", "id", "ID", "name", "NAME",
                     "HOUSEHOLD_S", "HOUSEHOLD", "Address", "ADDRESS"]
                )
            # Find household count field (number of households)
            hh_hhs_field = _first_field_case_insensitive(
                hh,
                ["HH", "HOUSEHOLD_S", "HOUSEHOLD", "hhs", "HH_ID", "households"]
            )

            created_garden = 0
            for hf in hh.getFeatures():
                hpt = _point_of(hf.geometry())
                if not hpt:
                    continue
                ref = QgsGeometry.fromPointXY(hpt)

                l_pt, l_d = _nearest_on_layer(idx_left,  left_sw,  ref, garden_k)
                r_pt, r_d = _nearest_on_layer(idx_right, right_sw, ref, garden_k)

                best_label, best_pt, best_d = None, None, float("inf")
                if l_pt is not None and l_d < best_d:
                    best_label, best_pt, best_d = "left",  l_pt, l_d
                if r_pt is not None and r_d < best_d:
                    best_label, best_pt, best_d = "right", r_pt, r_d

                if best_pt is None:
                    continue

                addr_val = str(hf[addr_field]) if addr_field and hf[addr_field] is not None else str(hf.id())
                hhs_val  = str(hf[hh_hhs_field]) if (hh_hhs_field and hh_hhs_field in hh_names and hf[hh_hhs_field] is not None) else None
                pdp_val  = str(hf[hh_pdp_field]) if (hh_pdp_field and hh_pdp_field in hh_names and hf[hh_pdp_field] is not None) else None

                line = QgsGeometry.fromPolylineXY([hpt, best_pt])

                gf = QgsFeature(g_fields)
                gf.setGeometry(line)
                gf["pdp_pol_id"] = pdp_val
                gf["addr_id"]    = addr_val
                gf["hhs"]        = hhs_val
                gf["sidewalk"]   = best_label
                gf["method"]     = "nearest"
                gf["distance_m"] = round(best_d, 2)
                gf["hh_fid"]     = int(hf.id())
                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                # (MUST happen BEFORE writes — ``pdp_lu`` is read below).
                poly_lu, pdp_lu = (pdp_id_lookup.get(pdp_val, (None, None)) if pdp_val and pdp_id_lookup else (None, None))
                gf["POLYGON_ID"] = poly_lu
                # Canonical PDP_ID only — lowercase ``pdp_id`` was removed
                # from ``g_fields`` to dodge GPKG case-insensitive column
                # collision with the canonical PDP_ID column.
                gf["PDP_ID"] = pdp_lu or pdp_val
                gf["MFG_ID"] = mfg_id_val
                sinkG.addFeature(gf)
                created_garden += 1

            # --- Trim Garden trenches against building buffer (if any) ---
            if bldg_bufL:
                try:
                    # materialize current garden sink to a layer
                    garden_raw = as_layer(processing.run(
                        "native:savefeatures",
                        {"INPUT": idG, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"], context, "garden_raw")

                    garden_trim = _erase_or_difference(garden_raw, bldg_bufL, context, feedback, "garden_trim")

                    # write trimmed garden to a fresh sink and replace idGarden
                    sinkG2, idG2 = self.parameterAsSink(
                        p, self.O_GARDEN, context, garden_trim.fields(), garden_trim.wkbType(), garden_trim.crs()
                    )
                    if idG2:
                        for f in garden_trim.getFeatures():
                            sinkG2.addFeature(f)
                        idG = idG2   # <- replace the original sink id with trimmed one
                except Exception as _e:
                    feedback.reportError(f"Garden trim vs buildings failed; using untrimmed. Error: {_e}")

            # keep using idG below
            idGarden = idG

        
            # -----------------------------
            # Stage 3: Pseudo Object/HH Points
            # -----------------------------
            ph_fields = QgsFields()
            for nm, t in (
                ("pdp_pol_id", QVariant.String),
                ("addr_id",    QVariant.String),
                ("hh_id",      QVariant.String),
                ("sidewalk",   QVariant.String),
                ("method",     QVariant.String),
                ("dist_m",     QVariant.Double),
                ("proj_fid",   QVariant.Int),
                ("pdp_id",     QVariant.String),
            ):
                ph_fields.append(QgsField(nm, t))

            sinkPH, idPH = self.parameterAsSink(
                p, self.O_PSEUDO_HH, context, ph_fields, QgsWkbTypes.Point, roads.crs()
            )

            # materialize garden and reuse Stage-2 sidewalks
            gardenL = as_layer(
                processing.run(
                    "native:savefeatures",
                    {"INPUT": idGarden, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                    is_child_algorithm=True, context=context, feedback=feedback
                )["OUTPUT"],
                context, "gardenL"
            )
            leftL, rightL = left_sw, right_sw

            idx_left  = QgsSpatialIndex(leftL.getFeatures())
            idx_right = QgsSpatialIndex(rightL.getFeatures())

            # Resolve common field names once (case-insensitive, tolerant)
            _garden_names = gardenL.fields().names()
            _pdp_pol_field = _first_field_case_insensitive(gardenL, ["pdp_pol_id", "pdp", "pdp_poly_id"])
            _addr_field    = _first_field_case_insensitive(gardenL, ["addr_id", "address_id"])
            _hh_field      = _first_field_case_insensitive(gardenL, ["hhs", "hh", "hh_id"])
            _pdp_field     = _first_field_case_insensitive(gardenL, ["pdp_id", "pdp"])

            def _choose_point(row_geom: QgsGeometry):
                """Return (pt, side, method, dist)."""
                bbox = row_geom.boundingBox()
                ln = row_geom.asMultiPolyline()[0] if row_geom.isMultipart() else row_geom.asPolyline()
                end_pt = QgsPointXY(ln[-1]) if ln else None

                # 1) prefer intersection
                if prefer_intersection:
                    for label, lyr, sidx in (("left", leftL, idx_left), ("right", rightL, idx_right)):
                        for fid in sidx.intersects(bbox):
                            sg = next(lyr.getFeatures(QgsFeatureRequest(fid)), None)
                            if not sg:
                                continue
                            inter = row_geom.intersection(sg.geometry())
                            if _geom_ok(inter) and QgsWkbTypes.geometryType(inter.wkbType()) == QgsWkbTypes.PointGeometry:
                                pts = inter.asMultiPoint() if QgsWkbTypes.isMultiType(inter.wkbType()) else [inter.asPoint()]
                                if end_pt:
                                    pts.sort(key=lambda p_: math.hypot(p_.x() - end_pt.x(), p_.y() - end_pt.y()))
                                return QgsPointXY(pts[0]), label, "intersection", 0.0

                # 2) nearest by distance
                best_pt, best_dist, best_side = None, float("inf"), None
                for label, lyr, sidx in (("left", leftL, idx_left), ("right", rightL, idx_right)):
                    for fid in sidx.intersects(bbox):
                        sg = next(lyr.getFeatures(QgsFeatureRequest(fid)), None)
                        if not sg:
                            continue
                        pt, d = _nearest_point_and_distance(sg.geometry(), row_geom)
                        if pt is not None and d < best_dist:
                            best_pt, best_dist, best_side = pt, d, label
                if best_pt is not None:
                    return best_pt, best_side, "nearest", best_dist

                # 3) fallback by end or centroid
                qpt = end_pt
                if qpt is None:
                    cen = row_geom.centroid()
                    if _geom_ok(cen) and QgsWkbTypes.geometryType(cen.wkbType()) == QgsWkbTypes.PointGeometry:
                        qpt = QgsPointXY(cen.asPoint())
                if qpt is not None:
                    for label, lyr, sidx in (("left", leftL, idx_left), ("right", rightL, idx_right)):
                        nn = sidx.nearestNeighbor(qpt, 1)
                        if nn:
                            sg = next(lyr.getFeatures(QgsFeatureRequest(nn[0])), None)
                            if sg:
                                pt, d = _nearest_point_and_distance(sg.geometry(), row_geom)
                                if pt is not None:
                                    return pt, label, "nearest", d
                return None, None, "nearest", None

            created_pts = 0
            for row in gardenL.getFeatures():
                g = row.geometry()
                if not _geom_ok(g):
                    continue
                pt, side, method, dist = _choose_point(g)
                if pt is None:
                    continue
                
                f = QgsFeature(ph_fields)
                f.setGeometry(QgsGeometry.fromPointXY(pt))
                # carry attributes if present (case-insensitive/tolerant)
                f["pdp_pol_id"] = row[_pdp_pol_field] if _pdp_pol_field else None
                f["addr_id"]    = row[_addr_field]    if _addr_field    else None
                # prefer an HH-like field; otherwise fall back to addr_id
                _hh_val = row[_hh_field] if _hh_field else (row[_addr_field] if _addr_field else None)
                f["hh_id"]      = _hh_val
                f["sidewalk"]   = side
                f["method"]     = method
                f["dist_m"]     = round(float(dist), 3) if dist is not None else None
                f["proj_fid"]   = int(row.id())
                f["pdp_id"]     = row[_pdp_field] if _pdp_field else None
                sinkPH.addFeature(f)
                created_pts += 1

            idPseudoHH = idPH
            feedback.pushInfo(f"✅ Created {created_pts} pseudo household points.")
            feedback.pushInfo(f"✅ Garden trenches created: {created_garden}")
            feedback.pushInfo(f"ℹ️ Proceeding to Stage-9 Distribution …")



            # --- 9) Distribution Trenches (strict PDP → Objects by attribute) ---
            idDist = idDistD = None

            # Only run if HH provided and PDP linkage field exists
            if hh and hh.featureCount() and hh_pdp_field:
                try:
                    if 'idPseudo' not in locals() or not idPseudo:
                        feedback.reportError("⚠️ Stage-9: Missing pseudo PDP points (idPseudo). Skipping Distribution.")
                        raise RuntimeError("missing_pseudo_pdp")

                    if 'idPseudoHH' not in locals() or not idPseudoHH:
                        feedback.reportError("⚠️ Stage-9: Missing pseudo HH points (idPseudoHH). Skipping Distribution.")
                        raise RuntimeError("missing_pseudo_hh")

                    feedback.pushInfo("🚀 Stage-9: Building Distribution Trenches (strict PDP → Objects) ...")

                    pseudo_pdp_L = _materialize_layer(idPseudo, context, feedback, "pseudo_pdp_L")
                    pseudo_hh_L  = _materialize_layer(idPseudoHH, context, feedback, "pseudo_hh_L")

                    if pseudo_pdp_L.featureCount() == 0 or pseudo_hh_L.featureCount() == 0:
                        feedback.reportError("⚠️ Stage-9: Pseudo PDP or pseudo HH layer is empty. Skipping Distribution.")
                        raise RuntimeError("empty_inputs")

                    # --- Build graph (Sidewalk Left + Right + Tangents) ---
                    Gd = nx.Graph()

                    def _add_lines(G, lyr): _add_lines_to_graph(G, lyr, dens, eps, _qkey)
                    _add_lines(Gd, left_sw)
                    _add_lines(Gd, right_sw)
                    if tan_tmp:
                        _add_lines(Gd, tan_tmp)

                    # Bridges between layers (sidewalks ↔ sidewalks); tangents are optional extras
                    _add_lr_bridges(Gd, left_sw, right_sw, heal, eps)
                    if tan_tmp:
                        _add_lr_bridges(Gd, left_sw,  tan_tmp, heal, eps)
                        _add_lr_bridges(Gd, right_sw, tan_tmp, heal, eps)

                    if Gd.number_of_nodes() == 0:
                        feedback.reportError("⚠️ Stage-9: Distribution graph is empty (no sidewalks/bridges). Skipping Distribution.")
                        raise RuntimeError("empty_graph")

                    node_layer, idx_nodes, nid2node, _snap_for_idx = _build_node_index_from_graph(Gd, roads.crs().authid(), snap)

                    # --- Outputs ---
                    lines_fields = QgsFields()
                    lines_fields.append(QgsField("obj_id",   QVariant.String))
                    lines_fields.append(QgsField("addr_id",  QVariant.String))   # canonical ADDR_ID of the target HH
                    lines_fields.append(QgsField("length_m", QVariant.Double))
                    lines_fields.append(QgsField("POLYGON_ID", QVariant.String))
                    lines_fields.append(QgsField("PDP_ID",     QVariant.String))
                    lines_fields.append(QgsField("MFG_ID",     QVariant.String))
                    sinkDL, idDist = self.parameterAsSink(p, self.O_DIST_LINES, context, lines_fields, QgsWkbTypes.LineString, roads.crs())
                    
                    # --- At this point, you've already finished adding features to sinkDL/idDist ---

                    # Optional safety trim: keep distribution lines away from buildings (run AFTER writing sinkDL)
                    if bldg_bufL and idDist:
                        try:
                            _dist_lines = _materialize_layer(idDist, context, feedback, "dist_lines_raw")
                            if _dist_lines and _dist_lines.featureCount() > 0:
                                _dist_trim = _erase_or_difference(_dist_lines, bldg_bufL, context, feedback, "dist_trim")

                                # re-write trimmed lines to a fresh sink and replace BOTH sinkDL and idDist
                                d_fields = _dist_trim.fields()
                                sinkDL2, idDist2 = self.parameterAsSink(
                                    p, self.O_DIST_LINES, context, d_fields, _dist_trim.wkbType(), _dist_trim.crs()
                                )
                                if idDist2:
                                    for f in _dist_trim.getFeatures():
                                        sinkDL2.addFeature(f)
                                    sinkDL = sinkDL2     # <- update sink handle used by QGIS for this output
                                    idDist = idDist2     # <- update the layer id for downstream use
                        except Exception as _e:
                            feedback.reportError(f"Distribution trim vs buildings failed; using untrimmed. Error: {_e}")

                    
                    # Now proceed to create the dissolve/summary sinks (they will use the trimmed idDist)
                    diss_fields = QgsFields()
                    diss_fields.append(QgsField("obj_cnt", QVariant.Int))
                    diss_fields.append(QgsField("total_m", QVariant.Double))
                    diss_fields.append(QgsField("POLYGON_ID", QVariant.String))
                    diss_fields.append(QgsField("PDP_ID",     QVariant.String))
                    diss_fields.append(QgsField("MFG_ID",     QVariant.String))
                    sinkDD, idDistD = self.parameterAsSink(p, self.O_DIST_DISS, context,
                                                           diss_fields, QgsWkbTypes.MultiLineString, roads.crs())
                    

                    # --- Snap PDP pseudo points to graph ---
                    pdp_id_to_node = {}
                    pp_names = pseudo_pdp_L.fields().names()
                    for pf in pseudo_pdp_L.getFeatures():
                        pid = str(pf["pdp_id"]) if ("pdp_id" in pp_names and pf["pdp_id"] is not None) else None
                        if not pid or pid in pdp_id_to_node:
                            continue
                        sp = _point_of(pf.geometry())
                        if sp is None:
                            continue
                        nn = _nearest_node(sp, node_layer, idx_nodes, nid2node, _snap_for_idx)
                        if not nn:
                            continue
                        exact = (sp.x(), sp.y())
                        Gd.add_edge(nn, exact, weight=0.01)
                        pdp_id_to_node[pid] = exact

                    if not pdp_id_to_node:
                        feedback.reportError("⚠️ Stage-9: No PDPs snapped — skipping Distribution.")
                    else:
                        # --- Group HH objects by their PDP id (strict) ---
                        objs_by_pid = {}
                        hh_names = pseudo_hh_L.fields().names()
                        # Prefer addr_id: pseudo-HH 'hh_id' carries the household
                        # COUNT (copied from garden 'hhs'), not an identifier.
                        obj_id_field = "addr_id" if "addr_id" in hh_names else ("hh_id" if "hh_id" in hh_names else None)
                        if not obj_id_field:
                            obj_id_field = "obj_id" if "obj_id" in hh_names else None
                        
                        skipped_no_pdp = []
                        skipped_no_node = 0
                        for of in pseudo_hh_L.getFeatures():
                            pid = str(of["pdp_id"]) if ("pdp_id" in hh_names and of["pdp_id"] is not None) else None
                            if not pid or pid not in pdp_id_to_node:
                                skipped_no_pdp.append(
                                    str(of[obj_id_field]) if (obj_id_field and of[obj_id_field] is not None) else str(of.id())
                                )
                                continue
                            pt = _point_of(of.geometry())
                            if pt is None:
                                continue
                            nn = _nearest_node(pt, node_layer, idx_nodes, nid2node, _snap_for_idx)
                            if not nn:
                                skipped_no_node += 1
                                continue
                            exact = (pt.x(), pt.y())
                            Gd.add_edge(nn, exact, weight=0.01)
                            objs_by_pid.setdefault(pid, []).append((of, exact))

                        if skipped_no_pdp:
                            _preview = ", ".join(skipped_no_pdp[:20]) + ("…" if len(skipped_no_pdp) > 20 else "")
                            feedback.pushWarning(
                                f"⚠️ Stage-9: {len(skipped_no_pdp)} household(s) skipped — no/unknown PDP link "
                                f"(check POLYGON_ID/PDP_ID on the Objects layer): {_preview}"
                            )
                        if skipped_no_node:
                            feedback.pushWarning(
                                f"⚠️ Stage-9: {skipped_no_node} household(s) skipped — no graph node within snap tolerance."
                            )

                        total_paths = 0
                        
                        per_pdp_geoms = defaultdict(list)
                        per_pdp_cnt = defaultdict(int)
                        per_pdp_len = defaultdict(float)

                        for pid, items in objs_by_pid.items():
                            src = pdp_id_to_node.get(pid)
                            if not src:
                                continue
                            try:
                                _, paths = nx.single_source_dijkstra(Gd, src, weight="weight")
                            except Exception:
                                paths = nx.single_source_shortest_path(Gd, src)

                            for of, tgt in items:
                                pnodes = paths.get(tgt)
                                if not pnodes or len(pnodes) < 2:
                                    continue
                                coords = [QgsPointXY(n[0], n[1]) for n in pnodes]
                                geom = QgsGeometry.fromPolylineXY(coords)
                                if not _geom_ok(geom) or geom.length() < 0.01:
                                    continue
                                
                                f = QgsFeature(lines_fields)
                                f.setGeometry(geom)
                                f["pdp_id"]   = pid
                                _addr_val = (str(of[obj_id_field]) if obj_id_field and of[obj_id_field] is not None else "")
                                f["obj_id"]   = _addr_val
                                f["addr_id"]  = _addr_val
                                f["length_m"] = round(geom.length(), 2)
                                # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                                poly_lu, pdp_lu = (pdp_id_lookup.get(pid, (None, None)) if pid and pdp_id_lookup else (None, None))
                                f["POLYGON_ID"] = poly_lu
                                f["PDP_ID"] = pdp_lu or pid
                                f["MFG_ID"] = mfg_id_val
                                sinkDL.addFeature(f)
                                # Also register tangent drills used by distribution trenches
                                _mark_used_tangents(geom)
                                per_pdp_geoms[pid].append(geom)
                                per_pdp_cnt[pid] += 1
                                per_pdp_len[pid] += geom.length()
                                total_paths += 1

                        for pid, geoms in per_pdp_geoms.items():

                            merged = _unary_union_geoms(geoms)
                            if not merged or merged.isEmpty():
                                continue
                            
                            df = QgsFeature(diss_fields)
                            df.setGeometry(merged)
                            df["pdp_id"]  = pid
                            df["obj_cnt"] = int(per_pdp_cnt[pid])
                            df["total_m"] = round(float(per_pdp_len[pid]), 2)
                            # Propagate POLYGON_ID / PDP_ID from NetworkManager lookup
                            poly_lu, pdp_lu = (pdp_id_lookup.get(pid, (None, None)) if pid and pdp_id_lookup else (None, None))
                            df["POLYGON_ID"] = poly_lu
                            df["PDP_ID"] = pdp_lu or pid
                            df["MFG_ID"] = mfg_id_val
                            sinkDD.addFeature(df)

                        feedback.pushInfo(f"✅ Stage-9: Created {total_paths} distribution paths across {len(per_pdp_geoms)} PDPs.")

                except RuntimeError:
                    # already reported above; continue pipeline gracefully
                    pass
                except Exception as e:
                    feedback.reportError(f"❌ Stage-9 Distribution error: {e}")

            else:
                feedback.pushInfo("ℹ️ Stage-9: Distribution skipped (no HHs or missing PDP ID field).")

        else:
            # Create empty optional outputs for stability
            sinkG, idGarden = self.parameterAsSink(p, self.O_GARDEN, context, QgsFields(), QgsWkbTypes.LineString, roads.crs())
            sinkPH, idPseudoHH = self.parameterAsSink(p, self.O_PSEUDO_HH, context, QgsFields(), QgsWkbTypes.Point, roads.crs())
            sinkDL, idDist = self.parameterAsSink(p, self.O_DIST_LINES, context, QgsFields(), QgsWkbTypes.LineString, roads.crs())
            sinkDD, idDistD = self.parameterAsSink(p, self.O_DIST_DISS, context, QgsFields(), QgsWkbTypes.MultiLineString, roads.crs())
            feedback.pushInfo("ℹ️ No Households provided or empty — skipping Garden/Distribution.")
        

        # --------------------------------------------------------
        # PATCH C — Final Tangent Trenches + Used Crossings
        # Ensure Final Tangent Trenches exists even if empty,
        # and export "used" tangents when available.
        # --------------------------------------------------------

        # --- Final Tangent Trenches (always create the layer, even if empty) ---
        ft_fields = QgsFields()
        ft_fields.append(QgsField("id", QVariant.Int))

        sinkFT, idFT = self.parameterAsSink(
            p, self.O_FINAL_TAN, context, ft_fields, QgsWkbTypes.LineString, roads.crs()
        )

        # If we produced tangent candidates (tan_tmp), copy them in; otherwise we still keep an empty table
        if 'tan_tmp' in locals() and tan_tmp and tan_tmp.featureCount() > 0:
            # If tan_tmp has its own fields, we’ll minimally map geometry + id into our standardized schema
            for t in tan_tmp.getFeatures():
                of = QgsFeature(ft_fields)
                of.setGeometry(t.geometry())
                try:
                    of["id"] = int(t["id"]) if "id" in tan_tmp.fields().names() else int(t.id())
                except Exception:
                    of["id"] = int(t.id())
                sinkFT.addFeature(of)
            feedback.pushInfo(f"✅ Final Tangent Trenches: {tan_tmp.featureCount()} feature(s) written.")
        else:
            feedback.pushInfo("ℹ️ Final Tangent Trenches: created empty layer (no tangent candidates).")

        # --- Used crossings layer (materialize only the tangents that were actually used) ---
        tan_used_fields = QgsFields()
        tan_used_fields.append(QgsField("id", QVariant.Int))
        sinkTanUsed, idTanUsed = self.parameterAsSink(
            p, self.O_TANGENTS_USED, context, tan_used_fields, QgsWkbTypes.LineString, roads.crs()
        )

        if 'tan_tmp' in locals() and tan_tmp and 'used_tan_ids' in locals() and used_tan_ids:
            for fid in set(used_tan_ids):
                tf = next(tan_tmp.getFeatures(QgsFeatureRequest(fid)), None)
                if not tf:
                    continue
                of = QgsFeature(tan_used_fields)
                of.setGeometry(tf.geometry())
                try:
                    of["id"] = int(tf["id"]) if "id" in tan_tmp.fields().names() else int(fid)
                except Exception:
                    of["id"] = int(fid)
                sinkTanUsed.addFeature(of)
            feedback.pushInfo(f"✅ {len(set(used_tan_ids))} used tangent crossing(s) exported.")
        else:
            feedback.pushInfo("ℹ️ No used tangent crossings to export.")

        # --- Materialize the layers for downstream steps (ids may be None if sinks failed) ---
        final_tangents_layer = _materialize_layer(idFT, context, feedback, "Final_Tangent_Trenches") if idFT else None
        drills_used = _materialize_layer(idTanUsed, context, feedback, "Drill_Used_Layer") if idTanUsed else None


        # --------------------------------------------------------
        # Final_Trenches (helper-style): tag + merge ONLY Feeder / Garden / Distribution (+ USED drills)
        # --------------------------------------------------------
        final_id = None
        final_tan_id = None
        try:
            # Materialize inputs (Feeder, Garden, Distribution)
            _feeder = _materialize_layer(idFD, context, feedback, "feeder_mem") if 'idFD' in locals() and idFD else None
            _garden = _materialize_layer(idGarden, context, feedback, "garden_mem") if 'idGarden' in locals() and idGarden else None
            _dist   = _materialize_layer(idDist, context, feedback, "dist_mem") if 'idDist' in locals() and idDist else None

            # Define helper to add a trench_type attribute to each feature
            def _tag_simple(layer, type_label):
                """Return a memory layer with 'trench_type' field set to type_label."""
                res = processing.run(
                    "native:fieldcalculator",
                    {
                        "INPUT": layer,
                        "FIELD_NAME": "trench_type",
                        "FIELD_TYPE": 2,
                        "FIELD_LENGTH": 64,
                        "FIELD_PRECISION": 0,
                        "NEW_FIELD": True,
                        "FORMULA": f"'{type_label}'",
                        "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
                    },
                    is_child_algorithm=True, context=context, feedback=feedback
                )["OUTPUT"]
                return as_layer(res, context, f"tagged_{type_label}")

            # Combine available trench types (tagging for downstream BOQ)
            parts = []
            if _feeder and _feeder.featureCount():
                parts.append(_tag_simple(_feeder, "Feeder"))
            if _garden and _garden.featureCount():
                parts.append(_tag_simple(_garden, "Garden"))
            if _dist and _dist.featureCount():
                parts.append(_tag_simple(_dist, "Distribution"))

            # NOTE: no separate Connector stubs anymore — feeder features now
            # physically span MFG → PDP (device points embedded in the route),
            # and distribution starts at the pseudo-PDP which lies on that route.

            # Keep tangent drills out of Final_Trenches; they are published separately
            # via OUT_FINAL_TANGENT_TRENCHES / OUT_TANGENTS_USED.

            if parts:
                merged_final = as_layer(
                    processing.run(
                        "native:mergevectorlayers",
                        {"LAYERS": parts, "CRS": roads.crs().authid(), "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        is_child_algorithm=True, context=context, feedback=feedback
                    )["OUTPUT"],
                    context, "Final_Trenches"
                )

                # Force MultiLineString container (best-effort) for safer downstream handling.
                # promotetomulti keeps one feature per trench (collect would collapse
                # everything into a single feature and lose trench_type granularity).
                try:
                    merged_final = as_layer(
                        processing.run(
                            "native:promotetomulti",
                            {"INPUT": merged_final, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                            is_child_algorithm=True, context=context, feedback=feedback
                        )["OUTPUT"],
                        context, "Final_Trenches_multi"
                    )
                except Exception:
                    pass

                # --- Final Tangent Trenches (USED drills only) -> mirrors Tangent_Used sublayer ---
                try:
                    if drills_used:
                        sinkFT, final_tan_id = self.parameterAsSink(
                            p, self.O_FINAL_TAN, context,
                            drills_used.fields(),
                            drills_used.wkbType(),
                            drills_used.crs()
                        )
                        if sinkFT:
                            for f in drills_used.getFeatures():
                                sinkFT.addFeature(f)
                except Exception:
                    final_tan_id = None  # best-effort; don't block main pipeline
                # If no USED drills were written, still allocate an empty Final_Tangent_Trenches sink for stability
                if not final_tan_id:
                    sinkFT, final_tan_id = self.parameterAsSink(
                        p, self.O_FINAL_TAN, context, QgsFields(), QgsWkbTypes.LineString, roads.crs()
                    )

                # Final safety: remove any trench parts inside buildings
                if bldg_bufL and merged_final:
                    try:
                        merged_final = _erase_or_difference(merged_final, bldg_bufL, context, feedback, "Final_Trenches_no_bldg")
                    except Exception as _e:
                        feedback.reportError(f"Final trim vs buildings failed; continuing. Error: {_e}")

                # Write Final_Trenches with POLYGON_ID / PDP_ID propagation
                # Build extended fields including POLYGON_ID and PDP_ID
                ext_fields = QgsFields()
                for field in merged_final.fields():
                    if field.name() not in ("POLYGON_ID", "PDP_ID"):
                        ext_fields.append(QgsField(field.name(), field.type(), field.typeName(), field.length(), field.precision()))
                ext_fields.append(QgsField("POLYGON_ID", QVariant.String))
                ext_fields.append(QgsField("PDP_ID", QVariant.String))

                sinkFinal, final_id = self.parameterAsSink(
                    p, self.O_FINAL, context,
                    ext_fields,
                    merged_final.wkbType() if merged_final.wkbType() != QgsWkbTypes.Unknown else QgsWkbTypes.LineString,
                    roads.crs()
                )
                if sinkFinal:
                    # POLYGON_ID / PDP_ID: copy straight from the merged features
                    # (the per-type layers already carry them). lookupField is
                    # case-insensitive; indexFromName is not and misses 'PDP_ID'.
                    src_fields = merged_final.fields()
                    poly_src = src_fields.lookupField("POLYGON_ID")
                    pdp_src = src_fields.lookupField("PDP_ID")

                    def _sval(v):
                        if v is None:
                            return None
                        s = str(v).strip()
                        return s if s and s.upper() != "NULL" else None

                    written = 0
                    for f in merged_final.getFeatures():
                        nf = QgsFeature(ext_fields)
                        nf.setGeometry(f.geometry())
                        # Copy all attributes from merged_final except POLYGON_ID/PDP_ID
                        # (those fields are appended separately below to avoid duplicates)
                        for field in merged_final.fields():
                            if field.name() not in ("POLYGON_ID", "PDP_ID"):
                                nf[field.name()] = f[field.name()]
                        poly_val = _sval(f[poly_src]) if poly_src >= 0 else None
                        pdp_val = _sval(f[pdp_src]) if pdp_src >= 0 else None
                        # Fallback: NetworkManager lookup keyed by the local pdp id
                        if pdp_val and not poly_val and pdp_id_lookup:
                            lu = pdp_id_lookup.get(pdp_val)
                            if lu:
                                poly_val = lu[0]
                                pdp_val = lu[1] or pdp_val
                        nf["POLYGON_ID"] = poly_val
                        nf["PDP_ID"] = pdp_val
                        sinkFinal.addFeature(nf)
                        written += 1
                    feedback.pushInfo(f"✅ Final_Trenches layer written with {written} features (incl. POLYGON_ID/PDP_ID).")
                else:
                    feedback.reportError("Could not allocate sink for Final_Trenches.")
            else:
                # Create empty sink for stability
                sinkFinal, final_id = self.parameterAsSink(
                    p, self.O_FINAL, context, QgsFields(), QgsWkbTypes.LineString, roads.crs()
                )
                feedback.pushInfo("ℹ️ No feeder/garden/distribution layers found — Final_Trenches empty.")

        except Exception as e:
            feedback.reportError(f"❌ Final_Trenches build failed: {e}")

        # --------------------------------------------------------
        # Outputs dict
        # --------------------------------------------------------
        out = {
            self.O_SIDE_L: idSL, self.O_SIDE_R: idSR,
            self.O_PDP_PROJ: idProj, self.O_PSEUDO_PDP: idPseudo,
            self.O_MFG: idMFG, self.O_INTER_BUFF: idIB if 'idIB' in locals() else None,
            self.O_TANGENTS: idTan if 'idTan' in locals() else None,
            self.O_TANGENTS_USED: idTanUsed,
            self.O_MFG_PDP: idMP, self.O_FEEDER: idFD,
            self.O_MERGED_PDP: idMergedPDP,
            self.O_FEEDER_FINAL: idFeederFinal,
        }

        # Stage 1 outputs
        out[self.O_S1_AOI_BUF_DISS]   = idS1_buf_diss if 'idS1_buf_diss' in locals() else None
        out[self.O_S1_AOI_OUTLINE]    = idS1_outline  if 'idS1_outline'  in locals() else None
        out[self.O_S1_ROADS_NEAR]     = idS1_roads_nr if 'idS1_roads_nr' in locals() else None
        out[self.O_S1_ROADS_FILTERED] = idS1_roads_fl if 'idS1_roads_fl' in locals() else None

        # Optional/conditional outputs
        if 'idTan' in locals():      out[self.O_TANGENTS] = idTan
        if idGarden:                 out[self.O_GARDEN] = idGarden
        if idPseudoHH:               out[self.O_PSEUDO_HH] = idPseudoHH
        if idDist:                   out[self.O_DIST_LINES] = idDist
        if idDistD:                  out[self.O_DIST_DISS] = idDistD
        if final_tan_id:             out[self.O_FINAL_TAN] = final_tan_id
        if final_id:                 out[self.O_FINAL] = final_id

        # --------------------------------------------------------
        # Final QA messages
        # --------------------------------------------------------
        if G.number_of_nodes() == 0:
            feedback.reportError("⚠️ Graph was empty — no routing performed. Check road/footway input classification and SNAP_TOL.")
        elif mfg_pt is None:
            feedback.pushInfo("ℹ️ MFG input omitted — feeder routing was skipped. Provide the MFG point layer to generate Feeder trenches.")
        elif mfg_node is None:
            feedback.reportError("⚠️ MFG did not snap to the graph — routing skipped. Increase SNAP_TOL or verify MFG location.")
        else:
            feedback.pushInfo("✅ All routing and trench layers generated successfully.")

        # --- ensure sink file handles are closed before QGIS tries to load the GPKG ---
        try:
            for _maybe in [
                "sinkF","sinkFD","sinkTan","sinkTanUsed","sinkL","sinkR",
                "sinkSide","sinkAoiBuf","sinkAoiLines","sinkRoadsNear",
                "sinkPseudoPDP","sinkMFG","sinkValidInt","sinkTrMfgPdp",
                "sinkPseudoHH","sinkDist","sinkDistD","sinkGarden","sinkFeeder",
                "sinkFT","sinkFinal"
            ]:
                s = locals().get(_maybe)
                if s:
                    del s
        except Exception:
            pass

        return out
