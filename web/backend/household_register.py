"""A pluggable EXTERNAL household register, so UK household counts are measured.

The household rule in `osm_source` is a heuristic and is honest about it: on
real Berlin data it was ~100 % `fallback_one` / `levels_x_footprint`, because
OpenStreetMap carries `building:flats` on 1 building in 15,583 and `addr:flats`
on none.  Trunk sizing, HH-based physical-location drop capacity, and the BOQ rest on
that heuristic, and
`household_method` travels with each row so nobody mistakes it for a survey.

The UK is the case where this is *partly fixable with authoritative data*:

* **UPRN** (OS Open UPRN / OS Open Names) gives a unique identifier and a
  coordinate for every addressable location, but does NOT include a dwelling
  count. AddressBase can carry per-UPRN property/dwelling information under its
  own licence. UPRNs are the join key for an address-level register only when
  paired with an explicit, licensed count source.
* **ONSPD** (ONS Postcode Directory) is free, quarterly, OGL v3, and is the
  canonical postcode lookup. **It is not a household-count source.** The May
  2026 release's full CSV, per-area CSV splits, and ArcGIS hosted table are
  the same 53-column geography lookup, with no `Dwellings` or `Pop01`. The
  postcode-area B CSV is available in place for geography lookup, but loading
  it as household counts is deliberately refused. Its useful role here is the
  official postcode -> census Output Area / LSOA / MSOA mapping.
* **ONS Census 2021 RM204 — Number of Dwellings** is the most relevant
  published count source found for England and Wales. It reports dwellings by
  census geography (down to Output Area), not by postcode. It is a defensible
  small-area dwelling total, but joining it to this postcode-keyed register
  requires an explicit geographic crosswalk and allocation rule; do not repeat
  an OA total on each postcode or label it an exact postcode count. Scotland
  and Northern Ireland require their own census sources/geographies. Official
  links: [ONS RM204](https://www.ons.gov.uk/datasets/RM204/editions/2021/versions/1)
  and [Nomis bulk Census downloads](https://www.nomisweb.co.uk/sources/census_2021_bulk)
  (one geography-specific CSV per zip; ONS says OA is its lowest census geography).

An operator-supplied ONSPD file from an older release is accepted only if it
actually contains a dwellings/households column; the CSV reader rejects a
geography-only file. The loader validates the first usable record before it
replaces any already-loaded source rows, so a rejected file cannot empty the
register.

So the register is keyed on the **postcode**, with an optional **UPRN** column
for operators who have an address-level extract.  Both are ingest-time operator
steps (`ingest_household_register.py`), never something a request downloads --
same rule as the boundary datasets: which dataset, and under which licence, is
not a decision this code should make on its own.

    python ingest_household_register.py --list
    python ingest_household_register.py --source onspd --file <count-bearing-release.zip>
    python ingest_household_register.py --source uprn --file uprns.csv

What "wins" means is stated precisely in `apply_register`, and it is the whole
point of the module: a register count REPLACES the heuristic for the premises it
covers, `household_method` says so (`register_postcode` / `register_uprn`), and the
register's own source, licence and vintage travel with the run.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import postgis

# ---------------------------------------------------------------------------
# Turning on/off
# ---------------------------------------------------------------------------
#
# Default ON for the countries the register covers (GB/UK), and only those:
# `household_register_for` refuses any other country before it reads anything,
# so a non-UK area never consults the register however this is set. It is also
# a no-op when nothing is loaded. The knob still forces it off explicitly
# (`OSM_HOUSEHOLD_REGISTER=0`) -- a register changes the number the cable sizing
# and the BOQ are computed from, so an operator must be able to measure a run
# both ways.
#
# `OSM_HOUSEHOLD_REGISTER` is the canonical name. `OSM_HH_REGISTER` is accepted
# as an alias because the ingest CLI and its help text told operators to set
# that name while this module read the other one -- so following the documented
# instruction turned nothing on, and the register stayed off with no error.
REGISTER_ENV_NAMES = ("OSM_HOUSEHOLD_REGISTER", "OSM_HH_REGISTER")


def register_env_name() -> str:
    """The enabled variable the operator actually set (canonical name otherwise)."""
    for name in REGISTER_ENV_NAMES:
        if os.environ.get(name) is not None:
            return name
    return REGISTER_ENV_NAMES[0]


def register_enabled_from_env() -> bool:
    """True unless a known OSM household-register knob says "off".

    On by default for the countries the register covers (GB/UK) -- the country
    gate in `household_register_for` is what keeps it off everywhere else, and
    it is never read for an area with no register loaded. Explicit "off" on the
    canonical name wins, so an operator can disable a register that an ambient
    `OSM_HH_REGISTER=1` would otherwise switch on.
    """
    if os.environ.get(REGISTER_ENV_NAMES[0]) is not None:
        return os.environ[REGISTER_ENV_NAMES[0]].strip().lower() \
            not in ("0", "false", "no", "off")
    for name in REGISTER_ENV_NAMES[1:]:
        if os.environ.get(name) is not None:
            return os.environ[name].strip().lower() not in ("0", "false", "no", "off")
    return True


REGISTER_ENABLED = register_enabled_from_env()

# A register count below this is treated as absent rather than as a real answer.
# ONSPD uses 0 (and a blank) for postcodes with no counted dwellings, and a
# postcode sector is a container that may hold no homes; forcing 1 household onto
# every premise in such a bucket would be inventing demand, which is the one
# thing this module exists to stop.
MIN_REGISTER_HOUSEHOLDS = 1

# The UK only.  A register keyed on postcodes is a UK-shaped idea, and the
# matching is deliberately not attempted for any other country rather than
# guessed at.
REGISTER_COUNTRIES = frozenset({"GB", "UK"})

# The local store, matching osm_source.OSM_SCHEMA. Named here rather than
# imported so this module stays importable (and testable) with no database.
OSM_SCHEMA = "osm"


# ---------------------------------------------------------------------------
# Postcode normalisation
# ---------------------------------------------------------------------------

# A UK postcode is an outward code and an inward code: "B11 3SA" is outward
# "B11" + inward "3SA".  The inward code is always the last three characters and
# is always a digit followed by two letters, which is what makes the split
# positional -- but the POSITION alone is not enough to tell a postcode from a
# word: "rubbish" is seven letters, and a purely positional rule happily turns
# it into " RUB BIS", which then matches nothing while looking like a valid key.
# So the shape is checked as well as the length.
_INWARD_RE = re.compile(r"^\d[A-Z]{2}$")
_OUTWARD_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")


def normalize_postcode(value: Any) -> str:
    """`"b11 3sa"`, `"B113SA"`, `"B11-3SA"` -> `"B11 3SA"`.  `""` when unusable.

    Registers, OSM `addr:postcode` and a planner's typing all spell the same
    postcode differently, and a join that misses on whitespace is a join that
    silently returns nothing -- so the register side and the premise side are
    normalised through this one function.

    Anything that is not shaped like a postcode returns "".  That matters more
    than it looks: a too-permissive rule does not return a wrong match so much as
    a plausible-looking key that matches nothing, and a register that appears to
    have no data for an area is indistinguishable from a UK that has none.
    """
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(text) < 5 or len(text) > 7:
        return ""
    inward, outward = text[-3:], text[:-3]
    if not _INWARD_RE.match(inward) or not _OUTWARD_RE.match(outward):
        return ""
    return f"{outward} {inward}"


def postcode_outward(value: Any) -> str:
    """The outward code of a postcode (`"B11 3SA"` -> `"B11"`), for area rollups."""
    full = normalize_postcode(value)
    return full[:-4].strip() if full else ""


# ---------------------------------------------------------------------------
# Named register sources
# ---------------------------------------------------------------------------
#
# Each entry is one explicit operator decision: this dataset, under this
# licence.  Mirrors UK_WARDS_SOURCE / US_ZCTA_SOURCE in osm_source, deliberately
# including the caveat about what the number IS -- `Dwellings` is a count of
# dwellings, not a count of people and not a survey of occupancy.

ONSPD_SOURCE: Dict[str, Any] = {
    "name": "ONS Postcode Directory (ONSPD)",
    "kind": "postcode",
    "country_code": "GB",
    "vintage": "operator-supplied release",
    "publisher": "Office for National Statistics",
    "url": "https://geoportal.statistics.gov.uk/datasets",
    "licence": (
        "Open Government Licence v3.0 (ONS Postcode Directory; "
        "https://www.ons.gov.uk/methodology/geography/licences)"
    ),
    "note": (
        "The May 2026 ONSPD publication is a postcode geography lookup, not a "
        "household-count source. This loader accepts an operator-supplied CSV/ZIP "
        "only when it has an explicit dwellings/households column; geography-only "
        "files are rejected. If a supplied count is present, it is a postcode "
        "dwelling total (not an occupancy survey or people count; vacant and "
        "second homes are included)."
    ),
    # ONSPD's own column names, so a downloaded file needs no renaming. The
    # reader accepts the usual spellings too (see REGISTER_COLUMN_ALIASES).
    "fields": {"postcode": "pcd", "dwellings": "dwellings", "population": "pop01"},
    # The ONSPD is distributed as a zip of CSVs split by postcode area, so the
    # reader looks inside one rather than expecting a single file.
    "zip_member_glob": "Data/*.csv",
}

UPRN_SOURCE: Dict[str, Any] = {
    "name": "UPRN household register (address-level extract)",
    "kind": "uprn",
    "country_code": "GB",
    "vintage": "operator-supplied extract",
    "publisher": "operator-supplied (e.g. OS Open UPRN / AddressBase / an internal extract)",
    "url": "https://www.ordnancesurvey.co.uk/products/os-open-uprn",
    "licence": (
        "Operator-supplied. OS Open UPRN is Open Government Licence v3.0, but the "
        "OPEN UPRN product carries identifiers and coordinates only -- the "
        "dwellings count must come from a licensed AddressBase extract or the "
        "operator's own data, and its licence is the operator's to state."
    ),
    "note": (
        "A UPRN is a unique identifier for every addressable location, not a "
        "household: a block of flats is ONE UPRN with several dwellings. A "
        "register keyed on UPRN is therefore more precise than one keyed on "
        "postcode only when it also carries that UPRN's dwelling count."
    ),
    "fields": {"uprn": "uprn", "postcode": "postcode", "dwellings": "dwellings"},
}


# EPC (Energy Performance Certificate) domestic certificates.  NOT a census or a
# dwelling register: it is one certificate per DWELLING, published at address
# level (postcode + UPRN), so counting distinct dwellings per postcode gives a
# real postcode figure -- which is exactly what the ONSPD and OS Open UPRN
# cannot (they carry no count at all).  England & Wales only; Scotland publishes
# separately.  The bulk archive needs a GOV.UK One Login, and the developer API
# a registered key, so this reads an operator-supplied file.
EPC_SOURCE: Dict[str, Any] = {
    "name": "EPC domestic certificates (Energy Performance of Buildings Data)",
    "kind": "epc",
    "country_code": "GB",
    "vintage": "operator-supplied release",
    "publisher": "Department for Energy Security and Net Zero / DLUHC",
    "url": "https://get-energy-performance-data.communities.gov.uk/",
    "licence": "Open Government Licence v3.0 (EPC open data)",
    "note": (
        "One certificate per dwelling, at address level (postcode + UPRN), so "
        "counting DISTINCT dwellings is counting homes -- the one free source "
        "that is genuinely postcode-keyed. It measures DWELLINGS, not occupied "
        "households, and only those that have been assessed (a property never "
        "sold or let since 2008 may be absent), so it can undercount. England & "
        "Wales only; Scotland is a separate publication."
    ),
    # EPC bulk CSVs carry `POSTCODE`, `UPRN` and `LMK_KEY` (the certificate id).
    "fields": {"postcode": "postcode", "uprn": "uprn", "certificate": "lmk_key"},
    # The archive ships as a zip of per-area CSVs, so the reader looks inside one.
    "zip_member_glob": "*.csv",
    "aggregates_to_postcode": True,
}



# Column spellings accepted for each field, lower-cased and stripped. Legacy
# ONSPD/count files may spell them `pcd` / `Dwellings` / `Pop01`; an operator's
# own extract may use `postcode` / `households` / `hh`. Current ONSPD geography
# files have no count column and are rejected by read_register_csv.
#
# ONSPD can also be read by postcode area over HTTP range requests. That area
# endpoint is retained as metadata for a future postcode/geography crosswalk,
# not as an ingestible household-register source: it has no dwelling counts.
#
# The URL is the ArcGIS item that serves the ONSPD release's CSV archive.  ONS
# re-publishes quarterly, so the item id changes each release -- hence a
# versioned constant rather than a scraped link, and `--source onspd --file`
# stays the route for a release this entry has not been pointed at yet.
ONSPD_AREA_SOURCE: Dict[str, Any] = {
    "name": "ONS Postcode Directory (May 2026) — area lookup only",
    "kind": "postcode_lookup",
    "country_code": "GB",
    "vintage": "May 2026",
    "publisher": "Office for National Statistics",
    "url": ("https://www.arcgis.com/sharing/rest/content/items/"
            "6fff67d204fd4f339591ed667a6e3642/data"),
    "licence": ONSPD_SOURCE["licence"],
    "note": (
        "The B-area member can be fetched without downloading the full release, "
        "but this 53-column geography lookup contains no dwellings count. It "
        "cannot be loaded into the household register; use it only as a postcode "
        "to census-geography crosswalk."
    ),
    "has_dwellings_count": False,
    "fields": ONSPD_SOURCE["fields"],
    "member_glob": "data/multi_csv/*_uk_{areas}.csv",
    "remote": True,
}


def read_remote_register(
    source: Dict[str, Any], areas: Sequence[str]
) -> Iterator[Dict[str, Any]]:
    """Read selected members of the remote ONSPD area lookup archive.

    This remains for geography-crosswalk tooling only. The current ONSPD has no
    household counts and ``ingest_source`` explicitly refuses this source.
    `areas` are postcode AREAS ("B", "EH", ...), not outward codes. The whole
    matching member is fetched (a few MB rather than the full archive).
    """
    import remote_zip

    url = str(source.get("url") or "")
    if not url:
        raise ValueError("remote register source needs a url")
    wanted = [a.strip().upper() for a in areas if str(a or "").strip()]
    if not wanted:
        raise ValueError("no postcode areas given")
    entries = remote_zip.zip_entries(url)
    # "_AB_," as one alternative would only match the AB area; a trailing "_"
    # is what makes a list of areas a list, because the member is ..._UK_<AREA>.csv
    pattern = str(source.get("member_glob") or "data/multi_csv/*_uk_{areas}.csv")
    pattern = pattern.format(areas="_".join(wanted) + "_")
    hits = remote_zip.select(entries, [pattern])
    if not hits:
        have = sorted({e.name.rsplit("/", 1)[-1] for e in entries
                       if not e.name.endswith("/")})[:12]
        raise ValueError(
            f"no member matching {pattern!r} in the archive; first members: {have}")
    for entry in sorted(hits, key=lambda e: e.name):
        yield from read_register_csv(
            io.TextIOWrapper(
                io.BytesIO(remote_zip.read_member(url, entry)),
                encoding="utf-8-sig", errors="replace", newline="",
            ),
            source,
        )


# The EPC service also offers a developer API, which is far better suited to a
# single project than the multi-GB bulk archive: it takes a postcode filter and
# returns the certificates for it, so the register can be filled for just the
# postcodes an area actually has.  Access is free but needs an account; the token
# is a Bearer token shown in the account page footer.  Endpoint and pagination
# live in this dict (not in code) so a change on their side is one line, and the
# reader tolerates both `rows` and `data` envelopes.
EPC_API_SOURCE: Dict[str, Any] = {
    "name": "EPC domestic certificates — developer API",
    "kind": "epc_api",
    "country_code": "GB",
    "vintage": "live service",
    "publisher": "Department for Energy Security and Net Zero / MHCLG",
    "url": "https://api.get-energy-performance-data.communities.gov.uk/api/domestic/search",
    "licence": EPC_SOURCE["licence"],
    "note": (
        "The same EPC certificates as the bulk archive, fetched per postcode. "
        "Free but registered: set EPC_API_TOKEN (the Bearer token in the "
        "account page footer), then `--source epc_api --postcodes <p1,p2>` or "
        "--areas <outward codes>`. Rows are aggregated to postcode totals the "
        "same way the bulk file is (distinct dwellings), so it measures "
        "DWELLINGS, not occupied households."
    ),
    "remote": True,
    "token_env": ("EPC_API_TOKEN", "EPC_API_KEY", "EPC_API_TOKEN_HEADER"),
    # Filter/response spellings, kept here so a change is one line.  `page_size`
    # is what the old open-data service used; the new one paginates the same way.
    "postcode_param": "postcode",
    "page_param": "page",
    "page_size_param": "page_size",
    "page_size": 5000,
    "rows_keys": ("data", "rows", "results"),
    "next_keys": ("next", "next_page", "links"),
    # A syntactically valid postcode with no certificates answers HTTP 404 with
    #   {"data": {"error": "No certificates could be found for that query"}}
    # which is "no dwellings here", not a broken request, so it ends that term
    # with an empty result instead of taking the whole load down.
    "empty_result_status": 404,
    "empty_result_marker": "No certificates could be found",
    # The new service paginates with an envelope rather than a next link:
    #   {"data": [...], "pagination": {"totalRecords": n, "currentPage": 1,
    #    "totalPages": t, "nextPage": 2|null, "prevPage": null, "pageSize": 5000}}
    # so the page number to fetch next lives at pagination.nextPage (null on the
    # last page), and pageSize is the server's effective page length.  The
    # postcode filter takes one VALID FULL postcode (an outward code like "B16"
    # is a 400), which is exactly the granularity the register is keyed at.
    "pagination_key": "pagination",
    "next_page_key": "nextPage",
    "total_pages_key": "totalPages",
    "page_size_key": "pageSize",
    "aggregates_to_postcode": True,
}

def read_epc_api(
    source: Dict[str, Any],
    terms: Sequence[str],
    *,
    opener: Optional[Any] = None,
    token: str = "",
    limit_pages: int = 1000,
) -> Iterator[Dict[str, Any]]:
    """Postcode-aggregated register rows from the EPC developer API.

    Queries the certificates for each postcode (or postcode prefix) and runs the
    SAME `aggregate_epc` the bulk archive goes through, so the two routes cannot
    disagree about what a postcode's number is: distinct dwellings, one `uprn`
    row each, and the postcode total first.

    Pagination follows the response's own next link when it offers one, else
    pages until a short page comes back.  `opener` is injectable so the request
    shape and the paging are testable with no network and no token.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = str(source.get("url") or "")
    if not url:
        raise ValueError("epc_api source needs a url")
    wanted = [str(t).strip() for t in (terms or []) if str(t or "").strip()]
    if not wanted:
        raise ValueError(
            "the EPC API needs --postcodes <p1,p2> (or --areas with outward codes)"
        )
    token = token or epc_api_token(source)
    open_url = opener or urllib.request.urlopen
    rows_keys = tuple(source.get("rows_keys") or ("rows", "data", "results"))
    page_size = int(source.get("page_size") or 5000)

    collected: List[Dict[str, Any]] = []
    pages = 0
    for term in wanted:
        params = {
            str(source.get("postcode_param") or "postcode"): term,
            str(source.get("page_param") or "page"): 1,
            str(source.get("page_size_param") or "page_size"): page_size,
        }
        next_url = ""
        while pages < limit_pages:
            pages += 1
            target = next_url or f"{url}?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(target, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "fibre-ftth-household-register/1.0",
            })
            try:
                with open_url(request) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
            except urllib.error.HTTPError as exc:
                marker = str(source.get("empty_result_marker") or "").lower()
                status = int(source.get("empty_result_status") or 0)
                body = ""
                if exc.code == status and marker:
                    try:
                        body = exc.read().decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001 - body is best-effort
                        body = ""
                if marker and marker in body.lower():
                    # Valid postcode, no certificates: no dwellings here.
                    break
                raise
            batch: List[Dict[str, Any]] = []
            for key in rows_keys:
                if isinstance(payload.get(key), list):
                    batch = payload[key]
                    break
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                # The API names its columns in its own case (`UPRN`, `LMK_KEY`),
                # so match the row's keys the same way a CSV header is matched.
                keys = list(raw.keys())
                pc_key = _match_column(keys, "postcode")
                uprn_key = _match_column(keys, "uprn")
                cert_key = _match_column(keys, "certificate")
                collected.append({
                    "postcode": raw.get(pc_key) if pc_key else None,
                    "uprn": raw.get(uprn_key) if uprn_key else "",
                    "certificate": (raw.get(cert_key) if cert_key
                                   else json.dumps(raw, sort_keys=True)),
                })
            next_url = ""
            for key in source.get("next_keys") or ():
                value = payload.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    next_url = value
                    break
                if isinstance(value, dict) and isinstance(value.get("next"), str):
                    next_url = value["next"]
                    break
            if next_url:
                continue
            page_param = str(source.get("page_param") or "page")
            # The service's own pagination envelope beats guessing from page
            # length: `nextPage` is the page to ask for, null on the last page.
            env_key = source.get("pagination_key")
            envelope = payload.get(env_key) if env_key else None
            if isinstance(envelope, dict):
                next_key = str(source.get("next_page_key") or "nextPage")
                size_key = str(source.get("page_size_key") or "")
                reported = envelope.get(size_key) if size_key else None
                if isinstance(reported, int) and reported > 0:
                    page_size = reported
                next_page = envelope.get(next_key)
                if isinstance(next_page, int) and next_page > 1:
                    params[page_param] = next_page
                    continue
                if next_key in envelope:
                    # Explicit end of the sequence (nextPage is null).
                    break
            # No envelope and no next link: a short page ends the term, a full
            # one asks for more.
            if len(batch) < page_size:
                break
            params[page_param] += 1

    records, _stats = aggregate_epc(collected)
    yield from records


