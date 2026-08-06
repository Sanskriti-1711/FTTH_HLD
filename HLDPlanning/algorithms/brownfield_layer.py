# -*- coding: utf-8 -*-
"""
Brownfield Layer — load existing infrastructure assets and produce a unified
'Existing Infrastructure' layer for downstream routing stages.

Loads:
  - Existing ducts (lines)        — with sub-duct capacity tracking
  - Existing chambers (points)   — splice points, manholes
  - Existing poles (points)      — overhead distribution
  - Existing fibre (lines)       — already laid fibre cables
  - Existing cabinets (points)   — street cabinets, PDP cabinets
  - Existing trenches (lines)    — already open/reinstatable trenches
  - Existing PDPs (points)       — pre-placed distribution points
  - Existing MFGs (points)       — pre-placed fibre gateway

Output:
  - Unified 'Existing Infrastructure' layer with ASSET_TYPE, INFRA_STATUS,
    VERIFY_STATUS, CAPACITY_USED, CAPACITY_TOTAL fields.
"""
import os

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    Qgis, QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber, QgsProcessingParameterString,
    QgsProcessingException, QgsWkbTypes, QgsFeature, QgsFeatureSink,
    QgsFields, QgsField, QgsGeometry, QgsProject,
)

from ..utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields
from ..utils.brownfield import BrownfieldRegistry, AssetType, InfraStatus, VerifyStatus


