"""A pluggable EXTERNAL household register, so UK household counts are measured.

The household rule in `osm_source` is a heuristic and is honest about it: on
real Berlin data it was ~100 % `fallback_one` / `levels_x_footprint`, because
OpenStreetMap carries `building:flats` on 1 building in 15,583 and `addr:flats`
on none.  Trunk sizing, HH-based physical-location drop capacity, and the BOQ rest on
that heuristic, and
`HH_METHOD` travels with each row so nobody mistakes it for a survey.

The UK is the case where this is *fixable with real data*, because the UK
publishes an address-level register of dwellings:

* **ONSPD** (ONS Postcode Directory) carries a `Dwellings` count for every one
  of the ~2.7 M UK postcodes.  Free, quarterly, OGL v3.
* **UPRN** (OS Open UPRN / OS Open Names) gives a unique identifier and a
  coordinate for every addressable location -- ~40 M of them -- and AddressBase
  carries per-UPRN dwelling counts.  UPRNs are the join key that makes a
  register address-accurate rather than postcode-accurate.

So the register is keyed on the **postcode**, with an optional **UPRN** column
for operators who have an address-level extract.  Both are ingest-time operator
steps (`ingest_household_register.py`), never something a request downloads --
same rule as the boundary datasets: which dataset, and under which licence, is
not a decision this code should make on its own.

    python ingest_household_register.py --list
    python ingest_household_register.py --source onspd --file ONSPD_*.zip
    python ingest_household_register.py --source uprn --file uprns.csv

What "wins" means is stated precisely in `apply_register`, and it is the whole
point of the module: a register count REPLACES the heuristic for the premises it
covers, `HH_METHOD` says so (`register_postcode` / `register_uprn`), and the
register's own source, licence and vintage travel with the run.
"""

from __future__ import annotations

import csv
import io
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
# Default OFF, and deliberately so: a register changes the number the cable
# sizing and the BOQ are computed from, so it is a decision an operator makes
# and can be measured both ways.  Same convention as the other `OSM_*` knobs
# (see OSM_PAVEMENT_CARRIERS / OSM_PAVEMENT_ARTERIALS in osm_source).
REGISTER_ENABLED = os.environ.get("OSM_HOUSEHOLD_REGISTER", "0").strip().lower() \
    not in ("0", "false", "no", "off")

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
        "The ONSPD Dwellings field is a count of DWELLINGS in a postcode, not a "
        "survey of how many are occupied and not a count of people. A vacant or "
        "second-home dwelling is counted, so this is an upper bound on connected "
        "homes in a fully-built-up postcode and the closest published measure "
        "rather than a measured one."
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

REGISTER_SOURCES: Dict[str, Dict[str, Any]] = {
    "onspd": ONSPD_SOURCE,
    "uprn": UPRN_SOURCE,
}


# Column spellings accepted for each field, lower-cased and stripped. A
# downloaded ONSPD spells them `pcd` / `Dwellings` / `Pop01`; an operator's own
# extract spells them `postcode` / `households` / `hh`. Being strict about the
# ONSPD spelling only would mean a hand-made CSV silently loads zero rows,
# which reads exactly like "the register has no data for this area".
REGISTER_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "postcode": ("pcd", "pcd7", "pcds", "postcode", "post_code", "outcode", "zip"),
    "uprn": ("uprn", "uprn_id", "uprnno", "uprn_no", "unique_property_reference_number", "id"),
    "dwellings": ("dwellings", "dwellings_count", "dwell", "households", "household",
                  "hh", "hh_count", "no_of_dwellings", "num_dwellings", "units"),
    "population": ("pop01", "pop", "population", "pop21", "residents"),
}


