# utils/file_io.py
import os
import unicodedata as _ud
from typing import Optional, Dict
import re
import shutil

from qgis.core import (
        QgsVectorLayer, QgsVectorFileWriter, QgsCoordinateTransformContext,
        QgsWkbTypes, QgsFields, QgsProject
    )
from qgis import processing

# ---------------------------------------------------------------------
# Safe delete utilities
# ---------------------------------------------------------------------
def delete_path(path: str) -> None:
    """
    Delete any file or directory path safely (no error if missing).
    """
    try:
        if not path:
            return
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def delete_shapefile(shp_path: str) -> None:
    """
    Delete a Shapefile and its sidecar files (.shx, .dbf, .prj, .cpg, etc.).
    """
    try:
        if not shp_path or not shp_path.lower().endswith(".shp"):
            return
        base = os.path.splitext(shp_path)[0]
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbx", ".sbn"):
            f = base + ext
            if os.path.exists(f):
                os.remove(f)
    except Exception:
        pass


# ---------------------------------------------------------------------
# URI and layername helpers
# ---------------------------------------------------------------------
def _safe_layername(name: str) -> str:
    """
    Sanitize layer names for safe storage inside GPKG / OGR drivers.
    """
    if not name:
        return "layer"
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", str(name))
    clean = clean.strip("_")
    if not clean:
        clean = "layer"
    if len(clean) > 63:
        clean = clean[:63]
    return clean


def _strip_double_gpkg_layer(uri: str) -> str:
    """
    Collapse duplicated '|layername=' segments that sometimes appear
    when reusing URIs in nested processing runs.
    """
    if not uri:
        return uri
    parts = uri.split("|")
    seen_layer = False
    filtered = []
    for p in parts:
        if p.startswith("layername="):
            if seen_layer:
                continue
            seen_layer = True
        filtered.append(p)
    return "|".join(filtered)


def _normalize_ogr_uri(uri: str) -> str:
    """
    Normalize a GeoPackage-style OGR URI.
    Examples:
        'path.gpkg' → 'path.gpkg'
        'path.gpkg|layername=foo' → 'path.gpkg|layername=foo'
        'path.gpkg|layername=foo|layername=foo' → 'path.gpkg|layername=foo'
    """
    if not uri:
        return uri
    u = _strip_double_gpkg_layer(uri)
    u = u.replace("\\", "/").strip()
    return re.sub(r"\|+", "|", u)


def _ascii_transliterate_df(df):
    def deumlaut(s):
        if s is None: return s
        try:
            s = str(s)
            s = _ud.normalize("NFKD", s)
            return "".join(ch for ch in s if not _ud.combining(ch))
        except Exception:
            return s
    df = df.copy()
    for c in df.columns:
        if c != "geometry" and getattr(df[c], "dtype", None) == object:
            df[c] = df[c].map(deumlaut)
    return df

def write_vector_geopandas(gdf, shp_path: Optional[str] = None, gpkg_path: Optional[str] = None,
                           layer: str = "layer", feedback=None) -> Dict[str, str]:
    """
    Writes with UTF-8, falls back to ASCII transliteration for shapefile.
    Returns dict keys 'shp' and/or 'gpkg' ONLY for files that were actually written.
    Errors are reported to feedback but never propagated; callers can detect
    failure by inspecting which keys are missing from the returned dict.
    """
    out = {}
    # ----- Shapefile (UTF-8 with ASCII fallback; both writes are guarded) -----
    if shp_path:
        written = False
        try:
            os.makedirs(os.path.dirname(shp_path), exist_ok=True)
            gdf.to_file(shp_path, encoding="UTF-8")
            written = True
        except Exception as e:
            if feedback:
                feedback.reportError(
                    f"UTF-8 shapefile write failed ({e}); trying ASCII fallback.",
                    fatalError=False,
                )
            try:
                _ascii_transliterate_df(gdf).to_file(shp_path)
                written = True
            except Exception as e2:
                if feedback:
                    feedback.reportError(
                        f"Shapefile ASCII fallback also failed: {e2}",
                        fatalError=False,
                    )
        if written:
            out["shp"] = shp_path

    # ----- GeoPackage (single guarded write) -----
    if gpkg_path:
        try:
            os.makedirs(os.path.dirname(gpkg_path), exist_ok=True)
            gdf.to_file(gpkg_path, layer=layer, driver="GPKG")
            out["gpkg"] = gpkg_path
        except Exception as e:
            if feedback:
                feedback.reportError(f"GeoPackage write failed: {e}", fatalError=False)
    return out

