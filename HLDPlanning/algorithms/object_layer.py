# -*- coding: utf-8 -*-
import os, re, json, warnings
import pandas as pd

try:
    import geopandas as gpd
    from shapely.geometry import Point
    _GEO_OK = True
except Exception:
    _GEO_OK = False
    Point = None  # type: ignore

warnings.filterwarnings("ignore", category=UserWarning, module="geopandas")

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterFile,
    QgsProcessingParameterString, QgsProcessingParameterCrs,
    QgsProcessingParameterBoolean, QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber, QgsCoordinateReferenceSystem,
    QgsVectorLayer, QgsProject, QgsProcessingException,
    QgsProcessingOutputVectorLayer, QgsProcessingOutputFile,
    QgsProcessingOutputNumber, QgsProcessingOutputString,
)

from ..utils.address_utils import build_structured_query, NominatimClient

# --- shared utilities (moved out of this file) ---
from ..utils.sheet_utils import (
    EXPECTED_MAP, fix_header_row, autodetect_mapping,
    ensure_households_column, generate_addr_ids,
)
from ..utils.geo_utils import valid_xy as _valid_xy
from ..utils.file_io import write_vector_geopandas
from ..utils.params import LAYERNAMES, FIELD
from ..utils.fields import COMMON_FIELDS
from ..utils.style_utils import apply_qml_if_exists

# optional styling helper (keep if you want HH-specific symbology)
try:
    from ..utils.symbology_utils import apply_hh_symbology
except Exception:
    apply_hh_symbology = None


# NOTE: autodetect_mapping() and fix_header_row() are imported from utils.sheet_utils.
# Do not redefine them here to keep a single source of truth.


def _projected_to_wgs84(x, y, src_epsg):
    """
    Reproject a projected coordinate pair (easting, northing) into WGS84
    (longitude, latitude).  Used to fill the LATITUDE/LONGITUDE attribute
    columns with sensible degree values for sheet_xy rows whose geometry
    was already supplied in a projected CRS.

    Returns (lon, lat) on success, or (None, None) on failure -- caller can
    fall back to storing the projected values verbatim if it wants.
    """
    if x is None or y is None:
        return None, None
    src = str(src_epsg or "").strip()
    if not src:
        return None, None
    # Already geographic WGS84 -- nothing to do.
    if src.upper() in ("EPSG:4326", "4326", "WGS84", "WGS 84"):
        try:
            return float(x), float(y)
        except Exception:
            return None, None
    try:
        # pyproj ships with every QGIS install and most geopandas setups.
        from pyproj import Transformer
        t = Transformer.from_crs(src, "EPSG:4326", always_xy=True)
        lon, lat = t.transform(float(x), float(y))
        return float(lon), float(lat)
    except Exception:
        return None, None


