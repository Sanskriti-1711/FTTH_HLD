# -*- coding: utf-8 -*-
"""
Cable Builder — Feeder Cable (copy/style) + grouped Distribution Cable branches
• Copies Feeder Trench to Feeder Cable.
• Builds ONE grouped Distribution Cable per same-footway group: joins each object's
  Distribution Trench segment (PDP → footway point) with its Garden Trench
  segment (object → footway point) into a single PDP → object cable, tagged
  with the object's addr_id and household count (hhs).

Parameter surface slimmed 2026-07-03: only the three trench layers remain.
PDP fields are auto-detected (PDP_ID/pdp_id), CRS is the pipeline standard
EPSG:25833, snapping/linemerge/dedupe run with the fixed DEFAULT_* values,
and the styling/add-to-project cosmetics plus the dead OUT_MERGED_INPUTS
output were removed.
"""

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFeatureSink, QgsProcessingException,
    QgsFeatureSink, QgsFields, QgsField, QgsWkbTypes,
    QgsFeature, QgsProcessingUtils, QgsSymbol, QgsGeometry,
    QgsCoordinateReferenceSystem, QgsPointXY,
)
from qgis import processing

# --- utils imports ---
from ..utils.string_utils import normalize_key
from ..utils.fields import first_field_case_insensitive
from ..utils.layer_ops import (
    fix_geometries,
    reproject_if_needed,
    subset_by_id,
    snap_layer,
    linemerge_layer,
    find_first_alg,
)


def _polyline_of(geom: QgsGeometry):
    """Return the first polyline (list of QgsPointXY) of a line geometry."""
    if geom is None or geom.isEmpty():
        return None
    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
        return None
    if QgsWkbTypes.isMultiType(geom.wkbType()):
        parts = geom.asMultiPolyline()
        return parts[0] if parts else None
    pts = geom.asPolyline()
    return pts or None


def _pts_close(a, b, tol=0.01):
    return abs(a.x() - b.x()) < tol and abs(a.y() - b.y()) < tol


def _concat_polylines(*polys, tol=0.01):
    """Concatenate polylines (lists of QgsPointXY), dropping shared endpoints."""
    out = []
    for pts in polys:
        if not pts:
            continue
        if out and _pts_close(pts[0], out[-1], tol):
            pts = pts[1:]
        if pts:
            out.extend(pts)
    return out


def _join_object_cable(dist_geom: QgsGeometry, garden_geom: QgsGeometry, proj_geom=None):
    """
    Build one continuous PDP → object cable.

    ``dist_geom`` runs pseudo-PDP → footway point (Distribution Trench);
    ``garden_geom`` runs object → footway point (Garden Trench). They share
    the footway endpoint, so the joined polyline is
    ``dist_points + reversed(garden_points)[1:]``.

    When the PDP sits back from the street, ``proj_geom`` is the PDP →
    pseudo-PDP projection line and is prepended (PDP → pseudo-PDP → footway
    → object).
    """
    dpts = _polyline_of(dist_geom)
    gpts = _polyline_of(garden_geom)
    if not dpts or not gpts:
        return None
    # The garden runs object → footway; reverse it to footway → object.
    g_rev = list(reversed(gpts))
    parts = [dpts, g_rev]
    if proj_geom is not None:
        ppts = _polyline_of(proj_geom)
        if ppts:
            parts.insert(0, ppts)  # PDP → pseudo-PDP
    joined = _concat_polylines(*parts)
    if len(joined) < 2:
        return None
    return QgsGeometry.fromPolylineXY(joined)


