"""Pure-Python tests for the opt-in surface imagery review prototype."""
from __future__ import annotations

import json
import math
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import surface_ai_review as review  # noqa: E402
from HLDPlanning.design import surface_cross_section as sx  # noqa: E402
from HLDPlanning.design import surface_geometry_check as sgc  # noqa: E402


def test_ai_review_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SURFACE_AI_REVIEW", raising=False)
    monkeypatch.delenv("SURFACE_AI_PROVIDER", raising=False)
    result = review.review_uncertain_spans([{"span_id": "T-1"}])
    assert result["status"] == "disabled"
    assert result["suggestions"] == []


def test_enabled_review_waits_for_an_imagery_source_without_guessing(monkeypatch):
    monkeypatch.delenv("SURFACE_AI_PROVIDER", raising=False)
    result = review.review_uncertain_spans(
        [{"span_id": "T-1", "claimed": "Asphalt"}], enabled=True)
    assert result["status"] == "awaiting_imagery_source"
    assert result["candidate_count"] == 1
    suggestion = result["suggestions"][0]
    assert suggestion["AI_SURFACE"] is None
    assert suggestion["confidence"] is None
    assert suggestion["imagery_source"] is None
    assert suggestion["imagery_date"] is None
    assert suggestion["review_status"] == "awaiting_imagery"


def test_image_provider_and_classifier_result_are_review_only():
    candidate = {"span_id": "T-2", "claimed": "Footway"}

    def image_provider(got):
        assert got == candidate
        return {
            "image_bytes": b"test-image",
            "mime_type": "image/jpeg",
            "source": "operator-supplied orthophoto",
            "date": "2026-05-10",
        }

    def classifier(image_bytes, mime_type, api_key, model):
        assert image_bytes == b"test-image"
        assert mime_type == "image/jpeg"
        assert api_key == "test-key"
        return {"ai_surface": "road", "confidence": 0.84,
                "reason": "Paved vehicle carriageway visible."}

    result = review.review_uncertain_spans(
        [candidate], image_provider, enabled=True, api_key="test-key",
        classifier=classifier)
    assert result["status"] == "ready"
    assert result["imagery_source"] == "operator-supplied orthophoto"
    suggestion = result["suggestions"][0]
    assert suggestion["AI_SURFACE"] == "road"
    assert suggestion["confidence"] == 0.84
    assert suggestion["imagery_source"] == "operator-supplied orthophoto"
    assert suggestion["imagery_date"] == "2026-05-10"
    assert suggestion["review_status"] == "pending"
    assert suggestion["review_required"] is True
    assert suggestion["claimed_surface"] == "Footway"


def test_one_bad_image_does_not_prevent_other_candidates():
    candidates = [{"span_id": "T-1"}, {"span_id": "T-2"}]

    def image_provider(candidate):
        if candidate["span_id"] == "T-1":
            raise OSError("unavailable")
        return {"image_bytes": b"img", "source": "local", "date": None}

    result = review.review_uncertain_spans(
        candidates, image_provider, enabled=True, api_key="test",
        classifier=lambda *_args: {"ai_surface": "garden", "confidence": 0.7,
                                   "reason": "Vegetation visible."})
    assert [item["review_status"] for item in result["suggestions"]] == ["error", "pending"]
    assert result["suggestions"][0]["reason"] == "OSError"
    assert result["suggestions"][1]["AI_SURFACE"] == "garden"


