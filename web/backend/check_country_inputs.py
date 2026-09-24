# -*- coding: utf-8 -*-
"""Check the generated inputs for a spread of countries, one area each.

The question this answers is not "does it work in Germany" but "does an area in
ANY country produce what stage 01 (`BuildObjectLayer`) needs": a workbook whose
ten keys `EXPECTED_MAP` declares all resolve, `Country`/`City` populated from the
resolution rather than a literal, coordinates the object layer will accept without
geocoding, and an objects layer that carries the OSM tags.

It is a script rather than a test on purpose: it hits the live services and the
database, so it must never run on import in CI.  `--size-only` is the cheap first
pass -- boundary resolution only, no OSM fetch -- which is how a candidate list
gets narrowed before paying for a cold fetch per country.

    python check_country_inputs.py --size-only
    python check_country_inputs.py --countries FR,US,GB --json tmp/matrix.json

Run it with PYTHONPATH unset (the engine's own convention): a global PYTHONPATH
drags QGIS's Python312 site-packages in and breaks numpy/pandas.  The repo .env is
loaded by the script itself, so the database is the one the servers use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


import osm_source  # noqa: E402

# The DB is remote (see .env), so the environment has to be loaded before any
# query.  One loader, in the module that owns the connection, so this script and
# ingest_boundaries.py cannot drift apart on which database they mean.
ENV_FILE = osm_source.load_env_file(BACKEND_DIR)

# The plugin's alias table, parsed as source: sheet_utils imports pandas at module
# level, and the ten keys are what stage 01 actually looks for.
# BACKEND_DIR is HLD_Planning_01/web/backend, so the package root is parents[1].
SHEET_UTILS = BACKEND_DIR.parents[1] / "HLDPlanning" / "utils" / "sheet_utils.py"

# One candidate per country, chosen to be SMALL: a village, commune or parish
# rather than a city, because a cold fetch is paid per country.  Several are
# deliberately near the size limit -- if a candidate turns out to be a 300 km2
# municipality, the size pass says so before anything is fetched.
# The size pass matters: a municipality is not a village.  Measured on the first
# pass, IE Cong was 152 km2, NO Roros 1,944 km2 and PT Monsaraz 464 km2 because
# Nominatim matches the whole municipality -- so those were replaced with smaller
# places rather than paid for.
CANDIDATES: List[Dict[str, str]] = [
    {"code": "DE", "area_name": "Mariendorf", "city": "Berlin"},
    {"code": "FR", "area_name": "Giverny"},
    {"code": "GB", "area_name": "New Frankley in Birmingham"},
    {"code": "IE", "area_name": "Adare", "city": "Limerick"},
    {"code": "NL", "area_name": "Marken"},
    {"code": "ES", "area_name": "Mogarraz"},
    {"code": "IT", "area_name": "Civita di Bagnoregio"},
    {"code": "PL", "area_name": "Kazimierz Dolny"},
    {"code": "TR", "area_name": "Sogutlu", "city": "Mugla"},
    {"code": "US", "area_name": "Telluride", "city": "Colorado"},
    {"code": "CA", "area_name": "Banff", "city": "Alberta"},
    # "Real de Catorce" alone matched a STREET of that name in Pachuca, Hidalgo --
    # 0.003 km2 with 0 buildings, so the pipeline refused with no_premises.  The
    # town is in San Luis Potosi, so the city has to be stated.
    {"code": "MX", "area_name": "Real de Catorce", "city": "San Luis Potosí"},
    {"code": "BR", "area_name": "Tiradentes", "city": "Minas Gerais"},
    {"code": "ZA", "area_name": "Greyton", "city": "Western Cape"},
    {"code": "IN", "area_name": "Mahabalipuram"},
    # Yufuin resolved to 25,731 premises -- over the run cap -- because Nominatim
    # matched the whole municipality.  An island town is the small unit in Japan.
    {"code": "JP", "area_name": "Naoshima"},
    {"code": "AU", "area_name": "Hahndorf", "city": "South Australia"},
]


def plugin_keys() -> List[str]:
    """The ten keys stage 01 resolves, from the plugin's own alias table."""
    if not SHEET_UTILS.is_file():
        return []
    source = SHEET_UTILS.read_text(encoding="utf-8")
    block = re.search(r"EXPECTED_MAP\s*=\s*\{(.*?)\n\}", source, flags=re.DOTALL)
    if not block:
        return []
    return re.findall(r'"([a-z_]+)"\s*:\s*\[', block.group(1))


