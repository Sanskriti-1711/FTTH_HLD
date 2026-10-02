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
    "household":    ["households","HH","HHS","HOUSEHOLDS","HOUSEHOLD","HOUSEHOLD_S","WE","WE_anzahl","Wohneinheiten","No. of HH","Anzahl WE"],
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

def ensure_households_column(df: pd.DataFrame, mapping: dict, out_name: str = "households"):
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
    hh_col: str = "households",
    method_col: str = "household_method",
    drop_source: bool = True,
) -> pd.DataFrame:
    """State each building's household total on every object row.

    The object layer writes ONE ROW PER PREMISE (a physical service location),
    and the pipeline's own stages need one row per location.  The HOUSEHOLD
    COUNT, however, belongs to the building: a block with five addresses is one
    building with five homes, not five one-home buildings.  So the per-location
    spread is collapsed into the building total and every row of that building
    carries it:

      households        the building's homes (its whole total, on every row)
      premises          how many service locations that building became
      household_method  the single method, or ``mixed(a,b)`` when they differ

    The old per-location column (`HH`) is dropped, so `households` is the one
    place a household number lives.  Because the total repeats across a
    building's rows, a consumer that sums the column per row would count a
    five-address block five times -- `households_by_object` is the helper that
    counts each building once, and the pipeline's demand stages use it.

    Building identity is ``building_col`` (OSM_ID -- the source building the
    premise came from).  A row with no identity is its own object: pooling it
    with unrelated rows would invent a count for a building we cannot name.
    """
    # Drop only what this call recomputes.  `households` is the SOURCE here (the
    # object layer hands it over already renamed), so it is never dropped.
    df.drop(
        columns=[c for c in HOUSEHOLD_AGGREGATE_COLUMNS
                 if c != "households" and c in df.columns],
        inplace=True,
    )
    if df.empty:
        df["households"] = pd.Series([], dtype="int64", index=df.index)
        df["premises"] = pd.Series([], dtype="int64", index=df.index)
        df["household_method"] = pd.Series([], dtype="object", index=df.index)
        return df

    # Accept the legacy `HH` / `HH_METHOD` spellings too, so an older workbook or
    # a frame written before the rename still produces the aggregates.
    hh_src = hh_col if hh_col in df.columns else "HH"
    method_src = method_col if method_col in df.columns else "HH_METHOD"
    hh = (
        pd.to_numeric(df[hh_src], errors="coerce").fillna(0)
        if hh_src in df.columns else pd.Series(1, index=df.index)
    )
    method = (
        df[method_src].fillna("").astype(str)
        if method_src in df.columns else pd.Series("", index=df.index)
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
    if drop_source:
        # Only the LEGACY per-location columns go; the canonical output columns
        # are the ones just written above.
        df.drop(
            columns=[c for c in (hh_src, method_src)
                     if c in df.columns and c not in HOUSEHOLD_AGGREGATE_COLUMNS],
            inplace=True,
        )
    return df


def households_by_object(
    rows,
    *,
    hh_key: str = "households",
    object_key: str = "OSM_ID",
    fallback_key: str = "ADDR_ID",
) -> dict:
    """{object identity: homes}, counting each building once.

    `households` repeats a building's total on each of its rows, so a plain sum
    over the rows inflates a multi-address block.  Every stage that sizes
    something from homes -- polygon growth and clubbing, the splitter plan, the
    per-PDP threshold -- has to ask this instead of adding the column up.

    Identity is the building (`object_key`); a row with none falls back to its
    own id, then to its position, so it is still counted exactly once.
    """
    out: dict = {}
    for i, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        try:
            homes = int(float(row.get(hh_key) or 0))
        except (TypeError, ValueError):
            homes = 0
        ident = row.get(object_key)
        if ident in (None, ""):
            ident = row.get(fallback_key)
        if ident in (None, ""):
            ident = f"__row_{i}"
        # The first row of a building carries the same total as the rest, so
        # whichever one lands first sets the value; later rows are the same
        # building and must not add to it.
        out.setdefault(str(ident), homes)
    return out


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
