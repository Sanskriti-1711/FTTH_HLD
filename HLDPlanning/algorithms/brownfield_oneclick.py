# -*- coding: utf-8 -*-
"""
Brownfield One-Click HLD Pipeline.

Extends the greenfield EndToEndPipelineAlgorithm with brownfield (existing
infrastructure) awareness.  Brownfield inputs appear first in the UI and
the "Enable brownfield" toggle defaults to ON.

Stage 0 always executes when brownfield input layers are provided,
regardless of toggle state.  The toggle gates whether downstream stages
(trench, network) actually use the registry for routing decisions.
"""

from qgis.PyQt.QtCore import QCoreApplication

from .oneclick import EndToEndPipelineAlgorithm


class BrownfieldOneClickAlgorithm(EndToEndPipelineAlgorithm):
    """One-Click pipeline for brownfield (existing infrastructure) areas.

    Inherits all stages from the greenfield pipeline and adds:
    - Dedicated brownfield input parameters (always visible, grouped first)
    - Default-enabled brownfield toggle
    - Stage 0 always runs when inputs are provided
    """

    # ── Identity (separate algorithm in QGIS toolbox) ────────────────────

    def name(self):
        return "brownfield_oneclick"

    def displayName(self):
        return self.tr("Brownfield – One-Click HLD Pipeline")

    def group(self):
        return self.tr("00 Brownfield")

    def groupId(self):
        return "00_brownfield"

    def shortHelpString(self):
        return self.tr(
            "Brownfield-aware HLD pipeline.  Loads existing infrastructure "
            "(ducts, chambers, PDPs, trenches, fibre) and prefers reuse over "
            "new construction during routing.\\n\\n"
            "All standard stages run automatically — the existing assets are "
            "injected into the routing graph at Stage 0 and respected by "
            "every downstream stage."
        )

    def createInstance(self):
        return BrownfieldOneClickAlgorithm()

    # ── Override: default the reuse toggle ON ────────────────────────────

    def initAlgorithm(self, config=None):
        """Same parameters as the greenfield pipeline, but the brownfield
        reuse toggle defaults to ON — this IS the brownfield pipeline."""
        super().initAlgorithm(config)
        try:
            p = self.parameterDefinition(self.P_USE_BROWNFIELD)
            if p is not None:
                p.setDefaultValue(True)
        except Exception:
            pass

    # ── Override: always run Stage 0 when inputs are present ─────────────

    def _run_stage_brownfield(self, parameters, context, steps,
                              feedback, results, out_dir):
        """Stage 0: Always load when brownfield layers are provided.

        Unlike the greenfield parent, this method ignores the
        P_USE_BROWNFIELD toggle — in brownfield mode you always want
        to see what exists on the map.  The toggle still gates whether
        downstream stages *reuse* those assets.
        """
        if feedback.isCanceled():
            return

        # Determine whether ANY brownfield input layer was provided
        has_inputs = any(
            self.parameterAsVectorLayer(parameters, key, context) is not None
            for key in (
                self.P_BF_DUCTS, self.P_BF_CHAMBERS, self.P_BF_POLES,
                self.P_BF_FIBRE, self.P_BF_CABINETS, self.P_BF_TRENCHES,
                self.P_BF_FEEDER_TRENCH, self.P_BF_DIST_TRENCH,
                self.P_BF_EXISTING_PDP, self.P_BF_EXISTING_MFG,
            )
        )

        if not has_inputs:
            results["brownfield_output"] = None
            results["brownfield_points"] = None
            feedback.pushInfo(self.tr(
                "  [timing] Brownfield: skipped (no input layers provided)."
            ))
            return

        # The toggle gates downstream *reuse*: the registry is still loaded so
        # the Existing Infrastructure outputs are produced, but every consumer
        # (routing, PDP snapping, classification) ignores it while disabled.
        use_bf = self.parameterAsBoolean(
            parameters, self.P_USE_BROWNFIELD, context)

        import time
        steps.setCurrentStep(0)
        feedback.pushInfo(self.tr(
            "[0%] Loading Brownfield (Existing) Infrastructure"
        ))
        t0 = time.time()

        bf_result = self._run(
            "Brownfield", self.run_brownfield_layer,
            parameters, context, steps, feedback,
        )
        results["brownfield_output"] = (
            bf_result.get("OUT_EXISTING_INFRA") if bf_result else None
        )
        results["brownfield_points"] = (
            bf_result.get("OUT_EXISTING_POINTS") if bf_result else None
        )

        elapsed = time.time() - t0
        has_lines = results.get("brownfield_output")
        has_points = results.get("brownfield_points")
        if has_lines or has_points:
            parts = []
            if has_lines:
                parts.append("lines")
            if has_points:
                parts.append("points")
            feedback.pushInfo(self.tr(
                f"  [timing] Brownfield: {elapsed:.3f}s ({', '.join(parts)})"
            ))
        else:
            feedback.pushInfo(self.tr(
                f"  [timing] Brownfield: skipped (no assets loaded) in {elapsed:.3f}s"
            ))

        # Save to output directory
        if results.get("brownfield_output"):
            results["brownfield_output"] = self._save_layer_to_gpkg(
                results["brownfield_output"],
                "Existing_Infrastructure.gpkg",
                out_dir, context, feedback,
            )
        if results.get("brownfield_points"):
            # Only persist the points layer when it actually contains assets
            # (e.g. chambers/PDPs/MFG) — avoids writing an empty GPKG when
            # the user supplied line-only brownfield inputs.
            try:
                from qgis.core import QgsProcessingUtils
                _pt = QgsProcessingUtils.mapLayerFromString(
                    results["brownfield_points"], context)
                _has = _pt is not None and _pt.featureCount() > 0
            except Exception:
                _has = True
            if _has:
                results["brownfield_points"] = self._save_layer_to_gpkg(
                    results["brownfield_points"],
                    "Existing_Infrastructure_Points.gpkg",
                    out_dir, context, feedback,
                )
            else:
                # No point assets loaded — drop the empty layer so the
                # Brownfield group does not show a featureless 'Existing Points'.
                results["brownfield_points"] = None

        # Apply the toggle to the shared registry state.  This is authoritative
        # for this pipeline — it overrides the 'reuse enabled by default' flag
        # that the Load Brownfield algorithm sets when it stores the registry.
        try:
            from ..utils.brownfield import BrownfieldRegistry
            BrownfieldRegistry.set_reuse_enabled(use_bf)
            feedback.pushInfo(self.tr(
                "  Brownfield reuse {} for downstream stages.".format(
                    "ENABLED" if use_bf else "DISABLED (toggle off)"
                )
            ))
        except Exception as exc:
            feedback.pushWarning(f"Could not apply brownfield toggle: {exc}")
