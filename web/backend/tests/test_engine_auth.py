"""Mutating engine routes must not be callable by anyone with the hostname.

The engine answers on a public Render URL. Before this, /ftth/hld/run,
/ftth/hld/projects/{id} (DELETE) and the stop/design/review routes accepted a
request from any client — the only thing protecting them was that the hostname
was not written down in public.

These tests exercise the middleware itself with a fake ASGI app: no route runs,
no database is touched, and the assertions are exactly the boundary (401/503 vs
reaching the route).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402

KEY = "test-engine-key"
HEADER = "x-ftth-engine-key"


def _call(method: str, supplied: str | None = None) -> tuple[int, bytes]:
    """Send one request through the middleware; return (status, body)."""
    reached = {"status": 200, "body": b'reached the route'}
    sent: list[dict] = []

    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": reached["status"],
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": reached["body"]})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    middleware = main.EngineKeyMiddleware(inner)
    headers = [(HEADER.encode(), supplied.encode())] if supplied is not None else []
    scope = {
        "type": "http",
        "method": method,
        "path": "/ftth/hld/run",
        "headers": headers,
    }
    asyncio.run(middleware(scope, receive, send))

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(main, "ENGINE_API_KEY", KEY)
    return KEY


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(main, "ENGINE_API_KEY", "")


def test_a_write_without_the_key_never_reaches_the_route(key):
    status, body = _call("POST")
    assert status == 401
    assert b"API key" in body
    assert b"reached the route" not in body


def test_a_write_with_the_key_reaches_the_route(key):
    assert _call("POST", KEY) == (200, b"reached the route")


def test_a_wrong_key_is_rejected(key):
    assert _call("POST", "not-the-key")[0] == 401


def test_every_mutating_method_is_guarded(key):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert _call(method)[0] == 401, method
        assert _call(method, KEY)[0] == 200, method


def test_reads_stay_open_for_the_proxy_and_the_browser(key):
    for method in ("GET", "HEAD", "OPTIONS"):
        assert _call(method)[0] == 200, method


def test_an_unconfigured_key_refuses_writes_instead_of_opening_them(no_key):
    status, body = _call("POST")
    assert status == 503
    assert b"FTTH_ENGINE_API_KEY" in body


def test_the_app_actually_wires_the_middleware():
    assert any(
        m.cls is main.EngineKeyMiddleware for m in main.app.user_middleware
    ), "EngineKeyMiddleware is not installed on the app"


# ── Through the real app, over HTTP semantics ─────────────────────────────
# ASGITransport skips the startup events, so this exercises routing +
# middleware without touching PostGIS.

def _http(method: str, path: str, supplied: str | None = None):
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://engine"
        ) as client:
            headers = {HEADER: supplied} if supplied is not None else {}
            return await client.request(method, path, headers=headers)

    return asyncio.run(go())


def test_a_real_route_refuses_an_anonymous_write(key):
    response = _http("POST", "/ftth/hld/projects/probe/stop")
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing engine API key"


def test_a_real_route_accepts_the_key_and_reaches_routing(key):
    # An unknown path with the key can only be 404 if the middleware let it
    # through; the guard would have answered 401 first.
    response = _http("POST", "/ftth/hld/__probe__", key)
    assert response.status_code == 404


def test_a_real_read_is_still_public(key):
    response = _http("GET", "/")
    assert response.status_code == 200


def test_a_rejection_is_readable_by_a_browser(key):
    """CORS must wrap the guard, so a 401 is not reported as a CORS failure."""
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://engine"
        ) as client:
            return await client.post(
                "/ftth/hld/run", headers={"Origin": "http://localhost:3000"}
            )

    response = asyncio.run(go())
    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == "*"
