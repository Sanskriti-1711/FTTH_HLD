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

def household_method_label(values) -> str:
    """One label for the estimating methods behind a building's total.

    A building's premises can be counted by more than one method (one address
    node with `addr:flats`, the rest `fallback_one`), and the label says so
    rather than picking one: reporting `fallback_one` on a building whose total
    is really a mix would present a measured number as a guess and a guess as
    measured.  Mirrors `osm_source.household_method_label` so the pre-run review
    layer and the served object layer word a mixed building the same way.
    """
    unique = sorted({str(v).strip() for v in values if str(v or "").strip()})
    if not unique:
        return ""
    return unique[0] if len(unique) == 1 else "mixed(" + ",".join(unique) + ")"


# The household aggregate columns the object layer carries on every row.  Named
# lower-case so they read identically on the pre-run review layer and on the
# served design output.
HOUSEHOLD_AGGREGATE_COLUMNS = ("households", "premises", "household_method")


def add_household_aggregates(
    df: pd.DataFrame,
    *,
    building_col: str = "OSM_ID",
    hh_col: str = "HH",
    method_col: str = "HH_METHOD",
) -> pd.DataFrame:
    """Carry each building's household aggregate on its premises' rows.

    The object layer writes ONE ROW PER PREMISE, which is the right granularity
    for the design (every premise needs its own service entry) but the wrong one
    for reading a household count off the layer: a block that became five
    premises appears as five rows of one household, and a reader summing `HH` by
    eye can read a five-home block as five one-home buildings.  So every row also
    carries its building's aggregate:

      households        sum of HH over the premises that share the building
      premises          how many premises that building became
      household_method  the single HH_METHOD, or ``mixed(a,b)`` when they differ

    Building identity is ``building_col`` (OSM_ID -- the source building the
    premise came from).  A row with no identity is its own object: pooling it
    with unrelated rows would invent a household count for a building we cannot
    name, so its own HH is the honest answer.  When the frame has no building
    column at all, every row is treated that way and the schema still appears.
    """
    if df.empty:
        for col in HOUSEHOLD_AGGREGATE_COLUMNS:
            df[col] = pd.Series(pd.array([], dtype="int64" if col != "household_method" else "object"), index=df.index)
        return df

    hh = (
        pd.to_numeric(df[hh_col], errors="coerce").fillna(0)
        if hh_col in df.columns else pd.Series(0, index=df.index)
    )
    method = (
        df[method_col].fillna("").astype(str)
        if method_col in df.columns else pd.Series("", index=df.index)
    )

    own_row = pd.Series([f"__row_{i}" for i in df.index], index=df.index)
    if building_col in df.columns:
        key = df[building_col].astype("object")
        blank = key.isna() | (key.astype(str).str.strip() == "")
        key = key.where(~blank, own_row)
    else:
        key = own_row

    df["households"] = hh.groupby(key).transform("sum").astype(int)
    df["premises"] = hh.groupby(key).transform("size").astype(int)
    df["household_method"] = method.groupby(key).transform(household_method_label)
    return df


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
