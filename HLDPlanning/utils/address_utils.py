# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from typing import Any, Dict, Optional, Tuple

import requests
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
try:
    # Retry is vendored in requests via urllib3
    from urllib3.util.retry import Retry  # type: ignore
except Exception:  # pragma: no cover
    Retry = None  # type: ignore[assignment]

__all__ = [
    "_slug",
    "normalize_street",
    "build_structured_query",
    "make_cache_key",
    "NominatimClient",
]


# ---------------------------
# Text normalization helpers
# ---------------------------

def _slug(s: str) -> str:
    """ASCII-ish normalization with whitespace collapsing. Keeps German letters readable."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s).strip())
    # Common German letter replacements (keep it deterministic)
    s = (
        s.replace("ß", "ss")
         .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
         .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_street(street: str) -> str:
    """
    Normalize common German street spellings:
      - 'Str.', 'Strasse' → 'Straße' (others stay transliterated)
    """
    if not street:
        return ""
    s = _slug(street)
    # Undo umlaut replacement for the very common 'Straße' spelling only
    s = (
        s.replace("Strasse", "Straße")
         .replace("Str.", "Straße")
         .replace("Str ", "Straße ")
         .replace(" strasse", " straße")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ------------------------------------
# Structured query + cache key helpers
# ------------------------------------

def build_structured_query(row: Dict[str, Any], mapping: Dict[str, str], defaults: Dict[str, str]) -> Dict[str, str]:
    """
    Build a Nominatim-friendly structured address dict from a table row + column mapping.
    Output keys: street, city, postalcode, country
    Works with dict-like (e.g. pandas.Series).
    """
    def pick(*keys):
        for k in keys:
            col = mapping.get(k, "")
            if col and (col in row):
                v = row.get(col, "")
                if v is not None and str(v).strip() != "" and str(v).lower() != "nan":
                    return str(v).strip()
        return ""

    street = pick("street") or pick("address")
    hnr = pick("house_number")
    if hnr and street and hnr not in street:
        street = f"{street} {hnr}"

    city = pick("city") or defaults.get("city", "")
    postcode = pick("postcode") or defaults.get("postcode", "")
    country = pick("country") or defaults.get("country", "Germany")
    district = pick("district")

    street = normalize_street(street)
    city = _slug(city)
    district = _slug(district)

    city_query = f"{district}, {city}" if district and (district.lower() not in city.lower()) else city
    return {
        "street": street,
        "city": city_query,
        "postalcode": str(postcode).strip(),
        "country": country or "Germany",
    }


def _norm_for_key(x: Optional[str]) -> str:
    return _slug(x or "").lower()


def make_cache_key(structured: Dict[str, str]) -> str:
    """
    Stable cache key that tolerates trivial punctuation/spacing/case differences.
    """
    parts = [
        _norm_for_key(structured.get("street", "")),
        _norm_for_key(structured.get("city", "")),
        _norm_for_key(structured.get("postalcode", "")),
        _norm_for_key(structured.get("country", "")),
    ]
    s = "|".join(parts)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# -----------------
# Nominatim client
# -----------------

class NominatimClient:
    """
    Polite Nominatim client with:
      - JSONL disk cache (in-memory index) to avoid rate limits
      - structured query by default (street/city/postalcode/country)
      - retry with exponential backoff on 429/5xx
      - configurable min_delay >= 1.0s between live calls (policy-friendly)
    API:
      geocode(structured) -> (lon, lat, raw, from_cache)
    """

    def __init__(
        self,
        email: str,
        base_url: str = "https://nominatim.openstreetmap.org/",
        min_delay: float = 1.1,
        cache_dir: Optional[str] = None,
        use_cache: bool = True,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 1.6,
    ):

        # Base URL
        self.base_url = base_url.strip()
        if not self.base_url.endswith("/"):
            self.base_url += "/"

        # HTTP session with retry (if available)
        self.session = requests.Session()
        ua_email = (email or "you@example.com").strip()
        self.session.headers.update({"User-Agent": f"HLDPlanning/1.0 (contact: {ua_email})"})

        if Retry is not None:
            retry_cfg = Retry(
                total=max(0, int(max_retries)),
                read=max(0, int(max_retries)),
                connect=max(0, int(max_retries)),
                backoff_factor=max(1.0, float(backoff_factor)),
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET"]),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry_cfg)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

        # Throttling
        self.min_delay = max(1.0, float(min_delay))
        self._last_call = 0.0

        # Timeouts / limits
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(1.0, float(backoff_factor))
        # Cache control
        self.use_cache = bool(use_cache) and bool(cache_dir)  # no dir => no cache

        # Cache (JSONL)
        self.cache_path = (
            os.path.join(cache_dir, "geocode_cache.jsonl") if self.use_cache else None
        )

        self._mem_cache: Dict[str, Dict[str, Any]] = {}
        if self.use_cache and self.cache_path:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            self._load_cache()


    # ---- cache I/O ----

    def _load_cache(self):
        if not self.use_cache or not self.cache_path or not os.path.exists(self.cache_path):
            return

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        key = rec.get("key")
                        if key:
                            self._mem_cache[key] = rec
                    except Exception:
                        # ignore corrupted lines
                        pass
        except Exception:
            # tolerate cache read errors
            pass

    def _write_cache(self, key: str, rec: Dict[str, Any]):
        self._mem_cache[key] = rec
        if self.cache_path:
            try:
                with open(self.cache_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                # tolerate cache write errors
                pass

    # ---- network ----

    def _throttle(self):
        now = time.time()
        sleep_for = self.min_delay - (now - self._last_call)
        # Avoid one long blocking sleep inside QGIS Processing worker threads.
        # Short chunks keep cancellation and the host event loop more responsive.
        while sleep_for > 0:
            time.sleep(min(0.1, sleep_for))
            sleep_for = self.min_delay - (time.time() - self._last_call)

    def _structured_params(self, structured: Dict[str, str]) -> Dict[str, str]:
        """
        Prefer structured search parameters supported by Nominatim.
        Fallback to 'q' if we barely have any components.
        """
        street = structured.get("street", "") or ""
        city = structured.get("city", "") or ""
        postalcode = structured.get("postalcode", "") or ""
        country = structured.get("country", "") or ""

        has_struct = any([street, city, postalcode, country])
        if has_struct:
            params = {
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": 1,
            }
            if street:     params["street"] = street
            if city:       params["city"] = city
            if postalcode: params["postalcode"] = str(postalcode)
            if country:    params["country"] = country
            # NEW: hint country code if Germany (avoids rare cross-border hits)
            if str(country).lower() in ("germany", "de", "deutschland"):
                params["countrycodes"] = "de"
            return params
        

        # Fallback to single 'q' string (should be rare in our pipeline)
        q_parts = [v for v in [street, city, postalcode, country] if v]
        return {
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 1,
            "q": ", ".join(q_parts),
        }

    def geocode(self, structured: Dict[str, str]) -> Tuple[Optional[float], Optional[float], Dict[str, Any], bool]:
        """
        returns: lon, lat, raw, from_cache
        - Uses cache first.
        - Otherwise performs structured search, with retries on 429/5xx.
        - Always caches the result (including None) to avoid repeated failing calls.
        """
        key = make_cache_key(structured)
        if self.use_cache:
            cached = self._mem_cache.get(key)
            if cached is not None:
                lon = cached.get("lon"); lat = cached.get("lat"); raw = cached.get("raw", {})
                if (lon is not None) and (lat is not None):
                    return lon, lat, raw, True


        # throttle before live call
        self._throttle()

        params = self._structured_params(structured)
        url = urljoin(self.base_url, "search")

        tries = 0
        raw: Dict[str, Any] = {}
        lon = lat = None

        while True:
            tries += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self._last_call = time.time()
                status = resp.status_code

                if status == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, list) and data:
                            first = data[0]
                            raw = first
                            lon = float(first.get("lon")) if first.get("lon") is not None else None
                            lat = float(first.get("lat")) if first.get("lat") is not None else None
                    except Exception:
                        # keep lon/lat as None if parsing fails
                        pass
                    break

                # Retry on 429/5xx (requests' adapter may already handle, but we also loop here)
                if status in (429, 500, 502, 503, 504) and tries <= self.max_retries:
                    self._sleep_backoff(self.backoff_factor ** (tries - 1))
                    continue

                # Non-retryable or retries exhausted
                break

            except requests.RequestException:
                # Network error; retry if allowed
                self._last_call = time.time()
                if tries <= self.max_retries:
                    self._sleep_backoff(self.backoff_factor ** (tries - 1))
                    continue
                break

        rec = {"key": key, "lon": lon, "lat": lat, "raw": raw, "q": structured}
        if self.use_cache:
            self._write_cache(key, rec)
        return lon, lat, raw, False

    def _sleep_backoff(self, seconds: float):
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            chunk = min(0.1, remaining)
            time.sleep(chunk)
            remaining -= chunk