REGISTER_SOURCES: Dict[str, Dict[str, Any]] = {
    "onspd": ONSPD_SOURCE,
    "onspd_area": ONSPD_AREA_SOURCE,
    "uprn": UPRN_SOURCE,
    "epc": EPC_SOURCE,
    "epc_api": EPC_API_SOURCE,
}


def epc_api_token(source: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> str:
    """The Bearer token for the EPC API, from the environment.

    Raises rather than returning empty: an unauthenticated call would come back
    403 and read like "this area has no certificates", which is the one answer a
    register loader must never invent.
    """
    env = os.environ if env is None else env
    for name in source.get("token_env") or ():
        value = str(env.get(name) or "").strip()
        if value:
            return value
    raise ValueError(
        "the EPC API needs a token: set "
        + " or ".join(source.get("token_env") or ()) 
        + " (the Bearer token on your EPC account page)"
    )

REGISTER_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "postcode": ("pcd", "pcd7", "pcds", "postcode", "post_code", "outcode", "zip"),
    "uprn": ("uprn", "uprn_id", "uprnno", "uprn_no", "unique_property_reference_number", "id"),
    "dwellings": ("dwellings", "dwellings_count", "dwell", "households", "household",
                  "hh", "hh_count", "no_of_dwellings", "num_dwellings", "units"),
    "population": ("pop01", "pop", "population", "pop21", "residents"),
    # EPC certificates are identified by LMK_KEY; the other spellings appear in
    # the API/derived extracts of the same archive.
    "certificate": ("lmk_key", "lmkkey", "certificate_number", "certificate",
                    "epc_id", "energy_performance_certificate_number"),
}