def resolve_keys(alias_source: str, headers: List[str]) -> List[str]:
    """Which of the plugin's keys this workbook's headers satisfy, by name."""
    found: List[str] = []
    lower = {str(h).lower(): h for h in headers if h}
    block = re.search(r"EXPECTED_MAP\s*=\s*\{(.*?)\n\}", alias_source, flags=re.DOTALL)
    for entry in re.finditer(r'"([a-z_]+)"\s*:\s*\[(.*?)\]', block.group(1), flags=re.DOTALL):
        aliases = re.findall(r'"([^"]+)"', entry.group(2))
        if any(a.lower() in lower for a in aliases):
            found.append(entry.group(1))
    return found


def label_for(cand: Dict[str, str]) -> str:
    return osm_source.compose_area(
        area_name=cand.get("area_name", ""),
        postcode=cand.get("postcode", ""),
        city=cand.get("city", ""),
        country_code=cand.get("code", ""),
    )


def size_one(cand: Dict[str, str]) -> Dict[str, Any]:
    """Boundary only: no OSM fetch, so this is cheap enough for a long list."""
    label = label_for(cand)
    started = time.time()
    try:
        preview = osm_source.preview_area(
            label, boundary_only=True, country_code=cand["code"],
            input_type=osm_source.input_type_for(
                area=label, area_name=cand.get("area_name", ""),
                postcode=cand.get("postcode", ""),
            ),
            postcode=cand.get("postcode", ""),
        )
        return {
            "country": cand["code"], "label": label, "ok": True,
            "area_km2": preview.get("area_km2"), "rung": preview.get("polygon_source"),
            "matched": preview.get("matched"),
            "postcode_is_boundary": preview.get("postcode_is_boundary"),
            "seconds": round(time.time() - started, 1),
        }
    except Exception as exc:  # noqa: BLE001 - a failure IS the result here
        return {
            "country": cand["code"], "label": label, "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:160],
            "seconds": round(time.time() - started, 1),
        }