def test_gemini_response_and_image_input_are_validated(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{
                "text": json.dumps({"ai_surface": "road", "confidence": 0.8,
                                    "reason": "Carriageway."})
            }]}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    result = review._gemini_classify(b"patch", "image/jpeg", "key", "test-model")
    assert result["ai_surface"] == "road"
    assert captured["url"].endswith("/test-model:generateContent")
    image_part = captured["body"]["contents"][0]["parts"][0]["inline_data"]
    assert image_part["mime_type"] == "image/jpeg"
    assert image_part["data"] == "cGF0Y2g="
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"

    invalid = {"candidates": [{"content": {"parts": [{"text": json.dumps({
        "ai_surface": "roof", "confidence": 1.4,
    })}]}}]}
    try:
        review._parse_model_response(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported model output must not be accepted")

    for mime in ("image/tiff", "image/jpeg; charset=binary"):
        try:
            review._gemini_classify(b"patch", mime, "key", "test-model")
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported image MIME types must not be sent")
    try:
        review._gemini_classify(b"x" * (review.MAX_IMAGE_BYTES + 1),
                                "image/jpeg", "key", "test-model")
    except ValueError:
        pass
    else:
        raise AssertionError("oversized imagery must not be sent")


def test_missing_api_credentials_prevents_image_fetch(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Pin the provider: a deployed image sets SURFACE_AI_PROVIDER=ollama, which
    # needs no key, so leaving it ambient would silently skip this Gemini path.
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    provider_calls = []
    result = review.review_uncertain_spans(
        [{"span_id": "T-1"}],
        image_provider=lambda _candidate: provider_calls.append(True),
        enabled=True,
    )
    assert result["status"] == "missing_credentials"
    assert result["suggestions"][0]["review_status"] == "missing_credentials"
    assert provider_calls == []


def test_image_provenance_is_required_and_reported_for_mixed_dates():
    candidates = [{"span_id": "T-1"}, {"span_id": "T-2"}]
    index = iter((
        {"image_bytes": b"1", "source": "orthophoto A", "date": "2026-01-01"},
        {"image_bytes": b"2", "source": "orthophoto B", "date": "2026-02-01"},
    ))
    result = review.review_uncertain_spans(
        candidates, lambda _candidate: next(index), enabled=True, api_key="test",
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.8,
                                   "reason": "Paved."})
    assert result["imagery_source"] is None
    assert result["imagery_date"] is None
    assert [item["imagery_date"] for item in result["suggestions"]] == ["2026-01-01", "2026-02-01"]

    missing_source = review.review_uncertain_spans(
        [{"span_id": "T-3"}], lambda _candidate: {"image_bytes": b"x"},
        enabled=True, api_key="test",
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.8,
                                   "reason": "Paved."})
    assert missing_source["suggestions"][0]["review_status"] == "error"


def test_candidate_cap_keeps_unprocessed_spans_visible(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_MAX_SPANS", "1")
    candidates = [
        {"span_id": "T-1", "known_share": 0.2},
        {"span_id": "T-2", "known_share": 0.8},
    ]
    called = []
    result = review.review_uncertain_spans(
        candidates, lambda candidate: called.append(candidate["span_id"]) or {
            "image_bytes": b"x", "source": "local", "date": None,
        }, enabled=True, api_key="test",
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.8,
                                   "reason": "Paved."})
    assert called == ["T-1"]
    assert result["candidate_count"] == 2
    assert result["processed_count"] == 1
    assert result["skipped_count"] == 1
    assert result["suggestions"][1]["span_id"] == "T-2"
    assert result["suggestions"][1]["review_status"] == "not_processed_limit"
    assert result["suggestions"][1]["review_required"] is True


def test_review_report_writes_a_separate_json_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("SURFACE_AI_PROVIDER", raising=False)
    result = review.review_uncertain_spans(
        [{"span_id": "T-1", "claimed": "Asphalt"}], enabled=True)
    path = pathlib.Path(review.write_review_report(str(tmp_path), result))
    assert path.name == "surface_ai_review.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "awaiting_imagery_source"
    assert not (tmp_path / "Final_Trenches.gpkg").exists()


