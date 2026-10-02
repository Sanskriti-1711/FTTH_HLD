# -*- coding: utf-8 -*-
# pyright: reportMissingImports=false
"""Trench Layer (Designer) — the civil trench designer as the pipeline stage.

This is the **designer-backed** implementation of ``04_trench_layer``. It
answers the same interface (identical input parameters, identical output keys),
so nothing downstream can tell which engine produced the trenches; only the
*geometry source* changes:

    legacy    roads → sidewalks → route on that graph → clubbed tiers → sliced
    designer  plan (MFG / PDPs / Objects / Polygons) + OSM streets
              → class-weighted street routing → runs → structural nodes
              → spans between nodes, typed Open Cut / HDD / Garden

Why a separate algorithm rather than a branch inside ``trench_layer``: the
legacy stage is built entirely around sidewalk buffers, while the designer is an
independent module (``HLDPlanning/design/trench_design.py``) that shares no code
with it. Two algorithms means one can be diffed against the other on the same
project, and a bad run is switched back with a single environment variable
(``TRENCH_ENGINE``, read by ``oneclick.py``) instead of a revert.

Contract — what the consumers actually read
-------------------------------------------
``cable_layer`` auto-detects field names case-insensitively and **raises** when
a Garden/Distribution trench carries no object identity, so the attribute
mapping below is load-bearing, not cosmetic:

* ``Garden_Trench``        ``addr_id`` (+ ``hhs``) — one drop leg per premise.
* ``Distribution_Trench``  ``addr_id`` **per address**. The designer publishes a
  SHARED spine (one span naming every house routed along it, comma-joined)
  while ``cable_layer`` indexes that layer by a single address — so a
  comma-joined key matches nothing and NO distribution cable is built. The
  spine is fanned out per address in the copy this layer consumes; the shared
  geometry stays honest everywhere it is displayed or measured.
* ``Final_Trenches``       ``trench_type`` = Open Cut | HDD | Garden (the closed
  3-value set the platform, BOQ and LLD read), plus ``addr_id`` / ``hhs`` /
  ``MFG_ID`` / ``RUN_ID`` / ``START_CHAMBER`` / ``END_CHAMBER``.
* ``Tangent_Crossings``    ``id``-only legacy parity. The drills also exist
  inside ``Final_Trenches`` as HDD spans, which is what puts a chamber at both
  ends of every crossing.
* ``Pseudo_HH``            the footway end of every garden leg (derived).
* ``OUT_PDP_TO_SIDE``      PDP → nearest network point, as a line (derived).
* ``OUT_SIDEWALK_L/R``     ± 3.0 m offsets of the DESIGNED corridor (derived).
* ``OUT_S1_AOI_BUFFER_DISSOLVED``  Polygons + 100 m, dissolved (derived).

Those three derived layers are built from the designer's own geometry on
purpose. Reusing the legacy sidewalk layers would build the distribution duct
graph on legacy offsets while the trenches it must sit inside are the
designer's — ducts beside their trench, the ``ON_TRENCH = 0`` failure the duct
stage exists to prevent.

Aerial drops are deliberately left to the pole/aerial stages exactly as before
(``INPUT_AERIAL_ZONES`` is not part of the trench stage's surface): the designer
can classify aerial legs, but moving that classification into the trench stage
would change what the pole stage sees. That switch is a separate decision.
"""

import os
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingUtils,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis import processing

from .trench_layer import TrenchLayerAlgorithm
from ..utils.fields import first_field_case_insensitive as _pick_field
from ..utils.layer_io import as_layer as _as_layer
from ..utils.geometry_ops import unary_union_geoms as _unary_union_geoms
from ..utils.geom import merge_contiguous_runs as _merge_contiguous_runs

# ---------------------------------------------------------------------------
# Construction catalogue — mirrors HLDPlanning/utils/attr_enrich.py so a
# designer span carries the same civil attributes as a legacy one.
# ---------------------------------------------------------------------------
TRENCH_WIDTH_MM = {"Open Cut": 300, "HDD": 300, "Garden": 150}
TRENCH_DEPTH_MM = {"Open Cut": 900, "HDD": 900, "Garden": 450}

# Derived-layer defaults, taken from the legacy stage's fixed tunables
# (trench_layer.py: sw_off = 3.0, sw_seg = 8, aoi_buf_dist = 100.0) so the
# derived layers sit on the same footing as the ones they replace.
SIDEWALK_OFFSET_M = 3.0
SIDEWALK_SEGMENTS = 8
AOI_BUFFER_M = 100.0

_TIER_GEOM = {
    "Feeder": QgsWkbTypes.MultiLineString,        # legacy: MULTILINESTRING
    "Distribution": QgsWkbTypes.LineString,       # legacy: LINESTRING
    "Garden": QgsWkbTypes.LineString,             # legacy: LINESTRING
}

# Final_Trenches / tier schema. Only the fields the consumers read plus the
# civil catalogue are emitted; the platform keeps the whole property set in a
# generic ``properties`` JSONB column, so nothing is lost either way.
_FINAL_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("id", QMetaType.Type.QString),
    ("TRENCH_ID", QMetaType.Type.QString),
    ("RUN_ID", QMetaType.Type.QString),
    ("MFG_ID", QMetaType.Type.QString),
    ("trench_type", QMetaType.Type.QString),
    ("USAGE_TYPE", QMetaType.Type.QString),
    ("CONSTRUCT", QMetaType.Type.QString),
    ("TRENCH_TIER", QMetaType.Type.QString),
    ("obj_id", QMetaType.Type.QString),
    ("addr_id", QMetaType.Type.QString),
    ("hhs", QMetaType.Type.QString),
    ("HH", QMetaType.Type.Double),
    ("length_m", QMetaType.Type.Double),
    ("SPAN_LEN_M", QMetaType.Type.Double),
    ("PDP_ID", QMetaType.Type.QString),
    ("POLYGON_ID", QMetaType.Type.QString),
    ("START_CHAMBER", QMetaType.Type.QString),
    ("END_CHAMBER", QMetaType.Type.QString),
    ("SPAN_INDEX", QMetaType.Type.Int),
    ("SPAN_COUNT", QMetaType.Type.Int),
    ("SPAN_KIND", QMetaType.Type.QString),
    ("INFRA_STATUS", QMetaType.Type.QString),
    ("VERIFY_STATUS", QMetaType.Type.QString),
    ("SURFACE", QMetaType.Type.QString),
    ("REINSTATE", QMetaType.Type.QString),
    ("sidewalk", QMetaType.Type.QString),
    ("method", QMetaType.Type.QString),
    ("WIDTH_MM", QMetaType.Type.Int),
    ("DEPTH_MM", QMetaType.Type.Int),
    ("SRC", QMetaType.Type.QString),
    # AERIAL_ZONE is corridor-restriction EVIDENCE (the span crosses land where
    # underground is not permitted) and never a construction class: feeder and
    # distribution are UG-only, aerial is a drop-stage decision, so a mains span
    # keeps Open Cut / HDD / Garden here. The Aerial class lives on the
    # Aerial_Drops layer (TRENCH_TYPE = "Aerial", EXCAVATION = 0).
    ("AERIAL_ZONE", QMetaType.Type.Int),
    ("AERIAL_ZONE_REASON", QMetaType.Type.QString),
    # ── Tier provenance ────────────────────────────────────────────────────
    # `_ensure_backbone_reach` relabels a distribution spine to Feeder so the
    # MFG → every PDP path exists on the published trench. That relabel is
    # deliberate and must stay — but without a record of it a consumer cannot
    # tell a real trunk from a promoted distribution spine, so anything that
    # joins a duct or cable back to its tier trench (BOQ by tier, tier
    # colouring, permit tier rules) mis-attributes ~24 % of ducts and ~50 % of
    # cables. Nothing is geometrically wrong; only the label is ambiguous.
    #
    #   PROMOTED_FROM  the tier the span had BEFORE the relabel, empty for a
    #                  span that was always what its TRENCH_TIER says
    #   SERVES_TIER    the same fact as a value to group by directly, i.e.
    #                  `PROMOTED_FROM or TRENCH_TIER`, derived in one place
    #                  (`_stamp_tier_provenance`) so the two cannot disagree
    ("PROMOTED_FROM", QMetaType.Type.QString),
    ("SERVES_TIER", QMetaType.Type.QString),
)

_DRILL_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("id", QMetaType.Type.QString),
    ("DRILL_ID", QMetaType.Type.QString),
    ("ROAD_CLASS", QMetaType.Type.QString),
    ("WIDTH_M", QMetaType.Type.Double),
    ("TRENCH_TYPE", QMetaType.Type.QString),
    ("INFRA_STATUS", QMetaType.Type.QString),
)

# Aerial legs the designer classified: not trenches, so they are published on
# their own layer with the reason they were not dug. ``TRENCH_TYPE`` is
# "Aerial" and ``TRENCH_TIER`` stays "Garden" — an aerial leg takes the place
# of the garden drop it replaces, it just is not excavated.
_AERIAL_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("id", QMetaType.Type.QString),
    ("DROP_ID", QMetaType.Type.QString),
    ("POLYGON_ID", QMetaType.Type.QString),
    ("PDP_ID", QMetaType.Type.QString),
    ("MFG_ID", QMetaType.Type.QString),
    ("addr_id", QMetaType.Type.QString),
    ("HH", QMetaType.Type.Double),
    ("TRENCH_TIER", QMetaType.Type.QString),
    ("TRENCH_TYPE", QMetaType.Type.QString),
    # Aerial is a METHOD: Overhead, never excavated. All the fields are carried
    # so the BOQ / platform / LLD never have to infer it from the type string.
    ("CONSTRUCTION_METHOD", QMetaType.Type.QString),
    # The three construction-class aliases every consumer reads (docs/stages/
    # AERIAL.md §2). Without them the layer only had TRENCH_TYPE, so a consumer
    # reading CONSTRUCT/USAGE_TYPE — the BOQ, the permit TMP rules, the platform
    # attribute table — saw an aerial span with no class at all.
    ("USAGE_TYPE", QMetaType.Type.QString),
    ("CONSTRUCT", QMetaType.Type.QString),
    # No section: an aerial span is not excavated, so a width/depth would be a
    # fabricated trench. 0 = not applicable, never billed as a section.
    ("WIDTH_MM", QMetaType.Type.Int),
    ("DEPTH_MM", QMetaType.Type.Int),
    ("length_m", QMetaType.Type.Double),
    ("AERIAL_REASON", QMetaType.Type.QString),
    ("INFRA_STATUS", QMetaType.Type.QString),
    ("VERIFY_STATUS", QMetaType.Type.QString),
    ("EXCAVATION", QMetaType.Type.Int),
    ("SRC", QMetaType.Type.QString),
)

# Structural nodes the designer placed (Trench_Nodes): the points where the
# network changes tier or construction method. Published so the chamber stage
# places its structures on real structure — HDD pit → bore, junction →
# manhole/handhole, PDP → splitter chamber, bend / pull point → handhole —
# instead of guessing them from wherever duct geometries happen to touch.
_NODE_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("id", QMetaType.Type.QString),
    ("NODE_ID", QMetaType.Type.QString),
    ("NODE_TYPE", QMetaType.Type.QString),
    ("PRIORITY", QMetaType.Type.Int),
    ("PDP_ID", QMetaType.Type.QString),
    ("MFG_ID", QMetaType.Type.QString),
    ("X", QMetaType.Type.Double),
    ("Y", QMetaType.Type.Double),
    ("SRC", QMetaType.Type.QString),
)

_PSEUDO_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("pdp_pol_id", QMetaType.Type.QString),
    ("addr_id", QMetaType.Type.QString),
    ("hh_id", QMetaType.Type.QString),
    ("sidewalk", QMetaType.Type.QString),
    ("side", QMetaType.Type.QString),
    ("method", QMetaType.Type.QString),
    ("dist_m", QMetaType.Type.Double),
    ("proj_fid", QMetaType.Type.Int),
    # NOTE: only ONE spelling of the PDP id — GeoPackage field names are
    # case-insensitive, so declaring both "pdp_id" and "PDP_ID" makes the
    # layer fail to create. Consumers look it up case-insensitively.
    ("pdp_id", QMetaType.Type.QString),
    ("POLYGON_ID", QMetaType.Type.QString),
    ("MFG_ID", QMetaType.Type.QString),
)

_PROJ_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("PDP_ID", QMetaType.Type.QString),
    ("POLYGON_ID", QMetaType.Type.QString),
    ("MFG_ID", QMetaType.Type.QString),
    ("distance_m", QMetaType.Type.Double),
)


def _tr(text: str) -> str:
    return QCoreApplication.translate("TrenchDesignLayer", text)


