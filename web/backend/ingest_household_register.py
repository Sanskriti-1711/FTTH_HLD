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
        f"{'':<8} kind={source.get('kind')} country={source.get('country_code')} "
        f"({rows})\n"
        f"{'':<8} publisher: {source.get('publisher')}\n"
        f"{'':<8} licence:   {source.get('licence')}\n"
        f"{'':<8} ON by default? yes for GB/UK — set OSM_HOUSEHOLD_REGISTER=0 to ignore a loaded register\n"
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
    args = ap.parse_args()

    print(f"env:    {ENV_FILE or 'no .env found (database defaults to localhost)'}")
    print(f"db:     {osm_source.os.environ.get('PGHOST', 'localhost')}:"
          f"{osm_source.os.environ.get('PGPORT', '5432')}/"
          f"{osm_source.os.environ.get('PGDATABASE', 'ftth')} "
          f"-> available={osm_source.postgis.is_available()}")
    print(f"on:     {household_register.REGISTER_ENABLED} "
          f"({household_register.REGISTER_ENV_NAMES[0]}="
          f"{'1' if household_register.REGISTER_ENABLED else '0'})\n")

    status = household_register.register_status("GB")

    if args.list:
        for slug, source in household_register.REGISTER_SOURCES.items():
            print(describe(slug, source, status))
            print()
        return 0

    if args.count:
        print(json_status(status))
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
    if not osm_source.postgis.is_available():
        print("PostGIS is not reachable, so there is nothing to load into. Check the "
              "database settings above.", file=sys.stderr)
        return 3

    print(describe(args.source, source, status))
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
    )
    print()
    print(f"purged {result.get('purged')} previous row(s), loaded {result.get('loaded')}")
    if result.get("areas"):
        print(f"areas:  {', '.join(result['areas'])} (the rest of the release was not read)")
    print(f"source:  {result.get('source')}")
    print(f"licence: {result.get('licence')}")
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