def _match_column(header: Sequence[str], field: str) -> Optional[str]:
    """The header cell that carries `field`, or None.

    Prefix matching, because the spellings are prefixes of one another
    (`pcd` / `pcd7` / `pcds`): the first alias that prefixes a header wins, so
    the shortest and most specific name is tried first.
    """
    wanted = sorted(REGISTER_COLUMN_ALIASES.get(field, ()) + (field,), key=len)
    # Match case-insensitively but return the header's OWN spelling: the caller
    # indexes a DictReader, whose keys are case-sensitive.  (EPC files shout
    # (`POSTCODE`, `UPRN`), ONSPD files do not (`pcd`), and both must resolve.)
    exact = {str(h or "").strip().lower(): str(h or "").strip() for h in header}
    for alias in wanted:
        if alias in exact:
            return exact[alias]
    for alias in wanted:
        for lower, original in exact.items():
            if lower.startswith(alias):
                return original
    return None


def parse_count(value: Any) -> Optional[int]:
    """A register count as an int, or None when the cell says nothing usable.

    A blank, a ``-``, and a non-numeric token are all "no answer" rather than
    zero, so the caller falls through to the OSM heuristic for that row instead
    of writing a household count of 0 over a real street of houses.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", ".", "0;0", "NA", "N/A", "null"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return int(round(float(m.group())))
    except (TypeError, ValueError):
        return None


def read_register_csv(
    handle: Any, source: Optional[Dict[str, Any]] = None
) -> Iterator[Dict[str, Any]]:
    """Yield one record per usable row of a register CSV.

    Each record is `{postcode, uprn?, households, population?}` with the
    postcode already normalised.  Pure: takes any text handle, so it is testable
    with no file, no database and no network.  Rows with no usable postcode or
    no usable count are skipped rather than defaulted -- a register row that
    cannot be placed must not be allowed to become a household count.
    """
    reader = csv.DictReader(handle)
    header = reader.fieldnames or []
    pc_col = _match_column(header, "postcode")
    uprn_col = _match_column(header, "uprn")
    dw_col = _match_column(header, "dwellings")
    pop_col = _match_column(header, "population")
    if not pc_col or not dw_col:
        raise ValueError(
            "register CSV needs a postcode column and a dwellings/households "
            f"column; got {list(header)!r}"
        )
    for raw in reader:
        if not raw:
            continue
        postcode = normalize_postcode(raw.get(pc_col))
        if not postcode:
            continue
        count = parse_count(raw.get(dw_col))
        if count is None or count < MIN_REGISTER_HOUSEHOLDS:
            continue
        uprn = str(raw.get(uprn_col) or "").strip() if uprn_col else ""
        rec: Dict[str, Any] = {"postcode": postcode, "households": count}
        if uprn:
            rec["uprn"] = uprn
        population = parse_count(raw.get(pop_col)) if pop_col else None
        if population is not None:
            rec["population"] = population
        yield rec


def aggregate_epc(
    rows: Iterable[Dict[str, Any]],
    *,
    areas: Optional[Sequence[str]] = None,
    keep_uprn: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Turn address-level EPC certificates into postcode register records.

    A certificate is one DWELLING, but a dwelling is re-certified over its life
    (a new boiler, a loft conversion), so counting certificates counts
    assessments, not homes.  The dwelling is identified by its UPRN, so the
    postcode figure is the number of DISTINCT UPRNs; rows with no UPRN fall back
    to distinct certificate ids, and how many did is reported rather than hidden.

    Returns `(records, stats)` where each record is either
      ``{postcode, households: <postcode total>}``            (one per postcode)
    or, when ``keep_uprn`` and the row carries one,
      ``{postcode, uprn, households: 1}``                     (one per dwelling)
    so a premise can be matched exactly by UPRN as well as apportioned by
    postcode.  `areas` filters to postcodes with any of those prefixes (e.g.
    ``["B16"]``), which is how a national archive is loaded for one project.
    """
    prefixes = tuple(str(a).strip().upper().replace(" ", "") for a in (areas or []) if a)
    dwellings: Dict[str, set] = {}
    certificates_without_uprn = 0
    kept = 0
    for row in rows:
        postcode = normalize_postcode(row.get("postcode"))
        if not postcode:
            continue
        if prefixes and not postcode.startswith(prefixes):
            continue
        uprn = str(row.get("uprn") or "").strip()
        cert = str(row.get("certificate") or "").strip()
        if not uprn and not cert:
            continue
        if not uprn:
            certificates_without_uprn += 1
        dwellings.setdefault(postcode, set()).add(uprn or f"cert:{cert}")
        kept += 1

    records: List[Dict[str, Any]] = []
    uprns: List[Dict[str, Any]] = []
    for postcode in sorted(dwellings):
        identities = dwellings[postcode]
        records.append({"postcode": postcode, "households": len(identities)})
        if keep_uprn:
            for ident in sorted(identities):
                if ident.startswith("cert:"):
                    continue
                uprns.append({"postcode": postcode, "uprn": ident, "households": 1})
    stats = {
        "certificates_read": kept,
        "postcodes": len(dwellings),
        "dwellings": sum(len(v) for v in dwellings.values()),
        "with_uprn": sum(1 for v in dwellings.values()
                         for i in v if not i.startswith("cert:")),
        "certificates_without_uprn": certificates_without_uprn,
    }
    # UPRN rows first so `load_register`'s first-row-wins postcode lookup still
    # sees a postcode TOTAL rather than one dwelling's `1`.
    return records + uprns, stats


