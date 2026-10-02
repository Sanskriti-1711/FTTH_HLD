# -*- coding: utf-8 -*-
"""Opt-in imagery/AI review for spans uncertain to the geometry checker.

This pass writes a separate JSON review artifact; it never edits
``Final_Trenches.gpkg`` or changes the deterministic geometry verdict. The
built-in IGN provider fetches a small patch from France's public BD ORTHO WMS,
transforms the candidate geometry to WGS84, and draws the planned route over
the patch before inference. IGN covers France only, and answers a point it holds
no imagery for with a blank white patch rather than an error — which reached the
model as an empty image and came back as an abstention. The default provider
therefore falls back to Esri World Imagery (free, keyless, worldwide) when IGN
has nothing for the point; which source answered is recorded per span as
``imagery_source``.

Provider settings:
  SURFACE_AI_PROVIDER=gemini|ollama (default: gemini)
  SURFACE_AI_MODEL=gemini-3.8-flash (Gemini default) or Ollama model override
  SURFACE_AI_OLLAMA_URL=http://127.0.0.1:11434
  SURFACE_AI_IMAGE_PROVIDER=HLDPlanning.design.surface_ai_review:worldwide_surface_imagery
  SURFACE_AI_REVIEW=1 (the review remains off unless explicitly enabled)
  SURFACE_AI_MAX_SPANS=50 (new spans sent to AI per run; 200 maximum)
  SURFACE_AI_MIN_INTERVAL_SECONDS=6 (pause between calls on a free tier)

Gemini images are sent to Google's API when Gemini is selected; Ollama runs on
the configured host, without a per-call API charge. Both paths are
suggestion-only and require human review.

The batch is bounded to 50 spans by default, and free-tier quota is respected:
calls are paced, a quota exhaustion ends the batch instead of erroring through
it, and a rerun resumes the spans still unprocessed (``previous=`` carries the
finished answers forward).
"""
from __future__ import annotations

import base64
import importlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

FAMILIES = ("road", "footway", "garden")
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OLLAMA_MODEL = "qwen2.5vl:3b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CANDIDATES = 50
ABSOLUTE_MAX_CANDIDATES = 200
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 429 is quota and 5xx is a transient demand spike ("this model is currently
# experiencing high demand"); both are worth another try. Any other status is a
# configuration problem that a retry cannot fix.
GEMINI_TRANSIENT_STATUS = frozenset((429, 500, 502, 503, 504))
# A demand spike can outlast a couple of quick retries, so allow four tries
# with a growing gap (~12s of waiting in the worst case).
GEMINI_RETRY_ATTEMPTS = 4
GEMINI_RETRY_BACKOFF_SECONDS = 2.0
# Free-tier Gemini quota is enforced per minute, so a batch must not fire calls
# back to back. Six seconds between calls keeps a run under the ten-requests-
# per-minute free-tier ceiling; SURFACE_AI_MIN_INTERVAL_SECONDS overrides it.
GEMINI_DEFAULT_MIN_INTERVAL_SECONDS = 6.0
IGN_WMS_URL = "https://data.geopf.fr/wms-r/wms?"
IGN_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
IGN_SOURCE = "IGN BD ORTHO (© IGN, Open Licence 2.0)"
# IGN answers a point it holds no imagery for — anywhere outside France, and over
# open water even inside it — with a constant-white JPEG rather than an error.
# Across 13 sampled patches every blank one came back byte-identical at 1527
# bytes while every real one measured 6761-9790 bytes, so a size guard with a
# wide margin on both sides separates them without decoding the image (this
# module deliberately carries no imaging dependency on the host).
IGN_BLANK_PATCH_MAX_BYTES = 4096
# Esri World Imagery covers the whole world from a single keyless endpoint, and is
# already the results map's satellite basemap, so a fallback patch is the same
# imagery a reader is looking at. One export request returns one patch, the same
# shape as the WMS call IGN needs.
ESRI_EXPORT_URL = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
                   "World_Imagery/MapServer/export?")
ESRI_SOURCE = "Esri World Imagery (© Esri, Maxar, Earthstar Geographics)"


def _max_image_dimension() -> int:
    """Resolve the WMS patch size, allowing slow hosts to request smaller tiles."""
    raw = os.environ.get("SURFACE_AI_IMAGE_MAX_DIM", "").strip()
    if not raw:
        return 1600
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1600
    return value if value >= 64 else 1600
WMS_TIMEOUT_SECONDS = 30.0
OLLAMA_TIMEOUT_SECONDS = 240.0


def _ollama_num_predict() -> int:
    """Cap generated tokens; the classification reply is a short JSON object."""
    raw = os.environ.get("SURFACE_AI_OLLAMA_NUM_PREDICT", "").strip()
    if not raw:
        return 128
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 128
    return value if value > 0 else 128


def _ollama_timeout_seconds() -> float:
    """Resolve the Ollama request timeout, allowing slow hosts to override it.

    CPU-only inference can take far longer than the default per request. The
    default is unchanged so normal deployments keep the tight bound.
    """
    raw = os.environ.get("SURFACE_AI_OLLAMA_TIMEOUT", "").strip()
    if not raw:
        return OLLAMA_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return OLLAMA_TIMEOUT_SECONDS
    return value if value > 0 else OLLAMA_TIMEOUT_SECONDS


def _provider_name() -> str:
    value = os.environ.get("SURFACE_AI_PROVIDER", "").strip().lower()
    return value if value in ("gemini", "ollama") else "gemini"


def _model_name(provider: Optional[str] = None) -> str:
    provider = provider or _provider_name()
    default = DEFAULT_OLLAMA_MODEL if provider == "ollama" else DEFAULT_GEMINI_MODEL
    return os.environ.get("SURFACE_AI_MODEL", default).strip() or default


def _report_provider(provider: Optional[str] = None) -> str:
    provider = provider or _provider_name()
    return "google_gemini" if provider == "gemini" else provider


def _empty_report(status: str, reason: Optional[str] = None) -> dict:
    provider = _provider_name()
    return {
        "enabled": status != "disabled",
        "status": status,
        "review_required": True,
        "provider": _report_provider(provider),
        "model": _model_name(provider),
        "imagery_source": None,
        "imagery_date": None,
        "suggestions": [],
        "reason": reason,
    }


_CONFIGURED_IMAGE_PROVIDER = None


class SurfaceAIQuotaExceeded(RuntimeError):
    """Every configured model refused the call for rate/quota reasons.

    Distinct from a generic ``RuntimeError`` so a batch can stop spending quota
    and leave the remaining spans for a later, resuming run instead of walking
    the whole candidate list through a wall of 429s.
    """