def full_one(cand: Dict[str, str], out_root: Path) -> Dict[str, Any]:
    """Resolve, fetch, write the workbook, then check it and the objects layer."""
    label = label_for(cand)
    started = time.time()
    row: Dict[str, Any] = {"country": cand["code"], "label": label}
    try:
        input_type = osm_source.input_type_for(
            area=label, area_name=cand.get("area_name", ""),
            postcode=cand.get("postcode", ""),
        )
        preview = osm_source.preview_area(
            label, country_code=cand["code"], input_type=input_type,
            postcode=cand.get("postcode", ""),
        )
        row.update({
            "rung": preview.get("polygon_source"),
            "area_km2": preview.get("area_km2"),
            "matched": preview.get("matched"),
            "country_from_resolution": preview.get("country"),
            "city_from_resolution": preview.get("city"),
            "premises": (preview.get("premises") or {}).get("count"),
            "households": (preview.get("households") or {}).get("total"),
            "can_run": preview.get("can_run"),
            "run_blocked_reason": preview.get("run_blocked_reason"),
            "roads_km": (preview.get("roads") or {}).get("total_km"),
            # How the premises were derived, and the caveats the planner was
            # shown.  A count alone cannot say whether the object layer will get
            # addresses or centroids, which is the thing that actually varies
            # by country.
            "from_address_node": (preview.get("premises") or {}).get("from_address_node"),
            "from_building_centroid": (preview.get("premises") or {}).get("from_building_centroid"),
            # Of the centroid premises: no address at all, vs an address taken
            # from the building polygon.  These are different designs, and the
            # gap is what the object layer actually works with.
            "without_address": (preview.get("premises") or {}).get("without_address"),
            "from_building_polygon": (preview.get("premises") or {}).get("from_building_polygon"),
            "household_estimated_share": (preview.get("households") or {}).get("estimated_share"),
            "postcode_is_boundary": preview.get("postcode_is_boundary"),
            "boundary": {
                "source": preview.get("boundary_source"),
                "name": preview.get("boundary_name"),
                "code": preview.get("boundary_code"),
                "kind": preview.get("boundary_kind"),
                "licence": preview.get("boundary_licence"),
                "vintage": preview.get("boundary_vintage"),
                "note": preview.get("boundary_note"),
            },
            "warnings": preview.get("warnings") or [],
        })

        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
        project = f"countrycheck-{cand['code'].lower()}-{slug}" if slug else f"countrycheck-{cand['code'].lower()}"
        built = osm_source.build_inputs(
            project, label, str(out_root / project),
            country_code=cand["code"], input_type=input_type,
            postcode=cand.get("postcode", ""),
        )
        row["workbook"] = built.get("excel_path")
    except Exception as exc:  # noqa: BLE001
        # A refusal for size is a working cap, not a broken input: Japan's
        # municipalities are large, so Yufuin came back as 25,731 premises and the
        # run cap stopped it.  Recorded separately so it is not read as a defect.
        if isinstance(exc, osm_source.TooManyPremises):
            row.update({"ok": False, "over_cap": True, "premises": exc.count,
                        "run_cap": exc.cap,
                        "error": f"over the {exc.cap}-premise run cap ({exc.count} premises)",
                        "seconds": round(time.time() - started, 1)})
            return row
        row.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                    "seconds": round(time.time() - started, 1)})
        return row

    # --- the workbook, read back the way stage 01 reads it ---
    try:
        from openpyxl import load_workbook

        wb = load_workbook(row["workbook"], read_only=True)
        rows = wb.active.iter_rows(values_only=True)
        headers = [c for c in next(rows) if c is not None]
        data = list(rows)
        idx = {h: i for i, h in enumerate(headers)}

        def col(name: str) -> List[Any]:
            return [r[idx[name]] for r in data] if name in idx else []

        def filled(name: str) -> int:
            return sum(1 for v in col(name) if v not in (None, ""))

        alias_source = SHEET_UTILS.read_text(encoding="utf-8")
        keys = resolve_keys(alias_source, headers)
        expected = plugin_keys()

        invalid = 0
        for r in data:
            try:
                lat, lon = float(r[idx["LATITUDE"]]), float(r[idx["LONGITUDE"]])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 85)):
                    invalid += 1
            except Exception:  # noqa: BLE001
                invalid += 1

        # Share, not just a count: "1,204 of 1,204" and "3 of 9,000" are both
        # "has a value", and only the second says the object layer is working
        # off centroids.
        n = max(1, len(data))

        def share(name: str) -> float:
            return round(filled(name) / n, 3)

        methods: Dict[str, int] = {}
        for v in col("HH_METHOD"):
            key = str(v or "").strip() or "(blank)"
            methods[key] = methods.get(key, 0) + 1

        row["workbook_check"] = {
            "rows": len(data),
            "keys_satisfied": len(keys),
            "keys_expected": len(expected),
            "missing_keys": sorted(set(expected) - set(keys)),
            "country_values": sorted({str(v) for v in col("Country") if v})[:3],
            "city_values": sorted({str(v) for v in col("City") if v})[:3],
            "country_filled": filled("Country"),
            "city_filled": filled("City"),
            "address_filled": filled("Address"),
            "housenumber_filled": filled("Housenumber"),
            "postcode_filled": filled("Postcode"),
            "district_filled": filled("District"),
            "address_share": share("Address"),
            "housenumber_share": share("Housenumber"),
            "postcode_share": share("Postcode"),
            "district_share": share("District"),
            "hh_methods": dict(sorted(methods.items(), key=lambda kv: -kv[1])),
            "invalid_coords": invalid,
            "blank_rows": sum(1 for r in data if all(v in (None, "") for v in r)),
        }
    except Exception as exc:  # noqa: BLE001
        row["workbook_check"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # --- the objects layer: every building, with its tags ---
    try:
        osm_source.ensure_area_data(label, preview["bbox"])
        resolution = osm_source.resolve_area(
            label, country_code=cand["code"], input_type=input_type,
            postcode=cand.get("postcode", ""),
        )
        objects = osm_source.input_layer_geojson(
            resolution["polygon"], "objects",
            country=resolution.get("country") or "", city=resolution.get("city") or "",
        )
        feats = objects.get("features") or []
        tag_keys: Dict[str, int] = {}
        untagged = 0
        for f in feats:
            props = f.get("properties") or {}
            if not props:
                untagged += 1
            for k in props:
                tag_keys[k] = tag_keys.get(k, 0) + 1
        row["objects"] = {
            "features": len(feats),
            "distinct_attributes": len(tag_keys),
            "features_with_no_attributes": untagged,
            "top_attributes": sorted(tag_keys.items(), key=lambda kv: -kv[1])[:8],
        }
    except Exception as exc:  # noqa: BLE001
        row["objects"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    row["ok"] = True
    row["seconds"] = round(time.time() - started, 1)
    return row


def _is_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return "429" in text or "Too many requests" in text


def attempt(fn, attempts: int, delay: float):
    """Run `fn`, backing off when the service says it is being hammered.

    Nominatim allows about one request a second and Overpass throttles hard: a
    long candidate list WILL be rate limited, and the useful answer is a slower
    run rather than a column of 429s.
    """
    last: BaseException | None = None
    for n in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limited(exc):
                raise
            last = exc
            pause = delay * (2 ** n)
            print(f"      rate limited, waiting {pause:.0f}s "
                  f"(attempt {n + 1}/{attempts})", flush=True)
            time.sleep(pause)
    raise last if last else RuntimeError("attempt failed")


def pick(candidates: List[Dict[str, str]], countries: str) -> List[Dict[str, str]]:
    if not countries:
        return candidates
    wanted = {c.strip().upper() for c in countries.split(",") if c.strip()}
    return [c for c in candidates if c["code"].upper() in wanted]


def parse_areas(spec: str) -> List[Dict[str, str]]:
    """Explicit candidates, for an audit that is not the country matrix.

    Format: `area|city|CC|postcode`, semicolon-separated; city and postcode may
    be empty (`Nanyuki||KE`).  The country matrix above is fixed on purpose --
    it is one candidate per country so a profile stays comparable -- so an
    ad-hoc global check passes its areas in rather than editing that list.
    """
    out: List[Dict[str, str]] = []
    for chunk in spec.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if not any(parts):
            continue
        while len(parts) < 4:
            parts.append("")
        area_name, city, code, postcode = parts[:4]
        out.append({"code": code.upper(), "area_name": area_name,
                    "city": city, "postcode": postcode})
    return [c for c in out if c["area_name"] or c["postcode"]]


def main() -> int:
    # A Japanese area resolves to a name like "直島町": the Windows console is
    # cp1252 and printing that would abort the whole pass after the work was done.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # not a real console / already wrapped
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--countries", default="", help="comma-separated ISO codes")
    ap.add_argument("--areas", default="",
                    help="explicit candidates instead of the matrix: "
                         "'area|city|CC|postcode' separated by ';' (city/postcode optional)")
    ap.add_argument("--size-only", action="store_true",
                    help="boundary resolution only: no OSM fetch, no workbook")
    ap.add_argument("--max-km2", type=float, default=0.0,
                    help="in --size-only mode, only report candidates up to this size")
    ap.add_argument("--out", default="outputs", help="where workbooks are written")
    ap.add_argument("--json", default="", help="write the rows to this JSON file")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between candidates, and the base backoff")
    ap.add_argument("--attempts", type=int, default=4,
                    help="tries per candidate before recording a failure")
    args = ap.parse_args()

    candidates = parse_areas(args.areas) if args.areas.strip() else pick(CANDIDATES, args.countries)
    out_root = Path(args.out)
    rows: List[Dict[str, Any]] = []

    mode = "size only (no fetch)" if args.size_only else "full (fetch + workbook + objects)"
    print(f"checking {len(candidates)} area(s) — {mode}")
    # Say which database this is about to use, and whether it answers: the
    # postgis_unavailable failure below is otherwise indistinguishable from an
    # outage when it is really a missing .env.
    print(f"  env:    {ENV_FILE or 'no .env found (database defaults to localhost)'}")
    print(f"  cty_db: {os.environ.get('PGHOST', 'localhost')}:"
          f"{os.environ.get('PGPORT', '5432')}/{os.environ.get('PGDATABASE', 'ftth')}"
          f" -> available={osm_source.postgis.is_available()}\n")
    for cand in candidates:
        print(f"  {cand['code']} {label_for(cand)}", flush=True)
        try:
            row = attempt(
                lambda c=cand: size_one(c) if args.size_only else full_one(c, out_root),
                attempts=args.attempts, delay=args.delay,
            )
        except Exception as exc:  # noqa: BLE001 - the failure IS the result
            row = {"country": cand["code"], "label": label_for(cand), "ok": False,
                   "error": f"{type(exc).__name__}: {exc}"[:160]}
        rows.append(row)
        # Written after EVERY candidate, not just at the end: a cold fetch is ~100 s
        # per area and a long list will outlive one tool call, so partial evidence
        # has to survive being interrupted.
        if args.json:
            Path(args.json).write_text(
                json.dumps(rows, indent=2, default=str), encoding="utf-8")
        time.sleep(args.delay)
        if args.size_only:
            if row.get("ok"):
                flag = "OK  " if (args.max_km2 <= 0 or (row.get("area_km2") or 1e9) <= args.max_km2) else "BIG "
                print(f"{flag}{row['country']}  {str(row.get('area_km2')):>9} km2  "
                      f"{str(row.get('rung')):<14} {str(row.get('matched'))[:52]}")
            else:
                print(f"FAIL {row['country']}  {row.get('error')}")
            continue

        check = row.get("workbook_check") or {}
        obj = row.get("objects") or {}
        if row.get("ok") and check.get("keys_satisfied") == check.get("keys_expected"):
            print(f"OK   {row['country']}  {row.get('area_km2')} km2  {row.get('rung')}  "
                  f"premises={row.get('premises')}  keys={check.get('keys_satisfied')}/"
                  f"{check.get('keys_expected')}  country={check.get('country_values')}  "
                  f"objects={obj.get('features')} attrs={obj.get('distinct_attributes')}  "
                  f"({row.get('seconds')}s)")
            print(f"     filled: house_no={check.get('housenumber_share')} "
                  f"address={check.get('address_share')} postcode={check.get('postcode_share')} "
                  f"district={check.get('district_share')}  \n"
                  f"     derived: addr_nodes={row.get('from_address_node')} "
                  f"centroid={row.get('from_building_centroid')} "
                  f"est_hh={row.get('household_estimated_share')}")
        elif row.get("over_cap"):
            print(f"CAP  {row['country']}  {row.get('premises')} premises > "
                  f"cap {row.get('run_cap')} — too large to run, not an input defect")
        else:
            print(f"FAIL {row['country']}  {row.get('error') or check or obj}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