class AlgCableBuilderAll(QgsProcessingAlgorithm):
    # --- Inputs ---
    FEEDER_SRC   = "FEEDER_TRENCH"
    GARDEN_L     = "GARDEN_TRENCHES"
    DISTR_L      = "DISTR_TRENCHES"
    PDP_PROJ     = "PDP_PROJECTIONS"   # optional: PDP→footway/Service lines

    # --- Outputs ---
    O_FEEDER     = "OUT_FEEDER_CABLE"
    O_DIST       = "OUT_DISTRIBUTION_CABLE"

    # --- Fixed defaults (formerly UI parameters) ---
    DEFAULT_CRS_AUTHID = "EPSG:25833"
    DEFAULT_SNAP_M     = 0.5      # snap tolerance (m) within PDP group
    DEFAULT_DO_MERGE   = True     # merge contiguous lines (linemerge)
    DEFAULT_DO_DEDUPE  = True     # remove duplicate geometries
    DEFAULT_DIST_COLOR = "#0000ff"
    DEFAULT_DIST_WIDTH = 0.8
    SAME_FOOTWAY_TOL_M = 0.5
    RESERVED_SPARE_FIBERS = 2

    # ------------------------- UI -------------------------
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.FEEDER_SRC, "Feeder Trench (source layer)", [QgsProcessing.TypeVectorAnyGeometry]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GARDEN_L, "Garden Trenches (lines; PDP_ID auto-detected)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.DISTR_L, "Distribution Trenches (lines; PDP_ID auto-detected)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.PDP_PROJ, "PDP→Footway projections (optional)", [QgsProcessing.TypeVectorLine],
            optional=True,
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_FEEDER, "Feeder Cable"
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_DIST, "Distribution Cable (per object)", QgsProcessing.TypeVectorLine
        ))

    # ------------------------ run -------------------------
    def processAlgorithm(self, p, context, feedback):
        feeder_src = self.parameterAsVectorLayer(p, self.FEEDER_SRC, context)
        if feeder_src is None:
            raise QgsProcessingException("Feeder Trench layer is required.")

        # Copy feeder trench → feeder cable
        f_fields, f_wkb, f_crs = feeder_src.fields(), feeder_src.wkbType(), feeder_src.crs()
        sinkF, outFeederId = self.parameterAsSink(p, self.O_FEEDER, context, f_fields, f_wkb, f_crs)
        copied = 0
        for f in feeder_src.getFeatures():
            nf = QgsFeature(f_fields)
            nf.setGeometry(f.geometry())
            nf.setAttributes(f.attributes())
            sinkF.addFeature(nf, QgsFeatureSink.FastInsert)
            copied += 1
        feedback.pushInfo(f"Feeder: copied {copied} features.")

        # --- Distribution build: one cable per object (PDP → object) ---
        garden = self.parameterAsVectorLayer(p, self.GARDEN_L, context)
        distr = self.parameterAsVectorLayer(p, self.DISTR_L, context)

        # Auto-detect the object linkage fields (pickers removed from the UI).
        # The Garden Trench runs object → footway point; the Distribution Trench
        # runs PDP → the SAME footway point. Both carry the object's addr_id.
        fld_g_addr = first_field_case_insensitive(garden, ["addr_id", "ADDR_ID", "hh_id", "object_id", "OBJ_ID"])
        fld_d_addr = first_field_case_insensitive(distr, ["addr_id", "ADDR_ID", "obj_id", "object_id"])
        fld_g_hhs = first_field_case_insensitive(garden, ["hhs", "hh", "HH", "households", "HOUSEHOLDS"])
        fld_g_pdp = first_field_case_insensitive(garden, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        fld_g_poly = first_field_case_insensitive(garden, ["POLYGON_ID", "polygon_id"])
        fld_g_mfg = first_field_case_insensitive(garden, ["MFG_ID", "mfg_id"])
        fld_d_pdp = first_field_case_insensitive(distr, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        if not fld_g_addr or not fld_d_addr:
            raise QgsProcessingException(
                "Garden/Distribution trenches need an addr_id (or obj_id) field to build per-object cables — run stage 04 first."
            )
        feedback.pushInfo(
            f"Auto-detected fields → Garden addr: '{fld_g_addr}', Distribution addr: '{fld_d_addr}', HH: '{fld_g_hhs or '-'}'"
        )

        crs_t = QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID)

        garden_t = reproject_if_needed(fix_geometries(garden, context, feedback), crs_t, context, feedback)
        distr_t = reproject_if_needed(fix_geometries(distr, context, feedback), crs_t, context, feedback)

        # NOTE: GeoPackage field names are case-insensitive, so we cannot have
        # both 'pdp_id' and 'PDP_ID' — keep only the uppercase PDP_ID used by
        # the rest of the pipeline.
        out_fields = QgsFields()
        out_fields.append(QgsField("addr_id",    QMetaType.Type.QString))
        out_fields.append(QgsField("ADDR_IDS",   QMetaType.Type.QString))
        out_fields.append(QgsField("hhs",        QMetaType.Type.QString))
        out_fields.append(QgsField("HH_COUNT",   QMetaType.Type.Int))
        out_fields.append(QgsField("FIBER_COUNT", QMetaType.Type.Int))
        out_fields.append(QgsField("RESERVED_SPARE_FIBERS", QMetaType.Type.Int))
        out_fields.append(QgsField("AVAILABLE_FIBERS", QMetaType.Type.Int))
        out_fields.append(QgsField("CONNECTION_TYPE", QMetaType.Type.QString))
        out_fields.append(QgsField("length_m",   QMetaType.Type.Double))
        out_fields.append(QgsField("POLYGON_ID", QMetaType.Type.QString))
        out_fields.append(QgsField("PDP_ID",     QMetaType.Type.QString))
        out_fields.append(QgsField("MFG_ID",     QMetaType.Type.QString))
        sinkD, outDistId = self.parameterAsSink(p, self.O_DIST, context, out_fields, QgsWkbTypes.MultiLineString, crs_t)

        # Index distribution segments by normalized addr_id
        distr_by_addr = {}
        for f in distr_t.getFeatures():
            key = normalize_key(f[fld_d_addr])
            if key:
                distr_by_addr.setdefault(key, []).append(f)

        # Optional PDP→footway projection lines, indexed by normalized PDP id.
        proj_layer = self.parameterAsVectorLayer(p, self.PDP_PROJ, context)
        proj_by_pdp = {}
        if proj_layer is not None:
            fld_proj_pdp = first_field_case_insensitive(
                proj_layer, ["pdp_id", "PDP_ID", "pdp_pol_id", "pDp_POL_ID"])
            proj_t = reproject_if_needed(
                fix_geometries(proj_layer, context, feedback), crs_t, context, feedback)
            for f in proj_t.getFeatures():
                pid = normalize_key(f[fld_proj_pdp]) if fld_proj_pdp else None
                if pid:
                    proj_by_pdp.setdefault(pid, []).append(f.geometry())

        made = 0
        grouped = {}
        for gf in garden_t.getFeatures():
            gpts = _polyline_of(gf.geometry())
            if not gpts:
                continue
            key = (normalize_key(gf[fld_g_pdp]) if fld_g_pdp else "", normalize_key(gf[fld_g_poly]) if fld_g_poly else "", round(gpts[-1].x() / self.SAME_FOOTWAY_TOL_M), round(gpts[-1].y() / self.SAME_FOOTWAY_TOL_M))
            grouped.setdefault(key, []).append(gf)

        for group in grouped.values():
            # Resolve every house in this same-footway group. Each house keeps
            # its own distribution and garden arm; the shared distribution
            # corridor is emitted once as the MultiLineString trunk component.
            resolved = []
            for gf in group:
                key = normalize_key(gf[fld_g_addr])
                if not key:
                    continue
                dfeats = distr_by_addr.get(key, [])
                if not dfeats:
                    continue
                gg = gf.geometry()
                if not gg or gg.isEmpty():
                    continue
                g_pts = _polyline_of(gg)
                g_end = g_pts[-1] if g_pts else None
                df = dfeats[0]
                if len(dfeats) > 1 and g_end is not None:
                    best_df, best_d = None, None
                    for cand in dfeats:
                        c_pts = _polyline_of(cand.geometry())
                        if not c_pts:
                            continue
                        c_end = c_pts[-1]
                        dist2 = (c_end.x() - g_end.x()) ** 2 + (c_end.y() - g_end.y()) ** 2
                        if best_d is None or dist2 < best_d:
                            best_d, best_df = dist2, cand
                    if best_df is not None:
                        df = best_df
                dg = df.geometry()
                if not dg or dg.isEmpty():
                    continue
                pid = normalize_key(df[fld_d_pdp]) if fld_d_pdp else ""
                if not pid and fld_g_pdp:
                    pid = normalize_key(gf[fld_g_pdp])
                proj_geom = proj_by_pdp.get(pid, [None])[0] if pid else None
                arm = _join_object_cable(dg, gg, proj_geom)
                if arm is not None and not arm.isEmpty():
                    resolved.append((gf, df, arm, pid))

            if not resolved:
                continue
            # A MultiLineString deliberately preserves the common trunk and
            # each house arm as separate components, avoiding duplicate trunk
            # geometry while retaining per-house traceability.
            cable_parts = []
            for item in resolved:
                geom = item[2]
                if geom.isNull() or geom.isEmpty():
                    continue
                if geom.isMultipart():
                    cable_parts.extend(geom.asMultiPolyline())
                else:
                    cable_parts.append(geom.asPolyline())
            coordinates = [
                [[point.x(), point.y()] for point in part]
                for part in cable_parts if len(part) >= 2
            ]
            if not coordinates:
                continue
            first_gf, first_df, _first_arm, first_pid = resolved[0]
            of = QgsFeature(out_fields)
            of.setGeometry(QgsGeometry.fromMultiPolylineXY([
                [QgsPointXY(x, y) for x, y in part] for part in coordinates
            ]))
            addr_ids = [str(item[0][fld_g_addr]) for item in resolved]
            hh_values = []
            for item, _df, _arm, _pid in resolved:
                try:
                    hh_values.append(float(item[fld_g_hhs]) if fld_g_hhs and item[fld_g_hhs] not in (None, "") else 1.0)
                except Exception:
                    hh_values.append(1.0)
            hh_count = int(sum(hh_values))
            of["addr_id"]    = addr_ids[0]
            of["ADDR_IDS"]   = ",".join(addr_ids)
            of["hhs"]        = str(hh_count)
            of["HH_COUNT"]   = hh_count
            of["FIBER_COUNT"] = max(48, hh_count + self.RESERVED_SPARE_FIBERS)
            of["RESERVED_SPARE_FIBERS"] = self.RESERVED_SPARE_FIBERS
            of["AVAILABLE_FIBERS"] = max(0, of["FIBER_COUNT"] - self.RESERVED_SPARE_FIBERS - hh_count)
            of["CONNECTION_TYPE"] = "Shared trunk + branches" if len(group) > 1 else "Dedicated drop"
            of["length_m"]   = round(of.geometry().length(), 2)
            of["POLYGON_ID"] = str(first_gf[fld_g_poly]) if fld_g_poly else None
            of["PDP_ID"]     = first_pid or None
            of["MFG_ID"]     = str(first_gf[fld_g_mfg]) if fld_g_mfg else None
            sinkD.addFeature(of, QgsFeatureSink.FastInsert)
            made += 1

        # Style output
        out_layer = QgsProcessingUtils.mapLayerFromString(outDistId, context)
        if out_layer:
            sym = QgsSymbol.defaultSymbol(out_layer.geometryType())
            sym.setColor(QColor(self.DEFAULT_DIST_COLOR))
            try: sym.symbolLayer(0).setWidth(self.DEFAULT_DIST_WIDTH)
            except Exception: pass
            out_layer.renderer().setSymbol(sym)

        feedback.pushInfo(f"Distribution: grouped branched cables={made} (same-footway groups, tolerance={self.SAME_FOOTWAY_TOL_M} m)")
        return {
            self.O_FEEDER: outFeederId,
            self.O_DIST: outDistId,
        }

    # --- metadata ---
    def name(self): return "06_cable_layer"
    def displayName(self): return "Generate Cables"
    def group(self): return "06 Cable Layer"
    def groupId(self): return "06_cable_layer"
    def createInstance(self): return AlgCableBuilderAll()
