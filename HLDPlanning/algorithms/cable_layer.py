# -*- coding: utf-8 -*-
"""
Cable Builder — Feeder Cable (copy/style) + Distribution Cable (merge by PDP)
• Copies Feeder Trench to Feeder Cable.
• Merges Garden + Distribution Trenches by PDP into Distribution Cable, with snap/merge/dedupe.

Parameter surface slimmed 2026-07-03: only the three trench layers remain.
PDP fields are auto-detected (PDP_ID/pdp_id), CRS is the pipeline standard
EPSG:25833, snapping/linemerge/dedupe run with the fixed DEFAULT_* values,
and the styling/add-to-project cosmetics plus the dead OUT_MERGED_INPUTS
output were removed.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFeatureSink, QgsProcessingException,
    QgsFeatureSink, QgsFields, QgsField, QgsWkbTypes,
    QgsFeature, QgsProcessingUtils, QgsSymbol,
    QgsCoordinateReferenceSystem,
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

class AlgCableBuilderAll(QgsProcessingAlgorithm):
    # --- Inputs ---
    FEEDER_SRC   = "FEEDER_TRENCH"
    GARDEN_L     = "GARDEN_TRENCHES"
    DISTR_L      = "DISTR_TRENCHES"

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

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_FEEDER, "Feeder Cable"
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_DIST, "Distribution Cable (by PDP)", QgsProcessing.TypeVectorLine
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

        # --- Distribution build ---
        garden = self.parameterAsVectorLayer(p, self.GARDEN_L, context)
        distr = self.parameterAsVectorLayer(p, self.DISTR_L, context)

        # Auto-detect the PDP linkage fields (pickers removed from the UI)
        fld_g = first_field_case_insensitive(garden, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        fld_d = first_field_case_insensitive(distr, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        if not fld_g or not fld_d:
            raise QgsProcessingException(
                "Garden/Distribution trenches need a PDP_ID (or pdp_id) field — run stage 04 first."
            )
        feedback.pushInfo(f"Auto-detected PDP fields → Garden: '{fld_g}', Distribution: '{fld_d}'")

        crs_t = QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID)
        snap_m = self.DEFAULT_SNAP_M
        do_merge = self.DEFAULT_DO_MERGE
        do_dedupe = self.DEFAULT_DO_DEDUPE

        garden_t = reproject_if_needed(fix_geometries(garden, context, feedback), crs_t, context, feedback)
        distr_t = reproject_if_needed(fix_geometries(distr, context, feedback), crs_t, context, feedback)

        out_fields = QgsFields(); out_fields.append(QgsField("pdp_id", QVariant.String))
        sinkD, outDistId = self.parameterAsSink(p, self.O_DIST, context, out_fields, QgsWkbTypes.MultiLineString, crs_t)

        id_keys = set()
        for f in garden_t.getFeatures(): id_keys.add(normalize_key(f[fld_g]))
        for f in distr_t.getFeatures(): id_keys.add(normalize_key(f[fld_d]))
        id_keys.discard("")

        total_groups, made = len(id_keys), 0
        for i, key in enumerate(sorted(id_keys), 1):
            g_sub = subset_by_id(garden_t, fld_g, key, normalize_key)
            d_sub = subset_by_id(distr_t, fld_d, key, normalize_key)
            grp = processing.run("native:mergevectorlayers", {"LAYERS": [g_sub, d_sub], "CRS": crs_t, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT}, context=context, feedback=feedback)["OUTPUT"]
            snapped = snap_layer(grp, grp, snap_m, context, feedback)
            cleaned = fix_geometries(snapped, context, feedback)
            if do_merge:
                cleaned = linemerge_layer(cleaned, context, feedback)
            if do_dedupe:
                dd_alg = find_first_alg("native:deleteduplicategeometries", "qgis:deleteduplicategeometries")
                if dd_alg:
                    cleaned = processing.run(dd_alg, {"INPUT": cleaned, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT}, context=context, feedback=feedback)["OUTPUT"]

            for f in cleaned.getFeatures():
                of = QgsFeature(out_fields)
                of.setGeometry(f.geometry())
                of["pdp_id"] = key
                sinkD.addFeature(of, QgsFeatureSink.FastInsert)
                made += 1

            feedback.setProgress(100.0 * i / total_groups)

        # Style output
        out_layer = QgsProcessingUtils.mapLayerFromString(outDistId, context)
        if out_layer:
            sym = QgsSymbol.defaultSymbol(out_layer.geometryType())
            sym.setColor(QColor(self.DEFAULT_DIST_COLOR))
            try: sym.symbolLayer(0).setWidth(self.DEFAULT_DIST_WIDTH)
            except Exception: pass
            out_layer.renderer().setSymbol(sym)

        feedback.pushInfo(f"Distribution: PDP groups={total_groups}, parts={made}")
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
