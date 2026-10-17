# -*- coding: utf-8 -*-
"""Eurostat GISCO LAU: authoritative population per municipality.

Why this exists: for anything outside the UK there is no postcode-keyed dwelling
register to load, and the design falls back to the OSM household heuristic -- so
`tmp/hld_test_areas.md` records "Germany / Europe: no household register" and a
German project's household counts are guesses. Eurostat publishes a real,
free, population figure for every municipality in Europe, which is the one
authoritative number available for those countries.

What GISCO gives, and what it does NOT:

  * GISCO LAU is keyed on the MUNICIPALITY (LAU), and a LAU has no postcode.
    There is no postcode column to read, so LAU figures cannot be loaded into a
    postcode-keyed register directly -- they have to be apportioned onto
    postcode polygons (see `attribute_population`).
  * LAU publishes POPULATION (at 31 December), not dwellings and not households.
    The register's `households` column is a dwelling/household count, so a
    published average household size is needed to convert. That is an ESTIMATE
    and `attribute_population` says so in the row it produces, because a
    population-derived household count and a counted one are different claims
    and a planner has to be able to tell them apart.
  * ACCESS is anonymous -- no key, no login, no account. Verified 2026-10-16:
    `datasets.json` and the per-year CSV both answer 200 to an unauthenticated
    GET. An earlier revision of this integration claimed a 403 /
    institutional-access requirement; that was wrong, and the real defect was a
    malformed URL.
  * USE is NOT unrestricted, and this is the part that matters. GISCO grants a
    non-exclusive, non-transferable right to use the data ON CONDITION THAT it
    "will not be used for commercial purposes" and the source is acknowledged
    with the notice "(c) EuroGeographics for the administrative boundaries".
    Commercial use requires a separate agreement with EuroGeographics. Neither
    fact is discoverable from the API, which is why they are recorded in
    `GISCO_LICENCE` and carried onto every row a load writes.

Stdlib only (`urllib`), with the network behind an injectable opener, so the
whole module is testable with no network and no database -- the same shape as
`remote_zip`.
"""

from __future__ import annotations

import csv
import json
import re
import struct
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

__all__ = [
    "GISCO_BASE",
    "GISCO_USER_AGENT",
    "GISCO_LICENCE",
    "LauError",
    "catalogue",
    "dataset_years",
    "dataset_files",
    "csv_member",
    "csv_url",
    "latest_year",
    "parse_lau_csv",
    "fetch_lau_records",
    "download_lau_csv",
    "download_lau_gpkg",
    "gpkg_layer",
    "gpkg_wkb",
    "read_lau_geometries",
    "persons_per_household",
    "households_for_population",
    "attribute_population",
    "PERSONS_PER_HOUSEHOLD",
]

# The distribution service that serves the LAU collection. The paths below are
# RELATIVE to this: the API is not `.../lau/<year>/<file>` (which is what an
# earlier revision built and got 403s for) but `.../lau/lau-<year>-<name>.json`
# for the indexes and `.../lau/<format>/<MEMBER>` for the data files themselves.
GISCO_BASE = "https://gisco-services.ec.europa.eu/distribution/v2/lau"

# GISCO asks that automated clients identify themselves; an empty or urllib
# default agent is the one thing its front end is most likely to push back on.
GISCO_USER_AGENT = "Fibre-FTTH-Household-Register/1.0 (+https://ec.europa.eu/eurostat/web/gisco)"

# The exact terms, from the GISCO "Statistical units" copyright block (read
# 2026-10-16). Reproduced rather than summarised because it is a restriction on
# USE, not on access, and an operator loading this into a commercial design has
# to be able to see that from the loaded rows alone.
GISCO_LICENCE = (
    "Non-commercial use only, attribution required. Eurostat/GISCO grants a "
    "non-exclusive, non-transferable right to use and process these data on "
    "condition that (a) they are not used for commercial purposes and (b) the "
    "source is acknowledged with the notice '© EuroGeographics for the "
    "administrative boundaries'. Commercial use requires a licence agreement "
    "with EuroGeographics. "
    "https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units"
)


class LauError(RuntimeError):
    """The GISCO service answered, but not with something usable."""


