import json
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_mikrotik.collect as collect_module
from omini_mikrotik.client import Client

FIXTURES = Path(__file__).parent / "fixtures"


class FakeRouterOS:
    """Answers like a RouterOS 7.15 REST API, from the JSON files in fixtures/
    (``ip__arp.json`` is ``/rest/ip/arp``). Only GETs and the read-only
    ethernet monitor POST are accepted: anything else fails the test."""

    def __init__(self):
        self.routes = {}
        self.forbidden = set()
        self.calls = []
        for f in FIXTURES.glob("*.json"):
            self.routes["/rest/" + f.stem.replace("__", "/")] = json.loads(f.read_text())
        self.auth = ("omini", "secret")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != httpx.BasicAuth(*self.auth)._auth_header:
            return httpx.Response(401, json={"error": 401, "message": "Unauthorized"})
        if request.method == "POST":
            assert path == "/rest/interface/ethernet/monitor"
            body = json.loads(request.content)
            assert "once" in body  # never a monitor that keeps running
            asked = body["numbers"].split(",")
            rows = [r for r in self.routes[path] if r["name"] in asked]
            return httpx.Response(200, json=rows)
        assert request.method == "GET", f"{request.method} {path}"
        if any(path.startswith(p) for p in self.forbidden):
            return httpx.Response(
                403, json={"error": 403, "message": "Forbidden", "detail": "not enough permissions"}
            )
        if path not in self.routes:
            return httpx.Response(
                400,
                json={
                    "error": 400,
                    "message": "Bad Request",
                    "detail": "no such command or directory",
                },
            )
        return httpx.Response(200, json=self.routes[path])


@pytest.fixture
def routeros(monkeypatch):
    fake = FakeRouterOS()
    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def cfg():
    from omini_sdk import Config

    return Config({"url": "https://192.168.88.1", "username": "omini", "password": "secret"})
