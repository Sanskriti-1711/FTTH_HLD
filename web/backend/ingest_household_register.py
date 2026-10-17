# -*- coding: utf-8 -*-
"""Load an EXTERNAL household register into `osm.household_register`.

An explicit operator step, not something a request triggers, for the same reason
`ingest_boundaries.py` is one: the UK datasets are large, quarterly, and each
carries a licence, and which vintage to trust is not a decision this code should
make on its own.

Why it exists: OSM household counts are heuristic, and the register can only use
a source that answers "how many dwellings are at this postcode". There is no free
postcode-keyed count of that shape: the current ONSPD is a geography lookup, OS
Open UPRN carries identifiers/coordinates but no count (a block of flats is one
UPRN), AddressBase Premium is licensed, and VOA Council Tax stock is published at
LA/LSOA/MSOA only. Census 2021 RM204 is area-level, so it must not be loaded
until a transparent OA-to-postcode allocation is designed.

The one free source that IS address-level is **EPC domestic certificates**: one
certificate per dwelling, at postcode + UPRN, under the Open Government Licence
(England & Wales; Scotland publishes separately). `--source epc` reads that
archive and AGGREGATES it to postcode totals -- distinct UPRNs per postcode, with
a per-UPRN row kept so a premise can also be matched by address. The EPC bulk
download needs a GOV.UK One Login (and the developer API a registered key), so the
file is operator-supplied:

    python ingest_household_register.py --list
    python ingest_household_register.py --count
    python ingest_household_register.py --source epc_api --postcodes "B16 9BH,B17 1AA"
    python ingest_household_register.py --source epc --file <epc-certificates.zip or .csv> --areas B16,B17
    python ingest_household_register.py --source onspd --file <count-bearing-release.zip>
    python ingest_household_register.py --source uprn --file <licensed-count-bearing-uprn.csv>

`--areas B16,B17` for EPC keeps only postcodes with those prefixes, so a national
archive can be loaded for one project instead of the whole country (address-level
files run into millions of rows). The EPC counts DWELLINGS, not occupied
households, and only dwellings that have been assessed; the age of the archive is
reported so the number is never mistaken for a survey.

**The API is the better route for one project**, and needs no multi-GB download:
`--source epc_api` queries the certificates for the postcodes you name (`--postcodes
"B16 9BH,B17 1AA"`, or `--areas B16,B17` as outward codes) and aggregates them
through the exact same code as the bulk file. Register free at
https://get-energy-performance-data.communities.gov.uk/ and set `EPC_API_TOKEN` to
the Bearer token shown on your account page.

`--source onspd_area --areas B` is not an ingest path: it is only a published
postcode/geography lookup, and the loader refuses it. Current May 2026 ONSPD has
no dwellings column, so passing that archive to `--source onspd` is also rejected
before any existing rows are deleted.

**Outside the UK, `--source gisco` is the route.**  There is no postcode-keyed
count for Europe, but Eurostat GISCO publishes a real POPULATION for every
municipality (LAU).  It is not shaped like a register, so a load is three steps
and all three are needed:

    python fetch_gisco_lau.py --year 2024 --country DE --out lau_2024_DE.csv
    python fetch_gisco_lau.py --year 2024 --country DE --gpkg --out lau_2024_3035.gpkg
    python ingest_household_register.py --source gisco --country DE \\
        --lau-gpkg lau_2024_3035.gpkg --file lau_2024_DE.csv

The GPKG supplies the municipality BOUNDARIES, the CSV supplies the populations,
and the load shares each municipality's population across the postcode polygons
of that country already in `osm.boundary_areas`.  Two consequences worth knowing
before running it: the country's postcode boundaries must be loaded FIRST (the
loader refuses with that message rather than reporting an empty country), and the
resulting counts are ESTIMATES -- population spread evenly within a municipality,
divided by Eurostat's average household size (ilc_lvph01) -- so every row is
marked and the CLI prints the assumptions.  The GISCO licence also restricts the
data to NON-COMMERCIAL use.

A re-ingest replaces only that named source's rows. `--keep-existing` merges
without purging existing rows from any source.

The load is what supplies the counts: the design reads it by default for a
GB/UK area. Set `OSM_HOUSEHOLD_REGISTER=0` to build an area without it and
compare the two.

Run it with PYTHONPATH unset (the engine's own convention): a global PYTHONPATH
drags QGIS's Python312 site-packages in and breaks numpy/pandas. The repo .env is
loaded by the script itself, so it writes to the database the servers use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gisco_lau  # noqa: E402
import household_register  # noqa: E402
import osm_source  # noqa: E402

ENV_FILE = osm_source.load_env_file(BACKEND_DIR)

# `REGISTER_ENABLED` is read at import time, which happens BEFORE the .env above
# is loaded -- so without this the `--list` header said the register was OFF even
# when `.env` switched it on. The design itself reads the env at run time and was
# never affected; only this report was.
household_register.REGISTER_ENABLED = household_register.register_enabled_from_env()


def describe(slug: str, source: Dict[str, Any], status: Dict[str, Any]) -> str:
    rows = "unknown"
    for entry in status.get("sources") or []:
        if entry.get("source") == source.get("name"):
            rows = f"{entry.get('rows')} loaded ({entry.get('uprn_rows')} with a UPRN)"
    return (
        f"{slug:<8} {source.get('name')}\n"
        f"{'':<8} kind={source.get('kind')} "
        # GISCO carries no single country, so showing a blank one would read as
        # "unset" rather than "you must choose one".
        f"country={source.get('country_code') or '--country (34 published)'} "
        f"({rows})\n"
        f"{'':<8} publisher: {source.get('publisher')}\n"
        f"{'':<8} licence:   {source.get('licence')}\n"
        f"{'':<8} ON by default? yes, for every country it covers — set "
        f"OSM_HOUSEHOLD_REGISTER=0 to ignore a loaded register\n"
        f"{'':<8} what it is: {source.get('note')}"
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the known sources and stop")
    ap.add_argument("--count", action="store_true", help="report what is loaded and stop")
    ap.add_argument("--source", default="onspd",
                    help=f"one of: {', '.join(household_register.REGISTER_SOURCES)}")
    ap.add_argument("--file", default="", help="the register file: a CSV, or a zip of CSVs (ONSPD)")
    ap.add_argument("--areas", default="",
                    help="postcode AREAS to pull from the ONSPD geography lookup "
                         "(e.g. B,EH); this source cannot be loaded as household counts")
    ap.add_argument("--postcodes", default="",
                    help="comma-separated full postcodes to query (the EPC API "
                         "source); --areas is accepted for it as outward codes")
    ap.add_argument("--limit", type=int, default=0,
                    help="load at most N rows (a smoke test, not a full load)")
    ap.add_argument("--keep-existing", action="store_true",
                    help="merge instead of replacing this source's rows")
    ap.add_argument("--country", default="",
                    help="ISO country code; required for --source gisco, which "
                         "publishes 34 countries and cannot default to one")
    ap.add_argument("--lau-gpkg", default="",
                    help="GISCO LAU GeoPackage to load the municipality polygons "
                         "from before building gisco rows (fetch it with "
                         "fetch_gisco_lau.py --gpkg)")
    args = ap.parse_args()

    print(f"env:    {ENV_FILE or 'no .env found (database defaults to localhost)'}")
    print(f"db:     {osm_source.os.environ.get('PGHOST', 'localhost')}:"
          f"{osm_source.os.environ.get('PGPORT', '5432')}/"
          f"{osm_source.os.environ.get('PGDATABASE', 'ftth')} "
          f"-> available={osm_source.postgis.is_available()}")
    print(f"on:     {household_register.REGISTER_ENABLED} "
          f"({household_register.REGISTER_ENV_NAMES[0]}="
          f"{'1' if household_register.REGISTER_ENABLED else '0'})\n")

    # Status is read PER COUNTRY. Asking a GB-only question and reporting the
    # answer for a German load shows "unknown" rows for a register that is
    # loaded, which is the one thing a status report must never do.
    requested_country = (args.country or "").strip().upper()
    statuses: Dict[str, Dict[str, Any]] = {}
    for code in sorted({"GB"} | ({requested_country} if requested_country else set())):
        statuses[code] = household_register.register_status(code)

    def status_for(source: Dict[str, Any]) -> Dict[str, Any]:
        code = str(source.get("country_code") or requested_country or "GB").strip().upper()
        return statuses.get(code) or statuses["GB"]

    if args.list:
        for slug, source in household_register.REGISTER_SOURCES.items():
            print(describe(slug, source, status_for(source)))
            print()
        print(f"row counts above are for {requested_country or 'GB'}; pass "
              "--country <CC> to report another country's load")
        return 0

    if args.count:
        print(json_status(statuses.get(requested_country) or statuses["GB"]))
        return 0

    source = household_register.REGISTER_SOURCES.get(args.source)
    if source is None:
        print(f"unknown source {args.source!r}; try --list", file=sys.stderr)
        return 2
    areas = [a for a in (args.areas or "").replace(" ", "").split(",") if a]
    postcodes = [p.strip() for p in (args.postcodes or "").split(",") if p.strip()]
    is_api = source.get("kind") == "epc_api"
    if source.get("has_dwellings_count") is False:
        print(f"{args.source} is a postcode geography lookup with no dwelling "
              "count; it cannot be loaded into osm.household_register.", file=sys.stderr)
        return 2
    if source.get("remote") and not is_api:
        print(f"--source {args.source} is a geography lookup, not a household-count source.",
              file=sys.stderr)
        return 2
    if is_api:
        if not (postcodes or areas):
            print(f"--source {args.source} needs --postcodes <p1,p2> (or --areas "
                  "<outward codes>)", file=sys.stderr)
            return 2
        try:
            household_register.epc_api_token(source)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if not is_api and not args.file:
        print(f"--source {args.source} needs --file <count-bearing register>", file=sys.stderr)
        return 2
    # Argument checks come BEFORE the database check: a missing --country is a
    # typed command line, and being told to fix the database instead is the wrong
    # answer, if it is an answer at all.
    country = (args.country or "").strip().upper()
    if source.get("needs_country") and not country:
        print(f"--source {args.source} needs --country <ISO code> (GISCO publishes "
              f"{len(household_register.POSTCODE_SHAPES)} countries and cannot "
              "default to one)",
              file=sys.stderr)
        return 2
    if args.lau_gpkg and not country:
        print("--lau-gpkg needs --country", file=sys.stderr)
        return 2
    if not osm_source.postgis.is_available():
        print("PostGIS is not reachable, so there is nothing to load into. Check the "
              "database settings above.", file=sys.stderr)
        return 3

    # The LAU polygons are a PREREQUISITE of the apportionment, so they are
    # loaded here, before any register row is built -- not lazily inside the
    # builder, where a geometry failure would surface as a confusing "no
    # overlaps" halfway through a load.
    if args.lau_gpkg:
        print(f"loading LAU polygons from {args.lau_gpkg} for {country} ...", flush=True)
        try:
            rows = gisco_lau.read_lau_geometries(args.lau_gpkg, country)
            loaded_geoms = household_register.load_gisco_lau_geometry(rows, country)
        except (gisco_lau.LauError, OSError) as exc:
            print(f"could not read {args.lau_gpkg}: {exc}", file=sys.stderr)
            return 4
        print(f"  {loaded_geoms} LAU polygon(s) loaded into "
              f"{household_register.OSM_SCHEMA}.{household_register.GISCO_LAU_TABLE}")
        if not loaded_geoms:
            print(f"no LAU polygon for {country} in that file; nothing to apportion "
                  "onto.", file=sys.stderr)
            return 4

    print(describe(args.source, source, status_for(source)))
    print()
    print(f"loading {args.postcodes or args.file} ...", flush=True)

    def _progress(n: int) -> None:
        print(f"  loaded {n}...", flush=True)

    result = household_register.ingest_source(
        args.source,
        args.file,
        limit=args.limit or None,
        replace=not args.keep_existing,
        on_batch=_progress,
        areas=areas or None,
        postcodes=postcodes or None,
        country_code=country or None,
    )
    print()
    print(f"purged {result.get('purged')} previous row(s), loaded {result.get('loaded')}")
    if result.get("areas"):
        print(f"areas:  {', '.join(result['areas'])} (the rest of the release was not read)")
    print(f"source:  {result.get('source')}")
    # The vintage is the file's own, so it is worth printing: two loads of the
    # same country a year apart are told apart by this line and nothing else.
    print(f"vintage: {result.get('vintage')}")
    print(f"licence: {result.get('licence')}")
    estimate = result.get("estimate")
    if estimate:
        # Said out loud, and last, because it is the one thing about this load a
        # planner must not miss: every number above is derived, not counted, and
        # the two assumptions behind it are named rather than implied.
        print()
        print("ESTIMATED counts -- these are NOT measured dwellings:")
        print(f"  basis:  {estimate.get('basis')}")
        size_note = ("published for this country"
                     if estimate.get("household_size_published")
                     else "EU27 fallback -- no national figure in the table")
        print(f"  household size: {estimate.get('persons_per_household')} persons "
              f"({size_note})")
        print(f"  municipalities: {estimate.get('municipalities')} "
              f"({estimate.get('with_population')} with a published population)")
        print(f"  postcode polygons: {estimate.get('postcode_polygons')}, "
              f"overlaps measured: {estimate.get('overlaps')}")
        if estimate.get("clamped_lau_totals"):
            print(f"  {estimate['clamped_lau_totals']} municipality/municipalities "
                  "whose overlaps exceeded their own area were scaled back to 100%")
        print("  The source licence restricts this data to NON-COMMERCIAL use.")
    if not household_register.REGISTER_ENABLED:
        print("\nThe register is loaded but NOT in use. Set "
              f"{household_register.REGISTER_ENV_NAMES[0]}=1 and "
              "restart the engine to make it win over the OSM heuristic.")
    return 0


def json_status(status: Dict[str, Any]) -> str:
    if not status.get("available"):
        return ("no register is loaded. That is the default state, not an error: "
                "load an operator-supplied CSV/ZIP with a verified dwelling-count "
                "column; current ONSPD and OS Open UPRN do not provide the required counts.")
    lines = [f"{status.get('rows')} register row(s) loaded, in use: {status.get('enabled')}"]
    for entry in status.get("sources") or []:
        lines.append(
            f"  {entry.get('source')}: {entry.get('rows')} row(s), "
            f"{entry.get('uprn_rows')} with a UPRN, loaded {entry.get('loaded_at')}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
