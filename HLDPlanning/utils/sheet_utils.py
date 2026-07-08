# utils/sheet_utils.py
import re
import pandas as pd

# Shared header alias map (can be overridden/extended per vendor if needed)
EXPECTED_MAP = {
    "address":      ["Address","Adresse","Straße","Strasse","street","Stra\u00dfe","Standort"],
    "house_number": ["Housenumber","house number","house numb","Hausnummer","hnr"],
    "city":         ["City","Ort","Stadt","city"],
    "postcode":     ["Postcode","PLZ","postal code","Zip","postal cod"],
    "country":      ["Country","Land","country"],
    "district":     ["District","Ortsteil","Bezirk","borough"],
    "household":    ["HH","HHS","HOUSEHOLDS","HOUSEHOLD","HOUSEHOLD_S","WE","WE_anzahl","Wohneinheiten","No. of HH","Anzahl WE"],
    "addr_id":      ["ADDR_ID","Adress_ID","Address ID","Adress ID","Address_ID"],
    "latitude":     ["LATITUDE","latitude","Lat","Y","y","Y_COORD","YCOORD","POINT_Y","northing","NORTHING"],
    "longitude":    ["LONGITUDE","longitude","Lon","Lng","X","x","X_COORD","XCOORD","POINT_X","easting","EASTING"],
}

def fix_header_row(df: pd.DataFrame) -> pd.DataFrame:
    unnamed_ratio = sum(1 for c in df.columns if str(c).startswith("Unnamed")) / max(1, len(df.columns))
    if unnamed_ratio > 0.5 and len(df) > 0:
        first = df.iloc[0].fillna("")
        header_candidates = "|".join([
            "Address","Adresse","Straße","Strasse","City","Ort","PLZ","Postcode",
            "Housenumber","Hausnummer","ADDR","Adress","Zip","postal"
        ])
        if any(re.search(header_candidates, str(v), flags=re.IGNORECASE) for v in first.values):
            df2 = df[1:].copy()
            df2.columns = [str(v).strip() if str(v).strip() != "" else f"col_{i}" for i, v in enumerate(first.values)]
            return df2
    return df

def autodetect_mapping(df: pd.DataFrame) -> dict:
    mapping = {}
    cols = [str(c) for c in df.columns]
    lower = {c.lower(): c for c in cols}
    for key, options in EXPECTED_MAP.items():
        for name in options:
            lc = name.lower()
            if lc in lower:
                mapping[key] = lower[lc]
                break

    # Coordinate exports commonly contain empty LATITUDE/LONGITUDE fields next
    # to populated projected X/Y fields. Select populated numeric aliases.
    for key in ("latitude", "longitude"):
        for name in EXPECTED_MAP[key]:
            col = lower.get(name.lower())
            if col and pd.to_numeric(df[col], errors="coerce").notna().any():
                mapping[key] = col
                break

    # Keep paired numeric X/Y columns even when they are projected coordinates.
    # Geographic bounds are only useful when one coordinate was detected alone.
    def _has_valid_numeric(series: pd.Series, kind: str) -> bool:
        ser = pd.to_numeric(series, errors="coerce").dropna()
        if ser.empty:
            return False
        return ser.between(-90, 90).any() if kind == "lat" else ser.between(-180, 180).any()

    lat_col = mapping.get("latitude")
    lon_col = mapping.get("longitude")
    if lat_col and lon_col and lat_col in df.columns and lon_col in df.columns:
        paired = pd.DataFrame({
            "x": pd.to_numeric(df[lon_col], errors="coerce"),
            "y": pd.to_numeric(df[lat_col], errors="coerce"),
        }).dropna()
        if paired.empty:
            mapping.pop("latitude", None)
            mapping.pop("longitude", None)
    else:
        if lat_col and lat_col in df.columns and not _has_valid_numeric(df[lat_col], "lat"):
            mapping.pop("latitude", None)
        if lon_col and lon_col in df.columns and not _has_valid_numeric(df[lon_col], "lon"):
            mapping.pop("longitude", None)
    return mapping

def ensure_households_column(df: pd.DataFrame, mapping: dict, out_name: str = "HH"):
    col = mapping.get("household")
    if col and col in df.columns:
        df[out_name] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    elif out_name not in df.columns:
        df[out_name] = 0

def generate_addr_ids(df: pd.DataFrame, prefix: str):
    if "ADDR_ID" not in df.columns:
        df["ADDR_ID"] = ""
    def _norm(s): return str(s).strip().lower() if pd.notna(s) else ""
    def _num(s):
        m = re.search(r"\d+", str(s))
        return int(m.group()) if m else 10**9

    street_col = next((c for c in df.columns if c.lower() in ("street","strasse","straße")), None)
    postal_col = next((c for c in df.columns if c.lower() in ("postal_cod","postcode","plz","zip")), None)
    hnum_col   = next((c for c in df.columns if c.lower() in ("house_numb","house_no","hnr","house_number")), None)

    df["_street_s"] = df[street_col].map(_norm) if street_col in df.columns else ""
    df["_postal_s"] = df[postal_col].astype(str) if postal_col in df.columns else ""
    df["_hnum_i"]   = df[hnum_col].map(_num)      if hnum_col   in df.columns else 10**9

    mask_blank = (
        df["ADDR_ID"].isna()
        | (df["ADDR_ID"].astype(str).str.strip() == "")
        | (df["ADDR_ID"].astype(str).str.lower().isin(["nan","none"]))
    )
    existing_nums = (
        df.loc[~mask_blank, "ADDR_ID"]
          .astype(str)
          .str.extract(rf"^{re.escape(prefix)}(\d+)$")[0]
          .dropna()
          .astype(int)
    )
    start_num = int(existing_nums.max()) + 1 if not existing_nums.empty else 1
    order_idx = df.loc[mask_blank].sort_values(["_street_s","_hnum_i","_postal_s"], na_position="last").index
    for i, idx in enumerate(order_idx, start=start_num):
        df.at[idx, "ADDR_ID"] = f"{prefix}{i:05d}"
    df.drop(columns=[c for c in ["_street_s","_postal_s","_hnum_i"] if c in df.columns], inplace=True)
