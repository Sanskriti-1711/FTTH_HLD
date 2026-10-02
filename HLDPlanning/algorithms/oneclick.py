# -*- coding: utf-8 -*-
import os
import shutil
import datetime
import time

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFeatureSink,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsSpatialIndex,
    QgsRectangle,
    QgsWkbTypes,
    QgsVectorLayer,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsVectorFileWriter,
    QgsMapLayer,
    QgsProcessingOutputFile,
    QgsProcessingParameterVectorDestination,
    Qgis,
)
from qgis import processing

from ..utils.params import ALG, LAYERNAMES, TRENCH_ENGINE
from ..utils import attr_enrich
from ..utils.style_utils import apply_simple_line_style


class PipelineStageError(QgsProcessingException):
    pass


class _LoggingFeedback(QgsProcessingFeedback):
    """Wraps a QgsProcessingFeedback and also appends messages to a log file.
    Buffers writes and flushes periodically for performance."""

    _FLUSH_INTERVAL = 50  # flush to disk every N messages

    def __init__(self, feedback, log_path):
        super().__init__()
        self._inner = feedback
        self._log_path = log_path
        self._buf = []
        self._count = 0

    def pushInfo(self, msg):
        self._write(msg)
        self._inner.pushInfo(msg)

    def pushWarning(self, msg):
        self._write(f"WARNING: {msg}")
        self._inner.pushWarning(msg)

    def pushCommandInfo(self, msg):
        self._write(msg)
        self._inner.pushCommandInfo(msg)

    def pushDebugInfo(self, msg):
        self._inner.pushDebugInfo(msg)

    def pushConsoleInfo(self, msg):
        self._inner.pushConsoleInfo(msg)

    def reportError(self, msg, fatal=False):
        self._write(f"ERROR{' (fatal)' if fatal else ''}: {msg}")
        self._inner.reportError(msg, fatal)

    def setProgress(self, progress):
        self._inner.setProgress(progress)

    def setProgressText(self, msg):
        self._inner.setProgressText(msg)

    def _write(self, msg):
        self._buf.append(msg + '\n')
        self._count += 1
        if self._count % self._FLUSH_INTERVAL == 0:
            self._flush()

    def _flush(self):
        if not self._buf:
            return
        try:
            with open(self._log_path, 'a', encoding='utf-8') as f:
                f.writelines(self._buf)
            self._buf = []
        except Exception:
            self._buf = []

    def flush(self):
        self._flush()

    @property
    def progress(self):
        return self._inner.progress

    def isCanceled(self):
        return self._inner.isCanceled()

    def cancel(self):
        self._flush()
        self._inner.cancel()