def _default_open(url: str, timeout: int = 120) -> bytes:
    """GET `url`. Overridable in tests, so nothing here needs the network.

    TLS is verified. An earlier revision disabled certificate verification
    process-wide to work around a failure that was actually a wrong URL, which
    turned a clear 404 into an opaque SSL/403 story and would have accepted any
    certificate had the host been wrong in a different way.
    """
    req = urllib.request.Request(url, headers={"User-Agent": GISCO_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, opener: Callable[..., bytes]) -> Any:
    try:
        raw = opener(url)
    except urllib.error.HTTPError as exc:
        raise LauError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - surface as a LauError, not a bare socket error
        raise LauError(f"{url}: {type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise LauError(f"{url}: not JSON ({exc})") from exc


def catalogue(opener: Optional[Callable[..., bytes]] = None) -> Dict[str, Dict[str, Any]]:
    """Every published LAU vintage, keyed `"lau-2024"` -> metadata.

    Read this rather than hard-coding a year: the collection is extended every
    year, and a hard-coded vintage silently becomes a stale register.
    """
    data = _get_json(f"{GISCO_BASE}/datasets.json", opener or _default_open)
    if not isinstance(data, dict) or not data:
        raise LauError("datasets.json did not describe any LAU vintage")
    return data


def dataset_years(opener: Optional[Callable[..., bytes]] = None) -> List[str]:
    """The published vintage years, oldest first (`["2011", ..., "2024"]`)."""
    return sorted(
        key.rsplit("-", 1)[-1]
        for key in catalogue(opener)
        if key.startswith("lau-")
    )


def latest_year(opener: Optional[Callable[..., bytes]] = None) -> str:
    """The most recent published vintage year."""
    years = dataset_years(opener)
    if not years:
        raise LauError("no LAU vintage is published")
    return years[-1]


def dataset_files(year: str | int, opener: Optional[Callable[..., bytes]] = None
                  ) -> Dict[str, Dict[str, str]]:
    """One vintage's file index: `{format: {member_name: relative_path}}`."""
    year = str(year)
    data = _get_json(f"{GISCO_BASE}/lau-{year}-files.json", opener or _default_open)
    if not isinstance(data, dict) or not data:
        raise LauError(f"lau-{year}-files.json listed no files")
    return data


def _srid_of(member: str) -> str:
    """The EPSG code a GISCO member name carries: `LAU_RG_01M_2024_4326.csv` -> 4326."""
    stem = member.rsplit(".", 1)[0]
    return stem.rsplit("_", 1)[-1]


def csv_member(year: str | int, srid: int = 4326,
               files: Optional[Dict[str, Dict[str, str]]] = None,
               opener: Optional[Callable[..., bytes]] = None) -> str:
    """The CSV member for a vintage and CRS, e.g. `csv/LAU_RG_01M_2024_4326.csv`.

    Located through the vintage's own index rather than assembled from a guessed
    filename: the naming convention has changed between releases (`LAU_RG_01M_`
    today), and the index is the authority on what exists.
    """
    idx = files if files is not None else dataset_files(year, opener)
    members = (idx.get("csv") or {})
    wanted = {str(name): path for name, path in members.items()}
    # The CRS suffix is the last underscore field.  No "only one file, so use
    # it" fallback: a caller asking for 3857 and silently handed 4326 has made a
    # mistake this function could have caught, and for the GeoPackage the
    # equivalent shortcut would turn metres into degrees.
    for name, path in sorted(wanted.items()):
        if _srid_of(name) == str(srid):
            return path
    raise LauError(
        f"no CSV for EPSG:{srid} in LAU {year}; the vintage offers "
        f"{sorted(_srid_of(n) for n in wanted) or 'nothing'}"
    )


def csv_url(year: str | int, srid: int = 4326,
            files: Optional[Dict[str, Dict[str, str]]] = None,
            opener: Optional[Callable[..., bytes]] = None) -> str:
    """The absolute URL of a vintage's LAU CSV."""
    return f"{GISCO_BASE}/{csv_member(year, srid, files, opener)}"


# ---------------------------------------------------------------------------
# Reading a LAU CSV
# ---------------------------------------------------------------------------

def _pick(header: Iterable[str], *names: str) -> Optional[str]:
    """The header cell matching one of `names`, case-insensitively."""
    exact = {str(h or "").strip().lower(): str(h or "").strip() for h in header}
    for name in names:
        if name.lower() in exact:
            return exact[name.lower()]
    return None


def _population_column(header: Iterable[str]) -> str:
    """The total-population cell of a LAU CSV.

    The column is VINTAGE-SPECIFIC (`POP_2011` ... `POP_2024`), so it cannot be
    named as a constant, and `POP_DENS_<year>` sits right beside it -- matching
    on the `pop` prefix alone would read density as population for every
    municipality in Europe, which is a wrong answer of exactly the kind this
    whole module exists to remove. So density is excluded explicitly, and the
    header must carry a real `POP_<year>` rather than anything that merely
    starts with `pop`.
    """
    header = list(header)
    for cell in header:
        low = str(cell or "").strip().lower()
        if low.startswith("pop_") and "dens" not in low:
            return str(cell).strip()
    raise LauError(
        f"LAU CSV has no population column; header was {header!r}"
    )


def parse_lau_csv(handle: Any, country_code: Optional[str] = None,
                  year: Optional[str | int] = None) -> Iterator[Dict[str, Any]]:
    """Yield one record per LAU in a GISCO LAU CSV.

    A record is `{lau_id, country_code, name, population, area_km2, year}`, with
    the raw `population` never coerced to 0: GISCO ships 0 for a municipality
    whose population is not published (every Albanian LAU in the 2024 release is
    0), and a 0 that means "not published" and a 0 that means "nobody lives here"
    must not be averaged together. Unpublished rows keep `population=None` and
    the caller decides; `attribute_population` drops them.

    `year` is the vintage the FILE states: its own `YEAR` column when it has one,
    else the year inside the population column's name (`POP_2024`), else whatever
    the caller passed.  Read from the file rather than required as an argument,
    because the register stamps the vintage onto every row it writes and a
    vintage that came from the command line rather than the data is a claim about
    the wrong thing.

    Pure: takes any text handle, so the whole shape is testable with a few lines
    of CSV and no download.
    """
    reader = csv.DictReader(handle)
    header = reader.fieldnames or []
    if not header:
        raise LauError("LAU CSV is empty")
    # 2024 dropped LAU_ID and kept only GISCO_ID (`DE_01058132`); earlier vintages
    # carry both. GISCO_ID is the one column every vintage has.
    id_col = _pick(header, "GISCO_ID", "LAU_ID", "id")
    if not id_col:
        raise LauError(f"LAU CSV has no id column; header was {header!r}")
    pop_col = _population_column(header)
    # A vintage with no YEAR column still names its year in the population column
    # (`POP_2011` .. `POP_2024`), which is the one column the parse always has to
    # resolve anyway.
    header_year = _year_from_population_column(pop_col)
    name_col = _pick(header, "LAU_NAME", "name")
    cntr_col = _pick(header, "CNTR_CODE", "country")
    area_col = _pick(header, "AREA_KM2", "area")
    year_col = _pick(header, "YEAR")

    wanted = str(country_code or "").strip().upper() or None
    for raw in reader:
        if not raw:
            continue
        lau_id = str(raw.get(id_col) or "").strip()
        if not lau_id:
            continue
        code = str(raw.get(cntr_col) or "").strip().upper() if cntr_col else ""
        if not code:
            # GISCO_ID is `<CNTR>_<LAU>`, so the country survives even when the
            # dedicated column is absent.
            code = lau_id.split("_", 1)[0].upper()
        if wanted and code != wanted:
            continue
        population = _int_or_none(raw.get(pop_col))
        row_year = str(raw.get(year_col) or "").strip() if year_col else ""
        rec: Dict[str, Any] = {
            "lau_id": lau_id,
            "country_code": code,
            "name": str(raw.get(name_col) or "").strip() if name_col else "",
            "population": population,
            "area_km2": _float_or_none(raw.get(area_col)) if area_col else None,
            "year": row_year or header_year or str(year or ""),
        }
        yield rec


def _year_from_population_column(column: Any) -> str:
    """`POP_2024` -> `"2024"`; "" for anything that is not a vintage population.

    `POP_DENS_2024` deliberately does not match: density sits beside population
    and must never be read as either the count or its year.
    """
    match = re.fullmatch(r"POP_(\d{4})", str(column or "").strip().upper())
    return match.group(1) if match else ""


def _int_or_none(value: Any) -> Optional[int]:
    """A published integer, or None when the cell says nothing usable.

    Blank, `-`, and the non-numeric tokens GISCO uses for "not published" are
    all "no answer", not zero.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("\u00a0", " ")
    if not text or text in ("-", ".", "NA", "N/A", "null", "None", ":"):
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", ".", "NA", "N/A", "null", "None", ":"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def download_lau_csv(year: str | int, path: str, country_code: Optional[str] = None,
                     srid: int = 4326,
                     opener: Optional[Callable[..., bytes]] = None
                     ) -> Dict[str, Any]:
    """Download a vintage's LAU CSV, optionally filtered to one country.

    The file written is a LAU CSV in GISCO's OWN format -- same header, same
    cell values -- rather than a renamed or re-typed copy. That is what makes
    the two operator steps compose: the fetched file is a valid `--file` for the
    loader, so the ingest re-reads exactly what was downloaded and the
    attribution can never disagree with the file on disk about a population.

    Returns `{path, rows, bytes, url, year, country_code}`; `rows` is the number
    of data rows written, which is 0 for a country with no LAUs in that vintage
    and is reported rather than treated as success.
    """
    import io
    from pathlib import Path

    url = csv_url(year, srid, opener=opener)
    try:
        raw = (opener or _default_open)(url)
    except urllib.error.HTTPError as exc:
        raise LauError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LauError(f"{url}: {type(exc).__name__}: {exc}") from exc

    text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig",
                            errors="replace", newline="")
    reader = csv.DictReader(text)
    header = reader.fieldnames or []
    if not header:
        raise LauError(f"{url}: empty CSV")
    wanted = str(country_code or "").strip().upper() or None
    cntr_col = _pick(header, "CNTR_CODE", "country")
    id_col = _pick(header, "GISCO_ID", "LAU_ID", "id")

    out_path = Path(path)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in reader:
            if not row:
                continue
            if wanted:
                code = str(row.get(cntr_col) or "").strip().upper() if cntr_col else ""
                if not code and id_col:
                    code = str(row.get(id_col) or "").strip().split("_", 1)[0].upper()
                if code != wanted:
                    continue
            writer.writerow(row)
            rows += 1
    return {
        "path": str(out_path), "rows": rows, "bytes": out_path.stat().st_size,
        "url": url, "year": str(year), "country_code": wanted,
    }


def fetch_lau_records(year: str | int, country_code: Optional[str] = None,
                      srid: int = 4326,
                      opener: Optional[Callable[..., bytes]] = None
                      ) -> List[Dict[str, Any]]:
    """Download a vintage and return its records for one country (or all of them).

    The whole European CSV is a few MB, so it is read in one request and the
    country filter is applied while parsing rather than by a second request.
    """
    import io

    url = csv_url(year, srid, opener=opener)
    try:
        raw = (opener or _default_open)(url)
    except urllib.error.HTTPError as exc:
        raise LauError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LauError(f"{url}: {type(exc).__name__}: {exc}") from exc
    text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig",
                            errors="replace", newline="")
    return list(parse_lau_csv(text, country_code, year))


# ---------------------------------------------------------------------------
# Population -> households
# ---------------------------------------------------------------------------
#
# LAU publishes PEOPLE; the register's `households` column is a household count,
# so a divisor is unavoidable. It is Eurostat's own "Average household size"
# (dataset ilc_lvph01), read 2026-10-16 for reference year 2024, and the values
# below are that table -- not a rule of thumb. Household size moves slowly but it
# does move, so the year is part of the provenance the loaded rows carry.
#
# `EU27_2020` is the fallback for a GISCO country that is not in the table, and
# it is a real published figure rather than a guess; it is still an ESTIMATE for
# that country, which is why `attribute_population` marks every row it makes.
PERSONS_PER_HOUSEHOLD: Dict[str, float] = {
    "AT": 2.2, "BE": 2.3, "BG": 2.3, "CY": 2.5, "CZ": 2.3, "DE": 2.0,
    "DK": 2.0, "EE": 2.1, "EL": 2.4, "ES": 2.5, "FI": 1.9, "FR": 2.1,
    "HR": 2.7, "HU": 2.3, "IE": 2.7, "IS": 2.2, "IT": 2.2, "LI": 2.3,
    "LT": 1.9, "LU": 2.3, "LV": 2.2, "ME": 2.5, "MK": 2.9, "MT": 2.4,
    "NL": 2.1, "NO": 2.0, "PL": 2.9, "PT": 2.4, "RO": 2.5, "RS": 2.7,
    "SE": 2.0, "SI": 2.4, "SK": 3.1, "AL": 2.9, "CH": 2.2, "TR": 3.1,
    "UK": 2.3, "GB": 2.3,
}

HOUSEHOLD_SIZE_DATASET = "ilc_lvph01"
HOUSEHOLD_SIZE_YEAR = "2024"
EU27_PERSONS_PER_HOUSEHOLD = 2.3


def persons_per_household(country_code: str) -> Tuple[float, bool]:
    """`(persons per household, is_published_for_this_country)`.

    The flag is returned rather than logged, because the difference between a
    national figure and the EU27 fallback is the difference between an estimate
    with a real basis and one without, and the caller has to be able to record
    which it used.
    """
    code = str(country_code or "").strip().upper()
    if code in PERSONS_PER_HOUSEHOLD:
        return PERSONS_PER_HOUSEHOLD[code], True
    return EU27_PERSONS_PER_HOUSEHOLD, False


def households_for_population(population: Any, country_code: str) -> Optional[int]:
    """Published population -> an ESTIMATED household count, or None.

    None when the population is missing or non-positive: a municipality GISCO
    does not publish a figure for must not be turned into `0 households`, which
    would place a real town in the register as empty.
    """
    if population is None:
        return None
    try:
        people = float(population)
    except (TypeError, ValueError):
        return None
    if people <= 0:
        return None
    people_per_hh, _published = persons_per_household(country_code)
    if people_per_hh <= 0:
        return None
    return max(1, int(round(people / people_per_hh)))


# ---------------------------------------------------------------------------
# Apportioning a LAU figure onto postcodes
# ---------------------------------------------------------------------------

def attribute_population(overlaps: Iterable[Dict[str, Any]],
                         country_code: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Turn LAU-postcode overlap areas into postcode register records.

    A LAU publishes one figure for the whole municipality, and postcodes do not
    nest inside municipalities -- a postcode can span three LAUs and a LAU holds
    hundreds of postcodes -- so the figure has to be SHARED OUT. The shares come
    from area: the population in a (postcode, LAU) overlap is the LAU's
    population times the overlap's share of the LAU's area.

    That assumes population is spread evenly across a municipality, which is
    false wherever a commune has one dense town and a large empty field: the
    estimate is good for the average and wrong for the outlier, so the records
    this returns are marked `household_method = "gisco_lau_estimate"` and the
    stats carry the assumption. That is deliberately the opposite of the UK
    path, where the count is a published one and no assumption is involved.

    `overlaps` are dicts from the geometry step:
        {postcode, lau_id, overlap_km2, lau_area_km2, lau_population}
    A LAU may appear against many postcodes and a postcode against many LAUs;
    both directions are summed and neither is allowed past its own total: a
    postcode's shares are clamped so a row can never claim more than the LAU's
    whole population, which is what keeps a bad polygon (one that covers a
    neighbouring district) from inflating a postcode.

    Returns `(records, stats)` where each record is
    `{postcode, households, population, basis}`.
    """
    # Materialised ONCE: the two passes below cannot share a generator, and the
    # caller's source is a stream off a download or a query cursor.
    rows = list(overlaps)
    code = str(country_code or "").strip().upper()
    people_per_hh, published = persons_per_household(code)
    stats: Dict[str, Any] = {
        "country_code": code,
        "overlaps": 0,
        "lau_seen": 0,
        "postcodes": 0,
        "population_attributed": 0,
        "population_available": 0,
        "dropped_no_population": 0,
        "dropped_no_area": 0,
        "clamped": 0,
        "persons_per_household": people_per_hh,
        "household_size_published": published,
        "assumption": ("area-proportional share of the municipality's published "
                       "population; household count divides it by Eurostat "
                       f"{HOUSEHOLD_SIZE_DATASET} {HOUSEHOLD_SIZE_YEAR} average "
                       "household size"),
    }

    pop_by_postcode: Dict[str, float] = {}
    lau_ids: Dict[str, Dict[str, Any]] = {}
    claimed_by_lau: Dict[str, float] = {}

    for row in rows:
        stats["overlaps"] += 1
        postcode = str(row.get("postcode") or "").strip()
        lau_id = str(row.get("lau_id") or "").strip()
        if not postcode or not lau_id:
            continue
        lau_ids[lau_id] = row
        area = _float_or_none(row.get("lau_area_km2"))
        share_area = _float_or_none(row.get("overlap_km2"))
        population = _int_or_none(row.get("lau_population"))
        if population is None or population <= 0:
            stats["dropped_no_population"] += 1
            continue
        if not area or area <= 0 or share_area is None or share_area <= 0:
            stats["dropped_no_area"] += 1
            continue
        share = share_area / area
        if share > 1.0:
            # A postcode polygon cannot hold more of a LAU than the LAU has. If
            # it claims to, the geometry is wrong (overlapping postcode polygons
            # are the usual cause), so the share is capped and counted rather
            # than allowed to invent population that does not exist.
            share = 1.0
            stats["clamped"] += 1
        claimed_by_lau[lau_id] = claimed_by_lau.get(lau_id, 0.0) + share
    stats["lau_seen"] = len(lau_ids)

    # A second pass is needed to cap each LAU's TOTAL share at 1.0, which is only
    # knowable once every overlap has been seen. Without it, three adjacent
    # postcodes that between them cover 130% of a municipality would be handed
    # 130% of its population.
    scale: Dict[str, float] = {
        lau_id: (1.0 / claimed) for lau_id, claimed in claimed_by_lau.items() if claimed > 1.0
    }
    if scale:
        stats["clamped_lau_totals"] = len(scale)

    for row in rows:
        postcode = str(row.get("postcode") or "").strip()
        lau_id = str(row.get("lau_id") or "").strip()
        if not postcode or not lau_id:
            continue
        area = _float_or_none(row.get("lau_area_km2"))
        share_area = _float_or_none(row.get("overlap_km2"))
        population = _int_or_none(row.get("lau_population"))
        if not population or population <= 0 or not area or area <= 0 or not share_area or share_area <= 0:
            continue
        share = min(1.0, share_area / area) * scale.get(lau_id, 1.0)
        pop_by_postcode[postcode] = pop_by_postcode.get(postcode, 0.0) + population * share
        stats["population_available"] += population * share

    records: List[Dict[str, Any]] = []
    for postcode in sorted(pop_by_postcode):
        people_f = pop_by_postcode[postcode]
        people = int(round(people_f))
        households = households_for_population(people, code)
        if households is None:
            continue
        records.append({
            "postcode": postcode,
            "households": households,
            "population": people,
            "basis": "gisco_lau_estimate",
        })
    stats["postcodes"] = len(records)
    stats["population_attributed"] = sum(int(r["population"]) for r in records)
    return records, stats


# ---------------------------------------------------------------------------
# LAU polygons, out of the GeoPackage
# ---------------------------------------------------------------------------
#
# The attribution needs the municipality BOUNDARIES, and the GeoPackage is the
# only GISCO member that can be read without loading 150 MB of GeoJSON into
# memory (the GeoJSON is a single 150 MB line, which on the free-plan instance
# that already crashed at 285 premises is not a risk worth taking).  A GPKG is
# SQLite, so one country can be read straight out of it.
#
# GeoPackage stores each geometry as a small binary header followed by WKB.  The
# header is what has to be understood: it carries the SRS (so the polygons are
# not silently assumed to be in 4326 when the file is actually in equal-area
# 3035, which would make every area -- and so every population share -- wrong).

_GPKG_MAGIC = b"GP"
# Envelope size by the header's 3-bit envelope indicator.  Indicators 5-7 are
# reserved by the spec, so they are refused rather than treated as zero, which
# would hand PostGIS a WKB body that starts 32 bytes too early.
_GPKG_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def gpkg_wkb(blob: bytes) -> Tuple[bytes, int]:
    """`(WKB, srid)` from a GeoPackage geometry blob.

    Reads the GPKG binary header (magic `GP`, version, flags, srid, optional
    envelope) and returns the WKB that follows it, so PostGIS can parse the
    geometry with `ST_GeomFromWKB` without a GDAL/OGR dependency.  An EMPTY
    geometry (flag bit 4) yields `b""`, which callers skip: a municipality with
    no polygon cannot be intersected with a postcode, and pretending it has a
    zero-area one would divide by zero downstream.
    """
    if len(blob) < 8 or blob[:2] != _GPKG_MAGIC:
        raise LauError("not a GeoPackage geometry (bad magic)")
    flags = blob[3]
    srid = int(struct.unpack("<i", blob[4:8])[0])
    if flags & 0x10:  # empty-geometry flag
        return b"", srid
    indicator = (flags >> 1) & 0x07
    envelope = _GPKG_ENVELOPE_BYTES.get(indicator)
    if envelope is None:
        raise LauError(f"reserved envelope indicator {indicator} in the GPKG header")
    start = 8 + envelope
    if len(blob) <= start:
        # Header only: an empty geometry that did not set its flag.
        return b"", srid
    return blob[start:], srid


def gpkg_layer(path: str) -> Tuple[str, int]:
    """`(table name, srid)` of the LAU feature table inside a GISCO GeoPackage.

    Found through `gpkg_contents`, not assumed from the filename: the table is
    named after the file INCLUDING its extension (`LAU_RG_01M_2024_3035.gpkg`),
    which is a name that has to be quoted in SQL, and the layout of a GPKG is
    the GeoPackage spec's business rather than something to hard-code.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT table_name, srs_id FROM gpkg_contents WHERE data_type = 'features'"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise LauError(f"{path}: no feature table (gpkg_contents is empty)")
    if len(rows) > 1:
        # More than one layer means this is not the single-layer LAU member this
        # reader is for, and picking one silently could load the wrong geometry.
        raise LauError(f"{path}: {len(rows)} feature tables; expected one")
    table, srid = rows[0]
    return str(table), int(srid or 0)


def read_lau_geometries(path: str, country_code: Optional[str] = None,
                        ) -> Iterator[Dict[str, Any]]:
    """Yield `{lau_id, country_code, name, wkb, srid}` from a GISCO GeoPackage.

    One country at a time, straight out of SQLite, so a 78 MB European file never
    has to be held in memory.  Rows with an EMPTY geometry are yielded with
    `wkb = b""` rather than skipped, so the caller can count what it dropped
    instead of quietly loading fewer municipalities than the file holds.

    The municipality NAME is read when the file carries one (GISCO spells it
    `LAU_NAME`, older layers `NAME_LATN`).  `osm.gisco_lau` has a `name` column
    and nothing filled it, so the table that says a postcode's population came
    from a municipality listed 10,978 rows with no name -- and the one question
    an operator asks of it is WHICH municipality.
    """
    import sqlite3

    table, layer_srid = gpkg_layer(path)
    quoted = '"' + str(table).replace('"', '""') + '"'
    wanted = str(country_code or "").strip().upper() or None
    conn = sqlite3.connect(path)
    try:
        columns = {str(row[1]).upper(): str(row[1])
                   for row in conn.execute(f"PRAGMA table_info({quoted})")}
        name_col = next((columns[key] for key in ("LAU_NAME", "NAME_LATN", "NAME")
                         if key in columns), None)
        selected = ["GISCO_ID", "CNTR_CODE", "geom"]
        if name_col:
            selected.append(name_col)
        sql = f"SELECT {', '.join(selected)} FROM {quoted}"
        params: Tuple[Any, ...] = ()
        if wanted:
            sql += " WHERE CNTR_CODE = ?"
            params = (wanted,)
        for row in conn.execute(sql, params):
            gisco_id, code, blob = row[0], row[1], row[2]
            if not blob:
                wkb, srid = b"", layer_srid
            else:
                wkb, srid = gpkg_wkb(bytes(blob))
            yield {
                "lau_id": str(gisco_id or ""),
                "country_code": str(code or "").upper(),
                "name": str(row[3] or "").strip() if name_col else "",
                "wkb": wkb,
                "srid": srid or layer_srid,
            }
    finally:
        conn.close()


def download_lau_gpkg(year: str | int, path: str, srid: int = 4326,
                      opener: Optional[Callable[..., bytes]] = None) -> Dict[str, Any]:
    """Download a vintage's LAU GeoPackage (the boundaries).

    Prefer 3035.  It is the equal-area European projection, so `ST_Area` on the
    loaded polygons returns real square metres; under 4326 the same polygons are
    in degrees, where an area is a number with no physical meaning, and every
    population share derived from it would be quietly wrong.
    """
    from pathlib import Path

    idx = dataset_files(year, opener)
    members = idx.get("gpkg") or {}
    chosen = next((p for n, p in sorted(members.items()) if _srid_of(n) == str(srid)), None)
    if chosen is None:
        available = sorted(_srid_of(n) for n in members) or ["nothing"]
        raise LauError(f"no GPKG for EPSG:{srid} in LAU {year}; offers {available}")
    url = f"{GISCO_BASE}/{chosen}"
    try:
        raw = (opener or _default_open)(url)
    except urllib.error.HTTPError as exc:
        raise LauError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LauError(f"{url}: {type(exc).__name__}: {exc}") from exc
    out = Path(path)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return {"path": str(out), "bytes": len(raw), "url": url, "year": str(year)}