def test_ollama_vision_request_uses_local_chat_api_without_api_key(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps({
                "ai_surface": "footway", "confidence": 0.76,
                "reason": "The route follows a visible sidewalk.",
            })}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("SURFACE_AI_OLLAMA_URL", "http://ollama:11434/")
    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    result = review._ollama_classify(b"patch", "image/jpeg", "", "qwen2.5vl:3b")

    assert result["ai_surface"] == "footway"
    assert result["confidence"] == 0.76
    assert captured["url"] == "http://ollama:11434/api/chat"
    assert captured["body"]["model"] == "qwen2.5vl:3b"
    assert captured["body"]["messages"][0]["images"] == ["cGF0Y2g="]
    assert captured["body"]["format"] == "json"
    assert captured["body"]["stream"] is False


def test_ollama_provider_needs_no_gemini_key_and_reports_local_model(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    monkeypatch.setenv("SURFACE_AI_MODEL", "qwen2.5vl:3b")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = review.review_uncertain_spans(
        [{"span_id": "T-ollama"}],
        lambda _candidate: {"image_bytes": b"img", "mime_type": "image/jpeg",
                           "source": "local fixture", "date": None},
        enabled=True,
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.82,
                                   "reason": "Paved carriageway."},
    )
    assert result["provider"] == "ollama"
    assert result["model"] == "qwen2.5vl:3b"
    assert result["suggestions"][0]["AI_SURFACE"] == "road"
    assert result["suggestions"][0]["review_required"] is True


def test_configured_ign_provider_is_loadable(monkeypatch):
    monkeypatch.setenv(
        "SURFACE_AI_IMAGE_PROVIDER",
        "HLDPlanning.design.surface_ai_review:ign_bd_ortho_image",
    )
    assert review.configured_image_provider() is review.ign_bd_ortho_image


def test_ign_wms_requests_selected_bd_ortho_layer_with_wms_130_crs84(monkeypatch):
    captured = {}

    class Headers:
        def get(self, name, default=""):
            return "image/jpeg" if name.lower() == "content-type" else default

    class FakeResponse:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return b"fake-jpeg"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    image = review._wms_get_map((1.0, 49.0, 1.001, 49.001), 256, 256)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert image == b"fake-jpeg"
    assert query["LAYERS"] == [review.IGN_LAYER]
    assert query["CRS"] == ["CRS:84"]
    assert query["VERSION"] == ["1.3.0"]
    assert query["REQUEST"] == ["GetMap"]