def read_epc_csv(
    handle: Any,
    source: Dict[str, Any],
    areas: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Aggregate one EPC CSV handle into register records (see `aggregate_epc`).

    Pure: takes any text handle, so the whole national shape can be exercised in
    a test with a few lines of CSV and no download.
    """
    reader = csv.DictReader(handle)
    header = reader.fieldnames or []
    pc_col = _match_column(header, "postcode")
    if not pc_col:
        raise ValueError(
            "an EPC file needs a postcode column; "
            f"got {list(header)!r}"
        )
    uprn_col = _match_column(header, "uprn")
    cert_col = _match_column(header, "certificate")
    rows = (
        {
            "postcode": raw.get(pc_col),
            "uprn": raw.get(uprn_col) if uprn_col else "",
            "certificate": raw.get(cert_col) if cert_col else json.dumps(raw, sort_keys=True),
        }
        for raw in reader
        if raw
    )
    return aggregate_epc(rows, areas=areas)


def read_register_file(
    path: str, source: Optional[Dict[str, Any]] = None,
    areas: Optional[Sequence[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Read a register from a plain CSV, or from every CSV inside a zip.

    Some ONSPD/count releases ship as a zip of per-area CSVs; archive sizes vary,
    so the reader opens matching CSV members without requiring manual extraction.
    Current May 2026 ONSPD is geography-only and has no count field.
    """
    src = dict(source or ONSPD_SOURCE)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    # EPC is address-level and must be AGGREGATED to postcode totals, so it does
    # not go through the row-per-record reader: its files are collected, counted
    # by dwelling, and emitted as postcode/ UPRN records.
    epc = src.get("aggregates_to_postcode")
    if not zipfile.is_zipfile(p):
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            if epc:
                records, _stats = read_epc_csv(fh, src, areas)
                yield from records
                return
            yield from read_register_csv(fh, src)
        return
    pattern = str(src.get("zip_member_glob") or "*.csv")
    with zipfile.ZipFile(p) as zf:
        names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        if pattern != "*.csv":
            import fnmatch
            names = [n for n in names if fnmatch.fnmatch(n, pattern)] or names
        for name in names:
            with zf.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
                if epc:
                    records, _stats = read_epc_csv(text, src, areas)
                    yield from records
                    continue
                yield from read_register_csv(text, src)


# ---------------------------------------------------------------------------
# Storing it
# ---------------------------------------------------------------------------

_REGISTER_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.household_register (
    country_code TEXT NOT NULL,
    postcode     TEXT NOT NULL,
    uprn         TEXT NOT NULL DEFAULT '',
    households   INTEGER NOT NULL,
    population   INTEGER,
    source       TEXT NOT NULL,
    vintage      TEXT,
    licence      TEXT,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, postcode, uprn)
);
CREATE INDEX IF NOT EXISTS {schema}_household_register_uprn_idx
    ON {schema}.household_register (uprn);