class BrownfieldLayerAlgorithm(QgsProcessingAlgorithm):
    """
    Stage 0: Load Brownfield (Existing) Infrastructure.

    Validates, normalises, and merges existing asset layers into a single
    in-memory registry used by downstream stages (Network, Trench, Cable, Duct).
    Also writes a unified 'Existing Infrastructure' output layer.
    """

    # ── Input parameter keys ────────────────────────────────────────────────
    P_DUCTS = "INPUT_DUCTS"
    P_CHAMBERS = "INPUT_CHAMBERS"
    P_POLES = "INPUT_POLES"
    P_FIBRE = "INPUT_FIBRE"
    P_CABINETS = "INPUT_CABINETS"
    P_TRENCHES = "INPUT_TRENCHES"
    P_FEEDER_TRENCH = "INPUT_FEEDER_TRENCH"
    P_DIST_TRENCH = "INPUT_DIST_TRENCH"
    P_EXISTING_PDP = "INPUT_EXISTING_PDP"
    P_EXISTING_MFG = "INPUT_EXISTING_MFG"

    P_DUCT_CAPACITY_FIELD = "DUCT_CAPACITY_FIELD"
    P_FIBRE_CAPACITY_FIELD = "FIBRE_CAPACITY_FIELD"
    P_CABINET_CAPACITY_FIELD = "CABINET_CAPACITY_FIELD"
    P_VERIFY_FIELD = "VERIFY_FIELD"

    # ── Output parameter keys ───────────────────────────────────────────────
    OUT_EXISTING = "OUT_EXISTING_INFRA"
    OUT_EXISTING_POINTS = "OUT_EXISTING_POINTS"

    # ── UI / Parameter definitions ──────────────────────────────────────────

    def tr(self, s):
        return QCoreApplication.translate("BrownfieldLayerAlgorithm", s)

    def name(self):
        return "00_brownfield_layer"

    def displayName(self):
        return self.tr("Load Brownfield (Existing) Infrastructure")

    def group(self):
        return self.tr("00 Brownfield")

    def groupId(self):
        return "00_brownfield"

    def createInstance(self):
        return BrownfieldLayerAlgorithm()

    def flags(self):
        """Hide from the Processing Toolbox.

        The one-click pipeline runs this stage internally via processing.run,
        so the standalone entry should not clutter the Toolbox — but it must
        stay registered for the pipeline to keep working.
        """
        flags = super().flags()
        try:
            flags |= Qgis.ProcessingAlgorithmFlag.HideFromToolbox
        except AttributeError:
            # QGIS < 3.32 — fall back to the deprecated enum.
            try:
                flags |= QgsProcessingAlgorithm.Flag.FlagHideFromToolbox
            except AttributeError:
                pass
        return flags

    def shortHelpString(self):
        return self.tr(
            "Load existing brownfield infrastructure layers (ducts, chambers, "
            "poles, fibre, cabinets, trenches, PDPs, MFGs). Produces a unified "
            "'Existing Infrastructure' layer and makes the brownfield registry "
            "available to downstream routing stages for asset reuse.\n\n"
            "All inputs are optional — provide only what you have."
        )

    def initAlgorithm(self, config=None):
        # ── Input layers ──────────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_DUCTS, self.tr("Existing Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_CHAMBERS, self.tr("Existing Chambers [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_POLES, self.tr("Existing Poles [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_FIBRE, self.tr("Existing Fibre [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_CABINETS, self.tr("Existing Cabinets [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TRENCHES, self.tr("Existing Trenches (legacy/merged) [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_FEEDER_TRENCH, self.tr("Existing Feeder Trenches [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_DIST_TRENCH, self.tr("Existing Distribution Trenches [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_EXISTING_PDP, self.tr("Existing PDPs [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_EXISTING_MFG, self.tr("Existing MFGs [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))

        # ── Optional field mappings ───────────────────────────────────────
        self.addParameter(QgsProcessingParameterString(
            self.P_DUCT_CAPACITY_FIELD,
            self.tr("Duct capacity field name (default: auto-detect)"),
            optional=True, defaultValue="",
        ))
        self.addParameter(QgsProcessingParameterString(
            self.P_FIBRE_CAPACITY_FIELD,
            self.tr("Fibre capacity field name (default: auto-detect)"),
            optional=True, defaultValue="",
        ))
        self.addParameter(QgsProcessingParameterString(
            self.P_CABINET_CAPACITY_FIELD,
            self.tr("Cabinet capacity field name (default: auto-detect)"),
            optional=True, defaultValue="",
        ))
        self.addParameter(QgsProcessingParameterString(
            self.P_VERIFY_FIELD,
            self.tr("Verification status field name (default: auto-detect)"),
            optional=True, defaultValue="",
        ))

        # ── Output (split by geometry type so both render correctly) ─────
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_EXISTING, self.tr("Existing Infrastructure — Lines"),
            optional=True, createByDefault=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_EXISTING_POINTS, self.tr("Existing Infrastructure — Points"),
            optional=True, createByDefault=True,
        ))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _param_layer(self, params, key, context):
        """Get an optional vector layer parameter."""
        try:
            return self.parameterAsVectorLayer(params, key, context)
        except Exception:
            return None

    def _param_str(self, params, key, context) -> str:
        """Get an optional string parameter."""
        try:
            val = self.parameterAsString(params, key, context)
            return val.strip() if val else ""
        except Exception:
            return ""

    def _auto_detect_field(self, layer, candidates):
        """Find the first field from candidates that exists in the layer."""
        if layer is None:
            return None
        names = {f.name().lower() for f in layer.fields()}
        for c in candidates:
            if c.lower() in names:
                # Return the actual case-correct field name
                for f in layer.fields():
                    if f.name().lower() == c.lower():
                        return f.name()
        return None

    # ── Processing logic ─────────────────────────────────────────────────────

    def processAlgorithm(self, params, context, feedback):
        # ── Resolve all optional layers ────────────────────────────────────
        ducts = self._param_layer(params, self.P_DUCTS, context)
        chambers = self._param_layer(params, self.P_CHAMBERS, context)
        poles = self._param_layer(params, self.P_POLES, context)
        fibre = self._param_layer(params, self.P_FIBRE, context)
        cabinets = self._param_layer(params, self.P_CABINETS, context)
        trenches = self._param_layer(params, self.P_TRENCHES, context)
        feeder_trench = self._param_layer(params, self.P_FEEDER_TRENCH, context)
        dist_trench = self._param_layer(params, self.P_DIST_TRENCH, context)
        existing_pdp = self._param_layer(params, self.P_EXISTING_PDP, context)
        existing_mfg = self._param_layer(params, self.P_EXISTING_MFG, context)

        # Determine project CRS from the first non-None layer
        crs = None
        for lyr in (ducts, chambers, poles, fibre, cabinets, trenches,
                     feeder_trench, dist_trench, existing_pdp, existing_mfg):
            if lyr is not None and lyr.isValid():
                crs = lyr.crs()
                break

        if crs is None:
            raise QgsProcessingException(
                self.tr("At least one brownfield layer must be provided.")
            )

        # ── Resolve field mappings ─────────────────────────────────────────
        duct_cap_field = self._param_str(params, self.P_DUCT_CAPACITY_FIELD, context)
        fibre_cap_field = self._param_str(params, self.P_FIBRE_CAPACITY_FIELD, context)
        cabinet_cap_field = self._param_str(params, self.P_CABINET_CAPACITY_FIELD, context)
        verify_field = self._param_str(params, self.P_VERIFY_FIELD, context)

        if not duct_cap_field:
            duct_cap_field = self._auto_detect_field(
                ducts, ["capacity_total", "capacity", "subducts", "sub_ducts", "n_subducts", "cap"]
            )
        if not fibre_cap_field:
            fibre_cap_field = self._auto_detect_field(
                fibre, ["capacity_total", "capacity", "strands", "fibre_count", "n_strands", "cap"]
            )
        if not cabinet_cap_field:
            cabinet_cap_field = self._auto_detect_field(
                cabinets, ["capacity_total", "capacity", "ports", "n_ports", "splitter_ports", "cap"]
            )
        if not verify_field:
            verify_field = self._auto_detect_field(
                ducts or chambers or poles or fibre or cabinets or trenches,
                ["verify_status", "status", "survey_status", "verification",
                 "field_status", "condition"]
            )

        feedback.pushInfo(self.tr(
            f"Brownfield: duct_cap_field='{duct_cap_field or '(default)'}', "
            f"fibre_cap_field='{fibre_cap_field or '(default)'}', "
            f"cabinet_cap_field='{cabinet_cap_field or '(default)'}', "
            f"verify_field='{verify_field or '(default)'}'"
        ))

        # ── Create registry and load all layers ────────────────────────────
        registry = BrownfieldRegistry(crs, feedback)

        loaded = 0
        loaded += registry.load_ducts(
            ducts, capacity_field=duct_cap_field or None, verify_field=verify_field or None
        )
        loaded += registry.load_chambers(
            chambers, verify_field=verify_field or None
        )
        loaded += registry.load_poles(
            poles, verify_field=verify_field or None
        )
        loaded += registry.load_fibre(
            fibre, capacity_field=fibre_cap_field or None, verify_field=verify_field or None
        )
        loaded += registry.load_cabinets(
            cabinets, capacity_field=cabinet_cap_field or None, verify_field=verify_field or None
        )
        loaded += registry.load_trenches(
            trenches, verify_field=verify_field or None
        )
        loaded += registry.load_trenches(
            feeder_trench, verify_field=verify_field or None
        )
        loaded += registry.load_trenches(
            dist_trench, verify_field=verify_field or None
        )
        loaded += registry.load_existing_pdps(
            existing_pdp, verify_field=verify_field or None
        )
        loaded += registry.load_existing_mfgs(
            existing_mfg, verify_field=verify_field or None
        )

        if loaded == 0:
            feedback.pushWarning(self.tr(
                "No brownfield assets were loaded. All provided layers were empty or invalid."
            ))

        feedback.pushInfo(registry.summary())

        # ── Store registry for downstream stages ───────────────────────────
        # Stored with reuse ENABLED by default: a standalone 'Load Brownfield'
        # run is meant to feed whichever pipeline runs next.  The brownfield
        # one-click pipeline later overrides the flag with its USE_BROWNFIELD
        # toggle (see BrownfieldOneClickAlgorithm._run_stage_brownfield).
        if registry.store_registry(registry):
            feedback.pushInfo(self.tr(
                "Brownfield registry stored on QgsProject for downstream stages "
                "(reuse enabled)."
            ))
        else:
            feedback.pushWarning(
                "Could not store brownfield registry on project for downstream stages."
            )

        # ── Write output layers (split by geometry type) ─────────────────
        out_fields = build_fields(THIN_PROFILES["EXISTING_INFRA"])

        # Line sink (ducts, trenches, fibre)
        sink_lines, out_id_lines = self.parameterAsSink(
            params, self.OUT_EXISTING, context,
            out_fields, QgsWkbTypes.MultiLineString, crs,
        )
        # Point sink (chambers, poles, cabinets, PDPs, MFGs)
        sink_points, out_id_points = self.parameterAsSink(
            params, self.OUT_EXISTING_POINTS, context,
            out_fields, QgsWkbTypes.MultiPoint, crs,
        )

        line_written = 0
        point_written = 0
        for asset_id in registry.asset_ids():
            a = registry.get_asset(asset_id)
            if a is None:
                continue
            geom = a["geom"]
            if not geom or geom.isEmpty():
                continue

            feat = QgsFeature(out_fields)
            feat.setGeometry(geom)
            feat[COMMON_FIELDS.ASSET_TYPE] = a["asset_type"]
            feat[COMMON_FIELDS.INFRA_STATUS] = InfraStatus.EXISTING
            feat[COMMON_FIELDS.VERIFY_STATUS] = a["verify_status"]
            feat[COMMON_FIELDS.CAPACITY_USED] = a["capacity_used"]
            feat[COMMON_FIELDS.CAPACITY_TOTAL] = a["capacity_total"]
            feat[COMMON_FIELDS.SRC_ID] = asset_id

            if AssetType.is_line(a["asset_type"]):
                if sink_lines is not None:
                    sink_lines.addFeature(feat, QgsFeatureSink.FastInsert)
                    line_written += 1
            else:
                if sink_points is not None:
                    sink_points.addFeature(feat, QgsFeatureSink.FastInsert)
                    point_written += 1

        feedback.pushInfo(self.tr(
            f"Existing Infrastructure output: {line_written} lines + "
            f"{point_written} points written."
        ))

        feedback.pushInfo(self.tr(
            f"Brownfield stage complete: {loaded} assets loaded."
        ))

        result = {}
        if out_id_lines:
            result[self.OUT_EXISTING] = out_id_lines
        if out_id_points:
            result[self.OUT_EXISTING_POINTS] = out_id_points
        return result