class EndToEndPipelineAlgorithm(QgsProcessingAlgorithm):

    P_EXCEL = "EXCEL"
    P_ROADS = "ROADS"

    P_SHEET = "SHEET"
    P_EMAIL = "EMAIL"
    P_OUT_CRS = "OUT_CRS"
    P_OBJ_THIN = "OBJ_THIN_EXPORT"
    P_OUTPUT_DIR = "OUTPUT_DIR"

    P_POLY_METHOD = "POLY_METHOD"
    P_POLY_PLAN_FIRST = "POLY_PLANNING_FIRST"
    P_POLY_MIN_HH = "POLY_MIN_HH"
    P_POLY_MAX_HH = "POLY_MAX_HH"
    P_POLY_NEIGH = "POLY_NEIGHBOR_DIST"
    P_POLY_SERVICE = "POLY_SERVICE_RADIUS"
    P_POLY_ACCESS = "POLY_ROAD_ACCESS_DIST"
    P_POLY_BUFFER = "POLY_BUFFER"
    P_POLY_SEEDBUF = "POLY_SEEDBUF"
    P_POLY_CLIP = "POLY_CLIP"
    P_POLY_BAR_ROADS = "POLY_BARRIER_ROADS"
    P_POLY_BAR_FIELD = "POLY_BARRIER_CLASS_FIELD"
    P_POLY_BAR_CLASSES = "POLY_BARRIER_CLASSES"
    P_POLY_BAR_EXTRA = "POLY_BARRIER_EXTRA"
    P_POLY_THIN = "POLY_THIN_EXPORT"

    P_OSM_PBF = "OSM_PBF"

    # Brownfield (existing infrastructure) — optional inputs
    P_BF_DUCTS = "BF_DUCTS"
    P_BF_CHAMBERS = "BF_CHAMBERS"
    P_BF_POLES = "BF_POLES"
    P_BF_FIBRE = "BF_FIBRE"
    P_BF_CABINETS = "BF_CABINETS"
    P_BF_TRENCHES = "BF_TRENCHES"
    P_BF_FEEDER_TRENCH = "BF_FEEDER_TRENCH"
    P_BF_DIST_TRENCH = "BF_DIST_TRENCH"
    P_BF_EXISTING_PDP = "BF_EXISTING_PDP"
    P_BF_EXISTING_MFG = "BF_EXISTING_MFG"
    P_USE_BROWNFIELD = "USE_BROWNFIELD"

    P_TR_ROADS = "TRENCH_ROADS"
    P_BUILDINGS = "BUILDINGS"
    P_TR_MFG = "TRENCH_MFG"
    P_PREMISES = "PREMISES"
    P_SPACING = "POLE_SPACING"

    OUT_OBJECTS = "OUT_OBJECTS"
    OUT_POLYGONS = "OUT_POLYGONS"
    OUT_PDP = "OUT_PDP"
    OUT_MFG = "OUT_MFG"
    OUT_MFG_AREAS = "OUT_MFG_AREAS"
    
    OUT_BROWNFIELD = "OUT_BROWNFIELD"
    OUT_BROWNFIELD_POINTS = "OUT_BROWNFIELD_POINTS"
    OUT_TRENCHES = "OUT_TRENCHES"
    OUT_FEEDER_TRENCH = "OUT_FEEDER_TRENCH"
    OUT_DIST_TRENCH = "OUT_DIST_TRENCH"
    OUT_GARDEN_TRENCH = "OUT_GARDEN_TRENCH"
    OUT_FEEDER_CABLE = "OUT_FEEDER_CABLE"
    OUT_DIST_CABLE = "OUT_DIST_CABLE"
    OUT_FEEDER_DUCTS = "OUT_FEEDER_DUCTS"
    OUT_DIST_DUCTS = "OUT_DIST_DUCTS"
    OUT_DROP_DUCTS = "OUT_DROP_DUCTS"
    OUT_COUPLEURS = "OUT_COUPLEURS"

    # HLD_attr civil layers
    P_AERIAL_ZONES = "AERIAL_ZONES"
    OUT_CHAMBERS = "OUT_CHAMBERS"
    OUT_POLES = "OUT_POLES"
    OUT_AERIAL_TRENCHES = "OUT_AERIAL_TRENCHES"
    OUT_AERIAL_CABLE = "OUT_AERIAL_CABLE"
    OUT_SERVED_PREMISES = "OUT_SERVED_PREMISES"

    _DEFAULT_OUTPUT_FILES = {
        OUT_BROWNFIELD: "Existing_Infrastructure.gpkg",
        OUT_OBJECTS: "Objects.gpkg",
        OUT_POLYGONS: "Polygons.gpkg",
        OUT_PDP: "PDPs.gpkg",
        OUT_MFG: "MFG.gpkg",
        OUT_MFG_AREAS: "MFG_Service_Areas.gpkg",
        OUT_FEEDER_TRENCH: "Feeder_Trench.gpkg",
        OUT_DIST_TRENCH: "Distribution_Trench.gpkg",
        OUT_GARDEN_TRENCH: "Garden_Trench.gpkg",
        OUT_TRENCHES: "Final_Trenches.gpkg",
        OUT_FEEDER_CABLE: "Feeder_Cable.gpkg",
        OUT_DIST_CABLE: "Distribution_Cable.gpkg",
        OUT_FEEDER_DUCTS: "Feeder_Ducts.gpkg",
        OUT_DIST_DUCTS: "Distribution_Ducts.gpkg",
        OUT_DROP_DUCTS: "Drop_Ducts.gpkg",
        OUT_COUPLEURS: "Coupleurs.gpkg",
        OUT_CHAMBERS: "Chambers.gpkg",
        OUT_POLES: "Poles.gpkg",
        # NOT "Aerial_Drop_Trenches". A trench is an excavation; these are
        # overhead spans on poles (EXCAVATION=0, CONSTRUCTION_METHOD=Overhead),
        # and the old name made the map's trench matcher pick them up as civil
        # trench — it had to test for this layer BEFORE `trenches` to exclude
        # it. "Aerial_Spans" cannot be mistaken for a dig.
        OUT_AERIAL_TRENCHES: "Aerial_Spans.gpkg",
        OUT_AERIAL_CABLE: "Aerial_Cable.gpkg",
        OUT_SERVED_PREMISES: "Served_Premises.gpkg",
    }

    _OBJ_EXCEL, _OBJ_SHEET, _OBJ_EMAIL = "EXCEL", "SHEET", "EMAIL"
    _OBJ_CRS, _OBJ_GPKG, _OBJ_THIN = "OUT_CRS", "OUT_GPKG", "THIN_EXPORT"

    _POLY_INPUT, _POLY_OUT = "INPUT", "OUT"
    _POLY_METHOD, _POLY_PLAN = "METHOD", "PLANNING_FIRST"
    _POLY_MIN, _POLY_MAX = "MIN_HH_PER_POLYGON", "MAX_HH_PER_POLYGON"
    _POLY_NEIGH, _POLY_SERVICE, _POLY_ACCESS = (
        "NEIGHBOR_DIST", "SERVICE_RADIUS", "ROAD_ACCESS_DIST",
    )
    _POLY_BUF, _POLY_SEEDBUF, _POLY_CLIP, _POLY_THIN = (
        "BUFFER", "SEEDBUF", "CLIP", "THIN_EXPORT",
    )
    _POLY_BAR_ROADS, _POLY_BAR_FIELD = "BARRIER_ROADS", "BARRIER_CLASS_FIELD"
    _POLY_BAR_CLASSES, _POLY_BAR_EXTRA = "BARRIER_MAIN_CLASSES", "BARRIER_EXTRA"

    _NET_POLY, _NET_ROADS, _NET_PBF, _NET_OBJECTS = (
        "INPUT_POLY", "INPUT_ROADS", "INPUT_OSM_PBF", "INPUT_OBJECTS",
    )
    _NET_EDGES, _NET_CAND, _NET_REMOVED, _NET_CLEAN = (
        "OUT_EDGES", "OUT_CAND", "OUT_REMOVED", "OUT_CLEAN",
    )
    _NET_ASSIGNED, _NET_MFG, _NET_FINAL_OBJECTS, _NET_MFG_AREAS = (
        "OUT_ASSIGNED", "OUT_MFG_POINT", "OUT_FINAL_OBJECTS", "OUT_MFG_AREAS",
    )

    _TR_POLY, _TR_ROADS_KEY, _TR_PDP = "INPUT_POLY", "INPUT_ROADS", "INPUT_PDP"
    _TR_HH, _TR_BLDG, _TR_MFG_KEY = "INPUT_HOUSEHOLDS", "INPUT_BUILDINGS", "INPUT_MFG"
    _TR_TAN_USED = "OUT_TANGENT_TRENCHES_USED"
    _TR_AOI_DISS = "OUT_S1_AOI_BUFFER_DISSOLVED"
    _TR_SIDE_L, _TR_SIDE_R = "OUT_SIDEWALK_LEFT", "OUT_SIDEWALK_RIGHT"
    _TR_MERGED_PDP, _TR_FEEDER_FINAL = "OUT_MERGED_PDP", "OUT_FEEDER_FINAL"
    _TR_GARDEN, _TR_FINAL, _TR_FINAL_TAN = (
        "OUT_GARDEN_TRENCHES", "OUT_FINAL_TRENCHES", "OUT_FINAL_TANGENT_TRENCHES",
    )
    _TR_DIST_LINES, _TR_DIST_DISS = "OUT_DISTRIBUTION_LINES", "OUT_DISTRIBUTION_DISS"
    # Aerial zones are an INPUT to the trench stage (the designer classifies
    # drop legs on the pole line and publishes Aerial_Drops) and the aerial
    # legs it classifies come out of the trench stage.
    _TR_AERIAL_IN = "INPUT_AERIAL_ZONES"
    _TR_AERIAL_DROPS = "OUT_AERIAL_DROPS"
    # The designer's structural nodes: published by the trench stage and
    # consumed by the chamber stage as its primary candidates.
    _TR_TRENCH_NODES = "OUT_TRENCH_NODES"
    _TR_ALL_OUTPUTS = (
        "OUT_SIDEWALK_LEFT", "OUT_SIDEWALK_RIGHT", "OUT_SIDEWALK_MERGED",
        "OUT_SIDEWALK_BUFFERED_LEFT", "OUT_SIDEWALK_BUFFERED_RIGHT",
        "OUT_PDP_TO_SIDE", "OUT_PSEUDO_PDP", "OUT_MERGED_PDP", "OUT_MFG_POINT",
        "OUT_VALID_INTERSECTIONS", "OUT_TANGENT_TRENCHES", "OUT_TANGENT_TRENCHES_USED",
        "OUT_TRENCHES_MFG_TO_PDP", "OUT_FEEDER_TRENCH", "OUT_GARDEN_TRENCHES",
        "OUT_PSEUDO_HH", "OUT_DISTRIBUTION_LINES", "OUT_DISTRIBUTION_DISS",
        "OUT_FINAL_TANGENT_TRENCHES", "OUT_FEEDER_FINAL", "OUT_FINAL_TRENCHES",
        "OUT_S1_AOI_BUFFER_DISSOLVED", "OUT_S1_AOI_OUTLINE_LINES",
        "OUT_S1_ROADS_NEAR", "OUT_S1_ROADS_FILTERED",
        "OUT_AERIAL_DROPS",
        "OUT_TRENCH_NODES",
    )

    _CB_FEEDER, _CB_GARDEN, _CB_DISTR = (
        "FEEDER_TRENCH", "GARDEN_TRENCHES", "DISTR_TRENCHES",
    )
    _CB_PROJ = "PDP_PROJECTIONS"
    _CB_FINAL_TR = "FINAL_TRENCHES"
    _CB_PDP = "PDP_POINTS"
    _CB_MFG = "MFG_POINTS"
    _CB_OUT_FEEDER, _CB_OUT_DIST = "OUT_FEEDER_CABLE", "OUT_DISTRIBUTION_CABLE"

    _DU_NETWORK, _DU_MFG, _DU_PDP, _DU_OBJECTS = (
        "NETWORK_LINES", "MFG_POINTS", "PDP_POINTS", "OBJECT_POINTS",
    )
    _DU_SIDE_L, _DU_SIDE_R, _DU_FINAL_TAN = (
        "SIDEWALK_LEFT", "SIDEWALK_RIGHT", "FINAL_TANGENT_TRENCHES",
    )
    _DU_PSEUDO, _DU_GARDEN = "PSEUDO_OBJECT_POINTS", "GARDEN_TRENCHES"
    _DU_FEEDER_CABLES, _DU_DIST_CABLES = "FEEDER_CABLES", "DIST_CABLES"
    _DU_OUT_FEEDER, _DU_OUT_DIST, _DU_OUT_DROP = (
        "OUT_FEEDER_DUCTS", "OUT_DISTRIBUTION_DUCTS", "OUT_DROP_DUCTS",
    )
    _DU_OUT_COUPLE = "OUT_COUPLEURS"
    # Per-route ducts (published feeder/distribution layers carry ONE
    # component per tier; the chamber stage counts the per-route runs).
    _DU_FEEDER_RUNS = "OUT_FEEDER_DUCT_RUNS"
    _DU_DIST_RUNS = "OUT_DISTRIBUTION_DUCT_RUNS"

    _METHOD_OPTIONS = [
        "Convex Hull (optional inset)",
        "Concave Hull (alpha shape)",
        "Voronoi Partition → Dissolve by group",
        "Seeded Growth (splitter-driven builder)",
    ]

    _ROAD_CLASS_FIELDS = ("fclass", "highway", "class")
    _PDP_ID_FIELDS = ("pdp_id", "pdp_pol_id")
    _HH_ID_FIELDS = ("addr_id", "hh_id", "address_id", "id")

    _HARDCODED_REPORT_FILES = (
        os.path.join("Drafts", "BOQ.xlsx"),
        os.path.join("Drafts", "BOM.xlsx"),
    )

    def tr(self, s):
        return QCoreApplication.translate("EndToEndPipelineAlgorithm", s)

    def createInstance(self):
        return EndToEndPipelineAlgorithm()

    def name(self):
        return "end_to_end_pipeline"

    def displayName(self):
        return self.tr("One Click – End-to-End HLD Pipeline")

    def group(self):
        return self.tr("00 One Click")

    def groupId(self):
        return "00_oneclick"

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def shortHelpString(self):
        return self.tr(
            "Runs the entire HLD Planning workflow in one step: Object → Polygon "
            "→ Network → Trench → Cable → Duct. Each stage is executed as a child "
            "Processing algorithm and its outputs are fed automatically into the "
            "next stage.\n\n"
            "Every stage's own inputs are exposed here, prefixed by stage number. "
            "Layer inputs produced by an earlier stage (premises, polygons, PDPs, "
            "MFG, trenches, sidewalks) are wired automatically and never asked "
            "for. The processing CRS standard is EPSG:25833.\n\n"
            "The Roads layer should be OSM lines WITH a class field "
            "(fclass/highway) and should include footways/paths — trench routing, "
            "sidewalk generation and cable/duct quality all depend on it.\n\n"
            "If any stage fails the pipeline stops immediately and reports which "
            "stage failed."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.P_EXCEL, self.tr("Input Excel address list (.xlsx)"), extension="xlsx"
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_ROADS,
            self.tr("Roads (OSM lines; fclass/highway field strongly recommended)"),
            [QgsProcessing.TypeVectorLine]
        ))

        # ── 00 Brownfield (Existing Infrastructure) ──────────────────
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_USE_BROWNFIELD,
            self.tr("00 Brownfield — Enable existing infrastructure reuse"),
            defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_DUCTS, self.tr("00 Brownfield — Existing Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_CHAMBERS, self.tr("00 Brownfield — Existing Chambers [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_POLES, self.tr("00 Brownfield — Existing Poles [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_FIBRE, self.tr("00 Brownfield — Existing Fibre [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_CABINETS, self.tr("00 Brownfield — Existing Cabinets [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_TRENCHES, self.tr("00 Brownfield — Existing Trenches [lines] (legacy/merged)"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_FEEDER_TRENCH, self.tr("00 Brownfield — Existing Feeder Trenches [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_DIST_TRENCH, self.tr("00 Brownfield — Existing Distribution Trenches [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_EXISTING_PDP, self.tr("00 Brownfield — Existing PDPs [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_EXISTING_MFG, self.tr("00 Brownfield — Existing MFGs [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))

        self.addParameter(QgsProcessingParameterString(
            self.P_SHEET, self.tr("01 Object — Excel sheet name (blank = first)"),
            optional=True, defaultValue=""
        ))
        self.addParameter(QgsProcessingParameterString(
            self.P_EMAIL, self.tr("01 Object — Email for Nominatim geocoder User-Agent"),
            defaultValue="you@example.com"
        ))
        self.addParameter(QgsProcessingParameterCrs(
            self.P_OUT_CRS, self.tr("01 Object — Output CRS"),
            defaultValue=QgsCoordinateReferenceSystem("EPSG:25833")
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_OBJ_THIN, self.tr("01 Object — Thin output profile (minimal fields)"),
            defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterFile(
            self.P_OUTPUT_DIR,
            self.tr("00 One Click — Output folder (optional; stores all final layers + BOQ/BOM copies)"),
            behavior=QgsProcessingParameterFile.Folder,
            optional=True,
            defaultValue=""
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.P_POLY_METHOD, self.tr("02 Polygon — Generation method"),
            options=self._METHOD_OPTIONS, defaultValue=3
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_POLY_PLAN_FIRST,
            self.tr("02 Polygon — Planning-first (force seeded-growth builder)"),
            defaultValue=False, optional=True
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_MIN_HH, self.tr("02 Polygon — Growth: minimum homes per polygon"),
            type=QgsProcessingParameterNumber.Integer, defaultValue=32, minValue=1
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_MAX_HH, self.tr("02 Polygon — Growth: maximum homes per polygon"),
            type=QgsProcessingParameterNumber.Integer, defaultValue=128, minValue=1
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_NEIGH, self.tr("02 Polygon — Growth: neighbour distance rule [m]"),
            type=QgsProcessingParameterNumber.Double, defaultValue=150.0, minValue=1.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_SERVICE,
            self.tr("02 Polygon — Growth: service radius, max building distance from FDP [m]"),
            type=QgsProcessingParameterNumber.Double, defaultValue=300.0, minValue=10.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_ACCESS,
            self.tr("02 Polygon — Growth: road-access check distance [m] (0 = off)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=100.0, minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_BUFFER, self.tr("02 Polygon — Post-buffer (+grow / -shrink) [m]"),
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_POLY_SEEDBUF,
            self.tr("02 Polygon — Growth: extra edge margin around built polygons [m]"),
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0, optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_POLY_CLIP, self.tr("02 Polygon — Optional clip layer / AOI [polygons]"),
            [QgsProcessing.TypeVectorPolygon], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_POLY_BAR_ROADS,
            self.tr("02 Polygon — Barrier-rule road layer [lines] (blank = main Roads)"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterField(
            self.P_POLY_BAR_FIELD,
            self.tr("02 Polygon — Barrier road class field (blank = fclass/highway)"),
            parentLayerParameterName=self.P_POLY_BAR_ROADS,
            type=QgsProcessingParameterField.Any, optional=True
        ))
        self.addParameter(QgsProcessingParameterString(
            self.P_POLY_BAR_CLASSES,
            self.tr("02 Polygon — Restricted road classes (comma-separated)"),
            defaultValue="motorway,trunk,primary,secondary", optional=True
        ))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.P_POLY_BAR_EXTRA,
            self.tr("02 Polygon — Extra barrier layers (railway / river / airport zone)"),
            QgsProcessing.TypeVectorAnyGeometry, optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_POLY_THIN, self.tr("02 Polygon — Thin output profile (minimal fields)"),
            defaultValue=False
        ))

        self.addParameter(QgsProcessingParameterFile(
            self.P_OSM_PBF,
            self.tr("03 Network — OSM PBF (optional alternative road source)"),
            extension="pbf", optional=True
        ))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TR_ROADS,
            self.tr("04 Trench — Roads override incl. footways [lines] (blank = main Roads)"),
            [QgsProcessing.TypeVectorLine], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BUILDINGS, self.tr("04 Trench — Buildings, trim trenches inside [polygons]"),
            [QgsProcessing.TypeVectorPolygon], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TR_MFG,
            self.tr("04 Trench — Existing MFG point override (blank = MFG from Network stage)"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_AERIAL_ZONES,
            self.tr("07 Civil — Aerial Zones [polygons] (blank = no poles planned)"),
            [QgsProcessing.TypeVectorPolygon], optional=True
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PREMISES,
            self.tr("08b Aerial — Premises with aerial_required flag [points]"),
            [QgsProcessing.TypeVectorPoint], optional=True
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_SPACING,
            self.tr("08b Aerial — Pole spacing [m]"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=50.0,
            minValue=10.0,
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_OBJECTS, self.tr("Object Layer"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_POLYGONS, self.tr("Polygon Layer"),
            QgsProcessing.TypeVectorPolygon, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_PDP, self.tr("Network - PDPs"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_MFG, self.tr("Network - MFG"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_MFG_AREAS, self.tr("Network - MFG serving-area boundaries"),
            QgsProcessing.TypeVectorPolygon, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FEEDER_TRENCH,            self.tr("Trenches - Feeder"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DIST_TRENCH,            self.tr("Trenches - Distribution"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_GARDEN_TRENCH,            self.tr("Trenches - Drop (HH->Footway)"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_TRENCHES, self.tr("Final Trenches (combined)"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FEEDER_CABLE,            self.tr("Cables - Feeder"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DIST_CABLE,            self.tr("Cables - Distribution"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FEEDER_DUCTS,            self.tr("Ducts - Feeder"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DIST_DUCTS,            self.tr("Ducts - Distribution"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DROP_DUCTS,            self.tr("Ducts - Drop (pseudo → object)"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_COUPLEURS,            self.tr("Coupleurs (pseudo → object connection points)"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_CHAMBERS,            self.tr("Civil - Chambers (planned)"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_POLES,            self.tr("Civil - Poles (aerial zones)"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_AERIAL_TRENCHES,  self.tr("Civil - Aerial Drop Trenches"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_AERIAL_CABLE,     self.tr("Cables - Aerial Drop"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_SERVED_PREMISES, self.tr("QA - Served Premises Register"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))

        self.addOutput(QgsProcessingOutputFile(
            "BOQ", self.tr("BOQ - Bill of Quantities")
        ))
        self.addOutput(QgsProcessingOutputFile(
            "BOM", self.tr("BOM - Bill of Materials")
        ))

    def _output_dir(self, parameters, context):
        out_dir = self.parameterAsFile(parameters, self.P_OUTPUT_DIR, context) or ""
        out_dir = out_dir.strip()
        if not out_dir:
            return ""
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _temp_path(self, fname):
        """Generate a unique temp GPKG file path for an output layer."""
        stem = os.path.splitext(fname)[0].replace(" ", "_")
        try:
            return QgsProcessingUtils.generateTempFilename(f"{stem}.gpkg")
        except Exception:
            import tempfile, uuid
            return os.path.join(
                tempfile.gettempdir(),
                f"HLD_{stem}_{uuid.uuid4().hex[:12]}.gpkg"
            )

    def _dest(self, parameters, key, context):
        val = parameters.get(key, None)
        is_blank = val is None or (isinstance(val, str) and not val.strip())
        is_temp = val == QgsProcessing.TEMPORARY_OUTPUT
        if isinstance(val, str):
            is_temp = is_temp or val.strip().upper() == str(QgsProcessing.TEMPORARY_OUTPUT).upper()

        if not is_blank and not is_temp:
            return val

        out_dir = self._output_dir(parameters, context)
        if out_dir:
            fname = self._DEFAULT_OUTPUT_FILES.get(key, f"{key}.gpkg")
            return os.path.join(out_dir, fname)

        # No output directory — keep layers in memory for performance.
        # Avoid writing to temp disk files which is a major bottleneck
        # for large datasets.
        return QgsProcessing.TEMPORARY_OUTPUT

    def _copy_hardcoded_reports(self, parameters, context, feedback):
        """
        BOQ/BOM generation moved to the Django backend (ftth_hld.boq).

        The engine used to copy static ``Drafts/BOQ.xlsx`` / ``Drafts/BOM.xlsx``
        templates (with hand-typed quantities) into the output folder. Those
        quantities were never recomputed, so they drifted from the design.
        The backend now computes BOQ/BOM from the persisted HLD layers and
        prices them with the rate card; the engine only ships the raw design
        layers and stays a pure planner.

        Kept as a no-op for pipeline compatibility (the call site below still
        invokes it); it no longer copies anything.
        """
        out_dir = self._output_dir(parameters, context)
        if out_dir:
            feedback.pushInfo(self.tr(
                "BOQ/BOM are generated by the backend from the HLD layers "
                "(ftth_hld.boq) — no template files copied."
            ))

    def _object_source(self, gpkg_path):
        if gpkg_path:
            return "{0}|layername={1}".format(gpkg_path, LAYERNAMES.OBJECT)
        return None

    def _split_lines_at_chambers(self, lines, chambers, context, feedback, label):
        """Split a line layer at the authoritative chamber positions.

        This is deliberately implemented with ``lineSubstring`` rather than a
        provider algorithm: QGIS installations differ on the availability and
        parameter names of the native point splitter. Source attributes are
        copied to every chamber-to-chamber span.
        """
        source = self._fast_resolve(lines, context)
        points = self._fast_resolve(chambers, context)
        if source is None or points is None or points.featureCount() == 0:
            return lines
        if source.featureCount() == 0:
            return lines

        # Index chamber points once. Testing every chamber against every line
        # (and repeating this for four trench tiers plus ducts/cables) made the
        # chamber normalization effectively O(lines × chambers × vertices),
        # which held full-area runs for many minutes after chamber placement.
        # A bounding-box query finds only candidates within the snap tolerance.
        chamber_index = QgsSpatialIndex()
        chamber_pts = {}
        for pf in points.getFeatures():
            pg = pf.geometry()
            if pg is None or pg.isEmpty():
                continue
            try:
                p = pg.asPoint()
            except Exception:
                continue
            chamber_pts[pf.id()] = QgsPointXY(p)
            chamber_index.addFeature(pf)
        if not chamber_pts:
            return lines

        out = QgsVectorLayer(
            "LineString?crs=%s" % source.crs().authid(),
            "%s_chamber_spans" % label, "memory")
        out.dataProvider().addAttributes(list(source.fields()))
        out.updateFields()
        written = 0
        for sf in source.getFeatures():
            sg = sf.geometry()
            if sg is None or sg.isEmpty():
                continue
            parts = []
            try:
                parts = sg.asMultiPolyline() if sg.isMultipart() else [sg.asPolyline()]
            except Exception:
                parts = []
            for coords in parts:
                if len(coords) < 2:
                    continue
                line = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in coords])
                cuts = []
                search_rect = QgsRectangle(line.boundingBox())
                search_rect.grow(0.75)
                for fid in chamber_index.intersects(search_rect):
                    cp = chamber_pts.get(fid)
                    if cp is None:
                        continue
                    try:
                        point_geom = QgsGeometry.fromPointXY(cp)
                        if line.distance(point_geom) <= 0.75:
                            m = float(line.lineLocatePoint(point_geom))
                            if 0.01 < m < line.length() - 0.01:
                                cuts.append(m)
                    except Exception:
                        continue
                cuts = sorted(set(round(m, 6) for m in cuts))
                bounds = [0.0] + cuts + [float(line.length())]
                for a, b in zip(bounds, bounds[1:]):
                    if b - a <= 0.02:
                        continue
                    try:
                        span = line.lineSubstring(a, b)
                    except Exception:
                        span = None
                    if span is None or span.isEmpty() or span.length() <= 0.02:
                        continue
                    nf = QgsFeature(out.fields())
                    nf.setGeometry(span)
                    nf.setAttributes(sf.attributes())
                    out.dataProvider().addFeature(nf)
                    written += 1
        out.updateExtents()
        if written == 0:
            return lines
        feedback.pushInfo(
            self.tr("Chamber span split: %s %d → %d feature(s)") %
            (label, source.featureCount(), written)
        )
        return out

    def _fast_resolve(self, val, context):
        """Fast layer existence check without loading features.
        Returns a QgsMapLayer if resolvable, None otherwise.
        Prioritises: temporaryLayerStore → mapLayerFromString → file check."""
        if not val:
            return None
        if isinstance(val, QgsMapLayer):
            return val
        if not isinstance(val, str) or not val.strip():
            return None
        # 1) Direct temporary store lookup (fastest for child-algorithm memory layers)
        try:
            store = context.temporaryLayerStore()
            if store:
                lyr = store.mapLayer(val)
                if lyr is not None:
                    return lyr
        except Exception:
            pass
        # 2) Standard context search
        try:
            return QgsProcessingUtils.mapLayerFromString(val, context)
        except Exception:
            pass
        # 3) File on disk
        if os.path.isfile(val):
            try:
                from qgis.core import QgsVectorLayer
                lyr = QgsVectorLayer(val, "", "ogr")
                if lyr.isValid():
                    return lyr
            except Exception:
                pass
        return None

    def _fast_count(self, val, context):
        """Fast feature count without loading all features.
        Returns the count or None if unavailable."""
        lyr = self._fast_resolve(val, context)
        if lyr is None:
            return None
        try:
            n = lyr.featureCount()
        except Exception:
            return None
        return n if n is not None and n >= 0 else None

    def _stage_error(self, stage, err):
        return self.tr(
            "Pipeline failed during {stage}\n\nOriginal Error:\n{err}"
        ).format(stage=stage, err=str(err))

    def _find_layer(self, val, context):
        """Find a layer by string ID / source path.
        Tries: temporary layer store → mapLayerFromString → project layers."""
        if not val or not isinstance(val, str):
            return None

        # 1) Direct ID lookup in the processing context's temporary layer store
        layer_store = context.temporaryLayerStore()
        if layer_store:
            try:
                layer = layer_store.mapLayer(val)
                if layer is not None:
                    return layer
            except Exception:
                pass

        # 2) Standard mapLayerFromString (searches by ID, name, source path)
        try:
            layer = QgsProcessingUtils.mapLayerFromString(val, context)
            if layer is not None:
                return layer
        except Exception:
            pass

        # 3) Project layers as last resort
        try:
            from qgis.core import QgsProject
            layer = QgsProject.instance().mapLayer(val)
            if layer is not None:
                return layer
        except Exception:
            pass

        return None

    def _has_field(self, layer, candidates):
        if layer is None:
            return False
        try:
            names = {f.name().lower() for f in layer.fields()}
        except Exception:
            return False
        return any(c in names for c in candidates)

    def run_brownfield_layer(self, parameters, context, feedback):
        """Stage 0: Load brownfield (existing) infrastructure into the registry."""
        params = {
            "INPUT_DUCTS": self.parameterAsVectorLayer(parameters, self.P_BF_DUCTS, context),
            "INPUT_CHAMBERS": self.parameterAsVectorLayer(parameters, self.P_BF_CHAMBERS, context),
            "INPUT_POLES": self.parameterAsVectorLayer(parameters, self.P_BF_POLES, context),
            "INPUT_FIBRE": self.parameterAsVectorLayer(parameters, self.P_BF_FIBRE, context),
            "INPUT_CABINETS": self.parameterAsVectorLayer(parameters, self.P_BF_CABINETS, context),
            "INPUT_TRENCHES": self.parameterAsVectorLayer(parameters, self.P_BF_TRENCHES, context),
            "INPUT_FEEDER_TRENCH": self.parameterAsVectorLayer(parameters, self.P_BF_FEEDER_TRENCH, context),
            "INPUT_DIST_TRENCH": self.parameterAsVectorLayer(parameters, self.P_BF_DIST_TRENCH, context),
            "INPUT_EXISTING_PDP": self.parameterAsVectorLayer(parameters, self.P_BF_EXISTING_PDP, context),
            "INPUT_EXISTING_MFG": self.parameterAsVectorLayer(parameters, self.P_BF_EXISTING_MFG, context),
            "OUT_EXISTING_INFRA": QgsProcessing.TEMPORARY_OUTPUT,
            # Points (chambers, PDPs, MFG) go to their own sink so point assets
            # are exported too — otherwise they are silently dropped.
            "OUT_EXISTING_POINTS": QgsProcessing.TEMPORARY_OUTPUT,
        }
        # Only run if at least one brownfield layer provided
        has_any = any(v is not None for k, v in params.items() if k.startswith("INPUT_"))
        if not has_any:
            return {"OUT_EXISTING_INFRA": None, "OUT_EXISTING_POINTS": None}
        return processing.run(ALG.BROWNFIELD, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_object_layer(self, parameters, context, feedback):
        params = {
            self._OBJ_EXCEL: self.parameterAsFile(parameters, self.P_EXCEL, context),
            self._OBJ_SHEET: self.parameterAsString(parameters, self.P_SHEET, context) or "",
            self._OBJ_EMAIL: self.parameterAsString(parameters, self.P_EMAIL, context)
                             or "you@example.com",
            self._OBJ_CRS: self.parameterAsCrs(parameters, self.P_OUT_CRS, context),
            self._OBJ_GPKG: QgsProcessing.TEMPORARY_OUTPUT,
            self._OBJ_THIN: self.parameterAsBoolean(parameters, self.P_OBJ_THIN, context),
        }
        return processing.run(ALG.OBJECT, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_polygon_layer(self, parameters, results, context, feedback):
        clip = self.parameterAsVectorLayer(parameters, self.P_POLY_CLIP, context)
        barrier_roads = self.parameterAsVectorLayer(parameters, self.P_POLY_BAR_ROADS, context)
        if barrier_roads is None:
            barrier_roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        barrier_field = self.parameterAsFields(parameters, self.P_POLY_BAR_FIELD, context)
        try:
            barrier_extra = self.parameterAsLayerList(parameters, self.P_POLY_BAR_EXTRA, context)
        except Exception:
            barrier_extra = None
        params = {
            self._POLY_INPUT: results["objects"],
            self._POLY_METHOD: self.parameterAsEnum(parameters, self.P_POLY_METHOD, context),
            self._POLY_PLAN: self.parameterAsBoolean(parameters, self.P_POLY_PLAN_FIRST, context),
            self._POLY_MIN: self.parameterAsInt(parameters, self.P_POLY_MIN_HH, context),
            self._POLY_MAX: self.parameterAsInt(parameters, self.P_POLY_MAX_HH, context),
            self._POLY_NEIGH: self.parameterAsDouble(parameters, self.P_POLY_NEIGH, context),
            self._POLY_SERVICE: self.parameterAsDouble(parameters, self.P_POLY_SERVICE, context),
            self._POLY_ACCESS: self.parameterAsDouble(parameters, self.P_POLY_ACCESS, context),
            self._POLY_BUF: self.parameterAsDouble(parameters, self.P_POLY_BUFFER, context),
            self._POLY_SEEDBUF: self.parameterAsDouble(parameters, self.P_POLY_SEEDBUF, context),
            self._POLY_BAR_CLASSES: self.parameterAsString(
                parameters, self.P_POLY_BAR_CLASSES, context
            ) or "motorway,trunk,primary,secondary",
            self._POLY_THIN: self.parameterAsBoolean(parameters, self.P_POLY_THIN, context),
            self._POLY_OUT: self._dest(parameters, self.OUT_POLYGONS, context),
        }
        if clip is not None:
            params[self._POLY_CLIP] = clip
        if barrier_roads is not None:
            params[self._POLY_BAR_ROADS] = barrier_roads
        if barrier_field:
            params[self._POLY_BAR_FIELD] = barrier_field[0]
        if barrier_extra:
            params[self._POLY_BAR_EXTRA] = barrier_extra
        return processing.run(ALG.POLYGON, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_network_layer(self, parameters, results, context, feedback):
        roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        pbf = self.parameterAsFile(parameters, self.P_OSM_PBF, context)
        mfg_override = self.parameterAsVectorLayer(parameters, self.P_TR_MFG, context)
        params = {
            self._NET_POLY: results["polygons"],
            self._NET_OBJECTS: results["objects"],
            self._NET_EDGES: QgsProcessing.TEMPORARY_OUTPUT,
            self._NET_CAND: QgsProcessing.TEMPORARY_OUTPUT,
            self._NET_REMOVED: QgsProcessing.TEMPORARY_OUTPUT,
            self._NET_CLEAN: QgsProcessing.TEMPORARY_OUTPUT,
            self._NET_ASSIGNED: self._dest(parameters, self.OUT_PDP, context),
            self._NET_MFG: (QgsProcessing.TEMPORARY_OUTPUT if mfg_override is not None
                            else self._dest(parameters, self.OUT_MFG, context)),
            self._NET_MFG_AREAS: self._dest(parameters, self.OUT_MFG_AREAS, context),
            self._NET_FINAL_OBJECTS: self._dest(parameters, self.OUT_OBJECTS, context),
        }
        if roads is not None:
            params[self._NET_ROADS] = roads
        if pbf:
            params[self._NET_PBF] = pbf
        return processing.run(ALG.NETWORK, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    # ------------------------------------------------------------------
    # Trench engine selection
    #
    # Production uses one trench implementation: the civil designer. The
    # legacy sidewalk/graph algorithm remains callable directly for comparison,
    # but the end-to-end pipeline cannot select that known-failing path. See
    # utils.params.TRENCH_ENGINE for the fixed resolver.
    # ------------------------------------------------------------------
    TRENCH_ENGINE_ENV = TRENCH_ENGINE.ENV
    TRENCH_ENGINES = TRENCH_ENGINE.ENGINES
    TRENCH_ENGINE_DEFAULT = TRENCH_ENGINE.DEFAULT

    @classmethod
    def _trench_engine(cls) -> str:
        return TRENCH_ENGINE.resolve()[0]

    @staticmethod
    def _restricted_zone_classes(zones):
        """The distinct restricted ``fclass`` values in a zone layer.

        Returns ``None`` when the layer has no ``fclass`` field at all — the
        signal that it is a hand-supplied "do not dig here" mask, which is
        honoured in full rather than second-guessed. Same rule as the designer's
        (``design/aerial_feasibility.RESTRICTED_LANDUSE``), so the count reported
        here is the count used there.
        """
        from ..design.aerial_feasibility import is_restricted_landuse
        try:
            idx = zones.fields().indexFromName("fclass")
        except Exception:
            return None
        if idx < 0:
            return None
        found = set()
        for feat in zones.getFeatures():
            value = feat[idx]
            if is_restricted_landuse(value):
                found.add(str(value).strip().lower())
        return found

    def run_trench_layer(self, parameters, results, context, feedback):
        roads = self.parameterAsVectorLayer(parameters, self.P_TR_ROADS, context)
        if roads is None:
            roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        buildings = self.parameterAsVectorLayer(parameters, self.P_BUILDINGS, context)
        params = {k: QgsProcessing.TEMPORARY_OUTPUT for k in self._TR_ALL_OUTPUTS}
        params[self._TR_POLY] = results["polygons"]
        params[self._TR_ROADS_KEY] = roads
        params[self._TR_PDP] = results["pdp"]
        params[self._TR_FINAL] = self._dest(parameters, self.OUT_TRENCHES, context)
        if results.get("objects"):
            params[self._TR_HH] = results["objects"]
        if results.get("mfg") is not None:
            params[self._TR_MFG_KEY] = results["mfg"]
        if buildings is not None:
            params[self._TR_BLDG] = buildings
        # Opt-in, exactly like the pole/aerial stages: without zones the trench
        # stage behaves as before (every drop leg is trenched). With zones the
        # designer classifies the legs that cannot be dug and publishes them as
        # Aerial_Drops, so the map, BOQ and LLD can see them as non-excavation.
        zones = self.parameterAsVectorLayer(parameters, self.P_AERIAL_ZONES, context)
        if zones is not None and zones.isValid() and zones.featureCount() > 0:
            # An area run hands us the RAW OSM landuse layer, which is mostly the
            # ground the network exists to serve (`residential`, `retail`,
            # `commercial`). Counting all of it as "aerial zones" both lies in
            # this line and — before the designer learned to filter — blanketed
            # the AOI. Report the classes that are actually restricted so the
            # number a reader trusts is the number the designer will use.
            restricted = self._restricted_zone_classes(zones)
            total = zones.featureCount()
            if restricted is None:
                # No fclass field: a hand-drawn "no dig" mask. Honoured as-is.
                feedback.pushInfo(self.tr(
                    "Trench stage: aerial zones supplied ({0} polygon(s), no "
                    "'fclass' field — taken as given) — drop legs that cannot "
                    "be dug are classified Aerial.").format(total))
            else:
                feedback.pushInfo(self.tr(
                    "Trench stage: aerial zones supplied ({0} polygon(s)) — "
                    "{1} restricted ({2}), {3} non-restricted discarded. Drop "
                    "legs that cannot be dug are classified Aerial.").format(
                        total, len(restricted),
                        ", ".join(sorted(restricted)) or "none",
                        total - len(restricted)))
            params[self._TR_AERIAL_IN] = zones
        engine, invalid = TRENCH_ENGINE.resolve()
        if invalid:
            feedback.pushWarning(self.tr(
                "{0}={1} is not a trench engine ({2}) — using {3}").format(
                    TRENCH_ENGINE.ENV, invalid,
                    "/".join(TRENCH_ENGINE.ENGINES), engine))
        alg_id = TRENCH_ENGINE.algorithm_id(engine)
        feedback.pushInfo(self.tr("Trench engine: {0}").format(
            "designer (civil trench designer)" if engine == TRENCH_ENGINE.DESIGN
            else "legacy (sidewalk/graph)"))
        return processing.run(alg_id, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_cable_layer(self, parameters, results, context, feedback):
        params = {
            self._CB_FEEDER: results.get("feeder"),
            self._CB_GARDEN: results.get("garden"),
            self._CB_DISTR: results.get("distribution"),
            self._CB_OUT_FEEDER: self._dest(parameters, self.OUT_FEEDER_CABLE, context),
            self._CB_OUT_DIST: self._dest(parameters, self.OUT_DIST_CABLE, context),
        }
        if results.get("pdp_proj"):
            params[self._CB_PROJ] = results["pdp_proj"]
        # Shared feeder planning inputs (Final_Trenches + PDPs + MFG).
        # When all three are present the feeder cable is PLANNED (clubbed,
        # sized by splitter demand); otherwise the legacy trench-copy runs.
        if results.get("trenches"):
            params[self._CB_FINAL_TR] = results["trenches"]
        if results.get("pdp"):
            params[self._CB_PDP] = results["pdp"]
        if results.get("mfg"):
            params[self._CB_MFG] = results["mfg"]
        # State the trench input in the log: the cable layer is planned ON the
        # designed trench route tree, and "which trench did this run use" was
        # otherwise only inferable from the numbers that came out.
        if self._CB_FINAL_TR in params:
            feedback.pushInfo(self.tr(
                "Cable stage: trench input = {0} feature(s) "
                "(FINAL_TRENCHES — the cable is planned on this route tree)."
            ).format(self._fast_count(params[self._CB_FINAL_TR], context)))
        else:
            feedback.pushInfo(self.tr(
                "Cable stage: NO trench input — the legacy trench-copy path is "
                "in use."))
        return processing.run(ALG.CABLE, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_duct_layer(self, parameters, results, context, feedback):
        params = {
            self._DU_NETWORK: results.get("trenches"),
            self._DU_MFG: results.get("mfg"),
            self._DU_PDP: results.get("pdp"),
            self._DU_OBJECTS: results.get("objects"),
            self._DU_OUT_FEEDER: self._dest(parameters, self.OUT_FEEDER_DUCTS, context),
            self._DU_OUT_DIST: self._dest(parameters, self.OUT_DIST_DUCTS, context),
            self._DU_OUT_DROP: self._dest(parameters, self.OUT_DROP_DUCTS, context),
            self._DU_OUT_COUPLE: self._dest(parameters, self.OUT_COUPLEURS, context),
        }
        # State the trench input in the log: every duct is built on this line
        # network (and, when the designer's corridor produced them, on the
        # offsets derived from it).
        if params.get(self._DU_NETWORK):
            feedback.pushInfo(self.tr(
                "Duct stage: trench input = {0} feature(s) "
                "(NETWORK_LINES — feeder/distribution/drop ducts are built on "
                "this network)."
            ).format(self._fast_count(params[self._DU_NETWORK], context)))
        else:
            feedback.pushInfo(self.tr(
                "Duct stage: NO trench input — duct placement will fall back to "
                "the sidewalk offsets only."))
        if results.get("sidewalk_l") and results.get("sidewalk_r"):
            feedback.pushInfo(self.tr(
                "Duct stage: corridor offsets present (both sides) — ducts are "
                "assigned their real side of the trench."))
        else:
            feedback.pushInfo(self.tr(
                "Duct stage: corridor offsets ABSENT — side labels are default."))
        if results.get("pseudo_hh"):
            params[self._DU_PSEUDO] = results["pseudo_hh"]
        if results.get("garden"):
            params[self._DU_GARDEN] = results["garden"]
        if results.get("sidewalk_l"):
            params[self._DU_SIDE_L] = results["sidewalk_l"]
        if results.get("sidewalk_r"):
            params[self._DU_SIDE_R] = results["sidewalk_r"]
        # Route-based duct bundling inputs: when the cable layers exist, the
        # duct stage emits ONE duct per connected route carrying the cables
        # on it (4-way Feeder / 2-way Distribution).
        cables = results.get("cables") or {}
        fc = cables.get(self._CB_OUT_FEEDER)
        if fc:
            params[self._DU_FEEDER_CABLES] = fc
        dc = cables.get(self._CB_OUT_DIST)
        if dc:
            params[self._DU_DIST_CABLES] = dc
        if not fc and not dc:
            # Cascade order: cables are planned AFTER the ducts now, so the
            # route-based bundling (one duct per cable route) has no input —
            # the duct stage falls back to its own bundling on the trench
            # network, which is the behaviour the plan approved for the
            # reordered cascade.
            feedback.pushInfo(self.tr(
                "Duct stage: no cable inputs (cables run after ducts in the "
                "cascade) — ducts are bundled on the trench network."))
        # Per-route duct layers for the chamber stage (see duct_layer):
        # junction chambers are placed where >= 3 DISTINCT ducts meet, so that
        # stage must see the runs rather than the published single component.
        out_dir = self._output_dir(parameters, context)
        if out_dir:
            params[self._DU_FEEDER_RUNS] = os.path.join(
                out_dir, "Feeder_Ducts_Runs.gpkg")
            params[self._DU_DIST_RUNS] = os.path.join(
                out_dir, "Distribution_Ducts_Runs.gpkg")
        return processing.run(ALG.DUCT, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_chamber_layer(self, parameters, results, context, feedback):
        """Stage 7: plan civil chambers from duct junctions + PDPs."""
        ducts = results.get("ducts") or {}
        # Prefer the per-route duct layers: the published feeder/distribution
        # layers are ONE component per tier, and the junction rule (>= 3
        # distinct ducts) plus the distribution-endpoint rule both need the
        # individual runs.  Falls back to the published layers when the runs
        # were not produced (no cable-driven duct build).
        runs = results.get("duct_runs") or {}
        # Legacy distribution mode does not emit per-route run layers. Resolve
        # the run path first and only use it when it is a valid layer; otherwise
        # pass the published duct layer to the chamber stage.
        feeder_runs = self._fast_resolve(runs.get(self._DU_FEEDER_RUNS), context)
        dist_runs = self._fast_resolve(runs.get(self._DU_DIST_RUNS), context)
        feeder_ducts_in = feeder_runs or ducts.get(self._DU_OUT_FEEDER)
        dist_ducts_in = dist_runs or ducts.get(self._DU_OUT_DIST)
        if feeder_ducts_in is None and dist_ducts_in is None:
            # Cascade order: chambers are placed straight after the trench
            # design, so the duct evidence rules (junctions, drop transitions)
            # cannot fire — the designer's Trench_Nodes + PDPs + trench
            # junctions are the candidate sources instead.
            feedback.pushInfo(self.tr(
                "Chamber stage: no duct inputs (ducts run after chambers in "
                "the cascade) — candidates come from Trench_Nodes, PDPs, "
                "tangent crossings and trench junctions."))
        # Resolve the trench stage's temporary tangent-crossing layer to a
        # concrete layer object (or None).  Passing an unresolved temp-id
        # string into a child algorithm has caused native crashes in headless
        # runs, so we never hand the raw id downstream.
        tangents = self._fast_resolve(results.get("tangents_used"), context)
        params = {
            "INPUT_FEEDER_DUCTS": feeder_ducts_in,
            "INPUT_DIST_DUCTS": dist_ducts_in,
            "INPUT_DROP_DUCTS": ducts.get(self._DU_OUT_DROP),
            "INPUT_PDP": results.get("pdp"),
            "INPUT_TANGENT_CROSSINGS": tangents,
            "INPUT_TRENCHES": results.get("trenches"),
            # The designer's structural nodes (HDD pits / junctions / PDPs /
            # bends / pulls): the primary candidate source when present, so
            # chambers land on the points where the network actually changes
            # tier or method instead of on duct-junction guesses.
            "INPUT_TRENCH_NODES": self._fast_resolve(
                results.get("trench_nodes"), context),
            "INPUT_AOI": self._fast_resolve(results.get("aoi"), context),
            "INPUT_BUILDINGS": self.parameterAsVectorLayer(parameters, self.P_BUILDINGS, context),
            "OUT_CHAMBERS": self._dest(parameters, self.OUT_CHAMBERS, context),
        }
        return processing.run(ALG.CHAMBER, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_pole_layer(self, parameters, results, context, feedback):
        """Stage 8: plan aerial poles inside user-supplied aerial zones.
        Opt-in: without valid aerial zones there is nothing to plan."""
        zones = self.parameterAsVectorLayer(parameters, self.P_AERIAL_ZONES, context)
        if zones is None or not zones.isValid() or zones.featureCount() == 0:
            return None
        params = {
            "INPUT_GARDEN_TRENCHES": results.get("garden"),
            "INPUT_AERIAL_ZONES": zones,
            "INPUT_FEEDER_TRENCHES": results.get("feeder"),
            "INPUT_PDP": results.get("pdp"),
            # The legs the trench stage just classified as aerial. Without
            # them a leg carries no pole, and the aerial drop stage below can
            # build nothing from it.
            "INPUT_AERIAL_LEGS": self._fast_resolve(
                results.get("aerial_drops"), context),
            "OUT_POLES": self._dest(parameters, self.OUT_POLES, context),
        }
        return processing.run(ALG.POLE, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def _classified_aerial_premises(self, results, context, feedback):
        """Premises the trench stage classified as aerial → stage 08b input.

        Stage 08b only builds drops for premises flagged ``aerial_required``.
        That flag normally comes from the field (the survey app) or from a
        planner flagging a long drop by hand — but the trench stage ALSO knows:
        it just classified the legs it could not dig and named their addresses
        on ``Aerial_Drops``. Turning that classification into the flag is what
        keeps one pipeline run self-consistent (classified aerial → built
        aerial) instead of needing a second, manual pass.

        Returns a memory layer of the flagged points, or None when there is
        nothing to flag.
        """
        drops, objects = results.get("aerial_drops"), results.get("objects")
        if not drops or not objects:
            return None
        try:
            dl = QgsVectorLayer(str(drops), "aerial_drops", "ogr")
            if not dl.isValid():
                return None
            addrs = set()
            for f in dl.getFeatures():
                for name in ("addr_id", "ADDR_ID"):
                    if name in dl.fields().names():
                        v = f[name]
                        if v not in (None, ""):
                            addrs.add(str(v).strip())
                        break
            if not addrs:
                return None
            src = QgsVectorLayer(str(objects), "objects", "ogr")
            if not src.isValid():
                return None
        except Exception:
            return None
        names = src.fields().names()
        f_addr = next((n for n in ("ADDR_ID", "addr_id", "obj_id", "OBJECT_ID")
                       if n in names), None)
        if f_addr is None:
            return None
        mem = QgsVectorLayer(
            "Point?crs=%s&field=aerial_required:integer&field=addr_id:string"
            % src.crs().authid(), "premises_aerial", "memory")
        mem.startEditing()
        n = 0
        for f in src.getFeatures():
            v = f[f_addr]
            if v is None or str(v).strip() not in addrs:
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
            nf = QgsFeature(mem.fields())
            nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt.x(), pt.y())))
            nf["aerial_required"] = 1
            nf["addr_id"] = str(v).strip()
            mem.addFeature(nf)
            n += 1
        mem.commitChanges()
        if not n:
            return None
        feedback.pushInfo(self.tr(
            "Aerial Drop Layer: {0} premise(s) flagged from the trench "
            "stage's aerial classification.").format(n))
        return mem

    def run_aerial_drop_layer(self, parameters, results, context, feedback):
        """Stage 8b: route aerial drop trenches from poles to flagged premises.
        Opt-in: without aerial zones (or poles) there is nothing to route."""
        premises = self.parameterAsVectorLayer(parameters, self.P_PREMISES, context)
        if premises is None or not premises.isValid() or premises.featureCount() == 0:
            # No explicit premises input: fall back to the legs the trench
            # stage just classified, so the pipeline builds what it classified.
            premises = self._classified_aerial_premises(results, context, feedback)
        if premises is None or not premises.isValid() or premises.featureCount() == 0:
            feedback.pushInfo(self.tr(
                "Aerial Drop Layer: no premise flagged aerial_required — "
                "nothing to build."))
            return None
        poles = results.get("poles")
        bf_poles = self.parameterAsVectorLayer(parameters, self.P_BF_POLES, context)
        zones = self.parameterAsVectorLayer(parameters, self.P_AERIAL_ZONES, context)
        roads = self.parameterAsVectorLayer(parameters, self.P_TR_ROADS, context)
        # The legs the trench stage classified as aerial are the authority, not
        # the zone polygons: a leg can be aerial by the chain/length rule with no
        # zone over it at all.  Bailing out on "no zones" was the second half of
        # why a run could classify legs and still publish 0 aerial drops.
        legs = self._fast_resolve(results.get("aerial_drops"), context)
        has_zones = zones is not None and zones.isValid() and zones.featureCount() > 0
        has_legs = legs is not None and legs.isValid() and legs.featureCount() > 0
        if not has_zones and not has_legs:
            return None
        params = {
            "INPUT_PREMISES": premises,
            "INPUT_POLES": poles,
            "INPUT_AERIAL_ZONES": zones if has_zones else None,
            "INPUT_AERIAL_LEGS": legs,
            "INPUT_ROADS": roads,
            "INPUT_BF_POLES": bf_poles,
            "POLE_SPACING_M": self.parameterAsDouble(parameters, self.P_SPACING, context),
            "OUT_AERIAL_TRENCH": self._dest(parameters, self.OUT_AERIAL_TRENCHES, context),
            "OUT_AERIAL_CABLE": self._dest(parameters, self.OUT_AERIAL_CABLE, context),
        }
        return processing.run(ALG.AERIAL, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def _preflight_cable(self, results, context, feedback):
        feeder_val = results.get("feeder")
        garden_val = results.get("garden")
        dist_val = results.get("distribution")

        # Fast existence check — just verify the value is present and resolvable
        missing = []
        for name, val in (("Feeder trenches", feeder_val),
                          ("Garden trenches", garden_val),
                          ("Distribution trenches", dist_val)):
            if not val or not isinstance(val, str) or not val.strip():
                missing.append(name)
            elif not os.path.isfile(val):
                # Not a file — try quick text-based existence (key in results = child produced it)
                pass
        if missing:
            raise PipelineStageError(self._stage_error(
                "Cable Layer",
                self.tr(
                    "The Trench stage did not produce: {m}. "
                    "Cables cannot be built without them. Check the Trench stage "
                    "log above for routing errors."
                ).format(m=", ".join(missing))
            ))

        n_f = self._fast_count(feeder_val, context)
        n_g = self._fast_count(garden_val, context)
        n_d = self._fast_count(dist_val, context)

        zero = lambda x: x is None or x == 0
        if zero(n_f) and zero(n_g) and zero(n_d):
            raise PipelineStageError(self._stage_error(
                "Cable Layer",
                self.tr(
                    "The Trench stage produced 0 feeder, 0 garden and 0 "
                    "distribution trenches, so there is nothing to build cables "
                    "from.\n\nMost likely causes:\n"
                    "- The Roads layer has no class field (fclass/highway), so "
                    "footways/vehicular roads could not be told apart and the "
                    "routing graph degraded.\n"
                    "- The MFG point could not be snapped onto the sidewalk "
                    "graph (check the Trench log for 'MFG did not snap').\n\n"
                    "Fix: supply OSM roads WITH an fclass/highway field that "
                    "include footway/path/service lines (use the '04 Trench — "
                    "Roads override' input if your main Roads layer is "
                    "vehicular-only), then re-run."
                )
            ))

        problems = []
        if not self._has_field(self._fast_resolve(garden_val, context),
                               self._PDP_ID_FIELDS):
            problems.append(self.tr(
                "Garden trenches carry no PDP_ID field (objects reached the "
                "Trench stage without PDP assignment)"
            ))
        if not self._has_field(self._fast_resolve(dist_val, context),
                               self._PDP_ID_FIELDS):
            problems.append(self.tr("Distribution trenches carry no PDP_ID field"))
        if problems:
            raise PipelineStageError(self._stage_error(
                "Cable Layer", "; ".join(problems) + "."
            ))

        if n_f is not None and n_f == 0:
            feedback.pushWarning(self.tr(
                "0 feeder trenches were routed (MFG snap or graph connectivity "
                "failed) — the Feeder Cable output will be empty. Check the "
                "Trench log and the Roads layer classification."
            ))
        fb = lambda x: str(x) if x is not None else "unknown"
        feedback.pushInfo(self.tr(
            "Cable pre-flight — feeder: {f}, garden: {g}, distribution: {d} features."
        ).format(f=fb(n_f), g=fb(n_g), d=fb(n_d)))

    def _preflight_duct(self, results, context, feedback):
        trenches_val = results.get("trenches")
        n_t = self._fast_count(trenches_val, context)
        if n_t is not None:
            feedback.pushInfo(self.tr(
                "Duct pre-flight — final trenches: %s features."
            ) % n_t)
        else:
            feedback.pushInfo(self.tr(
                "Duct pre-flight — final trenches: (could not count)."
            ))
        if n_t is None or n_t == 0:
            raise PipelineStageError(self._stage_error(
                "Duct Layer",
                self.tr(
                    "Final Trenches are empty — ducts need the trench network "
                    "lines to route on. Check the Trench stage log."
                )
            ))
        if results.get("mfg") is None or results.get("pdp") is None:
            raise PipelineStageError(self._stage_error(
                "Duct Layer",
                self.tr("MFG and PDP layers are required but missing.")
            ))
        if results.get("pseudo_hh") is None:
            feedback.pushWarning(self.tr(
                "No pseudo object points available — distribution ducts will "
                "route to the objects instead of stopping at the footway."
            ))
        objects_lyr = self._fast_resolve(results.get("objects"), context)
        problems = []
        if not self._has_field(objects_lyr, self._PDP_ID_FIELDS):
            problems.append(self.tr(
                "the Objects layer carries no PDP_ID field, so distribution "
                "ducts cannot match households to PDPs"
            ))
        if not self._has_field(objects_lyr, self._HH_ID_FIELDS):
            problems.append(self.tr(
                "the Objects layer carries no household id field "
                "(ADDR_ID/HH_ID/id)"
            ))
        if problems:
            raise PipelineStageError(self._stage_error(
                "Duct Layer", "; ".join(problems) + "."
            ))

    def _save_layer_to_gpkg(self, val, fname, out_dir, context, feedback):
        """
        Save a layer (string ID, file path, or QgsMapLayer object) to GPKG.
        Only writes to disk when an explicit output directory is requested.
        When no output directory is provided, returns the value as-is to avoid
        expensive disk I/O — layers stay in memory for downstream consumers.
        Returns the output file path on success, or the original value on failure.
        """
        t0 = time.time()
        if not val or not out_dir:
            feedback.pushInfo(f"  [timing] {fname}: skipped (no output dir) in {time.time() - t0:.3f}s")
            return val

        dst = os.path.join(out_dir, fname)

        # Already a file on disk — copy it to the output directory
        if isinstance(val, str) and os.path.isfile(val):
            if val == dst:
                feedback.pushInfo(f"  [timing] {fname}: already at destination in {time.time() - t0:.3f}s")
                return val
            try:
                shutil.copy2(val, dst)
                feedback.pushInfo(f"  [timing] {fname}: copied to disk in {time.time() - t0:.3f}s")
                return dst
            except Exception:
                feedback.pushInfo(f"  [timing] {fname}: copy failed in {time.time() - t0:.3f}s")
                return val

        # Resolve the layer: from string ID or use the object directly
        layer = None
        if isinstance(val, str):
            layer = self._find_layer(val, context)
        elif isinstance(val, QgsMapLayer):
            layer = val
        if layer is None:
            feedback.pushInfo(f"  [timing] {fname}: layer not found in {time.time() - t0:.3f}s")
            return val

        try:
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = fname.replace(".gpkg", "")
            err = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, dst, context.transformContext(), opts
            )
            elapsed = time.time() - t0
            if err[0] == QgsVectorFileWriter.NoError:
                feedback.pushInfo(f"  [timing] {fname}: written to {dst} in {elapsed:.3f}s")
                return dst
            else:
                feedback.pushInfo(f"  [timing] {fname}: write failed (err {err[0]}) in {elapsed:.3f}s")
        except Exception:
            feedback.pushInfo(f"  [timing] {fname}: write exception in {time.time() - t0:.3f}s")
        return val

    def _run_stage_brownfield(self, parameters, context, steps, feedback, results, out_dir):
        """Stage 0: Load brownfield (existing) infrastructure.

        Override in brownfield subclass to always execute when inputs are
        provided, regardless of the P_USE_BROWNFIELD toggle.
        """
        if feedback.isCanceled():
            return
        steps.setCurrentStep(0)
        feedback.pushInfo(self.tr("[0%] Loading Brownfield (Existing) Infrastructure"))
        t0 = time.time()
        use_bf = self.parameterAsBoolean(parameters, self.P_USE_BROWNFIELD, context)
        if use_bf:
            bf_result = self._run("Brownfield", self.run_brownfield_layer,
                                  parameters, context, steps, feedback)
            results["brownfield_output"] = bf_result.get("OUT_EXISTING_INFRA") if bf_result else None
            results["brownfield_points"] = bf_result.get("OUT_EXISTING_POINTS") if bf_result else None
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
                    f"  [timing] Brownfield: {elapsed:.3f}s ({', '.join(parts)})"))
            else:
                feedback.pushInfo(self.tr(
                    f"  [timing] Brownfield: skipped (no assets provided) in {elapsed:.3f}s"))
        else:
            results["brownfield_output"] = None
            results["brownfield_points"] = None
            feedback.pushInfo(self.tr(
                f"  [timing] Brownfield: disabled (toggle off) in {time.time() - t0:.3f}s"))

        # Save brownfield layers to output directory (if requested)
        if results.get("brownfield_output"):
            results["brownfield_output"] = self._save_layer_to_gpkg(
                results["brownfield_output"], "Existing_Infrastructure.gpkg",
                out_dir, context, feedback)
        if results.get("brownfield_points"):
            results["brownfield_points"] = self._save_layer_to_gpkg(
                results["brownfield_points"], "Existing_Infrastructure_Points.gpkg",
                out_dir, context, feedback)

    def execute_pipeline(self, parameters, context, feedback):
        t_pipeline = time.time()
        results = {}
        steps = QgsProcessingMultiStepFeedback(9, feedback)
        out_dir = self._output_dir(parameters, context)
        feedback.pushInfo(self.tr("[timing] Pipeline started (output_dir=%s)" % (out_dir or "memory")))

        # --- Stage 0: Brownfield (Existing Infrastructure) ---
        self._run_stage_brownfield(parameters, context, steps, feedback, results, out_dir)

        if feedback.isCanceled():
            return {}
        # --- Object Layer ---
        steps.setCurrentStep(1)
        feedback.pushInfo(self.tr("[5%] Running Object Layer"))
        t0 = time.time()
        obj = self._run("Object Layer", self.run_object_layer,
                        parameters, context, steps, feedback)
        results["objects_gpkg"] = obj.get(self._OBJ_GPKG, "")
        results["objects"] = self._object_source(results["objects_gpkg"])
        elapsed = time.time() - t0
        n_obj = self._fast_count(results["objects"], context)
        fc_str = "{} features, ".format(n_obj) if n_obj is not None else ""
        feedback.pushInfo(self.tr("  [timing] Object Layer: {}{:.3f}s".format(fc_str, elapsed)))
        if not results["objects"]:
            raise QgsProcessingException(self.tr(
                "Object Layer produced no GeoPackage; cannot continue."
            ))

        if feedback.isCanceled():
            return {}
        # --- Polygon Layer ---
        steps.setCurrentStep(2)
        feedback.pushInfo(self.tr("[25%] Running Polygon Layer"))
        t0 = time.time()
        poly = self._run("Polygon Layer", self.run_polygon_layer,
                         parameters, context, steps, feedback, results=results)
        results["polygons"] = poly.get(self._POLY_OUT)
        elapsed = time.time() - t0
        n_poly = self._fast_count(results["polygons"], context)
        fc_str = "{} features, ".format(n_poly) if n_poly is not None else ""
        feedback.pushInfo(self.tr("  [timing] Polygon Layer: {}{:.3f}s".format(fc_str, elapsed)))
        t_save = time.time()
        results["polygons"] = self._save_layer_to_gpkg(
            results["polygons"], "Polygons.gpkg", out_dir, context, feedback)
        feedback.pushInfo(self.tr("  [timing] Polygon output save: {:.3f}s".format(time.time() - t_save)))

        if feedback.isCanceled():
            return {}
        # --- Network Layer ---
        steps.setCurrentStep(3)
        feedback.pushInfo(self.tr("[40%] Running Network Layer"))
        t0 = time.time()
        net = self._run("Network Layer", self.run_network_layer,
                        parameters, context, steps, feedback, results=results)
        results["network"] = net.get(self._NET_EDGES)
        results["pdp"] = net.get(self._NET_ASSIGNED)
        results["mfg"] = net.get(self._NET_MFG)
        results["mfg_service_areas"] = net.get(self._NET_MFG_AREAS)
        elapsed = time.time() - t0
        n_pdp = self._fast_count(results["pdp"], context)
        n_fobj = self._fast_count(net.get(self._NET_FINAL_OBJECTS), context)
        parts = []
        if n_pdp is not None:
            parts.append("PDPs: {}".format(n_pdp))
        if n_fobj is not None:
            parts.append("Objects: {}".format(n_fobj))
        fc_str = ("{} features, ".format(", ".join(parts))) if parts else ""
        feedback.pushInfo(self.tr("  [timing] Network Layer: {}{:.3f}s".format(fc_str, elapsed)))
        results["pdp"] = self._save_layer_to_gpkg(
            results["pdp"], "PDPs.gpkg", out_dir, context, feedback)
        final_objects = net.get(self._NET_FINAL_OBJECTS)
        if final_objects:
            results["objects"] = final_objects
            results["objects"] = self._save_layer_to_gpkg(
                results["objects"], "Objects.gpkg", out_dir, context, feedback)
        else:
            raise PipelineStageError(self._stage_error(
                "Network Layer",
                self.tr(
                    "Final_Object_Layer was not produced — objects could not be "
                    "linked to polygons/PDPs, and the Trench, Cable and Duct "
                    "stages depend on that linkage."
                )
            ))
        if not results.get("pdp"):
            raise PipelineStageError(self._stage_error(
                "Network Layer", self.tr("No PDP layer was produced.")
            ))
        mfg_override = self.parameterAsVectorLayer(parameters, self.P_TR_MFG, context)
        if mfg_override is not None:
            feedback.pushInfo(self.tr("Using the user-supplied MFG point override."))
            results["mfg"] = mfg_override
        elif results.get("mfg") is None:
            raise PipelineStageError(self._stage_error(
                "Network Layer",
                self.tr(
                    "No MFG point was produced — the Trench (feeder routing) and "
                    "Duct stages require it. Supply one via '04 Trench — Existing "
                    "MFG point override' or check the Network stage log."
                )
            ))
        t_save = time.time()
        results["mfg"] = self._save_layer_to_gpkg(
            results["mfg"], "MFG.gpkg", out_dir, context, feedback)
        if results.get("mfg_service_areas"):
            results["mfg_service_areas"] = self._save_layer_to_gpkg(
                results["mfg_service_areas"], "MFG_Service_Areas.gpkg",
                out_dir, context, feedback)
        feedback.pushInfo(self.tr("  [timing] Network output saves: {:.3f}s".format(time.time() - t_save)))

        if feedback.isCanceled():
            return {}
        # --- Trench Layer ---
        steps.setCurrentStep(4)
        feedback.pushInfo(self.tr("[60%] Running Trench Layer"))
        t0 = time.time()
        tr = self._run("Trench Layer", self.run_trench_layer,
                       parameters, context, steps, feedback, results=results)
        results["sidewalk_l"] = tr.get(self._TR_SIDE_L)
        results["sidewalk_r"] = tr.get(self._TR_SIDE_R)
        results["trenches"] = tr.get(self._TR_FINAL)
        results["feeder"] = tr.get(self._TR_FEEDER_FINAL)
        elapsed = time.time() - t0
        n_tr = self._fast_count(results["trenches"], context)
        fc_str = "{} features, ".format(n_tr) if n_tr is not None else ""
        feedback.pushInfo(self.tr("  [timing] Trench Layer: {}{:.3f}s".format(fc_str, elapsed)))
        results["garden"] = tr.get(self._TR_GARDEN)
        results["pseudo_hh"] = tr.get("OUT_PSEUDO_HH")
        results["pdp_proj"] = tr.get("OUT_PDP_TO_SIDE")
        results["tangents_used"] = tr.get(self._TR_TAN_USED)
        results["aoi"] = tr.get(self._TR_AOI_DISS)
        results["distribution"] = (
            tr.get(self._TR_DIST_LINES)
            or tr.get(self._TR_DIST_DISS)
            or tr.get(self._TR_MERGED_PDP)
        )
        results["trenches"] = self._save_layer_to_gpkg(
            results["trenches"], "Final_Trenches.gpkg", out_dir, context, feedback)
        results["feeder"] = self._save_layer_to_gpkg(
            results["feeder"], "Feeder_Trench.gpkg", out_dir, context, feedback)
        results["distribution"] = self._save_layer_to_gpkg(
            results["distribution"], "Distribution_Trench.gpkg", out_dir, context, feedback)
        results["garden"] = self._save_layer_to_gpkg(
            results["garden"], "Garden_Trench.gpkg", out_dir, context, feedback)
        results["pseudo_hh"] = self._save_layer_to_gpkg(
            results["pseudo_hh"], "Pseudo_HH.gpkg", out_dir, context, feedback)
        results["tangents_used"] = self._save_layer_to_gpkg(
            results["tangents_used"], "Tangent_Crossings.gpkg", out_dir, context, feedback)
        # Aerial legs the trench stage classified: published so the map shows
        # them as a construction type (never dug), the BOQ excludes their
        # excavation and the LLD carries them as aerial rather than UG.
        results["aerial_drops"] = self._save_layer_to_gpkg(
            tr.get(self._TR_AERIAL_DROPS), "Aerial_Drops.gpkg", out_dir,
            context, feedback)
        # The structural nodes the designer placed: the chamber stage's
        # primary candidate source (a chamber is the opening at a node).
        results["trench_nodes"] = self._save_layer_to_gpkg(
            tr.get(self._TR_TRENCH_NODES), "Trench_Nodes.gpkg", out_dir,
            context, feedback)

        if feedback.isCanceled():
            return {}
        # --- Chamber Layer (civil structures) ---
        # Phase C cascade order (TRENCH_DESIGN.md §6.1): trench → chambers →
        # ducts → cables. Chambers come directly after the trench design
        # because a chamber IS the opening of the trench at a structural node
        # (INPUT_TRENCH_NODES — the designer's Trench_Nodes), and every later
        # stage is built chamber-to-chamber on the spans it defines. The duct
        # inputs are NOT available yet: chamber_layer takes them as optional,
        # and the designer nodes + PDPs + trench junctions supply candidates.
        steps.setCurrentStep(5)
        feedback.pushInfo(self.tr("[75%] Running Chamber Layer"))
        t0 = time.time()
        chambers = self._run("Chamber Layer", self.run_chamber_layer,
                             parameters, context, steps, feedback, results=results)
        results["chambers"] = chambers.get("OUT_CHAMBERS") if chambers else None
        elapsed = time.time() - t0
        n_ch = self._fast_count(results.get("chambers"), context)
        fc_str = "{} features, ".format(n_ch) if n_ch is not None else ""
        feedback.pushInfo(self.tr("  [timing] Chamber Layer: {}{:.3f}s".format(fc_str, elapsed)))
        if results.get("chambers"):
            results["chambers"] = self._save_layer_to_gpkg(
                results["chambers"], "Chambers.gpkg", out_dir, context, feedback)

        # Chambers are the authoritative civil break points. The trench layers
        # are normalized against them NOW, so the duct and cable stages that
        # follow are built chamber-to-chamber (one published component = one
        # chamber-to-chamber span). Cables and ducts do not exist yet at this
        # point — they get their own pass after the cable stage below.
        if results.get("chambers"):
            for key, filename in (
                    ("trenches", "Final_Trenches.gpkg"),
                    ("feeder", "Feeder_Trench.gpkg"),
                    ("distribution", "Distribution_Trench.gpkg"),
                    ("garden", "Garden_Trench.gpkg")):
                original = results.get(key)
                split = self._split_lines_at_chambers(
                    original, results["chambers"], context, feedback, filename)
                if split is not original:
                    results[key] = self._save_layer_to_gpkg(
                        split, filename, out_dir, context, feedback)
            feedback.pushInfo(self.tr(
                "Trench layers normalized at chambers: ducts and cables will "
                "be built on chamber-to-chamber spans."
            ))

        if feedback.isCanceled():
            return {}
        # --- Duct Layer ---
        steps.setCurrentStep(6)
        feedback.pushInfo(self.tr("[85%] Running Duct Layer"))
        self._preflight_duct(results, context, feedback)
        t0 = time.time()
        duct = self._run("Duct Layer", self.run_duct_layer,
                         parameters, context, steps, feedback, results=results)
        results["ducts"] = duct
        results["duct_runs"] = {
            self._DU_FEEDER_RUNS: duct.get(self._DU_FEEDER_RUNS),
            self._DU_DIST_RUNS: duct.get(self._DU_DIST_RUNS),
        }
        elapsed = time.time() - t0
        n_fd = self._fast_count(duct.get(self._DU_OUT_FEEDER), context)
        n_dd = self._fast_count(duct.get(self._DU_OUT_DIST), context)
        n_dr = self._fast_count(duct.get(self._DU_OUT_DROP), context)
        n_cp = self._fast_count(duct.get(self._DU_OUT_COUPLE), context)
        parts = []
        if n_fd is not None:
            parts.append("Feeder: {}".format(n_fd))
        if n_dd is not None:
            parts.append("Dist: {}".format(n_dd))
        if n_dr is not None:
            parts.append("Drop: {}".format(n_dr))
        if n_cp is not None:
            parts.append("Couplers: {}".format(n_cp))
        fc_str = ("{} features, ".format(", ".join(parts))) if parts else ""
        feedback.pushInfo(self.tr("  [timing] Duct Layer: {}{:.3f}s".format(fc_str, elapsed)))
        fd = results["ducts"].get(self._DU_OUT_FEEDER)
        if fd:
            results["ducts"][self._DU_OUT_FEEDER] = self._save_layer_to_gpkg(
                fd, "Feeder_Ducts.gpkg", out_dir, context, feedback)
        dd = results["ducts"].get(self._DU_OUT_DIST)
        if dd:
            results["ducts"][self._DU_OUT_DIST] = self._save_layer_to_gpkg(
                dd, "Distribution_Ducts.gpkg", out_dir, context, feedback)
        dr = results["ducts"].get(self._DU_OUT_DROP)
        if dr:
            results["ducts"][self._DU_OUT_DROP] = self._save_layer_to_gpkg(
                dr, "Drop_Ducts.gpkg", out_dir, context, feedback)
        cp = results["ducts"].get(self._DU_OUT_COUPLE)
        if cp:
            results["ducts"][self._DU_OUT_COUPLE] = self._save_layer_to_gpkg(
                cp, "Coupleurs.gpkg", out_dir, context, feedback)

        if feedback.isCanceled():
            return {}
        # --- Cable Layer (planned last: pulled where the civil work is) ---
        # Last in the cascade — chambers are placed and ducts are laid inside
        # the designed spans before a cable is planned. The route tree is still
        # the (chamber-split) trench network: the ducts lie inside those same
        # spans, so a cable can only go where a duct exists.
        steps.setCurrentStep(7)
        feedback.pushInfo(self.tr("[95%] Running Cable Layer"))
        self._preflight_cable(results, context, feedback)
        t0 = time.time()
        cab = self._run("Cable Layer", self.run_cable_layer,
                        parameters, context, steps, feedback, results=results)
        results["cables"] = cab
        elapsed = time.time() - t0
        n_fc = self._fast_count(cab.get(self._CB_OUT_FEEDER), context)
        n_dc = self._fast_count(cab.get(self._CB_OUT_DIST), context)
        parts = []
        if n_fc is not None:
            parts.append("Feeder: {}".format(n_fc))
        if n_dc is not None:
            parts.append("Dist: {}".format(n_dc))
        fc_str = ("{} features, ".format(", ".join(parts))) if parts else ""
        feedback.pushInfo(self.tr("  [timing] Cable Layer: {}{:.3f}s".format(fc_str, elapsed)))
        fc = results["cables"].get(self._CB_OUT_FEEDER)
        if fc:
            results["cables"][self._CB_OUT_FEEDER] = self._save_layer_to_gpkg(
                fc, "Feeder_Cable.gpkg", out_dir, context, feedback)
        dc = results["cables"].get(self._CB_OUT_DIST)
        if dc:
            results["cables"][self._CB_OUT_DIST] = self._save_layer_to_gpkg(
                dc, "Distribution_Cable.gpkg", out_dir, context, feedback)

        # Second normalization pass: the duct and cable layers were built
        # AFTER the trench pass above, so cut those at the chambers too — one
        # chamber-to-chamber component everywhere on the map and in the BOQ.
        if results.get("chambers"):
            cables = results.get("cables") or {}
            for key, filename, label in (
                    (self._CB_OUT_FEEDER, "Feeder_Cable.gpkg", "Feeder_Cable"),
                    (self._CB_OUT_DIST, "Distribution_Cable.gpkg", "Distribution_Cable")):
                original = cables.get(key)
                split = self._split_lines_at_chambers(
                    original, results["chambers"], context, feedback, label)
                if split is not original:
                    cables[key] = self._save_layer_to_gpkg(
                        split, filename, out_dir, context, feedback)
            results["cables"] = cables

            ducts = results.get("ducts") or {}
            for key, filename, label in (
                    (self._DU_OUT_FEEDER, "Feeder_Ducts.gpkg", "Feeder_Ducts"),
                    (self._DU_OUT_DIST, "Distribution_Ducts.gpkg", "Distribution_Ducts"),
                    (self._DU_OUT_DROP, "Drop_Ducts.gpkg", "Drop_Ducts")):
                original = ducts.get(key)
                split = self._split_lines_at_chambers(
                    original, results["chambers"], context, feedback, label)
                if split is not original:
                    ducts[key] = self._save_layer_to_gpkg(
                        split, filename, out_dir, context, feedback)
            results["ducts"] = ducts
            feedback.pushInfo(self.tr(
                "Chamber span normalization complete: trench, duct, and cable "
                "layers use the same chamber break positions."
            ))

        if feedback.isCanceled():
            return {}
        # --- Pole + Aerial Drop stages: OPT-IN (aerial zones requested) ---
        # Aerial conversion is exception-based per the planning document and
        # ONLY runs when the engineer/planner explicitly supplies aerial
        # zones (or flags premises for aerial). Without zones the pipeline
        # skips both stages and every drop stays buried UG — identical to
        # the pre-aerial pipeline behaviour.
        zones = self.parameterAsVectorLayer(parameters, self.P_AERIAL_ZONES, context)
        aerial_requested = zones is not None and zones.isValid() and zones.featureCount() > 0
        if not aerial_requested:
            feedback.pushInfo(self.tr(
                "  [info] Aerial zones not supplied — Pole/Aerial Drop stages skipped (all drops remain buried UG)."
            ))
            results["poles"] = None
            results["aerial_trench"] = None
            results["aerial_cable"] = None
        else:
            # --- Pole Layer (aerial zones only) ---
            steps.setCurrentStep(8)
            feedback.pushInfo(self.tr("[99%] Running Pole Layer"))
            t0 = time.time()
            poles = self._run("Pole Layer", self.run_pole_layer,
                              parameters, context, steps, feedback, results=results)
            results["poles"] = poles.get("OUT_POLES") if poles else None
            elapsed = time.time() - t0
            n_po = self._fast_count(results.get("poles"), context)
            fc_str = "{} features, ".format(n_po) if n_po is not None else ""
            feedback.pushInfo(self.tr("  [timing] Pole Layer: {}{:.3f}s".format(fc_str, elapsed)))
            if results.get("poles"):
                results["poles"] = self._save_layer_to_gpkg(
                    results["poles"], "Poles.gpkg", out_dir, context, feedback)

            # --- Aerial Drop Layer (pole-to-premise for flagged drops) ---
            if feedback.isCanceled():
                return {}
            steps.setCurrentStep(9)
            feedback.pushInfo(self.tr("[99.5%] Running Aerial Drop Layer"))
            t0 = time.time()
            aerial = self._run("Aerial Drop Layer", self.run_aerial_drop_layer,
                               parameters, context, steps, feedback, results=results)
            results["aerial_trench"] = aerial.get("OUT_AERIAL_TRENCH") if aerial else None
            results["aerial_cable"] = aerial.get("OUT_AERIAL_CABLE") if aerial else None
            elapsed = time.time() - t0
            n_at = self._fast_count(results.get("aerial_trench"), context)
            fc_str = "{} features, ".format(n_at) if n_at is not None else ""
            feedback.pushInfo(self.tr("  [timing] Aerial Drop Layer: {}{:.3f}s".format(fc_str, elapsed)))
            if results.get("aerial_trench"):
                results["aerial_trench"] = self._save_layer_to_gpkg(
                    results["aerial_trench"], "Aerial_Spans.gpkg", out_dir, context, feedback)
            if results.get("aerial_cable"):
                results["aerial_cable"] = self._save_layer_to_gpkg(
                    results["aerial_cable"], "Aerial_Cable.gpkg", out_dir, context, feedback)

        # Explicit premise coverage: a premise counts served only if a published
        # UG drop trench or aerial drop names its stable ADDR_ID.
        served_params = {
            "OBJECTS": results.get("objects"),
            "GARDEN_TRENCH": results.get("garden"),
            "AERIAL_DROPS": results.get("aerial_drops"),
            "OUTPUT": self._dest(parameters, self.OUT_SERVED_PREMISES, context),
        }
        try:
            served_result = processing.run(
                "hldplanning:served_premises", served_params,
                context=context, feedback=feedback, is_child_algorithm=True)
            results["served_premises"] = self._save_layer_to_gpkg(
                served_result.get("OUTPUT"), "Served_Premises.gpkg",
                out_dir, context, feedback)
        except Exception as exc:
            raise PipelineStageError(self._stage_error("Served Premises Register", exc))

        # --- HLD_attr catalogue enrichment (in place on the saved GPKGs) ---
        if out_dir:
            t_enrich = time.time()
            try:
                # The roads input is needed for one thing the pipeline can't
                # derive itself: attributing each trench SECTION to the road
                # it runs along (street name + fclass), which the permits,
                # street tables and BOQ reference all key on.
                _roads_lyr = None
                try:
                    _roads_lyr = self.parameterAsVectorLayer(
                        parameters, self.P_ROADS, context)
                except Exception:
                    _roads_lyr = None
                attr_enrich.enrich_all(out_dir, feedback, roads_lyr=_roads_lyr)
            except Exception as exc:
                feedback.pushWarning(
                    self.tr("Catalogue enrichment failed: %s") % exc)
            feedback.pushInfo(self.tr(
                "  [timing] Attribute enrichment: {:.3f}s".format(time.time() - t_enrich)
            ))

        feedback.pushInfo(self.tr(
            "[timing] Total pipeline: {:.3f}s".format(time.time() - t_pipeline)
        ))
        feedback.pushInfo(self.tr("[100%] Complete"))

        return self._collect_outputs(results, parameters, context, feedback)

    def _trace(self, message):
        """Append a timestamped stage line when HLD_STAGE_TRACE names a file.

        The Processing feedback log is block-buffered when the pipeline runs
        headless, so a hanging stage can leave the log far behind reality.
        This writes straight to a file (flush per line) for run diagnostics
        only, and is a no-op unless the env var is set.
        """
        path = os.environ.get("HLD_STAGE_TRACE")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
        except Exception:
            pass

    def _run(self, stage_name, runner, parameters, context, feedback, base_feedback=None,
             results=None):
        base = base_feedback if base_feedback is not None else feedback
        self._trace("START %s" % stage_name)
        try:
            if results is None:
                out = runner(parameters, context, feedback)
            else:
                out = runner(parameters, results, context, feedback)
            self._trace("END %s" % stage_name)
            return out
        except PipelineStageError:
            raise
        except Exception as exc:
            if base.isCanceled():
                raise QgsProcessingException(
                    self.tr("Pipeline canceled during %s.") % stage_name
                )
            raise PipelineStageError(self._stage_error(stage_name, exc))

    def _collect_outputs(self, results, parameters, context, feedback):
        out = {}

        def put(key, value):
            if value not in (None, ""):
                out[key] = value

        cables = results.get("cables") or {}
        ducts = results.get("ducts") or {}

        put(self.OUT_BROWNFIELD, results.get("brownfield_output"))
        put(self.OUT_BROWNFIELD_POINTS, results.get("brownfield_points"))
        put(self.OUT_OBJECTS, results.get("objects"))
        put(self.OUT_POLYGONS, results.get("polygons"))
        put(self.OUT_PDP, results.get("pdp"))
        put(self.OUT_MFG, results.get("mfg"))
        put(self.OUT_MFG_AREAS, results.get("mfg_service_areas"))
        put(self.OUT_FEEDER_TRENCH, results.get("feeder"))
        put(self.OUT_DIST_TRENCH, results.get("distribution"))
        put(self.OUT_GARDEN_TRENCH, results.get("garden"))
        put(self.OUT_TRENCHES, results.get("trenches"))
        put(self.OUT_FEEDER_CABLE, cables.get(self._CB_OUT_FEEDER))
        put(self.OUT_DIST_CABLE, cables.get(self._CB_OUT_DIST))
        put(self.OUT_FEEDER_DUCTS, ducts.get(self._DU_OUT_FEEDER))
        put(self.OUT_DIST_DUCTS, ducts.get(self._DU_OUT_DIST))
        put(self.OUT_DROP_DUCTS, ducts.get(self._DU_OUT_DROP))
        put(self.OUT_CHAMBERS, results.get("chambers"))
        put(self.OUT_POLES, results.get("poles"))
        put(self.OUT_AERIAL_TRENCHES, results.get("aerial_trench"))
        put(self.OUT_AERIAL_CABLE, results.get("aerial_cable"))
        put(self.OUT_SERVED_PREMISES, results.get("served_premises"))

        return out

    def _validate_inputs(self, parameters, context, feedback):
        excel = self.parameterAsFile(parameters, self.P_EXCEL, context)
        if not excel or not os.path.exists(excel):
            raise QgsProcessingException(self.tr(
                "Input Excel file not found: %s") % (excel or "<empty>"))

        roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        if roads is None or not roads.isValid():
            raise QgsProcessingException(self.tr(
                "A valid Roads layer is required (Network and Trench stages need it)."
            ))

        min_hh = self.parameterAsInt(parameters, self.P_POLY_MIN_HH, context)
        max_hh = self.parameterAsInt(parameters, self.P_POLY_MAX_HH, context)
        if max_hh < min_hh:
            raise QgsProcessingException(self.tr(
                "02 Polygon — maximum homes per polygon (%d) must be >= minimum (%d)."
            ) % (max_hh, min_hh))

        tr_roads = self.parameterAsVectorLayer(parameters, self.P_TR_ROADS, context)
        roads_for_trench = tr_roads if tr_roads is not None else roads
        if not self._has_field(roads_for_trench, self._ROAD_CLASS_FIELDS):
            feedback.pushWarning(self.tr(
                "The roads layer used for trenching ('%s') has NO road class "
                "field (fclass/highway/class). All lines will be treated as "
                "walkable, footways and vehicular roads cannot be told apart, "
                "and feeder/distribution routing quality will degrade — cables "
                "and ducts may come out empty. Strongly recommended: use an OSM "
                "roads export that keeps the fclass (Geofabrik shapefiles) or "
                "highway (raw OSM) attribute and includes footway/path/service "
                "lines."
            ) % roads_for_trench.name())
        if not self._has_field(roads, self._ROAD_CLASS_FIELDS):
            feedback.pushWarning(self.tr(
                "The main Roads layer ('%s') has no fclass/highway field — the "
                "Network stage cannot filter PDP-candidate streets and will use "
                "all clipped roads."
            ) % roads.name())

    def _setup_logging(self, parameters, context, feedback):
        """Open a timestamped log file and set up a LoggingFeedback wrapper.
        Returns (log_feedback, log_path) — log_feedback wraps the original
        feedback and also writes to the file; log_path is the file path or None."""
        try:
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(log_dir, f"OC_{ts}.txt")
            lines = [
                f"QGIS version: {Qgis.version()}",
                f"QGIS code revision: {Qgis.devVersion()}",
            ]
            try:
                from qgis.PyQt.QtCore import QT_VERSION_STR
                lines.append(f"Qt version: {QT_VERSION_STR}")
            except Exception:
                pass
            lines.append("")
            lines.append(f"Algorithm started at: {datetime.datetime.now().isoformat()}")
            lines.append("Algorithm 'One Click – End-to-End HLD Pipeline' starting...")
            lines.append("Input parameters:")
            lines.append(repr({k: v for k, v in parameters.items()
                               if not k.startswith("_")}))
            lines.append("")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            return _LoggingFeedback(feedback, log_path), log_path
        except Exception as exc:
            feedback.pushWarning(self.tr(
                "Could not set up log file: %s"
            ) % exc)
            return feedback, None

    def _finalize_logging(self, log_path, log_feedback, out, exc_info=None):
        """Append final results (or error info) to the log file."""
        if not log_path:
            return
        # Flush any buffered log messages before writing final results
        if hasattr(log_feedback, 'flush'):
            try:
                log_feedback.flush()
            except Exception:
                pass
        try:
            lines = []
            if exc_info:
                lines.append("")
                lines.append(f"Execution FAILED after {exc_info}.")
            else:
                lines.append("")
                lines.append("Results:")
                for k, v in out.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")
                lines.append("Loading resulting layers")
                lines.append("Algorithm 'One Click – End-to-End HLD Pipeline' finished")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def postProcessAlgorithm(self, context, feedback):
        """
        QGIS 3.38+ post-processing hook — called on the main thread after
        runPrepared() finishes, exactly once per run.

        Organises output layers into a hierarchical QGIS layer tree with groups:
          Object Layer / Polygon Layer (root)
          Brownfield, Network, Trenches, Cables, Ducts, Civil (groups)

        NOTE: this hook does NOT receive the results map, so processAlgorithm()
        stashes it in self._last_results. Returns an empty map so the results
        returned by processAlgorithm() are kept unchanged.

        Headless-mode calls (qgis_process) are silently skipped because
        QgsProject is not available.
        """
        results = getattr(self, "_last_results", None) or {}
        try:
            self._build_layer_tree(results, context)
        except Exception as exc:
            # Never let the post-run hook break an otherwise successful run.
            try:
                feedback.pushWarning(self.tr(
                    "Layer-tree grouping failed (outputs still valid): %s") % exc)
            except Exception:
                pass
        return {}

    def postProcess(self, results, context, feedback):
        """
        Legacy hook (QGIS < 3.38) — kept so older QGIS installs still populate
        the layer tree. Newer QGIS releases dispatch to postProcessAlgorithm()
        instead of this method.
        """
        self._build_layer_tree(results, context)

    def _build_layer_tree(self, results, context):
        """Shared implementation of the hierarchical layer-tree grouping used
        by both postProcessAlgorithm() (QGIS 3.38+) and postProcess() (legacy)."""
        try:
            from qgis.core import (
                QgsProject, QgsVectorLayer, QgsProcessingUtils, QgsMapLayer,
            )
        except Exception:
            return  # Not running inside a QGIS environment

        project = QgsProject.instance()
        if project is None:
            return  # Headless mode (qgis_process)

        # ---- group layout ---------------------------------------------------
        # Maps output-key -> (group_name, display_name, order_within_group)
        LAYOUT = {
            self.OUT_BROWNFIELD:       ("Brownfield", "Existing Infrastructure", 0),
            self.OUT_BROWNFIELD_POINTS: ("Brownfield", "Existing Points", 1),
            self.OUT_OBJECTS:          (None, "Object Layer", 0),    # root
            self.OUT_POLYGONS:   (None, "Polygon Layer", 0),       # root
            self.OUT_PDP:        ("Network", "PDPs", 0),
            self.OUT_MFG:        ("Network", "MFG", 1),
            self.OUT_MFG_AREAS:  ("Network", "MFG Service Areas", 2),
            self.OUT_FEEDER_TRENCH:  ("Trenches", "Feeder", 0),
            self.OUT_DIST_TRENCH:    ("Trenches", "Distribution", 1),
            self.OUT_GARDEN_TRENCH:  ("Trenches", "Drop (HH->Footway)", 2),
            self.OUT_TRENCHES:       ("Trenches", "Final Trenches", 3),
            self.OUT_FEEDER_CABLE: ("Cables", "Feeder", 0),
            self.OUT_DIST_CABLE:   ("Cables", "Distribution", 1),
            self.OUT_FEEDER_DUCTS: ("Ducts", "Feeder", 0),
            self.OUT_DIST_DUCTS:   ("Ducts", "Distribution", 1),
            self.OUT_DROP_DUCTS:   ("Ducts", "Drop", 2),
            self.OUT_CHAMBERS:     ("Civil", "Chambers", 0),
            self.OUT_POLES:        ("Civil", "Poles", 1),
            self.OUT_AERIAL_TRENCHES: ("Civil", "Aerial Drops", 2),
            self.OUT_AERIAL_CABLE:    ("Cables", "Aerial Drop", 2),
            self.OUT_SERVED_PREMISES: ("Network", "Served Premises", 2),
        }

        # Ordered group list (top-to-bottom in the legend)
        GROUP_ORDER = ("Brownfield", "Network", "Trenches", "Cables", "Ducts", "Civil")

        # Distinct colour per sublayer so the members of a group are easy to
        # tell apart in the legend (and in QField): feeder blue, distribution
        # green, garden orange, final purple, etc.
        LAYER_COLORS = {
            self.OUT_BROWNFIELD:        "#808080",  # grey
            self.OUT_BROWNFIELD_POINTS: "#9e9e9e",  # light grey
            self.OUT_MFG_AREAS:         "#65a30d",  # olive green service boundary
            self.OUT_FEEDER_TRENCH:     "#1e88e5",  # blue
            self.OUT_DIST_TRENCH:    "#43a047",  # green
            self.OUT_GARDEN_TRENCH:  "#fb8c00",  # orange
            self.OUT_TRENCHES:       "#8e24aa",  # purple
            self.OUT_FEEDER_CABLE:   "#d81b60",  # pink/red
            self.OUT_DIST_CABLE:     "#00897b",  # teal
            self.OUT_FEEDER_DUCTS:   "#5e35b1",  # violet
            self.OUT_DIST_DUCTS:     "#fdd835",  # yellow
            self.OUT_DROP_DUCTS:     "#00838f",  # dark cyan
            self.OUT_CHAMBERS:       "#757575",  # grey
            self.OUT_POLES:          "#795548",  # brown
            self.OUT_AERIAL_TRENCHES: "#ad1457",  # deep pink
            self.OUT_AERIAL_CABLE:    "#6a1b9a",  # purple
        }

        # ---- helpers --------------------------------------------------------
        def _resolve(key, val):
            """Resolve a QgsMapLayer from the output value."""
            if val is None or (isinstance(val, str) and not val.strip()):
                return None
            if isinstance(val, QgsMapLayer):
                return val
            if not isinstance(val, str):
                return None
            # Temporary layer store (memory layers from child algorithms)
            try:
                store = context.temporaryLayerStore()
                if store:
                    lyr = store.mapLayer(val)
                    if lyr is not None:
                        return lyr
            except Exception:
                pass
            # Standard context lookup (resolves layer IDs and file URIs)
            try:
                return QgsProcessingUtils.mapLayerFromString(val, context)
            except Exception:
                pass
            # Bare file path fallback
            try:
                name = LAYOUT.get(key, (None, key, 0))[1]
                lyr = QgsVectorLayer(val, name, "ogr")
                if lyr.isValid():
                    return lyr
            except Exception:
                pass
            return None

        def _remove_existing(source_uri):
            """Remove any layer already in the project with the same source URI
            so re-runs replace layers instead of duplicating them."""
            if not source_uri or not isinstance(source_uri, str):
                return
            for existing in list(project.mapLayers().values()):
                try:
                    if existing.source() == source_uri:
                        project.removeMapLayer(existing)
                except Exception:
                    pass

        # ---- build groups (insert at top, reversed order for correct stacking) ---
        root = project.layerTreeRoot()
        groups = {}
        for grp_name in reversed(GROUP_ORDER):
            existing = root.findGroup(grp_name)
            if existing is not None:
                # Clear stale child layers from previous runs
                for child in list(existing.children()):
                    existing.removeChildNode(child)
                # Move group to top of legend
                root.removeChildNode(existing)
                root.insertChildNode(0, existing)
                groups[grp_name] = existing
            else:
                groups[grp_name] = root.insertGroup(0, grp_name)

        # ---- add each output to its group (or root) -------------------------
        for key, val in results.items():
            if key not in LAYOUT:
                continue
            lyr = _resolve(key, val)
            if lyr is None:
                continue

            grp_name, display_name, _ = LAYOUT[key]

            # Set a friendly layer name
            try:
                lyr.setName(display_name)
            except Exception:
                pass

            # Give each sublayer a distinct colour so group members are
            # distinguishable (feeder vs distribution vs garden, ...).
            # Both helpers already guard against invalid layers / geometry.
            hexcol = LAYER_COLORS.get(key)
            if hexcol:
                try:
                    if lyr.geometryType() == QgsWkbTypes.PointGeometry:
                        from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer
                        m = QgsMarkerSymbol.createSimple(
                            {"color": hexcol, "outline_color": "0,0,0,255",
                             "size": "2.2", "size_unit": "MM"})
                        lyr.setRenderer(QgsSingleSymbolRenderer(m))
                    elif lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
                        from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer
                        symbol = QgsFillSymbol.createSimple({
                            "color": hexcol + ",45", "outline_color": hexcol,
                            "outline_width": "0.8",
                        })
                        lyr.setRenderer(QgsSingleSymbolRenderer(symbol))
                    else:
                        apply_simple_line_style(lyr, hexcol, 1.0)
                except Exception:
                    pass

            # Remove any previous project layer with the same source
            try:
                _remove_existing(lyr.source())
            except Exception:
                pass

            if grp_name is None:
                # Root level — add normally
                project.addMapLayer(lyr)
                # Move to top
                tl = root.findLayer(lyr.id())
                if tl:
                    c = tl.clone()
                    root.insertChildNode(0, c)
                    root.removeChildNode(tl)
            else:
                grp = groups.get(grp_name)
                if grp is None:
                    project.addMapLayer(lyr)
                else:
                    project.addMapLayer(lyr, False)
                    grp.addLayer(lyr)

        # Every output we care about is now in the project tree. Clear the
        # context's pending-load list so the Processing GUI does not load
        # (and duplicate) the same layers again — in particular the
        # intermediate layers registered by the child algorithm runs during
        # the pipeline.
        try:
            context.setLayersToLoadOnCompletion({})
        except Exception:
            pass

    def _arm_dump_traceback(self):
        """When HLD_FAULTHANDLER names a file, dump the Python stack every
        60 s there. A headless run block-buffers its log, so a hung stage can
        otherwise leave no clue where it is stuck. Diagnostics only — a no-op
        unless the env var is set."""
        path = os.environ.get("HLD_FAULTHANDLER")
        if not path:
            return
        try:
            import faulthandler
            self._fh_handle = open(path, "a", encoding="utf-8")
            faulthandler.dump_traceback_later(
                60, repeat=True, file=self._fh_handle)
        except Exception:
            pass

    def processAlgorithm(self, parameters, context, feedback):
        log_feedback, log_path = self._setup_logging(
            parameters, context, feedback)
        self._arm_dump_traceback()
        try:
            self._validate_inputs(parameters, context, log_feedback)
            out = self.execute_pipeline(parameters, context, log_feedback)
            self._copy_hardcoded_reports(parameters, context, log_feedback)
            # Include BOQ/BOM file paths in output if they exist
            out_dir = self._output_dir(parameters, context)
            if out_dir:
                boq_path = os.path.join(out_dir, "BOQ.xlsx")
                bom_path = os.path.join(out_dir, "BOM.xlsx")
                if os.path.isfile(boq_path):
                    out["BOQ"] = boq_path
                if os.path.isfile(bom_path):
                    out["BOM"] = bom_path
            # Stash the exact results so the QGIS 3.38+ postProcessAlgorithm()
            # hook (which does not receive the results map) can populate the
            # layer tree with the same values returned to the GUI.
            self._last_results = out
            self._finalize_logging(log_path, log_feedback, out)
            return out
        except Exception as exc:
            self._finalize_logging(log_path, log_feedback, None, exc_info=str(exc))
            raise
