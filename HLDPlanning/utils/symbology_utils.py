# -*- coding: utf-8 -*-
"""
Apply manual HH-based symbology to a point layer.

- Robust HH field detection (handles Integer64/Decimal/Numeric, etc.).
- Works even if HH is stored as TEXT, via to_int() in rule expressions.
- Optional explicit NULL/unknown bucket (on by default).
- Same colors and 2 mm size, outline suppressed.

Tested on QGIS 3.44 (Qt 5.15), PyQGIS API.
"""

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsSymbol,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsUnitTypes,
)

# ---- Color palette ----
COLOR_0     = QColor(255, 255, 255)   # white
COLOR_1     = QColor( 68, 204,  53)   # green
COLOR_2     = QColor(250, 241,  59)   # yellow
COLOR_3_31  = QColor(139,  78, 218)   # violet
COLOR_32P   = QColor(255,   0,   0)   # red
COLOR_NULL  = QColor(200, 200, 200)   # light gray for NULL/unknown

POINT_SIZE_MM = 2.0                   # uniform point size


# ---------- Helpers ----------
def _pick_hh_field(layer: QgsVectorLayer) -> str:
    """Pick the best field to represent households (HH)."""
    fields = layer.fields()
    names = [f.name() for f in fields]
    if not names:
        return "HH"

    # 1) exact 'HH'
    for n in names:
        if n.lower() == "hh":
            return n

    # 2) common variants
    for cand in ("households", "anz_hh", "anzahl_hh", "hh_count"):
        for n in names:
            if n.lower() == cand:
                return n

    # 3) any numeric field
    for f in fields:
        try:
            if hasattr(f, "isNumeric") and f.isNumeric():
                return f.name()
        except Exception:
            pass
        t = (f.typeName() or "").lower()
        if any(k in t for k in (
            "int", "integer", "integer64", "real", "double", "float",
            "decimal", "numeric", "number"
        )):
            return f.name()

    # 4) last resort: first field
    return names[0]


def _make_point_symbol(color: QColor) -> QgsSymbol:
    """Create a point symbol with fill color, no outline, fixed size in mm."""
    sym = QgsSymbol.defaultSymbol(QgsWkbTypes.PointGeometry)
    sym.setColor(color)
    sym.setSize(POINT_SIZE_MM)
    sym.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    sl = sym.symbolLayer(0)
    if hasattr(sl, "setOutlineColor"):
        sl.setOutlineColor(QColor(0, 0, 0, 0))
    if hasattr(sl, "setOutlineWidth"):
        sl.setOutlineWidth(0)
    return sym


# ---------- Main entry ----------
def apply_hh_symbology(layer: QgsVectorLayer, include_null_bucket: bool = True) -> None:
    """
    Apply manual color scheme by HH counts using rule-based expressions.
    - Robust to HH being TEXT; coerces via to_int().
    - Adds optional NULL/unknown bucket.
    """
    if not isinstance(layer, QgsVectorLayer):
        return
    if layer.geometryType() != QgsWkbTypes.PointGeometry:
        return

    hh_field = _pick_hh_field(layer)

    # Safe expression: coalesce to sentinel when NULL/non-numeric
    val_expr = f'coalesce(to_int("{hh_field}"), -999999)'

    # Root rule
    root = QgsRuleBasedRenderer.Rule(None)

    # Helper to add a rule (constructor in QGIS 3.44 does NOT take keyword args)
    def add_rule(color: QColor, expr: str, label: str):
        r = QgsRuleBasedRenderer.Rule(_make_point_symbol(color))
        r.setFilterExpression(expr)
        r.setLabel(label)
        root.appendChild(r)

    if include_null_bucket:
        add_rule(COLOR_NULL, f'{val_expr} = -999999', "Unknown/NULL HH")

    add_rule(COLOR_0,    f'{val_expr} = 0',                       "0 HH")
    add_rule(COLOR_1,    f'{val_expr} = 1',                       "1 HH")
    add_rule(COLOR_2,    f'{val_expr} = 2',                       "2 HH")
    add_rule(COLOR_3_31, f'{val_expr} >= 3 AND {val_expr} <= 31', "3 ≤ HH ≤ 31")
    add_rule(COLOR_32P,  f'{val_expr} >= 32',                     "HH ≥ 32")

    renderer = QgsRuleBasedRenderer(root)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


# ---------- Optional convenience ----------
def apply_to_active_layer(include_null_bucket: bool = True) -> None:
    """Apply to the current active layer in QGIS."""
    from qgis.core import QgsProject
    lyr = QgsProject.instance().layerTreeRoot().currentLayer()
    if isinstance(lyr, QgsVectorLayer):
        apply_hh_symbology(lyr, include_null_bucket=include_null_bucket)
