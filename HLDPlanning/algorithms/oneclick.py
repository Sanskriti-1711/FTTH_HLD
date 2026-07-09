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
    QgsCoordinateReferenceSystem,
    QgsVectorFileWriter,
    QgsMapLayer,
    Qgis,
)
from qgis import processing

from ..utils.params import ALG, LAYERNAMES


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

    P_TR_ROADS = "TRENCH_ROADS"
    P_BUILDINGS = "BUILDINGS"
    P_TR_MFG = "TRENCH_MFG"

    OUT_OBJECTS = "OUT_OBJECTS"
    OUT_POLYGONS = "OUT_POLYGONS"
    OUT_PDP = "OUT_PDP"
    OUT_MFG = "OUT_MFG"
    
    OUT_TRENCHES = "OUT_TRENCHES"
    OUT_FEEDER_CABLE = "OUT_FEEDER_CABLE"
    OUT_DIST_CABLE = "OUT_DIST_CABLE"
    OUT_FEEDER_DUCTS = "OUT_FEEDER_DUCTS"
    OUT_DIST_DUCTS = "OUT_DIST_DUCTS"

    _DEFAULT_OUTPUT_FILES = {
        OUT_OBJECTS: "Objects.gpkg",
        OUT_POLYGONS: "Polygons.gpkg",
        OUT_PDP: "PDPs.gpkg",
        OUT_MFG: "MFG.gpkg",
        OUT_TRENCHES: "Final_Trenches.gpkg",
        OUT_FEEDER_CABLE: "Feeder_Cable.gpkg",
        OUT_DIST_CABLE: "Distribution_Cable.gpkg",
        OUT_FEEDER_DUCTS: "Feeder_Ducts.gpkg",
        OUT_DIST_DUCTS: "Distribution_Ducts.gpkg",
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
    _NET_ASSIGNED, _NET_MFG, _NET_FINAL_OBJECTS = (
        "OUT_ASSIGNED", "OUT_MFG_POINT", "OUT_FINAL_OBJECTS",
    )

    _TR_POLY, _TR_ROADS_KEY, _TR_PDP = "INPUT_POLY", "INPUT_ROADS", "INPUT_PDP"
    _TR_HH, _TR_BLDG, _TR_MFG_KEY = "INPUT_HOUSEHOLDS", "INPUT_BUILDINGS", "INPUT_MFG"
    _TR_SIDE_L, _TR_SIDE_R = "OUT_SIDEWALK_LEFT", "OUT_SIDEWALK_RIGHT"
    _TR_MERGED_PDP, _TR_FEEDER_FINAL = "OUT_MERGED_PDP", "OUT_FEEDER_FINAL"
    _TR_GARDEN, _TR_FINAL, _TR_FINAL_TAN = (
        "OUT_GARDEN_TRENCHES", "OUT_FINAL_TRENCHES", "OUT_FINAL_TANGENT_TRENCHES",
    )
    _TR_DIST_LINES, _TR_DIST_DISS = "OUT_DISTRIBUTION_LINES", "OUT_DISTRIBUTION_DISS"
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
    )

    _CB_FEEDER, _CB_GARDEN, _CB_DISTR = (
        "FEEDER_TRENCH", "GARDEN_TRENCHES", "DISTR_TRENCHES",
    )
    _CB_OUT_FEEDER, _CB_OUT_DIST = "OUT_FEEDER_CABLE", "OUT_DISTRIBUTION_CABLE"

    _DU_NETWORK, _DU_MFG, _DU_PDP, _DU_OBJECTS = (
        "NETWORK_LINES", "MFG_POINTS", "PDP_POINTS", "OBJECT_POINTS",
    )
    _DU_SIDE_L, _DU_SIDE_R, _DU_FINAL_TAN = (
        "SIDEWALK_LEFT", "SIDEWALK_RIGHT", "FINAL_TANGENT_TRENCHES",
    )
    _DU_OUT_FEEDER, _DU_OUT_DIST = "OUT_FEEDER_DUCTS", "OUT_DISTRIBUTION_DUCTS"

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

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_OBJECTS, self.tr("Final Object Layer"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_POLYGONS, self.tr("Polygons"),
            QgsProcessing.TypeVectorPolygon, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_PDP, self.tr("PDPs (per polygon)"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_MFG, self.tr("MFG point"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_TRENCHES, self.tr("Final Trenches"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FEEDER_CABLE, self.tr("Feeder Cable"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DIST_CABLE, self.tr("Distribution Cable"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FEEDER_DUCTS, self.tr("Feeder Ducts"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_DIST_DUCTS, self.tr("Distribution Ducts"),
            QgsProcessing.TypeVectorLine, optional=True, createByDefault=True
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
        out_dir = self._output_dir(parameters, context)
        if not out_dir:
            return

        plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        search_roots = [
            plugin_root,
            os.getcwd(),
            os.path.dirname(plugin_root),
        ]

        workspace_root = os.environ.get("HLDPLANNING_WORKSPACE_ROOT", "").strip()
        if workspace_root:
            search_roots.append(workspace_root)

        seen = set()
        deduped_roots = []
        for root in search_roots:
            if not root:
                continue
            norm = os.path.abspath(root)
            if norm in seen:
                continue
            seen.add(norm)
            deduped_roots.append(norm)

        copied = 0
        missing = []
        for rel_path in self._HARDCODED_REPORT_FILES:
            src = None
            rel_or_abs = rel_path
            if os.path.isabs(rel_or_abs) and os.path.exists(rel_or_abs):
                src = rel_or_abs
            else:
                for root in deduped_roots:
                    cand = os.path.join(root, rel_or_abs)
                    if os.path.exists(cand):
                        src = cand
                        break

            if src is None:
                missing.append(rel_path)
                continue

            dst = os.path.join(out_dir, os.path.basename(src))
            try:
                shutil.copy2(src, dst)
                copied += 1
            except Exception as exc:
                feedback.pushWarning(self.tr(
                    "Failed to copy hardcoded report template '%s': %s"
                ) % (src, exc))

        if copied:
            feedback.pushInfo(self.tr(
                "Copied %d hardcoded report file(s) (BOQ/BOM) to output folder."
            ) % copied)
        elif missing:
            feedback.pushInfo(self.tr(
                "BOQ/BOM template files were not found in configured hardcoded locations; skipping report copy."
            ))

    def _object_source(self, gpkg_path):
        if gpkg_path:
            return "{0}|layername={1}".format(gpkg_path, LAYERNAMES.OBJECT)
        return None

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
            self._NET_FINAL_OBJECTS: self._dest(parameters, self.OUT_OBJECTS, context),
        }
        if roads is not None:
            params[self._NET_ROADS] = roads
        if pbf:
            params[self._NET_PBF] = pbf
        return processing.run(ALG.NETWORK, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

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
        return processing.run(ALG.TRENCH, params, context=context, feedback=feedback,
                              is_child_algorithm=True)

    def run_cable_layer(self, parameters, results, context, feedback):
        params = {
            self._CB_FEEDER: results.get("feeder"),
            self._CB_GARDEN: results.get("garden"),
            self._CB_DISTR: results.get("distribution"),
            self._CB_OUT_FEEDER: self._dest(parameters, self.OUT_FEEDER_CABLE, context),
            self._CB_OUT_DIST: self._dest(parameters, self.OUT_DIST_CABLE, context),
        }
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
        }
        if results.get("sidewalk_l"):
            params[self._DU_SIDE_L] = results["sidewalk_l"]
        if results.get("sidewalk_r"):
            params[self._DU_SIDE_R] = results["sidewalk_r"]
        return processing.run(ALG.DUCT, params, context=context, feedback=feedback,
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

    def execute_pipeline(self, parameters, context, feedback):
        t_pipeline = time.time()
        results = {}
        steps = QgsProcessingMultiStepFeedback(6, feedback)
        out_dir = self._output_dir(parameters, context)

        if feedback.isCanceled():
            return {}
        # --- Object Layer ---
        steps.setCurrentStep(0)
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
        steps.setCurrentStep(1)
        feedback.pushInfo(self.tr("[20%] Running Polygon Layer"))
        t0 = time.time()
        poly = self._run("Polygon Layer", self.run_polygon_layer,
                         parameters, context, steps, feedback, results=results)
        results["polygons"] = poly.get(self._POLY_OUT)
        elapsed = time.time() - t0
        n_poly = self._fast_count(results["polygons"], context)
        fc_str = "{} features, ".format(n_poly) if n_poly is not None else ""
        feedback.pushInfo(self.tr("  [timing] Polygon Layer: {}{:.3f}s".format(fc_str, elapsed)))
        results["polygons"] = self._save_layer_to_gpkg(
            results["polygons"], "Polygons.gpkg", out_dir, context, feedback)

        if feedback.isCanceled():
            return {}
        # --- Network Layer ---
        steps.setCurrentStep(2)
        feedback.pushInfo(self.tr("[35%] Running Network Layer"))
        t0 = time.time()
        net = self._run("Network Layer", self.run_network_layer,
                        parameters, context, steps, feedback, results=results)
        results["network"] = net.get(self._NET_EDGES)
        results["pdp"] = net.get(self._NET_ASSIGNED)
        results["mfg"] = net.get(self._NET_MFG)
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
        results["mfg"] = self._save_layer_to_gpkg(
            results["mfg"], "MFG.gpkg", out_dir, context, feedback)

        if feedback.isCanceled():
            return {}
        # --- Trench Layer ---
        steps.setCurrentStep(3)
        feedback.pushInfo(self.tr("[55%] Running Trench Layer"))
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
        results["distribution"] = (
            tr.get(self._TR_DIST_LINES)
            or tr.get(self._TR_DIST_DISS)
            or tr.get(self._TR_MERGED_PDP)
        )
        results["trenches"] = self._save_layer_to_gpkg(
            results["trenches"], "Final_Trenches.gpkg", out_dir, context, feedback)

        if feedback.isCanceled():
            return {}
        # --- Cable Layer ---
        steps.setCurrentStep(4)
        feedback.pushInfo(self.tr("[75%] Running Cable Layer"))
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

        if feedback.isCanceled():
            return {}
        # --- Duct Layer ---
        steps.setCurrentStep(5)
        feedback.pushInfo(self.tr("[90%] Running Duct Layer"))
        self._preflight_duct(results, context, feedback)
        t0 = time.time()
        duct = self._run("Duct Layer", self.run_duct_layer,
                         parameters, context, steps, feedback, results=results)
        results["ducts"] = duct
        elapsed = time.time() - t0
        n_fd = self._fast_count(duct.get(self._DU_OUT_FEEDER), context)
        n_dd = self._fast_count(duct.get(self._DU_OUT_DIST), context)
        parts = []
        if n_fd is not None:
            parts.append("Feeder: {}".format(n_fd))
        if n_dd is not None:
            parts.append("Dist: {}".format(n_dd))
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

        feedback.pushInfo(self.tr(
            "[timing] Total pipeline: {:.3f}s".format(time.time() - t_pipeline)
        ))
        feedback.pushInfo(self.tr("[100%] Complete"))

        return self._collect_outputs(results, parameters, context, feedback)

    def _run(self, stage_name, runner, parameters, context, feedback, base_feedback=None,
             results=None):
        base = base_feedback if base_feedback is not None else feedback
        try:
            if results is None:
                return runner(parameters, context, feedback)
            return runner(parameters, results, context, feedback)
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

        put(self.OUT_OBJECTS, results.get("objects"))
        put(self.OUT_POLYGONS, results.get("polygons"))
        put(self.OUT_PDP, results.get("pdp"))
        put(self.OUT_MFG, results.get("mfg"))
        put(self.OUT_TRENCHES, results.get("trenches"))
        put(self.OUT_FEEDER_CABLE, cables.get(self._CB_OUT_FEEDER))
        put(self.OUT_DIST_CABLE, cables.get(self._CB_OUT_DIST))
        put(self.OUT_FEEDER_DUCTS, ducts.get(self._DU_OUT_FEEDER))
        put(self.OUT_DIST_DUCTS, ducts.get(self._DU_OUT_DIST))

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

    def processAlgorithm(self, parameters, context, feedback):
        log_feedback, log_path = self._setup_logging(
            parameters, context, feedback)
        try:
            self._validate_inputs(parameters, context, log_feedback)
            out = self.execute_pipeline(parameters, context, log_feedback)
            self._copy_hardcoded_reports(parameters, context, log_feedback)
            self._finalize_logging(log_path, log_feedback, out)
            return out
        except Exception as exc:
            self._finalize_logging(log_path, log_feedback, None, exc_info=str(exc))
            raise