"""


def init_register_schema() -> None:
    with postgis.get_conn().cursor() as cur:
        cur.execute(_REGISTER_DDL.format(schema=OSM_SCHEMA))


def register_purge(country_code: str, source: Optional[str] = None) -> int:
    """Delete a loaded register for a country (optionally one source).

    A source-scoped replace prevents a newer vintage from leaving withdrawn
    postcodes behind without deleting independent registers for the same country.
    """
    from countries import normalize_country_code
    if not postgis.is_available():
        return 0
    clauses = ["country_code = %s"]
    params: List[Any] = [normalize_country_code(country_code)]
    if source:
        clauses.append("source = %s")
        params.append(str(source))
    try:
        with postgis.get_conn().cursor() as cur:
            cur.execute(
                f"DELETE FROM {OSM_SCHEMA}.household_register WHERE " + " AND ".join(clauses),
                tuple(params),
            )
            return cur.rowcount or 0
    except Exception:  # noqa: BLE001 - table may not exist yet
        return 0


def register_ingest(
    records: Iterable[Dict[str, Any]],
    source: str,
    country_code: str = "GB",
    vintage: Optional[str] = None,
    licence: Optional[str] = None,
) -> int:
    """Load register rows into `osm.household_register`.

    Deliberately a function over an iterable, not a downloader -- the same
    contract as `boundary_dataset_ingest`, for the same reason: a register is a
    data/licence decision for the operator. Batched because a national register
    may contain millions of rows and the database is remote.
    """
    from countries import normalize_country_code
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    init_register_schema()
    code = normalize_country_code(country_code)
    total = 0
    chunk: List[Tuple[Any, ...]] = []
    for rec in records:
        postcode = normalize_postcode(rec.get("postcode"))
        count = parse_count(rec.get("households"))
        if not postcode or count is None or count < MIN_REGISTER_HOUSEHOLDS:
            continue
        chunk.append((
            code,
            postcode,
            str(rec.get("uprn") or "").strip(),
            int(count),
            parse_count(rec.get("population")),
            str(source),
            vintage,
            licence,
        ))
        if len(chunk) >= 5000:
            total += _register_insert(chunk)
            chunk = []
    if chunk:
        total += _register_insert(chunk)
    return total


def _register_insert(rows: Sequence[Tuple[Any, ...]]) -> int:
    from psycopg2.extras import execute_values
    sql = (
        f"INSERT INTO {OSM_SCHEMA}.household_register "
        "(country_code, postcode, uprn, households, population, source, vintage, licence) "
        "VALUES %s ON CONFLICT (country_code, postcode, uprn) DO UPDATE SET "
        "households = EXCLUDED.households, population = EXCLUDED.population, "
        "source = EXCLUDED.source, vintage = EXCLUDED.vintage, "
        "licence = EXCLUDED.licence, loaded_at = now()"
    )
    # A postcode can appear more than once in one batch -- two spellings of it,
    # or a postcode file plus a UPRN file for the same sector -- and PostgreSQL
    # rejects an ON CONFLICT statement that would touch the same row twice with
    # a CardinalityViolation, taking the whole batch with it.  Keep the LAST row
    # for each key, which is the same choice `_insert_rows` makes for the OSM
    # tables and the right one for a register: a later row is the later reading.
    unique = list({(r[0], r[1], r[2]): r for r in rows}.values())
    with postgis.get_conn().cursor() as cur:
        execute_values(cur, sql, unique, page_size=1000)
    return len(unique)


def ingest_source(
    slug: str,
    path: str = "",
    limit: Optional[int] = None,
    replace: bool = True,
    on_batch: Optional[Any] = None,
    areas: Optional[Sequence[str]] = None,
    postcodes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Fetch-and-load front end for one named register source (the CLI's entry).

    `path` is the operator's own count-bearing file. Area lookup sources are
    metadata/crosswalk-only and are rejected before any database write.
    """
    source = REGISTER_SOURCES.get(slug)
    if source is None:
        raise KeyError(slug)
    if source.get("has_dwellings_count") is False:
        raise ValueError(
            f"{slug} is a postcode geography lookup, not a dwelling-count source; "
            "it cannot be loaded into osm.household_register"
        )
    if source.get("kind") == "epc_api":
        # The API takes a postcode FILTER, so a project loads exactly the
        # postcodes it has -- either listed outright or given as outward codes.
        records = read_epc_api(source, list(postcodes or areas or []))
    elif source.get("remote"):
        records = read_remote_register(source, list(areas or []))
    else:
        if not path:
            raise ValueError(f"--source {slug} needs --file <path to the register>")
        records = read_register_file(path, source, areas)
    # Validate before replacing anything. In particular, current ONSPD has no
    # dwellings column; asking for a first usable row raises on its header while
    # the old register is still intact. An empty/zero-only source is also a
    # no-op rather than an accidental purge.
    records = iter(records)
    try:
        first_record = next(records)
    except StopIteration:
        first_record = None
    if first_record is None:
        return {
            "source": source["name"], "licence": source.get("licence"),
            "vintage": source.get("vintage"), "purged": 0, "loaded": 0,
            "kind": source.get("kind"), "country_code": source.get("country_code"),
            "areas": sorted(a.upper() for a in (areas or [])) or None,
        }
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    # Replace is source-scoped. A merge must not pass ``source=None`` here:
    # register_purge interprets that as "delete every register row for GB".
    purged = (
        register_purge(source["country_code"], source=str(source["name"]))
        if replace else 0
    )
    loaded = 0
    batch: List[Dict[str, Any]] = [first_record]
    for rec in records:
        batch.append(rec)
        if len(batch) >= 5000:
            loaded += register_ingest(
                batch, source=str(source["name"]),
                country_code=source["country_code"],
                vintage=source.get("vintage"), licence=source.get("licence"),
            )
            batch = []
            if on_batch:
                on_batch(loaded)
            if limit and loaded >= limit:
                break
    if batch and (not limit or loaded < limit):
        loaded += register_ingest(
            batch, source=str(source["name"]),
            country_code=source["country_code"],
            vintage=source.get("vintage"), licence=source.get("licence"),
        )
    if on_batch:
        on_batch(loaded)
    return {
        "source": source["name"],
        "licence": source.get("licence"),
        "vintage": source.get("vintage"),
        "purged": purged,
        "loaded": loaded,
        "kind": source.get("kind"),
        "country_code": source.get("country_code"),
        "areas": sorted(a.upper() for a in (areas or [])) or None,
    }


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------