def _match_column(header: Sequence[str], field: str) -> Optional[str]:
    """The header cell that carries `field`, or None.

    Prefix matching, because the spellings are prefixes of one another
    (`pcd` / `pcd7` / `pcds`): the first alias that prefixes a header wins, so
    the shortest and most specific name is tried first.
    """
    wanted = sorted(REGISTER_COLUMN_ALIASES.get(field, ()) + (field,), key=len)
    cells = [str(h or "").strip().lower() for h in header]
    for alias in wanted:
        for cell in cells:
            if cell == alias:
                return cell
    for alias in wanted:
        for cell in cells:
            if cell.startswith(alias):
                return cell
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


def read_register_file(
    path: str, source: Optional[Dict[str, Any]] = None
) -> Iterator[Dict[str, Any]]:
    """Read a register from a plain CSV, or from every CSV inside a zip.

    The ONSPD ships as one zip of per-area CSVs, so a zip is the normal input
    for it and requiring the operator to unpack a ~700 MB download by hand would
    make the documented command wrong.
    """
    src = dict(source or ONSPD_SOURCE)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if not zipfile.is_zipfile(p):
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
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

    A replace rather than a merge, for the same reason `boundary_dataset_purge`
    is: a newer vintage must not leave a withdrawn postcode behind still serving
    a household count.
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
    contract as `boundary_dataset_ingest`, for the same reason: ONSPD is a
    ~700 MB quarterly download under a licence, and which vintage to trust is an
    operator decision.  Batched, because the full ONSPD is ~2.7 M rows and the
    database is remote.
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
    path: str,
    limit: Optional[int] = None,
    replace: bool = True,
    on_batch: Optional[Any] = None,
) -> Dict[str, Any]:
    """Fetch-and-load front end for one named register source (the CLI's entry)."""
    source = REGISTER_SOURCES.get(slug)
    if source is None:
        raise KeyError(slug)
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    purged = register_purge(
        source["country_code"], source=str(source["name"]) if replace else None
    )
    records = read_register_file(path, source)
    loaded = 0
    batch: List[Dict[str, Any]] = []
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


def load_register(country_code: str, postcodes: Sequence[str]
                  ) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, Any]]:
    """`({postcode: households}, {uprn: households}, meta)` for the given postcodes.

    The lookup is by the area's own postcodes, so it costs one indexed query for
    the area rather than a scan of ~2.7 M register rows.
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
            cur.execute(
                f"SELECT postcode, households, source, licence, vintage "
                f"FROM {OSM_SCHEMA}.household_register "
                "WHERE country_code = %s AND postcode = ANY(%s) "
                "ORDER BY loaded_at DESC",
                (normalize_country_code(country_code), wanted),
            )
            by_postcode: Dict[str, int] = {}
            for row in cur.fetchall():
                if row[0] in by_postcode:
                    continue
                by_postcode[row[0]] = int(row[1])
                meta["source"] = row[2]
                meta["licence"] = row[3]
                meta["vintage"] = row[4]
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

# Provenance values written to HH_METHOD. They are deliberately distinct from
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
         `HH_METHOD = register_uprn`, and its households are taken OUT of the
         postcode total so the two cannot double-count
      2. the register's postcode total, less whatever the UPRN matches already
         claimed, apportioned across that postcode's remaining premises by their
         existing estimate -- `HH_METHOD = register_postcode`
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
    stats["heuristic_households_before"] = sum(int(p.get("HH") or 0) for p in rows)

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
                rows[i]["HH"] = int(by_uprn[uprn])
                rows[i]["HH_METHOD"] = "register_uprn"
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
        weights = [int(rows[i].get("HH") or 0) or 1 for i in pool]
        parts = _weighted_split(remaining_total, weights)
        for i, value in zip(pool, parts):
            rows[i]["HH"] = int(value)
            rows[i]["HH_METHOD"] = "register_postcode"
            registered.add(i)

    stats["premises_registered"] = len(registered)
    stats["register_households"] = sum(int(rows[i].get("HH") or 0) for i in registered)
    stats["heuristic_households_after"] = sum(int(p.get("HH") or 0) for p in rows)
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
