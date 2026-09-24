# -*- coding: utf-8 -*-
"""Load authoritative boundary datasets into `osm.boundary_areas`.

An explicit operator step, not something a request triggers.  The datasets are
open-licensed but large, and which one to trust is a decision this code should
not make on its own -- `boundary_dataset_ingest` takes an iterable for exactly
that reason, and this script is the fetch-and-load front end for the named
sources in `osm_source`.

Why it exists: in OpenStreetMap a UK postcode is not a boundary at all, it is a
tag on address points.  So a Birmingham postcode resolves to the enclosing city
(266.9 km², 257,127 premises) and is refused as too large, and a district name
like "Handsworth" resolves to the same city because OSM carries no ward polygons
inside it.  The ONS ward polygons are what make either of them narrow.

    python ingest_boundaries.py --list
    python ingest_boundaries.py --source uk-wards
    python ingest_boundaries.py --source uk-wards --limit 200 --verify=-1.9360,52.5140
    python ingest_boundaries.py --count

A re-ingest REPLACES a source's polygons for its country rather than merging
them: `boundary_dataset_ingest` upserts on (country_code, code), so a newer ONS
vintage would update the wards that still exist and silently leave an abolished
ward behind.  `--keep-existing` opts out of that, and is only useful for adding a
second country to a shared source.

Run it with PYTHONPATH unset (the engine's own convention): a global PYTHONPATH
drags QGIS's Python312 site-packages in and breaks numpy/pandas.  The repo .env
is loaded by the script itself, so it writes to the database the servers use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import osm_source  # noqa: E402

ENV_FILE = osm_source.load_env_file(BACKEND_DIR)

# Every source this tool can load, by the slug you pass to --source, with the
# ingest function that fetches and loads it.  Both ingest functions share one
# signature (limit, page_size, replace) so the CLI needs no special cases.
SOURCES: Dict[str, Dict[str, Any]] = {
    "uk-wards": osm_source.UK_WARDS_SOURCE,
    "us-zcta": osm_source.US_ZCTA_SOURCE,
}

INGESTERS: Dict[str, Any] = {
    "uk-wards": osm_source.ingest_uk_wards,
    "us-zcta": osm_source.ingest_us_zctas,
}


def loaded_rows(country_code: str, kind: Optional[str] = None) -> Optional[int]:
    """How many polygons for this country/kind are in the database right now."""
    if not osm_source.postgis.is_available() or not osm_source.schema_ready():
        return None
    try:
        clauses = ["country_code = %s"]
        params: List[Any] = [osm_source.normalize_country_code(country_code)]
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        rows = osm_source._query(
            f"SELECT count(*) AS n FROM {osm_source.OSM_SCHEMA}.boundary_areas "
            f"WHERE " + " AND ".join(clauses),
            tuple(params),
        )
    except Exception:  # noqa: BLE001 - reported as unknown, not as zero
        return None
    return int(rows[0]["n"]) if rows else 0


def describe(slug: str, source: Dict[str, Any]) -> str:
    have = loaded_rows(str(source.get("country_code") or ""), source.get("kind"))
    loaded = "unknown" if have is None else f"{have} loaded"
    return (
        f"{slug:<10} {source.get('name')}\n"
        f"{'':<10} kind={source.get('kind')} country={source.get('country_code')} "
        f"vintage={source.get('vintage')} ({loaded})\n"
        f"{'':<10} publisher: {source.get('publisher')}\n"
        f"{'':<10} licence:   {source.get('licence')}"
    )


def parse_point(text: str) -> Optional[tuple]:
    parts = [p for p in str(text or "").replace(" ", "").split(",") if p]
    if len(parts) != 2:
        return None
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return lon, lat


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the known sources and stop")
    ap.add_argument("--source", default="uk-wards", help=f"one of: {', '.join(SOURCES)}")
    ap.add_argument("--count", action="store_true", help="report what is loaded and stop")
    ap.add_argument("--limit", type=int, default=0,
                    help="load at most N polygons (a smoke test, not a full load)")
    ap.add_argument("--page-size", type=int, default=2000,
                    help="records per request to the source service")
    ap.add_argument("--keep-existing", action="store_true",
                    help="merge instead of replacing this source's polygons")
    ap.add_argument("--start-offset", type=int, default=0,
                    help="skip the first N records: resume an interrupted load "
                         "(pair it with --keep-existing)")
    # Wrapped in `=` when the longitude is negative, e.g. --verify=-1.9360,52.5140:
    # argparse otherwise reads the leading "-" as the start of another option.
    ap.add_argument("--verify", default="",
                    help="after loading, report the loaded polygon containing LON,LAT "
                         "(write --verify=LON,LAT when LON is negative)")
    args = ap.parse_args()

    print(f"env:    {ENV_FILE or 'no .env found (database defaults to localhost)'}")
    print(f"db:     {osm_source.os.environ.get('PGHOST', 'localhost')}:"
          f"{osm_source.os.environ.get('PGPORT', '5432')}/"
          f"{osm_source.os.environ.get('PGDATABASE', 'ftth')} "
          f"-> available={osm_source.postgis.is_available()}\n")

    if args.list:
        for slug, source in SOURCES.items():
            print(describe(slug, source))
            print()
        return 0

    if args.count:
        for slug, source in SOURCES.items():
            have = loaded_rows(source["country_code"], source.get("kind"))
            print(f"{slug:<10} {have if have is not None else 'unknown'} "
                  f"{source.get('kind')} polygon(s) for {source.get('country_code')}")
        return 0

    source = SOURCES.get(args.source)
    if not source:
        print(f"unknown source {args.source!r}; try --list", file=sys.stderr)
        return 2

    if not osm_source.postgis.is_available():
        print(
            "PostGIS is not reachable, so there is nothing to load into. Check the "
            "database settings above (the DB is remote: see .env).",
            file=sys.stderr,
        )
        return 3

    print(describe(args.source, source))
    print()
    print(f"loading {'all' if not args.limit else args.limit} polygon(s)...", flush=True)
    result = INGESTERS[args.source](
        limit=args.limit or None,
        page_size=args.page_size,
        replace=not args.keep_existing,
        on_batch=lambda n: print(f"  loaded {n}...", flush=True),
        start_offset=args.start_offset,
    )
    print(f"purged {result.get('purged')} previous polygon(s), "
          f"loaded {result.get('loaded')}")
    if args.keep_existing:
        print("(kept the existing polygons: --keep-existing)")

    have = loaded_rows(source["country_code"], source.get("kind"))
    print(f"now {have if have is not None else 'unknown'} "
          f"{source.get('kind')} polygon(s) for {source.get('country_code')}")

    point = parse_point(args.verify)
    if point:
        lon, lat = point
        hit = osm_source.boundary_dataset_containing(
            source["country_code"], lon, lat, kind=source.get("kind")
        )
        if hit:
            print(f"verify ({lon}, {lat}) -> {hit.get('name')} "
                  f"({hit.get('code')}), {hit.get('area_km2')} km², "
                  f"from {hit.get('source')}")
        else:
            print(f"verify ({lon}, {lat}) -> no loaded polygon contains this point",
                  file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
