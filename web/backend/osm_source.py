"""Area name -> HLD inputs, fetched and cached automatically.

The platform's HLD pipeline takes exactly two files: an address workbook
(`Main_DataSet.xlsx`) and a roads dataset.  This module produces both from an
area name, so a user can type "Mariendorf, Berlin, Germany" instead of
preparing the dataset by hand.

    area name
        |  Nominatim /search  (one request per AREA, not per premise)
        v
    polygon + bbox
        |  Overpass, scoped to that bbox, cached in the local `osm` schema
        v
    buildings / address points / roads / landuse   (a local PostGIS OSM store)
        |  premises + estimated households
        v
    inputs/Main_DataSet.xlsx + inputs/roads.geojson  -> the existing pipeline

Design notes that matter:

* **Nothing here is a manual setup step.**  The first call for an area fetches
  what it needs and writes it into the local `osm` schema; every later call for
  an overlapping area is served from that store.  `osm2pgsql` + a Geofabrik PBF
  remains a supported way to fill the same tables in bulk (see
  docs/subprojects/ftth-engine/OSM_AREA_INPUTS.md) -- it is an optimisation, not
  a prerequisite.
* **Premises are NOT geocoded one by one.** They come out of the OSM store,
  which is why an area costs a single Nominatim lookup. The old manual path
  geocoded 285 addresses at 1.2 s each inside stage 01.
* **The household rule is the accuracy risk.** It drives trunk sizing, the
  HH-based capacity on each physical-location drop cable, and the BOQ, so every
  service-location record carries the method that produced its load and the
  preview reports the mix. It is never presented as measured.
* The premise -> household logic is deliberately split into PURE functions
  (`parse_flats`, `estimate_buildings`, `distribution_for`, `assemble_premises`)
  that take dicts and a mapping, so they are unit-testable with no database and
  no network.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import postgis
import household_register
from countries import country_name, country_options, normalize_country_code

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# Public endpoints are flaky, and which one responds varies over time -- the
# same mirror list and fallback the permit loader uses.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Nominatim's usage policy requires an identifying User-Agent.
USER_AGENT = "fiber-ftth-engine/1.0 (FTTH HLD area inputs)"

OSM_SCHEMA = "osm"
EXTRACT_SOURCE = "overpass"


def load_env_file(start: Optional[Path] = None) -> Optional[Path]:
    """Fill missing environment variables from the repo `.env`, and return its path.

    The database is NOT localhost: it is whatever `.env` points at, while
    `postgis.py` falls back to `localhost:5432/ftth` on an empty environment.
    Without this, an operator script run from a bare shell reports
    `postgis_unavailable` against a database that is perfectly healthy -- which
    reads exactly like an outage and cost me a wrong diagnosis once.  A variable
    that is already set wins, so one value can still be overridden for one run.

    The servers do not need this (start-servers.sh sources .env itself), so it is
    opt-in for operator tooling rather than an import side effect.
    """
    here = Path(start) if start else Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        candidate = parent / ".env"
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None

# How long a cached extract is trusted before an area re-fetches.  OSM changes
# slowly at the building level, so a month is generous and keeps repeat runs
# offline.
STALE_DAYS = 30

# How long a caller waits for someone else's in-flight download of the same
# bbox before giving up and reporting progress instead.  A cold city takes
# 10-16 min, so this is generous on purpose: the alternative is every caller
# starting its own copy of the same download.
FETCH_WAIT_SECONDS = int(os.environ.get("OSM_FETCH_WAIT_SECONDS", "900"))

# The largest area the BOUNDARY call will start downloading on its own.
#
# Auto-starting is what makes the counts arrive without a second wait, but it
# is also a download running inside the engine process, and a bad boundary can
# be enormous: "Galway, Ireland" resolves through Nominatim to "Galway Bay,
# Ireland" and the administrative rung then returns THE WHOLE ISLAND -- a
# 5.35 x 4.4 degree bbox. Fetching that (millions of ways, `out geom`, hundreds
# of MB of JSON parsed in-process) pegged the engine so hard that even /health
# stopped answering, which is a far worse failure than a slow preview.
#
# So the automatic download is bounded to a sane design area. Above it the
# boundary still draws, the preview still reports the real size, and the
# planner is told to narrow -- which is what the preview's own over-cap warning
# already says. An explicit counts request still fetches whatever was asked
# for; this cap only governs what starts by itself.
AUTO_FETCH_MAX_KM2 = float(os.environ.get("OSM_AUTO_FETCH_MAX_KM2", "500"))

# Guards against a runaway fetch.  The RUN's cap is the only one that decides
# whether a design can start -- a design orders of magnitude larger than
# anything the pipeline has run is a decision, not an accident.
#
# The PREVIEW does not refuse at all.  Refusing there hid the one thing a
# planner needs: the sub-area breakdown that says where to narrow.  Measured on
# Birmingham (a 266.9 km^2 city boundary, 257,127 premises) the preview refused
# before it could name a single usable postcode, and the refusal quoted the
# RUN's cap while the preview's own cap was a different number.  Reporting the
# size of a large area is the preview's whole job, and the cost it guards was
# already paid by then: assemble_premises() runs before any cap check, and the
# response carries a 20-row sample, not the premises themselves.
#
# Measured on Mariendorf (Berlin, 14.9 km^2) the raw fetch returns ~15.6k
# buildings / 8.3k address points, so a 5k cap would have refused a perfectly
# ordinary Berlin Ortsteil -- which is why the run cap is stated as a number to
# raise rather than a limit discovered by failing.
MAX_PREMISES = 20_000


class TooManyPremises(ValueError):
    """More premises than a run-sized cap allows.

    The count, the cap and a narrowing hint travel on the exception so every
    caller can quote the number that actually applied.  They are not always the
    same number -- the run has a fixed cap, a preview caller may impose its own
    -- and quoting the wrong one sends a planner to the wrong fix.
    """

    def __init__(self, count: int, cap: int, hint: str = "") -> None:
        self.count = int(count)
        self.cap = int(cap)
        self.hint = hint
        # The machine-readable token stays the str() form, so logs and callers
        # that match on the prefix keep working.
        super().__init__(f"too_many_premises:{self.count}:{self.cap}")


# Sub-area buckets are named in this order when several are offered.
_SUB_AREA_KINDS = ("postcode", "district")


def _bucket_value_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def own_area_values(resolution: Dict[str, Any], postcode: str = "") -> List[str]:
    """The values that name the area we are ALREADY in, so they cannot narrow it.

    Previewing postcode 12105 and being told to "design postcode 12105 instead"
    is a loop, not a suggestion: premises inside it carry that same postcode.  The
    same applies to the name that was searched for -- measured, the Mariendorf
    preview's own hint read "e.g. postcode 12107 (3404 premises), or **Mariendorf**
    (6131 premises)", and "Mariendorf" is the area it was already describing.
    """
    values = [postcode]
    if str(resolution.get("boundary_kind") or "") == "postcode":
        values.append(resolution.get("boundary_code"))
    # The local name of the area, which is what a district bucket would hold, and
    # a loaded dataset's own record name (a ward bucket for a ward).
    area = str(resolution.get("area") or "")
    if area:
        values.append(area.split(",")[0])
    if str(resolution.get("polygon_source") or "") == "dataset":
        values.append(resolution.get("boundary_name"))
    return [v for v in values if str(v or "").strip()]


def sub_area_kinds_that_narrow(
    resolution: Dict[str, Any],
    loaded_kinds: Sequence[str] = (),
    postcode_verified: Optional[bool] = None,
) -> List[str]:
    """Which sub-area buckets a planner can actually narrow to, from here.

    A bucket is a way to a smaller design area only if resolving that value
    returns something smaller, and in a loaded-dataset boundary it can return the
    area we are already in.  The proof is geometric rather than a guess: every
    premise carrying that postcode is inside the current boundary, and a postcode
    resolves through "the smallest loaded polygon containing its point", so when
    the current boundary IS a loaded polygon the answer is that same polygon.

    Measured, and the reason this exists: "B11 3SA" previews the 3.396 km² ward
    *Sparkbrook & Balsall Heath East* -- 6,058 premises, over the run cap -- and
    the hint offered "e.g. postcode B11 3SA (73 premises)", a real bucket *inside*
    the ward.  Typing it back returns the same 3.396 km² ward.  From anywhere else
    (an OSM polygon, the enclosing administrative area, a rectangle) a containing
    polygon is strictly smaller, so the suggestion stands.

    `postcode_verified` is the measured answer for a country with no loaded
    dataset, and it exists because the argument above is only valid when there is
    a containing polygon to resolve through.  Where there is none, a postcode
    resolves to whatever Nominatim returns for it, and in India that is a single
    building: `421201` and `421202` both returned the same State Bank of India
    branch at **0.0 km²**.  A bank is "strictly smaller" than the 55.474 km²
    Dombivli city by every size comparison and is not a design area at all, so
    the chip would have looked valid and led nowhere.  Pass the probed result
    (see `postcode_narrows_to_area`); None means "not probed" and keeps the
    previous inference, which is what the GB and US cases rely on.
    """
    boundary_is_dataset = str(resolution.get("polygon_source") or "") == "dataset"
    boundary_kind = str(resolution.get("boundary_kind") or "")
    kinds = tuple(loaded_kinds)
    offered: List[str] = []
    # A postcode resolves to its own polygon in OSM (Germany has them: 12107 is
    # 4.38 km² against the 9.343 km² Mariendorf it sits in), or to a loaded
    # postcode dataset.  A postcode dataset is a strictly smaller polygon than a
    # ward, which is what breaks the loop there.
    #
    # With NO dataset loaded for the country, that "own polygon in OSM" is the
    # only way through, and whether it exists is a fact about the country rather
    # than something this can infer -- so it is probed instead of assumed.
    if not boundary_is_dataset or boundary_kind == "postcode" or "postcode" in kinds:
        if postcode_verified is None or postcode_verified:
            offered.append("postcode")
    # A district is DELIBERATELY stricter, because a district name has no polygon
    # of its own to resolve to: measured, the Mariendorf preview offered "or
    # Tempelhof (13 premises)" from a 13-premise bucket inside it, and Tempelhof
    # resolves to **12.137 km²** -- larger than the 9.343 km² area being previewed,
    # so the suggestion would widen the design.  The only district value we can
    # stand behind is one that names a LOADED polygon, which needs a dataset with
    # names in it (the ONS wards: "Handsworth" is a real 1.565 km² ward inside the
    # 266.9 km² city), and never the kind of polygon we are already standing on.
    named_areas_loaded = any(k and k != "postcode" for k in kinds)
    if named_areas_loaded and (not boundary_is_dataset or boundary_kind == "postcode"):
        offered.append("district")
    return offered


def sub_area_kinds_offered(
    resolution: Dict[str, Any], postcode_verified: Optional[bool] = None
) -> List[str]:
    """`sub_area_kinds_that_narrow` with the country's loaded kinds looked up.

    Always looked up, because whether a DISTRICT can be offered depends on it in
    every case: a district name only has somewhere to resolve to when a dataset
    with names is loaded.  One indexed `DISTINCT kind` over `boundary_areas`
    (thousands of rows) against the rest of a preview's work, and it is the
    difference between a suggestion and a wrong suggestion.
    """
    loaded: Sequence[str] = boundary_dataset_kinds(str(resolution.get("country_code") or ""))
    return sub_area_kinds_that_narrow(resolution, loaded, postcode_verified)


def narrowing_hint(
    sub_areas: Dict[str, Any],
    kinds: Optional[Sequence[str]] = None,
    exclude: Sequence[str] = (),
) -> str:
    """Name the biggest buckets the data already has, so "narrow it" is usable.

    Taken from `addr:postcode` / `addr:suburb` on the address points we fetched,
    so it is a fact about this area rather than a guess about where to draw a
    line.  Empty when the area carries neither.

    `kinds` limits the offer to the kinds that can actually be resolved to a
    smaller area (see `sub_area_kinds_that_narrow`), and `exclude` drops values
    that name the area we are already in.  Both default to the old behaviour, but
    a caller that has a resolution should pass them: a suggestion the resolver
    cannot deliver is worse than no suggestion, because the planner pays for the
    round trip to find out.
    """
    offered = _SUB_AREA_KINDS if kinds is None else tuple(kinds)
    skip = {_bucket_value_key(v) for v in exclude if str(v or "").strip()}

    def first_usable(kind: str) -> Dict[str, Any]:
        for entry in sub_areas.get(kind) or []:
            if _bucket_value_key(entry.get("value")) not in skip:
                return entry
        return {}

    top_postcode = first_usable("postcode") if "postcode" in offered else {}
    top_district = first_usable("district") if "district" in offered else {}
    hint = ""
    if top_postcode.get("value"):
        hint = (f" e.g. postcode {top_postcode['value']} "
                f"({top_postcode['premises']} premises)")
        if top_district.get("value") and top_district["value"] != top_postcode["value"]:
            hint += (f", or {top_district['value']} "
                     f"({top_district['premises']} premises)")
    return hint


def narrowing_fallback(kinds: Sequence[str]) -> str:
    """What to say when no bucket can be offered honestly.

    "One street at a time" is right when the data simply has no postcode or
    district to offer.  It is the wrong answer when the reason is that nothing
    smaller is loaded for this country -- then the planner needs to know that a
    postcode will resolve back to this same area, or they will spend a round trip
    discovering it.
    """
    if not tuple(kinds):
        return (
            " a smaller share of it — no smaller boundary is loaded for this "
            "country, so a postcode or district here resolves back to this same "
            "area"
        )
    return " one street at a time"


def oversize_detail(count: int, cap: int, hint: str = "") -> str:
    """The one wording for "too big", describing whichever cap actually applied.

    The run's cap is a fixed pipeline limit; a preview cap belongs to the call
    that set it.  Calling both of them "the per-run cap" is how a planner ends
    up hunting for a setting that would not have helped.
    """
    if cap == MAX_PREMISES:
        lead = f"more than one run will design (the per-run cap is {cap})"
        tail = (
            "That cap is a fixed per-run limit inside the pipeline, not a request "
            "option; the preview reports the count and the largest "
            "postcode/district buckets for any area, so a smaller one can be "
            "picked from its chips."
        )
    else:
        lead = f"above the cap this request set ({cap})"
        tail = (
            "The preview sets no cap of its own and can report the count and the "
            "largest postcode/district buckets for any area."
        )
    return (
        f"This area yields {count} premises, {lead}. Design a narrower area —"
        f"{hint or ' one street or one Baublock at a time'}. {tail}"
    )

# Nothing below this is usable: a boundary with a handful of premises is a
# typo or a wrong area, and a design built on it looks like a success.
MIN_PREMISES = 1

# Above this, the preview stops just reporting and starts RECOMMENDING a
# narrower area, because the pipeline's reference design is 285 premises and a
# whole Berlin Ortsteil is roughly fifty times that.
SCALE_GUIDE_PREMISES = 1000

# The household heuristic, in ONE place so it is obvious what to tune.
# German average dwelling is ~90 m^2; these are the documented starting points
# and are checked against a known project's households (see the design doc).
UNIT_AREA_M2: Dict[str, float] = {
    "apartments": 85.0,
    "terrace": 110.0,
    "detached": 140.0,
    "default": 100.0,
}
MAX_FLATS_PER_BUILDING = 200

# A garage is not a premise.  One in the object layer becomes a garden leg and a
# drop duct, so these classes are excluded outright.
EXCLUDED_BUILDINGS = frozenset({
    "garage", "garages", "shed", "hut", "roof", "greenhouse", "carport",
    "industrial", "warehouse", "farm_auxiliary", "farm", "barn",
    "construction", "ruins", "service", "toilets", "shelter",
    "transformer_tower", "water_tower", "storage_tank", "silo",
})

# Used to pick the unit-area constant.  Anything else falls back to `default`.
BUILDING_TYPE_KEYS = ("apartments", "terrace", "detached", "house")

_POLYGON_JSON = "ST_GeomFromGeoJSON(%s)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# A single token that could be a postal code: 12105, SW1A 1AA, 201301, 75001.
# Deliberately not country-shaped -- any 3-11 char code with a digit counts.
_POSTCODE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9 -]{1,10}$")


def looks_like_postcode(value: str) -> bool:
    """Is this one postal-code-shaped token rather than a place name?"""
    text = str(value or "").strip()
    if not text or "," in text:
        return False
    return bool(_POSTCODE_RE.match(text))


def compose_area(
    area_name: str = "",
    postcode: str = "",
    city: str = "",
    country_code: str = "",
) -> str:
    """Build the search label from the structured area inputs.

    Every part is optional; the country is stated, never inferred.  A bare
    five-digit code used to be rewritten to "<code>, Germany", which read a US
    ZIP as a German one -- 10001 resolved to a street in Tuebingen.  No shape of
    postcode identifies a country: five digits are also used by France, Spain,
    Italy, Mexico and Norway, and the UK, Canada and the Netherlands are not
    numeric at all.

    The label doubles as the project label and the basis of the resolution
    cache key, so the country NAME is part of it: "Berlin" and "Berlin,
    Germany" are different searches and must not share a cache entry.
    """
    parts: List[str] = []
    for value in (area_name, postcode):
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text:
            parts.append(text)
    lower = {p.casefold() for p in parts}
    for value in (city, country_name(country_code)):
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text and text.casefold() not in lower:
            parts.append(text)
            lower.add(text.casefold())
    return ", ".join(parts)


def nominatim_query(area: str) -> str:
    """The literal text sent to Nominatim for an already-composed label.

    The name is kept from the pre-structured version so the change in behaviour
    is visible: nothing is appended here any more.  The country travels as a
    separate, explicit `countrycodes` filter instead.
    """
    return re.sub(r"\s+", " ", str(area or "").strip())


def normalize_area(area: str) -> str:
    """Case/punctuation-insensitive form of an area name, for the cache key."""
    if not area:
        return ""
    s = unicodedata.normalize("NFKD", str(area).strip().lower())
    s = s.replace("ß", "ss")
    s = re.sub(r"[^\w\s,;/]", " ", s)
    s = re.sub(r"\s+", " ", s)
    # A space around a separator must not change the key: "Berlin , Germany"
    # and "Berlin, Germany" are the same place and must share a cache entry.
    s = re.sub(r"\s*([,;/])\s*", r"\1", s)
    return s.strip(" ,;/")


def area_key(area: str) -> str:
    return hashlib.sha1(normalize_area(area).encode("utf-8")).hexdigest()


# Bump when the MEANING of a cached resolution changes.  Serving a row written
# under older rules silently reinstates them -- this has now happened twice:
#
#   v1 -> v2  the country became an explicit input.  A v1 row carries a query
#             built by the old assumption (a bare "12105" was searched as
#             "12105, Germany") and no country code.
#   v2 -> v3  the boundary ladder was introduced.  A v2 row has the OLD
#             polygon/no-polygon decision baked in, so a US ZIP cached before
#             the administrative fallback existed kept resolving to a rectangle
#             and the fallback never ran.  Caught by re-testing 10001.
#
#   v3 -> v4  a postcode input now records whether the postcode actually
#             DEFINED the area.  A v3 row was cached when "the best polygon
#             wins" was the whole rule, so a UK postcode that matched a building
#             came back as a fine-looking `nominatim` resolution with no way to
#             tell it apart from a real postcode boundary.  Re-testing "B1"
#             caught it.
#
# So: change how a resolution is DERIVED, and bump this.
_RESOLUTION_VERSION = "4"


def resolution_key(area: str, country_code: str = "") -> str:
    """Cache key for a resolution: the schema version, label and country filter.

    A composed label already carries the country name, but a caller may pass the
    filter without it, and "Berlin" filtered to DE is not the same search as
    "Berlin" filtered to US.  Putting the code in the key means those two can
    never collide in `osm.area_cache`.
    """
    code = normalize_country_code(country_code)
    return area_key(f"v{_RESOLUTION_VERSION}|{area}|{code}")


def input_type_for(area: str = "", area_name: str = "",
                   postcode: str = "", city: str = "") -> str:
    """How the area was specified, from the structured inputs when they exist.

    Read off the label alone this cannot work: "12105, Berlin, Germany" contains
    a comma, so a postcode search would be recorded as a name search.  The
    caller that still has the parts says which they were.

    `city` is accepted for symmetry with `compose_area` and is deliberately not
    decisive: a city with no postcode and no area name is a place search.
    """
    if str(postcode or "").strip() and not str(area_name or "").strip():
        return "postcode"
    if str(area_name or "").strip():
        return "area"
    return "postcode" if looks_like_postcode(area) else "area"


# Mean Earth radius, km (IUGG).  Used for the spherical area below.
_EARTH_RADIUS_KM = 6371.0088


def _ring_area_km2(ring: Sequence[Sequence[float]]) -> float:
    """Spherical area of one closed ring, km^2 (Chamberlain & Duquette).

    Planar shoelace maths on lon/lat is wrong at these latitudes and at these
    sizes -- degrees are not equal-area -- so the ring is treated as a spherical
    polygon.
    """
    if len(ring) < 4:
        return 0.0
    total = 0.0
    n = len(ring)
    for i in range(n):
        lon1, lat1 = float(ring[i][0]), float(ring[i][1])
        lon2, lat2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        total += math.radians(lon2 - lon1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total) * (_EARTH_RADIUS_KM ** 2) / 2.0


def polygon_area_km2(geometry: Optional[Dict[str, Any]]) -> float:
    """Area of a GeoJSON Polygon/MultiPolygon in km^2, holes subtracted.

    PURE, so it is unit-testable with no database.  It exists because the
    reported area used to be the area of the BOUNDING BOX: for postcode 12105
    that published 7.31 km^2 for a 5.81 km^2 boundary, a 26 % overstatement on a
    number a planner reads as the size of the job.
    """
    if not isinstance(geometry, dict):
        return 0.0
    gtype = geometry.get("type")
    if gtype == "Polygon":
        parts = [geometry.get("coordinates") or []]
    elif gtype == "MultiPolygon":
        parts = geometry.get("coordinates") or []
    else:
        return 0.0
    total = 0.0
    for rings in parts:
        if not rings:
            continue
        total += _ring_area_km2(rings[0])
        for hole in rings[1:]:
            total -= _ring_area_km2(hole)
    return max(0.0, total)


def bbox_polygon(bbox: Sequence[float]) -> Dict[str, Any]:
    """Bbox [w, s, e, n] -> GeoJSON Polygon, so PostGIS can cache and compare it."""
    w, s, e, n = (float(v) for v in bbox)
    return {
        "type": "Polygon",
        "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
    }


def polygon_bbox(geometry: Dict[str, Any]) -> List[float]:
    """Bounding box of any GeoJSON geometry: [w, s, e, n]."""
    xs: List[float] = []
    ys: List[float] = []

    def walk(node: Any) -> None:
        if not isinstance(node, (list, tuple)):
            return
        if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        for child in node:
            walk(child)

    walk(geometry.get("coordinates"))
    if not xs:
        raise ValueError("geometry has no coordinates")
    return [min(xs), min(ys), max(xs), max(ys)]


def area_km2(bbox: Sequence[float]) -> float:
    """Approximate bbox area -- a magnitude check for the preview, not a survey."""
    w, s, e, n = (float(v) for v in bbox)
    lat_mid = math.radians((s + n) / 2.0)
    width_m = (e - w) * 111_320.0 * math.cos(lat_mid)
    height_m = (n - s) * 110_540.0
    return abs(width_m * height_m) / 1_000_000.0


def _http_json(
    url: str,
    data: Optional[bytes] = None,
    timeout: float = 180.0,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """GET/POST JSON with a bounded retry.  stdlib only -- no extra dependency."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            last = exc
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"HTTP request failed: {last}") from last


