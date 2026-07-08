# -*- coding: utf-8 -*-
import re
from typing import Iterable, Optional
from qgis.core import QgsVectorLayer


def first_field_case_insensitive(layer: QgsVectorLayer, names):
    fields = [f.name() for f in layer.fields()]
    lower = {f.lower(): f for f in fields}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None

def swap_canonical_field(expr: str,
                         layer: QgsVectorLayer,
                         canonical: str = "fclass",
                         candidates = ("fclass", "highway", "class")) -> str:
    """
    Replace references to the canonical field with the actual field name on `layer`,
    handling quoted/unquoted forms WITHOUT creating double quotes.
    """
    if not expr:
        return expr

    actual = first_field_case_insensitive(layer, list(candidates))
    if not actual or actual.lower() == canonical.lower():
        return expr

    # replace "fclass" (quoted) -> "actual"
    out = re.sub(r'\"{}\s*\"'.format(re.escape(canonical)), f'"{actual}"', expr, flags=re.IGNORECASE)
    # replace bare fclass -> "actual"
    out = re.sub(r'\b{}\b'.format(re.escape(canonical)), f'"{actual}"', out, flags=re.IGNORECASE)
    return out

def expr_ci_in(fieldname: str, values: Iterable[str]) -> str:
    vals = ",".join([f"'{str(v).lower()}'" for v in values])
    return f'lower("{fieldname}") IN ({vals})'

def expr_area_not_true(layer: QgsVectorLayer) -> Optional[str]:
    """
    Returns an expression to filter out area polygons often encoded as area='T'.
    If no area-like field exists, returns None.
    """
    fname = first_field_case_insensitive(layer, ["area"])
    if not fname:
        return None
    # coalesce to 'F' then check <> 'T' (case-insensitive)
    return f"lower(coalesce(\"{fname}\",'f')) <> 't'"
