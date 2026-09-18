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
    ("AERIAL", QMetaType.Type.Int),
    ("AERIAL_REASON", QMetaType.Type.QString),
)

_DRILL_FIELDS: Tuple[Tuple[str, object], ...] = (
    ("id", QMetaType.Type.QString),
    ("DRILL_ID", QMetaType.Type.QString),
    ("ROAD_CLASS", QMetaType.Type.QString),
    ("WIDTH_M", QMetaType.Type.Double),
    ("TRENCH_TYPE", QMetaType.Type.QString),
    ("INFRA_STATUS", QMetaType.Type.QString),
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

        out_dir = os.path.join(work, "design")
        cfg = {
            "mfg": src["mfg"], "pdps": src["pdps"], "objects": src["objects"],
            "polygons": src["polygons"], "roads": src["roads"], "out": out_dir,
            "aerial": None, "target_epsg": int(target_epsg),
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
        final_rows = self._attach_premise_lookup(final_rows, objects)
        if not final_rows:
            raise QgsProcessingException(_tr(
                "The trench designer produced no usable spans."))
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

        # ── derived layers
        pseudo_rows = self._pseudo_rows(final_rows)
        sinks[self.O_PSEUDO_HH] = self._write_rows(
            parameters, context, self.O_PSEUDO_HH, pseudo_rows, _PSEUDO_FIELDS,
            QgsWkbTypes.Point, target_epsg, feedback)
        feedback.pushInfo(_tr("  pseudo objects on the footway: {0}").format(
            len(pseudo_rows)))

        proj_rows = self._pdp_projection_rows(pdps_t, final, target_epsg)
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
                "AERIAL": _as_int(g(f, "AERIAL")) or 0,
                "AERIAL_REASON": g(f, "AERIAL_REASON"),
            })
        return rows

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
    def _attach_premise_lookup(rows: List[dict], objects: QgsVectorLayer) -> List[dict]:
        """Fill PDP_ID / POLYGON_ID on a span from the premise it serves.

        The designer's spine spans already carry the splitter; its drop legs do
        not, and ``duct_layer`` skips the whole Distribution step when the
        endpoint points have no PDP field with values. The objects layer holds
        both ids per address, so this is a join, not a guess.
        """
        if objects is None:
            return rows
        names = objects.fields().names()
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
                             epsg: int) -> List[dict]:
        """PDP → nearest point on the designed network, as a short line.

        ``cable_layer`` prepends this to the drop cable when the cabinet sits
        back from the street (PDP → pseudo-PDP → footway → object).
        """
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
            diss = self._run_child(
                "native:dissolve",
                {"INPUT": final, "FIELD": [], "SEPARATE_DISJOINT": False},
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