# ---------------------------------------------------------------------------
# Schema -- created on demand, so there is no manual setup step
# ---------------------------------------------------------------------------

_OSM_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.area_cache (
    area_key        TEXT PRIMARY KEY,
    area            TEXT NOT NULL,
    display_name    TEXT,
    osm_type        TEXT,
    osm_id          BIGINT,
    bbox            GEOMETRY(Polygon, 4326),
    polygon         GEOMETRY(Geometry, 4326),
    polygon_source  TEXT,
    resolution      JSONB,
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.extract_meta (
    id          BIGSERIAL PRIMARY KEY,
    area_key    TEXT,
    source      TEXT NOT NULL,
    detail      TEXT,
    bbox        GEOMETRY(Polygon, 4326),
    counts      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.buildings (
    osm_id           BIGINT PRIMARY KEY,
    geom             GEOMETRY(Geometry, 4326),
    building         TEXT,
    name             TEXT,
    addr_street      TEXT,
    addr_housenumber TEXT,
    addr_postcode    TEXT,
    addr_city        TEXT,
    addr_suburb      TEXT,
    building_levels  TEXT,
    building_flats   TEXT
);

CREATE TABLE IF NOT EXISTS {schema}.address_nodes (
    osm_id           BIGINT PRIMARY KEY,
    geom             GEOMETRY(Point, 4326),
    addr_street      TEXT,
    addr_housenumber TEXT,
    addr_postcode    TEXT,
    addr_city        TEXT,
    addr_suburb      TEXT,
    addr_flats       TEXT
);

CREATE TABLE IF NOT EXISTS {schema}.roads (
    osm_id   BIGINT PRIMARY KEY,
    geom     GEOMETRY(LineString, 4326),
    highway  TEXT,
    fclass   TEXT,
    name     TEXT,
    ref      TEXT,
    oneway   TEXT,
    bridge   TEXT,
    tunnel   TEXT,
    access   TEXT,
    surface  TEXT,
    maxspeed TEXT,
    lanes    TEXT
);

CREATE TABLE IF NOT EXISTS {schema}.landuse (
    osm_id   BIGINT PRIMARY KEY,
    geom     GEOMETRY(Geometry, 4326),
    landuse  TEXT,
    -- "natural" is a PostgreSQL reserved word (NATURAL JOIN) and must be
    -- quoted as a column name; unquoted, the whole DDL fails and the store
    -- never gets created.
    "natural" TEXT,
    leisure  TEXT,
    boundary TEXT
);

-- The FULL OSM tags, so an object can be shown with all of its attributes
-- rather than only the dozen this module happens to name.  CREATE TABLE IF NOT
-- EXISTS leaves an existing table alone, so the columns are added explicitly.
ALTER TABLE {schema}.buildings     ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {schema}.address_nodes ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {schema}.roads         ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {schema}.landuse       ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {schema}.extract_meta  ADD COLUMN IF NOT EXISTS version INTEGER;

-- Authoritative boundary polygons loaded from open datasets (US Census TIGER
-- ZCTA, UK ONS/OS Open Data, ...).  These win over everything else because they
-- are the only source that is accurate in countries where OSM has no postcode
-- boundary at all.  Loaded by boundary_dataset_ingest().
CREATE TABLE IF NOT EXISTS {schema}.boundary_areas (
    country_code TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    kind         TEXT NOT NULL DEFAULT 'postcode',
    admin_level  TEXT,
    source       TEXT NOT NULL,
    properties   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    geom         GEOMETRY(Geometry, 4326) NOT NULL,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, code)
);

CREATE INDEX IF NOT EXISTS {schema}_buildings_geom_idx    ON {schema}.buildings USING GIST (geom);
CREATE INDEX IF NOT EXISTS {schema}_addr_nodes_geom_idx   ON {schema}.address_nodes USING GIST (geom);
CREATE INDEX IF NOT EXISTS {schema}_roads_geom_idx        ON {schema}.roads USING GIST (geom);
CREATE INDEX IF NOT EXISTS {schema}_landuse_geom_idx      ON {schema}.landuse USING GIST (geom);
CREATE INDEX IF NOT EXISTS {schema}_extract_meta_bbox_idx ON {schema}.extract_meta USING GIST (bbox);
CREATE INDEX IF NOT EXISTS {schema}_boundary_areas_geom_idx ON {schema}.boundary_areas USING GIST (geom);
"""

# Bump when the STORED SHAPE of an extract changes, so that a cache written by an
# older version is re-fetched instead of being served.
#
#   v1 -> v2  the full `tags` column was added: a v1 extract has the geometries
#             but no attributes, so an objects layer built on it would be empty.
#   v2 -> v3  the upsert was fixed to refresh every column on conflict.  The v2
#             extracts out there have tags = '{}' on every pre-existing row
#             because that bug, so they are not what v2 is supposed to mean and
#             must not be served as complete.
_EXTRACT_VERSION = 3


def init_schema() -> None:
    """Create the `osm` schema and its tables if they are missing (idempotent)."""
    conn = postgis.get_conn()
    with conn.cursor() as cur:
        cur.execute(_OSM_DDL.format(schema=OSM_SCHEMA))


def schema_ready() -> bool:
    if not postgis.is_available():
        return False
    try:
        with postgis.get_conn().cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{OSM_SCHEMA}.buildings",))
            return cur.fetchone()[0] is not None
    except Exception:
        return False


def _query(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Run a read query and return dict rows.  Uses postgis' guarded connection."""
    conn = postgis.get_conn()
    with conn.cursor(cursor_factory=postgis.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _execute(sql: str, params: Sequence[Any] = ()) -> None:
    conn = postgis.get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ---------------------------------------------------------------------------
# Nominatim -- ONE request per area
# ---------------------------------------------------------------------------

def nominatim_search(area: str, country_code: str = "") -> List[Dict[str, Any]]:
    """Resolve an area name.  Raises RuntimeError when Nominatim is unreachable.

    A distinct error type matters: "Nominatim is down" and "no such area" must
    never be confusable in the API (see resolve_area's callers).

    `country_code` becomes a hard `countrycodes` filter, which is what keeps two
    towns with the same name in different countries from being confused.
    """
    query: Dict[str, Any] = {
        "q": area,
        "format": "geojson",
        "polygon_geojson": 1,
        "addressdetails": 1,
        "limit": 3,
    }
    code = normalize_country_code(country_code)
    if code:
        query["countrycodes"] = code.lower()
    payload = _http_json(f"{NOMINATIM_URL}?{urllib.parse.urlencode(query)}", timeout=30.0)
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        return payload.get("features") or []
    if isinstance(payload, list):
        return payload
    return []


def _result_address(result: Dict[str, Any]) -> Dict[str, Any]:
    props = result.get("properties") or {}
    return props.get("address") or {}


def _country_code_of(result: Dict[str, Any]) -> str:
    return normalize_country_code(_result_address(result).get("country_code"))


def pick_area_result(
    results: Sequence[Dict[str, Any]],
    area: str,
    country_code: str = "",
) -> Dict[str, Any]:
    """Choose the single best match, or raise with the candidates listed.

    Ambiguity is reported, never resolved by silently taking the first hit: a
    wrong boundary means a design for the wrong place.
    """
    if not results:
        raise LookupError("not_found")
    wanted = normalize_area(area)
    code = normalize_country_code(country_code)

    def rank(r: Dict[str, Any]) -> Tuple[int, int, int]:
        # Prefer a polygon over a bare point, then a result in the country the
        # caller stated, then a name close to what was typed.  With no country
        # stated the middle key is neutral for every candidate, so the ranking
        # no longer quietly prefers Germany.
        in_country = 0 if (not code or _country_code_of(r) == code) else 1
        return (
            0 if (r.get("geometry") or {}).get("type") == "Polygon" else 1,
            in_country,
            abs(len(str((r.get("properties") or {}).get("display_name", "")).lower()) - len(wanted)),
        )

    return sorted(results, key=rank)[0]


# Settlement-like OSM result types, i.e. what belongs in a "city" dropdown.
_CITY_TYPES = frozenset({
    "city", "town", "village", "municipality", "hamlet", "borough",
    "suburb", "quarter", "city_district",
})

_SUGGEST_MIN_CHARS = 2
_SUGGEST_TTL_SECONDS = 600.0
_SUGGEST_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_SUGGEST_CACHE_MAX = 500


def suggest_places(text: str, country_code: str = "", limit: int = 8) -> Dict[str, Any]:
    """City/town suggestions for the area input's city combobox.

    Nominatim has no "list the cities of a country" endpoint, so this is a
    search: the typed text is matched inside the stated country and the results
    are filtered to settlement-like `addresstype` values.

    The response is cached in-process because a combobox fires while typing, and
    Nominatim's usage policy allows roughly one request per second.  A failure
    is reported as `reason: unavailable` rather than raising: a dropdown that
    cannot load must not turn into a 502 on a page the planner is still filling
    in.
    """
    query = re.sub(r"\s+", " ", str(text or "").strip())
    code = normalize_country_code(country_code)
    if len(query) < _SUGGEST_MIN_CHARS:
        return {"query": query, "country_code": code, "places": [], "reason": "too_short"}

    key = (query.casefold(), code)
    now = time.time()
    hit = _SUGGEST_CACHE.get(key)
    if hit and now - hit[0] < _SUGGEST_TTL_SECONDS:
        return hit[1]

    params: Dict[str, Any] = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": max(int(limit) * 4, 20),
        "accept-language": "en",
    }
    if code:
        params["countrycodes"] = code.lower()
    try:
        payload = _http_json(f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}", timeout=20.0)
    except Exception:  # noqa: BLE001 - a suggestion list is not worth failing the page for
        return {"query": query, "country_code": code, "places": [], "reason": "unavailable"}

    rows = payload if isinstance(payload, list) else []
    settlements: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("addresstype") or row.get("type") or "")
        name = str(row.get("name") or "").strip()
        if not name:
            name = str(row.get("display_name") or "").split(",")[0].strip()
        entry = {
            "name": name,
            "display_name": row.get("display_name"),
            "addresstype": kind,
            "osm_type": row.get("osm_type"),
            "osm_id": row.get("osm_id"),
            "latitude": row.get("lat"),
            "longitude": row.get("lon"),
            "kind": "city" if kind in _CITY_TYPES else "other",
        }
        (settlements if entry["kind"] == "city" else others).append(entry)

    # Typing a prefix often matches non-settlements ("muench" hits buildings
    # named Muench) before the town itself.  Offering those clearly marked beats
    # an empty dropdown mid-typing; they are never silently passed off as cities.
    chosen = settlements or others
    result = {
        "query": query,
        "country_code": code,
        "places": chosen[: max(1, int(limit))],
        "reason": None if settlements else ("non_settlement_matches" if others else "no_match"),
    }
    if len(_SUGGEST_CACHE) >= _SUGGEST_CACHE_MAX:
        _SUGGEST_CACHE.clear()
    _SUGGEST_CACHE[key] = (now, result)
    return result


def country_list() -> List[Dict[str, str]]:
    """Options for the country dropdown."""
    return country_options()


# ---------------------------------------------------------------------------
# Boundaries: the resolution ladder
# ---------------------------------------------------------------------------
#
# A good boundary like the German postcode polygons is DATA, not code.  Measured
# over the planet:
#
#   * Germany has postcode boundary relations in OSM (234 around Berlin), so
#     `12105` resolves to relation 1105327 `boundary=postal_code` and a real
#     polygon.
#   * US ZIPs and UK postcodes have NO boundary in OSM at all.  Nominatim
#     returns `osm_type=null, osm_id=null` for both -- the result is synthesised
#     from `addr:postcode` tags on scattered addresses, so there is no geometry
#     to fetch.  A 50 x 50 km envelope around Manhattan is not a coarse ZIP; it
#     is the absence of one.
#
# So an area is resolved down a ladder, and WHICH RUNG WAS USED is always
# reported -- a rectangle silently standing in for a borough is the same class of
# error as the country guess that used to live here:
#
#   1. a loaded authoritative dataset  -> polygon_source "dataset"
#   2. the OSM object's own polygon    -> polygon_source "nominatim"
#   3. the enclosing administrative boundary, a REAL polygon that exists
#      worldwide                        -> polygon_source "administrative"
#   4. the bounding box, last resort    -> polygon_source "bbox" (+ warning)

# Tightest first: the smallest enclosing administrative area is the most useful
# stand-in for a postcode.  Walked in order, so the first real polygon wins.
_ADMIN_REVERSE_ZOOMS = (16, 14, 12, 10)


def reverse_lookup(lat: float, lon: float, zoom: int) -> Optional[Dict[str, Any]]:
    """Nominatim /reverse as one GeoJSON feature, or None."""
    params = {
        "lat": lat,
        "lon": lon,
        "format": "geojson",
        "polygon_geojson": 1,
        "zoom": int(zoom),
        "addressdetails": 1,
    }
    payload = _http_json(
        f"{NOMINATIM_REVERSE_URL}?{urllib.parse.urlencode(params)}", timeout=30.0
    )
    if isinstance(payload, dict):
        features = payload.get("features") or []
        if features:
            return features[0]
    return None