def _fields(spec) -> QgsFields:
    out = QgsFields()
    for name, typ in spec:
        out.append(QgsField(name, typ))
    return out


def _as_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    f = _as_float(value)
    return int(f) if f is not None else None


def _first_line(geom: QgsGeometry) -> Optional[QgsGeometry]:
    """The single LineString inside a (possibly multi) line geometry."""
    if geom is None or geom.isEmpty():
        return None
    if not geom.isMultipart():
        return geom
    parts = geom.asMultiPolyline()
    if not parts:
        return None
    return QgsGeometry.fromPolylineXY([QgsPointXY(p.x(), p.y()) for p in parts[0]])


def _qgs_fields_of(layer: Optional[QgsVectorLayer]) -> QgsFields:
    return layer.fields() if layer is not None else QgsFields()


class TrenchDesignLayerAlgorithm(TrenchLayerAlgorithm):
    """``04_trench_layer`` implemented by the civil trench designer."""

    def name(self) -> str:
        return "04_trench_design_layer"

    def displayName(self) -> str:
        return _tr("Generate Trenches (Designer)")

    def group(self) -> str:
        return _tr("04 Trench Layer")

    def groupId(self) -> str:
        return "04_trench_layer"

    def createInstance(self):
        return TrenchDesignLayerAlgorithm()

    def shortHelpString(self) -> str:
        return _tr(
            "Designs the trench network from the plan (MFG / PDPs / Objects / "
            "Polygons + OSM streets) as node-to-node spans typed Open Cut / HDD "
            "/ Garden, then publishes the same layer and field contract as the "
            "legacy trench layer. Same parameters and outputs as "
            "04_trench_layer, so the rest of the pipeline is unchanged — pick "
            "the engine with TRENCH_ENGINE."
        )

    # Same parameter + output surface as the legacy stage. Delegating instead
    # of re-declaring means the two engines cannot drift apart.
    def initAlgorithm(self, config=None):
        TrenchLayerAlgorithm.initAlgorithm(self, config)

    # ------------------------------------------------------------------ run
    def processAlgorithm(self, parameters, context, feedback):
        work = tempfile.mkdtemp(prefix="trench_design_")
        try:
            return self._process(parameters, context, feedback, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _process(self, parameters, context, feedback, work: str):
        polys = self.parameterAsVectorLayer(parameters, self.P_POLY, context)
        roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        pdps = self.parameterAsVectorLayer(parameters, self.P_PDP, context)
        objects = self._layer(parameters, self.P_HH, context)
        mfg = self._layer(parameters, self.P_MFG, context)
        if polys is None or roads is None or pdps is None:
            raise QgsProcessingException(_tr(
                "Designer trench layer needs Polygons, Roads and PDPs."))
        if objects is None or objects.featureCount() == 0:
            raise QgsProcessingException(_tr(
                "Designer trench layer needs the household/object layer — the "
                "garden drop legs and the per-address attribution come from it."))
        if mfg is None or mfg.featureCount() == 0:
            raise QgsProcessingException(_tr(
                "Designer trench layer needs the MFG point (feeder origin)."))

        # The designer reads GeoPackages, so materialise the QGIS layers.
        target_epsg = self._target_epsg((polys, roads, pdps, objects, mfg))
        src = {
            "polygons": self._dump(polys, os.path.join(work, "Polygons.gpkg"),
                                   context, feedback),
            "roads": self._dump(roads, os.path.join(work, "Roads.gpkg"),
                                context, feedback),
            "pdps": self._dump(pdps, os.path.join(work, "PDPs.gpkg"),
                               context, feedback),
            "objects": self._dump(objects, os.path.join(work, "Objects.gpkg"),
                                  context, feedback),
            "mfg": self._dump(mfg, os.path.join(work, "MFG.gpkg"),
                              context, feedback),
        }

        # Import guarded: the designer needs networkx + GDAL, and a readable
        # message beats an import traceback in the middle of the pipeline.
        try:
            from ..design.trench_design import design as run_design
        except Exception as exc:                     # pragma: no cover
            raise QgsProcessingException(_tr(
                "The trench designer could not be loaded (needs networkx + "
                "osgeo): {0}").format(exc))

        # Aerial zones are opt-in, exactly like the pole/aerial stages: with
        # zones supplied the designer classifies the drop legs that cannot be
        # dug (inside a zone, longer than `aerial_max_leg_m`, or branching off
        # an aerial leg) and emits them on `Aerial_Drops` instead of trenching
        # them; it also flags any span running THROUGH a zone with
        # `AERIAL_ZONE`/`AERIAL_ZONE_REASON` (a corridor RESTRICTION — the span
        # itself stays Open Cut / HDD / Garden).  Without zones nothing changes.
        aerial_dst = None
        zones = self._layer(parameters, self.P_AERIAL_ZONES, context)
        if zones is not None and zones.featureCount() > 0:
            aerial_dst = self._dump(zones, os.path.join(work, "Aerial_Zones.gpkg"),
                                    context, feedback)
            feedback.pushInfo(_tr(
                "Aerial zones: {0} polygon(s) → the designer classifies the "
                "drop legs that cannot be dug.").format(zones.featureCount()))

        out_dir = os.path.join(work, "design")
        cfg = {
            "mfg": src["mfg"], "pdps": src["pdps"], "objects": src["objects"],
            "polygons": src["polygons"], "roads": src["roads"], "out": out_dir,
            "aerial": aerial_dst, "target_epsg": int(target_epsg),
            # The ONE-SIDED kerb band is OFF, on the operator's instruction after
            # reviewing it: it moved the whole carriageway-carried network onto
            # one kerb and changed the shape that was approved. It is superseded
            # by the two-sided rule — trench on BOTH kerbs of a street, never on
            # the centreline, and cross between the two sides only where a
            # premise needs it (that crossing is an HDD). The band stays in the
            # designer, tested, behind this value; set it to KERB_OFFSET_M (the
            # per-class base, 4.5) to bring the one-sided version back. See
            # docs/GLOBAL_TODO.md.
            "kerb_offset_m": 0.0,
            "sidewalk_link_m": 0.0,   # off too — see Params.sidewalk_link_m
        }
        feedback.pushInfo(_tr("Running the civil trench designer …"))
        report = run_design(cfg) or {}
        stats = {k: v for k, v in report.items() if k != "log"}
        feedback.pushInfo(_tr("Designer report: {0}").format(
            ", ".join("%s=%s" % (k, stats[k]) for k in
                      ("runs", "spans", "drills", "nodes", "loose_ends", "pruned_spans")
                      if k in stats)))

        # ── read the designer's publications back into QGIS layers
        final = self._read_gpkg(os.path.join(out_dir, "Final_Trenches.gpkg"),
                                "Final_Trenches")
        if final is None:
            raise QgsProcessingException(_tr(
                "The trench designer produced no trenches."))
        nodes = self._read_gpkg(os.path.join(out_dir, "Trench_Nodes.gpkg"), "Trench_Nodes")
        drills = self._read_gpkg(os.path.join(out_dir, "Tangent_Crossings.gpkg"),
                                 "Tangent_Crossings")

        final_rows = self._final_rows(final)
        # The designer's drop legs carry the premise, not the splitter that
        # serves it, while the duct stage refuses to run its Distribution step
        # without a PDP on the endpoint points (pseudo objects / objects). The
        # lookup is a join on the address the objects layer already holds.
        final_rows = self._attach_premise_lookup(
            final_rows, objects, target_epsg, context, feedback
        )
        if not final_rows:
            raise QgsProcessingException(_tr(
                "The trench designer produced no usable spans."))
        # Last write before the sinks: close the sub-metre gaps the designer's
        # own anchor pass cannot see (it runs on the runs, before the spans are
        # cut at nodes and the HDD bores replace their Open Cut segment).
        # ── Splitters belong ON the open-cut backbone ────────────────────
        # Run this BEFORE the weld: a cabinet that is moved onto its own
        # trench needs no spur, no relabel and no bridge — the feeder simply
        # passes through it. The guards in the snap (short move, stay with the
        # served polygon) mean an anchor that cannot be moved honestly is left
        # where the designer put it, and the weld/reach passes still cover it.
        anchors = self._anchor_points(pdps, mfg, target_epsg, context, feedback)
        snapped: Dict[str, Tuple[float, float]] = {}
        pdp_axes = self._pdp_anchor_rows(pdps, target_epsg, context, feedback)

        if self._snap_pdps_to_backbone(pdp_axes, final_rows, polys,
                                       target_epsg, context, feedback):
            for a in pdp_axes:
                if a["x"] != a["x0"] or a["y"] != a["y0"]:
                    snapped[str(a["id"])] = (a["x"], a["y"])
            anchors = [(lb, aid, snapped.get(str(aid), (ax, ay))[0],
                        snapped.get(str(aid), (ax, ay))[1], mfg_id)
                       for (lb, aid, ax, ay, mfg_id) in anchors]

        weld = self._weld_network(
            final_rows, anchors, feedback)
        if weld["welded"] or weld["connectors"] or weld["stitched"]:
            feedback.pushInfo(_tr(
                "Network weld: {0} anchor endpoint(s) snapped, {1} anchor "
                "connector(s), {2} span(s) stitched").format(
                    weld["welded"], weld["connectors"], weld["stitched"]))
        # The FEEDER (Open Cut) backbone must run from the MFG to every PDP.
        # The weld above guarantees the geometry touches the anchors; this
        # guarantees the *tier* does too, so Feeder_Trench / the feeder cable /
        # the feeder duct all reach every splitter.
        self._ensure_backbone_reach(final_rows, anchors, feedback)
        # Multiple PDP routes may describe the same physical side-of-road
        # corridor. Consolidate only coincident/connected main-corridor rows;
        # chamber placement and the downstream duct/cable stages consume this
        # physical network and preserve the chamber break rule.
        final_rows = self._consolidate_physical_corridors(final_rows, feedback)
        promoted_spans = self._stamp_tier_provenance(final_rows)
        if promoted_spans:
            feedback.pushInfo(_tr(
                "Tier provenance: {0} span(s) are promoted (TRENCH_TIER reads "
                "Feeder, PROMOTED_FROM names the tier they serve) - group by "
                "SERVES_TIER, not TRENCH_TIER, when attributing a duct or "
                "cable to a tier.").format(promoted_spans))
        feedback.pushInfo(_tr(
            "Designer spans: {0} total, {1} with an address, {2} HDD, "
            "{3} Garden, {4} node(s), {5} drill(s)").format(
                len(final_rows),
                sum(1 for r in final_rows if r.get("addr_id")),
                sum(1 for r in final_rows if r["trench_type"] == "HDD"),
                sum(1 for r in final_rows if r["trench_type"] == "Garden"),
                nodes.featureCount() if nodes else 0,
                drills.featureCount() if drills else 0))

        sinks: Dict[str, object] = {}

        # ── Final_Trenches (MULTILINESTRING, one part per span — legacy parity)
        sinks[self.O_FINAL] = self._write_rows(
            parameters, context, self.O_FINAL, final_rows, _FINAL_FIELDS,
            QgsWkbTypes.MultiLineString, target_epsg, feedback)

        # ── tier mirrors
        # Feeder and Garden publish the designer's spans as-is. Distribution
        # publishes the SHARED SPINE, one row per span — the ducts and cables
        # are laid IN the trench, so their geometry must be a subset of the
        # trench geometry. (Historically this layer was one routed corridor
        # per house, which duplicated the spine once per premise: ~25 km of
        # drawn distribution cable for ~1.4 km of unique geometry on Berlin.)
        # cable_layer's per-address join is served by ``addr_id`` holding
        # every premise the span serves, comma-joined — see _fan_out_per_address
        # kept for the pipeline runs that still index that way.
        for tier, key in (("Feeder", self.O_FEEDER_FINAL),
                          ("Garden", self.O_GARDEN)):
            rows = [r for r in final_rows if r["TRENCH_TIER"] == tier]
            sinks[key] = self._write_rows(
                parameters, context, key, rows, _FINAL_FIELDS,
                _TIER_GEOM[tier], target_epsg, feedback)
            feedback.pushInfo(_tr("  {0} trench: {1} feature(s)").format(tier, len(rows)))

        # The splitter's own coordinates anchor the PDP projections; the plan
        # layers arrive as WGS84 in some runs, so the PDP layer is put in the
        # project CRS first.
        pdps_t = self._reproject(pdps, target_epsg, context, feedback) or pdps
        spine_rows = [r for r in final_rows if r["TRENCH_TIER"] == "Distribution"]
        # cable_layer resolves a premise's trunk span by indexing the
        # distribution layer on addr_id and looking the address up — which a
        # comma-joined value never matches. The fanned rows (one row per
        # address, same span geometry) go to the lines output the cable stage
        # reads. The duplication is bookkeeping only: identical geometry
        # unions back into one corridor in the duct clubber, and the trunk
        # cable build groups by span identity (SPAN_INDEX + tier), so one
        # trunk cable is still published per span.
        if spine_rows:
            fan_rows = self._fan_out_per_address(spine_rows)
            sinks[self.O_DIST_LINES] = self._write_rows(
                parameters, context, self.O_DIST_LINES, fan_rows, _FINAL_FIELDS,
                QgsWkbTypes.LineString, target_epsg, feedback)
        feedback.pushInfo(_tr(
            "  Distribution trench: {0} shared spine span(s) ({1} m) for {2} "
            "addressed premise(s)").format(
                len(spine_rows),
                round(sum(r.get("SPAN_LEN_M") or 0 for r in spine_rows)),
                len(self._fan_out_per_address(spine_rows)) if spine_rows else 0))

        # ── Distribution dissolved (cable_layer's fallback input)
        if spine_rows:
            sinks[self.O_DIST_DISS] = self._write_rows(
                parameters, context, self.O_DIST_DISS, spine_rows, _FINAL_FIELDS,
                QgsWkbTypes.MultiLineString, target_epsg, feedback)

        # ── the designer's drills. They are already the HDD spans inside
        #    Final_Trenches; publishing them here is what lets the chamber
        #    stage's fallback (and the map) keep seeing the crossings.
        if drills is not None:
            drill_rows = self._drill_rows(drills)
            if drill_rows:
                sinks[self.O_TANGENTS_USED] = self._write_rows(
                    parameters, context, self.O_TANGENTS_USED, drill_rows,
                    _DRILL_FIELDS, QgsWkbTypes.LineString, target_epsg, feedback)
                sinks[self.O_FINAL_TAN] = self._write_rows(
                    parameters, context, self.O_FINAL_TAN, drill_rows,
                    _DRILL_FIELDS, QgsWkbTypes.LineString, target_epsg, feedback)

        # ── the legs that are NOT dug (aerial classification)
        self._publish_aerial_drops(parameters, context, work, out_dir,
                                   target_epsg, feedback, sinks)

        # ── the structural nodes (HDD pits, junctions, bends, pulls) — the
        #    chamber stage's primary candidate source
        self._publish_trench_nodes(parameters, context, out_dir,
                                   target_epsg, feedback, sinks)

        # ── derived layers
        pseudo_rows = self._pseudo_rows(final_rows)
        sinks[self.O_PSEUDO_HH] = self._write_rows(
            parameters, context, self.O_PSEUDO_HH, pseudo_rows, _PSEUDO_FIELDS,
            QgsWkbTypes.Point, target_epsg, feedback)
        feedback.pushInfo(_tr("  pseudo objects on the footway: {0}").format(
            len(pseudo_rows)))

        proj_rows = self._pdp_projection_rows(pdps_t, final, target_epsg,
                                              snapped=snapped)
        sinks[self.O_PDP_PROJ] = self._write_rows(
            parameters, context, self.O_PDP_PROJ, proj_rows, _PROJ_FIELDS,
            QgsWkbTypes.LineString, target_epsg, feedback)
        feedback.pushInfo(_tr("  PDP projections off the corridor: {0}").format(
            len(proj_rows)))

        # sidewalks + AOI, derived from the DESIGN so duct side selection and
        # chamber clipping see the same geometry the trenches are.
        side_l, side_r = self._sidewalk_layers(final, context, feedback)
        sinks[self.O_SIDE_L] = self._write_layer(
            parameters, context, self.O_SIDE_L, side_l, QgsWkbTypes.LineString,
            target_epsg, feedback)
        sinks[self.O_SIDE_R] = self._write_layer(
            parameters, context, self.O_SIDE_R, side_r, QgsWkbTypes.LineString,
            target_epsg, feedback)

        aoi = self._aoi_layer(polys, context, feedback)
        sinks[self.O_S1_AOI_BUF_DISS] = self._write_layer(
            parameters, context, self.O_S1_AOI_BUF_DISS, aoi, QgsWkbTypes.Polygon,
            target_epsg, feedback)

        # MFG passthrough, as the legacy stage published it.
        sinks[self.O_MFG] = self._write_layer(
            parameters, context, self.O_MFG, mfg, QgsWkbTypes.Point,
            target_epsg, feedback)

        feedback.pushInfo(_tr("Designer trench layer finished."))
        return sinks

    # ------------------------------------------------------------- helpers
    def _layer(self, parameters, key, context) -> Optional[QgsVectorLayer]:
        if not key:
            return None
        try:
            return self.parameterAsVectorLayer(parameters, key, context)
        except Exception:
            return None

    @staticmethod
    def _target_epsg(layers) -> int:
        """The CRS the designer works in.

        NOT simply an input layer's CRS: the plan layers arrive as WGS84 in some
        runs and in the project CRS in others, and the designer must work in a
        PROJECTED system — in degrees every metre-based tolerance (street
        noding, snap radii, the search bbox) silently collapses the network to a
        handful of nodes. So a projected input CRS is honoured and a geographic
        one is ignored, falling back to the project grid the rest of the
        pipeline publishes in (EPSG:25833, ETRS89/UTM32N).
        """
        for layer in layers:
            if layer is None:
                continue
            try:
                crs = layer.crs()
                if crs.isGeographic():
                    continue
                code = crs.postgisSrid()
            except Exception:
                continue
            if code and code > 0:
                return int(code)
        return 25833

    @staticmethod
    def _dump(layer: QgsVectorLayer, path: str, context, feedback) -> str:
        """Materialise a QGIS layer to a GPKG the designer can read."""
        processing.run(
            "native:savefeatures",
            {"INPUT": layer, "OUTPUT": path,
             "LAYER_NAME": os.path.splitext(os.path.basename(path))[0]},
            context=context, feedback=feedback, is_child_algorithm=True)
        if not os.path.exists(path):
            raise QgsProcessingException(_tr(
                "Could not materialise a trench-designer input: {0}").format(path))
        return path

    @staticmethod
    def _read_gpkg(path: str, name: str) -> Optional[QgsVectorLayer]:
        if not os.path.exists(path):
            return None
        lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            return None
        return lyr if lyr.featureCount() > 0 else None

    # ---- row building ---------------------------------------------------
    # ── final geometry repairs ──────────────────────────────────────────
    # The designer's own anchor pass (``connect_unreached_pdps`` /
    # ``connect_unreached_mfg``) runs on the RUNS, before the spans are cut at
    # structural nodes and before each HDD bore replaces its Open Cut segment.
    # Whatever it closed can therefore reopen: measured on Berlin
    # `0dc85304…`, 28 of 31 PDPs sit exactly on the published trench and 3 end
    # 0.07-0.40 m short, which reads as an unconnected splitter on the map.
    # These thresholds are applied to the geometry that SHIPS, as the last
    # write before the sinks.
    _WELD_MAX_M = 1.0     # largest anchor gap worth closing
    _WELD_MOVE_M = 0.5    # move an existing endpoint when it is this close...
    _BACKBONE_TOL_M = 1.0  # a PDP is "on the backbone" when a feeder span is
    #                       this close (see _ensure_backbone_reach)
    # A splitter belongs ON the open-cut backbone — it is a cabinet dug into the
    # same trench, not a point in a garden. So the anchor is snapped onto the
    # nearest Feeder (Open Cut) span, under two guards that keep the placement
    # honest: the move stays short, and the cabinet must remain with the polygon
    # it serves (never inside a neighbour's).
    _PDP_SNAP_MAX_M = 30.0     # never drag a cabinet further than this
    _PDP_SNAP_POLY_M = 15.0    # ...and never more than this off its own polygon
    _PDP_SNAP_MOVE_M = 2.0     # a snap is a NUDGE, not a relocation. A cabinet
    #                            further than this off the backbone is left
    #                            where the platform publishes it and gets a real
    #                            SPUR instead (_ensure_backbone_reach). Moving
    #                            it further only the DESIGN's copy of the anchor
    #                            (the PDP layer is published before this stage)
    #                            left the map showing a cabinet 13 m off the
    #                            trench it was supposedly welded onto.
    _FEEDER_BRIDGE_MAX_M = 30.0  # longest spur worth digging back to the backbone
    _STITCH_M = 0.5       # ...otherwise add a connector; same tolerance welds
    #                       span endpoints that stop just short of each other
    #                       (the designer's straightening leaves 15 spans in
    #                       0.05-0.5 m islands on Berlin)

    def _anchor_points(self, pdps, mfg, epsg: int, context, feedback):
        """Every PDP + MFG in the project CRS, as (label, id, x, y)."""
        out: List[Tuple[str, str, float, float, str]] = []
        for lyr, label, idfield in ((pdps, "PDP", "PDP_ID"),
                                    (mfg, "MFG", "MFG_ID")):
            if lyr is None:
                continue
            t = self._reproject(lyr, epsg, context, feedback) or lyr
            names = t.fields().names()
            for f in t.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                msg = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
                aid = str(f[idfield]) if idfield in names and f[idfield] else "?"
                mfg_id = str(f["MFG_ID"]) if "MFG_ID" in names and f["MFG_ID"] else ""
                out.append((label, aid, msg.x(), msg.y(), mfg_id))
        return out

    def _weld_network(self, rows: List[dict], anchors, feedback=None) -> Dict[str, int]:
        """Weld each MFG catchment independently, never to another origin."""
        mfg_ids = sorted({str(row.get("MFG_ID") or "") for row in rows})
        if len(mfg_ids) <= 1:
            return self._weld_network_single(rows, anchors, feedback)
        totals = {"welded": 0, "connectors": 0, "stitched": 0}
        additions = []
        for mfg_id in mfg_ids:
            group_rows = [row for row in rows if str(row.get("MFG_ID") or "") == mfg_id]
            group_anchors = [a for a in anchors
                             if len(a) > 4 and str(a[4] or "") == mfg_id]
            original_ids = {id(row) for row in group_rows}
            stats = self._weld_network_single(group_rows, group_anchors, feedback)
            for key in totals:
                totals[key] += stats.get(key, 0)
            additions.extend(row for row in group_rows if id(row) not in original_ids)
        rows.extend(additions)
        return totals

    def _weld_network_single(self, rows: List[dict], anchors, feedback=None) -> Dict[str, int]:
        """Close the last metres between the published network and its anchors.

        Two repairs, both on the final span geometry:

        1. **Anchor weld** — a PDP/MFG within ``_WELD_MAX_M`` of a span is
           connected: the span endpoint is moved onto it when it is within
           ``_WELD_MOVE_M`` (no extra feature, the trench simply terminates on
           the cabinet), otherwise a short connector span is appended (the
           cabinet taps a passing trench).
        2. **Span stitch** — a span endpoint within ``_STITCH_M`` of another
           span is moved onto that span, so the published network is ONE
           connected graph instead of a main network plus sub-metre islands.

        Only geometry that was already within a metre is touched, and the
        derived lengths are recomputed so the attributes keep agreeing with the
        geometry.
        """
        stats = {"welded": 0, "connectors": 0, "stitched": 0}
        if not rows:
            return stats

        paths: Dict[int, List[Tuple[float, float]]] = {}
        for i, r in enumerate(rows):
            line = _first_line(r.get("_geom"))
            if line is None:
                continue
            pts = [(p.x(), p.y()) for p in line.asPolyline()]
            if len(pts) >= 2:
                paths[i] = pts
        if not paths:
            return stats
        touched = set()
        # A point welded onto an anchor is FINAL. The stitch pass below would
        # otherwise pull it onto whatever span happens to pass nearby and undo
        # the weld - measured on Berlin, that is exactly how PDP00009 went back
        # to being 0.398 m off after it had been snapped on.
        locked = set()

        def _dist(ax, ay, bx, by):
            return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

        for anchor in (anchors or []):
            label, aid, ax, ay = anchor[:4]
            mfg_id = str(anchor[4] if len(anchor) > 4 else "")
            if label not in ("PDP", "MFG"):
                continue
            best_end = (float("inf"), None, None)
            best_seg = (float("inf"), None)
            for i, pts in paths.items():
                if mfg_id and str(rows[i].get("MFG_ID") or "") != mfg_id:
                    continue
                dd = _dist(pts[0][0], pts[0][1], ax, ay)
                if dd < best_end[0]:
                    best_end = (dd, i, 0)
                dd = _dist(pts[-1][0], pts[-1][1], ax, ay)
                if dd < best_end[0]:
                    best_end = (dd, i, len(pts) - 1)
                d_seg = rows[i]["_geom"].distance(QgsGeometry.fromPointXY(QgsPointXY(ax, ay)))
                if d_seg < best_seg[0]:
                    best_seg = (d_seg, i)
            if best_seg[1] is None or best_seg[0] > self._WELD_MAX_M:
                continue
            if best_end[0] <= self._WELD_MOVE_M:
                _, i, which = best_end
                pts = list(paths[i])
                pts[which] = (ax, ay)
                paths[i] = pts
                touched.add(i)
                locked.add((i, which))
                stats["welded"] += 1
                if feedback:
                    feedback.pushInfo(_tr(
                        "  weld: {0} {1} was {2:.2f} m off the trench - "
                        "endpoint snapped on").format(label, aid, best_end[0]))
                continue
            i = best_seg[1]
            src = rows[i]
            near = src["_geom"].nearestPoint(QgsGeometry.fromPointXY(QgsPointXY(ax, ay)))
            q = near.asPoint() if near is not None and not near.isEmpty() else QgsPointXY(ax, ay)
            conn = dict(src)
            conn["_geom"] = QgsGeometry.fromPolylineXY([q, QgsPointXY(ax, ay)])
            span = _dist(q.x(), q.y(), ax, ay)
            conn["length_m"] = round(span, 2)
            conn["SPAN_LEN_M"] = round(span, 2)
            conn["SRC"] = "anchor-connector"
            conn["MFG_ID"] = mfg_id or conn.get("MFG_ID")
            conn["START_CHAMBER"] = None
            conn["END_CHAMBER"] = None
            conn["SPAN_KIND"] = "Unchambered"
            rows.append(conn)
            stats["connectors"] += 1
            if feedback:
                feedback.pushInfo(_tr(
                    "  connector: {0} {1} tapped the trench {2:.2f} m from it"
                    ).format(label, aid, span))

        # Span stitch: weld endpoints that stop just short of another span.
        tol = self._STITCH_M
        cell = max(tol * 4.0, 2.0)
        grid: Dict[Tuple[int, int], List[int]] = {}
        boxes = {}
        for i, r in enumerate(rows):
            g = r.get("_geom")
            if g is None:
                continue
            env = g.boundingBox()
            boxes[i] = (env.xMinimum(), env.xMaximum(), env.yMinimum(), env.yMaximum())
            for cx in range(int(env.xMinimum() / cell), int(env.xMaximum() / cell) + 1):
                for cy in range(int(env.yMinimum() / cell), int(env.yMaximum() / cell) + 1):
                    grid.setdefault((cx, cy), []).append(i)
        for i in list(paths.keys()):
            pts = paths[i]
            for which in (0, len(pts) - 1):
                if (i, which) in locked:
                    continue
                px, py = pts[which]
                x0, y0 = px - tol, py - tol
                cands = set()
                for cx in range(int(x0 / cell), int((px + tol) / cell) + 1):
                    for cy in range(int(y0 / cell), int((py + tol) / cell) + 1):
                        cands.update(grid.get((cx, cy), []))
                best = (float("inf"), None)
                for j in cands:
                    if j == i or str(rows[j].get("MFG_ID") or "") != str(rows[i].get("MFG_ID") or ""):
                        continue
                    bb = boxes.get(j)
                    if bb is None:
                        continue
                    if (px < bb[0] - tol or px > bb[1] + tol
                            or py < bb[2] - tol or py > bb[3] + tol):
                        continue
                    d = rows[j]["_geom"].distance(
                        QgsGeometry.fromPointXY(QgsPointXY(px, py)))
                    if d < best[0]:
                        best = (d, j)
                if best[1] is None or best[0] <= 0.0 or best[0] > tol:
                    continue
                near = rows[best[1]]["_geom"].nearestPoint(
                    QgsGeometry.fromPointXY(QgsPointXY(px, py)))
                if near is None or near.isEmpty():
                    continue
                q = near.asPoint()
                # Refuse a weld that would collapse the span.
                other = pts[-1] if which == 0 else pts[0]
                if _dist(q.x(), q.y(), other[0], other[1]) < 0.1:
                    continue
                pts = list(pts)
                pts[which] = (q.x(), q.y())
                paths[i] = pts
                touched.add(i)
                stats["stitched"] += 1

        for i in touched:
            pts = paths[i]
            rows[i]["_geom"] = QgsGeometry.fromPolylineXY(
                [QgsPointXY(x, y) for x, y in pts])
            total = sum(_dist(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1])
                        for k in range(len(pts) - 1))
            rows[i]["length_m"] = round(total, 2)
            rows[i]["SPAN_LEN_M"] = round(total, 2)
        return stats

    def _pdp_anchor_rows(self, pdps, epsg: int, context, feedback) -> List[dict]:
        """PDP features as anchors carrying their served polygon.

        Same points ``_anchor_points`` produces, plus ``POLYGON_ID`` — the
        guard in ``_snap_pdps_to_backbone`` needs to know which premises each
        splitter was placed for.
        """
        out: List[dict] = []
        if pdps is None:
            return out
        t = self._reproject(pdps, epsg, context, feedback) or pdps
        names = t.fields().names()
        f_pdp = _pick_field(t, ["PDP_ID", "pdp_id", "pdp"])
        f_poly = _pick_field(t, ["POLYGON_ID", "polygon_id"])
        f_mfg = _pick_field(t, ["MFG_ID", "mfg_id"])
        for f in t.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
            aid = str(f[f_pdp]) if (f_pdp in names and f[f_pdp]) else "?"
            poly = str(f[f_poly]) if (f_poly in names and f[f_poly]) else ""
            out.append({"label": "PDP", "id": aid, "polygon_id": poly,
                        "mfg_id": str(f[f_mfg]) if (f_mfg in names and f[f_mfg]) else "",
                        "x": pt.x(), "y": pt.y(),
                        "x0": pt.x(), "y0": pt.y()})
        return out

    def _snap_pdps_to_backbone(self, anchors: List[dict], rows: List[dict],
                               polys, epsg: int, context, feedback) -> int:
        """Put every PDP ON the open-cut backbone, near the polygon it serves.

        The designer tiers a run by what it serves and snaps anchors to the
        street graph, so a splitter can end up a metre or two off the feeder
        alignment — and a cabinet that is not on the trench is what makes the
        feeder cable/duct look disconnected on the map, whatever the routing
        does afterwards.

        Rather than relabel or re-route around that, move the anchor itself:
        project it onto the nearest **Feeder (Open Cut)** span (its own trench
        is Feeder by construction after ``_ensure_backbone_reach``), subject to

        * shift <= ``_PDP_SNAP_MOVE_M`` — a cabinet is only nudged (the weld
          below finishes the job). A larger gap is NOT a snap: the cabinet is
          left where the platform publishes it and ``_ensure_backbone_reach``
          digs a spur to it, so the point on the map and the trench agree, and
        * the snapped point stays with its own polygon (inside it, or within
          ``_PDP_SNAP_POLY_M`` of it) and inside no other polygon — the PDP
          must keep serving the premises it was placed for.

        Returns how many anchors moved.
        """
        if not anchors or not rows:
            return 0
        own: Dict[str, object] = {}
        others: List[object] = []
        if polys is not None:
            t = self._reproject(polys, epsg, context, feedback) or polys
            names = t.fields().names()
            f_poly = _pick_field(t, ["POLYGON_ID", "polygon_id", "id"])
            for pf in t.getFeatures():
                pg = pf.geometry()
                if pg is None or pg.isEmpty():
                    continue
                if f_poly in names and pf[f_poly]:
                    own[str(pf[f_poly])] = pg
                    others.append(pg)
                else:
                    others.append(pg)
        moved = 0
        worst = 0.0
        for a in anchors:
            if a["label"] != "PDP":
                continue
            pt = QgsGeometry.fromPointXY(QgsPointXY(a["x"], a["y"]))
            best, best_d = None, float("inf")
            for row in rows:
                if str(row.get("MFG_ID") or "") != str(a.get("mfg_id") or ""):
                    continue
                if str(row.get("TRENCH_TIER") or "") != "Feeder":
                    continue
                g = row.get("_geom")
                if g is None:
                    continue
                d = g.distance(pt)
                if d < best_d:
                    best_d, best = d, g
            if best is None or best_d <= 0.05:
                continue                       # already on the backbone
            if best_d > self._PDP_SNAP_MOVE_M:
                continue                       # a spur, not a snap — see below
            proj = best.nearestPoint(pt)
            if proj is None or proj.isEmpty():
                continue
            q = proj.asPoint()
            cand = QgsGeometry.fromPointXY(QgsPointXY(q.x(), q.y()))
            pid = str(a.get("polygon_id") or "")
            g_own = own.get(pid) if pid else None
            if g_own is not None:
                inside_own = g_own.contains(cand)
                if not inside_own and g_own.distance(cand) > self._PDP_SNAP_POLY_M:
                    continue                   # would stray off the served polygon
                if any(o is not g_own and o.contains(cand) for o in others):
                    continue                   # would land in a neighbour's polygon
            a["x"], a["y"] = q.x(), q.y()
            moved += 1
            worst = max(worst, best_d)
        if moved and feedback:
            feedback.pushInfo(_tr(
                "PDP snap: {0} splitter(s) nudged onto the open-cut backbone "
                "(max {1:.2f} m, guards: <= {2:g} m shift, own polygon +- "
                "{3:g} m). Anything further gets a feeder spur instead."
            ).format(moved, worst, self._PDP_SNAP_MOVE_M,
                     self._PDP_SNAP_POLY_M))
        return moved

    def _span_adjacency(self, rows: List[dict], tol: float = 0.05):
        """Neighbour lists over the WELDED spans (spans that touch).

        Uses a coarse grid on the span bounding boxes so the O(n²) distance
        tests only run between spans that could possibly touch (542 spans on
        Berlin), then keeps pairs whose geometries come within ``tol``.
        """
        cell = 25.0
        bboxes, grid = [], {}
        for i, r in enumerate(rows):
            g = r.get("_geom")
            bb = g.boundingBox() if g is not None else None
            bboxes.append(bb)
            if bb is None:
                continue
            for cx in range(int(bb.xMinimum() // cell) - 1,
                            int(bb.xMaximum() // cell) + 2):
                for cy in range(int(bb.yMinimum() // cell) - 1,
                                int(bb.yMaximum() // cell) + 2):
                    grid.setdefault((cx, cy), []).append(i)
        adj = {i: set() for i in range(len(rows))}
        tested = set()
        for i, r in enumerate(rows):
            bb = bboxes[i]
            g = r.get("_geom")
            if bb is None or g is None:
                continue
            cand = set()
            for cx in range(int(bb.xMinimum() // cell) - 1,
                            int(bb.xMaximum() // cell) + 2):
                for cy in range(int(bb.yMinimum() // cell) - 1,
                                int(bb.yMaximum() // cell) + 2):
                    cand.update(grid.get((cx, cy), ()))
            for j in cand:
                if j == i or (min(i, j), max(i, j)) in tested:
                    continue
                tested.add((min(i, j), max(i, j)))
                bj = bboxes[j]
                gj = rows[j].get("_geom")
                if bj is None or gj is None:
                    continue
                if (bb.xMaximum() + tol < bj.xMinimum()
                        or bj.xMaximum() + tol < bb.xMinimum()
                        or bb.yMaximum() + tol < bj.yMinimum()
                        or bj.yMaximum() + tol < bb.yMinimum()):
                    continue
                if g.distance(gj) <= tol:
                    adj[i].add(j)
                    adj[j].add(i)
        return adj

    def _ensure_backbone_reach(self, rows: List[dict], anchors,
                               feedback=None) -> int:
        """Repair backbone reach separately for every assigned MFG.

        Partitioning is only meaningful when the inputs actually carry area
        information: spans stamped with an ``MFG_ID`` and anchors extended to
        ``(label, id, x, y, mfg_id)``. Legacy callers pass 4-tuple anchors and
        rows with no ``MFG_ID``; those carry nothing to partition on, and
        filtering them into per-MFG groups emptied every group and silently
        disabled the whole repair. They are therefore handled as ONE group,
        exactly as before the multi-area split.
        """
        row_mfgs = {str(row.get("MFG_ID") or "") for row in rows}
        anchors_carry_mfg = any(len(a) > 4 for a in anchors)
        if not (row_mfgs - {""}) or not anchors_carry_mfg:
            return self._ensure_backbone_reach_single(rows, anchors, feedback)
        total = 0
        additions = []
        for mfg_id in sorted(row_mfgs):
            group_rows = [row for row in rows if str(row.get("MFG_ID") or "") == mfg_id]
            group_anchors = [a for a in anchors
                             if len(a) > 4 and str(a[4] or "") == mfg_id]
            original_ids = {id(row) for row in group_rows}
            total += self._ensure_backbone_reach_single(
                group_rows, group_anchors, feedback
            )
            additions.extend(row for row in group_rows if id(row) not in original_ids)
        rows.extend(additions)
        return total

    def _ensure_backbone_reach_single(self, rows: List[dict], anchors,
                                      feedback=None) -> int:
        if not rows or not anchors:
            return 0
        feeder = {i for i, r in enumerate(rows)
                  if str(r.get("TRENCH_TIER") or "") == "Feeder"}
        if not feeder:
            return 0
        adj = self._span_adjacency(rows)

        def _pt(x, y):
            return QgsGeometry.fromPointXY(QgsPointXY(x, y))

        def _near(x, y, only=None):
            """(distance, index) of the nearest span, optionally within a set."""
            best, best_i = float("inf"), None
            p = _pt(x, y)
            for i, r in enumerate(rows):
                if only is not None and i not in only:
                    continue
                g = r.get("_geom")
                if g is None:
                    continue
                d = g.distance(p)
                if d < best:
                    best, best_i = d, i
            return best, best_i

        # ── Ground truth: the component the MFG actually sits on ────────
        mfgs = [a for a in anchors if str(a[0]).upper() == "MFG"]
        if not mfgs:
            return 0
        mfg_id = str(mfgs[0][4] if len(mfgs[0]) > 4 else mfgs[0][1])
        mfg = mfgs[0]
        # Ownership only restricts the search when the spans actually carry an
        # MFG stamp. Legacy rows have none, so requiring them to match the
        # anchor's id would leave nothing owned and disable the repair.
        partitioned = bool({str(r.get("MFG_ID") or "") for r in rows} - {""})
        if partitioned:
            owned_rows = {i for i, row in enumerate(rows)
                          if str(row.get("MFG_ID") or "") == mfg_id}
        else:
            owned_rows = set(range(len(rows)))
        if not owned_rows:
            return 0
        mfg_i = None
        mfg_pt = QgsGeometry.fromPointXY(QgsPointXY(mfg[2], mfg[3]))
        origin_rows = {i for i in owned_rows
                       if str(rows[i].get("TRENCH_TIER") or "") != "Garden"}
        for i in origin_rows or owned_rows:
            geom = rows[i].get("_geom")
            if geom is not None and not geom.isEmpty():
                if mfg_i is None or geom.distance(mfg_pt) < rows[mfg_i]["_geom"].distance(mfg_pt):
                    mfg_i = i
        if mfg_i is None:
            return 0
        for i in range(len(rows)):
            if i not in owned_rows:
                adj.pop(i, None)
        for neighbours in adj.values():
            neighbours.intersection_update(owned_rows)
        feeder.intersection_update(owned_rows)

        # A Garden leg is a premise drop, NOT a route: the backbone must be
        # reachable without walking down one. This is the piece that hid the
        # 11 stubs — they DO join the network, but only through the garden leg
        # that taps the street, so a walk that allowed Garden spans called them
        # connected while the feeder tier stayed in 12 islands.
        def _walkable(i):
            return str(rows[i].get("TRENCH_TIER") or "") != "Garden"

        comp = {mfg_i}
        queue = [mfg_i]
        while queue:
            nxt = []
            for n in queue:
                if not _walkable(n):
                    continue
                for m in adj.get(n, ()):
                    if m not in comp and _walkable(m):
                        comp.add(m)
                        nxt.append(m)
            queue = nxt
        # The backbone is the FEEDER sub-network the MFG can reach *through
        # feeder spans only*. Taking "every Feeder span in the component" was
        # self-defeating: a 0.6-8.0 m feeder stub that hangs off a
        # DISTRIBUTION span (which is what all 11 Berlin stubs turned out to
        # be — measured 0.00 m to a distribution trench, 0.5-12.9 m to the
        # real backbone) is itself a Feeder span in the component, so it was
        # counted as its own backbone and the pass did nothing.
        if mfg_i in feeder:
            backbone = {mfg_i}
            walk = [mfg_i]
            while walk:
                nxt = []
                for n in walk:
                    for m in adj.get(n, ()):
                        if m in backbone or m not in feeder or not _walkable(m):
                            continue
                        backbone.add(m)
                        nxt.append(m)
                walk = nxt
        else:
            backbone = {i for i in comp if i in feeder} or set(comp)
        promoted = 0
        spurs = 0
        far = []

        def _promote(start):
            """Relabel the welded path start → backbone as Feeder."""
            prev = {start: None}
            queue2, hit = [start], None
            while queue2 and hit is None:
                nxt = []
                for n in queue2:
                    if not _walkable(n):
                        continue
                    for m in adj.get(n, ()):
                        if m in prev or not _walkable(m):
                            continue
                        prev[m] = n
                        if m in backbone:
                            hit = m
                            break
                        nxt.append(m)
                    if hit is not None:
                        break
                queue2 = nxt
            if hit is None:
                return 0
            node, n_added = hit, 0
            while node is not None:
                if (node not in backbone
                        and str(rows[node].get("TRENCH_TIER") or "") != "Garden"):
                    row = rows[node]
                    # Provenance, only when the TIER actually changes. A span
                    # that already reads Feeder is merely absorbed into the
                    # backbone set here; recording it as "promoted" would
                    # fabricate a history that never happened (measured: 11 of
                    # 27 absorbed spans on one Berlin run). `or` keeps the
                    # FIRST origin if a span is ever promoted twice.
                    was = str(row.get("TRENCH_TIER") or "")
                    if was and was != "Feeder":
                        row["PROMOTED_FROM"] = (row.get("PROMOTED_FROM")
                                                or was)
                    row["TRENCH_TIER"] = "Feeder"
                    backbone.add(node)
                    comp.add(node)
                    feeder.add(node)
                    n_added += 1
                node = prev[node]
            return n_added

        def _spur(src_i, x, y):
            """ONE Feeder/Open Cut spur from the backbone to the cabinet."""
            best, tx, ty, tgt = float("inf"), None, None, None
            p = _pt(x, y)
            for i in backbone:
                g = rows[i].get("_geom")
                if g is None:
                    continue
                near = g.nearestPoint(p)
                if near is None or near.isEmpty():
                    continue
                q = near.asPoint()
                d = ((q.x() - x) ** 2 + (q.y() - y) ** 2) ** 0.5
                if d < best:
                    best, tx, ty, tgt = d, q.x(), q.y(), i
            if tgt is None or best > self._FEEDER_BRIDGE_MAX_M or best <= 0.05:
                return False
            # The connector must terminate at the actual cabinet coordinate.
            # Starting it at the detached trench's nearest point instead can
            # leave a second, unmeasured gap between that span and the PDP.
            sx, sy = x, y
            if ((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5 <= 0.05:
                return False
            cls = str(rows[tgt].get("trench_type") or "Open Cut")
            if cls not in TRENCH_WIDTH_MM:
                cls = "Open Cut"
            spur_id = "FEEDER-SPUR-%03d" % (spurs + 1)
            geom = QgsGeometry.fromPolylineXY(
                [QgsPointXY(sx, sy), QgsPointXY(tx, ty)])
            row = dict(rows[tgt])          # same street / surface context
            row.update({
                "_geom": QgsGeometry.fromMultiPolylineXY(
                    [[QgsPointXY(sx, sy), QgsPointXY(tx, ty)]]),
                # Only stamp an origin when the design is partitioned; on a
                # legacy single-origin run every span is deliberately unstamped.
                **({"MFG_ID": mfg_id} if partitioned else {}),
                "TRENCH_ID": spur_id,
                "RUN_ID": spur_id,
                "TRENCH_TIER": "Feeder",
                # The row was copied from `rows[tgt]`, so drop any provenance
                # it would otherwise inherit: this spur is new Feeder
                # construction, not a promoted distribution spine.
                "PROMOTED_FROM": None,
                "SERVES_TIER": "Feeder",
                "trench_type": cls,
                "USAGE_TYPE": cls,
                "CONSTRUCT": cls,
                "addr_id": None, "obj_id": None,
                "hhs": None, "HH": None,
                "START_CHAMBER": None, "END_CHAMBER": None,
                "length_m": round(geom.length(), 2),
                "SPAN_LEN_M": round(geom.length(), 2),
                "SPAN_INDEX": 1, "SPAN_COUNT": 1,
                "SPAN_KIND": "Unchambered",
                "INFRA_STATUS": "New",
                "VERIFY_STATUS": "Designed",
                "WIDTH_MM": TRENCH_WIDTH_MM.get(cls, 300),
                "DEPTH_MM": TRENCH_DEPTH_MM.get(cls, 900),
                "SRC": "feeder-reach",
                "method": "designed",
            })
            idx = len(rows)
            rows.append(row)
            adj.setdefault(idx, set()).add(tgt)
            adj.setdefault(tgt, set()).add(idx)
            comp.add(idx)
            backbone.add(idx)
            feeder.add(idx)
            return True

        non_garden = None
        for anchor in anchors:
            label, aid, x, y = anchor[:4]
            if label != "PDP" or (len(anchor) > 4 and str(anchor[4] or "") != mfg_id):
                continue
            if x is None or y is None:
                continue
            d_any, start = _near(x, y, only=owned_rows)
            if start is None:
                continue
            if not _walkable(start):
                # The splitter's own trench is not the drop leg it happens to
                # sit next to — work from the nearest non-Garden span.
                if non_garden is None:
                    non_garden = {i for i in range(len(rows)) if _walkable(i)}
                _dg, alt = _near(x, y, only=non_garden & owned_rows)
                if alt is not None:
                    start = alt
            if start in comp:
                # Welded to the MFG — only the TIER can be wrong.
                d_back, back_i = _near(x, y, only=backbone)
                if back_i is None or d_back > self._BACKBONE_TOL_M:
                    promoted += _promote(start)
                continue
            # Detached island: the PDP is on trench that no corridor reaches.
            if _spur(start, x, y):
                spurs += 1
                # The spur just joined the whole island to the backbone, so a
                # second PDP sitting on it needs no spur of its own.
                island = {start}
                q3 = [start]
                while q3:
                    nxt = []
                    for n in q3:
                        for m in adj.get(n, ()):
                            if m not in island and m not in comp:
                                island.add(m)
                                nxt.append(m)
                    q3 = nxt
                comp |= island
                backbone |= {i for i in island if i in feeder}
            else:
                far.append((label, aid, round(d_any, 1)))

        if promoted and feedback:
            feedback.pushInfo(_tr(
                "Backbone reach: {0} span(s) relabelled Feeder so the MFG → "
                "every PDP path exists on the published trench.").format(promoted))
        if spurs and feedback:
            feedback.pushInfo(_tr(
                "Backbone reach: {0} feeder spur(s) added — a splitter that had "
                "no welded path to the backbone now has one.").format(spurs))
        if far and feedback:
            feedback.pushWarning(_tr(
                "Backbone reach: {0} anchor(s) could not be joined to the MFG "
                "within {1:g} m: {2}").format(
                    len(far), self._FEEDER_BRIDGE_MAX_M,
                    ", ".join("{0} {1} ({2} m)".format(*f) for f in far[:8])))
        return promoted + spurs

    @staticmethod
    def _stamp_tier_provenance(rows: List[dict]) -> int:
        """Derive ``SERVES_TIER`` from ``PROMOTED_FROM`` on every span.

        The last write before the sinks, so the published layer can never show
        a ``SERVES_TIER`` that disagrees with the promotion record. Keeping the
        derivation in one place is what makes it safe for a consumer to group
        by ``SERVES_TIER`` alone instead of re-deriving the rule.

        Returns how many spans carry provenance, i.e. were promoted, so the run
        can report it.
        """
        promoted = 0
        for row in rows:
            origin = str(row.get("PROMOTED_FROM") or "").strip()
            if origin:
                promoted += 1
            row["PROMOTED_FROM"] = origin or None
            row["SERVES_TIER"] = origin or str(row.get("TRENCH_TIER") or "")
        return promoted

    def _final_rows(self, final: QgsVectorLayer) -> List[dict]:
        """Designer span rows → the pipeline's Final_Trenches contract."""
        names = final.fields().names()

        def g(f, name, default=None):
            return f[name] if name in names and f[name] is not None else default

        rows: List[dict] = []
        for f in final.getFeatures():
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            ttype = str(g(f, "TRENCH_TYPE", "Open Cut") or "Open Cut").strip()
            if ttype not in TRENCH_WIDTH_MM:
                ttype = "Open Cut"
            tier = str(g(f, "TRENCH_TIER", "Distribution") or "Distribution").strip()
            if tier not in _TIER_GEOM:
                tier = "Distribution"
            addr = g(f, "ADDR_ID")
            addr_s = str(addr).strip() if addr is not None and str(addr).strip() else None
            hh = _as_float(g(f, "HH"))
            start = str(g(f, "START_NODE") or "").strip() or None
            end = str(g(f, "END_NODE") or "").strip() or None
            rows.append({
                "_geom": geom,
                "TRENCH_ID": g(f, "TRENCH_ID"),
                "RUN_ID": g(f, "RUN_ID"),
                "MFG_ID": g(f, "MFG_ID"),
                "trench_type": ttype,
                # The legacy publication writes the construction class into all
                # three aliases; the platform re-normalises them on the way out.
                "USAGE_TYPE": ttype,
                "CONSTRUCT": ttype,
                "TRENCH_TIER": tier,
                # The designer does not emit provenance today; read it back if
                # a future designer does, so the field survives a round trip.
                "PROMOTED_FROM": g(f, "PROMOTED_FROM"),
                "SERVES_TIER": g(f, "SERVES_TIER") or tier,
                "obj_id": addr_s,
                "addr_id": addr_s,
                "hhs": None if hh is None else str(int(hh)),
                "HH": hh,
                "length_m": _as_float(g(f, "length_m")),
                "SPAN_LEN_M": _as_float(g(f, "length_m")),
                "PDP_ID": g(f, "PDP_ID"),
                "POLYGON_ID": g(f, "POLYGON_ID"),
                "START_CHAMBER": start,
                "END_CHAMBER": end,
                "SPAN_INDEX": _as_int(g(f, "SPAN_INDEX")) or 1,
                "SPAN_COUNT": _as_int(g(f, "SPAN_COUNT")) or 1,
                "SPAN_KIND": "Chamber span" if (start and end) else "Unchambered",
                "INFRA_STATUS": g(f, "INFRA_STATUS", "New") or "New",
                "VERIFY_STATUS": g(f, "VERIFY_STATUS", "Designed") or "Designed",
                "SURFACE": g(f, "SURFACE"),
                "REINSTATE": g(f, "REINSTATE"),
                "sidewalk": g(f, "SURFACE"),
                "method": "designed",
                "WIDTH_MM": TRENCH_WIDTH_MM.get(ttype, 300),
                "DEPTH_MM": TRENCH_DEPTH_MM.get(ttype, 900),
                "SRC": g(f, "SRC", "trench-designer") or "trench-designer",
                "AERIAL_ZONE": _as_int(g(f, "AERIAL_ZONE")) or 0,
                "AERIAL_ZONE_REASON": g(f, "AERIAL_ZONE_REASON"),
            })
        return rows

    @staticmethod
    def _consolidate_physical_corridors(rows: List[dict], feedback=None) -> List[dict]:
        """Collapse duplicate physical trench geometry without changing routing.

        The designer can emit one route record per PDP/polygon even when those
        records ride the same physical corridor.  Keep the designer geometry as
        authoritative, but dissolve only lines that have the same construction
        type/tier and actually touch or overlap.  Parallel lines on opposite
        sides of a street remain separate, as do HDD, open-cut, and garden
        geometry.  Chamber segmentation runs later on the consolidated rows.
        """
        if not rows:
            return rows

        def value(row, key):
            return str(row.get(key) or "").strip()

        def append_unique(values, value):
            for item in str(value or "").split(","):
                item = item.strip()
                if item and item not in values:
                    values.append(item)

        # Garden legs are premise-specific branches, not a shared corridor.
        # Never dissolve them merely because two premises meet at the network.
        groups = {}
        for row in rows:
            tier = value(row, "TRENCH_TIER")
            # Surface/reinstatement are attributes of individual sub-spans,
            # not physical identity. Keeping them in this key preserved every
            # drafting break as a separate trench even when the route, tier and
            # construction method were continuous. Chamber/PDP anchors are the
            # authoritative breaks downstream.
            key = (value(row, "trench_type"), tier, value(row, "MFG_ID"),
                   "garden" if tier == "Garden" else "main")
            groups.setdefault(key, []).append(row)

        consolidated = []
        merged_rows = 0
        merged_groups = 0
        for key, members in groups.items():
            clusters = []
            for row in members:
                geom = row.get("_geom")
                if geom is None or geom.isEmpty():
                    consolidated.append(row)
                    continue
                host = None
                # Only exact/near contact is eligible.  This cannot pull a
                # sidewalk route across a street to another side.
                for cluster in clusters:
                    if any(geom.distance(other.get("_geom")) <= 0.05
                           for other in cluster):
                        host = cluster
                        break
                if host is None:
                    clusters.append([row])
                else:
                    host.append(row)

            for cluster in clusters:
                if len(cluster) == 1 or key[-1] == "garden":
                    consolidated.extend(cluster)
                    continue
                geoms = [r["_geom"] for r in cluster
                         if r.get("_geom") is not None and not r["_geom"].isEmpty()]
                merged_geom = _unary_union_geoms(geoms)
                if merged_geom is None or merged_geom.isEmpty():
                    consolidated.extend(cluster)
                    continue
                merged_geom = _merge_contiguous_runs(merged_geom, grid=0.01)
                base = dict(cluster[0])
                base["_geom"] = merged_geom
                for field in ("PDP_ID", "POLYGON_ID", "MFG_ID", "addr_id", "obj_id"):
                    values = []
                    for row in cluster:
                        append_unique(values, row.get(field))
                    if values:
                        base[field] = ",".join(values)
                base["length_m"] = round(merged_geom.length(), 2)
                base["SPAN_LEN_M"] = base["length_m"]
                base["SPAN_COUNT"] = 1
                base["SPAN_INDEX"] = 1
                base["SOURCE_RUNS"] = len(cluster)
                consolidated.append(base)
                merged_rows += len(cluster) - 1
                merged_groups += 1

        if feedback and merged_rows:
            feedback.pushInfo(_tr(
                "Physical trench consolidation: {0} coincident/connected "
                "main-corridor row(s) folded into {1} physical feature(s); "
                "garden legs and parallel opposite-side routes preserved. "
                "Chamber splitting remains downstream.").format(
                    merged_rows, merged_groups))
        return consolidated

    @staticmethod
    def _pdp_points(pdps: QgsVectorLayer) -> Dict[str, QgsPointXY]:
        """PDP id → its position in the project CRS."""
        names = pdps.fields().names()
        f_pdp = _pick_field(pdps, ["PDP_ID", "pdp_id", "pdp"])
        if not f_pdp:
            return {}
        out: Dict[str, QgsPointXY] = {}
        for f in pdps.getFeatures():
            if f[f_pdp] is None:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.asPoint()
            out[str(f[f_pdp])] = QgsPointXY(pt.x(), pt.y())
        return out

    def _distribution_routes(self, final_rows: List[dict], pdp_xy, feedback
                             ) -> List[dict]:
        """RETAINED FOR FALLBACK USE — currently unused by processAlgorithm.

        The per-address routing this method implements was the historic way to
        satisfy cable_layer's shared-footway-endpoint join: one corridor per
        addressed premise, routed along the designer's spans from the premise's
        splitter to its footway attachment point. It duplicated the spine once
        per house (25 km drawn for ~1.4 km of unique geometry on Berlin), which
        is why the stage now publishes the shared spine and cable_layer groups
        trunk cables per span instead. Kept, documented and reachable so a run
        that must reproduce the old per-house corridors can call it directly.
        """
        import heapq

        # ── node the mains, then build the route graph ───────────────────
        # The spans alone are not a usable graph: within one run consecutive
        # spans do share their endpoints, but a run routinely ENDS in the middle
        # of another run's span (every spine meets the feeder at a T, and the
        # operator who joins them only ever placed one of the two points on the
        # other's endpoint). Measured on Berlin: 202 mains spans, 224 distinct
        # endpoints — i.e. almost every span its own island, so a route from the
        # splitter to a drop could not be found. Noding the mains at their own
        # intersections restores the connectivity the design actually has.
        mains_geoms: List[QgsGeometry] = []
        for row in final_rows:
            if row["TRENCH_TIER"] == "Garden":
                continue
            line = _first_line(row.get("_geom"))
            if line is not None:
                mains_geoms.append(line)
        noded = self._node_lines(mains_geoms)
        if not noded:
            noded = mains_geoms

        by_coord: Dict[Tuple[int, int], int] = {}
        coords_of: Dict[int, QgsPointXY] = {}

        def endpoint_vertex(pt) -> int:
            # 5 cm cells: noding produces shared coordinates, so near-exact
            # matching is what joins the pieces at a junction.
            k = (int(round(pt.x() * 20)), int(round(pt.y() * 20)))
            v = by_coord.get(k)
            if v is None:
                v = len(coords_of)
                by_coord[k] = v
                coords_of[v] = QgsPointXY(pt.x(), pt.y())
            return v

        adj: Dict[int, List[Tuple[int, float, List[QgsPointXY]]]] = {}
        for geom in noded:
            for part in ([geom] if not geom.isMultipart() else
                         [QgsGeometry.fromPolylineXY([QgsPointXY(p.x(), p.y())
                                                      for p in seg])
                          for seg in geom.asMultiPolyline()]):
                pts = part.asPolyline()
                if len(pts) < 2:
                    continue
                length = sum(((pts[i + 1].x() - pts[i].x()) ** 2 +
                              (pts[i + 1].y() - pts[i].y()) ** 2) ** 0.5
                             for i in range(len(pts) - 1))
                a, b = endpoint_vertex(pts[0]), endpoint_vertex(pts[-1])
                if a == b:
                    continue
                adj.setdefault(a, []).append((b, length, pts))
                adj.setdefault(b, []).append((a, length,
                                              list(reversed(pts))))
        root_coords = dict(coords_of)
        feedback.pushInfo(_tr(
            "  distribution routing network: {0} piece(s), {1} node(s)")
            .format(sum(len(v) for v in adj.values()) // 2, len(adj)))

        # Every addressed drop leg: where it attaches and who feeds it.
        want: Dict[str, dict] = {}
        for row in final_rows:
            if row["TRENCH_TIER"] != "Garden" or not row.get("addr_id"):
                continue
            addr = row["addr_id"]
            if addr in want:
                continue
            line = _first_line(row.get("_geom"))
            if line is None:
                continue
            pts = line.asPolyline()
            if len(pts) < 2:
                continue
            # The route must start at the splitter, so the start vertex is the
            # network node nearest the PDP's own position. (Searching the spans'
            # ADDR_ID for the PDP id cannot work: that field holds premises.)
            pdp_pt = pdp_xy.get(str(row.get("PDP_ID") or ""))
            start = (self._nearest_vertex(adj, root_coords, pdp_pt)
                     if pdp_pt is not None else None)
            want[addr] = {"attach": pts[-1], "start": start, "template": row}

        out: List[dict] = []
        unrouted: List[str] = []
        why: Dict[str, int] = {"no splitter position": 0, "no splitter node": 0,
                              "splitter node == attachment node": 0,
                              "not connected": 0}
        for addr, spec in want.items():
            attach = spec["attach"]
            if spec["start"] is None:
                why["no splitter position" if spec.get("pdp") is None
                    else "no splitter node"] += 1
                unrouted.append(addr)
                continue
            target = self._nearest_vertex(adj, root_coords, attach)
            if target is not None and target == spec["start"]:
                # The drop leg attaches at (or beside) the splitter's own node —
                # the corridor is that one hop, not a search.
                why["splitter node == attachment node"] += 1
                node = root_coords[target]
                path = [QgsPointXY(node.x(), node.y())]
                if ((node.x() - attach.x()) ** 2 +
                        (node.y() - attach.y()) ** 2) ** 0.5 > 0.01:
                    path.append(QgsPointXY(attach.x(), attach.y()))
                if len(path) < 2:
                    unrouted.append(addr)
                    continue
            else:
                path = self._dijkstra(adj, root_coords, spec["start"], attach)
            if not path:
                why["not connected"] += 1
                unrouted.append(addr)
                continue
            row = dict(spec["template"])
            row["TRENCH_TIER"] = "Distribution"
            row["addr_id"] = addr
            row["obj_id"] = addr
            row["length_m"] = round(sum(
                ((path[i + 1].x() - path[i].x()) ** 2 +
                 (path[i + 1].y() - path[i].y()) ** 2) ** 0.5
                for i in range(len(path) - 1)), 2)
            row["SPAN_LEN_M"] = row["length_m"]
            row["SPAN_KIND"] = "Route"
            row["SRC"] = "designed-route"
            row["START_CHAMBER"] = None
            row["END_CHAMBER"] = None
            row["_geom"] = QgsGeometry.fromPolylineXY(path)
            out.append(row)

        # A premise whose corridor could not be routed still needs a
        # distribution row, or no distribution cable is built for it at all.
        # The shared spine span that names it is the honest fallback: the
        # geometry is the real trench, only the endpoint is a node instead of
        # the footway point.
        if unrouted:
            fallback_by_addr = {}
            for row in final_rows:
                if row["TRENCH_TIER"] != "Distribution":
                    continue
                for a in str(row.get("addr_id") or "").split(","):
                    a = a.strip()
                    if a and a not in fallback_by_addr:
                        fallback_by_addr[a] = row
            added = 0
            for addr in unrouted:
                row = fallback_by_addr.get(addr)
                if row is None:
                    continue
                clone = dict(row)
                clone["addr_id"] = addr
                clone["obj_id"] = addr
                out.append(clone)
                added += 1
            feedback.pushWarning(_tr(
                "{0} premise(s) could not be routed to their splitter ({1}) and "
                "fall back to their shared spine span ({2} of {0} covered); "
                "neither shares the garden's footway endpoint.").format(
                    len(unrouted),
                    ", ".join("%s=%d" % kv for kv in sorted(why.items())),
                    added))
        return out

    @staticmethod
    def _node_lines(geoms: List[QgsGeometry]) -> List[QgsGeometry]:
        """Split every line where any other line ends on it (or crosses it).

        Without this the mains are a bag of independent spans and no route can
        be found between two of them.
        """
        if not geoms:
            return []
        try:
            mem = QgsVectorLayer("LineString?crs=EPSG:25833", "mains", "memory")
            provider = mem.dataProvider()
            feats = []
            for g in geoms:
                f = QgsFeature()
                f.setGeometry(g)
                feats.append(f)
            provider.addFeatures(feats)
            mem.updateExtents()
            out = processing.run(
                "native:splitwithlines",
                {"INPUT": mem, "LINES": mem, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})
            result = out.get("OUTPUT")
            if isinstance(result, QgsVectorLayer) and result.isValid():
                return [f.geometry() for f in result.getFeatures()
                        if f.geometry() and not f.geometry().isEmpty()]
        except Exception:
            return []
        return []

    @staticmethod
    def _nearest_vertex(adj, root_coords, point):
        """The graph vertex nearest ``point`` (graph vertices only)."""
        best, best_d = None, None
        for node, pt in root_coords.items():
            if node not in adj:
                continue
            d = ((pt.x() - point.x()) ** 2 + (pt.y() - point.y()) ** 2) ** 0.5
            if best_d is None or d < best_d:
                best, best_d = node, d
        return best

    @staticmethod
    def _dijkstra(adj, root_coords, start, attach):
        """Shortest path start → the vertex nearest the attachment."""
        import heapq
        if start is None or not adj:
            return None
        target = TrenchDesignLayerAlgorithm._nearest_vertex(adj, root_coords, attach)
        if target is None:
            return None
        dist = {start: 0.0}
        prev: Dict[int, Tuple[int, List[QgsPointXY]]] = {}
        heap = [(0.0, start)]
        seen = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in seen:
                continue
            seen.add(node)
            if node == target:
                break
            for nxt, w, pts in adj.get(node, ()):
                nd = d + w
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    prev[nxt] = (node, pts)
                    heapq.heappush(heap, (nd, nxt))
        if target not in dist:
            return None
        legs: List[List[QgsPointXY]] = []
        node = target
        while node != start:
            step = prev.get(node)
            if step is None:
                return None
            parent, pts = step              # pts runs parent → node
            legs.append(list(pts))
            node = parent
        legs.reverse()
        points: List[QgsPointXY] = []
        for leg in legs:
            points.extend(leg if not points else leg[1:])
        if not points:
            return None
        # Close onto the drop leg's attachment point; the designer snapped the
        # leg to the corridor, so this is normally a sub-metre connector.
        if ((points[-1].x() - attach.x()) ** 2 +
                (points[-1].y() - attach.y()) ** 2) ** 0.5 > 0.01:
            points.append(QgsPointXY(attach.x(), attach.y()))
        return points

    @staticmethod
    def _fan_out_per_address(rows: List[dict]) -> List[dict]:
        """One row per address, for the layer ``cable_layer`` indexes by address.

        The designer's distribution layer is a SHARED spine: a span that carries
        several houses' drops names every address it serves (comma-joined, up to
        10 on Berlin). ``cable_layer`` builds its ``addr → feature`` index from a
        single value, so a comma-joined key matches no garden leg and no
        distribution cable is produced at all. Repeating the span per address in
        this one layer fixes that without touching the shared geometry anywhere
        it is displayed or measured.
        """
        out: List[dict] = []
        for row in rows:
            addr = row.get("addr_id")
            if not addr or "," not in str(addr):
                out.append(row)
                continue
            addrs = [a.strip() for a in str(addr).split(",") if a.strip()]
            if not addrs:
                out.append(row)
                continue
            for a in addrs:
                clone = dict(row)
                clone["addr_id"] = a
                clone["obj_id"] = a
                out.append(clone)
        return out

    @staticmethod
    def _pseudo_rows(final_rows: List[dict]) -> List[dict]:
        """The footway end of every garden leg — the pseudo object point.

        ``cable_layer`` groups garden rows by their LAST vertex, so the pseudo
        point must be that same vertex and not the house.
        """
        rows: List[dict] = []
        for row in final_rows:
            if row["TRENCH_TIER"] != "Garden":
                continue
            line = _first_line(row.get("_geom"))
            if line is None:
                continue
            pts = line.asPolyline()
            if len(pts) < 2:
                continue
            end = pts[-1]
            rows.append({
                "pdp_pol_id": row.get("PDP_ID"),
                "addr_id": row.get("addr_id"),
                "hh_id": row.get("addr_id"),
                "sidewalk": row.get("SURFACE"),
                "side": row.get("sidewalk"),
                "method": "designed",
                "dist_m": row.get("length_m"),
                "proj_fid": None,
                "pdp_id": row.get("PDP_ID"),
                "POLYGON_ID": row.get("POLYGON_ID"),
                "MFG_ID": row.get("MFG_ID"),
                "_geom": QgsGeometry.fromPointXY(QgsPointXY(end.x(), end.y())),
            })
        return rows

    @staticmethod
    def _attach_premise_lookup(rows: List[dict], objects: QgsVectorLayer,
                               epsg: int, context, feedback) -> List[dict]:
        """Fill PDP_ID / POLYGON_ID on a span from the premise it serves.

        The designer's spine spans already carry the splitter; its drop legs do
        not, and ``duct_layer`` skips the whole Distribution step when the
        endpoint points have no PDP field with values. The objects layer holds
        both ids per address, so this is a join, not a guess.
        """
        if objects is None:
            return rows
        # Work in the designer CRS: object coordinates originate in the
        # plan CRS, while published trench geometry is projected.
        objects = TrenchDesignLayerAlgorithm._reproject(
            objects, epsg, context, feedback
        ) or objects
        f_addr = _pick_field(objects, ["ADDR_ID", "addr_id", "id"])
        f_pdp = _pick_field(objects, ["PDP_ID", "pdp_id"])
        f_poly = _pick_field(objects, ["POLYGON_ID", "polygon_id"])
        f_mfg = _pick_field(objects, ["MFG_ID", "mfg_id"])
        if not f_addr or not (f_pdp or f_poly):
            return rows
        lookup: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}
        for f in objects.getFeatures():
            key = str(f[f_addr]).strip() if f[f_addr] is not None else ""
            if not key or key in lookup:
                continue
            lookup[key] = (
                str(f[f_pdp]) if f_pdp and f[f_pdp] is not None else None,
                str(f[f_poly]) if f_poly and f[f_poly] is not None else None,
                str(f[f_mfg]) if f_mfg and f[f_mfg] is not None else None,
            )
        for row in rows:
            addr = row.get("addr_id")
            if not addr:
                continue
            hit = lookup.get(str(addr).split(",")[0].strip())
            if not hit:
                continue
            pdp_id, poly_id, mfg_id = hit
            if not row.get("PDP_ID"):
                row["PDP_ID"] = pdp_id
            if not row.get("POLYGON_ID"):
                row["POLYGON_ID"] = poly_id
            if not row.get("MFG_ID"):
                row["MFG_ID"] = mfg_id
        return rows

    @staticmethod
    def _reproject(layer: Optional[QgsVectorLayer], epsg: int, context, feedback
                   ) -> Optional[QgsVectorLayer]:
        """A copy of ``layer`` in ``epsg``.

        Required before any spatial query against the published trenches: the
        plan layers arrive as WGS84 in some runs and the trenches are always
        published in the project CRS. Measuring a degree-space point against a
        metre-space network silently produces distances in the millions.
        """
        if layer is None:
            return None
        try:
            if layer.crs().postgisSrid() == int(epsg):
                return layer
        except Exception:
            pass
        try:
            out = processing.run(
                "native:reprojectlayer",
                {"INPUT": layer, "TARGET_CRS": QgsCoordinateReferenceSystem(
                    "EPSG:%d" % int(epsg)),
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
            return _as_layer(out, context, "reprojected")
        except Exception as exc:
            feedback.pushWarning(_tr(
                "Could not reproject the PDP layer ({0}); PDP projections are "
                "skipped.").format(exc))
            return None

    @staticmethod
    def _pdp_projection_rows(pdps: QgsVectorLayer,
                             final: QgsVectorLayer,
                             epsg: int,
                             snapped: Dict[str, Tuple[float, float]] = None) -> List[dict]:
        """PDP → nearest point on the designed network, as a short line.

        ``cable_layer`` prepends this to the drop cable when the cabinet sits
        back from the street (PDP → pseudo-PDP → footway → object).

        ``snapped`` overrides the cabinet position with the one the backbone
        snap chose; the published connector then runs from the splitter that is
        ON the open-cut trench, not from the plan point it was moved off.
        """
        snapped = snapped or {}
        idx = QgsSpatialIndex(final.getFeatures())
        pdp_names = pdps.fields().names()
        f_pdp = _pick_field(pdps, ["PDP_ID", "pdp_id", "pdp"])
        f_poly = _pick_field(pdps, ["POLYGON_ID", "polygon_id"])
        f_mfg = _pick_field(pdps, ["MFG_ID", "mfg_id"])

        def value(f, name):
            if name and name in pdp_names and f[name] is not None:
                return str(f[name])
            return None

        rows: List[dict] = []
        for f in pdps.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.asPoint()
            _aid = value(f, f_pdp)
            if _aid and str(_aid) in snapped:
                sx, sy = snapped[str(_aid)]
                pt = QgsPointXY(sx, sy)
            near = idx.nearestNeighbor(pt, 1)
            if not near:
                continue
            target = final.getFeature(near[0])
            tg = target.geometry()
            if tg is None or tg.isEmpty():
                continue
            proj = tg.nearestPoint(QgsGeometry.fromPointXY(pt))
            if proj is None or proj.isEmpty():
                continue
            q = proj.asPoint()
            if abs(q.x() - pt.x()) < 0.01 and abs(q.y() - pt.y()) < 0.01:
                continue                     # the cabinet already sits on a trench
            rows.append({
                "PDP_ID": value(f, f_pdp),
                "POLYGON_ID": value(f, f_poly),
                "MFG_ID": value(f, f_mfg),
                "distance_m": round(((q.x() - pt.x()) ** 2 +
                                     (q.y() - pt.y()) ** 2) ** 0.5, 2),
                "_geom": QgsGeometry.fromPolylineXY([pt, QgsPointXY(q.x(), q.y())]),
            })
        return rows

    def _publish_aerial_drops(self, parameters, context, work: str, out_dir: str,
                              target_epsg: int, feedback, sinks: Dict) -> int:
        """Publish the designer's aerial legs on the stage's own layer.

        These legs are classified, not dug: they are the ones that are inside
        an aerial zone, longer than the UG drop limit, or branching off another
        aerial leg.  Publishing them separately is what lets the map show them
        as a construction type (`TRENCH_TYPE = "Aerial"`, the colour the legend
        reserves), the BOQ leave their excavation out and charge aerial fibre,
        and the LLD carry them as aerial instead of underground.
        """
        if self.O_AERIAL_DROPS not in parameters:
            return 0
        path = os.path.join(out_dir, "Aerial_Drops.gpkg")
        layer = self._read_gpkg(path, "Aerial_Drops") if os.path.exists(path) else None
        if layer is None or layer.featureCount() == 0:
            feedback.pushInfo(_tr(
                "Aerial drops: none — every drop leg could be trenched."))
            return 0
        names = layer.fields().names()

        def g(f, name, default=None):
            return f[name] if name in names and f[name] is not None else default

        rows: List[dict] = []
        for i, f in enumerate(layer.getFeatures(), 1):
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            length = g(f, "length_m")
            if length is None:
                length = round(geom.length(), 2)
            rows.append({
                "id": g(f, "DROP_ID"),
                "DROP_ID": g(f, "DROP_ID"),
                "POLYGON_ID": g(f, "POLYGON_ID"),
                # Aerial is a METHOD, not an inferred label: the published row
                # carries Overhead + EXCAVATION = 0 so the BOQ, the map and the
                # LLD never have to guess "not dug" from the type string.
                # The designer's aerial legs carry the address, not the splitter
                # (they leave the network on the pole line); the platform joins
                # the PDP from the polygon when it needs it.
                "PDP_ID": g(f, "PDP_ID"),
                "MFG_ID": g(f, "MFG_ID"),
                "addr_id": g(f, "ADDR_ID"),
                "HH": g(f, "HH"),
                # Tier: the drop leg this aerial span replaces ("Drop"), never
                # "Garden" — Garden is the excavated micro-trench class.
                "TRENCH_TIER": g(f, "TRENCH_TIER", "Drop") or "Drop",
                "TRENCH_TYPE": g(f, "TRENCH_TYPE", "Aerial") or "Aerial",
                "CONSTRUCTION_METHOD": (g(f, "CONSTRUCTION_METHOD", "Overhead")
                                       or "Overhead"),
                # All three class aliases read Aerial and the section is 0, so
                # nothing downstream can fold this span back into Open Cut.
                "USAGE_TYPE": "Aerial",
                "CONSTRUCT": "Aerial",
                "WIDTH_MM": 0,
                "DEPTH_MM": 0,
                "length_m": round(float(length or 0.0), 2),
                "AERIAL_REASON": g(f, "AERIAL_REASON"),
                "INFRA_STATUS": g(f, "INFRA_STATUS", "New") or "New",
                "VERIFY_STATUS": "Designed",
                # Nothing is dug for an aerial leg — the flag is the contract
                # the BOQ and the platform read instead of inferring it from
                # the type string.
                "EXCAVATION": 0,
                "SRC": "trench-designer:aerial",
                "_geom": geom,
            })
        if not rows:
            return 0
        # Register the sink id: the pipeline saves the layer by looking this
        # output up in the stage's result dict, so discarding it means the
        # classification is written to a temporary layer and never published.
        sinks[self.O_AERIAL_DROPS] = self._write_rows(
            parameters, context, self.O_AERIAL_DROPS, rows,
            _AERIAL_FIELDS, QgsWkbTypes.MultiLineString,
            target_epsg, feedback)
        total = sum(float(r["length_m"] or 0.0) for r in rows) / 1000.0
        reasons: Dict[str, int] = {}
        for r in rows:
            key = str(r.get("AERIAL_REASON") or "?")
            reasons[key] = reasons.get(key, 0) + 1
        feedback.pushInfo(_tr(
            "Aerial drops published: {0} leg(s), {1:,.2f} km NOT excavated "
            "({2}).").format(
                len(rows), total,
                ", ".join("%s=%d" % (k, v) for k, v in sorted(reasons.items()))))
        return len(rows)

    def _publish_trench_nodes(self, parameters, context, out_dir: str,
                              target_epsg: int, feedback, sinks: Dict) -> int:
        """Publish the designer's structural nodes (``Trench_Nodes``).

        The designer decides these from the geometry it builds — HDD drill
        openings, tier-change junctions, splitter locations, direction changes
        and the ~250 m pull points — and already separates them by
        ``min_node_sep_m`` / ``hdd_pit_keepout_m``. They are the chamber
        stage's primary candidates: a chamber IS the opening of the trench at
        a structural node, so deriving structures from duct junctions (the
        legacy route) placed them wherever ducts happened to cross rather than
        where the network changes tier.
        """
        if self.O_TRENCH_NODES not in parameters:
            return 0
        layer = self._read_gpkg(os.path.join(out_dir, "Trench_Nodes.gpkg"),
                               "Trench_Nodes")
        if layer is None or layer.featureCount() == 0:
            feedback.pushInfo(_tr("Trench nodes: none placed by the designer."))
            return 0
        names = layer.fields().names()

        def g(f, name, default=None):
            return f[name] if name in names and f[name] is not None else default

        rows: List[dict] = []
        for f in layer.getFeatures():
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            rows.append({
                "id": g(f, "NODE_ID"),
                "NODE_ID": g(f, "NODE_ID"),
                "NODE_TYPE": g(f, "NODE_TYPE"),
                "PRIORITY": int(_as_float(g(f, "PRIORITY")) or 0),
            "PDP_ID": g(f, "PDP_ID"),
            "MFG_ID": g(f, "MFG_ID"),
            "X": _as_float(g(f, "X")),
                "Y": _as_float(g(f, "Y")),
                "SRC": "trench-designer:node",
                "_geom": geom,
            })
        if not rows:
            return 0
        # Register the sink id: the pipeline saves the layer by looking this
        # output up in the stage's result dict, so discarding it means the
        # nodes never reach the chamber stage.
        sinks[self.O_TRENCH_NODES] = self._write_rows(
            parameters, context, self.O_TRENCH_NODES, rows, _NODE_FIELDS,
            QgsWkbTypes.Point, target_epsg, feedback)
        kinds: Dict[str, int] = {}
        for r in rows:
            key = str(r.get("NODE_TYPE") or "?")
            kinds[key] = kinds.get(key, 0) + 1
        feedback.pushInfo(_tr(
            "Trench nodes published: {0} structural node(s) ({1}) — the "
            "chamber stage places its structures on them.").format(
                len(rows), ", ".join("%s=%d" % (k, v) for k, v in sorted(kinds.items()))))
        return len(rows)

    @staticmethod
    def _drill_rows(drills: QgsVectorLayer) -> List[dict]:
        names = drills.fields().names()
        rows: List[dict] = []
        for f in drills.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            rows.append({
                "id": f["DRILL_ID"] if "DRILL_ID" in names else None,
                "DRILL_ID": f["DRILL_ID"] if "DRILL_ID" in names else None,
                "ROAD_CLASS": f["ROAD_CLASS"] if "ROAD_CLASS" in names else None,
                "WIDTH_M": _as_float(f["WIDTH_M"]) if "WIDTH_M" in names else None,
                "TRENCH_TYPE": "HDD",
                "INFRA_STATUS": "New",
                "_geom": g,
            })
        return rows

    # ---- derived geometry ----------------------------------------------
    def _sidewalk_layers(self, final: QgsVectorLayer, context, feedback):
        """± offset lines around the designed corridor.

        Deviation from the legacy stage, deliberately: the legacy offset the OSM
        roads, because its trenches came from those roads. The designer's
        corridor is its own geometry, so the offsets are taken from it — the
        duct stage builds its graph on these, and a graph on legacy offsets
        while the trenches are the designer's would place ducts beside their
        trench.
        """
        try:
            # Dissolve needs the corridor's GEOMETRY only. `native:dissolve`
            # copies the input's attribute schema onto its temporary output, and
            # that output declares a 48-char string field; a run whose corridor
            # carries a concatenated address list (`addr_id` reaches 223 chars on
            # Berlin) then fails the write with "String of length 87 exceeds
            # maximum field length (48)". The except below swallows it and
            # returns (None, None), so the duct stage silently loses its
            # trench-derived offsets and falls back to default side labels.
            # Nothing downstream reads these attributes — the offsets ARE
            # geometry — so drop them and the whole failure class goes away.
            geom_only = self._geometry_only_layer(final)
            diss = self._run_child(
                "native:dissolve",
                {"INPUT": geom_only if geom_only is not None else final,
                 "FIELD": [], "SEPARATE_DISJOINT": False},
                context, feedback, "corridor_dissolved")
            left = self._run_child(
                "native:offsetline",
                {"INPUT": diss, "DISTANCE": -SIDEWALK_OFFSET_M,
                 "SEGMENTS": SIDEWALK_SEGMENTS, "JOIN_STYLE": 1,
                 "MITER_LIMIT": 2.0},
                context, feedback, "sidewalk_left")
            right = self._run_child(
                "native:offsetline",
                {"INPUT": diss, "DISTANCE": SIDEWALK_OFFSET_M,
                 "SEGMENTS": SIDEWALK_SEGMENTS, "JOIN_STYLE": 1,
                 "MITER_LIMIT": 2.0},
                context, feedback, "sidewalk_right")
        except Exception as exc:
            feedback.pushWarning(_tr(
                "Corridor offsets failed ({0}) — the duct stage will fall back "
                "to default side labels.").format(exc))
            return None, None
        return left, right

    @staticmethod
    def _geometry_only_layer(layer: QgsVectorLayer, name: str = "corridor_geom",
                             feedback=None) -> Optional[QgsVectorLayer]:
        """An in-memory copy of ``layer`` with its geometry and NO attributes.

        Used only for derived helper geometry (offsets, buffers), whose own
        attribute set is a rendering detail, not a contract — and whose schema
        is a liability, because a child algorithm's temporary output may declare
        a narrower string field than the values in it. Returns ``None`` when the
        copy cannot be built, so callers fall back to the source layer and
        nothing is lost but the optimisation.
        """
        if layer is None:
            return None
        try:
            uri = "%s?crs=%s" % (
                QgsWkbTypes.displayString(layer.wkbType()), layer.crs().authid())
            mem = QgsVectorLayer(uri, name, "memory")
            if not mem.isValid():
                return None
            feats: List[QgsFeature] = []
            for f in layer.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                out = QgsFeature()
                out.setGeometry(g)
                feats.append(out)
            if not feats:
                return None
            mem.dataProvider().addFeatures(feats)
            return mem
        except Exception as exc:  # pragma: no cover - defensive
            if feedback is not None:
                feedback.pushInfo(_tr(
                    "  geometry-only copy failed ({0}); using the source "
                    "layer.").format(exc))
            return None

    def _aoi_layer(self, polys: QgsVectorLayer, context, feedback):
        """Dissolved design boundary (Polygons + AOI buffer) — legacy parity."""
        try:
            return self._run_child(
                "native:buffer",
                {"INPUT": polys, "DISTANCE": AOI_BUFFER_M, "SEGMENTS": 16,
                 "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2.0,
                 "DISSOLVE": True},
                context, feedback, "aoi_dissolved")
        except Exception as exc:
            feedback.pushWarning(_tr(
                "AOI derivation failed ({0}) — chambers will not be clipped to "
                "the design boundary.").format(exc))
            return None

    @staticmethod
    def _run_child(alg_id: str, params: dict, context, feedback, hint: str):
        """Run a child algorithm and resolve its temporary output to a layer."""
        out = dict(params)
        out["OUTPUT"] = QgsProcessing.TEMPORARY_OUTPUT
        result = processing.run(alg_id, out, context=context, feedback=feedback,
                                is_child_algorithm=True)
        return _as_layer(result.get("OUTPUT"), context, hint)

    # ---- sinks ----------------------------------------------------------
    def _write_rows(self, parameters, context, key, rows: List[dict], spec,
                    geom_type, epsg: int, feedback):
        fields = _fields(spec)
        names = [n for n, _t in spec]
        sink, dest = self.parameterAsSink(
            parameters, key, context, fields, geom_type, self._crs(epsg))
        if sink is None:
            feedback.pushWarning(_tr(
                "Output '{0}' is not available; skipped.").format(key))
            return dest
        count = 0
        for row in rows:
            geom = row.get("_geom")
            if geom is None or geom.isEmpty():
                continue
            if geom_type == QgsWkbTypes.LineString and geom.isMultipart():
                # The tier mirrors are LINESTRING (legacy parity) but a span is
                # published as a one-part MultiLineString.
                geom = _first_line(geom)
                if geom is None:
                    continue
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            for n in names:
                if n in row and row[n] is not None:
                    feat[n] = row[n]
            if sink.addFeature(feat, QgsFeatureSink.FastInsert):
                count += 1
        feedback.pushInfo(_tr("  published {0}: {1} feature(s)").format(key, count))
        return dest

    def _write_layer(self, parameters, context, key, layer, geom_type, epsg: int,
                     feedback):
        """Publish an existing layer (or a derived temp layer) through a sink."""
        fields = _qgs_fields_of(layer)
        sink, dest = self.parameterAsSink(
            parameters, key, context, fields, geom_type, self._crs(epsg))
        if sink is None:
            feedback.pushWarning(_tr(
                "Output '{0}' is not available; skipped.").format(key))
            return dest
        if layer is None:
            return dest
        count = 0
        for f in layer.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            # Geometry only: these are derived helpers (offsets, buffers), and
            # their own attribute set is a rendering detail, not a contract.
            feat = QgsFeature(fields)
            feat.setGeometry(g)
            if sink.addFeature(feat, QgsFeatureSink.FastInsert):
                count += 1
        feedback.pushInfo(_tr("  published {0}: {1} feature(s)").format(key, count))
        return dest

    @staticmethod
    def _crs(epsg: int) -> QgsCoordinateReferenceSystem:
        return QgsCoordinateReferenceSystem("EPSG:%d" % int(epsg))