def register_ready(country_code: str = "GB") -> bool:
    from countries import normalize_country_code
    if not postgis.is_available():
        return False
    try:
        with postgis.get_conn().cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{OSM_SCHEMA}.household_register",))
            if cur.fetchone()[0] is None:
                return False
            cur.execute(
                f"SELECT 1 FROM {OSM_SCHEMA}.household_register WHERE country_code = %s LIMIT 1",
                (normalize_country_code(country_code),),
            )
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - table may not exist yet
        return False


def _postcode_totals(rows: Sequence[Sequence[Any]]) -> Dict[str, int]:
    """Collapse register rows into `{postcode: households}`.

    A register row is EITHER a postcode total (no UPRN) or one dwelling keyed
    on its UPRN.  The two must not be added together: a postcode whose total
    row says 14 and whose 13 UPRN rows say 1 each has 14 dwellings, not 27.
    So a postcode's count is its total row when one exists, and the sum of its
    per-UPRN rows when it does not -- a plain ONSPD extract has no UPRN rows at
    all, while a UPRN-keyed file has only per-dwelling rows.

    Rows must already be ordered newest-first: the first total row for a
    postcode wins, so a newer vintage supersedes an older one instead of the
    two being summed into a number that describes neither.
    """
    totals: Dict[str, int] = {}
    per_uprn: Dict[str, int] = {}
    for row in rows:
        postcode = str(row[0] or "").strip()
        if not postcode:
            continue
        uprn = str(row[1] or "").strip()
        count = int(row[2] or 0)
        if uprn:
            per_uprn[postcode] = per_uprn.get(postcode, 0) + count
        else:
            totals.setdefault(postcode, count)
    by_postcode: Dict[str, int] = {}
    for postcode in set(totals) | set(per_uprn):
        by_postcode[postcode] = totals.get(postcode, per_uprn.get(postcode, 0))
    return by_postcode