def admin_boundary_for_point(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Tightest enclosing administrative POLYGON for a point, or None.

    This is the rung that makes a country with no postcode boundaries in OSM
    usable: reverse geocoding 40.7506,-73.9972 returns the Manhattan borough
    polygon, and 51.5010,-0.1416 returns the City of Westminster polygon.  Both
    are real boundaries, verified as Polygons/MultiPolygons -- not envelopes.

    Zoom is walked from tightest to widest because a finer zoom may return a
    point (a "quarter" place node) instead of a boundary; the first real
    administrative polygon wins.
    """
    for zoom in _ADMIN_REVERSE_ZOOMS:
        try:
            feature = reverse_lookup(lat, lon, zoom)
        except Exception:  # noqa: BLE001 - one bad zoom must not abort the walk
            # Continue rather than give up: a single timeout at a tight zoom
            # would otherwise drop the whole fallback and publish a rectangle.
            continue
        if not feature:
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if str(props.get("category") or "") != "boundary":
            continue
        return {
            "geometry": geometry,
            "display_name": props.get("display_name"),
            "addresstype": props.get("addresstype"),
            "osm_type": props.get("osm_type"),
            "osm_id": props.get("osm_id"),
            "zoom": zoom,
        }
    return None


# ---------------------------------------------------------------------------
# Named authoritative boundary datasets
#
# Each entry is one explicit operator decision: this dataset, under this licence.
# `boundary_dataset_ingest` deliberately takes an iterable rather than choosing a
# download itself, so a "source" is just a paged fetcher plus these facts.
# ---------------------------------------------------------------------------

UK_WARDS_SOURCE: Dict[str, Any] = {
    # ONS Open Geography Portal, hosted as an ArcGIS feature service.  Measured:
    # 8,396 wards, 2,000 per page, `supportsPagination` true, `f=geojson` gives
    # WGS84 polygons directly (so no Esri geometry conversion is needed), and a
    # point query works -- (-1.9360, 52.5140) -> E05011142 Handsworth, which is
    # the property a UK postcode needs.
    "name": "ONS Wards (May 2024) Boundaries UK BSC",
    "kind": "ward",
    "country_code": "GB",
    "vintage": "May 2024",
    "publisher": "Office for National Statistics",
    "url": (
        "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
        "Wards_May_2024_Boundaries_UK_BSC/FeatureServer/0/query"
    ),
    "licence": (
        "Open Government Licence v3.0 (ONS geography licences, "
        "https://www.ons.gov.uk/methodology/geography/licences); boundary "
        "geometry contains OS data (c) Crown copyright and database right"
    ),
    "fields": {"code": "WD24CD", "name": "WD24NM", "welsh": "WD24NMW"},
}


US_ZCTA_SOURCE: Dict[str, Any] = {
    # TIGERweb, the Census Bureau's own service.  Measured: 33,791 ZCTAs (2020),
    # `maxRecordCount` 100,000, `f=geojson` works, and `copyrightText` is "Source:
    # U.S. Census Bureau" -- TIGER/Line is public domain, so unlike the UK there
    # is nothing to license around.
    #
    # A ZCTA IS NOT A USPS ZIP CODE, and the name says so.  ZIP codes are USPS
    # delivery routes, not areas; a ZCTA is the Census Bureau's area approximation
    # of one.  It is the right boundary to design a ZIP-sized area against, and it
    # is not the shape of a mail route, so the difference is carried into the
    # dataset note that reaches the planner.
    "name": "US Census TIGER ZCTA (2020)",
    "kind": "postcode",
    "country_code": "US",
    "vintage": "2020 Census",
    "publisher": "U.S. Census Bureau (TIGERweb)",
    "url": (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Census2020/MapServer/84/query"
    ),
    "licence": (
        "Public domain (U.S. Census Bureau TIGER/Line; no use restrictions). "
        "ZCTA is a Census Bureau statistical area approximating a USPS ZIP code, "
        "not the ZIP delivery route itself."
    ),
    "note": (
        "A ZCTA is the Census Bureau's statistical approximation of a USPS ZIP "
        "code area -- ZIP codes are delivery routes, not areas -- so the design "
        "area is a close approximation of the ZIP, not the postal service's own "
        "boundary."
    ),
    "fields": {"code": "ZCTA5", "name": "NAME"},
    # TIGER geometry is coastline-dense -- measured on a 1,000-ZCTA page: 29 KB
    # per feature at full resolution (~980 MB extrapolated for the set), 3.1 KB
    # per feature with `maxAllowableOffset` 0.0001 deg (~11 m, ~106 MB), and
    # 1.3 KB at 0.0005 (~55 m, ~45 MB).  A boundary that defines a design area
    # does not need the full detail, so ~11 m is used and the choice is stated
    # here rather than hidden in a query string.
    "simplify_deg": 0.0001,
    "page_size": 2000,
}


def fetch_uk_wards(
    page_size: int = 2000,
    limit: Optional[int] = None,
    source: Optional[Dict[str, Any]] = None,
    start_offset: int = 0,
) -> Iterator[Dict[str, Any]]:
    """Yield every UK ward as `boundary_dataset_ingest` records.

    Paged because the service caps a page at 2,000 (the full set is 8,396), and
    ordered by code because an unordered page walk can repeat or skip features.
    One ward per record, with the licence and vintage carried onto the record so
    a loaded polygon can be traced back to what it came from.
    """
    src = dict(source or UK_WARDS_SOURCE)
    fields = src.get("fields") or {}
    code_field = fields.get("code", "WD24CD")
    name_field = fields.get("name", "WD24NM")
    welsh_field = fields.get("welsh", "WD24NMW")
    out_fields = ",".join(f for f in (code_field, name_field, welsh_field) if f)
    offset = max(0, int(start_offset or 0))
    seen = 0
    while True:
        url = (
            f"{src['url']}?where=1%3D1&outFields={out_fields}&returnGeometry=true"
            f"&outSR=4326&orderByFields={code_field}&resultOffset={offset}"
            f"&resultRecordCount={int(page_size)}&f=geojson"
        )
        page = _http_json(url, timeout=180.0)
        features = (page or {}).get("features") or []
        if not features:
            return
        for feature in features:
            props = feature.get("properties") or {}
            code = str(props.get(code_field) or "").strip()
            geometry = feature.get("geometry")
            if not code or not (geometry or {}).get("type"):
                continue
            yield {
                "country_code": src.get("country_code") or "GB",
                "code": code,
                "name": str(props.get(name_field) or "").strip(),
                "kind": src.get("kind") or "ward",
                # Not an OSM admin_level: naming it as the dataset's own kind
                # keeps "ward" from being read as one of OSM's levels.
                "admin_level": src.get("kind") or "ward",
                "geometry": geometry,
                "properties": {
                    "welsh_name": str(props.get(welsh_field) or "").strip(),
                    "dataset": src.get("name"),
                    "vintage": src.get("vintage"),
                    "publisher": src.get("publisher"),
                    "licence": src.get("licence"),
                    "url": src.get("url"),
                },
            }
            seen += 1
            if limit is not None and seen >= int(limit):
                return
        offset += len(features)


def fetch_us_zctas(
    page_size: int = 2000,
    limit: Optional[int] = None,
    source: Optional[Dict[str, Any]] = None,
    start_offset: int = 0,
) -> Iterator[Dict[str, Any]]:
    """Yield every US ZIP Code Tabulation Area as ingest records.

    Geometry is simplified by the service (`maxAllowableOffset`) rather than after
    download: the raw set is ~980 MB of coastline-dense polygons, and this is a
    boundary that decides which premises are in the design, not a cartographic
    product.  Population and housing-unit counts are carried as properties -- they
    come with the polygon and are useful evidence about an area's size.
    """
    src = dict(source or US_ZCTA_SOURCE)
    fields = src.get("fields") or {}
    code_field = fields.get("code", "ZCTA5")
    name_field = fields.get("name", "NAME")
    out_fields = ",".join(f for f in (code_field, name_field, "POP100", "HU100") if f)
    simplify = str(src.get("simplify_deg") or "")
    # A country-sized load outlives one command, and the page walk is ordered, so
    # an interrupted load resumes from a record offset instead of re-fetching what
    # is already in the database (the fetch is bandwidth-bound, not cheap).
    offset = max(0, int(start_offset or 0))
    seen = 0
    while True:
        url = (
            f"{src['url']}?where=1%3D1&outFields={out_fields}&returnGeometry=true"
            f"&outSR=4326&geometryPrecision=5&orderByFields={code_field}"
            f"&resultOffset={offset}&resultRecordCount={int(page_size)}&f=geojson"
        )
        if simplify:
            url += f"&maxAllowableOffset={simplify}"
        page = _http_json(url, timeout=300.0)
        features = (page or {}).get("features") or []
        if not features:
            return
        for feature in features:
            props = feature.get("properties") or {}
            code = str(props.get(code_field) or "").strip()
            geometry = feature.get("geometry")
            if not code or not (geometry or {}).get("type"):
                continue
            yield {
                "country_code": src.get("country_code") or "US",
                "code": code,
                "name": str(props.get(name_field) or "").strip() or code,
                "kind": src.get("kind") or "postcode",
                "admin_level": src.get("kind") or "postcode",
                "geometry": geometry,
                "properties": {
                    "population": props.get("POP100"),
                    "housing_units": props.get("HU100"),
                    "dataset": src.get("name"),
                    "vintage": src.get("vintage"),
                    "publisher": src.get("publisher"),
                    "licence": src.get("licence"),
                    "note": src.get("note"),
                    "url": src.get("url"),
                },
            }
            seen += 1
            if limit is not None and seen >= int(limit):
                return
        offset += len(features)


def ingests_in_batches(
    records: Iterable[Dict[str, Any]],
    source: str,
    batch: int = 2000,
    on_batch: Optional[Any] = None,
) -> int:
    """Load records in batches, so a country-sized dataset is not one statement.

    `boundary_dataset_ingest` builds every row and one `execute_values` for the
    whole set.  That is right for 8,396 UK wards and wrong for 33,791 ZCTAs whose
    geometry alone is over 100 MB: the rows would be held in memory and then sent
    as a single statement.  Batching also means a failure at page 20 leaves the
    first 19 loaded rather than nothing.
    """
    total = 0
    chunk: List[Dict[str, Any]] = []
    for rec in records:
        chunk.append(rec)
        if len(chunk) >= int(batch):
            total += boundary_dataset_ingest(chunk, source=source)
            chunk = []
            # A country-sized load outlives one command, so report as it goes
            # rather than only at the end: an interrupted run then says how far
            # it got, and a re-run (upserts, `--keep-existing`) finishes it.
            if on_batch is not None:
                on_batch(total)
    if chunk:
        total += boundary_dataset_ingest(chunk, source=source)
        if on_batch is not None:
            on_batch(total)
    return total


def ingest_us_zctas(
    limit: Optional[int] = None,
    page_size: int = 2000,
    replace: bool = True,
    on_batch: Optional[Any] = None,
    start_offset: int = 0,
) -> Dict[str, Any]:
    """Fetch and load the US ZIP Code Tabulation Areas."""
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    purged = 0
    if replace:
        purged = boundary_dataset_purge(
            US_ZCTA_SOURCE["country_code"], kind=US_ZCTA_SOURCE["kind"]
        )
    loaded = ingests_in_batches(
        fetch_us_zctas(page_size=page_size, limit=limit, start_offset=start_offset),
        source=str(US_ZCTA_SOURCE["name"]),
        on_batch=on_batch,
    )
    return {
        "source": US_ZCTA_SOURCE["name"],
        "licence": US_ZCTA_SOURCE["licence"],
        "purged": purged,
        "loaded": loaded,
        "kind": US_ZCTA_SOURCE["kind"],
        "country_code": US_ZCTA_SOURCE["country_code"],
    }


def boundary_dataset_purge(
    country_code: str, kind: Optional[str] = None, source: Optional[str] = None
) -> int:
    """Delete loaded dataset polygons for a country (optionally one kind/source).

    Needed because `boundary_dataset_ingest` upserts on (country_code, code): a
    re-ingest of a NEWER vintage would update the wards that still exist and
    silently leave the ones that were abolished, so an old ward polygon could
    outlive the ward.  Deleting first makes a re-ingest a replace.
    """
    if not postgis.is_available():
        return 0
    clauses = ["country_code = %s"]
    params: List[Any] = [normalize_country_code(country_code)]
    if kind:
        clauses.append("kind = %s")
        params.append(str(kind))
    if source:
        clauses.append("source = %s")
        params.append(str(source))
    try:
        with postgis.get_conn().cursor() as cur:
            cur.execute(
                f"DELETE FROM {OSM_SCHEMA}.boundary_areas WHERE " + " AND ".join(clauses),
                tuple(params),
            )
            return cur.rowcount or 0
    except Exception:  # noqa: BLE001 - table may not exist yet
        return 0


def ingest_uk_wards(
    limit: Optional[int] = None,
    page_size: int = 2000,
    replace: bool = True,
    on_batch: Optional[Any] = None,
    start_offset: int = 0,
) -> Dict[str, Any]:
    """Fetch and load the ONS ward polygons for the UK.

    A replace, not a merge (see `boundary_dataset_purge`), so re-running it after
    a new ONS vintage does not leave abolished wards behind.
    """
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    purged = 0
    if replace:
        purged = boundary_dataset_purge(
            UK_WARDS_SOURCE["country_code"],
            kind=UK_WARDS_SOURCE["kind"],
        )
    loaded = ingests_in_batches(
        fetch_uk_wards(page_size=page_size, limit=limit, start_offset=start_offset),
        source=str(UK_WARDS_SOURCE["name"]),
        on_batch=on_batch,
    )
    return {
        "source": UK_WARDS_SOURCE["name"],
        "licence": UK_WARDS_SOURCE["licence"],
        "purged": purged,
        "loaded": loaded,
        "kind": UK_WARDS_SOURCE["kind"],
        "country_code": UK_WARDS_SOURCE["country_code"],
    }


def boundary_dataset_lookup(country_code: str, code: str) -> Optional[Dict[str, Any]]:
    """Exact match in the loaded authoritative boundary datasets, or None.

    Datasets are the only rung that is accurate where OSM has no postcode
    boundary, so they win when present.  See `boundary_dataset_ingest`.
    """
    code_norm = re.sub(r"\s+", "", str(code or "")).upper()
    if not code_norm or not schema_ready():
        return None
    try:
        rows = _query(
            f"SELECT code, name, kind, admin_level, source, properties, "
            f"ST_AsGeoJSON(geom) AS geom_json "
            f"FROM {OSM_SCHEMA}.boundary_areas "
            f"WHERE country_code = %s AND code = %s LIMIT 1",
            (normalize_country_code(country_code), code_norm),
        )
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None
    if not rows:
        return None
    row = rows[0]
    geometry = _json_geometry(row.get("geom_json"))
    if not geometry:
        return None
    return {
        "geometry": geometry,
        "source": row.get("source"),
        "code": row.get("code"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "admin_level": row.get("admin_level"),
        "properties": row.get("properties") or {},
    }


def _dataset_row_payload(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A `boundary_areas` row -> the same shape `boundary_dataset_lookup` returns."""
    geometry = _json_geometry(row.get("geom_json"))
    if not geometry:
        return None
    payload = {
        "geometry": geometry,
        "source": row.get("source"),
        "code": row.get("code"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "admin_level": row.get("admin_level"),
        "properties": row.get("properties") or {},
    }
    if row.get("area_km2") is not None:
        payload["area_km2"] = float(row["area_km2"])
    return payload


# The columns every dataset lookup returns, so a caller can tell a ward from a
# postcode polygon without a second query.
_DATASET_COLUMNS = (
    "code, name, kind, admin_level, source, properties, "
    "ST_AsGeoJSON(geom) AS geom_json, "
    "ST_Area(geom::geography) / 1000000.0 AS area_km2"
)


def boundary_dataset_containing(
    country_code: str,
    lon: float,
    lat: float,
    kind: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The loaded dataset polygon that CONTAINS a point, smallest first, or None.

    This is what lets a UK postcode narrow at all.  A postcode in the UK is not a
    boundary in OSM -- it is a tag on address points -- so the ladder has nothing
    to find and falls back to the enclosing city (266.9 km2 for Birmingham).  The
    postcode's own point is known though, so the ward containing it is the
    smallest real boundary that answers "the area this postcode sits in".

    Smallest-first because datasets nest: if a postcode dataset and a ward dataset
    are both loaded, the postcode polygon is the tighter answer.
    """
    code_in = normalize_country_code(country_code)
    if not code_in or lon is None or lat is None or not schema_ready():
        return None
    try:
        clauses = ["country_code = %s", "ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))"]
        params: List[Any] = [code_in, float(lon), float(lat)]
        if kind:
            clauses.append("kind = %s")
            params.append(str(kind))
        rows = _query(
            f"SELECT {_DATASET_COLUMNS} FROM {OSM_SCHEMA}.boundary_areas "
            f"WHERE " + " AND ".join(clauses) + " ORDER BY ST_Area(geom::geography) ASC LIMIT 1",
            tuple(params),
        )
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None
    return _dataset_row_payload(rows[0]) if rows else None


def boundary_dataset_by_name(
    country_code: str,
    names: Sequence[str],
    kind: Optional[str] = None,
    within: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Loaded dataset polygon whose name exactly matches, smallest first, or None.

    `within` is not a nicety, it is the guard that makes a name lookup safe.  Ward
    names repeat inside a country -- there is a Handsworth in Birmingham AND one
    in Sheffield -- so without containment in the area the name already resolved
    to, "Handsworth" would just pick whichever ward happened to be smaller.
    Containment also supplies the local meaning for a common name.
    """
    code_in = normalize_country_code(country_code)
    wanted = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not code_in or not wanted or not schema_ready():
        return None
    try:
        clauses = ["country_code = %s", "lower(name) = ANY(%s)"]
        params: List[Any] = [code_in, [n.lower() for n in wanted]]
        if kind:
            clauses.append("kind = %s")
            params.append(str(kind))
        if within and (within.get("type") or ""):
            clauses.append(
                "ST_Contains(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), ST_PointOnSurface(geom))"
            )
            params.append(json.dumps(within))
        rows = _query(
            f"SELECT {_DATASET_COLUMNS} FROM {OSM_SCHEMA}.boundary_areas "
            f"WHERE " + " AND ".join(clauses) + " ORDER BY ST_Area(geom::geography) ASC LIMIT 1",
            tuple(params),
        )
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None
    return _dataset_row_payload(rows[0]) if rows else None


def dataset_stamp(country_code: str) -> str:
    """A fingerprint of the loaded boundary datasets for a country ("" if none).

    Ingesting a dataset is an operator step that can happen at any time, so a
    cached area resolution must not outlive it.  Measured, and this is the THIRD
    time a cache has hidden a rule change on this feature: the ONS wards were
    loaded and `B11 3SA, Birmingham, United Kingdom` still resolved to the
    266.9 km² city from a row written before the ingest -- so the ingest looked
    like it had done nothing, on exactly the postcode it was loaded for.

    The stamp is the newest `loaded_at` in the table, so the first lookup after an
    ingest re-resolves and everything after that is served from cache again.  A
    version constant would have to be bumped by hand on every ingest, which is
    the kind of thing this feature has already been bitten by twice.
    """
    code_in = normalize_country_code(country_code)
    if not code_in or not schema_ready():
        return ""
    try:
        rows = _query(
            f"SELECT max(loaded_at) AS at FROM {OSM_SCHEMA}.boundary_areas "
            f"WHERE country_code = %s",
            (code_in,),
        )
    except Exception:  # noqa: BLE001 - table may not exist yet
        return ""
    return str(rows[0]["at"]) if rows and rows[0].get("at") else ""


def boundary_dataset_kinds(country_code: str) -> List[str]:
    """Which kinds of dataset polygon are loaded for a country ([] if none)."""
    code_in = normalize_country_code(country_code)
    if not code_in or not schema_ready():
        return []
    try:
        rows = _query(
            f"SELECT DISTINCT kind FROM {OSM_SCHEMA}.boundary_areas "
            f"WHERE country_code = %s AND kind IS NOT NULL",
            (code_in,),
        )
    except Exception:  # noqa: BLE001 - table may not exist yet
        return []
    return sorted(str(r["kind"]) for r in rows if r.get("kind"))


def area_name_candidates(area: str) -> List[str]:
    """The names in an input that could name a dataset area, most specific first.

    "Handsworth, Birmingham, United Kingdom" -> ["Handsworth", "Handsworth,
    Birmingham, United Kingdom"].  The first component is the local name a ward
    lookup needs; the whole string is kept because a dataset may also carry a
    fuller name.
    """
    text = str(area or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        # Punctuation only (", ,"), so there is no name here to look up.  Falling
        # back to the raw text would query the dataset for ", ,".
        return []
    candidates = []
    for name in [parts[0], text]:
        if name and name not in candidates:
            candidates.append(name)
    return candidates


def boundary_dataset_ingest(
    records: Iterable[Dict[str, Any]],
    source: str,
) -> int:
    """Load authoritative boundary polygons into `osm.boundary_areas`.

    Each record is `{country_code, code, name?, kind?, admin_level?, properties?,
    geometry}`.  Deliberately a function over an iterable rather than a
    downloader: which dataset to fetch, and under which licence, is not a
    decision this code should make on its own.  The bulk sources are public
    domain or open-licensed (US Census TIGER ZCTA, UK ONS/OS Open Data) but are
    large, so ingesting them is an explicit operator step.
    """
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    init_schema()
    rows: List[Tuple[Any, ...]] = []
    for rec in records:
        geometry = rec.get("geometry")
        code = str(rec.get("code") or "").strip()
        geometry = _json_geometry(geometry) if isinstance(geometry, str) else geometry
        if not code or not isinstance(geometry, dict) or not geometry.get("type"):
            continue
        rows.append((
            normalize_country_code(rec.get("country_code")),
            re.sub(r"\s+", "", code).upper(),
            rec.get("name"),
            str(rec.get("kind") or "postcode"),
            rec.get("admin_level"),
            source,
            json.dumps(rec.get("properties") or {}),
            json.dumps(geometry),
        ))
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {OSM_SCHEMA}.boundary_areas "
        f"(country_code, code, name, kind, admin_level, source, properties, geom) "
        f"VALUES %s ON CONFLICT (country_code, code) DO UPDATE SET "
        f"name = EXCLUDED.name, kind = EXCLUDED.kind, admin_level = EXCLUDED.admin_level, "
        f"source = EXCLUDED.source, properties = EXCLUDED.properties, "
        f"geom = EXCLUDED.geom, loaded_at = now()"
    )
    # Batched, not one round trip per row: the remote database made per-row
    # inserts the bottleneck once already (see the address-node fetch).
    from psycopg2.extras import execute_values
    with postgis.get_conn().cursor() as cur:
        execute_values(
            cur, sql,
            rows,
            template="(%s, %s, %s, %s, %s, %s, %s::jsonb, "
                     f"ST_MakeValid(ST_GeomFromGeoJSON(%s)))",
        )
    return len(rows)


def postcode_defines_the_area(
    postcode: str, source: str, category: str, kind: str, dataset_kind: str = ""
) -> Optional[bool]:
    """Did a postcode input actually resolve to a postcode boundary?

    `None` when the input was not a postcode, because then the question does not
    apply.  The rung cannot answer it on its own: a building footprint is as real
    a polygon as a postcode boundary is, so "the best polygon won" looks the same
    either way.  Measured, all four through the same ladder:

    * `12105, Berlin, DE` — `boundary`/`postal_code`, relation 1105327 → **True**,
      a real postcode polygon.
    * `B1, Birmingham, GB` — `building`/`office`, way 31543580 → **False**, an
      office block that merely contains the postcode.
    * `B14 7, Birmingham` — `boundary`/`administrative` → **False**, the enclosing
      city.  Note this one *is* a boundary: only `postal_code` counts.
    * `B11 3SA, Birmingham` — `place`/`postcode`, `osm_type` null → **False**, no
      object at all, just address points synthesised into a label.

    A loaded POSTCODE dataset counts: it exists precisely to supply the postcode
    boundary OSM does not have.  A loaded WARD dataset does not, even though it
    comes from the same rung -- a ward merely contains the postcode, so calling it
    a postcode boundary would silence the warning that says the design area is not
    the postcode.  That is why `dataset_kind` is part of the question.
    """
    if not re.sub(r"\s+", "", str(postcode or "")):
        return None
    if source == "dataset":
        return str(dataset_kind or "") == "postcode"
    return str(category or "") == "boundary" and str(kind or "") == "postal_code"


def _best_point(
    result: Dict[str, Any],
    geometry: Dict[str, Any],
    bbox: Sequence[float],
) -> Tuple[Optional[float], Optional[float]]:
    """A (lat, lon) to reason from: the result's own coordinates, else its centre."""
    lat, lon = result.get("lat"), result.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    if geometry.get("type") == "Point":
        coords = geometry.get("coordinates") or []
        if len(coords) >= 2:
            return float(coords[1]), float(coords[0])
    if bbox:
        w, s, e, n = (float(v) for v in bbox)
        return (s + n) / 2.0, (w + e) / 2.0
    return None, None


def resolve_area(area: str, refresh: bool = False, country_code: str = "",
                 input_type: str = "", postcode: str = "") -> Dict[str, Any]:
    """Area name -> polygon + bbox, cached in `osm.area_cache`.

    The polygon is preferred; when Nominatim returns only a point/line the bbox
    rectangle is used and `polygon_source` says so, because a silent bbox
    fallback would quietly widen the design area.

    `country_code` is an explicit filter, not a guess: `area` is expected to be
    a label already composed by `compose_area`, and the code keeps the search
    inside the stated country.
    """
    if not (area or "").strip():
        raise ValueError("area is required")
    code_in = normalize_country_code(country_code)
    postcode_in = re.sub(r"\s+", "", str(postcode or "")).upper()

    # Make sure the store exists BEFORE resolving, so the resolution can be
    # cached from the very first call.  Waiting until the OSM fetch creates the
    # schema means the first resolve is not cached, and a boundary+freshness
    # pair of calls pays for two Nominatim lookups of the same name.
    if postgis.is_available():
        init_schema()

    key = resolution_key(area, code_in)
    # Read before the cache: a loaded dataset can change the answer for a country,
    # and a row cached under the previous dataset state must not be served.
    stamp = dataset_stamp(code_in) if schema_ready() else ""
    if schema_ready() and not refresh:
        cached = _query(
            f"SELECT area, display_name, osm_type, osm_id, polygon_source, resolution, "
            f"ST_AsGeoJSON(bbox) AS bbox_json, ST_AsGeoJSON(polygon) AS poly_json "
            f"FROM {OSM_SCHEMA}.area_cache WHERE area_key = %s",
            (key,),
        )
        if cached:
            resolved = _resolution_from_row(cached[0])
            # An area cached before this country's datasets were loaded carries no
            # stamp, or an older one, and must be re-resolved rather than served:
            # otherwise the ingest appears to do nothing.
            if str(resolved.get("dataset_stamp") or "") == stamp:
                return resolved

    search_area = nominatim_query(area)
    results = nominatim_search(search_area, code_in)
    best = pick_area_result(results, search_area, code_in)
    props = best.get("properties") or {}
    geometry = best.get("geometry") or {}
    bbox = best.get("bbox")
    if not bbox and geometry:
        bbox = polygon_bbox(geometry)
    if not bbox:
        raise LookupError("not_found")

    # The country actually returned, which may differ from the filter when the
    # caller stated none.  It is what the generated workbook's Country column
    # uses, so a non-German area is no longer labelled Germany.
    address = _result_address(best)
    resolved_cc = _country_code_of(best) or code_in

    # --- the boundary ladder: the best real polygon available --------------
    use_polygon = geometry.get("type") in ("Polygon", "MultiPolygon")
    polygon = geometry if use_polygon else None
    source = "nominatim" if use_polygon else ""
    boundary_detail: Dict[str, Any] = {}

    if polygon is None and postcode_in:
        # 1. A loaded authoritative dataset.  The only rung that is accurate
        #    where OSM has no postcode boundary (US ZIP, UK postcode).
        dataset = boundary_dataset_lookup(resolved_cc, postcode_in)
        if dataset:
            polygon = dataset["geometry"]
            source = "dataset"
            # The same provenance the name/point rung carries, including the
            # dataset's own note: a ZIP that resolved here is a ZCTA, and the
            # planner is told so rather than left to assume USPS precision.
            dataset_props = dataset.get("properties") or {}
            boundary_detail = {
                "boundary_source": dataset.get("source"),
                "boundary_code": dataset.get("code"),
                "boundary_name": dataset.get("name"),
                "boundary_kind": dataset.get("kind"),
                "boundary_licence": dataset_props.get("licence"),
                "boundary_vintage": dataset_props.get("vintage"),
                "boundary_note": dataset_props.get("note"),
            }

    if polygon is None:
        # 2. The enclosing administrative boundary -- a REAL polygon that
        #    exists worldwide, unlike a postcode boundary.  Without this, a US
        #    ZIP or a UK postcode falls straight through to a rectangle.
        lat, lon = _best_point(best, geometry, bbox)
        admin = admin_boundary_for_point(lat, lon) if lat is not None else None
        if admin:
            polygon = admin["geometry"]
            source = "administrative"
            boundary_detail = {
                "boundary_name": admin.get("display_name"),
                "boundary_admin_level": admin.get("addresstype"),
                "boundary_osm_type": admin.get("osm_type"),
                "boundary_osm_id": admin.get("osm_id"),
                "boundary_zoom": admin.get("zoom"),
            }

    if polygon is None:
        # 3. Last resort: the rectangle, and the caller says so out loud.
        polygon = bbox_polygon(bbox)
        source = "bbox"

    matched_category = str(props.get("category") or "")
    matched_type = str(props.get("type") or "")

    # --- 4. the loaded datasets, which can beat what OSM offered -------------
    # Two measured failures this is here to fix, both in the UK:
    #
    #   "Handsworth, Birmingham"  OSM returns the CITY (266.9 km2) because
    #                             Birmingham has no ward polygons at all, so the
    #                             name resolves to the enclosing city.
    #   "B11 3SA, Birmingham"     a UK postcode is not a boundary in OSM, so the
    #                             ladder falls back to the same city.
    #
    # A dataset polygon is preferred only when it is the SAME PLACE by name, or
    # when the postcode's own point actually sits inside it -- never merely
    # because a dataset exists for the country.  A real postcode boundary in OSM
    # ("12105, Berlin") is a boundary already and is left alone.
    postcode_like = bool(postcode_in) or looks_like_postcode(area)
    osm_postcode_boundary = postcode_defines_the_area(
        postcode_in or area, source, matched_category, matched_type
    )
    if source != "dataset":
        refined: Optional[Dict[str, Any]] = None
        if postcode_like:
            if osm_postcode_boundary is not True:
                lat, lon = _best_point(best, geometry, bbox)
                if lat is not None and lon is not None:
                    refined = boundary_dataset_containing(resolved_cc, lon, lat)
        else:
            refined = boundary_dataset_by_name(
                resolved_cc, area_name_candidates(search_area), within=polygon
            )
        if refined:
            polygon = refined["geometry"]
            source = "dataset"
            props_dataset = refined.get("properties") or {}
            boundary_detail = {
                "boundary_source": refined.get("source"),
                "boundary_code": refined.get("code"),
                "boundary_name": refined.get("name"),
                "boundary_kind": refined.get("kind"),
                "boundary_dataset_km2": refined.get("area_km2"),
                "boundary_licence": props_dataset.get("licence"),
                "boundary_vintage": props_dataset.get("vintage"),
                # A dataset can carry a caveat about what its polygons ARE -- a
                # ZCTA is not a USPS ZIP code -- and it has to reach the planner,
                # or the name starts doing work the data cannot back up.
                "boundary_note": props_dataset.get("note"),
            }

    # A real polygon means a tight bbox around IT, which is what makes the
    # Overpass fetch no larger than the design area needs.
    tight = polygon_bbox(polygon)
    if source != "bbox":
        bbox = tight

    # Did a postcode DEFINE this area, or was it only the search term?
    #
    # The distinction cannot be read off the rung.  Measured:
    #   "12105, Berlin, DE"  -> boundary/postal_code, relation 1105327, a real
    #                           postcode polygon: the postcode IS the boundary.
    #   "B1, Birmingham, GB" -> building/office, way 31543580 -- an office block
    #                           that merely contains the postcode.
    #   "B14 7, Birmingham"  -> boundary/administrative: the enclosing city.
    #   "B11 3SA, Birmingham"-> place/postcode with osm_type null: no object at
    #                           all, just address points synthesised into a label.
    # The first is a postcode boundary; the others are a postcode used as a
    # search term, with the area coming from whatever was found around it.  Both
    # of the latter look identical to the first once the polygon has been chosen,
    # which is how a single office block can be designed without complaint.
    # `postcode_in` only, not the area text: this is reported in the payload, and
    # for a place input the question does not apply (None).  The gate above uses
    # the area text because a postcode can arrive inside it rather than beside it.
    postcode_is_boundary = postcode_defines_the_area(
        postcode_in, source, matched_category, matched_type,
        dataset_kind=str(boundary_detail.get("boundary_kind") or ""),
    )

    resolution = {
        "area": area,
        "query": search_area,
        "input_type": input_type or ("postcode" if looks_like_postcode(area) else "area"),
        "country_code": resolved_cc,
        "country": country_name(resolved_cc),
        "city": (address.get("city") or address.get("town") or address.get("village")
                 or address.get("municipality") or address.get("county") or ""),
        "matched": props.get("display_name"),
        "osm_type": props.get("osm_type"),
        "osm_id": props.get("osm_id"),
        "polygon_source": source,
        "bbox": [float(v) for v in bbox],
        "polygon": polygon,
        # The area of the BOUNDARY, not of its envelope.  Both are reported so
        # the difference is visible rather than hidden behind one number.
        "area_km2": round(polygon_area_km2(polygon), 3),
        "bbox_km2": round(polygon_area_km2(bbox_polygon(tight)), 3),
        # What was matched, and whether a postcode input actually resolved to a
        # postcode boundary (None when the input was not a postcode).
        "matched_category": matched_category,
        "matched_type": matched_type,
        "postcode_is_boundary": postcode_is_boundary,
        "resolved_at": _now().isoformat(timespec="seconds"),
        # What the datasets looked like when this was resolved, so a later ingest
        # invalidates it instead of the cache hiding the new polygons.
        "dataset_stamp": stamp,
        **boundary_detail,
    }
    if schema_ready():
        init_schema()
        _execute(
            f"INSERT INTO {OSM_SCHEMA}.area_cache "
            f"(area_key, area, display_name, osm_type, osm_id, bbox, polygon, polygon_source, resolution) "
            f"VALUES (%s, %s, %s, %s, %s, {_POLYGON_JSON}, {_POLYGON_JSON}, %s, %s::jsonb) "
            f"ON CONFLICT (area_key) DO UPDATE SET "
            f"display_name = EXCLUDED.display_name, bbox = EXCLUDED.bbox, "
            f"polygon = EXCLUDED.polygon, polygon_source = EXCLUDED.polygon_source, "
            f"resolution = EXCLUDED.resolution, resolved_at = now()",
            (
                key, area, resolution["matched"], resolution["osm_type"],
                int(resolution["osm_id"]) if str(resolution["osm_id"] or "").isdigit() else None,
                json.dumps(bbox_polygon(resolution["bbox"])), json.dumps(polygon),
                source, json.dumps(resolution),
            ),
        )
    return resolution


def _resolution_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    cached = dict(row.get("resolution") or {})
    if row.get("bbox_json"):
        cached["bbox"] = [float(v) for v in polygon_bbox(json.loads(row["bbox_json"]))]
    if row.get("poly_json"):
        cached["polygon"] = json.loads(row["poly_json"])
    cached.setdefault("polygon_source", row.get("polygon_source"))
    cached.setdefault("matched", row.get("display_name"))
    return cached


# ---------------------------------------------------------------------------
# The local OSM store -- fetched automatically, reused afterwards
# ---------------------------------------------------------------------------

def covered_by_cache(bbox: Sequence[float]) -> Optional[Dict[str, Any]]:
    """Is this bbox already inside a fresh CURRENT extract we fetched before?

    The extract version is part of the test: an extract written before the full
    `tags` column existed holds the geometries but no attributes, so serving it
    would make every object's attribute list silently empty rather than absent.
    """
    if not schema_ready():
        return None
    rows = _query(
        f"SELECT id, source, detail, counts, fetched_at, ST_AsGeoJSON(bbox) AS bbox_json "
        f"FROM {OSM_SCHEMA}.extract_meta "
        f"WHERE fetched_at > %s AND version = %s AND ST_Contains(bbox, {_POLYGON_JSON}) "
        f"ORDER BY fetched_at DESC LIMIT 1",
        (_now() - timedelta(days=STALE_DAYS), _EXTRACT_VERSION, json.dumps(bbox_polygon(bbox))),
    )
    if not rows:
        return None
    row = rows[0]
    counts = row.get("counts") or {}
    # An extract that stored no buildings cannot serve a premise build; treat it
    # as absent so the area re-fetches rather than returning a plausible zero.
    if not counts.get("buildings") and not counts.get("address_nodes"):
        return None
    return row


# The three groups are fetched independently so one slow group never blocks the
# others (Overpass rate-limits and times out per query).
_OVERPASS_GROUPS: Dict[str, str] = {
    "buildings": 'way["building"];way["building:part"];way["addr:housenumber"]',
    "addresses": 'node["addr:housenumber"]',
    "roads": 'way["highway"]',
    "landuse": 'way["landuse"];way["natural"];way["leisure"];way["boundary"="protected_area"]',
}


def overpass_query(body: str, bbox: Sequence[float], timeout: int = 180) -> Tuple[List[Dict[str, Any]], str]:
    """Fetch one group's elements, scoped bbox-first.

    The bbox goes immediately after the element keyword (`way(bbox)[tags]`).
    Tag-first scoping makes Overpass scan the planet and 504s; bbox-first uses
    the spatial index.  Returns (elements, mirror_used).
    """
    w, s, e, n = (float(v) for v in bbox)
    bbox_arg = f"({s},{w},{n},{e})"
    statements = [st.strip() for st in body.split(";") if st.strip()]
    scoped = ";".join(re.sub(r"^(node|way|rel)", rf"\1{bbox_arg}", st) for st in statements)
    query = f"[out:json][timeout:{int(timeout)}];({scoped};);out geom;".encode("utf-8")

    last: Optional[Exception] = None
    for url in OVERPASS_URLS:
        try:
            payload = _http_json(
                url, data=query, timeout=float(timeout) + 30.0,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return list(payload.get("elements") or []), url
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last = exc
    raise RuntimeError(f"Overpass request failed on all mirrors: {last}") from last


def _element_geometry(el: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Overpass `out geom` element -> GeoJSON geometry (point/polygon/linestring)."""
    if el.get("type") == "node":
        if el.get("lon") is None or el.get("lat") is None:
            return None
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    geom = el.get("geometry")
    if not geom or len(geom) < 2:
        return None
    coords = [[g["lon"], g["lat"]] for g in geom]
    if coords[0] == coords[-1] and len(coords) > 3:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


def _tags(el: Dict[str, Any]) -> Dict[str, Any]:
    return el.get("tags") or {}


# Column order per table.  `geom` is always the 2nd entry of a row tuple.
_TABLE_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "buildings": ("osm_id", "geom", "building", "name", "addr_street", "addr_housenumber",
                  "addr_postcode", "addr_city", "addr_suburb", "building_levels", "building_flats",
                  "tags"),
    "address_nodes": ("osm_id", "geom", "addr_street", "addr_housenumber", "addr_postcode",
                      "addr_city", "addr_suburb", "addr_flats", "tags"),
    "roads": ("osm_id", "geom", "highway", "fclass", "name", "ref", "oneway", "bridge",
              "tunnel", "access", "surface", "maxspeed", "lanes", "tags"),
    "landuse": ("osm_id", "geom", "landuse", '"natural"', "leisure", "boundary", "tags"),
}


def classify_elements(elements: Iterable[Dict[str, Any]]) -> Dict[str, List[Tuple[Any, ...]]]:
    """Pure: Overpass elements -> one row list per table.  Testable without a database."""
    buildings: List[Tuple[Any, ...]] = []
    addresses: List[Tuple[Any, ...]] = []
    roads: List[Tuple[Any, ...]] = []
    landuse: List[Tuple[Any, ...]] = []

    for el in elements:
        tags = _tags(el)
        osm_id = el.get("id")
        if osm_id is None:
            continue
        geometry = _element_geometry(el)
        if not geometry:
            continue
        gj = json.dumps(geometry)
        is_node = el.get("type") == "node"
        # The complete tag set is stored verbatim, so an object can be shown
        # with ALL of its attributes instead of only the ones named below.
        ts = json.dumps(tags)

        if is_node and tags.get("addr:housenumber"):
            addresses.append((
                osm_id, gj,
                tags.get("addr:street"), tags.get("addr:housenumber"),
                tags.get("addr:postcode"), tags.get("addr:city"),
                tags.get("addr:suburb"), tags.get("addr:flats"), ts,
            ))
            continue

        if tags.get("building") or tags.get("building:part") or tags.get("addr:housenumber"):
            if geometry.get("type") == "Polygon":
                buildings.append((
                    osm_id, gj, tags.get("building") or tags.get("building:part"),
                    tags.get("name"), tags.get("addr:street"),
                    tags.get("addr:housenumber"), tags.get("addr:postcode"),
                    tags.get("addr:city"), tags.get("addr:suburb"),
                    tags.get("building:levels"), tags.get("building:flats"), ts,
                ))
                continue

        if tags.get("highway"):
            # A closed way tagged highway (roundabout, loop, small square) comes
            # back as a Polygon.  Dropping it -- which the first version did --
            # silently removes real roads from the routing graph and leaves gaps
            # the design then has to route around.  A ring is still a line.
            if geometry.get("type") == "Polygon":
                ring = (geometry.get("coordinates") or [[]])[0]
                if len(ring) < 2:
                    continue
                geometry = {"type": "LineString", "coordinates": ring}
                gj = json.dumps(geometry)
            if geometry.get("type") == "LineString":
                # fclass is the OSM highway class -- the same vocabulary the
                # existing projects' roads files use, and the one the permit
                # engine maps to a road authority.
                roads.append((
                    osm_id, gj, tags.get("highway"), tags.get("highway"),
                    tags.get("name"), tags.get("ref"), tags.get("oneway"),
                    tags.get("bridge"), tags.get("tunnel"), tags.get("access"),
                    tags.get("surface"), tags.get("maxspeed"), tags.get("lanes"), ts,
                ))
            continue

        if tags.get("landuse") or tags.get("natural") or tags.get("leisure") or tags.get("boundary"):
            landuse.append((
                osm_id, gj, tags.get("landuse"), tags.get("natural"),
                tags.get("leisure"), tags.get("boundary"), ts,
            ))

    return {
        "buildings": buildings,
        "address_nodes": addresses,
        "roads": roads,
        "landuse": landuse,
    }


def _insert_rows(table: str, rows: Sequence[Tuple[Any, ...]]) -> int:
    """Upsert one table's rows; geom always GeoJSON, keyed on osm_id.

    Batched as ONE multi-row statement per page via ``execute_values``.  Plain
    ``executemany`` is a trap here and was measured as such: the connection is
    autocommit and the database is REMOTE, so every row became its own round
    trip and its own transaction — caching one area's 15,583 buildings and
    8,291 address points took ~10 minutes of pure latency, while the Overpass
    fetches themselves were seconds.  (`permits/analysis/road_class.py` already
    reaches for ``execute_values`` over the same remote database for the same
    reason.)
    """
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    cols = _TABLE_COLUMNS[table]
    # Refresh EVERY column on conflict, not just the geometry.  ``SET geom = ...``
    # alone was written when geom was all that mattered, and it silently kept
    # stale values for everything else: a re-fetch after the `tags` column was
    # added left all 2,358 buildings with tag_count 0, because every row already
    # existed and the conflict path never touched the new column.  It would
    # equally have ignored a corrected `building:levels` from OSM.
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[2:])
    sql = (
        f"INSERT INTO {OSM_SCHEMA}.{table} "
        f"(osm_id, geom, {', '.join(cols[2:])}) VALUES %s "
        f"ON CONFLICT (osm_id) DO UPDATE SET {updates}"
    )
    # `tags` is the LAST column in every table and needs an explicit cast:
    # psycopg2 sends a Python str as text, and PostgreSQL has no implicit
    # text -> jsonb assignment, so an uncast insert fails outright.
    extra = len(cols) - 2
    middle = ", ".join(["%s"] * max(0, extra - 1))
    template = (
        f"(%s, {_POLYGON_JSON}, "
        f"{middle + ', ' if middle else ''}%s::jsonb)"
    )
    # A way may match more than one Overpass selector in the same area query
    # (for example `building` and `building:part`). PostgreSQL rejects a single
    # ON CONFLICT statement when the proposed rows contain the same key twice,
    # so keep the last complete representation for each OSM object.
    unique_rows = list({int(row[0]): row for row in rows}.values())
    conn = postgis.get_conn()
    with conn.cursor() as cur:
        execute_values(cur, sql, unique_rows, template=template, page_size=500)
    return len(unique_rows)


def store_overpass_elements(elements: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Store classified elements in the `osm` schema.  Returns per-table counts."""
    rows_by_table = classify_elements(elements)
    return {table: _insert_rows(table, rows) for table, rows in rows_by_table.items()}


# -----------------------------------------------------------------------
# Single-flight + progress for the Overpass fetch
# -----------------------------------------------------------------------
# One area's OSM download is a 10-16 minute job on a cold city (measured:
# Southampton 56 km^2 took ~16 min, Kreuzberg ~1.5 min).  Three things used
# to go wrong because of that:
#
#   1. The page asked for the boundary, drew it, and then issued ONE blocking
#      call for the counts.  The gateway cut that call at 900 s, so a cold area
#      ended as a 502: boundary on the map, nothing else.
#   2. Once the counts did arrive, the page fired SEVEN input-layer calls in
#      parallel.  Each one re-ran ensure_area_data() and, because the cache
#      only gets written when the WHOLE fetch finishes, each started its own
#      Overpass download of the same bbox.
#   3. Nothing reported progress, so a wait that is working correctly looks
#      exactly like a hang.
#
# So: one fetch per bbox (keyed, single-flight, everyone else waits on the
# same thread), progress recorded as it goes, and the boundary call can start
# the fetch so it is already running by the time the counts are asked for.
# -----------------------------------------------------------------------

_FETCH_LOCK = threading.RLock()
_FETCHES: Dict[str, Dict[str, Any]] = {}

# Human labels for the fetch phases, so the page can say what is happening
# rather than "loading".
_FETCH_PHASE_LABELS = {
    "queued": "Waiting for the area download to start",
    "fetching_buildings": "Downloading buildings and addresses",
    "fetching_addresses": "Downloading address points",
    "fetching_roads": "Downloading roads",
    "fetching_landuse": "Downloading landuse and natural areas",
    "storing": "Saving the area into the database",
    "ready": "Area data is ready",
    "failed": "The area download failed",
}


# The registry above is in-memory, but the PAGE polls it across process
# lifetimes.  Before this, an engine restart mid-download turned a running
# fetch into `unknown`, and the page then polled a state that could never
# change: the area never became ready, no input layer was ever requested, and
# nothing told the planner why the object points had not appeared.  The phases
# are persisted so a restart can answer honestly -- an interrupted download is
# reported as `failed`, which is startable, rather than as a wait with no end.
_FETCH_STATE_ENV = "HLD_AREA_FETCH_STATE"
_FETCH_STATE_IN_MEMORY = {"memory", "off", "none"}


def _fetch_state_path() -> Optional[Path]:
    """Where the fetch registry is mirrored, or None when it must not be.

    ``HLD_AREA_FETCH_STATE`` overrides the location; ``memory``/``off`` keep the
    registry strictly in-process, which is what the test suite asks for so one
    run's downloads are never read back by the next.
    """
    raw = str(os.environ.get(_FETCH_STATE_ENV, "")).strip()
    if raw.lower() in _FETCH_STATE_IN_MEMORY:
        return None
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "outputs" / "area_fetch_state.json"


def _persist_fetches(snapshot: Dict[str, Dict[str, Any]]) -> None:
    """Write the fetch registry for the next process.  Best-effort."""
    path = _fetch_state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(path.name + ".tmp")
        staging.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
        os.replace(staging, path)
    except OSError:
        # Losing the progress file costs a clearer message, never a run.
        pass


def _load_fetches() -> None:
    """Restore the registry, demoting anything that was in flight at exit."""
    path = _fetch_state_path()
    if path is None:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        loaded = json.loads(raw)
    except ValueError:
        return
    if not isinstance(loaded, dict):
        return
    now = _now().isoformat(timespec="seconds")
    with _FETCH_LOCK:
        for key, entry in loaded.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("state") not in ("ready", "failed"):
                # Nothing is downloading it any more.  Say so, so the page can
                # stop waiting and the retry path is reachable.
                entry["state"] = "failed"
                entry["error"] = entry.get("error") or "engine_restarted_mid_download"
                entry["finished_at"] = entry.get("finished_at") or now
                entry["updated_at"] = entry["finished_at"]
            _FETCHES[key] = entry


# Restore what the last process knew, so a page polling across a restart gets
# an answer it can act on instead of `unknown` forever.
_load_fetches()


def _bbox_key(bbox: Sequence[float]) -> str:
    """A stable key for a fetch: the bbox, rounded so neighbours share it."""
    try:
        return ",".join(f"{round(float(v), 4):.4f}" for v in bbox)
    except (TypeError, ValueError):
        return str(bbox)


def area_fetch_state(area: str = "", bbox: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """What the OSM fetch for this area is doing, for a progress poll.

    Reads only in-memory bookkeeping, so it is safe to call while the download
    is running and never touches the database.  ``cached`` says the data is
    already there (no download needed) -- the state a preview returns from.
    """
    result: Dict[str, Any] = {
        "state": "unknown",
        "label": "",
        "area": area,
        "fetching": False,
        "groups_done": 0,
        "groups_total": len(_OVERPASS_GROUPS),
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
        "elapsed_s": None,
        "detail": None,
        "source": None,
        "counts": {},
        "error": None,
    }
    key = _bbox_key(bbox) if bbox is not None else None
    with _FETCH_LOCK:
        entry = _FETCHES.get(key) if key else None
        if entry is not None:
            result.update(
                {
                    "state": entry["state"],
                    "label": _FETCH_PHASE_LABELS.get(entry["state"], entry["state"]),
                    "fetching": entry["state"] not in ("ready", "failed"),
                    "groups_done": entry.get("groups_done", 0),
                    "started_at": entry.get("started_at"),
                    "updated_at": entry.get("updated_at"),
                    "finished_at": entry.get("finished_at"),
                    "detail": entry.get("detail"),
                    "source": entry.get("source"),
                    "counts": entry.get("counts") or {},
                    "error": entry.get("error"),
                }
            )
    if result["started_at"]:
        try:
            end = result["finished_at"] or _now().isoformat(timespec="seconds")
            started = datetime.fromisoformat(str(result["started_at"]))
            finished = datetime.fromisoformat(str(end))
            result["elapsed_s"] = max(0, int((finished - started).total_seconds()))
        except (TypeError, ValueError):
            result["elapsed_s"] = None
    if result["state"] == "unknown" and not result["fetching"]:
        result["label"] = "Checking whether this area is already downloaded"
    return result


def _set_fetch_state(
    key: str,
    state: str,
    *,
    groups_done: Optional[int] = None,
    detail: Optional[str] = None,
    source: Optional[str] = None,
    counts: Optional[Dict[str, int]] = None,
    error: Optional[str] = None,
    reset: bool = False,
) -> None:
    with _FETCH_LOCK:
        entry = _FETCHES.setdefault(
            key,
            {
                "state": "queued",
                "groups_done": 0,
                "started_at": _now().isoformat(timespec="seconds"),
                "updated_at": None,
                "finished_at": None,
                "detail": None,
                "source": None,
                "counts": {},
                "error": None,
            },
        )
        if reset:
            # A new attempt must not inherit the previous one's counts: a
            # retry that failed after storing buildings used to report
            # "buildings: 83245" beside "0 of 4 groups", which is unreadable
            # and hides the fact that the earlier download had failed.
            entry["groups_done"] = 0
            entry["detail"] = None
            entry["source"] = None
            entry["counts"] = {}
            entry["error"] = None
            entry["finished_at"] = None
            entry["started_at"] = _now().isoformat(timespec="seconds")
        entry["state"] = state
        if groups_done is not None:
            entry["groups_done"] = int(groups_done)
        if detail is not None:
            entry["detail"] = detail
        if source is not None:
            entry["source"] = source
        if counts is not None:
            entry["counts"] = counts
        if error is not None:
            entry["error"] = error
        entry["updated_at"] = _now().isoformat(timespec="seconds")
        if state in ("ready", "failed"):
            entry["finished_at"] = entry["updated_at"]
        snapshot = {k: dict(v) for k, v in _FETCHES.items()}
    _persist_fetches(snapshot)


def _run_area_fetch(key: str, area: str, bbox: Sequence[float]) -> None:
    """Fetch + store one bbox.  Runs on its own thread; the lock serialises."""
    groups = list(_OVERPASS_GROUPS.items())
    done = 0
    mirror_used = OVERPASS_URLS[0]
    # The counts belong to THIS extract: what each group's insert wrote. The
    # store's totals would claim a 40 km^2 village had stored every building
    # the platform has ever seen, and extract_meta is what the cache check and
    # the provenance line read.
    counts: Dict[str, int] = {}
    try:
        # Every attempt starts from zero, whether it was started here or
        # entered directly through ensure_area_data().
        _set_fetch_state(key, "queued", groups_done=0, reset=True)
        for group, body in groups:
            _set_fetch_state(key, f"fetching_{group}", groups_done=done)
            group_elements, mirror_used = overpass_query(body, bbox)
            done += 1
            # Store each group as it arrives instead of holding every element in
            # memory until the last one lands: a city download is tens of
            # thousands of elements, and the ones already in make the counts and
            # the point layers available to anything reading the store.
            for table, written in (store_overpass_elements(group_elements) or {}).items():
                counts[table] = counts.get(table, 0) + int(written or 0)
            _set_fetch_state(
                key, f"fetching_{group}", groups_done=done, detail=mirror_used,
                counts=dict(counts),
            )
        _set_fetch_state(key, "storing", groups_done=len(groups), detail=mirror_used)
        _execute(
            f"INSERT INTO {OSM_SCHEMA}.extract_meta (area_key, source, detail, bbox, counts, version) "
            f"VALUES (%s, %s, %s, {_POLYGON_JSON}, %s::jsonb, %s)",
            (area_key(area), EXTRACT_SOURCE, mirror_used,
             json.dumps(bbox_polygon(bbox)), json.dumps(counts), _EXTRACT_VERSION),
        )
        _set_fetch_state(
            key, "ready", groups_done=len(groups), source="fetched",
            detail=mirror_used, counts=counts,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the waiting callers
        # The failed entry is KEPT, not deleted: it is how the page says WHY the
        # download stopped, and a retry is allowed because start_area_fetch()
        # treats a failed entry as startable rather than in-flight.
        _set_fetch_state(key, "failed", groups_done=done, error=str(exc))


def start_area_fetch(area: str, bbox: Sequence[float]) -> Dict[str, Any]:
    """Kick off (or join) the OSM download for a bbox.  Returns immediately.

    This is what the boundary-only call invokes, so the download is already
    running by the time the page asks for the counts.  Joining an in-flight
    fetch for the same bbox is the point: seven input-layer calls must not
    become seven downloads of the same city.
    """
    key = _bbox_key(bbox)
    with _FETCH_LOCK:
        entry = _FETCHES.get(key)
        if entry and entry["state"] != "failed":
            # Someone is already downloading this bbox (or just finished it) --
            # join them rather than starting a second copy of the same download.
            return area_fetch_state(area, bbox)
    if not postgis.is_available():
        return {"state": "failed", "error": "postgis_unavailable", "fetching": False}
    _set_fetch_state(key, "queued", groups_done=0, reset=True)
    thread = threading.Thread(
        target=_run_area_fetch, args=(key, area, list(bbox)),
        name=f"osm-fetch-{key[:12]}", daemon=True,
    )
    thread.start()
    return area_fetch_state(area, bbox)


def ensure_area_data(area: str, bbox: Sequence[float]) -> Dict[str, Any]:
    """Make sure the local store holds OSM data for this bbox, fetching if not.

    This is the automat: no operator runs an import.  A fresh overlapping
    extract is reused; otherwise the download runs once and every caller that
    needs the same bbox waits for that one run rather than starting its own.
    """
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    init_schema()

    cached = covered_by_cache(bbox)
    if cached:
        return {
            "source": "cache",
            "detail": cached.get("detail"),
            "fetched_at": cached["fetched_at"].isoformat(timespec="seconds")
            if hasattr(cached.get("fetched_at"), "isoformat") else str(cached.get("fetched_at")),
            "counts": cached.get("counts") or {},
        }

    key = _bbox_key(bbox)
    # Wait for an in-flight download of THIS bbox (started by the boundary
    # call, or by an earlier preview) instead of starting a second one.
    if _join_inflight(key):
        # Re-check the cache: the fetch we waited for wrote it.
        cached = covered_by_cache(bbox)
        if cached:
            return {
                "source": "cache",
                "detail": cached.get("detail"),
                "fetched_at": cached["fetched_at"].isoformat(timespec="seconds")
                if hasattr(cached.get("fetched_at"), "isoformat") else str(cached.get("fetched_at")),
                "counts": cached.get("counts") or {},
            }
        state = area_fetch_state(area, bbox)
        if state.get("state") == "failed" and state.get("error"):
            raise RuntimeError(f"overpass_fetch_failed: {state['error']}")
    # Nothing in flight and nothing cached: fetch it here, on this thread.
    # A previous FAILED attempt does not block this one — its entry is cleared
    # so a retry actually retries.
    with _FETCH_LOCK:
        entry = _FETCHES.get(key)
        if entry is None or entry["state"] in ("ready", "failed"):
            _FETCHES.pop(key, None)
    _set_fetch_state(key, "queued", groups_done=0)
    _run_area_fetch(key, area, list(bbox))
    state = area_fetch_state(area, bbox)
    if state.get("state") == "failed":
        raise RuntimeError(f"overpass_fetch_failed: {state.get('error') or 'unknown'}")
    return {
        "source": "fetched",
        "detail": state.get("detail"),
        "fetched_at": state.get("finished_at") or _now().isoformat(timespec="seconds"),
        "counts": state.get("counts") or {},
    }


def _join_inflight(key: str) -> bool:
    """Wait for someone else's download of this bbox.  True if one was running.

    The wait is bounded (FETCH_WAIT_SECONDS) and polls the state, so a caller
    that arrives after the download already finished returns immediately.
    """
    with _FETCH_LOCK:
        entry = _FETCHES.get(key)
        if not entry or entry["state"] in ("ready", "failed"):
            return False
    deadline = time.time() + FETCH_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(0.5)
        with _FETCH_LOCK:
            entry = _FETCHES.get(key)
            if entry is None or entry["state"] in ("ready", "failed"):
                return True
    return True


def ensure_area_data_background(area: str, bbox: Sequence[float]) -> Dict[str, Any]:
    """Start the download if needed and return at once (boundary-call path).

    Refuses to auto-start above AUTO_FETCH_MAX_KM2: see that constant. The
    refusal is a normal, reported state -- the page shows the size and the
    narrowing advice instead of silently downloading a county.
    """
    if not bbox or len(list(bbox)) != 4:
        return {"state": "not_started", "fetching": False, "label": ""}
    try:
        km2 = polygon_area_km2(bbox_polygon(bbox))
    except Exception:  # noqa: BLE001 - an odd geometry must not block the map
        km2 = 0.0
    if km2 > AUTO_FETCH_MAX_KM2:
        return {
            "state": "not_started",
            "fetching": False,
            "label": "",
            "too_large_km2": round(km2, 1),
            "auto_fetch_max_km2": AUTO_FETCH_MAX_KM2,
            "reason": (
                f"This boundary is {km2:.0f} km², above the {AUTO_FETCH_MAX_KM2:.0f} km² "
                "the area page downloads automatically. Narrow it to a ward, "
                "postcode or district first — the preview will still report its size."
            ),
        }
    try:
        if postgis.is_available() and covered_by_cache(bbox):
            return {
                "state": "ready",
                "source": "cache",
                "detail": "already downloaded",
                "fetching": False,
                "label": "This area's OpenStreetMap data is already downloaded",
                "counts": {},
            }
    except Exception:  # noqa: BLE001 - the fetch below reports the real error
        pass
    return start_area_fetch(area, bbox)


# ---------------------------------------------------------------------------
# The household rule -- PURE functions, unit-tested without a database
# ---------------------------------------------------------------------------

def parse_flats(value: Any) -> Optional[int]:
    """Dwelling count from an OSM `flats` tag.

    Berlin data carries all of `"3"`, `"1-6"` (a range) and `"1;3;5"` (a list).
    A range means the highest dwelling number; a list means its length.  Returns
    None when there is nothing usable, so the caller falls through to the
    heuristic rather than inventing a zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return max(1, int(text))
    for pattern in (r"(\d+)\s*[-–]\s*(\d+)", r"(\d+)\s*(?:to|bis)\s*(\d+)"):
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            return max(1, high - low + 1) if high >= low else None
    parts = [p for p in re.split(r"[,;/|]+", text) if p.strip()]
    if len(parts) > 1:
        return len(parts)
    m = re.search(r"\d+", text)
    return max(1, int(m.group())) if m else None


def unit_area_for(building_type: Optional[str]) -> float:
    key = (building_type or "").strip().lower()
    if key in UNIT_AREA_M2:
        return UNIT_AREA_M2[key]
    if "apart" in key or "flat" in key:
        return UNIT_AREA_M2["apartments"]
    if "terrace" in key or "row" in key:
        return UNIT_AREA_M2["terrace"]
    if "detached" in key or "house" in key or "villa" in key:
        return UNIT_AREA_M2["detached"]
    return UNIT_AREA_M2["default"]


def parse_height_m(value: Any) -> Optional[float]:
    """Building height in metres from an OSM `building:height` tag.

    Handles "12", "12.5 m" and imperial ("39 ft", "39'"). None when there is
    nothing usable, so the caller falls through rather than inventing a floor.
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    height = float(m.group(1))
    if re.search(r"ft|feet|'", text):
        height *= 0.3048
    return height if height > 0 else None


LEVEL_HEIGHT_M = 3.0   # metres per storey used to turn a height into levels


def _tags_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """A store row's raw OSM tags (the `tags` JSONB column), as a dict."""
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            return {}
    return tags if isinstance(tags, dict) else {}


def estimate_households(building: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """Dwellings for a building: OSM tags first, heuristic second.

    Reads BOTH the named columns and the raw `tags` JSON — the ingest does not
    promote every tag to a column, and `building:flats` / `addr:flats` /
    `building:height` are exactly the tags that carry real dwelling data.

    Order: `building:flats`, then `addr:flats` (an explicit dwelling count on
    the address), then `building:levels` x footprint, then `building:height` x
    footprint. Returns (count, method). `method` travels with the number to
    every layer downstream, so an estimate is never mistaken for a survey.
    """
    tags = _tags_of(building)

    tagged = (parse_flats(building.get("building_flats"))
              or parse_flats(tags.get("building:flats")))
    if tagged:
        return min(tagged, MAX_FLATS_PER_BUILDING), "building_flats"

    addr_flats = (parse_flats(building.get("addr_flats"))
                  or parse_flats(tags.get("addr:flats")))
    if addr_flats:
        return min(addr_flats, MAX_FLATS_PER_BUILDING), "addr_flats"

    levels = (parse_flats(building.get("building_levels"))
              or parse_flats(tags.get("building:levels")))
    method = "levels_x_footprint"
    if not levels:
        height_m = parse_height_m(tags.get("building:height"))
        if height_m:
            levels = max(1, int(round(height_m / LEVEL_HEIGHT_M)))
            method = "height_x_footprint"

    footprint = building.get("footprint_m2")
    try:
        footprint_f = float(footprint) if footprint is not None else 0.0
    except (TypeError, ValueError):
        footprint_f = 0.0

    if levels and footprint_f > 0:
        per_floor = footprint_f / unit_area_for(building.get("building"))
        estimate = int(math.ceil(min(levels, 12) * per_floor))
        return max(1, min(estimate, MAX_FLATS_PER_BUILDING)), method

    return None, None


def distribution_for(total: int, keys: Sequence[str]) -> List[int]:
    """Split a building's dwelling count across its address points.

    Summing per-address estimates would double-count badly, so the parts must
    sum EXACTLY to the building total: `max(1, total // n)` each, with the whole
    remainder added to the first (lowest-numbered) address.
    """
    n = len(keys)
    if n == 0:
        return []
    total = max(int(total), n)
    base = max(1, total // n)
    out = [base] * n
    out[0] += total - base * n
    return out


def _sort_key_housenumber(value: Any) -> Tuple[int, float, str]:
    """Order addresses numerically so '2' sorts before '10'."""
    text = str(value or "")
    m = re.match(r"\s*(\d+)", text)
    if m:
        return (0, float(m.group(1)), text)
    return (1, 0.0, text)


def _premise_postcode(item: Dict[str, Any]) -> str:
    """The postcode of a queued premise, from its address or its building.

    Address first, then building: a building polygon often carries a postcode
    that its address nodes do not, and vice versa, so either can be the only one
    present. Normalised through the register's own function, because a join that
    misses on `B113SA` vs `B11 3SA` is a join that silently returns nothing.
    """
    addr = item.get("addr") or {}
    building = item.get("building") or {}
    return household_register.normalize_postcode(
        addr.get("addr_postcode") or building.get("addr_postcode")
    )


def _premise_uprn(item: Dict[str, Any]) -> str:
    """A UPRN on the premise, from either row's tags.

    UPRN is a `ref:uprn` (or `uprn`) tag in OSM, so it is read from the raw tag
    bag rather than from a promoted column -- the ingest does not promote every
    tag, and this is one of the tags that carries real addressing data.
    """
    for row in (item.get("addr") or {}, item.get("building") or {}):
        tags = _tags_of(row)
        for key in ("ref:uprn", "uprn", "addr:uprn"):
            value = str(tags.get(key) or "").strip()
            if value:
                return value
    return ""


def _apply_household_register(
    pending: List[Dict[str, Any]], register: Dict[str, Any]
) -> Dict[str, Any]:
    """Replace heuristic household counts with register counts, in place.

    Mutates the queued premises' `hh` / `method`, and returns the stats the
    preview and the run log carry.  The per-premise logic itself lives in
    `household_register.apply_register` -- this is only the adapter that turns a
    queued premise into the flat dict that function works on and copies the
    result back.
    """
    by_postcode = register.get("by_postcode") or {}
    by_uprn = register.get("by_uprn") or {}
    flat: List[Dict[str, Any]] = [
        {
            "HH": int(item["hh"]),
            "HH_METHOD": item["method"],
            "Postcode": _premise_postcode(item),
            "UPRN": _premise_uprn(item),
        }
        for item in pending
    ]
    resolved, stats = household_register.apply_register(flat, by_postcode, by_uprn)
    for item, row in zip(pending, resolved):
        item["hh"] = int(row.get("HH") or 1)
        item["method"] = str(row.get("HH_METHOD") or item["method"])
    stats["source"] = register.get("source")
    stats["licence"] = register.get("licence")
    stats["vintage"] = register.get("vintage")
    return stats


def _register_payload(
    stats: Optional[Dict[str, Any]], register: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """What a preview/build payload says about the household register.

    None when no register was consulted, rather than a block of falses: "the
    register was not used" and "the register was used and matched nothing" are
    different answers, and a client should be able to tell them apart.
    """
    if not stats or not stats.get("enabled"):
        return None
    payload = {
        "enabled": True,
        "premises_registered": stats.get("premises_registered"),
        "postcodes_matched": stats.get("postcodes_matched"),
        "postcodes_seen": stats.get("postcodes_seen"),
        "households_before": stats.get("heuristic_households_before"),
        "households_after": stats.get("heuristic_households_after"),
        "caveat": household_register.REGISTER_CAVEAT,
    }
    for key in ("source", "licence", "vintage"):
        value = stats.get(key) or (register or {}).get(key)
        if value:
            payload[key] = value
    if stats.get("below_premise_count"):
        payload["below_premise_count"] = stats["below_premise_count"]
        payload["shortfall"] = stats.get("shortfall", 0)
    return payload


def household_register_for(
    country_code: str, postcodes: Sequence[str]
) -> Optional[Dict[str, Any]]:
    """The external household register's lookup for this area, or None.

    Takes the area's POSTCODES rather than its premises, because the register is
    consulted while the premises are being built (inside `assemble_premises`) --
    it decides the household count, so it cannot need the finished rows first.
    The postcodes come from the same address/building rows, so they are available
    a moment earlier.

    Returns None -- meaning "carry on with the OSM heuristic" -- in every case
    that is not a usable register: the knob is off, the country is not one the
    register covers, nothing is loaded, or none of this area's postcodes match.
    The distinction matters, because a register that matched nothing and a
    register that was never loaded are very different things for a planner to be
    told, and `build_inputs` reports which happened.
    """
    if not household_register.REGISTER_ENABLED:
        return None
    code = normalize_country_code(country_code)
    if code not in household_register.REGISTER_COUNTRIES:
        return None
    by_postcode, by_uprn, meta = household_register.load_register(code, postcodes)
    if not by_postcode and not by_uprn:
        return None
    return {
        "by_postcode": by_postcode,
        "by_uprn": by_uprn,
        "source": meta.get("source"),
        "licence": meta.get("licence"),
        "vintage": meta.get("vintage"),
        "meta": meta,
    }


def area_postcodes(
    buildings: Sequence[Dict[str, Any]], addresses: Sequence[Dict[str, Any]]
) -> List[str]:
    """Every postcode on the area's address and building rows, deduplicated."""
    out: List[str] = []
    for row in list(addresses) + list(buildings):
        value = row.get("addr_postcode")
        if value:
            out.append(str(value))
    return out


def household_register_warning(stats: Optional[Dict[str, Any]]) -> Optional[str]:
    """The sentence a planner needs when a register did NOT decide the answer.

    Returns None when there is nothing to say -- no register, or one that
    covered the area cleanly.  A register is a change to the number the cable
    sizing and the BOQ are computed from, so where it applied, where it did not,
    and where it disagreed with the premise set all have to be visible rather
    than inferred from a total that quietly moved.
    """
    if not stats or not stats.get("enabled"):
        return None
    matched = int(stats.get("postcodes_matched") or 0)
    seen = int(stats.get("postcodes_seen") or 0)
    registered = int(stats.get("premises_registered") or 0)
    before = int(stats.get("heuristic_households_before") or 0)
    after = int(stats.get("heuristic_households_after") or 0)
    if not registered:
        return None
    parts = [
        f"Household counts for {registered} premise(s) across {matched} postcode(s) "
        f"come from the external household register"
        + (f" ({stats.get('source')})" if stats.get("source") else "")
        + f", not from the OpenStreetMap building heuristic: the area's household "
        f"total moves from {before} to {after}."
    ]
    if seen > matched:
        parts.append(
            f"{seen - matched} postcode(s) in this area are not in the register, "
            "so their premises keep the heuristic count."
        )
    if stats.get("below_premise_count"):
        parts.append(
            f"In {stats['below_premise_count']} postcode(s) the register counts "
            f"FEWER households than the area has premises ({stats.get('shortfall', 0)} "
            "in total), so each premise there is floored at 1 and the register's "
            "total is not reproduced -- the register and the premise set disagree."
        )
    parts.append(household_register.REGISTER_CAVEAT)
    return " ".join(parts)


def assemble_premises(
    buildings: Sequence[Dict[str, Any]],
    addresses: Sequence[Dict[str, Any]],
    addr_to_building: Dict[int, int],
    country: str = "",
    city: str = "",
    register: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build premise rows from the OSM store.

    PURE: takes plain dicts plus a {address_osm_id: building_osm_id} map (built
    by a SQL spatial join in `read_area_rows`), so it is testable without a
    database.  `register` is the external household register's already-loaded
    lookup (see `household_register.load_register`); it is passed in rather than
    read here so this function stays database-free.

    Source priority, first match wins:
      1. an `addr:housenumber` NODE inside the area
      2. an `addr:housenumber` BUILDING polygon with no node
      3. a building CENTROID, for a residential building with no address at all

    The household estimate or register allocation is stored as `HH` on each
    physical building/service-location record. Multiple address nodes inside a
    building remain separate service locations; they are not multiplied by the
    household count to invent unit geometries.
    """
    by_id = {int(b["osm_id"]): b for b in buildings}
    stats = {
        "duplicates_merged": 0,
        "buildings_excluded": 0,
        "boundary_buildings": 0,
    }

    # --- 1/2. address points, grouped by the building they sit in -----------
    # Each OSM address node is a physical service location. Keep distinct nodes
    # (including repeated street/number values), but never replicate a location
    # for each logical household.


    grouped: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for addr in addresses:
        housenumber = str(addr.get("addr_housenumber") or "").strip()
        if not housenumber:
            continue
        grouped.setdefault(addr_to_building.get(int(addr["osm_id"])), []).append(addr)
    # Kept for API compatibility; no merging or coordinate jitter is performed.
    stats["duplicates_merged"] = 0

    def _source_lonlat(lon: float, lat: float) -> Tuple[float, float]:
        """Keep the source service-entry coordinate; coincident points are valid."""
        return round(float(lon), 7), round(float(lat), 7)

    premises: List[Dict[str, Any]] = []

    def _row(addr: Dict[str, Any], building: Optional[Dict[str, Any]], hh: int,
             method: str, suffix: str, lonlat: Tuple[float, float]) -> Dict[str, Any]:
        oid = int(building["osm_id"]) if building else None
        return {
            "ADDR_ID": f"OSM-{suffix}",
            "Address": (addr.get("addr_street") or (building or {}).get("addr_street") or ""),
            "Housenumber": (addr.get("addr_housenumber") or (building or {}).get("addr_housenumber") or ""),
            # The country/city come from the OSM tags when present and from the
            # resolved area otherwise.  They used to be the literals "Berlin"
            # and "Germany", which mislabelled every non-German project in the
            # generated workbook and everything downstream of it.
            "City": addr.get("addr_city") or (building or {}).get("addr_city") or city,
            "Postcode": addr.get("addr_postcode") or (building or {}).get("addr_postcode") or "",
            "Country": country,
            "District": addr.get("addr_suburb") or (building or {}).get("addr_suburb") or "",
            "HH": int(hh),
            "HH_METHOD": method,
            "LATITUDE": round(float(lonlat[1]), 7),
            "LONGITUDE": round(float(lonlat[0]), 7),
            "OSM_ID": oid if oid is not None else int(addr["osm_id"]),
        }

    # Physical service locations are collected first. Register allocation is
    # weighted by each location's building/address estimate, then HH stays as a
    # logical load attribute on that same physical feature.
    pending: List[Dict[str, Any]] = []

    def _emit(addr: Dict[str, Any], building: Optional[Dict[str, Any]], hh: int,
              method: str, suffix: str, lonlat: Tuple[float, float]) -> None:
        """Queue one physical building or address/service location."""
        try:
            n = int(hh)
        except (TypeError, ValueError):
            n = 1
        pending.append({
            "addr": addr,
            "building": building,
            "hh": max(1, n),
            "method": method,
            "suffix": suffix,
            "lonlat": lonlat,
        })

    for building_id, addr_list in grouped.items():
        building = by_id.get(building_id) if building_id is not None else None
        ordered = sorted(addr_list, key=lambda a: _sort_key_housenumber(a.get("addr_housenumber")))

        if building is None:
            # An address with no building polygon: its own tags, else 1.
            # Every node is kept — even two nodes with the same street+number
            # become two premises with distinct points.
            for addr in ordered:
                flats = parse_flats(addr.get("addr_flats"))
                hh = min(flats, MAX_FLATS_PER_BUILDING) if flats else 1
                method = "addr_flats" if flats else "fallback_one"
                _emit(addr, None, hh, method, f"N{int(addr['osm_id'])}",
                      (addr["lon"], addr["lat"]))
            continue

        building_type = str(building.get("building") or "").strip().lower()
        if building_type in EXCLUDED_BUILDINGS:
            stats["buildings_excluded"] += 1
            continue

        total, method = estimate_households(building)
        addr_flats = [parse_flats(a.get("addr_flats")) for a in ordered]

        if total is None:
            # No building-level number: use per-address tags where present.
            per = [min(f, MAX_FLATS_PER_BUILDING) if f else 1 for f in addr_flats]
            methods = ["addr_flats" if f else "fallback_one" for f in addr_flats]
        else:
            # Explicit address counts are fixed loads; distribute the remainder
            # of the building total only across locations without an address
            # count, so the resulting location HH still reconciles to the
            # building estimate whenever its minimum-one floor permits it.
            fixed = [min(f, MAX_FLATS_PER_BUILDING) if f else None for f in addr_flats]
            open_indices = [i for i, value in enumerate(fixed) if value is None]
            per = [int(value) if value is not None else 1 for value in fixed]
            methods = ["addr_flats" if f else method for f in addr_flats]
            if open_indices:
                remaining = max(len(open_indices), int(total) - sum(
                    int(value) for value in fixed if value is not None
                ))
                shares = distribution_for(remaining, [str(i) for i in open_indices])
                for i, value in zip(open_indices, shares):
                    per[i] = value

        multi = len(ordered) > 1
        for i, addr in enumerate(ordered):
            # Suffix uses osm_id when several addresses share the same building
            # and same housenumber — P-index alone would hide which nodes they are.
            if multi and len({str(a.get("addr_housenumber") or "").strip().lower() for a in ordered}) != len(ordered):
                suffix = f"W{building['osm_id']}-N{int(addr['osm_id'])}"
            else:
                suffix = f"W{building['osm_id']}-P{i + 1}" if multi else f"W{building['osm_id']}"
            _emit(addr, building, per[i], methods[i], suffix,
                  (addr["lon"], addr["lat"]))

    # --- 3. buildings with no address node ---------------------------------
    with_addr_node = set(grouped.keys())
    for building in buildings:
        bid = int(building["osm_id"])
        if bid in with_addr_node:
            continue
        building_type = str(building.get("building") or "").strip().lower()
        if building_type in EXCLUDED_BUILDINGS:
            stats["buildings_excluded"] += 1
            continue
        if building.get("lon") is None or building.get("lat") is None:
            continue
        total, method = estimate_households(building)
        if total is None:
            total, method = 1, "fallback_one"
        synthetic = {
            "osm_id": bid,
            "addr_street": building.get("addr_street"),
            "addr_housenumber": building.get("addr_housenumber") or building.get("name"),
            "addr_city": building.get("addr_city"),
            "addr_postcode": building.get("addr_postcode"),
            "addr_suburb": building.get("addr_suburb"),
        }
        _emit(synthetic, building, total, method, f"W{bid}-C",
              (building["lon"], building["lat"]))
        stats["boundary_buildings"] += 1

    # --- 4. allocate register totals across physical locations -------------
    #
    # A postcode total cannot identify which building owns each dwelling. The
    # existing per-location estimate supplies the apportionment weights while
    # the resulting HH stays on each location as logical demand.
    if register and (register.get("by_postcode") or register.get("by_uprn")):
        stats["household_register"] = _apply_household_register(pending, register)

    for item in pending:
        addr, building = item["addr"], item["building"]
        premises.append(_row(
            addr, building, max(1, int(item["hh"])), item["method"],
            item["suffix"], _source_lonlat(item["lonlat"][0], item["lonlat"][1]),
        ))

    return premises, stats


def sub_area_breakdown(premises: Sequence[Dict[str, Any]], limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
    """Premises/households grouped by the subdivisions the data already carries.

    This is how a too-large area gets narrowed honestly: `addr:postcode` and
    `addr:suburb` are real values on the address points we already fetched, so
    "design postcode 12107 instead" is a fact about this area rather than a
    guess about where to draw a line.
    """
    buckets: Dict[str, Dict[str, Dict[str, int]]] = {"postcode": {}, "district": {}}
    for p in premises:
        for key, field in (("postcode", "Postcode"), ("district", "District")):
            value = str(p.get(field) or "").strip()
            if not value:
                continue
            slot = buckets[key].setdefault(value, {"premises": 0, "households": 0})
            slot["premises"] += 1
            slot["households"] += int(p.get("HH") or 0)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, bucket in buckets.items():
        ordered = sorted(bucket.items(), key=lambda kv: -kv[1]["premises"])[:limit]
        out[key] = [{"value": value, **stats} for value, stats in ordered]
    return out


def household_summary(premises: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Mix of methods behind the household total -- the number the design is sized on."""
    by_method: Dict[str, int] = {}
    buckets: Dict[str, int] = {}
    total = 0
    for p in premises:
        hh = int(p.get("HH") or 0)
        total += hh
        by_method[str(p.get("HH_METHOD"))] = by_method.get(str(p.get("HH_METHOD")), 0) + 1
        if hh == 1:
            buckets["1"] = buckets.get("1", 0) + 1
        elif hh <= 4:
            buckets["2-4"] = buckets.get("2-4", 0) + 1
        elif hh <= 12:
            buckets["5-12"] = buckets.get("5-12", 0) + 1
        else:
            buckets["13+"] = buckets.get("13+", 0) + 1
    # A REGISTER count is a published figure, not a rule applied to a building,
    # so it counts as measured here for the same reason an `addr:flats` tag does:
    # both answer "how many homes" with a number somebody stated.  The caveat
    # that a register figure is DWELLINGS rather than occupied households is
    # carried in the register's own note and preview warning, not by pretending
    # the number is a household survey.
    measured_methods = ("building_flats", "addr_flats") + tuple(
        household_register.REGISTER_METHODS
    )
    estimated = total - sum(
        int(p.get("HH") or 0) for p in premises
        if str(p.get("HH_METHOD")) in measured_methods
    )
    return {
        "total": total,
        "by_method": by_method,
        "by_density_bucket": buckets,
        "estimated_share": round(estimated / total, 3) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Reading the store
# ---------------------------------------------------------------------------

# A narrowing chip has to land on a real design AREA, not merely on something
# smaller.  Measured: 421201 and 421202 in Dombivli both resolve to the same
# State Bank of India branch, 0.0 km² -- which is "strictly smaller" than the
# 55.474 km² city by every comparison and is not a place to design anything.
#
# The floor sits far below any real neighbourhood (the smallest polygon measured
# anywhere in this feature is a 1.565 km² ward, and a good OSM postcode is
# 4.38 km²) and far above the largest single object measured (0.008 km² for the
# Birmingham `B1` office block).  Two orders of magnitude of clearance on both
# sides, so it does not depend on where the real values happen to sit.
MIN_NARROWING_AREA_KM2 = 0.05


def postcode_narrows_to_area(
    resolution: Dict[str, Any], postcode: str
) -> bool:
    """Would typing this postcode back resolve to a real area smaller than this one?

    One Nominatim lookup, served from `area_cache` after the first time.  It
    exists only for a country with no loaded boundary dataset, which is the only
    case where whether a postcode narrows is a fact about that country rather
    than a consequence of the boundary we are standing on -- Germany has real
    postcode relations in OSM, India resolves one to a bank.

    True only when the result is both a real area (above the floor) and
    genuinely smaller, so a chip that leads back to the area being previewed is
    rejected as firmly as one that leads nowhere.
    """
    pc = str(postcode or "").strip()
    if not pc:
        return False
    try:
        probe = resolve_area(
            pc,
            country_code=str(resolution.get("country_code") or ""),
            input_type="postcode",
            postcode=pc,
        )
    except Exception:  # noqa: BLE001 - a failed probe must not fail the preview
        return False
    area = probe.get("area_km2")
    try:
        area_f = float(area)
    except (TypeError, ValueError):
        return False
    if area_f < MIN_NARROWING_AREA_KM2:
        return False
    current = resolution.get("area_km2")
    try:
        current_f = float(current)
    except (TypeError, ValueError):
        return True
    return area_f < current_f


def resolvable_kinds(
    resolution: Dict[str, Any], sub_areas: Dict[str, List[Dict[str, Any]]]
) -> List[str]:
    """Which buckets a client may offer as narrowing chips, for this area.

    One place, because the preview and the run's refusal message must not
    disagree about what a chip is worth: a run that refuses with "narrow to
    postcode 421201" while the preview showed no such chip is a contradiction the
    planner has to notice.  Probes the biggest postcode bucket when the country
    has no loaded dataset to reason from (see `postcode_narrows_to_area`).
    """
    verified: Optional[bool] = None
    if not boundary_dataset_kinds(str(resolution.get("country_code") or "")):
        top = (sub_areas.get("postcode") or [{}])[0].get("value")
        if top:
            verified = postcode_narrows_to_area(resolution, str(top))
    return sub_area_kinds_offered(resolution, verified)


def read_area_rows(polygon: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[int, int], List[Dict[str, Any]]]:
    """Buildings, address nodes, the address->building map, and roads, for a polygon.

    The address->building map is computed in SQL (ST_Contains) rather than
    guessed from proximity, so the household distribution is anchored on a real
    containment.  Buildings are selected by their CENTROID being inside the area:
    a building straddling the boundary must count once, in the area that holds
    its centre.
    """
    poly = json.dumps(polygon)
    buildings = _query(
        f"SELECT osm_id, building, name, addr_street, addr_housenumber, addr_postcode, "
        f"       addr_city, addr_suburb, building_levels, building_flats, tags, "
        f"       ST_Area(geom::geography) AS footprint_m2, "
        f"       ST_AsGeoJSON(geom) AS geom_json, "
        f"       ST_X(ST_Centroid(geom)) AS lon, ST_Y(ST_Centroid(geom)) AS lat "
        f"FROM {OSM_SCHEMA}.buildings "
        f"WHERE ST_Contains({_POLYGON_JSON}, ST_Centroid(geom))",
        (poly,),
    )
    addresses = _query(
        f"SELECT osm_id, addr_street, addr_housenumber, addr_postcode, addr_city, "
        f"       addr_suburb, addr_flats, ST_AsGeoJSON(geom) AS geom_json, "
        f"       ST_X(geom) AS lon, ST_Y(geom) AS lat "
        f"FROM {OSM_SCHEMA}.address_nodes WHERE ST_Contains({_POLYGON_JSON}, geom)",
        (poly,),
    )
    join = _query(
        f"SELECT a.osm_id AS addr_id, b.osm_id AS building_id "
        f"FROM {OSM_SCHEMA}.address_nodes a "
        f"JOIN {OSM_SCHEMA}.buildings b ON ST_Contains(b.geom, a.geom) "
        f"WHERE ST_Contains({_POLYGON_JSON}, a.geom)",
        (poly,),
    )
    roads = _query(
        f"SELECT osm_id, highway, fclass, name, ref, oneway, bridge, tunnel, access, "
        f"       surface, maxspeed, lanes, ST_AsGeoJSON(geom) AS geom_json "
        f"FROM {OSM_SCHEMA}.roads WHERE ST_Intersects({_POLYGON_JSON}, geom)",
        (poly,),
    )
    addr_to_building = {int(r["addr_id"]): int(r["building_id"]) for r in join}
    return buildings, addresses, addr_to_building, roads


# Public input-layer names -> their storage table. The `gis.osm_*` tables are
# curated OSM reference layers already used by the permit engine; they are read
# here, never overwritten by an area run. Missing optional reference tables
# return an empty layer rather than hiding the buildings/premises layers.
_INPUT_LAYER_TABLES = {
    "railways": "gis.osm_railway",
    "waterways": "gis.osm_waterway",
    "boundaries": "gis.osm_admin_boundary",
    "protected_areas": "gis.osm_protected_area",
    "trees": "gis.osm_tree",
}


def _feature(geometry: Optional[Dict[str, Any]], properties: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not geometry:
        return None
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def object_properties(row: Dict[str, Any]) -> Dict[str, Any]:
    """One OSM object's full attribute set, as flat feature properties.

    The raw OSM tags come first and unmodified -- they are what OSM actually
    says about the building -- and the derived measurements the design cares
    about are added alongside.  Nothing is renamed, so a tag an inspector looks
    for is present under the name OSM uses.
    """
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            tags = None
    props: Dict[str, Any] = dict(tags) if isinstance(tags, dict) else {}
    # Counted before anything derived is added, so this is the number of real
    # OSM tags on the object.
    tag_count = len(props)

    # The named columns are derived from these same tags, so a NULL column must
    # never overwrite the tag it came from -- an empty column blanking out a real
    # `building=yes` is data loss, not a missing value.
    for key, value in (
        ("building", row.get("building")),
        ("name", row.get("name")),
        ("addr:street", row.get("addr_street")),
        ("addr:housenumber", row.get("addr_housenumber")),
        ("addr:postcode", row.get("addr_postcode")),
        ("addr:city", row.get("addr_city")),
        ("addr:suburb", row.get("addr_suburb")),
        ("building:levels", row.get("building_levels")),
        ("building:flats", row.get("building_flats")),
    ):
        if value is not None and str(value).strip() != "":
            props.setdefault(key, value)

    props.update({
        "osm_object_id": row.get("osm_id"),
        "osm_object_type": "way",
        "latitude": row.get("lat"),
        "longitude": row.get("lon"),
        "building_type": row.get("building"),
        "footprint_m2": (round(float(row["footprint_m2"]), 1)
                         if row.get("footprint_m2") is not None else None),
        "tag_count": tag_count,
    })
    return props


def _json_geometry(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return None


def _reference_layer_exists(table: str) -> bool:
    try:
        rows = _query("SELECT to_regclass(%s) AS name", (table,))
        return bool(rows and rows[0].get("name"))
    except Exception:
        return False


def _reference_features(table: str, polygon: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read a curated OSM reference layer clipped to the selected boundary."""
    if not _reference_layer_exists(table):
        return []
    rows = _query(
        f"SELECT id, ST_AsGeoJSON(geom) AS geom_json, properties "
        f"FROM {table} WHERE ST_Intersects(ST_MakeValid(geom), ST_MakeValid({_POLYGON_JSON}))",
        (json.dumps(polygon),),
    )
    features = []
    for row in rows:
        props = dict(row.get("properties") or {})
        props.update({"source_table": table, "source_id": row.get("id")})
        item = _feature(_json_geometry(row.get("geom_json")), props)
        if item:
            features.append(item)
    return features


def input_layer_geojson(
    polygon: Dict[str, Any],
    layer: str,
    country: str = "",
    city: str = "",
) -> Dict[str, Any]:
    """Return one complete pre-run input layer as GeoJSON.

    This endpoint is intentionally based on the generated OSM snapshot and
    project OSM reference layers only. It does not read a manual workbook or
    any existing HLD design output.
    """
    key = str(layer or "").strip().lower().replace("-", "_")
    allowed = {
        "objects", "buildings", "addresses", "premises", "roads", "landuse",
        "natural", "railways", "waterways", "boundaries", "protected_areas", "trees",
    }
    if key not in allowed:
        raise KeyError(f"Unknown input layer '{layer}'")

    features: List[Dict[str, Any]] = []
    if key in _INPUT_LAYER_TABLES:
        features = _reference_features(_INPUT_LAYER_TABLES[key], polygon)
    else:
        buildings, addresses, addr_to_building, roads = read_area_rows(polygon)
        if key == "buildings":
            for row in buildings:
                props = {k: v for k, v in row.items() if k not in ("geom_json", "lon", "lat")}
                props.update({"latitude": row.get("lat"), "longitude": row.get("lon"),
                              "osm_object_id": row.get("osm_id"), "osm_object_type": "way"})
                item = _feature(_json_geometry(row.get("geom_json")), props)
                if item:
                    features.append(item)
        elif key == "addresses":
            for row in addresses:
                props = {k: v for k, v in row.items() if k not in ("geom_json", "lon", "lat")}
                props.update({"latitude": row.get("lat"), "longitude": row.get("lon"),
                              "osm_object_id": row.get("osm_id"), "osm_object_type": "node"})
                item = _feature(_json_geometry(row.get("geom_json")), props)
                if item:
                    features.append(item)
        elif key == "objects":
            # The objects layer, derived from the OSM store itself: one POINT per
            # building (its centroid) carrying ALL of the building's OSM tags,
            # not just the handful this module names.  This is the layer the HLD
            # object layer is built from, so what a planner reviews here is what
            # the design will actually see.
            for row in buildings:
                props = object_properties(row)
                lon, lat = row.get("lon"), row.get("lat")
                if lon is None or lat is None:
                    continue
                item = _feature({"type": "Point", "coordinates": [lon, lat]}, props)
                if item:
                    features.append(item)
        elif key == "premises":
            premises, _stats = assemble_premises(
                buildings, addresses, addr_to_building, country=country, city=city
            )
            for row in premises:
                props = dict(row)
                lat, lon = props.pop("LATITUDE", None), props.pop("LONGITUDE", None)
                props.update({"latitude": lat, "longitude": lon})
                item = _feature(
                    {"type": "Point", "coordinates": [lon, lat]}
                    if lon is not None and lat is not None else None,
                    props,
                )
                if item:
                    features.append(item)
        elif key == "roads":
            for row in roads:
                geom = _json_geometry(row.get("geom_json"))
                props = {k: v for k, v in row.items() if k != "geom_json"}
                props.update({"osm_object_id": row.get("osm_id"), "osm_object_type": "way"})
                item = _feature(geom, props)
                if item:
                    features.append(item)
        elif key in ("landuse", "natural"):
            # The automatic snapshot currently stores both landuse and natural
            # tags in the same OSM landuse table; separate them at presentation.
            rows = _query(
                f"SELECT osm_id, landuse, \"natural\", leisure, boundary, ST_AsGeoJSON(geom) AS geom_json "
                f"FROM {OSM_SCHEMA}.landuse WHERE ST_Intersects({_POLYGON_JSON}, geom)",
                (json.dumps(polygon),),
            )
            for row in rows:
                if key == "natural" and not row.get("natural"):
                    continue
                if key == "landuse" and not (row.get("landuse") or row.get("leisure") or row.get("boundary")):
                    continue
                props = {k: v for k, v in row.items() if k != "geom_json"}
                item = _feature(_json_geometry(row.get("geom_json")), props)
                if item:
                    features.append(item)

    return {"type": "FeatureCollection", "features": features,
            "layer": key, "feature_count": len(features)}


def write_landuse_geojson(path: str, polygon: Dict[str, Any]) -> Optional[str]:
    """Write `inputs/osm/landuse/landuse.geojson` from the area's own OSM store.

    This is the THIRD file an area run writes, and it exists for exactly one
    consumer: `design.derive_aerial_zones` reads this path to build
    `Aerial_Zones`, which is what makes `AERIAL_REASON = "zone"` reachable. Until
    it was written here, only a manual multi-layer upload could supply landuse,
    so every area-generated run derived no zones at all and silently trenched
    houses standing in parks, nature reserves and cemeteries — the derivation is
    wrapped in a bare `except`, and a design without zones is a valid design.

    The `fclass` property is the contract, not a convenience: the consumer reads
    that field and nothing else, and the store has no such column (it keeps
    `landuse` / `natural` / `leisure` / `boundary` separately, because OSM does).
    So fclass is resolved here, most-specific tag first, and a row whose tags
    resolve to nothing is left out rather than written as an empty class.

    Returns the path, or None when the area holds no landuse at all — an area
    with no parks is not an error, and writing an empty file would make "the
    derivation ran and found nothing" indistinguishable from "there was nothing
    to look at".
    """
    try:
        rows = _query(
            f"SELECT osm_id, landuse, \"natural\", leisure, boundary, "
            f"       ST_AsGeoJSON(geom) AS geom_json "
            f"FROM {OSM_SCHEMA}.landuse WHERE ST_Intersects({_POLYGON_JSON}, geom)",
            (json.dumps(polygon),),
        )
    except Exception:  # noqa: BLE001 - optional input, never fails a run
        return None
    features: List[Dict[str, Any]] = []
    for row in rows:
        fclass = str(row.get("landuse") or row.get("leisure")
                     or row.get("natural") or "").strip()
        if not fclass:
            continue
        geom = _json_geometry(row.get("geom_json"))
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "fclass": fclass,
                "landuse": row.get("landuse"),
                "natural": row.get("natural"),
                "leisure": row.get("leisure"),
                "boundary": row.get("boundary"),
                "osm_id": row.get("osm_id"),
            },
        })
    if not features:
        return None
    payload = {"type": "FeatureCollection", "features": features}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def road_summary(roads: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Road kilometres by fclass -- also the sanity check on the fclass contract."""
    by_class: Dict[str, float] = {}
    total = 0.0
    for r in roads:
        try:
            geom = json.loads(r["geom_json"]) if isinstance(r.get("geom_json"), str) else None
        except (TypeError, ValueError):
            geom = None
        length = _line_length_m(geom)
        total += length
        key = str(r.get("fclass") or r.get("highway") or "unknown")
        by_class[key] = by_class.get(key, 0.0) + length
    return {
        "total_km": round(total / 1000.0, 1),
        "by_fclass": {k: round(v / 1000.0, 1) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
    }


def _line_length_m(geometry: Optional[Dict[str, Any]]) -> float:
    """Planar-ish length of a LineString/MultiLineString in metres."""
    if not geometry:
        return 0.0
    lines: List[List[List[float]]] = []
    if geometry.get("type") == "LineString":
        lines = [geometry.get("coordinates") or []]
    elif geometry.get("type") == "MultiLineString":
        lines = geometry.get("coordinates") or []
    total = 0.0
    for line in lines:
        for a, b in zip(line, line[1:]):
            mid = math.radians((a[1] + b[1]) / 2.0)
            dx = (b[0] - a[0]) * 111_320.0 * math.cos(mid)
            dy = (b[1] - a[1]) * 110_540.0
            total += math.hypot(dx, dy)
    return total


# ---------------------------------------------------------------------------
# Preview -- resolve + report, no pipeline
# ---------------------------------------------------------------------------

def _boundary_payload(
    area: str,
    resolution: Dict[str, Any],
    boundary_only: bool,
    country_code: str = "",
    input_type: str = "",
) -> Dict[str, Any]:
    """The part of the preview that needs no OSM data (Nominatim only)."""
    return {
        "area": area,
        "query": resolution.get("query") or nominatim_query(area),
        "input_type": (resolution.get("input_type")
                       or input_type
                       or ("postcode" if looks_like_postcode(area) else "area")),
        "country_code": resolution.get("country_code") or normalize_country_code(country_code),
        "country": resolution.get("country") or country_name(country_code),
        "city": resolution.get("city") or "",
        "matched": resolution.get("matched"),
        "osm_type": resolution.get("osm_type"),
        "osm_id": resolution.get("osm_id"),
        "bbox": resolution["bbox"],
        "polygon": resolution["polygon"],
        "polygon_source": resolution.get("polygon_source"),
        # The area of the BOUNDARY when one exists, with the envelope reported
        # separately.  Reporting only the bbox overstated postcode 12105 by 26 %.
        "area_km2": (resolution.get("area_km2")
                     if resolution.get("area_km2") is not None
                     else round(polygon_area_km2(resolution.get("polygon")), 3)),
        "bbox_km2": (resolution.get("bbox_km2")
                     if resolution.get("bbox_km2") is not None
                     else round(polygon_area_km2(bbox_polygon(resolution["bbox"])), 3)),
        "boundary_name": resolution.get("boundary_name"),
        "boundary_source": resolution.get("boundary_source"),
        "boundary_kind": resolution.get("boundary_kind"),
        "boundary_admin_level": resolution.get("boundary_admin_level"),
        # Provenance for a dataset rung: which record, which licence, which
        # vintage.  A boundary whose licence cannot be stated is not one a design
        # should be published on without the planner seeing it.
        "boundary_code": resolution.get("boundary_code"),
        "boundary_dataset_km2": resolution.get("boundary_dataset_km2"),
        "boundary_licence": resolution.get("boundary_licence"),
        "boundary_vintage": resolution.get("boundary_vintage"),
        "boundary_note": resolution.get("boundary_note"),
        # What was matched, and — for a postcode input — whether the postcode
        # actually defined the area or was only the search term.  Reported from
        # the boundary call too, so a client can flag it before paying for the
        # OSM fetch rather than after.
        "matched_category": resolution.get("matched_category"),
        "matched_type": resolution.get("matched_type"),
        "postcode_is_boundary": resolution.get("postcode_is_boundary"),
        "resolution_key": resolution_key(area, country_code),
        "boundary_only": boundary_only,
    }


def preview_area(
    area: str,
    max_premises: Optional[int] = None,
    boundary_only: bool = False,
    country_code: str = "",
    input_type: str = "",
    postcode: str = "",
) -> Dict[str, Any]:
    """Area -> boundary, premises and household mix.  Starts nothing.

    `boundary_only` returns as soon as Nominatim resolves the name, so the map
    can draw the boundary immediately.  The premise/household counts need an
    OSM fetch that costs ~2.5 min on a cold area (and is instant once cached),
    which is far too long to hold a map render behind.

    This function does NOT refuse a large area: reporting the size of one, with
    the sub-area buckets to narrow it by, is the whole point of a preview.
    Whether a run can start is reported instead, via `can_run` / `over_run_cap`
    against `MAX_PREMISES`, and the warnings say so in words.  A caller that
    genuinely wants a hard ceiling can pass `max_premises`; when it is exceeded
    the raised TooManyPremises names THAT cap, not the run's.
    """
    resolution = resolve_area(
        area, country_code=country_code, input_type=input_type, postcode=postcode
    )
    base = _boundary_payload(area, resolution, boundary_only, country_code, input_type)
    if boundary_only:
        return base
    # What the download for this area is doing, reported even on the counts
    # path: a caller that waited on someone else's in-flight fetch needs to be
    # able to say so, and a cached area needs to say that too.
    base["osm_fetch"] = area_fetch_state(area, resolution["bbox"])

    bbox = resolution["bbox"]
    warnings: List[str] = []

    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")

    if not country_code and resolution.get("input_type") == "postcode":
        # Measured, not theoretical: a bare "12105" with no country filter
        # resolves to a district in South Korea, and the old code resolved the
        # same input to Germany.  A postal code without a country is not unique,
        # so say which place was actually used instead of returning it silently.
        warnings.append(
            "No country was chosen, so this postcode was matched anywhere in the "
            f"world and resolved to {resolution.get('matched') or 'an unstated place'}. "
            "Choose a country to make the match unambiguous."
        )

    extract = ensure_area_data(area, bbox)
    buildings, addresses, join, roads = read_area_rows(resolution["polygon"])
    register = household_register_for(
        resolution.get("country_code") or "", area_postcodes(buildings, addresses)
    )
    premises, stats = assemble_premises(
        buildings, addresses, join,
        country=resolution.get("country") or "",
        city=resolution.get("city") or "",
        register=register,
    )

    # The register decided (or did not decide) part of the household total.  It
    # goes in the warnings, not only in a stats field: this is the number the
    # cable sizing and the BOQ are computed from, and a total that moved has to
    # say so on the page a planner is reading.
    register_warning = household_register_warning(stats.get("household_register"))
    if register_warning:
        warnings.append(register_warning)
    if max_premises is not None and len(premises) > max_premises:
        breakdown = sub_area_breakdown(premises)
        raise TooManyPremises(
            len(premises), max_premises,
            narrowing_hint(
                breakdown,
                kinds=resolvable_kinds(resolution, breakdown),
                exclude=own_area_values(resolution, postcode),
            ),
        )

    # Which rung of the boundary ladder was used is reported, never implied: a
    # rectangle standing in for a borough is the same class of error as a
    # guessed country, and the planner is the one who can judge it.
    boundary_source = resolution.get("polygon_source")
    boundary_kind = str(resolution.get("boundary_kind") or "")
    postcode_input = str(resolution.get("input_type") or "") == "postcode"
    if boundary_source == "dataset" and postcode_input and boundary_kind and boundary_kind != "postcode":
        # The postcode selected the place and a loaded dataset supplied the area.
        # Said plainly, with the size: the design area is a ward, not a postcode,
        # and the planner is the one who can judge whether that is what they meant.
        measured = resolution.get("boundary_dataset_km2") or resolution.get("area_km2")
        warnings.append(
            f"A postcode is not a boundary in OpenStreetMap, so the {boundary_kind} "
            f"containing it was used as the design area instead: "
            f"{resolution.get('boundary_name') or 'an unnamed area'}"
            f"{(' (' + str(resolution.get('boundary_code')) + ')') if resolution.get('boundary_code') else ''}"
            f", {measured} km², from {resolution.get('boundary_source') or 'a loaded dataset'}. "
            f"The postcode chose the place; the design area is that {boundary_kind}."
        )
    elif boundary_source == "administrative":
        warnings.append(
            f"This area has no boundary of its own in OpenStreetMap (a US ZIP or a "
            f"UK postcode is only address points), so the enclosing administrative "
            f"boundary was used instead: "
            f"{resolution.get('boundary_name') or 'an unnamed administrative area'}"
            f"{(' (' + str(resolution.get('boundary_admin_level')) + ')') if resolution.get('boundary_admin_level') else ''}."
            " The design area is that administrative area, not the postcode."
        )
    elif resolution.get("postcode_is_boundary") is False:
        # A postcode only chose what to search for; the AREA came from whatever
        # was found around it.  Said plainly and with the size, because one of
        # these outcomes is a single building: "B1, Birmingham" matches an office
        # block, and a design on it is a design for that office block.  The rung
        # cannot say this on its own — a building footprint is as real a polygon
        # as a postcode boundary is.
        kind = "/".join(
            p for p in (resolution.get("matched_category"), resolution.get("matched_type")) if p
        )
        warnings.append(
            "This postcode did not resolve to a postcode boundary in "
            "OpenStreetMap. The match was "
            f"{resolution.get('matched') or 'an unnamed object'}"
            f"{(' (' + kind + ')') if kind else ''}, and the design area is that "
            f"object — {resolution.get('area_km2')} km² — not the postcode. "
            "Check that this is the area you meant."
        )
    elif boundary_source == "dataset":
        note = str(resolution.get("boundary_note") or "").strip()
        vintage = resolution.get("boundary_vintage")
        warnings.append(
            "The boundary came from the loaded authoritative dataset "
            f"{resolution.get('boundary_source') or ''}: "
            f"{resolution.get('boundary_name') or 'an unnamed area'}"
            f"{(' (' + boundary_kind + ')') if boundary_kind else ''}"
            f"{(' (' + str(vintage) + ')') if vintage else ''}"
            f" (code {resolution.get('boundary_code') or '?'}), not from OpenStreetMap."
            # A dataset's own caveat about what its polygons are, e.g. "a ZCTA is
            # an approximation of a ZIP, not the delivery route".
            + (f" {note}" if note else "")
        )
    elif boundary_source != "nominatim":
        warnings.append(
            "No polygon could be found for this area, so the bounding box was used "
            "instead — it is a rectangle and may include neighbouring areas."
        )
    # What the workbook actually carries, counted from its ROWS rather than
    # from the ADDR_ID suffix.  A `-C` suffix means the point is the building's
    # centroid, and that is true both for a building with `addr:*` of its own
    # and for one with no address at all.  Measured on a US ZIP, the old count
    # said "5,946 premises have no addr:housenumber" when 46 % of them carried
    # one -- a warning that overstates the gap is as misleading as one that
    # hides it.  The planner needs to know how many designs will carry no
    # address versus how many took one from the building polygon.
    centroid_only = sum(1 for p in premises if str(p["ADDR_ID"]).endswith("-C"))
    anonymous = sum(
        1 for p in premises
        if not str(p.get("Housenumber") or "").strip()
        and not str(p.get("Address") or "").strip()
    )
    if anonymous:
        warnings.append(
            f"{anonymous} premise(s) have neither a street nor a house number in "
            "OpenStreetMap; they are the building's centroid and the design will "
            "carry no address for them."
        )
    if centroid_only and centroid_only > anonymous:
        warnings.append(
            f"{centroid_only - anonymous} premise(s) took their address from the "
            "building polygon rather than from a separate address point."
        )
    if stats["duplicates_merged"]:
        warnings.append(f"{stats['duplicates_merged']} duplicate address(es) merged.")
    if not premises:
        warnings.append(
            "No premises found in this area. Check that the area name resolved to the "
            "right place and that the boundary is not a building-free box."
        )
    hh = household_summary(premises)
    if hh["total"] and hh["estimated_share"] >= 0.5:
        warnings.append(
            f"{int(round(hh['estimated_share'] * 100))} % of households are estimated "
            "(no building:flats / addr:flats tag on those buildings)."
        )
    # Scale is stated against the only reference the platform has: the
    # UI-Brownfield-Verify corridor, a Mariendorf design of 285 premises.  An
    # area 30x that is a different undertaking, and the preview must say so --
    # and offer a narrower area taken from the data, not a vague suggestion.
    sub_areas = sub_area_breakdown(premises)
    # Which buckets are worth OFFERING is a property of the boundary we are
    # standing on, not of the premises inside it -- see sub_area_kinds_that_narrow
    # and `resolvable_kinds`.  The breakdown itself is still reported: it is a
    # fact about the area.
    chip_kinds = resolvable_kinds(resolution, sub_areas)
    hint = narrowing_hint(
        sub_areas, kinds=chip_kinds,
        exclude=own_area_values(resolution, postcode),
    )
    over_run_cap = len(premises) > MAX_PREMISES
    if over_run_cap:
        warnings.append(
            f"This area yields {len(premises)} premises, about "
            f"{len(premises) // 285}x the largest design the pipeline has run "
            f"(285 premises), and above the {MAX_PREMISES}-premise per-run cap — "
            f"no run can start until it is narrowed. Design a narrower area "
            f"instead —{hint or narrowing_fallback(chip_kinds)}."
        )
    elif len(premises) >= SCALE_GUIDE_PREMISES:
        warnings.append(
            f"This area yields {len(premises)} premises, about "
            f"{len(premises) // 285}x the largest design the pipeline has run "
            f"(285 premises). Design a narrower area instead —"
            f"{hint or narrowing_fallback(chip_kinds)}."
        )

    # Whether a RUN can start is a different question from whether the area can
    # be reviewed, and the page needs the first answer to enable its button
    # honestly rather than letting it start a run the engine will refuse.
    # Mirrors the three refusals in build_inputs().
    if len(premises) < MIN_PREMISES:
        blocked_reason = (
            "No premises were found inside this boundary — the area may have "
            "resolved to a building-free area, or the boundary is wrong."
        )
    elif over_run_cap:
        blocked_reason = (
            f"{len(premises)} premises is above the {MAX_PREMISES}-premise per-run "
            f"cap. Narrow the area first —{hint or narrowing_fallback(chip_kinds)}."
        )
    elif not roads:
        blocked_reason = (
            "No roads were found inside this boundary, so there is nothing for "
            "the design to route along."
        )
        warnings.append(blocked_reason)
    else:
        blocked_reason = None

    return {
        **base,
        "extract": extract,
        "household_register": _register_payload(stats.get("household_register"), register),
        "premises": {
            "count": len(premises),
            "from_address_node": sum(1 for p in premises if not str(p["ADDR_ID"]).endswith("-C")),
            "from_building_centroid": centroid_only,
            # Of the centroid premises, how many carry NO address at all, as
            # opposed to one taken from the building polygon.  The two are
            # different designs: the first puts an unnamed point on the map,
            # the second has a street and a number from OSM.
            "without_address": anonymous,
            "from_building_polygon": max(0, centroid_only - anonymous),
            "duplicates_merged": stats["duplicates_merged"],
            "buildings_excluded": stats["buildings_excluded"],
            "sample": premises[:20],
        },
        "households": hh,
        "sub_areas": sub_areas,
        # Which of the buckets above the resolver can actually reach.  The page
        # offers chips from these only, so a chip is never a round trip to the
        # area it was clicked from.
        "sub_areas_resolvable": chip_kinds,
        "roads": road_summary(roads),
        "warnings": warnings,
        # The review can always be shown; these say whether a design can start.
        "run_cap": MAX_PREMISES,
        "over_run_cap": over_run_cap,
        "can_run": blocked_reason is None,
        "run_blocked_reason": blocked_reason,
    }


def osm_status() -> Dict[str, Any]:
    """What the local OSM store holds -- the honest gate the API reports on."""
    status: Dict[str, Any] = {
        "schema": OSM_SCHEMA,
        "loaded": False,
        "postgis": postgis.is_available(),
        "tables": {},
        "extract": None,
        # Reported here because the register is an OFF-BY-DEFAULT operator step:
        # an empty one is a deliberate configuration, not a broken install, and
        # `enabled: false` with rows loaded is exactly the combination that
        # otherwise looks like the feature is broken.
        "household_register": household_register.register_status("GB"),
    }
    if not schema_ready():
        return status
    for table in ("buildings", "address_nodes", "roads", "landuse"):
        try:
            rows = _query(f"SELECT count(*) AS n FROM {OSM_SCHEMA}.{table}")
            status["tables"][table] = int(rows[0]["n"])
        except Exception:
            status["tables"][table] = 0
    status["loaded"] = bool(status["tables"].get("buildings") or status["tables"].get("address_nodes"))
    try:
        rows = _query(
            f"SELECT source, detail, fetched_at FROM {OSM_SCHEMA}.extract_meta "
            f"ORDER BY fetched_at DESC LIMIT 1"
        )
        if rows:
            fetched = rows[0]["fetched_at"]
            status["extract"] = {
                "source": rows[0]["source"],
                "detail": rows[0]["detail"],
                "fetched_at": fetched.isoformat(timespec="seconds")
                if hasattr(fetched, "isoformat") else str(fetched),
                "stale_days": (_now() - fetched).days if hasattr(fetched, "isoformat") else None,
            }
    except Exception:
        pass
    return status


# ---------------------------------------------------------------------------
# Writing the pipeline's two input files
# ---------------------------------------------------------------------------

# Header spellings are the aliases in HLDPlanning/utils/sheet_utils.py
# EXPECTED_MAP, which is what object_layer autodetects on.  Renaming any of
# these breaks the read silently, so a test asserts they are still present there.
EXCEL_HEADERS = (
    "ADDR_ID", "Address", "Housenumber", "City", "Postcode", "Country",
    "District", "HH", "HH_METHOD", "LATITUDE", "LONGITUDE", "OSM_ID",
)

ROADS_PROPERTIES = ("fclass", "highway", "name", "ref", "oneway", "bridge",
                    "tunnel", "access", "surface", "maxspeed", "lanes", "osm_id",
                    "carrier_source")


def write_address_workbook(path: str, premises: Sequence[Dict[str, Any]]) -> str:
    """Write the premise workbook the pipeline reads.  openpyxl only -- no pandas."""
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - environment problem
        raise RuntimeError(
            "openpyxl is required to write the address workbook (pip install openpyxl)"
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Addresses"
    ws.append(list(EXCEL_HEADERS))
    for p in premises:
        ws.append([p.get(h) for h in EXCEL_HEADERS])
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb.save(path)
    return path


def write_roads_geojson(path: str, roads: Sequence[Dict[str, Any]]) -> str:
    features: List[Dict[str, Any]] = []
    for r in roads:
        geom = r.get("geom_json")
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except (TypeError, ValueError):
                continue
        if not geom:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {k: r.get(k) for k in ROADS_PROPERTIES if r.get(k) is not None},
        })
    payload = {"type": "FeatureCollection", "features": features}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


# ---------------------------------------------------------------------------
# The pavement (sidewalk) carrier network
# ---------------------------------------------------------------------------
# A duct trench runs in the PAVEMENT, not down the carriageway, and the designer
# is built that way round: with `Params.sidewalk_only` on -- the default -- it
# routes ONLY on footway/sidewalk classes and keeps the carriageway classes back
# in a separate vehicular layer that exists for HDD crossing detection. Its own
# network stage places the splitter cabinets on a sidewalk line 3 m off the road
# centreline (`trench_layer.SIDEWALK_OFFSET_M`), so the pavement beside every
# street is already part of the engine's world model.
#
# The OSM extract has to carry that pavement, and in the UK it largely does not.
# Measured on North Edgbaston (3.54 km^2, 71.8 km of roads): OSM maps 184
# footway/path/cycleway ways, about 5.9 km of them. Handed that as the routing
# graph, the design fell into 29 pieces, 44 of its 45 splitters could not reach
# the MFG, the real street routing was dropped (42 runs / 4 834 m) and 280
# straight `pdp-spur` features replaced it -- chords up to 998 m, drawn across
# whatever lay between two points. That is what "the trenches are not on the
# roads" was.
#
# So the roads layer this build writes carries the pavement network itself: every
# carrier-class street is offset to BOTH kerbs, the derived lines are welded to
# each other and tied into the mapped footways, and the result is written as
# ordinary `footway` features. Nothing about the design changes: an arterial is
# still a carriageway crossing, so crossing one is still a drill.
#
# WHERE the band sits: half the carriageway plus a footway inset — per class,
# not one flat number. A flat 3.0 m was measured wrong on the ground (North
# Edgbaston): for a 6.5 m residential carriageway 3.0 m is still ON the road,
# hard against the edge line, so the published trench was "not really on the
# sidewalk". The same rule runs in the designer (trench_design.kerb_offset_for)
# and in the network stage's cabinet band (DEFAULT_SIDEWALK), and tests pin the
# three together — a trench, its pavement and its splitter cabinet sit on one
# band.
PAVEMENT_FOOTWAY_INSET_M = 1.25
# Carriageway width by class for the band rule. MIRRORED from the designer's
# VEHICULAR_WIDTH_M (HLDPlanning/design/trench_design.py) — the engine backend
# cannot import plugin code, so the table is duplicated here and the two are
# pinned together in tests/test_trench_basis.py.
PAVEMENT_WIDTH_M: Dict[str, float] = {
    "motorway": 14.0, "motorway_link": 6.0,
    "trunk": 12.0, "trunk_link": 6.0,
    "primary": 11.0, "primary_link": 5.0,
    "secondary": 9.0, "secondary_link": 4.0,
    "tertiary": 7.5, "tertiary_link": 4.0,
    "residential": 6.5, "unclassified": 6.0,
    "service": 5.0, "living_street": 5.5, "track": 4.0,
}


def pavement_offset_for(fclass: Optional[str]) -> float:
    """Centreline -> pavement band for a street class (metres).

    Half the carriageway plus the footway inset, so the trench lands in the
    footway of a narrow street AND of a wide one. Unknown classes get the
    default 6 m street width.
    """
    width = PAVEMENT_WIDTH_M.get(str(fclass or "").strip().lower(), 6.0)
    return width / 2.0 + PAVEMENT_FOOTWAY_INSET_M


# The base (residential) band. `pavement_offset_for()` is the rule; this stays
# for the flat-override call sites and the documentation.
PAVEMENT_OFFSET_M = pavement_offset_for("residential")
# The carrier classes to derive a pavement beside -- exactly the classes the
# designer itself treats as routable when `sidewalk_only` is off. Arterials
# (primary/secondary/trunk/tertiary) are deliberately left alone: a derived
# pavement there would be the cheapest carrier in the class ladder and would let
# the router run a trench down the A456, which the class factors exist to prevent.
PAVEMENT_CLASSES = ("residential", "unclassified", "living_street", "service",
                    "track")
# The classes that ARE the pavement where OSM maps one; kept as they arrive.
PAVEMENT_MAPPED_CLASSES = ("footway", "path", "pedestrian", "cycleway", "steps",
                           "bridleway", "sidewalk")
PAVEMENT_WELD_M = 0.75       # two vertices this close are one pavement node
PAVEMENT_LINK_M = 3.0        # a loose end is tied to the nearest line within this
PAVEMENT_EXTEND_M = 4.2      # end extension that closes a junction corner
PAVEMENT_SOURCE = "derived-pavement"
# Pavement ends that face each other across one of these are joined by a
# crossing. The arterials are the classes deliberately left WITHOUT a derived
# pavement (see PAVEMENT_CLASSES), so they are exactly what cuts the carrier
# network into pieces: on North Edgbaston the pavement on one side of City Road,
# Sandon Road, Icknield Port Road or Hagley Road sat 5-25 m from the pavement on
# the other side, with no mapped crossing between them. A link is only made over
# a carriageway, and the designer still classifies the crossing it spans as a
# drill, so this restores connectivity without putting a trench along the road.
PAVEMENT_CROSS_M = 35.0
PAVEMENT_CROSSING_CLASSES = ("primary", "primary_link", "secondary",
                             "secondary_link", "tertiary", "tertiary_link",
                             "trunk", "trunk_link")
# Off switch, so an operator can compare an area with and without the derived
# pavement without editing code (same convention as the other OSM_* knobs).
PAVEMENT_ENABLED = os.environ.get("OSM_PAVEMENT_CARRIERS", "1").strip().lower() \
    not in ("0", "false", "no", "off")
# Pavement beside the ARTERIALS as well, not just the carrier streets.
#
# Default OFF: the class ladder exists so a trench does not run down an arterial
# (permits, traffic management), and a derived pavement there is a footway-class
# carrier with the cheapest weight in that ladder, so the router would use it.
# ON, the carrier network mirrors every street and nothing is stranded: measured
# on North Edgbaston, 45 of 45 splitters reach the MFG with it on, against 34 of
# 45 with it off (the other 11 sit on estates whose only connection to the rest
# of the network is across an arterial).  Named rather than hidden, so that
# trade is a decision and not a default.
PAVEMENT_ARTERIALS = os.environ.get("OSM_PAVEMENT_ARTERIALS", "0").strip().lower() \
    not in ("0", "false", "no", "off")
ARTERIAL_CLASSES = ("primary", "primary_link", "secondary", "secondary_link",
                    "tertiary", "tertiary_link", "trunk", "trunk_link")


def pavement_classes() -> Tuple[str, ...]:
    """The street classes a pavement is derived beside (see PAVEMENT_CLASSES)."""
    if PAVEMENT_ARTERIALS:
        return PAVEMENT_CLASSES + ARTERIAL_CLASSES
    return PAVEMENT_CLASSES


def _polyline_parts(row: Dict[str, Any]) -> List[List[List[float]]]:
    """The line parts of a road row's geometry (single, or a MultiLineString)."""
    geom = row.get("geom_json")
    if isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except (TypeError, ValueError):
            return []
    if not isinstance(geom, dict):
        return []
    if geom.get("type") == "LineString":
        coords = geom.get("coordinates") or []
        return [coords] if len(coords) >= 2 else []
    if geom.get("type") == "MultiLineString":
        return [c for c in (geom.get("coordinates") or []) if len(c) >= 2]
    return []


def _offset_polyline(pts: Sequence[Sequence[float]], dist: float,
                     miter_limit: float = 2.0) -> List[Tuple[float, float]]:
    """A polyline offset to its LEFT by ``dist`` metres (negative = right).

    Miter joins with a limit, which is what the engine's own sidewalk offset
    uses (`native:offsetline`, JOIN_STYLE=1, MITER_LIMIT=2.0), so a derived
    pavement turns a corner the way the engine expects one to. A miter longer
    than the limit is clamped rather than spiked.
    """
    clean: List[Tuple[float, float]] = [(float(pts[0][0]), float(pts[0][1]))]
    for p in pts[1:]:
        q = (float(p[0]), float(p[1]))
        if math.hypot(q[0] - clean[-1][0], q[1] - clean[-1][1]) > 1e-9:
            clean.append(q)
    if len(clean) < 2:
        return clean
    normals: List[Tuple[float, float]] = []
    for a, b in zip(clean, clean[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        normals.append((-dy / length, dx / length))
    out: List[Tuple[float, float]] = []
    last = len(clean) - 1
    for i, p in enumerate(clean):
        if i == 0:
            mx, my = normals[0]
        elif i == last:
            mx, my = normals[-1]
        else:
            nx, ny = normals[i - 1][0] + normals[i][0], normals[i - 1][1] + normals[i][1]
            length = math.hypot(nx, ny)
            if length < 1e-9:            # a doubling back bend: no outer side
                mx, my = normals[i - 1]
            else:
                mx, my = nx / length, ny / length
                cos_half = mx * normals[i - 1][0] + my * normals[i - 1][1]
                scale = min(1.0 / max(cos_half, 1e-6), miter_limit)
                mx, my = mx * scale, my * scale
        out.append((p[0] + mx * dist, p[1] + my * dist))
    return out


def _extend_ends(pts: List[Tuple[float, float]], extra: float
                 ) -> List[Tuple[float, float]]:
    """Push both ends of a line out along its own direction.

    A street offset to the kerb stops level with the junction node, so the two
    pavements meeting at a corner stop 3 m short of each other. Extending by
    more than the offset makes them cross instead, which is the corner.
    """
    if len(pts) < 2 or extra <= 0.0:
        return list(pts)
    out = list(pts)
    a, b = out[0], out[1]
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length > 1e-9:
        out[0] = (a[0] + (a[0] - b[0]) / length * extra,
                  a[1] + (a[1] - b[1]) / length * extra)
    a, b = out[-1], out[-2]
    length = math.hypot(a[0] - b[0], a[1] - b[1])
    if length > 1e-9:
        out[-1] = (a[0] + (a[0] - b[0]) / length * extra,
                   a[1] + (a[1] - b[1]) / length * extra)
    return out


def _union_find(size: int) -> Tuple[List[int], Any, Any]:
    parent = list(range(size))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    return parent, find, union


def _weld_vertices(lines: List[List[Tuple[float, float]]], tol: float
                   ) -> List[List[Tuple[float, float]]]:
    """Merge vertices within ``tol`` into one point, at their mean.

    The designer nodes its graph on shared VERTICES (keys rounded to 0.25 m), so
    two pavement lines that merely cross do not connect. Welding turns every
    near-coincident vertex into exactly the same point, which is what makes the
    derived network one graph instead of dozens of pieces -- the failure the
    29-component run logged.
    """
    flat: List[Tuple[float, float]] = []
    where: List[Tuple[int, int]] = []
    for li, line in enumerate(lines):
        for vi, p in enumerate(line):
            flat.append(p)
            where.append((li, vi))
    if not flat:
        return lines
    parent, find, union = _union_find(len(flat))
    cells: Dict[Tuple[int, int], List[int]] = {}
    for i, p in enumerate(flat):
        cells.setdefault((int(math.floor(p[0] / tol)),
                          int(math.floor(p[1] / tol))), []).append(i)
    for i, p in enumerate(flat):
        cx, cy = int(math.floor(p[0] / tol)), int(math.floor(p[1] / tol))
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in cells.get((gx, gy), ()):
                    if j <= i:
                        continue
                    q = flat[j]
                    if math.hypot(q[0] - p[0], q[1] - p[1]) <= tol:
                        union(i, j)
    members: Dict[int, List[int]] = {}
    for i in range(len(flat)):
        members.setdefault(find(i), []).append(i)
    mean: Dict[int, Tuple[float, float]] = {}
    for root, group in members.items():
        mean[root] = (sum(flat[i][0] for i in group) / len(group),
                      sum(flat[i][1] for i in group) / len(group))
    out = [list(line) for line in lines]
    for i, (li, vi) in enumerate(where):
        out[li][vi] = mean[find(i)]
    return out


def _segment_hit(a: Tuple[float, float], b: Tuple[float, float],
                 c: Tuple[float, float], d: Tuple[float, float]
                 ) -> Optional[Tuple[Tuple[float, float], float, float]]:
    """Where two segments cross: ``(point, t, u)``, or ``None``.

    Endpoint touches are deliberately left to the welding pass; this finds the
    interior crossings that a pavement grid is full of (every street crosses
    another) and that the vertex-only node keying would otherwise ignore.  The
    two parameters are how far along each segment the crossing sits -- they are
    what keeps the inserted vertices in order (see :func:`_split_at_crossings`).
    """
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-12:            # parallel or collinear
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if not (-1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9):
        return None
    return ((a[0] + t * rx, a[1] + t * ry), t, u)


def _split_at_crossings(lines: List[List[Tuple[float, float]]],
                        cell_m: float = 20.0) -> int:
    """Insert a shared vertex wherever two lines cross.

    Without this the network looks connected on a map and is not: the designer
    keys nodes on identical vertices, so two pavements that merely cross are two
    graphs. Returns how many crossings were noded.
    """
    segs: List[Tuple[Tuple[float, float], Tuple[float, float], int, int]] = []
    for li, line in enumerate(lines):
        for si, (a, b) in enumerate(zip(line, line[1:])):
            segs.append((a, b, li, si))
    if not segs:
        return 0
    grid: Dict[Tuple[int, int], List[int]] = {}
    for k, (a, b, _li, _si) in enumerate(segs):
        for gx in range(int(math.floor(min(a[0], b[0]) / cell_m)),
                        int(math.floor(max(a[0], b[0]) / cell_m)) + 1):
            for gy in range(int(math.floor(min(a[1], b[1]) / cell_m)),
                            int(math.floor(max(a[1], b[1]) / cell_m)) + 1):
                grid.setdefault((gx, gy), []).append(k)
    # line -> segment -> [(position along that segment, point)].  The position
    # is what makes the insertions ordered: several crossings can land on ONE
    # segment, and inserting them in any other order folds the line back over
    # itself (measured: a first version inflated the network by 14 km of zig-zag
    # on this ward alone).
    hits: Dict[int, Dict[int, Dict[float, Tuple[float, float]]]] = {}
    seen: set = set()
    noded = 0
    for members in grid.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                k1, k2 = members[i], members[j]
                pair = (k1, k2) if k1 < k2 else (k2, k1)
                if pair in seen:
                    continue
                seen.add(pair)
                a, b, l1, s1 = segs[k1]
                c, d, l2, s2 = segs[k2]
                if l1 == l2:
                    continue
                hit = _segment_hit(a, b, c, d)
                if hit is None:
                    continue
                point, t, u = hit
                hits.setdefault(l1, {}).setdefault(s1, {})[round(t, 9)] = point
                hits.setdefault(l2, {}).setdefault(s2, {})[round(u, 9)] = point
                noded += 1
    for li, per_segment in hits.items():
        for si in sorted(per_segment, reverse=True):      # earlier indices stay valid
            for offset, key in enumerate(sorted(per_segment[si])):
                lines[li].insert(si + 1 + offset, per_segment[si][key])
    return noded


def _loose_ends(lines: List[List[Tuple[float, float]]]
                ) -> List[Tuple[int, int]]:
    """Line endpoints that no other line shares -- the pavement's dead ends."""
    count: Dict[Tuple[int, int], int] = {}
    for line in lines:
        for p in (line[0], line[-1]):
            k = (int(round(p[0] / 0.25)), int(round(p[1] / 0.25)))
            count[k] = count.get(k, 0) + 1
    ends: List[Tuple[int, int]] = []
    for li, line in enumerate(lines):
        if len(line) < 2:
            continue
        for vi in (0, len(line) - 1):
            p = line[vi]
            k = (int(round(p[0] / 0.25)), int(round(p[1] / 0.25)))
            if count.get(k, 0) <= 1:
                ends.append((li, vi))
    return ends


def _piece_index(lines: List[List[Tuple[float, float]]]) -> List[int]:
    """Which connected piece each line belongs to, under the designer's keying."""
    parent, find, union = _union_find(len(lines))
    cells: Dict[Tuple[int, int], int] = {}
    for li, line in enumerate(lines):
        for p in line:
            k = (int(round(p[0] / 0.25)), int(round(p[1] / 0.25)))
            other = cells.setdefault(k, li)
            if other != li:
                union(other, li)
    return [find(i) for i in range(len(lines))]


def _tie_across_carriageways(lines: List[List[Tuple[float, float]]],
                             carriageways: List[List[Tuple[float, float]]],
                             max_m: float = PAVEMENT_CROSS_M,
                             cell_m: float = 25.0) -> int:
    """Tie a pavement end onto the pavement on the far side of a carriageway.

    This is the crossing: a duct (and a pedestrian) gets to the other side by
    crossing the road, and the designer already classifies a carriageway crossing
    as an HDD drill -- so the network only has to carry the crossing as a line.
    The end is moved onto the far pavement, so the crossing is part of the
    pavement itself rather than a separate link to account for.

    Only ends of DIFFERENT pieces are tied, shortest first and each end once, so
    this can never invent a shortcut inside a pavement that is already connected.
    Returns how many crossings were made.
    """
    if not lines or not carriageways:
        return 0
    ends = _loose_ends(lines)
    if not ends:
        return 0
    car_segs = [(a, b) for line in carriageways for a, b in zip(line, line[1:])]
    if not car_segs:
        return 0
    car_grid: Dict[Tuple[int, int], List[int]] = {}
    for k, (a, b) in enumerate(car_segs):
        for gx in range(int(math.floor(min(a[0], b[0]) / cell_m)),
                        int(math.floor(max(a[0], b[0]) / cell_m)) + 1):
            for gy in range(int(math.floor(min(a[1], b[1]) / cell_m)),
                            int(math.floor(max(a[1], b[1]) / cell_m)) + 1):
                car_grid.setdefault((gx, gy), []).append(k)

    def crosses_a_carriageway(a: Tuple[float, float],
                              b: Tuple[float, float]) -> bool:
        for gx in range(int(math.floor(min(a[0], b[0]) / cell_m)),
                        int(math.floor(max(a[0], b[0]) / cell_m)) + 1):
            for gy in range(int(math.floor(min(a[1], b[1]) / cell_m)),
                            int(math.floor(max(a[1], b[1]) / cell_m)) + 1):
                for k in car_grid.get((gx, gy), ()):
                    if _segment_hit(a, b, car_segs[k][0], car_segs[k][1]) is not None:
                        return True
        return False

    # Every pavement segment, so an end can reach the far side wherever it is --
    # not only where another pavement happens to end facing it.
    segs: List[Tuple[Tuple[float, float], Tuple[float, float], int, int]] = []
    for li, line in enumerate(lines):
        for si, (a, b) in enumerate(zip(line, line[1:])):
            segs.append((a, b, li, si))
    grid: Dict[Tuple[int, int], List[int]] = {}
    for k, (a, b, _li, _si) in enumerate(segs):
        for gx in range(int(math.floor(min(a[0], b[0]) / cell_m)),
                        int(math.floor(max(a[0], b[0]) / cell_m)) + 1):
            for gy in range(int(math.floor(min(a[1], b[1]) / cell_m)),
                            int(math.floor(max(a[1], b[1]) / cell_m)) + 1):
                grid.setdefault((gx, gy), []).append(k)

    piece = _piece_index(lines)
    candidates: List[Tuple[float, int, int, int, int, Tuple[float, float]]] = []
    radius = int(math.ceil(max_m / cell_m)) + 1
    for li, vi in ends:
        p = lines[li][vi]
        gx, gy = int(math.floor(p[0] / cell_m)), int(math.floor(p[1] / cell_m))
        for i in range(gx - radius, gx + radius + 1):
            for j in range(gy - radius, gy + radius + 1):
                for k in grid.get((i, j), ()):
                    a, b, lj, si = segs[k]
                    if lj == li or piece[lj] == piece[li]:
                        continue
                    vx, vy = b[0] - a[0], b[1] - a[1]
                    L2 = vx * vx + vy * vy
                    t = 0.0 if L2 == 0.0 else min(1.0, max(
                        0.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2))
                    q = (a[0] + t * vx, a[1] + t * vy)
                    d = math.hypot(q[0] - p[0], q[1] - p[1])
                    if d <= max_m and d > 1e-6:
                        candidates.append((round(d, 3), li, vi, lj, si, q))
    candidates.sort()
    used: set = set()
    accepted: Dict[int, List[Tuple[int, Tuple[float, float]]]] = {}
    made = 0
    for d, li, vi, lj, si, q in candidates:
        if (li, vi) in used or piece[li] == piece[lj] or d > max_m:
            continue
        if not crosses_a_carriageway(lines[li][vi], q):
            continue
        accepted.setdefault(lj, []).append((si, q))
        lines[li][vi] = q
        used.add((li, vi))
        piece[li] = piece[lj]
        made += 1
    # Descending, so inserting one crossing does not shift the index of the next.
    for lj, items in accepted.items():
        for si, q in sorted(set(items), reverse=True):
            lines[lj].insert(si + 1, q)
    return made


def _tie_loose_ends(lines: List[List[Tuple[float, float]]], tol: float) -> int:
    """Tie every loose end onto the nearest other line within ``tol``.

    A mapped footway ends at the kerb, a derived pavement runs past it; a
    derived pavement stops at a junction, a mapped one may cross it. Either way
    the connection only exists once both share a VERTEX, so the end is projected
    onto the nearest line and that point is inserted into it. Returns how many
    ends were tied.
    """
    segs: List[Tuple[Tuple[float, float], Tuple[float, float], int, int]] = []
    for li, line in enumerate(lines):
        for si, (a, b) in enumerate(zip(line, line[1:])):
            segs.append((a, b, li, si))
    if not segs:
        return 0
    cell = max(tol, 1.0)
    grid: Dict[Tuple[int, int], List[int]] = {}
    for k, (a, b, _li, _si) in enumerate(segs):
        for gx in range(int(math.floor((min(a[0], b[0]) - tol) / cell)),
                        int(math.floor((max(a[0], b[0]) + tol) / cell)) + 1):
            for gy in range(int(math.floor((min(a[1], b[1]) - tol) / cell)),
                            int(math.floor((max(a[1], b[1]) + tol) / cell)) + 1):
                grid.setdefault((gx, gy), []).append(k)
    inserts: Dict[int, List[Tuple[int, Tuple[float, float]]]] = {}
    tied = 0
    for li, line in enumerate(lines):
        for vi in (0, len(line) - 1):
            p = line[vi]
            gx, gy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
            best: Optional[Tuple[float, int, int, Tuple[float, float]]] = None
            for i in (gx - 1, gx, gx + 1):
                for j in (gy - 1, gy, gy + 1):
                    for k in grid.get((i, j), ()):
                        a, b, lj, si = segs[k]
                        if lj == li:
                            continue
                        vx, vy = b[0] - a[0], b[1] - a[1]
                        L2 = vx * vx + vy * vy
                        t = 0.0 if L2 == 0.0 else min(1.0, max(
                            0.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2))
                        q = (a[0] + t * vx, a[1] + t * vy)
                        d = math.hypot(q[0] - p[0], q[1] - p[1])
                        if d <= tol and (best is None or d < best[0]):
                            best = (d, lj, si, q)
            if best is None:
                continue
            _d, lj, si, q = best
            inserts.setdefault(lj, []).append((si, q))
            line[vi] = q
            tied += 1
    # Descending, so the local segment index of every later insert stays valid.
    for lj, items in inserts.items():
        for si, q in sorted(set(items), reverse=True):
            lines[lj].insert(si + 1, q)
    return tied


def pavement_carriers(roads: Sequence[Dict[str, Any]],
                      offset_m: Optional[float] = None
                      ) -> Dict[str, Any]:
    """Derive the pavement network beside the carrier streets. Pure + testable.

    The band follows the class width (``pavement_offset_for``) unless
    ``offset_m`` forces one flat distance. Returns ``{"rows", "derived_km",
    "mapped_km", "total_km", "pieces", "tied"}``. Only the DERIVED lines are
    returned as rows -- the mapped footways are already in ``roads`` and stay
    exactly as OSM drew them; they are fed into the welding pass so the derived
    pavement joins onto them.
    """
    derived: List[Tuple[List[List[float]], Dict[str, Any], float]] = []
    mapped: List[Tuple[List[List[float]], Dict[str, Any]]] = []
    for row in roads:
        fclass = str(row.get("fclass") or "")
        if fclass in pavement_classes():
            band = offset_m if offset_m is not None else pavement_offset_for(fclass)
            for coords in _polyline_parts(row):
                if len(coords) >= 2:
                    # BOTH kerbs: the engine places its splitter cabinets on the
                    # sidewalk of either side, so a one-sided network leaves the
                    # far side's cabinets unreachable again.
                    for dist in (band, -band):
                        derived.append((coords, row, dist))
        elif fclass in PAVEMENT_MAPPED_CLASSES:
            mapped.extend((coords, row) for coords in _polyline_parts(row))
    if not derived:
        return {"rows": [], "replaced_osm_ids": [], "derived_km": 0.0,
                "mapped_km": 0.0, "total_km": 0.0, "pieces": 0, "tied": 0,
                "nodings": 0, "crossings": 0, "arterials": False}

    # All geometry in a local metre plane, so an offset is an offset everywhere
    # in the area and not a different distance north to south.
    sample = [c for coords, _row, _d in derived for c in coords][:4000] or \
        [c for coords, _row in mapped for c in coords][:4000]
    lon0 = sum(float(c[0]) for c in sample) / len(sample)
    lat0 = sum(float(c[1]) for c in sample) / len(sample)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110540.0

    def to_xy(c: Sequence[float]) -> Tuple[float, float]:
        return ((float(c[0]) - lon0) * mx, (float(c[1]) - lat0) * my)

    def to_lonlat(p: Tuple[float, float]) -> Tuple[float, float]:
        return (round(p[0] / mx + lon0, 7), round(p[1] / my + lat0, 7))

    lines: List[List[Tuple[float, float]]] = []
    sources: List[Dict[str, Any]] = []
    kinds: List[str] = []
    for coords, row, dist in derived:
        off = _offset_polyline([to_xy(c) for c in coords], dist)
        lines.append(_extend_ends(off, PAVEMENT_EXTEND_M))
        sources.append(row)
        kinds.append("derived")
    for coords, row in mapped:
        lines.append([to_xy(c) for c in coords])
        sources.append(row)
        kinds.append("mapped")

    lines = _weld_vertices(lines, PAVEMENT_WELD_M)
    crossed = _split_at_crossings(lines)
    tied = _tie_loose_ends(lines, PAVEMENT_LINK_M)
    lines = _weld_vertices(lines, PAVEMENT_WELD_M)
    # The welding above moves vertices, so a second pass nodes the crossings it
    # created; a line is never connected by a crossing that is not a vertex.
    crossed += _split_at_crossings(lines)

    # The arterial crossings, then one more node pass so the moved ends are
    # welded onto the pavements they reach.
    carriageways = [[to_xy(c) for c in coords]
                    for row in roads
                    if str(row.get("fclass") or "") in PAVEMENT_CROSSING_CLASSES
                    for coords in _polyline_parts(row)]
    crossings = _tie_across_carriageways(lines, carriageways)
    lines = _weld_vertices(lines, PAVEMENT_WELD_M)
    crossed += _split_at_crossings(lines)

    # Both halves go back to the caller: the derived pavements as new `footway`
    # features, and the mapped footways REBUILT, because the node/tying passes
    # inserted vertices into them.  Emitting only the derived lines would hand
    # the designer a pavement that touches a mapped footway at a point which is
    # not a vertex of it -- which is not a connection to a graph keyed on
    # vertices, and was measured as reaching 1 splitter of 45.
    rows: List[Dict[str, Any]] = []
    replaced: List[Any] = []
    derived_m = 0.0
    mapped_m = 0.0
    for i, line in enumerate(lines):
        pts = _thin_vertices(line)
        if len(pts) < 2:
            continue
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip(pts, pts[1:]))
        if length < 0.5:
            continue
        geom_json = json.dumps({"type": "LineString",
                                "coordinates": [list(to_lonlat(p)) for p in pts]})
        row = sources[i]
        if kinds[i] == "derived":
            derived_m += length
            # `osm_id` must stay an INTEGER: the roads GeoJSON is read by
            # QGIS as a vector layer and a mixed int/string column fails the
            # whole Trench stage ("Error converting value (8098827) for field
            # osm_id") — the first feature fixes the field type, the other
            # type then fails.  Derived pavements are not OSM objects, so use
            # a synthetic negative id outside the OSM range (unique per line).
            synthetic_id = -(1_000_000_000 + i)
            rows.append({
                "osm_id": synthetic_id,
                "fclass": "footway",
                "highway": "footway",
                "name": row.get("name"),
                "surface": row.get("surface"),
                "carrier_source": PAVEMENT_SOURCE,
                "geom_json": geom_json,
            })
        else:
            mapped_m += length
            rebuilt = dict(row)
            rebuilt["geom_json"] = geom_json
            rebuilt["carrier_source"] = "osm-footway"
            rows.append(rebuilt)
            if row.get("osm_id") is not None:
                replaced.append(row.get("osm_id"))
    total_m = derived_m + mapped_m
    return {
        "rows": rows,
        "replaced_osm_ids": replaced,
        "derived_km": round(derived_m / 1000.0, 1),
        "mapped_km": round(mapped_m / 1000.0, 1),
        "total_km": round(total_m / 1000.0, 1),
        "pieces": _pieces(lines),
        "tied": tied,
        "nodings": crossed,
        "crossings": crossings,
        "arterials": bool(PAVEMENT_ARTERIALS),
    }


def _thin_vertices(pts: List[Tuple[float, float]], min_gap_m: float = 1.0
                   ) -> List[Tuple[float, float]]:
    """Drop the duplicate vertices the welding pass leaves behind."""
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    for q in pts[1:-1]:
        if math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) >= min_gap_m:
            out.append(q)
    if math.hypot(pts[-1][0] - out[-1][0], pts[-1][1] - out[-1][1]) > 1e-9:
        out.append(pts[-1])
    return out


def _pieces(lines: Sequence[Sequence[Tuple[float, float]]],
            key_m: float = 0.25) -> int:
    """How many pieces the line set falls into under the designer's own keying.

    The trench designer nodes on shared vertices rounded to 0.25 m, so this is
    the number it will see. Reported with the build so a regression that returns
    the network to dozens of pieces is visible without running a design.
    """
    parent, find, union = _union_find(len(lines) if lines else 0)
    cells: Dict[Tuple[int, int], int] = {}
    for li, line in enumerate(lines):
        for p in line:
            k = (int(round(p[0] / key_m)), int(round(p[1] / key_m)))
            other = cells.setdefault(k, li)
            if other != li:
                union(other, li)
    if not lines:
        return 0
    return len({find(i) for i in range(len(lines))})


def build_inputs(
    project_id: str,
    area: str,
    output_dir: str,
    country_code: str = "",
    input_type: str = "",
    postcode: str = "",
) -> Dict[str, Any]:
    """Write the two pipeline input files for an area.  Returns their paths + provenance."""
    resolution = resolve_area(
        area, country_code=country_code, input_type=input_type, postcode=postcode
    )
    bbox = resolution["bbox"]
    if not postgis.is_available():
        raise RuntimeError("postgis_unavailable")
    extract = ensure_area_data(area, bbox)
    buildings, addresses, join, roads = read_area_rows(resolution["polygon"])
    register = household_register_for(
        resolution.get("country_code") or "", area_postcodes(buildings, addresses)
    )
    premises, stats = assemble_premises(
        buildings, addresses, join,
        country=resolution.get("country") or "",
        city=resolution.get("city") or "",
        register=register,
    )
    if len(premises) < MIN_PREMISES:
        raise ValueError("no_premises")
    if not roads:
        raise ValueError("no_roads")
    if len(premises) > MAX_PREMISES:
        # Carry the narrowing hint too: this refusal is the one that stops an
        # actual run, so it is the one that most needs to say where to go next.
        breakdown = sub_area_breakdown(premises)
        raise TooManyPremises(
            len(premises), MAX_PREMISES,
            hint=narrowing_hint(
                breakdown,
                kinds=resolvable_kinds(resolution, breakdown),
                exclude=own_area_values(resolution, postcode),
            ),
        )
    inputs_dir = os.path.join(output_dir, "inputs")
    excel_path = write_address_workbook(os.path.join(inputs_dir, "Main_DataSet.xlsx"), premises)
    # `roads_km` keeps meaning the OSM street network the area resolved to; the
    # derived pavement is reported separately, because it is our geometry, not
    # OSM's, and a planner has to be able to tell the two apart.
    streets_km = road_summary(roads)["total_km"]
    pavement = (pavement_carriers(roads) if PAVEMENT_ENABLED
                else {"rows": [], "replaced_osm_ids": [], "derived_km": 0.0,
                      "mapped_km": 0.0, "total_km": 0.0, "pieces": 0,
                      "tied": 0, "nodings": 0, "crossings": 0,
                      "arterials": False})
    # The mapped footways come back rebuilt (the node and tying passes put
    # vertices on them), so the originals are dropped rather than written twice.
    replaced = {str(v) for v in pavement.get("replaced_osm_ids") or []}
    kept_roads = [r for r in roads if str(r.get("osm_id")) not in replaced]
    roads_path = write_roads_geojson(
        os.path.join(inputs_dir, "roads.geojson"),
        kept_roads + list(pavement["rows"]),
    )
    # The landuse the aerial-zones derivation needs.  Written from the store we
    # already fetched, so an area run produces the same constraint inputs a
    # manual multi-layer upload does; without it the designer falls back to
    # classifying drop legs on length and chain alone and a house in a park gets
    # trenched (see `write_landuse_geojson`).
    landuse_path = write_landuse_geojson(
        os.path.join(inputs_dir, "osm", "landuse", "landuse.geojson"),
        resolution["polygon"],
    )

    hh = household_summary(premises)
    return {
        "excel_path": excel_path,
        "roads_path": roads_path,
        # Not one of the pipeline's two input files: the designer reads this to
        # derive aerial zones, and `landuse_path: null` is the honest signal that
        # it had no land to work with.
        "landuse_path": landuse_path,
        "area": area,
        "input_type": resolution.get("input_type") or input_type or "area",
        "matched": resolution.get("matched"),
        "country_code": resolution.get("country_code") or "",
        "country": resolution.get("country") or "",
        "city": resolution.get("city") or "",
        "polygon_source": resolution.get("polygon_source"),
        # WHICH boundary, and where it came from.  `polygon_source: dataset` alone
        # is not provenance: measured, a ward run recorded that it used a dataset
        # and nothing about which ward, its code, or its licence -- so the audit
        # trail could not say what the design area actually was.
        "boundary_name": resolution.get("boundary_name"),
        "boundary_code": resolution.get("boundary_code"),
        "boundary_kind": resolution.get("boundary_kind"),
        "boundary_source": resolution.get("boundary_source"),
        "boundary_licence": resolution.get("boundary_licence"),
        "boundary_vintage": resolution.get("boundary_vintage"),
        "boundary_note": resolution.get("boundary_note"),
        "area_km2": resolution.get("area_km2"),
        "bbox": resolution.get("bbox"),
        "resolution_key": resolution_key(area, country_code),
        "premises": len(premises),
        "households": hh,
        "household_register": _register_payload(
            stats.get("household_register"), register
        ),
        "roads_km": streets_km,
        # The pavement network the designer actually routes on: what OSM mapped,
        # what had to be derived beside the carrier streets, and how many pieces
        # the result falls into under the designer's own node keying.
        "pavement": {k: v for k, v in pavement.items()
                     if k not in ("rows", "replaced_osm_ids")},
        "extract": extract,
        "stats": stats,
    }