class ImageryUnavailable(RuntimeError):
    """An imagery source answered, but holds nothing for this location.

    Kept apart from a transport failure on purpose. Open water and other places
    Esri publishes no high-resolution tiles for come back as an HTTP 500 from the
    export endpoint, and that is a real answer — "nothing here" — which the
    review can report as ``no_imagery``. A timeout or a refused connection is
    not an answer, so it stays an error instead of being silently swallowed.
    """


class SurfaceAIReplyError(ValueError):
    """A model's reply could not be read as a surface classification.

    Carries the model's own ``raw_reply`` so a rejected span can be diagnosed
    from the artifact — "a ValueError happened" says nothing about whether the
    model answered off-vocabulary, wrapped its answer, or refused the image.
    """

    def __init__(self, message: str, raw_reply: str = ""):
        super().__init__(message)
        self.raw_reply = str(raw_reply or "")


# How much of a rejected reply to carry into the artifact. Enough to see the
# family/confidence a model returned, without bloating the review for every
# failed span.
MAX_ERROR_DETAIL = 500


def configure_image_provider(provider: Optional[Callable[[dict], Optional[dict]]]) -> None:
    """Register the deployment's approved imagery source for run-time reviews."""
    if provider is not None and not callable(provider):
        raise TypeError("image provider must be callable")
    global _CONFIGURED_IMAGE_PROVIDER
    _CONFIGURED_IMAGE_PROVIDER = provider


def configured_image_provider():
    """Return a registered provider or load the configured ``module:function``."""
    if _CONFIGURED_IMAGE_PROVIDER is not None:
        return _CONFIGURED_IMAGE_PROVIDER
    target = os.environ.get("SURFACE_AI_IMAGE_PROVIDER", "").strip()
    if not target or ":" not in target:
        return None
    module_name, function_name = target.rsplit(":", 1)
    try:
        provider = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError, ValueError):
        return None
    return provider if callable(provider) else None


def _candidate_limit() -> int:
    try:
        configured = int(os.environ.get("SURFACE_AI_MAX_SPANS", DEFAULT_MAX_CANDIDATES))
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_CANDIDATES
    return max(1, min(configured, ABSOLUTE_MAX_CANDIDATES))


def _candidate_key(candidate: dict) -> str:
    return str(candidate.get("span_id") or "")


