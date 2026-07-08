# -*- coding: utf-8 -*-
"""
utils.style_utils
Color helpers and renderer setup for FTTH layers
"""

import os
from qgis.core import (
    QgsVectorLayer,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsSymbol,
    QgsRendererCategory,
    QgsCategorizedSymbolRenderer,
    QgsProject,
)
from qgis.PyQt.QtGui import QColor

# ----------------------------------------------------------------------
# Universal color palettes
# ----------------------------------------------------------------------
TRUNK_COLOR = "#808080"
BASE_COLORS = [
    "#d81b60",  # pink/red
    "#1e88e5",  # blue
    "#43a047",  # green
    "#8e24aa",  # purple
    "#fb8c00",  # orange
    "#00897b",  # teal
    "#5e35b1",  # violet
]

# Distribution-duct side palettes (per FTTH spec)
DISTRIB_LEFT_COLORS  = ["#e53935", "#43a047"]  # red, green
DISTRIB_RIGHT_COLORS = ["#1e88e5", "#fdd835"]  # blue, yellow


# ----------------------------------------------------------------------
# Generic renderers and helpers
# ----------------------------------------------------------------------
def apply_outline_blue(vector_path: str, feedback=None, width_mm: float = 0.8):
    """Apply blue outline to polygon layers and save sidecar QML."""
    try:
        v = QgsVectorLayer(vector_path, "tmp_poly_style", "ogr")
        if v and v.isValid():
            sym = QgsFillSymbol.createSimple({
                'style': 'no', 'color': '0,0,0,0',
                'outline_style': 'solid',
                'outline_color': '30,107,170,255',
                'outline_width': str(width_mm),
                'outline_width_unit': 'MM'
            })
            v.setRenderer(QgsSingleSymbolRenderer(sym))
            side_qml = os.path.splitext(vector_path)[0] + ".qml"
            v.saveNamedStyle(side_qml)
            if feedback:
                feedback.pushInfo("Saved outline-blue style sidecar.")
    except Exception:
        pass


def apply_qml_if_exists(layer_or_path, qml_name: str, feedback=None):
    """
    Looks for ../resources/qml/<qml_name> and loads it to the given layer/path.
    """
    try:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        qml = os.path.normpath(os.path.join(base_dir, "resources", "qml", qml_name))
        if not os.path.exists(qml):
            return
        if isinstance(layer_or_path, QgsVectorLayer):
            lyr = layer_or_path
            if lyr.isValid():
                lyr.loadNamedStyle(qml)
                lyr.triggerRepaint()
                return
        path = str(layer_or_path)
        v = QgsVectorLayer(path, os.path.basename(path), "ogr")
        if v and v.isValid():
            v.loadNamedStyle(qml)
            side_qml = os.path.splitext(path)[0] + ".qml"
            try:
                v.saveNamedStyle(side_qml)
            except Exception:
                pass
    except Exception:
        pass


def colorize_mem_line_layer(layer, hexcolor: str, width: float = 0.8):
    """Best-effort styling for in-memory line layers."""
    if not layer:
        return
    try:
        from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer, QgsSingleSymbolRenderer
        from qgis.PyQt.QtGui import QColor

        # Create a simple line symbol layer with the given color and width
        line_symbol = QgsLineSymbol()
        simple_line = QgsSimpleLineSymbolLayer()
        simple_line.setColor(QColor(hexcolor))
        simple_line.setWidth(width)

        # Apply the simple line layer to the symbol
        line_symbol.changeSymbolLayer(0, simple_line)

        # Wrap it in a single-symbol renderer and apply to the layer
        renderer = QgsSingleSymbolRenderer(line_symbol)
        layer.setRenderer(renderer)

        # Force a visual refresh
        layer.triggerRepaint()

    except Exception:
        # Styling is best-effort; never let it break the main algorithm
        pass



def colorize_mem_poly_outline(layer, outline_rgba: str = "30,107,170,255", width_mm: float = 0.8):
    """Outline-only style for in-memory polygon layers."""
    if not layer:
        return
    try:
        r, g, b, a = [int(x) for x in outline_rgba.split(",")]
        sym = QgsFillSymbol.createSimple({
            'style': 'no', 'color': '0,0,0,0',
            'outline_style': 'solid',
            'outline_color': f'{r},{g},{b},{a}',
            'outline_width': str(width_mm),
            'outline_width_unit': 'MM'
        })
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()
    except Exception:
        pass


def apply_simple_line_style(layer, hexcolor: str, width_mm: float = 0.8):
    """Best-effort single-symbol line styling."""
    try:
        if not (layer and layer.isValid()):
            return
        from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer
        sym = QgsLineSymbol()
        base = QgsSimpleLineSymbolLayer()
        base.setColor(QColor(hexcolor))
        base.setWidth(width_mm)
        sym.changeSymbolLayer(0, base)
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Color logic for ducts
# ----------------------------------------------------------------------
def color_for_index(i: int) -> str:
    """Return consistent color code for branch index (used by Feeder ducts)."""
    if i < len(BASE_COLORS):
        return BASE_COLORS[i]
    hue = int((i * 137) % 360)
    return QColor.fromHsv(hue, 160, 230).name()


def distribution_color(side: str, idx: int) -> str:
    """
    Return color for Distribution ducts based on side and group index.
    Left → red→green; Right → blue→yellow; cycles if more than 2 groups.
    """
    side = (side or "").upper()
    palette = DISTRIB_LEFT_COLORS if side.startswith("L") else DISTRIB_RIGHT_COLORS
    if not palette:
        return "#999999"
    return palette[idx % len(palette)]


# ----------------------------------------------------------------------
# Categorized color renderer for any layer
# ----------------------------------------------------------------------
def apply_color_renderer(layer_path_or_obj, layer_name="Layer", attr="color"):
    """Applies categorized color renderer to a vector layer."""
    try:
        if isinstance(layer_path_or_obj, str):
            layer = QgsVectorLayer(layer_path_or_obj, layer_name, "ogr")
        else:
            layer = layer_path_or_obj
        if not layer or not layer.isValid():
            return
        cats = []
        vals = sorted({f[attr] for f in layer.getFeatures() if f[attr]})
        for c in vals:
            sym = QgsSymbol.defaultSymbol(layer.geometryType())
            try:
                sym.setWidth(0.9)
            except Exception:
                pass
            sym.setColor(QColor(c))
            cats.append(QgsRendererCategory(c, sym, str(c)))
        renderer = QgsCategorizedSymbolRenderer(attr, cats)
        layer.setRenderer(renderer)
        QgsProject.instance().addMapLayer(layer)
    except Exception as e:
        print(f"[apply_color_renderer] Error: {e}")
