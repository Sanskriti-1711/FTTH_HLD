# utils/ogr_gdal.py
# Minimal wrapper; OneClick can call these. Tries GDAL alg first, then python GDAL, then QuickOSM.
import os
from qgis.core import QgsProcessing, QgsApplication
from qgis import processing

def _alg_exists(alg_id: str) -> bool:
    try:
        return QgsApplication.processingRegistry().algorithmById(alg_id) is not None
    except Exception:
        return False

def extract_osm_lines_to(pbf_path: str, out_gpkg: str, context, feedback) -> str:
    """
    Extract OSM 'lines' where highway IS NOT NULL into out_gpkg.
    Returns path or raises on failure.
    """
    if _alg_exists("gdal:vectortranslate"):
        processing.run("gdal:vectortranslate", {
            "INPUT": pbf_path, "OUTPUT": out_gpkg,
            "LAYER_NAME": "lines",
            "SQL": "SELECT * FROM lines WHERE highway IS NOT NULL"
        }, context=context, feedback=feedback, is_child_algorithm=True)
        return out_gpkg
    raise Exception("No GDAL VectorTranslate; add a python-gdal or QuickOSM branch here if needed.")

def extract_osm_buildings_to(pbf_path: str, out_gpkg: str, context, feedback) -> str:
    if _alg_exists("gdal:vectortranslate"):
        processing.run("gdal:vectortranslate", {
            "INPUT": pbf_path, "OUTPUT": out_gpkg,
            "LAYER_NAME": "multipolygons",
            "SQL": "SELECT * FROM multipolygons WHERE building IS NOT NULL"
        }, context=context, feedback=feedback, is_child_algorithm=True)
        return out_gpkg
    raise Exception("No GDAL VectorTranslate; add a python-gdal or QuickOSM branch here if needed.")