def load_register(country_code: str, postcodes: Sequence[str]
                  ) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, Any]]:
    """`({postcode: households}, {uprn: households}, meta)` for the given postcodes.

    The lookup is by the area's own postcodes, so it costs one indexed query for
    the area rather than a scan of the full register.
    """
    from countries import normalize_country_code
    wanted = sorted({p for p in (normalize_postcode(pc) for pc in postcodes) if p})
    meta: Dict[str, Any] = {"available": False, "matched": 0, "requested": len(wanted)}
    if not wanted or not register_ready(country_code):
        return {}, {}, meta
    try:
        with postgis.get_conn().cursor() as cur:
            # The provenance comes back WITH the numbers, not from a separate
            # status call: a design's household count and the licence of the
            # dataset behind it have to be able to disagree about as little as
            # possible, and one query cannot.
            # The postcode TOTAL comes from a dedicated row (no UPRN), not from
            # whichever row a tie happened to return: every row in a batch
            # shares one `loaded_at`, so "the first row" was an arbitrary
            # dwelling's `1` rather than the postcode's count.  Aggregate the
            # rows instead of trusting their order (see `_postcode_totals`).
            cur.execute(
                f"SELECT postcode, uprn, households, source, licence, vintage "
                f"FROM {OSM_SCHEMA}.household_register "
                "WHERE country_code = %s AND postcode = ANY(%s) "
                "ORDER BY loaded_at DESC",
                (normalize_country_code(country_code), wanted),
            )
            fetched = cur.fetchall()
            by_postcode = _postcode_totals(fetched)
            if fetched:
                meta["source"] = fetched[0][3]
                meta["licence"] = fetched[0][4]
                meta["vintage"] = fetched[0][5]
            cur.execute(
                f"SELECT uprn, households FROM {OSM_SCHEMA}.household_register "
                "WHERE country_code = %s AND uprn <> ''",
                (normalize_country_code(country_code),),
            )
            by_uprn = {row[0]: int(row[1]) for row in cur.fetchall()}
    except Exception:  # noqa: BLE001 - report as unavailable, not as empty
        return {}, {}, meta
    meta["available"] = True
    meta["matched"] = len(by_postcode)
    return by_postcode, by_uprn, meta