# (Optional) Utilities used by OneClick as well — safe to import here


def unload_layers_with_source(vector_path: str):
    try:
        src_abs = os.path.abspath(vector_path)
        proj = QgsProject.instance()
        to_remove = []
        for lyr in proj.mapLayers().values():
            try:
                raw = lyr.source() if hasattr(lyr, "source") else ""
                base = raw.split("|", 1)[0]
                if os.path.abspath(base) == src_abs:
                    to_remove.append(lyr.id())
            except Exception:
                continue
        for lid in to_remove:
            proj.removeMapLayer(lid)
    except Exception:
        pass

def gpkg_best_sublayer(gpkg_path: str):
    """
    Returns 'path|layername=XXX' of first non-empty sublayer, or ''.
    """
    try:
        if not gpkg_path or not os.path.exists(gpkg_path):
            return ""
        probe = QgsVectorLayer(gpkg_path, "probe", "ogr")
        subs = probe.dataProvider().subLayers() or []
        def _open(name): return QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
        def _lname(e):  # "0:layername:geom?crs=..."
            parts = e.split(":", 2)
            return parts[1] if len(parts) >= 2 else ""
        cand = [_lname(s) for s in subs if s]
        for nm in cand:   # prefer points
            lyr = _open(nm)
            if lyr and lyr.isValid() and lyr.geometryType() == 0 and lyr.featureCount() > 0:
                return f"{gpkg_path}|layername={nm}"
        for nm in cand:   # any non-empty
            lyr = _open(nm)
            if lyr and lyr.isValid() and lyr.featureCount() > 0:
                return f"{gpkg_path}|layername={nm}"
    except Exception:
        pass
    return ""

def best_object_source(gpkg_path: str, shp_path: Optional[str] = None) -> str:
    src = gpkg_best_sublayer(gpkg_path)
    if src:
        return src
    if shp_path and os.path.exists(shp_path):
        lyr = QgsVectorLayer(shp_path, "object_shp", "ogr")
        if lyr and lyr.isValid() and lyr.featureCount() > 0:
            return shp_path
    return ""

def save_layer_to_gpkg_table(src_layer, gpkg_path: str, table_name: str, unload_source: bool = True):
    """
    Write features from `src_layer` to a sublayer `table_name` in `gpkg_path`.
    Returns QgsVectorFileWriter error code (QgsVectorFileWriter.NoError == 0).
    Headless-safe:
      * optionally unloads open handles to the gpkg first,
      * writes via a memory clone (stable field order),
      * CreateOrOverwriteLayer if gpkg exists, else CreateOrOverwriteFile.
    """


    if unload_source:
        try:
            unload_layers_with_source(gpkg_path)
        except Exception:
            pass

    if not isinstance(src_layer, QgsVectorLayer) or not src_layer.isValid():
        return QgsVectorFileWriter.ErrInvalidLayer

    # mem clone to stabilize
    mem_uri = f"Memory?geometry={QgsWkbTypes.displayString(src_layer.wkbType())}&crs={src_layer.sourceCrs().authid()}"
    mem = QgsVectorLayer(mem_uri, "memcpy", "memory")
    dp = mem.dataProvider()
    dp.addAttributes(list(src_layer.fields()))
    mem.updateFields()
    dp.addFeatures(list(src_layer.getFeatures()))
    mem.updateExtents()

    clean = re.sub(r"[^A-Za-z0-9_]+", "_", table_name).strip("_") or "layer"
    if clean.lower().endswith(".gpkg"):
        clean = clean[:-5]

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = clean
    opts.fileEncoding = "UTF-8"

    file_exists = os.path.exists(gpkg_path)
    try:
        opts.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteLayer if file_exists
            else QgsVectorFileWriter.CreateOrOverwriteFile
        )
    except AttributeError:
        # Older QGIS – emulate overwrite by deleting the file if it didn’t exist earlier
        if not file_exists and os.path.exists(gpkg_path):
            try: os.remove(gpkg_path)
            except Exception: pass

    err, _ = QgsVectorFileWriter.writeAsVectorFormatV2(mem, gpkg_path, QgsCoordinateTransformContext(), opts)
    del mem
    return err
