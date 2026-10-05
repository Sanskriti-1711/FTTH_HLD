"""Check the concurrent surface review: same artifact, less wall time.

Run with any Python that has pytest's deps out of the way (no osgeo needed):

  python tmp/bench_review_concurrency.py
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from HLDPlanning.design import surface_ai_review as review  # noqa: E402

CALL_S = 0.6  # stands in for imagery download + inference latency


def _candidates(n):
    return [
        {"span_id": f"T-{i}", "claimed": "Asphalt", "coordinates": [[0, 0], [1, 1]],
         "coordinates_crs": "EPSG:4326", "reason": "no_road_evidence",
         "confidence": 0.2, "known_share": 0.1}
        for i in range(n)
    ]


def _provider(candidate):
    return {"image_bytes": b"\xff\xd8fake", "mime_type": "image/jpeg",
            "source": "Esri World Imagery", "date": None}


def _install_fake_classifier():
    state = {"calls": 0}

    def fake(image_bytes, mime_type, api_key, model):
        state["calls"] += 1
        time.sleep(CALL_S)
        return {"ai_surface": "road", "confidence": 0.8, "reason": "looks paved"}

    review._ollama_classify = fake
    return state


def _run(n, concurrency):
    os.environ["SURFACE_AI_REVIEW"] = "1"
    os.environ["SURFACE_AI_PROVIDER"] = "ollama"
    os.environ.pop("SURFACE_AI_MIN_INTERVAL_SECONDS", None)
    os.environ["SURFACE_AI_CONCURRENCY"] = str(concurrency)
    state = _install_fake_classifier()
    start = time.perf_counter()
    report = review.review_uncertain_spans(_candidates(n), image_provider=_provider,
                                           enabled=True)
    elapsed = time.perf_counter() - start
    assert state["calls"] == n, (state["calls"], n)
    return report, elapsed


def main():
    serial, t_serial = _run(6, 1)
    concurrent, t_conc = _run(6, 4)
    print(f"serial={t_serial:.2f}s concurrent={t_conc:.2f}s")
    assert concurrent["status"] == serial["status"] == "ready", concurrent["status"]
    assert len(concurrent["suggestions"]) == len(serial["suggestions"]) == 6
    assert concurrent["deferred_count"] == serial["deferred_count"] == 0
    ok = True
    for a, b in zip(serial["suggestions"], concurrent["suggestions"]):
        for key in ("span_id", "AI_SURFACE", "confidence", "review_status",
                    "imagery_source", "claimed_surface"):
            if a.get(key) != b.get(key):
                ok = False
                print("  DIFF", key, a.get(key), b.get(key))
    print("artifacts identical:", ok)
    print("RESULT", "OK" if ok and t_conc < t_serial * 0.6 else "FAIL")


if __name__ == "__main__":
    main()