class BuildObjectLayer(QgsProcessingAlgorithm):
    # Parameter surface slimmed 2026-07-03: fixed internal defaults replace the
    # old MIN_DELAY / OUT_SHP / INCLUDE_ALL / FORCE_LIVE / ADDRESS_ID_PREFIX
    # inputs (values preserved below as DEFAULT_*).
    PARAM_EXCEL       = "EXCEL"
    PARAM_SHEET       = "SHEET"
    PARAM_EMAIL       = "EMAIL"
    PARAM_OUT_CRS     = "OUT_CRS"
    PARAM_OUT_GPKG    = "OUT_GPKG"
    PARAM_THIN_EXPORT = "THIN_EXPORT"

    DEFAULT_MIN_DELAY   = 1.2     # seconds between Nominatim requests
    DEFAULT_INCLUDE_ALL = True    # keep not-found rows at the (0, 85) sentinel
    DEFAULT_FORCE_LIVE  = False   # always honor the geocode cache
    DEFAULT_ADDR_PREFIX = "ADDR"  # ADDR_ID prefix when the column is missing

    def tr(self, s): return QCoreApplication.translate("BuildObjectLayer", s)
    def createInstance(self): return BuildObjectLayer()
    def name(self): return "01_object_layer"
    def displayName(self): return self.tr("Build from Excel")
    def group(self): return self.tr("01 Object Layer")
    def groupId(self): return "01_object_layer"
    def flags(self):
        # GeoPandas/Fiona/GDAL cleanup is not reliable in QGIS worker threads.
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading
    def shortHelpString(self):
        return self.tr(
            "Reads Excel, normalizes addresses, geocodes with Nominatim (cached, rate-limited), "
            "generates ADDR_IDs if needed, and writes SHP/GPKG with QML style."
        )

    def initAlgorithm(self, config=None):
        # Inputs
        self.addParameter(QgsProcessingParameterFile(
            self.PARAM_EXCEL, self.tr("Input Excel (.xlsx)"), extension="xlsx"
        ))
        self.addParameter(QgsProcessingParameterString(
            self.PARAM_SHEET, self.tr("Sheet name (blank = first)"),
            optional=True, defaultValue=""
        ))
        self.addParameter(QgsProcessingParameterString(
            self.PARAM_EMAIL, self.tr("Email for Nominatim User-Agent"),
            defaultValue="you@example.com"
        ))
        self.addParameter(QgsProcessingParameterCrs(
            self.PARAM_OUT_CRS, self.tr("Output CRS"),
            defaultValue=QgsCoordinateReferenceSystem("EPSG:25833")
        ))

        # Output
        self.addParameter(QgsProcessingParameterFileDestination(
            self.PARAM_OUT_GPKG, self.tr("Output GeoPackage (.gpkg)"),
            fileFilter="GeoPackage (*.gpkg)"
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.PARAM_THIN_EXPORT,
            self.tr("Thin output profile (downstream-safe, minimal fields)"),
            defaultValue=False
        ))

        # Declared outputs for auxiliary results
        # NOTE: OUT_SHP and OUT_GPKG are already outputs via QgsProcessingParameterFileDestination
        # Do NOT call addOutput() for them again -- it causes QGIS output resolution to fail.
        self.addOutput(QgsProcessingOutputNumber("COUNT_OK",        self.tr("Geocoded count")))
        self.addOutput(QgsProcessingOutputNumber("COUNT_NOT_FOUND", self.tr("Not-found count")))
        self.addOutput(QgsProcessingOutputFile("NOT_FOUND",         self.tr("Not-found CSV")))
        self.addOutput(QgsProcessingOutputFile("QML",               self.tr("QML style path")))
    def processAlgorithm(self, parameters, context, feedback):
        # --- Read and validate inputs
        excel = self.parameterAsFile(parameters, self.PARAM_EXCEL, context)
        if not excel or not os.path.exists(excel):
            raise QgsProcessingException(self.tr("Input Excel file not found."))

        sheet = (self.parameterAsString(parameters, self.PARAM_SHEET, context) or "").strip()
        email = (self.parameterAsString(parameters, self.PARAM_EMAIL, context) or "").strip() or "you@example.com"

        min_delay = self.DEFAULT_MIN_DELAY

        out_crs = self.parameterAsCrs(parameters, self.PARAM_OUT_CRS, context)
        out_epsg = out_crs.authid() or "EPSG:25833"

        out_shp  = ""  # SHP mirror retired; GPKG is the single output format
        out_gpkg = self.parameterAsFileOutput(parameters, self.PARAM_OUT_GPKG, context) or ""

        include_all = self.DEFAULT_INCLUDE_ALL
        force_live  = self.DEFAULT_FORCE_LIVE
        thin_export = self.parameterAsBool(parameters, self.PARAM_THIN_EXPORT, context)

        addr_prefix = self.DEFAULT_ADDR_PREFIX

        # --- Load Excel (tolerant sheet handling)
        try:
            xf = pd.ExcelFile(excel)
            if sheet:
                if sheet not in xf.sheet_names:
                    feedback.pushInfo(self.tr(f"Sheet '{sheet}' not found. Falling back to first sheet '{xf.sheet_names[0]}'."))
                    df = xf.parse(0)
                else:
                    df = xf.parse(sheet)
            else:
                df = xf.parse(0)
        except Exception as e:
            raise QgsProcessingException(self.tr(f"Failed to read Excel: {e}"))

        # Promote header row if needed; drop all-empty rows
        df = fix_header_row(df).dropna(how="all").reset_index(drop=True)

        # --- Column mapping & canonical fields
        mapping = autodetect_mapping(df)

        # Ensure HH exists (canonical households field)
        ensure_households_column(df, mapping, out_name="HH")

        # Ensure ADDR_ID exists and generate missing values using shared helper
        if "ADDR_ID" not in df.columns:
            df["ADDR_ID"] = ""
        generate_addr_ids(df, addr_prefix)

        # --- Geocoding (respect force_live flag)
        cache_base = (
            os.path.dirname(os.path.abspath(out_gpkg))
            if out_gpkg else
            os.path.dirname(os.path.abspath(out_shp))
            if out_shp else
            os.path.dirname(os.path.abspath(excel))
        )
        cache_dir = os.path.join(cache_base, ".hldplanning_cache")
        if force_live:
            feedback.pushInfo("Force live geocoding enabled; geocode cache will be bypassed.")
        else:
            feedback.pushInfo(f"Geocode cache: {cache_dir}")

        client = NominatimClient(
            email=email,
            cache_dir=cache_dir,
            use_cache=not force_live,
            min_delay=min_delay
        )

        lats, lons, statuses, sources, queries = [], [], [], [], []
        geometries = []
        source_crs_values = []
        cmap = autodetect_mapping(df)  # refresh (we may have added ADDR_ID)

        xcol = cmap.get("longitude")
        ycol = cmap.get("latitude")


        feedback.pushInfo(f"Mapping: {cmap}; xcol={xcol}, ycol={ycol}")

        total = len(df)
        for idx, row in df.iterrows():
            if feedback.isCanceled():
                break

            # Prefer valid coordinates from sheet if present (strict guard)
            from_sheet = False
            if xcol and ycol and (xcol in df.columns) and (ycol in df.columns):
                xv = row.get(xcol); yv = row.get(ycol)
                if pd.notna(xv) and pd.notna(yv) and str(xv).strip() and str(yv).strip():
                    try:
                        xv_f = float(xv); yv_f = float(yv)
                    except Exception:
                        xv_f = yv_f = None
                    if xv_f is not None and yv_f is not None:
                        if _valid_xy(xv_f, yv_f):
                            # Values fit lat/lon range: treat as WGS84 directly.
                            attr_lon, attr_lat = xv_f, yv_f
                            sources.append("sheet_lonlat")
                            geometries.append(Point(xv_f, yv_f) if Point else None)
                            source_crs_values.append("EPSG:4326")
                        else:
                            # Values look projected: the GEOMETRY column keeps
                            # the original projected coordinates (the mixed-CRS
                            # split logic below treats them as already being in
                            # out_epsg).  The LATITUDE/LONGITUDE attribute
                            # columns are reprojected to WGS84 so they carry
                            # degrees rather than metres.
                            attr_lon, attr_lat = _projected_to_wgs84(
                                xv_f, yv_f, out_epsg
                            )
                            if attr_lon is None or attr_lat is None:
                                feedback.pushWarning(self.tr(
                                    f"Could not reproject row {idx} "
                                    f"({xv_f:.2f}, {yv_f:.2f}) from "
                                    f"{out_epsg} to WGS84; keeping projected "
                                    f"values in LAT/LON attributes."
                                ))
                                attr_lon, attr_lat = xv_f, yv_f
                            sources.append("sheet_xy")
                            geometries.append(Point(xv_f, yv_f) if Point else None)
                            source_crs_values.append(out_epsg)
                        lons.append(attr_lon); lats.append(attr_lat)
                        statuses.append("ok"); queries.append("from_sheet")
                        from_sheet = True

            if from_sheet:
                if (idx + 1) % 25 == 0:
                    feedback.pushInfo(f"Processed {idx+1}/{total} rows…")
                continue

            # Build structured query
            structured = build_structured_query(row, cmap, {"country": "Germany"})

            # Ensure housenumber in street text for Nominatim accuracy
            hnr = (structured.get("housenumber") or "").strip()
            street_base = (structured.get("street") or "").strip()
            if street_base and hnr and hnr not in street_base.split():
                structured["street"] = f"{street_base} {hnr}".strip()

            # If city is empty but district present, use it as fallback
            if not structured.get("city") and structured.get("district"):
                structured["city"] = structured["district"]

            street = structured.get("street","")
            city   = structured.get("city","")
            plz    = structured.get("postalcode","")
            country= structured.get("country","Germany")

            attempts = []
            def do(q):
                q2 = dict(q)
                if force_live and q2.get("street"):
                    q2["street"] = q2["street"] + " "  # minimal cache-buster
                try:
                    lon3, lat3, raw, from_cache = client.geocode(q2)
                except Exception:
                    lon3, lat3, from_cache = (None, None, False)
                attempts.append({**q2, "_from_cache": bool(from_cache)})
                return lon3, lat3, from_cache

            # primary attempt
            lon2, lat2, was_cache = do({"street": street, "city": city, "postalcode": plz, "country": country})
            if not (lat2 and lon2):
                # fallbacks
                for cand in (
                    {"street": street, "city": city, "postalcode": "",   "country": country},
                    {"street": street, "city": "",    "postalcode": plz, "country": country},
                    {"street": "",     "city": city,  "postalcode": plz, "country": country},
                ):
                    lon2, lat2, was_cache = do(cand)
                    if lat2 and lon2:
                        break

            if lat2 and lon2:
                lats.append(lat2); lons.append(lon2); statuses.append("ok")
                sources.append("nominatim_cache" if was_cache else "nominatim_live")
                geometries.append(Point(float(lon2), float(lat2)) if Point else None)
                source_crs_values.append("EPSG:4326")
            else:
                lats.append(None); lons.append(None); statuses.append("not_found")
                sources.append("nominatim_live" if force_live else "nominatim")
                geometries.append(Point(0.0, 85.0) if include_all and Point else None)
                source_crs_values.append("EPSG:4326")

            queries.append(json.dumps(attempts, ensure_ascii=False))

            if (idx + 1) % 25 == 0:
                ok_so_far = sum(1 for s in statuses if s == "ok")
                feedback.pushInfo(f"Geocoded {idx+1}/{total} rows; OK so far: {ok_so_far}")

        # Canonical output columns
        df[FIELD.LAT]            = lats
        df[FIELD.LON]            = lons
        df[FIELD.GEOCODE_STATUS] = statuses
        df[FIELD.GEOCODE_SOURCE] = sources
        df[FIELD.GEOCODE_Q]      = queries
        df[COMMON_FIELDS.STAGE]  = "object"
        if FIELD.ADDR_ID in df.columns:
            df[COMMON_FIELDS.SRC_ID] = df[FIELD.ADDR_ID].astype(str)
        else:
            df[COMMON_FIELDS.SRC_ID] = (df.index + 1).astype(str)

        # --- Build GeoDataFrame + write
        if not _GEO_OK:
            raise QgsProcessingException(self.tr(
                "This algorithm requires GeoPandas/Shapely to write outputs. "
                "Please install them in your QGIS Python environment."
            ))

        # Build GeoDataFrame. Sheet lon/lat and Nominatim rows are WGS84;
        # projected sheet X/Y rows are treated as already being in OUT_CRS.
        if any(crs == out_epsg for crs in source_crs_values):
            parts = []
            for src_crs in sorted(set(source_crs_values)):
                idxs = [i for i, crs in enumerate(source_crs_values) if crs == src_crs]
                part_df = df.iloc[idxs].copy()
                part_geoms = [geometries[i] for i in idxs]
                part = gpd.GeoDataFrame(part_df, geometry=part_geoms, crs=src_crs)
                if src_crs != out_epsg:
                    part = part.to_crs(out_epsg)
                parts.append(part)
            gdf = pd.concat(parts).sort_index()
            gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=out_epsg)
        else:
            gdf = gpd.GeoDataFrame(df, geometry=geometries, crs="EPSG:4326")

        # keep all or drop missing geometries
        gdf_ok = gdf.copy() if include_all else gdf[gdf["geometry"].notna()].copy()

        if thin_export:
            thin_keep = [
                FIELD.ADDR_ID,
                "HH",
                COMMON_FIELDS.POLYGON_ID,
                COMMON_FIELDS.PDP_ID,
                FIELD.LAT,
                FIELD.LON,
                FIELD.GEOCODE_STATUS,
                FIELD.GEOCODE_SOURCE,
                COMMON_FIELDS.SRC_ID,
                COMMON_FIELDS.STAGE,
            ]
            thin_keep += [c for c in ("Address", "street", "city", "postcode", "house number") if c in gdf_ok.columns]
            thin_keep = [c for c in thin_keep if c in gdf_ok.columns]
            if thin_keep:
                gdf_ok = gdf_ok[thin_keep + ["geometry"]].copy()

        # separate reporting for not-found (status-based, independent of include_all)
        not_found_mask = df[FIELD.GEOCODE_STATUS].ne("ok")
        df_not_found = df.loc[not_found_mask].copy()

        # reproject to requested output CRS
        if str(gdf_ok.crs) != out_epsg:
            try:
                gdf_ok = gdf_ok.to_crs(out_epsg)
            except Exception as e:
                raise QgsProcessingException(self.tr(f"Failed to reproject to {out_epsg}: {e}"))

        # Write outputs via shared utility (handles shapefile quirks & ASCII fallback)
        written = write_vector_geopandas(
            gdf_ok,
            shp_path=out_shp or None,
            gpkg_path=out_gpkg or None,
            layer=LAYERNAMES.OBJECT,
            feedback=feedback
        )
        # Only keep paths for files that were ACTUALLY written.  The previous
        # `... or out_gpkg` fallback silently masked write failures by reporting
        # the user-supplied path even when nothing was generated, which made
        # the algorithm LOOK as if it had produced an output file.
        out_shp  = written.get("shp",  "")
        out_gpkg = written.get("gpkg", "")

        if not out_shp and not out_gpkg:
            raise QgsProcessingException(self.tr(
                "No output file was generated: both Shapefile and GeoPackage "
                "writes failed. See the log panel for the underlying error."
            ))

        # Not-found CSV: derive stem from preferred output (GPKG, else SHP)
        stem_for_csv = (os.path.splitext(out_gpkg)[0] if out_gpkg
                        else os.path.splitext(out_shp)[0] if out_shp
                        else "")
        err_csv = (stem_for_csv + "_not_found.csv") if stem_for_csv else ""
        if len(df_not_found) and err_csv:
            try:
                df_not_found.to_csv(err_csv, index=False)
            except Exception as e:
                feedback.reportError(self.tr(f"Failed to write not-found CSV: {e}"), fatalError=False)
                err_csv = ""

        # Style & add-to-project: prefer the GPKG
        target_for_qml = out_gpkg or out_shp
        qml_path = ""
        if target_for_qml:
            try:
                if out_gpkg:
                    # Make sure we open the intended sublayer inside the GPKG
                    uri = f"{out_gpkg}|layername={LAYERNAMES.OBJECT}"
                else:
                    uri = out_shp
        
                vlayer = QgsVectorLayer(uri, os.path.basename(target_for_qml), "ogr")
                qml_path = os.path.splitext(target_for_qml)[0] + ".qml"
        
                if vlayer and vlayer.isValid():
                    if apply_hh_symbology:
                        try:
                            apply_hh_symbology(vlayer)     # optional custom HH styling
                            vlayer.saveNamedStyle(qml_path)
                        except Exception:
                            pass
                    else:
                        try:
                            apply_qml_if_exists(vlayer, "01_object_layer.qml", feedback)
                            vlayer.saveNamedStyle(qml_path)
                        except Exception:
                            pass
                    # File destinations don't auto-load into the ToC; we have
                    # to add the layer to the project explicitly so the user
                    # actually sees the result on the map canvas.
                    try:
                        if vlayer.isValid():
                            QgsProject.instance().addMapLayer(vlayer)
                    except Exception:
                        pass
            except Exception as e:
                feedback.reportError(self.tr(f"QML styling step failed: {e}"), fatalError=False)
        

        # status-based totals (independent of include_all)
        count_ok = int((df[FIELD.GEOCODE_STATUS] == "ok").sum())
        count_not_found = int((df[FIELD.GEOCODE_STATUS] != "ok").sum())

        # Return keys MUST match declared parameter/output names exactly so
        # QGIS can resolve output files and load them into the map.
        return {
            self.PARAM_OUT_GPKG:     out_gpkg or "",
            "NOT_FOUND":             err_csv if count_not_found else "",
            "COUNT_OK":              count_ok,
            "COUNT_NOT_FOUND":       count_not_found,
            "QML":                   qml_path if os.path.exists(qml_path) else "",
        }