def register_status(country_code: str = "GB") -> Dict[str, Any]:
    """What is loaded, for `--count` and the health block."""
    from countries import normalize_country_code
    if not postgis.is_available():
        return {"available": False, "rows": 0, "enabled": REGISTER_ENABLED}
    try:
        with postgis.get_conn().cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{OSM_SCHEMA}.household_register",))
            if cur.fetchone()[0] is None:
                return {"available": False, "rows": 0, "enabled": REGISTER_ENABLED,
                        "reason": "not_loaded"}
            cur.execute(
                f"SELECT source, count(*), count(*) FILTER (WHERE uprn <> ''), "
                f"       max(loaded_at) FROM {OSM_SCHEMA}.household_register "
                "WHERE country_code = %s GROUP BY source",
                (normalize_country_code(country_code),),
            )
            sources = [
                {"source": r[0], "rows": int(r[1]), "uprn_rows": int(r[2]),
                 "loaded_at": r[3].isoformat(timespec="seconds")
                 if hasattr(r[3], "isoformat") else str(r[3])}
                for r in cur.fetchall()
            ]
    except Exception:  # noqa: BLE001
        return {"available": False, "rows": 0, "enabled": REGISTER_ENABLED}
    return {
        "available": bool(sources),
        "rows": sum(s["rows"] for s in sources),
        "enabled": REGISTER_ENABLED,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Applying it -- the part that decides what "wins" means
# ---------------------------------------------------------------------------

# Provenance values written to household_method. They are deliberately distinct from
# the OSM-derived methods, so `household_summary` can tell a REGISTERED
# household from a tagged one from a guessed one -- which is the whole point:
# the preview reports `estimated_share`, and a register count is not an estimate
# in the sense that matters (it is a published dwellings count, not a rule).
REGISTER_METHODS = frozenset({"register_postcode", "register_uprn"})

# What a register number IS, said where a planner will actually read it. Kept
# short because it goes into a preview warning verbatim.
REGISTER_CAVEAT = (
    "A register count is a published DWELLINGS count for the postcode, not a "
    "census of occupied homes and not a count of people."
)


def _weighted_split(total: int, weights: Sequence[int]) -> List[int]:
    """Split `total` across `weights` so the parts sum to `total` exactly.

    Largest-remainder apportionment, with a floor of 1 per part (every premise
    is at least one household) and the leftover going to the largest fractional
    claim.  The exact-sum property matters: the register total is the authority,
    so the parts must reconcile to it rather than merely be close -- a design
    that reports 512 households from a 500-household register is wrong in the
    one number the register was loaded to fix.
    """
    n = len(weights)
    if n == 0:
        return []
    if total <= n:
        # Not enough households to give every premise one: floor everything at 1
        # and let the caller record the shortfall rather than zeroing a premise.
        return [1] * n
    w = [max(0.0, float(x)) for x in weights]
    s = sum(w)
    if s <= 0:
        w = [1.0] * n
        s = float(n)
    base = [1] * n
    remaining = total - n
    shares = [remaining * (wi / s) for wi in w]
    floors = [int(math.floor(x)) for x in shares]
    out = [base[i] + floors[i] for i in range(n)]
    left = remaining - sum(floors)
    # Largest fractional part first, ties by index so the result is stable.
    order = sorted(range(n), key=lambda i: (-(shares[i] - floors[i]), i))
    for i in order[:left]:
        out[i] += 1
    return out


def apply_register(
    premises: Sequence[Dict[str, Any]],
    by_postcode: Dict[str, int],
    by_uprn: Optional[Dict[str, int]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Replace heuristic household counts with register counts where they exist.

    A COPY of the premise list is returned; the input is not mutated, because a
    caller may want the heuristic for comparison (and the tests do).

    Precedence, per physical service-location record:

      1. a UPRN match (register row keyed on this location's UPRN) --
         `household_method = register_uprn`, and its households are taken OUT of the
         postcode total so the two cannot double-count
      2. the register's postcode total, less whatever the UPRN matches already
         claimed, apportioned across that postcode's remaining premises by their
         existing estimate -- `household_method = register_postcode`
      3. otherwise the physical location is left exactly as OSM produced it

    Why apportion rather than assign: a register is keyed on the POSTCODE, and
    one postcode is typically tens to hundreds of service locations, so there is
    no per-location number to read off. Assigning the postcode total to each
    location would multiply the households by the location count. The register
    therefore fixes the postcode's TOTAL, and the existing per-location estimate only
    decides the shape within it -- the estimator keeps doing the one job it is
    good at (a 12-storey block really is more homes than a bungalow) and stops
    deciding the thing it is bad at (how many homes a postcode has).

    `stats` reports what happened, including the cases that must not be silent:
    a postcode where the register has fewer households than the area has
    service locations (`below_premise_count`) cannot be honoured without either
    zeroing a location or inflating the total, so the total wins where it can and the
    shortfall is reported.
    """
    rows = [dict(p) for p in premises]
    stats: Dict[str, Any] = {
        "enabled": False,
        "postcodes_matched": 0,
        "postcodes_seen": 0,
        "premises_registered": 0,
        "register_households": 0,
        "heuristic_households_before": 0,
        "heuristic_households_after": 0,
        "below_premise_count": 0,
        "shortfall": 0,
    }
    if not by_postcode and not by_uprn:
        return rows, stats

    by_uprn = by_uprn or {}
    stats["enabled"] = True
    stats["heuristic_households_before"] = sum(int(p.get("households") or 0) for p in rows)

    # Group the premise indices by normalised postcode, preserving order.
    groups: Dict[str, List[int]] = {}
    for i, p in enumerate(rows):
        pc = normalize_postcode(p.get("Postcode"))
        if pc:
            groups.setdefault(pc, []).append(i)
    stats["postcodes_seen"] = len(groups)

    registered: set = set()
    for pc, idxs in groups.items():
        total = by_postcode.get(pc)
        if total is None or total < MIN_REGISTER_HOUSEHOLDS:
            continue
        stats["postcodes_matched"] += 1
        # A UPRN on a premise is the one place the register is per-premise, so
        # it is read first.  Its households come OUT of the postcode total
        # rather than being added to it: the postcode total is the number for
        # the whole postcode, and a UPRN-matched premise is one of the premises
        # in it.  Leaving them in the pool would apportion the full total
        # across the remaining premises and report more households than the
        # register states.
        pool = list(idxs)
        claimed = 0
        for i in list(idxs):
            uprn = str(rows[i].get("UPRN") or "").strip()
            if uprn and uprn in by_uprn:
                rows[i]["households"] = int(by_uprn[uprn])
                rows[i]["household_method"] = "register_uprn"
                registered.add(i)
                claimed += int(by_uprn[uprn])
                pool.remove(i)
        remaining_total = int(total) - claimed
        if not pool:
            continue
        if remaining_total < len(pool):
            # The register has fewer households than the area has service
            # locations in this postcode. Honour the total's floor (1 each) and report the
            # gap rather than quietly inflating: the register is right and the
            # premise set is the thing that disagrees.
            stats["below_premise_count"] += 1
            stats["shortfall"] += len(pool) - remaining_total
        weights = [int(rows[i].get("households") or 0) or 1 for i in pool]
        parts = _weighted_split(remaining_total, weights)
        for i, value in zip(pool, parts):
            rows[i]["households"] = int(value)
            rows[i]["household_method"] = "register_postcode"
            registered.add(i)

    stats["premises_registered"] = len(registered)
    stats["register_households"] = sum(int(rows[i].get("households") or 0) for i in registered)
    stats["heuristic_households_after"] = sum(int(p.get("households") or 0) for p in rows)
    return rows, stats


def register_premise_provenance(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The block a preview/build payload carries about the register itself."""
    if not meta or not meta.get("available"):
        return None
    return {
        "available": True,
        "enabled": REGISTER_ENABLED,
        "postcodes_matched": meta.get("matched"),
        "postcodes_asked": meta.get("requested"),
        "source": meta.get("source"),
        "licence": meta.get("licence"),
        "vintage": meta.get("vintage"),
        "caveat": REGISTER_CAVEAT,
    }