def test_ign_getfeatureinfo_date_requires_labeled_metadata(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return b'{"features":[{"properties":{"capture_date":"2024-03-07"}}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    date = review._wms_capture_date((1.0, 49.0, 1.001, 49.001), 256, 256)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert date == "2024-03-07"
    assert query["CRS"] == ["CRS:84"]
    assert query["VERSION"] == ["1.3.0"]
    assert query["REQUEST"] == ["GetFeatureInfo"]

    assert review._capture_date_from_info('{"properties":{"id":"2024-03-07"}}') is None
    assert review._capture_date_from_info('{"properties":{"capture_date":"2024-19-44"}}') is None


def test_geometry_checker_preserves_per_span_uncertain_evidence_only():
    lon, lat = 13.4, 52.5
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    line = [(lon, lat), (lon + 50.0 / kx, lat + 30.0 / ky)]
    road_line = [(lon - 20.0 / kx, lat), (lon + 80.0 / kx, lat)]
    road = sgc.Road(road_line, sx.RoadTags.from_osm("residential", {"sidewalk": "no"}))
    span = sgc.Span("T-uncertain", line, "Asphalt")
    result = sgc.check_spans([span], [road])
    assert result["uncertain"] == 1
    assert len(result["uncertain_spans"]) == 1
    candidate = result["uncertain_spans"][0]
    assert candidate["span_id"] == "T-uncertain"
    assert candidate["coordinates_crs"] == "EPSG:4326"
    assert result["flags"] == []


def test_geometry_checker_preserves_custom_projected_candidate_crs():
    road = sgc.Road(
        [(500000.0, 5400000.0), (500100.0, 5400000.0)],
        sx.RoadTags.from_osm("residential", {"sidewalk": "no"}),
    )
    span = sgc.Span("T-projected", [(500000.0, 5400010.0), (500050.0, 5400030.0)], "Asphalt")
    result = sgc.check_spans(
        [span], [road], coordinates_are_projected=True,
        coordinates_crs="EPSG:32631")
    assert result["uncertain_spans"][0]["coordinates_crs"] == "EPSG:32631"


def test_point_classify_is_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SURFACE_AI_REVIEW", raising=False)
    item = review.classify_point([1.5248, 49.0762])
    assert item["review_status"] == "disabled"
    assert item["AI_SURFACE"] is None
    assert item["claimed_surface"] is None
    assert item["review_required"] is True


def test_point_classify_uses_the_imagery_provider_and_classifier(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    monkeypatch.setenv("SURFACE_AI_MODEL", "qwen2.5vl:3b")
    seen = {}

    def image_provider(candidate):
        seen["candidate"] = candidate
        return {"image_bytes": b"patch", "mime_type": "image/jpeg",
                "source": "IGN BD ORTHO (c) IGN", "date": "2024-03-07"}

    def classifier(image_bytes, mime_type, api_key, model):
        seen["classify"] = (image_bytes, mime_type, api_key, model)
        return {"ai_surface": "footway", "confidence": 0.71,
                "reason": "Sidewalk along the clicked point."}

    item = review.classify_point([1.5248, 49.0762], "EPSG:4326",
                                 image_provider=image_provider, classifier=classifier)
    assert item["review_status"] == "pending"
    assert item["AI_SURFACE"] == "footway"
    assert item["confidence"] == 0.71
    assert item["reason"] == "Sidewalk along the clicked point."
    assert item["imagery_source"] == "IGN BD ORTHO (c) IGN"
    assert item["imagery_date"] == "2024-03-07"
    assert item["review_required"] is True
    assert item["provider"] == "ollama"
    assert item["model"] == "qwen2.5vl:3b"
    assert item["point"] == [1.5248, 49.0762]
    assert seen["classify"][0] == b"patch"
    # The imagery provider is handed a short two-point segment through the point.
    assert seen["candidate"]["coordinates_crs"] == "EPSG:4326"
    coords = seen["candidate"]["coordinates"]
    assert len(coords) == 2
    assert all(abs(c[0] - 1.5248) < 0.001 for c in coords)
    assert all(abs(c[1] - 49.0762) < 0.001 for c in coords)


def test_point_classify_rejects_unusable_coordinates(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    for bad in (None, [], [1.0], ["a", "b"], [float("nan"), 1.0]):
        try:
            review.classify_point(bad)
        except ValueError:
            continue
        raise AssertionError("bad coordinates must raise ValueError: %r" % (bad,))


def test_point_classify_records_a_provider_failure_without_raising(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")

    def image_provider(_candidate):
        raise OSError("imagery unavailable")

    item = review.classify_point(
        [1.5248, 49.0762], image_provider=image_provider,
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.5})
    assert item["review_status"] == "error"
    assert item["reason"] == "OSError"
    assert item["AI_SURFACE"] is None
    assert item["review_required"] is True


def test_point_classify_reports_missing_imagery(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    item = review.classify_point(
        [1.5248, 49.0762], image_provider=lambda _candidate: None,
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.5})
    assert item["review_status"] == "no_imagery"
    assert item["AI_SURFACE"] is None


def test_point_classify_falls_back_when_the_provider_has_no_source(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    item = review.classify_point(
        [1.5248, 49.0762],
        image_provider=lambda _candidate: {"image_bytes": b"patch"},
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.5})
    assert item["review_status"] == "error"
    assert item["reason"] == "ValueError"


def test_point_classify_transforms_a_projected_coordinate(monkeypatch):
    pytest.importorskip("osgeo")
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    seen = {}

    def image_provider(candidate):
        seen["candidate"] = candidate
        return {"image_bytes": b"patch", "mime_type": "image/jpeg",
                "source": "IGN BD ORTHO", "date": None}

    # The Giverny project's own trench CRS is EPSG:25833.
    item = review.classify_point(
        [-482744.5175, 5524003.8769], "EPSG:25833",
        image_provider=image_provider,
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.6})
    assert item["review_status"] == "pending"
    assert abs(item["point"][0] - 1.5249) < 0.01
    assert abs(item["point"][1] - 49.0762) < 0.01
    # The provider always receives the synthesised segment in WGS84.
    assert seen["candidate"]["coordinates_crs"] == "EPSG:4326"
    assert seen["candidate"]["span_id"] == item["span_id"]


def _overlay_candidate():
    return {"span_id": "T-overlay", "claimed": None,
            "coordinates": [[1.5249, 49.0762], [1.52492, 49.07622]],
            "coordinates_crs": "EPSG:4326"}


def test_imagery_draws_the_route_when_the_qt_bindings_exist(monkeypatch):
    monkeypatch.setattr(review, "_qt_overlay_available", lambda: True)
    monkeypatch.setattr(review, "_overlay_route", lambda raw, bbox, route: b"overlaid")
    monkeypatch.setattr(review, "_wms_get_map", lambda bbox, width, height: b"raw")
    monkeypatch.setattr(review, "_wms_capture_date", lambda bbox, width, height: None)
    image = review.ign_bd_ortho_image(_overlay_candidate())
    assert image["image_bytes"] == b"overlaid"
    assert image["route_overlaid"] is True
    assert image["source"] == review.IGN_SOURCE


def test_imagery_falls_back_to_the_raw_patch_without_qt_bindings(monkeypatch):
    # The host engine has no qgis.PyQt; the review must still run, sending the
    # unmarked IGN patch instead of failing on every candidate.
    monkeypatch.setattr(review, "_qt_overlay_available", lambda: False)

    def _overlay_must_not_run(*_args):
        raise AssertionError("the overlay must not be attempted without Qt")

    monkeypatch.setattr(review, "_overlay_route", _overlay_must_not_run)
    monkeypatch.setattr(review, "_wms_get_map", lambda bbox, width, height: b"raw")
    monkeypatch.setattr(review, "_wms_capture_date", lambda bbox, width, height: None)
    image = review.ign_bd_ortho_image(_overlay_candidate())
    assert image["image_bytes"] == b"raw"
    assert image["route_overlaid"] is False
    assert image["source"] == review.IGN_SOURCE


def test_point_classify_records_a_missing_route_marker(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    item = review.classify_point(
        [1.5249, 49.0762],
        image_provider=lambda _c: {"image_bytes": b"patch", "mime_type": "image/jpeg",
                                   "source": "IGN BD ORTHO", "date": None,
                                   "route_overlaid": False},
        classifier=lambda *_args: {"ai_surface": "garden", "confidence": 0.6})
    assert item["review_status"] == "pending"
    assert item["AI_SURFACE"] == "garden"
    assert item["imagery_route_overlaid"] is False


def test_point_classify_leaves_the_marker_flag_unknown_for_custom_providers(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    item = review.classify_point(
        [1.5249, 49.0762],
        image_provider=lambda _c: {"image_bytes": b"patch", "mime_type": "image/jpeg",
                                   "source": "operator orthophoto", "date": None},
        classifier=lambda *_args: {"ai_surface": "road", "confidence": 0.7})
    assert item["imagery_route_overlaid"] is None


def test_classification_prompt_covers_an_unmarked_patch():
    prompt = review._classification_prompt()
    # An unmarked patch must not be described to the model as having a red route.
    assert "if no route is drawn" in prompt
    for family in review.FAMILIES:
        assert family in prompt


def test_classification_json_unwraps_a_single_item_list():
    # gemini-3.5-flash-lite answers with a one-item JSON array.
    payload = json.dumps([{"ai_surface": "road", "confidence": 0.8,
                           "reason": "Paved carriageway."}])
    result = review._parse_classification_json(payload)
    assert result["ai_surface"] == "road"
    assert result["confidence"] == 0.8


def test_classification_json_rejects_unsupported_shapes():
    for payload in ("[]", "[{}, {}]", '"road"', "42", "null"):
        try:
            review._parse_classification_json(payload)
        except ValueError:
            continue
        raise AssertionError("unsupported response shape must be rejected: %s" % payload)


def _gemini_body(text):
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()


def test_gemini_retries_a_transient_status_then_succeeds(monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return _gemini_body(json.dumps({"ai_surface": "road", "confidence": 0.9}))

    def fake_urlopen(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 503, "UNAVAILABLE", None, None)
        return FakeResponse()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    result = review._gemini_classify(b"patch", "image/jpeg", "key", "test-model")
    assert result["ai_surface"] == "road"
    assert len(calls) == 3


def test_gemini_does_not_retry_a_non_transient_status(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 404, "NOT_FOUND", None, None)

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    try:
        review._gemini_classify(b"patch", "image/jpeg", "key", "test-model")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a 404 must surface as RuntimeError")
    assert len(calls) == 1


def test_gemini_gives_up_after_the_retry_budget(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 429, "RESOURCE_EXHAUSTED", None, None)

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    try:
        review._gemini_classify(b"patch", "image/jpeg", "key", "test-model")
    except RuntimeError:
        pass
    else:
        raise AssertionError("persistent transient failure must surface as RuntimeError")
    assert len(calls) == review.GEMINI_RETRY_ATTEMPTS


def test_model_chain_keeps_order_and_drops_blanks_and_duplicates(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_MODEL_FALLBACK", " second ,, first , second ")
    assert review._model_chain("first") == ["first", "second"]


def test_model_chain_defaults_to_the_primary(monkeypatch):
    monkeypatch.delenv("SURFACE_AI_MODEL_FALLBACK", raising=False)
    assert review._model_chain("only-model") == ["only-model"]


def test_gemini_falls_back_when_the_primary_model_is_exhausted(monkeypatch):
    # Free-tier quota is per model, so a 429 on the primary must not lose the
    # suggestion when a fallback model can still answer.
    monkeypatch.setenv("SURFACE_AI_MODEL_FALLBACK", "fallback-model")
    seen_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return _gemini_body(json.dumps({"ai_surface": "garden", "confidence": 0.7}))

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        if "primary-model" in request.full_url:
            raise urllib.error.HTTPError(
                request.full_url, 429, "RESOURCE_EXHAUSTED", None, None)
        return FakeResponse()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    result = review._gemini_classify(b"patch", "image/jpeg", "key", "primary-model")
    assert result["ai_surface"] == "garden"
    assert sum("primary-model" in url for url in seen_urls) == review.GEMINI_RETRY_ATTEMPTS
    assert any("fallback-model" in url for url in seen_urls)


def test_gemini_reports_failure_when_every_model_is_exhausted(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_MODEL_FALLBACK", "fallback-model")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 429, "RESOURCE_EXHAUSTED", None, None)

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    try:
        review._gemini_classify(b"patch", "image/jpeg", "key", "primary-model")
    except RuntimeError:
        pass
    else:
        raise AssertionError("an exhausted model chain must surface as RuntimeError")
    assert len(calls) == 2 * review.GEMINI_RETRY_ATTEMPTS


def test_throttle_interval_defaults_per_provider(monkeypatch):
    monkeypatch.delenv("SURFACE_AI_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    assert review._throttle_interval() == review.GEMINI_DEFAULT_MIN_INTERVAL_SECONDS
    # Local inference is not metered per minute, so it is not paced by default.
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")
    assert review._throttle_interval() == 0.0
    monkeypatch.setenv("SURFACE_AI_MIN_INTERVAL_SECONDS", "1.5")
    assert review._throttle_interval() == 1.5
    monkeypatch.setenv("SURFACE_AI_MIN_INTERVAL_SECONDS", "junk")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    assert review._throttle_interval() == review.GEMINI_DEFAULT_MIN_INTERVAL_SECONDS


def test_review_paces_real_provider_calls(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    monkeypatch.setenv("SURFACE_AI_MIN_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(review, "_gemini_classify",
                        lambda *_args: {"ai_surface": "road", "confidence": 0.5})
    clock = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(review.time, "monotonic", lambda: next(clock))
    slept = []
    monkeypatch.setattr(review.time, "sleep", lambda seconds: slept.append(seconds))
    result = review.review_uncertain_spans(
        [{"span_id": "T-1"}, {"span_id": "T-2"}],
        lambda _candidate: {"image_bytes": b"x", "source": "local", "date": None},
        enabled=True)
    assert result["min_interval_seconds"] == 5
    # One second elapsed between calls, so the second waits the remaining four.
    assert slept == [4.0]
    assert [item["AI_SURFACE"] for item in result["suggestions"]] == ["road", "road"]


def test_review_defers_remaining_spans_when_quota_is_exhausted(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fetched = []

    def classifier(*_args):
        raise review.SurfaceAIQuotaExceeded("quota exhausted")

    def provider(candidate):
        fetched.append(candidate["span_id"])
        return {"image_bytes": b"x", "source": "local", "date": None}

    result = review.review_uncertain_spans(
        [{"span_id": "T-1"}, {"span_id": "T-2"}, {"span_id": "T-3"}],
        provider, enabled=True, classifier=classifier)
    statuses = {item["span_id"]: item["review_status"]
                for item in result["suggestions"]}
    assert statuses == {"T-1": "deferred_rate_limit",
                        "T-2": "deferred_rate_limit",
                        "T-3": "deferred_rate_limit"}
    assert result["deferred_count"] == 2
    # The batch stops at the first quota failure instead of burning the rest.
    assert fetched == ["T-1"]


def test_gemini_distinguishes_exhausted_quota_from_other_failures(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "RESOURCE_EXHAUSTED", None, None)

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(review.time, "sleep", lambda _seconds: None)
    try:
        review._gemini_classify(b"patch", "image/jpeg", "key", "test-model")
    except review.SurfaceAIQuotaExceeded:
        pass
    else:
        raise AssertionError("exhausted quota must surface as SurfaceAIQuotaExceeded")


def test_review_resumes_previous_answers_and_processes_the_rest(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_MAX_SPANS", "5")
    previous = {"suggestions": [
        {"span_id": "T-1", "review_status": "pending", "AI_SURFACE": "road",
         "confidence": 0.9, "imagery_source": "orthophoto",
         "review_required": True},
        # An errored span produced no answer, so it must be retried.
        {"span_id": "T-2", "review_status": "error", "AI_SURFACE": None},
    ]}
    called = []
    result = review.review_uncertain_spans(
        [{"span_id": "T-1"}, {"span_id": "T-2"}],
        lambda candidate: called.append(candidate["span_id"]) or {
            "image_bytes": b"x", "source": "local", "date": None},
        enabled=True, api_key="key",
        classifier=lambda *_args: {"ai_surface": "garden", "confidence": 0.6},
        previous=previous)
    assert called == ["T-2"]
    assert result["resumed_count"] == 1
    assert result["processed_count"] == 1
    items = {item["span_id"]: item for item in result["suggestions"]}
    assert items["T-1"]["AI_SURFACE"] == "road"
    assert items["T-1"]["confidence"] == 0.9
    assert items["T-1"]["imagery_source"] == "orthophoto"
    assert items["T-2"]["review_status"] == "pending"
    assert items["T-2"]["AI_SURFACE"] == "garden"


def test_review_ignores_a_previous_item_without_a_span_id(monkeypatch):
    previous = {"suggestions": ["not-a-dict", {"review_status": "pending"}]}
    assert review._processed_items(previous) == {}
    assert review._processed_items(None) == {}


def test_rejected_reply_carries_the_models_own_text():
    payload = json.dumps({"ai_surface": "roof", "confidence": 0.9,
                          "reason": "the clicked point is on a roof"})
    try:
        review._parse_classification_json(payload)
    except review.SurfaceAIReplyError as exc:
        assert isinstance(exc, ValueError)
        assert '"roof"' in exc.raw_reply
    else:
        raise AssertionError("an off-vocabulary family must be rejected")
    # The raw reply is bounded so one bad answer cannot bloat the artifact.
    huge = "x" * (review.MAX_ERROR_DETAIL * 3)
    try:
        review._parse_classification_json(huge)
    except review.SurfaceAIReplyError as exc:
        assert len(exc.raw_reply) == review.MAX_ERROR_DETAIL
    else:
        raise AssertionError("invalid JSON must be rejected")


def test_a_string_null_reply_is_read_as_an_abstention():
    # Gemini answered `"ai_surface": "null"` on a span sitting over a roof:
    # "cannot determine" is a legitimate answer, not an invalid family.
    payload = json.dumps({"ai_surface": "null", "confidence": 0.9,
                          "reason": "The centre sits on a building roof."})
    result = review._parse_classification_json(payload)
    assert result["ai_surface"] is None
    assert result["confidence"] == 0.9
    assert review._parse_classification_json(
        json.dumps({"ai_surface": " none ", "confidence": 0.4}))["ai_surface"] is None
    # A real family is still honoured, and a wrong one is still rejected.
    assert review._parse_classification_json(
        json.dumps({"ai_surface": "garden", "confidence": 0.7}))["ai_surface"] == "garden"
    try:
        review._parse_classification_json(
            json.dumps({"ai_surface": "roof", "confidence": 0.7}))
    except review.SurfaceAIReplyError:
        pass
    else:
        raise AssertionError("an off-vocabulary family must still be rejected")


def test_gemini_reply_with_no_candidate_text_is_recorded():
    try:
        review._parse_model_response({"candidates": []})
    except review.SurfaceAIReplyError as exc:
        assert "candidates" in exc.raw_reply
    else:
        raise AssertionError("an empty Gemini payload must be rejected")


def test_review_error_item_keeps_the_rejected_reply(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    def classifier(*_args):
        raise review._reply_error(
            "Vision model returned an unsupported surface family",
            '{"ai_surface": "roof", "confidence": 0.9}')

    result = review.review_uncertain_spans(
        [{"span_id": "TR-001033"}],
        lambda _candidate: {"image_bytes": b"x", "source": "local", "date": None},
        enabled=True, classifier=classifier)
    item = result["suggestions"][0]
    assert item["review_status"] == "error"
    assert item["reason"] == "SurfaceAIReplyError"
    assert '"roof"' in item["error_detail"]


def test_a_failure_without_a_reply_reports_no_detail(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    def classifier(*_args):
        raise RuntimeError("transport died")

    result = review.review_uncertain_spans(
        [{"span_id": "TR-1"}],
        lambda _candidate: {"image_bytes": b"x", "source": "local", "date": None},
        enabled=True, classifier=classifier)
    item = result["suggestions"][0]
    assert item["reason"] == "RuntimeError"
    assert item["error_detail"] is None


def test_point_classify_keeps_the_rejected_reply(monkeypatch):
    monkeypatch.setenv("SURFACE_AI_REVIEW", "1")
    monkeypatch.setenv("SURFACE_AI_PROVIDER", "ollama")

    def classifier(*_args):
        raise review._reply_error("invalid JSON", "not json at all")

    item = review.classify_point(
        [1.5249, 49.0762],
        image_provider=lambda _c: {"image_bytes": b"patch", "mime_type": "image/jpeg",
                                   "source": "IGN BD ORTHO", "date": None},
        classifier=classifier)
    assert item["review_status"] == "error"
    assert item["reason"] == "SurfaceAIReplyError"
    assert item["error_detail"] == "not json at all"


def test_queued_items_carry_a_null_error_detail():
    assert review._not_processed({"span_id": "T-1"})["error_detail"] is None
