# -*- coding: utf-8 -*-
"""Download Eurostat GISCO LAU data — the population half of a European register.

Run this FIRST, then load what it wrote:

    python fetch_gisco_lau.py --list
    python fetch_gisco_lau.py --year 2024 --country DE --out lau_2024_DE.csv
    python ingest_household_register.py --source gisco --file lau_2024_DE.csv --country DE

Why two steps rather than one: the download needs no database, and the load
needs no network but does need the country's postcode polygons already in
PostGIS. Splitting them means a fetch failure and a geometry failure are told
apart, and the file in between is inspectable — an operator can look at the
population that is about to become household counts before any of it is loaded.

The API is public. It needs no key and no institutional licence (verified
2026-10-16: both the vintage index and the per-year CSV answer 200 to an
anonymous GET). A previous revision of this integration claimed a 403 /
institutional-access requirement; that was incorrect — the request URL was
malformed, and certificate verification had been switched off process-wide,
which disguised it.

`--country` is worth passing even though the whole European file is only a few
MB: 34 countries are published and 29 of them are irrelevant to a German job.
What is written is a LAU CSV in GISCO's own format, so the loader reads it back
with no renaming and no second download.

Output is UTF-8. GISCO municipality names carry accents, and a cp1252 console
would fail the write rather than the read if this printed them directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gisco_lau  # noqa: E402


def _default_out(year: str, country: str) -> str:
    tag = f"_{country}" if country else ""
    return f"lau_{year}{tag}.csv"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--list", action="store_true",
                    help="show the published LAU vintages and stop")
    ap.add_argument("--years", action="store_true",
                    help="print a comma-separated list of vintage years and stop")
    ap.add_argument("--year", default="",
                    help="vintage to fetch (default: the latest published)")
    ap.add_argument("--country", default="",
                    help="ISO country code to keep, e.g. DE; omit for all 34")
    ap.add_argument("--out", default="", help="output CSV path")
    ap.add_argument("--srid", type=int, default=4326,
                    help="EPSG code of the file to fetch (default 4326)")
    ap.add_argument("--gpkg", action="store_true",
                    help="fetch the GeoPackage of BOUNDARIES instead of the CSV "
                         "of populations. The register load needs both: the CSV "
                         "says how many people live in a municipality, the GPKG "
                         "says where the municipality is. Defaults to EPSG:3035 "
                         "for the GPKG, because it is equal-area so areas are "
                         "real square metres.")
    args = ap.parse_args()

    try:
        catalogue = gisco_lau.catalogue()
    except gisco_lau.LauError as exc:
        print(f"cannot reach the GISCO distribution API: {exc}", file=sys.stderr)
        print("The API is public, so this is a network/proxy problem, not a "
              "licence one.", file=sys.stderr)
        return 3

    # The catalogue is already in hand, so the newest vintage is read off it
    # rather than re-requested via `latest_year()`.
    vintage_years = sorted(k.rsplit("-", 1)[-1] for k in catalogue if k.startswith("lau-"))
    latest = vintage_years[-1] if vintage_years else ""

    if args.list:
        print(f"{len(vintage_years)} LAU vintage(s) published:\n")
        for key in sorted(catalogue):
            info = catalogue[key] or {}
            print(f"  {key:<10} {info.get('title') or ''}  (dated {info.get('date') or '?'})")
        print(f"\nFetch one with:  python fetch_gisco_lau.py --year {latest} --country DE")
        return 0

    if args.years:
        print(",".join(vintage_years))
        return 0

    year = args.year or latest
    if f"lau-{year}" not in catalogue:
        print(f"no LAU vintage {year!r}; try --list", file=sys.stderr)
        return 2

    country = (args.country or "").strip().upper()

    if args.gpkg:
        # A GPKG is whole-Europe whatever the country, and 3035 is the right
        # default for it: the polygons are only ever used to measure areas, and
        # 3035 is the equal-area projection where an area is square metres.
        srid = args.srid if args.srid != 4326 else 3035
        path = args.out or f"lau_{year}_{srid}.gpkg"
        print(f"vintage: {year}{' (latest)' if year == latest else ''}")
        print(f"kind:    GeoPackage of LAU boundaries, EPSG:{srid}")
        print(f"out:     {path}")
        print("downloading ...", flush=True)
        try:
            got = gisco_lau.download_lau_gpkg(year, path, srid)
        except gisco_lau.LauError as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return 3
        print(f"\nwrote {got['path']} ({got['bytes']:,} bytes)")
        print(f"url:   {got['url']}")
        print("\nNext:  python ingest_household_register.py --source gisco "
              f"--country {country or 'DE'} --lau-gpkg {got['path']} --file lau_{year}_...csv")
        return 0

    out = args.out or _default_out(year, country)

    print(f"vintage: {year}{' (latest)' if year == latest else ''}")
    print(f"country: {country or 'all published'}")
    print(f"out:     {out}")
    print("downloading ...", flush=True)

    try:
        result = gisco_lau.download_lau_csv(year, out, country or None, args.srid)
    except gisco_lau.LauError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 3

    print(f"\nwrote {result['path']} ({result['bytes']:,} bytes, {result['rows']} row(s))")
    print(f"url:   {result['url']}")

    if not result["rows"]:
        # A country with no LAU in a vintage is a real answer (the UK left the
        # collection after 2020) but it is never a success for the caller.
        print(f"\nNo LAU row for {country or 'any country'} in {year}. Nothing to load.",
              file=sys.stderr)
        return 4

    try:
        with open(result["path"], encoding="utf-8-sig", errors="replace", newline="") as fh:
            records = list(gisco_lau.parse_lau_csv(fh, country or None, year))
    except gisco_lau.LauError as exc:
        print(f"the downloaded file did not parse back: {exc}", file=sys.stderr)
        return 5

    published = [r for r in records if r["population"]]
    print(f"\n{len(records)} municipalities (LAU)")
    print(f"  with a published population: {len(published)}")
    if published:
        print(f"  population total: {sum(r['population'] or 0 for r in published):,}")
    if len(published) < len(records):
        print(f"  {len(records) - len(published)} have no published figure and are "
              "left blank rather than treated as zero")

    print("\nNext:  python ingest_household_register.py "
          f"--source gisco --file {result['path']}"
          + (f" --country {country}" if country else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