def _throttle_interval(provider: Optional[str] = None) -> float:
    """Seconds to leave between vision calls, so a batch stays inside quota.

    ``SURFACE_AI_MIN_INTERVAL_SECONDS`` overrides the per-provider default in
    both directions (0 disables pacing). Gemini defaults to a free-tier safe
    six seconds; Ollama is local and needs no pacing.
    """
    raw = os.environ.get("SURFACE_AI_MIN_INTERVAL_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is not None and value >= 0:
            return value
    provider = provider or _provider_name()
    return GEMINI_DEFAULT_MIN_INTERVAL_SECONDS if provider == "gemini" else 0.0


def _processed_items(previous: Optional[dict]) -> dict:
    """Span id → already-answered review item from a previous artifact.

    Only items that carry a final AI verdict are resumable: an ``error``,
    ``no_imagery`` or ``deferred_rate_limit`` item has no answer yet and is
    retried on the next run. Items without a usable span id are ignored, since
    they cannot be matched back to a candidate.
    """
    items = {}
    if not isinstance(previous, dict):
        return items
    suggestions = previous.get("suggestions")
    if not isinstance(suggestions, list):
        return items
    for item in suggestions:
        if not isinstance(item, dict):
            continue
        if item.get("review_status") != "pending":
            continue
        key = str(item.get("span_id") or "")
        if key:
            items[key] = item
    return items


def _rank_metric(candidate: dict, name: str) -> float:
    try:
        value = float(candidate.get(name, 0.0))
        return value if 0.0 <= value <= 1.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _queued_item(candidate: dict, status: str, reason: str) -> dict:
    """A review item for a span that was not (or could not be) sent to AI.

    Shares the artifact shape of a processed item so consumers can read the
    whole ``suggestions`` list uniformly.
    """
    return {
        "span_id": _candidate_key(candidate),
        "claimed_surface": candidate.get("claimed"),
        "coordinates": candidate.get("coordinates"),
        "coordinates_crs": candidate.get("coordinates_crs"),
        "geometry_reason": candidate.get("reason"),
        "geometry_confidence": candidate.get("confidence"),
        "known_share": candidate.get("known_share"),
        "AI_SURFACE": None,
        "confidence": None,
        "imagery_source": None,
        "imagery_date": None,
        "review_status": status,
        "review_required": True,
        "reason": reason,
        "error_detail": None,
    }


def _not_processed(candidate: dict) -> dict:
    return _queued_item(candidate, "not_processed_limit",
                        "Candidate limit reached; not sent to AI.")


def _deferred(candidate: dict) -> dict:
    return _queued_item(
        candidate, "deferred_rate_limit",
        "Rate limit reached; rerun to continue from here.")


def _ordered_suggestions(candidates: list, *item_maps: dict) -> list:
    """One review item per candidate, in the batch's sorted candidate order.

    Every candidate is expected to appear in exactly one map (resumed, freshly
    processed, deferred, or over the per-run limit), so the artifact keeps a
    single stable ordering however the run was split.
    """
    out = []
    for candidate in candidates:
        for mapping in item_maps:
            item = mapping.get(id(candidate))
            if item is not None:
                out.append(item)
                break
    return out


def _validate_image(image_bytes: bytes, mime_type: str) -> None:
    if not isinstance(image_bytes, bytes):
        raise ValueError("Imagery provider must return image bytes")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Imagery patch exceeds 10 MiB")
    if mime_type not in ("image/jpeg", "image/png", "image/webp"):
        raise ValueError("Imagery must be JPEG, PNG, or WebP")


def _classification_prompt() -> str:
    return (
        "Review this overhead aerial image patch for a fibre trench route. "
        "When a planned route is drawn it appears as a bright red line with a "
        "dark outline; if no route is drawn, judge the ground at the centre of "
        "the patch and say so. "
        "Classify the visible ground along the route (or at the centre) as exactly "
        "one family: road (vehicle carriageway), footway (pedestrian pavement "
        "or path), garden (grass, vegetation, or other off-road strip), or null "
        "if it cannot be determined reliably. Do not infer exact paving material. "
        "This is an advisory suggestion only; never request a design change. "
        "Return JSON with ai_surface, confidence from 0 to 1, and a brief reason. "
        "Lower confidence for blur, shadows, occlusion, ambiguous boundaries, "
        "stale imagery, or missing route context."
    )


def _reply_error(message: str, raw_reply) -> SurfaceAIReplyError:
    """Build the rejection error, carrying the model's own reply for diagnosis."""
    if not isinstance(raw_reply, str):
        try:
            raw_reply = json.dumps(raw_reply, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_reply = str(raw_reply)
    return SurfaceAIReplyError(message, str(raw_reply or "")[:MAX_ERROR_DETAIL])


def _parse_classification_json(text: str) -> dict:
    try:
        result = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _reply_error("Vision model returned invalid JSON", text) from exc
    # Constrained JSON output may still arrive wrapped in a one-item list, so
    # unwrap it rather than failing the span on a valid answer.
    if isinstance(result, list):
        if len(result) != 1:
            raise _reply_error(
                "Vision model returned an unsupported response shape", text)
        result = result[0]
    if not isinstance(result, dict):
        raise _reply_error(
            "Vision model returned an unsupported response shape", text)
    family = result.get("ai_surface")
    # Models spell the "cannot determine" answer as a string far more often than
    # as real JSON null — Gemini answered `"ai_surface": "null"` on a roof,
    # which is a legitimate abstention, not an invalid family. Read it as one.
    if isinstance(family, str):
        family = family.strip()
        if family.lower() in ("", "null", "none", "n/a", "na", "unknown"):
            family = None
    if family not in FAMILIES and family is not None:
        raise _reply_error(
            "Vision model returned an unsupported surface family", text)
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise _reply_error("Vision model returned invalid confidence", text) from exc
    if not 0.0 <= confidence <= 1.0:
        raise _reply_error(
            "Vision model confidence must be between 0 and 1", text)
    return {
        "ai_surface": family,
        "confidence": round(confidence, 3),
        "reason": str(result.get("reason") or "")[:500],
    }


def _parse_model_response(payload: dict) -> dict:
    """Extract and validate Gemini's constrained JSON response."""
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _reply_error("Gemini returned an invalid structured response", payload) from exc
    return _parse_classification_json(text)


def _model_chain(primary: str) -> list:
    """Ordered models to try: the configured primary, then the fallbacks.

    Free-tier Gemini quota is per model, and a busy model can also return 503
    for minutes. ``SURFACE_AI_MODEL_FALLBACK`` (comma-separated) lets a review
    still produce a suggestion when the primary is exhausted.
    """
    names = [primary] + (os.environ.get("SURFACE_AI_MODEL_FALLBACK", "") or "").split(",")
    chain = []
    for name in names:
        name = str(name or "").strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def _gemini_classify(image_bytes: bytes, mime_type: str, api_key: str,
                     model: str, timeout: float = 30.0) -> dict:
    """Classify one local image using Gemini's REST API (no SDK dependency)."""
    _validate_image(image_bytes, mime_type)
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured")
    body = {
        "contents": [{"parts": [
            {"inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }},
            {"text": _classification_prompt()},
        ]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid Gemini model name")
    last_error = None
    for candidate in _model_chain(model):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
            raise ValueError("Invalid Gemini model name")
        request = urllib.request.Request(
            GEMINI_API_URL.format(model=candidate),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        for attempt in range(GEMINI_RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return _parse_model_response(payload)
            except urllib.error.HTTPError as exc:
                last_error = exc
                # A non-transient status (404 for a model this key cannot use)
                # means no amount of retrying this model will help.
                if exc.code not in GEMINI_TRANSIENT_STATUS:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < GEMINI_RETRY_ATTEMPTS - 1:
                time.sleep(GEMINI_RETRY_BACKOFF_SECONDS * (attempt + 1))
    # A 429 on the last model means the whole chain is out of quota, which no
    # further candidate in this batch can fix; let the caller stop early.
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
        raise SurfaceAIQuotaExceeded(
            "Gemini surface review quota is exhausted") from last_error
    raise RuntimeError("Gemini surface review request failed") from last_error


def _ollama_base_url() -> str:
    raw_url = os.environ.get("SURFACE_AI_OLLAMA_URL", DEFAULT_OLLAMA_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("SURFACE_AI_OLLAMA_URL must be an HTTP(S) base URL")
    return raw_url


def _ollama_classify(image_bytes: bytes, mime_type: str, _api_key: str,
                     model: str, timeout: float = None) -> dict:
    """Send one image to the configured local Ollama vision model."""
    if timeout is None:
        timeout = _ollama_timeout_seconds()
    _validate_image(image_bytes, mime_type)
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
        raise ValueError("Invalid Ollama model name")
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": _classification_prompt(),
            "images": [base64.b64encode(image_bytes).decode("ascii")],
        }],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0, "num_predict": _ollama_num_predict()},
    }
    request = urllib.request.Request(
        _ollama_base_url() + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["message"]["content"]
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
            KeyError, TypeError) as exc:
        raise RuntimeError("Ollama surface review request failed; check Ollama and that the model is pulled") from exc
    return _parse_classification_json(text)


def _coords_to_lonlat(coords, crs_name: str):
    """Transform coordinates from their recorded CRS to WGS84 lon/lat pairs."""
    crs_name = str(crs_name or "").strip().upper()
    if crs_name in ("EPSG:4326", "CRS84", "OGC:CRS84"):
        # Validate the degrees path the same way the transform path is: a
        # projected coordinate mislabelled as WGS84 (an easy caller mistake, and
        # the default CRS) otherwise becomes a nonsense bbox and an image of
        # nowhere, instead of the error that says what went wrong.
        result = []
        for point in coords:
            lon, lat = float(point[0]), float(point[1])
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(
                    "Coordinate is outside WGS84 bounds; check coordinates_crs")
            result.append((lon, lat))
        return result
    try:
        from osgeo import osr
        source = osr.SpatialReference()
        if not crs_name or source.SetFromUserInput(crs_name) != 0:
            raise ValueError("Candidate route CRS is missing or unsupported")
        target = osr.SpatialReference()
        if target.ImportFromEPSG(4326) != 0:
            raise ValueError("Unable to initialize WGS84 CRS")
        for srs in (source, target):
            try:
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:
                pass
        transform = osr.CoordinateTransformation(source, target)
        result = []
        for point in coords:
            lon, lat, *_ = transform.TransformPoint(float(point[0]), float(point[1]))
            if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Transformed candidate coordinate is outside WGS84 bounds")
            result.append((lon, lat))
        return result
    except ImportError as exc:
        raise RuntimeError("QGIS/GDAL is required to transform the HLD route to WGS84") from exc


def _candidate_lonlat(candidate: dict):
    """Transform candidate route vertices from their recorded CRS to WGS84."""
    coords = candidate.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ValueError("Candidate has no usable route geometry")
    return _coords_to_lonlat(coords, candidate.get("coordinates_crs"))


def _route_bbox(route_lonlat, padding_m: float = 20.0):
    lons = [point[0] for point in route_lonlat]
    lats = [point[1] for point in route_lonlat]
    middle_lat = sum(lats) / len(lats)
    cos_lat = max(abs(math.cos(math.radians(middle_lat))), 0.1)
    pad_lat = padding_m / 110540.0
    pad_lon = padding_m / (111320.0 * cos_lat)
    return (min(lons) - pad_lon, min(lats) - pad_lat,
            max(lons) + pad_lon, max(lats) + pad_lat)


def _segment_lonlat(lon: float, lat: float, length_m: float = 8.0,
                    bearing: Optional[float] = None):
    """Build a short two-point WGS84 segment centred on one point.

    The imagery provider highlights a route line, so a clicked point is
    expressed as a very short segment through it. ``bearing`` is degrees
    clockwise from north; ``None`` runs the segment north/south. Keeping the
    segment short means its orientation barely affects what sits "along" it.
    """
    try:
        length = float(length_m)
    except (TypeError, ValueError):
        length = 8.0
    length = max(0.5, min(length, 500.0))
    try:
        angle = math.radians(float(bearing)) if bearing is not None else 0.0
    except (TypeError, ValueError):
        angle = 0.0
    half = length / 2.0
    north_m = half * math.cos(angle)
    east_m = half * math.sin(angle)
    cos_lat = max(abs(math.cos(math.radians(lat))), 0.1)
    dlat = north_m / 110540.0
    dlon = east_m / (111320.0 * cos_lat)
    return [(lon - dlon, lat - dlat), (lon + dlon, lat + dlat)]


def _image_size_for_bbox(bbox, max_dimension: int = None):
    if max_dimension is None:
        max_dimension = _max_image_dimension()
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = (min_lat + max_lat) / 2.0
    ground_width_m = max((max_lon - min_lon) * 111320.0 * abs(math.cos(math.radians(mean_lat))), 1.0)
    ground_height_m = max((max_lat - min_lat) * 110540.0, 1.0)
    # DOP20 is 20 cm/pixel. Request no more than the source resolution, while
    # keeping large route boxes within the WMS image-size ceiling.
    scale = min(1.0 / 0.2, max_dimension / max(ground_width_m, ground_height_m))
    width = max(64, min(max_dimension, int(round(ground_width_m * scale))))
    height = max(64, min(max_dimension, int(round(ground_height_m * scale))))
    return width, height


def _wms_get_map(bbox, width: int, height: int) -> bytes:
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": IGN_LAYER, "STYLES": "", "CRS": "CRS:84",
        "BBOX": ",".join(format(value, ".8f") for value in bbox),
        "WIDTH": str(width), "HEIGHT": str(height), "FORMAT": "image/jpeg",
    }
    request = urllib.request.Request(
        IGN_WMS_URL + urllib.parse.urlencode(params),
        headers={"Accept": "image/jpeg", "User-Agent": "Fibre-FTTH-SurfaceReview/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=WMS_TIMEOUT_SECONDS) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            image_bytes = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("IGN BD ORTHO WMS request failed") from exc
    if "image/" not in content_type:
        raise RuntimeError("IGN BD ORTHO returned no image; location may be outside French coverage")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("IGN imagery patch exceeds 10 MiB")
    return image_bytes


def _capture_date_from_info(text: str) -> Optional[str]:
    """Return a date only when GetFeatureInfo labels it as date metadata."""
    values = []
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None

    markers = ("date", "acquisition", "capture", "millesime", "millsime", "annee", "anne")

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub("[^a-z]", "", str(key).lower())
                if any(marker in normalized for marker in markers):
                    if isinstance(child, (str, int, float)):
                        values.append(str(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    if payload is not None:
        visit(payload)
    else:
        for line in text.splitlines():
            if re.search("date|acquisition|capture|mill[eé]sime|ann[eé]e", line, re.I):
                values.append(line)

    for value in values:
        parts = re.split("[^0-9]+", value.strip())
        parts = [part for part in parts if part]
        if not parts or len(parts[0]) != 4 or not parts[0].isdigit():
            continue
        try:
            if len(parts) >= 3:
                return datetime.strptime("-".join(parts[:3]), "%Y-%m-%d").strftime("%Y-%m-%d")
            if len(parts) == 2:
                return datetime.strptime("-".join(parts), "%Y-%m").strftime("%Y-%m")
            return datetime.strptime(parts[0], "%Y").strftime("%Y")
        except ValueError:
            continue
    return None


def _wms_capture_date(bbox, width: int, height: int) -> Optional[str]:
    """Best-effort WMS GetFeatureInfo query; date may be absent in IGN replies."""
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo",
        "LAYERS": IGN_LAYER, "QUERY_LAYERS": IGN_LAYER, "STYLES": "",
        "CRS": "CRS:84",
        "BBOX": ",".join(format(value, ".8f") for value in bbox),
        "WIDTH": str(width), "HEIGHT": str(height), "FORMAT": "image/jpeg",
        "INFO_FORMAT": "application/json", "I": str(width // 2), "J": str(height // 2),
    }
    request = urllib.request.Request(
        IGN_WMS_URL + urllib.parse.urlencode(params),
        headers={"Accept": "application/json, text/plain, text/html",
                 "User-Agent": "Fibre-FTTH-SurfaceReview/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=WMS_TIMEOUT_SECONDS) as response:
            text = response.read(64 * 1024).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return _capture_date_from_info(text)


def _qt_pen_enums(Qt):
    """Resolve Qt pen style/cap/join enums across Qt5 and Qt6 bindings.

    Qt6 removed the unscoped ``QPen.SolidLine`` style names, so unscoped
    lookups raise ``AttributeError`` there. Prefer the scoped form and fall
    back to the unscoped one for older bindings.
    """
    def resolve(group: str, member: str):
        scoped = getattr(Qt, group, None)
        if scoped is not None and hasattr(scoped, member):
            return getattr(scoped, member)
        if hasattr(Qt, member):
            return getattr(Qt, member)
        raise RuntimeError("Qt binding does not expose a usable %s enum" % member)

    return (
        resolve("PenStyle", "SolidLine"),
        resolve("PenCapStyle", "RoundCap"),
        resolve("PenJoinStyle", "RoundJoin"),
    )


def _qt_overlay_available() -> bool:
    """Whether the QGIS Qt bindings needed to draw the route can be imported.

    The container image bundles QGIS, but the host engine runs Anaconda Python
    and adding QGIS's Python 3.12 packages to it breaks ``pydantic_core``, so
    the route marker cannot be drawn in-process there. Callers fall back to an
    unmarked patch instead of failing the whole review.
    """
    try:
        from qgis.PyQt.QtCore import QBuffer, QIODevice, QPointF, Qt  # noqa: F401
        from qgis.PyQt.QtGui import (  # noqa: F401
            QColor, QImage, QPainter, QPen, QPolygonF,
        )
    except ImportError:
        return False
    return True


def _overlay_route(image_bytes: bytes, bbox, route_lonlat) -> bytes:
    """Draw a high-contrast route line over the WMS patch using bundled QGIS Qt."""
    try:
        from qgis.PyQt.QtCore import QBuffer, QIODevice
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
        from qgis.PyQt.QtCore import QPointF
    except ImportError as exc:
        raise RuntimeError("QGIS Qt is required to highlight the planned route") from exc
    image = QImage()
    if not image.loadFromData(image_bytes) or image.isNull():
        raise ValueError("IGN response could not be decoded as an image")
    min_lon, min_lat, max_lon, max_lat = bbox
    points = [QPointF(
        (lon - min_lon) / (max_lon - min_lon) * image.width(),
        (max_lat - lat) / (max_lat - min_lat) * image.height(),
    ) for lon, lat in route_lonlat]
    if len(points) < 2:
        raise ValueError("Candidate route cannot be overlaid")
    pen_style, pen_cap, pen_join = _qt_pen_enums(Qt)
    polygon = QPolygonF(points)
    # Always end the painter, otherwise Qt destroys a device mid-paint and the
    # original failure is masked by a QPaintDevice warning.
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(15, 23, 42, 230), 9, pen_style, pen_cap, pen_join))
        painter.drawPolyline(polygon)
        painter.setPen(QPen(QColor(255, 45, 45, 255), 5, pen_style, pen_cap, pen_join))
        painter.drawPolyline(polygon)
    finally:
        painter.end()
    buffer = QBuffer()
    if not buffer.open(QIODevice.WriteOnly):
        raise RuntimeError("Unable to create overlaid imagery buffer")
    try:
        if not image.save(buffer, "JPEG", 90):
            raise RuntimeError("Unable to encode route-highlighted imagery")
        result = bytes(buffer.data())
    finally:
        buffer.close()
    if len(result) > MAX_IMAGE_BYTES:
        raise ValueError("Route-highlighted imagery exceeds 10 MiB")
    return result


def ign_bd_ortho_image(candidate: dict) -> Optional[dict]:
    """Fetch IGN BD ORTHO imagery at a candidate span and overlay its route.

    Returns JPEG image bytes plus source and best-effort capture date. IGN's
    WMS is open data (Open Licence 2.0); source attribution is retained in the
    review artifact. No request is sent to this provider unless the review pass
    is opted into and at least one uncertain candidate is selected.  When IGN
    holds no imagery for the point it returns a blank patch instead of failing,
    so ``has_imagery`` reports that state for ``worldwide_surface_imagery`` to
    act on — it is measured on the RAW patch, because drawing the route over a
    blank one would hide the signal.
    """
    route = _candidate_lonlat(candidate)
    bbox = _route_bbox(route)
    width, height = _image_size_for_bbox(bbox)
    raw = _wms_get_map(bbox, width, height)
    has_imagery = _ign_patch_has_content(raw)
    if _qt_overlay_available():
        highlighted = _overlay_route(raw, bbox, route)
        overlaid = True
    else:
        highlighted = raw
        overlaid = False
    capture_date = _wms_capture_date(bbox, width, height)
    return {
        "image_bytes": highlighted,
        "mime_type": "image/jpeg",
        "source": IGN_SOURCE,
        "date": capture_date,
        "has_imagery": has_imagery,
        # Whether the planned route was drawn on the patch. When QGIS Qt is
        # unavailable the raw patch is sent, and the artifact records that the
        # classifier judged the centre of the image rather than a marked route.
        "route_overlaid": overlaid,
    }


def _ign_patch_has_content(image_bytes: bytes) -> bool:
    """False when IGN returned its constant-white no-coverage patch.

    The reference case is a 236x236 request, which is where the 1527-byte blank
    was measured; a larger request scales the blank with it, and the threshold
    is a fraction of the smallest real patch seen, so it holds for both.
    """
    return len(image_bytes) > IGN_BLANK_PATCH_MAX_BYTES


def _esri_get_map(bbox, width: int, height: int) -> bytes:
    """Fetch one image patch for a WGS84 bbox from Esri World Imagery."""
    params = {
        "bbox": ",".join(format(value, ".8f") for value in bbox),
        "bboxSR": "4326", "imageSR": "4326",
        "size": "%d,%d" % (width, height), "format": "jpg", "f": "image",
    }
    request = urllib.request.Request(
        ESRI_EXPORT_URL + urllib.parse.urlencode(params),
        headers={"Accept": "image/jpeg", "User-Agent": "Fibre-FTTH-SurfaceReview/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=WMS_TIMEOUT_SECONDS) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            image_bytes = response.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # "Error: bytes" — the service declined the bbox because it holds no
        # imagery for it, which is the answer for open water and similar.
        raise ImageryUnavailable(
            "Esri World Imagery has no imagery for this location") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Esri World Imagery request failed") from exc
    # A 200 carrying something other than an image is still a refusal, so it is
    # the same "no imagery" state as the 500 above, not a transport error.
    if "image/" not in content_type:
        raise ImageryUnavailable("Esri World Imagery returned no image")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Imagery patch exceeds 10 MiB")
    return image_bytes


def esri_world_imagery(candidate: dict) -> Optional[dict]:
    """Fetch a patch from Esri World Imagery, which covers the whole world.

    Same contract as :func:`ign_bd_ortho_image`: one image for a candidate span,
    with the planned route drawn over it when the Qt bindings exist. Esri
    publishes no capture date for an export patch, so ``date`` stays None and the
    review records the imagery as undated rather than guessing one.
    """
    route = _candidate_lonlat(candidate)
    bbox = _route_bbox(route)
    width, height = _image_size_for_bbox(bbox)
    raw = _esri_get_map(bbox, width, height)
    if _qt_overlay_available():
        highlighted = _overlay_route(raw, bbox, route)
        overlaid = True
    else:
        highlighted = raw
        overlaid = False
    return {
        "image_bytes": highlighted,
        "mime_type": "image/jpeg",
        "source": ESRI_SOURCE,
        "date": None,
        "route_overlaid": overlaid,
        "has_imagery": True,
    }


def worldwide_surface_imagery(candidate: dict) -> Optional[dict]:
    """IGN BD ORTHO where it has imagery, Esri World Imagery everywhere else.

    IGN is still tried first because it is 20 cm and openly licensed, but it only
    covers France and reports a point it has no imagery for as a blank patch
    instead of an error. Sending that blank patch to the model produced
    "completely blank image" abstentions for every span outside France (and over
    open water, which BD ORTHO also does not cover), so an empty or failed IGN
    answer is re-fetched from Esri. The artifact records ``imagery_source`` per
    span, so which provider actually answered remains visible. When neither
    source holds imagery — open water, or anywhere Esri publishes no
    high-resolution tiles — it returns None, which the review reports as
    ``no_imagery`` rather than as a provider failure.
    """
    try:
        image = ign_bd_ortho_image(candidate)
    except RuntimeError:
        # Any IGN refusal is worth retrying from Esri, including its documented
        # "outside French coverage" error.
        image = None
    if image is not None and image.get("has_imagery", True):
        return image
    try:
        return esri_world_imagery(candidate)
    except ImageryUnavailable:
        # Neither source holds imagery here. Returning None makes the review
        # report "no_imagery" — a state it already has — instead of an opaque
        # provider error for a span that simply has no photo of it.
        return None


def review_uncertain_spans(
    candidates: Iterable[dict],
    image_provider: Optional[Callable[[dict], Optional[dict]]] = None,
    *,
    enabled: Optional[bool] = None,
    api_key: Optional[str] = None,
    classifier: Optional[Callable[[bytes, str, str, str], dict]] = None,
    previous: Optional[dict] = None,
) -> dict:
    """Suggest surfaces for uncertain candidates with separately sourced imagery.

    ``image_provider(candidate)`` returns ``{"image_bytes": bytes,
    "mime_type": "image/jpeg", "source": str, "date": str|None}``, or
    ``None`` when no image is available. Injected classifier/provider hooks make
    this pure Python function testable without network access or credentials.

    ``previous`` is a prior review artifact. Spans it already answered are
    carried forward untouched and only the unanswered ones are sent to AI, so a
    rerun makes progress instead of paying for the same spans again. Calls are
    paced by ``SURFACE_AI_MIN_INTERVAL_SECONDS`` and, when a provider reports
    exhausted quota, the batch stops and marks the remaining spans deferred.
    """
    enabled = (os.environ.get("SURFACE_AI_REVIEW", "").strip().lower()
               in ("1", "true", "yes", "on")) if enabled is None else bool(enabled)
    candidates = list(candidates)
    candidates.sort(key=lambda candidate: (
        _rank_metric(candidate, "known_share"),
        _rank_metric(candidate, "confidence"),
    ))
    total_candidates = len(candidates)
    prior = _processed_items(previous)
    # Split the sorted candidates into the answers a previous run already
    # produced (free to reuse) and the still-unprocessed remainder.
    resumed = {}
    todo = []
    for candidate in candidates:
        key = _candidate_key(candidate)
        done = prior.get(key) if key else None
        if done is not None:
            resumed[id(candidate)] = done
        else:
            todo.append(candidate)
    candidate_limit = _candidate_limit()
    selected = todo[:candidate_limit]
    over_limit = todo[candidate_limit:]
    skipped_count = len(over_limit)
    if not enabled:
        report = _empty_report("disabled", "Set SURFACE_AI_REVIEW=1 to enable imagery review.")
        report["candidate_count"] = 0
        report["skipped_count"] = 0
        return report
    if image_provider is None:
        report = _empty_report(
            "awaiting_imagery_source",
            "No imagery source configured; design and geometry results are unchanged.",
        )
        report["candidate_count"] = total_candidates
        report["processed_count"] = len(selected)
        report["skipped_count"] = skipped_count
        report["resumed_count"] = len(resumed)
        awaiting = {id(candidate): _queued_item(
            candidate, "awaiting_imagery", "No imagery source configured."
        ) for candidate in selected}
        report["suggestions"] = _ordered_suggestions(
            candidates, resumed, awaiting,
            {id(candidate): _not_processed(candidate)
             for candidate in over_limit})
        return report
    provider = _provider_name()
    model = _model_name(provider)
    api_key = api_key if api_key is not None else (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    classify = classifier or (_ollama_classify if provider == "ollama" else _gemini_classify)
    if provider == "gemini" and not api_key and classifier is None:
        report = _empty_report("missing_credentials", "GEMINI_API_KEY is not configured.")
        report["candidate_count"] = total_candidates
        report["processed_count"] = len(selected)
        report["skipped_count"] = skipped_count
        report["resumed_count"] = len(resumed)
        blocked = {id(candidate): _queued_item(
            candidate, "missing_credentials", "GEMINI_API_KEY is not configured."
        ) for candidate in selected}
        report["suggestions"] = _ordered_suggestions(
            candidates, resumed, blocked,
            {id(candidate): _not_processed(candidate)
             for candidate in over_limit})
        return report

    report = _empty_report("ready")
    report["provider"] = _report_provider(provider)
    report["model"] = model
    report["candidate_count"] = total_candidates
    report["processed_count"] = len(selected)
    report["skipped_count"] = skipped_count
    report["resumed_count"] = len(resumed)
    # An injected classifier is a test seam, not a metered API, so only the real
    # provider path is paced.
    throttle = _throttle_interval(provider) if classifier is None else 0.0
    report["min_interval_seconds"] = throttle
    seen_sources = set()
    seen_dates = set()
    processed = {}
    deferred = {}
    last_call = None
    for index, candidate in enumerate(selected):
        item = {
            "span_id": _candidate_key(candidate),
            "claimed_surface": candidate.get("claimed"),
            "coordinates": candidate.get("coordinates"),
            "coordinates_crs": candidate.get("coordinates_crs"),
            "geometry_reason": candidate.get("reason"),
            "geometry_confidence": candidate.get("confidence"),
            "known_share": candidate.get("known_share"),
            "review_status": "pending",
            "review_required": True,
            "AI_SURFACE": None,
            "confidence": None,
            "imagery_source": None,
            "imagery_date": None,
            "imagery_route_overlaid": None,
            "reason": None,
            "error_detail": None,
        }
        try:
            if throttle > 0:
                now = time.monotonic()
                if last_call is not None and now - last_call < throttle:
                    time.sleep(throttle - (now - last_call))
            image = image_provider(candidate)
            if not image:
                item.update(review_status="no_imagery", review_required=True,
                            reason="No imagery available for this span.")
            else:
                if not isinstance(image, dict):
                    raise ValueError("Imagery provider must return an image record")
                source = str(image.get("source") or "").strip()
                date = image.get("date")
                if not source:
                    raise ValueError("Imagery provider must identify its source")
                if date is not None and not str(date).strip():
                    date = None
                item["imagery_source"] = source
                item["imagery_date"] = str(date) if date else None
                overlaid = image.get("route_overlaid")
                item["imagery_route_overlaid"] = None if overlaid is None else bool(overlaid)
                if source:
                    seen_sources.add(source)
                if date:
                    seen_dates.add(str(date))
                result = classify(
                    image["image_bytes"], image.get("mime_type", "image/jpeg"),
                    api_key or "", model,
                )
                if result.get("ai_surface") not in FAMILIES and result.get("ai_surface") is not None:
                    raise ValueError("Classifier returned an unsupported surface family")
                confidence = float(result.get("confidence"))
                if not 0.0 <= confidence <= 1.0:
                    raise ValueError("Classifier returned invalid confidence")
                item["AI_SURFACE"] = result.get("ai_surface")
                item["confidence"] = round(confidence, 3)
                item["reason"] = str(result.get("reason") or "")[:500]
                item["review_status"] = "pending"
                item["review_required"] = True
        except SurfaceAIQuotaExceeded:
            # The whole model chain is out of quota, so no later candidate can
            # succeed. Stop spending and leave the remainder for a rerun.
            item.update(review_status="deferred_rate_limit", review_required=True,
                        reason="Rate limit reached; rerun to continue from here.")
            processed[id(candidate)] = item
            deferred = {id(rest): _deferred(rest) for rest in selected[index + 1:]}
            break
        except Exception as exc:
            item.update(review_status="error", review_required=True,
                        reason=type(exc).__name__,
                        error_detail=getattr(exc, "raw_reply", None))
        else:
            last_call = time.monotonic()
        processed[id(candidate)] = item
    if len(seen_sources) == 1:
        report["imagery_source"] = next(iter(seen_sources))
    if len(seen_dates) == 1:
        report["imagery_date"] = next(iter(seen_dates))
    report["deferred_count"] = len(deferred)
    report["suggestions"] = _ordered_suggestions(
        candidates, resumed, processed, deferred,
        {id(candidate): _not_processed(candidate) for candidate in over_limit})
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["review_required"] = True
    return report


POINT_SPAN_PREFIX = "CLICK"


def _review_item(span_id: str) -> dict:
    """The item shape every surface answer uses, batched or on demand.

    Keeping the batch artifact's fields means the results page renders a span the
    pipeline answered and a span a reader opted into identically. ``provider`` and
    ``model`` name what an on-demand answer actually used; the batch artifact
    records those only at report level.
    """
    return {
        "span_id": span_id,
        "point": None,
        "claimed_surface": None,
        "coordinates": None,
        "coordinates_crs": "EPSG:4326",
        "geometry_reason": None,
        "geometry_confidence": None,
        "known_share": None,
        "AI_SURFACE": None,
        "confidence": None,
        "imagery_source": None,
        "imagery_date": None,
        "imagery_route_overlaid": None,
        "review_status": "pending",
        "review_required": True,
        "provider": _report_provider(),
        "model": _model_name(),
        "reason": None,
        "error_detail": None,
    }


def _point_pair(coordinates) -> tuple:
    """Validate one ``[x, y]`` coordinate pair, rejecting anything unusable."""
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise ValueError("A point needs [x, y] coordinates")
    try:
        x = float(coordinates[0])
        y = float(coordinates[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("Point coordinates must be numeric") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("Point coordinates must be finite")
    return x, y


def _route_points(coordinates) -> list:
    """Validate a span's own route line (two or more ``[x, y]`` pairs)."""
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise ValueError("A span needs at least two coordinates")
    points = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("Span coordinates must be [x, y] pairs")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError("Span coordinates must be numeric") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Span coordinates must be finite")
        points.append((x, y))
    return points


def _apply_image(item: dict, image, include_imagery: bool,
                 no_imagery_reason: str) -> bool:
    """Record a provider's patch on the item; True when there is imagery.

    ``include_imagery`` carries the patch itself as base64 so a caller can show
    exactly what the model was given. The batch artifact deliberately stores no
    image bytes, so this is off unless someone asked for it.
    """
    if not image:
        item.update(review_status="no_imagery", reason=no_imagery_reason)
        return False
    if not isinstance(image, dict):
        raise ValueError("Imagery provider must return an image record")
    source = str(image.get("source") or "").strip()
    date = image.get("date")
    if not source:
        raise ValueError("Imagery provider must identify its source")
    if date is not None and not str(date).strip():
        date = None
    item["imagery_source"] = source
    item["imagery_date"] = str(date) if date else None
    overlaid = image.get("route_overlaid")
    item["imagery_route_overlaid"] = None if overlaid is None else bool(overlaid)
    if include_imagery and isinstance(image.get("image_bytes"), bytes):
        item["image_base64"] = base64.b64encode(image["image_bytes"]).decode("ascii")
        item["image_mime_type"] = image.get("mime_type") or "image/jpeg"
    return True


def _surface_review(item: dict, candidate: dict, *,
                    image_provider=None, classifier=None, api_key=None,
                    include_imagery: bool = False,
                    no_imagery_reason: str = "No imagery available.") -> dict:
    """Answer one candidate: its imagery, then the vision model. Advisory only.

    Shared by the batch pass, the clicked-point probe and the per-span opt-in so
    all three agree on enablement, credentials, the no-imagery/error states and
    the item shape. A provider or model failure never raises — it is recorded as
    the item's ``review_status``.
    """
    enabled = (os.environ.get("SURFACE_AI_REVIEW", "").strip().lower()
               in ("1", "true", "yes", "on"))
    if not enabled:
        item.update(review_status="disabled",
                    reason="Set SURFACE_AI_REVIEW=1 to enable imagery review.")
        return item
    provider_callable = (image_provider if image_provider is not None
                         else configured_image_provider())
    if provider_callable is None:
        item.update(review_status="awaiting_imagery_source",
                    reason="No imagery source configured.")
        return item
    provider = _provider_name()
    model = _model_name(provider)
    item["provider"] = _report_provider(provider)
    item["model"] = model
    api_key = api_key if api_key is not None else (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    classify = classifier or (_ollama_classify if provider == "ollama" else _gemini_classify)
    if provider == "gemini" and not api_key and classifier is None:
        item.update(review_status="missing_credentials",
                    reason="GEMINI_API_KEY is not configured.")
        return item
    try:
        image = provider_callable(candidate)
        if not _apply_image(item, image, include_imagery, no_imagery_reason):
            return item
        result = classify(
            image["image_bytes"], image.get("mime_type", "image/jpeg"),
            api_key or "", model,
        )
        if result.get("ai_surface") not in FAMILIES and result.get("ai_surface") is not None:
            raise ValueError("Classifier returned an unsupported surface family")
        confidence = float(result.get("confidence"))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Classifier returned invalid confidence")
        item["AI_SURFACE"] = result.get("ai_surface")
        item["confidence"] = round(confidence, 3)
        item["reason"] = str(result.get("reason") or "")[:500]
        item["review_status"] = "pending"
        item["review_required"] = True
    except Exception as exc:
        item.update(review_status="error", review_required=True,
                    reason=type(exc).__name__,
                    error_detail=getattr(exc, "raw_reply", None))
    return item


def classify_point(coordinates, crs: str = "EPSG:4326", *,
                   length_m: float = 8.0, bearing: Optional[float] = None,
                   span_id: Optional[str] = None,
                   image_provider: Optional[Callable[[dict], Optional[dict]]] = None,
                   classifier: Optional[Callable[[bytes, str, str, str], dict]] = None,
                   api_key: Optional[str] = None,
                   include_imagery: bool = False) -> dict:
    """Suggest the surface under a single clicked coordinate (advisory only).

    Same contract as ``review_uncertain_spans`` for one point: it never edits
    design outputs and never changes the deterministic geometry verdict. A
    short segment is synthesised through the point so the imagery provider can
    highlight where the answer applies. Returns one review item shaped like the
    artifact's ``suggestions`` entries, whose ``review_status`` is ``pending``,
    ``no_imagery``, ``error``, ``disabled``, ``awaiting_imagery_source`` or
    ``missing_credentials``. Invalid coordinates raise ``ValueError``.
    ``include_imagery`` also returns the patch the model saw, as base64.
    """
    x, y = _point_pair(coordinates)
    crs_name = str(crs or "").strip() or "EPSG:4326"
    lon, lat = _coords_to_lonlat([(x, y)], crs_name)[0]

    item = _review_item(span_id or "%s-%.5f-%.5f" % (POINT_SPAN_PREFIX, lon, lat))
    item["point"] = [round(lon, 7), round(lat, 7)]
    item["geometry_reason"] = "manual_point_probe"
    candidate = {
        "span_id": item["span_id"],
        "claimed": None,
        "coordinates": _segment_lonlat(lon, lat, length_m, bearing),
        "coordinates_crs": "EPSG:4326",
    }
    item["coordinates"] = candidate["coordinates"]
    return _surface_review(
        item, candidate, image_provider=image_provider, classifier=classifier,
        api_key=api_key, include_imagery=include_imagery,
        no_imagery_reason="No imagery available for this point.")


def classify_span(span_id, coordinates, coordinates_crs: str = "EPSG:4326", *,
                  claimed_surface=None, geometry_reason=None,
                  geometry_confidence=None, known_share=None,
                  image_provider: Optional[Callable[[dict], Optional[dict]]] = None,
                  classifier: Optional[Callable[[bytes, str, str, str], dict]] = None,
                  api_key: Optional[str] = None,
                  include_imagery: bool = False) -> dict:
    """Suggest the surface along ONE span a reader opted into (one call).

    The on-demand counterpart of ``review_uncertain_spans``: it takes the span's
    own route geometry — the vertices the geometry check left uncertain — rather
    than synthesising a probe segment, and sends exactly that one span to the
    imagery provider and the vision model. Nothing is written to the review
    artifact, so a reader chooses span by span and spends one call per choice.
    """
    span_id = str(span_id or "").strip()
    if not span_id:
        raise ValueError("A span needs an id")
    points = _route_points(coordinates)
    crs_name = str(coordinates_crs or "").strip() or "EPSG:4326"
    item = _review_item(span_id)
    item["claimed_surface"] = claimed_surface
    item["geometry_reason"] = geometry_reason
    item["geometry_confidence"] = geometry_confidence
    item["known_share"] = known_share
    item["coordinates"] = points
    item["coordinates_crs"] = crs_name
    candidate = {
        "span_id": span_id,
        "claimed": claimed_surface,
        "coordinates": points,
        "coordinates_crs": crs_name,
    }
    return _surface_review(
        item, candidate, image_provider=image_provider, classifier=classifier,
        api_key=api_key, include_imagery=include_imagery,
        no_imagery_reason="No imagery available for this span.")


def preview_imagery(coordinates, crs: str = "EPSG:4326", *,
                    length_m: float = 8.0, bearing: Optional[float] = None,
                    span_id: Optional[str] = None,
                    image_provider: Optional[Callable[[dict], Optional[dict]]] = None) -> dict:
    """Fetch the patch a detect would send, without contacting the model.

    Lets a reader see exactly which imagery the model would be given before
    spending a call on it. Accepts the same two input shapes as ``classify``: one
    ``[x, y]`` point (a short probe segment is synthesised through it, matching
    ``classify_point``) or a span's route line ``[[x, y], ...]``. The patch comes
    back as base64 in ``image_base64`` with its ``imagery_source``, ``imagery_date``
    and whether the planned route was drawn over it.
    """
    item = _review_item(str(span_id or "PREVIEW").strip() or "PREVIEW")
    if (isinstance(coordinates, (list, tuple)) and coordinates
            and isinstance(coordinates[0], (list, tuple))):
        points = _route_points(coordinates)
        crs_name = str(crs or "").strip() or "EPSG:4326"
        item["coordinates"] = points
        item["coordinates_crs"] = crs_name
        candidate = {"span_id": item["span_id"], "claimed": None,
                     "coordinates": points, "coordinates_crs": crs_name}
    else:
        x, y = _point_pair(coordinates)
        crs_name = str(crs or "").strip() or "EPSG:4326"
        lon, lat = _coords_to_lonlat([(x, y)], crs_name)[0]
        item["point"] = [round(lon, 7), round(lat, 7)]
        item["coordinates"] = _segment_lonlat(lon, lat, length_m, bearing)
        item["coordinates_crs"] = "EPSG:4326"
        candidate = {"span_id": item["span_id"], "claimed": None,
                     "coordinates": item["coordinates"], "coordinates_crs": "EPSG:4326"}

    enabled = (os.environ.get("SURFACE_AI_REVIEW", "").strip().lower()
               in ("1", "true", "yes", "on"))
    if not enabled:
        item.update(review_status="disabled",
                    reason="Set SURFACE_AI_REVIEW=1 to enable imagery review.")
        return item
    provider_callable = (image_provider if image_provider is not None
                         else configured_image_provider())
    if provider_callable is None:
        item.update(review_status="awaiting_imagery_source",
                    reason="No imagery source configured.")
        return item
    try:
        image = provider_callable(candidate)
        if not _apply_image(item, image, True,
                           "No imagery available for this location."):
            return item
        item["review_status"] = "pending"
        item["reason"] = "Patch preview only; the vision model was not called."
    except Exception as exc:
        item.update(review_status="error", review_required=True,
                    reason=type(exc).__name__,
                    error_detail=getattr(exc, "raw_reply", None))
    return item


def write_review_report(out_dir: str, report: dict) -> str:
    """Atomically write only the AI review artifact, never design/GPKG outputs."""
    path = Path(out_dir) / "surface_ai_review.json"
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    return str(path) 