# ----------------------------------------------
# utils/pricing.py
# ----------------------------------------------
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

import pandas as pd
import numpy as np


# Heuristics used to locate the header row
HEADER_TOKENS = [
    "einheit", "unit", "preis", "ep", "amount", "position", "pos", "bezeichnung", "beschreibung",
]


_NUM_TOKEN_RE = re.compile(
    r"""
    (?P<sign>[-+()]?)
    \s*
    (?P<num>
        (?:\d{1,3}(?:[.,]\d{3})+|\d+)
        (?:[.,]\d+)?      # optional decimal part
    )
    """,
    re.VERBOSE,
)


def _coerce_price_cell(x) -> Optional[float]:
    """
    Robustly parse a price from messy strings:
      - Handles currency symbols, spaces, and mixed separators.
      - Handles negatives with leading '-' or surrounding parentheses.
      - Prefers the *last* numeric token if multiple are present (common when notes trail the number).
    Returns float or None.
    """
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.lower() in {"nan", "none", "-", "--"}:
        return None

    # Extract all numeric-looking tokens
    tokens = list(_NUM_TOKEN_RE.finditer(s))
    if not tokens:
        return None

    # Choose the last token; in practice this is most often the unit price field even with clutter before.
    m = tokens[-1]
    raw = m.group("num")

    # Normalize thousands/decimal separators:
    # If both '.' and ',' appear, assume the rightmost symbol is decimal separator; strip the other.
    if "." in raw and "," in raw:
        # Rightmost separator:
        last_sep_pos = max(raw.rfind(","), raw.rfind("."))
        dec_sep = raw[last_sep_pos]
        # Remove the other separator everywhere
        other = "," if dec_sep == "." else "."
        raw = raw.replace(other, "")
        # Make decimal separator '.'
        raw = raw.replace(",", ".")
    else:
        # Single-type separator; if it's a comma assume decimal comma
        raw = raw.replace(",", ".")

    # Detect negative via sign group or parentheses in the original string
    neg = m.group("sign") == "-" or ("(" in s and ")" in s)
    try:
        val = float(raw)
        return -val if neg else val
    except Exception:
        return None


def read_price_table(xlsx_path: str) -> pd.DataFrame:
    """
    Return DataFrame(code, desc, unit, unit_price) scanning all sheets and auto-detecting the header row.
    Picks the sheet with the highest count of priced rows.
    """
    if not xlsx_path or not os.path.exists(xlsx_path):
        return pd.DataFrame(columns=["code", "desc", "unit", "unit_price"])

    xl = pd.ExcelFile(xlsx_path)
    best_df: Optional[pd.DataFrame] = None
    best_priced_count = -1

    for sh in xl.sheet_names:
        try:
            raw = pd.read_excel(xlsx_path, sheet_name=sh, header=None, dtype=str)
        except Exception:
            continue

        header_row = None
        # Scan first 50 rows to guess header
        for i in range(min(50, len(raw))):
            row_vals = [str(x).strip().lower() for x in list(raw.iloc[i].fillna(""))]
            score = sum(any(tok in v for v in row_vals) for tok in HEADER_TOKENS)
            if score >= 2:
                header_row = i
                break
        if header_row is None:
            continue

        try:
            df = pd.read_excel(xlsx_path, sheet_name=sh, header=header_row)
        except Exception:
            continue

        # Normalize columns
        cols_map = {c: "" for c in df.columns}
        for c in df.columns:
            lc = str(c).strip().lower()
            if any(k in lc for k in ["pos", "position", "code", "nr"]):
                cols_map[c] = "code"
            elif any(k in lc for k in ["bezeich", "beschr", "item", "beschreibung", "desc"]):
                cols_map[c] = "desc"
            elif any(k in lc for k in ["einheit", "unit"]):
                cols_map[c] = "unit"
            elif any(k in lc for k in ["preis", "ep", "einheitspreis", "unit price"]):
                cols_map[c] = "unit_price"

        df = df.rename(columns=cols_map)
        keep_cols = [c for c in ["code", "desc", "unit", "unit_price"] if c in df.columns]
        if len(keep_cols) < 3:
            # Need at least code/desc/unit (price may be missing)
            continue

        sub = df[keep_cols].copy()

        # Clean types
        if "code" not in sub.columns:
            sub["code"] = ""
        if "desc" not in sub.columns:
            sub["desc"] = ""
        if "unit" not in sub.columns:
            sub["unit"] = ""
        if "unit_price" not in sub.columns:
            sub["unit_price"] = np.nan

        sub["code"] = sub["code"].fillna("").astype(str).str.strip()
        sub["desc"] = sub["desc"].fillna("").astype(str).str.strip()
        sub["unit"] = sub["unit"].fillna("").astype(str).str.strip()

        # Coerce price
        sub["unit_price"] = sub["unit_price"].apply(_coerce_price_cell)
        # Ensure float dtype
        sub["unit_price"] = pd.to_numeric(sub["unit_price"], errors="coerce")

        priced_count = int(sub["unit_price"].notna().sum())

        # Prefer sheets with more priced rows
        if priced_count > best_priced_count:
            best_df = sub
            best_priced_count = priced_count

    if best_df is None:
        return pd.DataFrame(columns=["code", "desc", "unit", "unit_price"])

    # Drop rows without description
    best_df = best_df[best_df["desc"].astype(str).str.strip() != ""].copy()

    # Final column order & dtypes
    for col in ["code", "desc", "unit", "unit_price"]:
        if col not in best_df.columns:
            best_df[col] = np.nan if col == "unit_price" else ""
    best_df = best_df[["code", "desc", "unit", "unit_price"]]
    best_df["unit_price"] = pd.to_numeric(best_df["unit_price"], errors="coerce")

    return best_df.reset_index(drop=True)


