# -*- coding: utf-8 -*-
"""Load an EXTERNAL household register into `osm.household_register`.

An explicit operator step, not something a request triggers, for the same reason
`ingest_boundaries.py` is one: the UK datasets are large, quarterly, and each
carries a licence, and which vintage to trust is not a decision this code should
make on its own.

Why it exists: the household rule in `osm_source` is a heuristic, and on real
UK data it is almost entirely `fallback_one` -- OpenStreetMap carries
`building:flats` on 1 building in 15,583 and `addr:flats` on none, so trunk
sizing and the BOQ for a generated UK area rest on `UNIT_AREA_M2` rather than
on a count. The UK publishes the count: ONSPD carries
a `Dwellings` figure for every postcode, and a UPRN extract carries one per
addressable location. This loads either, and `OSM_HH_REGISTER=1` then makes the
loaded number win over the heuristic for the postcodes it covers.

    python ingest_household_register.py --list
    python ingest_household_register.py --count
    python ingest_household_register.py --source onspd --file "D:/ONSPD_2026_05.zip"
    python ingest_household_register.py --source onspd --file ONSPD.zip --limit 50000
    python ingest_household_register.py --source uprn --file uprns.csv

A re-ingest REPLACES the source's rows for its country rather than merging, so a
newer vintage cannot leave a withdrawn postcode still serving a household count.
`--keep-existing` opts out.

The load does nothing on its own: `OSM_HH_REGISTER=1` is what turns it on, so an
area can be built with and without the register and the two compared.

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
        f"{'':<8} ON by default? no — set OSM_HH_REGISTER=1 to use a loaded register\n"
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
          f"(OSM_HH_REGISTER={'1' if household_register.REGISTER_ENABLED else '0'})\n")

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
    if not args.file:
        print(f"--source {args.source} needs --file <path to the register>", file=sys.stderr)
        return 2
    if not osm_source.postgis.is_available():
        print("PostGIS is not reachable, so there is nothing to load into. Check the "
              "database settings above.", file=sys.stderr)
        return 3

    print(describe(args.source, source, status))
    print()
    print(f"loading {args.file} ...", flush=True)

    def _progress(n: int) -> None:
        print(f"  loaded {n}...", flush=True)

    result = household_register.ingest_source(
        args.source,
        args.file,
        limit=args.limit or None,
        replace=not args.keep_existing,
        on_batch=_progress,
    )
    print()
    print(f"purged {result.get('purged')} previous row(s), loaded {result.get('loaded')}")
    print(f"source:  {result.get('source')}")
    print(f"licence: {result.get('licence')}")
    if not household_register.REGISTER_ENABLED:
        print("\nThe register is loaded but NOT in use. Set OSM_HH_REGISTER=1 and "
              "restart the engine to make it win over the OSM heuristic.")
    return 0


def json_status(status: Dict[str, Any]) -> str:
    if not status.get("available"):
        return ("no register is loaded. That is the default state, not an error: "
                "load one with --source onspd --file <ONSPD zip>.")
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