def match_prices(
    items_df: pd.DataFrame,
    price_df: pd.DataFrame,
    code_map: Optional[Dict[str, str]] = None,
    desc_hints: Optional[Dict[str, List[str]]] = None,
) -> pd.DataFrame:
    """
    Attach unit_price and amount to items_df.
    Expects columns: section, code, item, unit, quantity, notes  (item may instead be item_name).
    Returns a copy with 'unit_price' (float) and 'amount' (float).
    """
    res = items_df.copy()

    # Normalize expected columns
    if "item" not in res.columns and "item_name" in res.columns:
        res["item"] = res["item_name"]

    for col, default in [("section", ""), ("code", ""), ("item", ""), ("unit", ""), ("quantity", 0), ("notes", "")]:
        if col not in res.columns:
            res[col] = default

    # Init numeric columns
    res["unit_price"] = np.nan
    res["amount"] = np.nan

    if price_df is None or price_df.empty:
        return res

    # Build quick lookups
    price_df = price_df.copy()
    price_df["_desc_lc"] = price_df["desc"].astype(str).str.lower()
    price_df["_unit_lc"] = price_df["unit"].astype(str).str.strip().str.lower()
    price_by_code = {str(r["code"]).strip(): r for _, r in price_df.iterrows() if str(r["code"]).strip()}

    for idx, row in res.iterrows():
        name = str(row.get("item", "")).strip()
        unit = str(row.get("unit", "")).strip().lower()

        # 1) exact code mapping
        if code_map and name in code_map and code_map[name]:
            code = str(code_map[name]).strip()
            r = price_by_code.get(code)
            if r is not None and (not r.get("unit") or str(r.get("unit")).strip().lower() == unit):
                res.at[idx, "code"] = code
                res.at[idx, "unit_price"] = r.get("unit_price")
                continue  # priced, go next

        # 2) description hints filtering (AND across hints)
        cand = price_df
        hints = (desc_hints or {}).get(name, [])
        if hints:
            for h in hints:
                q = str(h).strip().lower()
                cand = cand[cand["_desc_lc"].str.contains(re.escape(q), na=False)]

        # 3) fallback: use first two informative words from item text (length >= 4)
        if cand.empty:
            words = [w for w in name.lower().split() if len(w) >= 4]
            cand = price_df
            for w in words[:2]:
                cand = cand[cand["_desc_lc"].str.contains(re.escape(w), na=False)]

        if not cand.empty:
            # Prefer matching unit if available
            u_match = cand[cand["_unit_lc"] == unit] if unit else pd.DataFrame()
            pick = u_match.iloc[0] if not u_match.empty else cand.iloc[0]

            res.at[idx, "code"] = str(pick.get("code") or "")
            res.at[idx, "unit_price"] = pick.get("unit_price")

    # Compute amount = quantity * unit_price
    def _mul(q, p):
        try:
            if p is None or (isinstance(p, float) and np.isnan(p)):
                return np.nan
            return round(float(q) * float(p), 2)
        except Exception:
            return np.nan

    res["amount"] = [_mul(q, p) for q, p in zip(res.get("quantity", []), res.get("unit_price", []))]

    # Ensure numeric dtype
    res["unit_price"] = pd.to_numeric(res["unit_price"], errors="coerce")
    res["amount"] = pd.to_numeric(res["amount"], errors="coerce")

    return res
