import ipaddress
import itertools
import json
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_mikrotik.collect as collect_module
from omini_mikrotik.client import Client

FIXTURES = Path(__file__).parent / "fixtures"

# Menus a change may add to, set and remove from (anything else fails the test).
WRITABLE = {
    "/rest/interface/bridge/port",
    "/rest/interface/bridge/vlan",
    "/rest/interface/vlan",
    "/rest/ip/address",
    "/rest/ip/pool",
    "/rest/ip/dhcp-server",
    "/rest/ip/dhcp-server/network",
    "/rest/ip/dhcp-server/lease",
    "/rest/ip/firewall/filter",
    "/rest/ip/firewall/address-list",
    "/rest/ip/dns/static",
    "/rest/ip/firewall/connection",
    "/rest/ipv6/firewall/filter",
    "/rest/ipv6/firewall/address-list",
    "/rest/ipv6/firewall/connection",
    "/rest/interface/wifi",
    "/rest/interface/wireless",
}

FORBIDDEN = {"error": 403, "message": "Forbidden", "detail": "not enough permissions (9)"}


class FakeRouterOS:
    """Answers like a RouterOS 7.16 REST API, from the JSON files in fixtures/
    (``ip__arp.json`` is ``/rest/ip/arp``), and keeps what a change writes:
    PUT (add), POST <menu>/add (add with place-before), PATCH (set) and DELETE
    (remove) on the menus a change may touch, POST /ip/dns/set, and PATCHes
    of a port's ``disabled`` property. The read-only ethernet monitor POST is
    the only other write-looking request accepted: anything else fails the test."""

    def __init__(self):
        self.routes = {}
        self.forbidden = set()
        self.calls = []
        self.patches = []  # (path, body) of every PATCH taken
        self.writes = []  # (method, path, body) of every write taken
        self.read_only = False  # the user's group has no write policy
        self.ignore_changes = False  # answers 200 to a write but changes nothing
        self.ignore_fields = set()  # properties a write answers 200 to but does not keep
        self.fail = {}  # "METHOD /rest/path" → the detail of a 400 answer
        self.fail_get = set()  # paths whose GET answers 500
        self.slow_get = set()  # paths whose GET times out
        self.ids = itertools.count(0x100)
        self.wol = []  # bodies of POST /tool/wol
        self.export = (FIXTURES / "export.rsc").read_text()
        self.executes = []  # bodies of POST /execute
        self.execute_answers = {}  # as-string value → (status, body) instead of the export
        for f in FIXTURES.glob("*.json"):
            self.routes["/rest/" + f.stem.replace("__", "/")] = json.loads(f.read_text())
        self.auth = ("omini", "secret")
        self.radio_stuck = False  # a radio whose monitor keeps its old channel

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if request.headers.get("authorization") != httpx.BasicAuth(*self.auth)._auth_header:
            return httpx.Response(401, json={"error": 401, "message": "Unauthorized"})
        body = json.loads(request.content) if request.content else None
        if method == "POST" and path == "/rest/interface/ethernet/monitor":
            assert "once" in body  # never a monitor that keeps running
            asked = body["numbers"].split(",")
            rows = [r for r in self.routes[path] if r["name"] in asked]
            return httpx.Response(200, json=rows)
        if method == "POST" and path == "/rest/interface/bridge/monitor":
            assert "once" in body  # read-only: never a monitor that keeps running
            rows = [r for r in self.routes[path] if r["name"] == body["numbers"]]
            return httpx.Response(200, json=rows)
        if method == "POST" and path in (
            "/rest/interface/wifi/monitor",
            "/rest/interface/wireless/monitor",
        ):
            assert "once" in body  # read-only: never a monitor that keeps running
            return httpx.Response(200, json=self.radio_monitor(path, body["numbers"]))
        if method == "POST" and path == "/rest/execute":
            # Only the export: a read, with the user's policies (sensitive values left out).
            assert body["script"] == "/export terse", body
            assert "show-sensitive" not in body["script"]
            self.executes.append(body)
            if any(path.startswith(p) for p in self.forbidden):
                return httpx.Response(403, json=FORBIDDEN)
            status, answer = self.execute_answers.get(
                body.get("as-string"), (200, {"ret": self.export})
            )
            return httpx.Response(status, json=answer)
        if method == "GET":
            if any(path.startswith(p) for p in self.forbidden):
                return httpx.Response(403, json=FORBIDDEN)
            if path in self.slow_get:
                raise httpx.ReadTimeout("timed out", request=request)
            if path in self.fail_get:
                return httpx.Response(500, json={"error": 500, "message": "Internal Error"})
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
        return self.write(method, path, body)

    # --- writes --------------------------------------------------------------

    def write(self, method: str, path: str, body: dict | None) -> httpx.Response:
        if body is not None:
            assert all(isinstance(v, str) for v in body.values()), body  # RouterOS takes strings
        if self.read_only:
            return httpx.Response(403, json=FORBIDDEN)
        detail = self.fail.get(f"{method} {path}")
        if detail:
            return httpx.Response(
                400, json={"error": 400, "message": "Bad Request", "detail": detail}
            )
        if method == "PATCH" and path.rpartition("/")[0] == "/rest/interface/ethernet":
            assert set(body) == {"disabled"}, body  # nothing else of the port is touched
        self.writes.append((method, path, body))
        if method == "PATCH":
            self.patches.append((path, body))
        if method == "POST" and path == "/rest/tool/wol":
            assert set(body) == {"mac", "interface"}, body
            self.wol.append(body)
            return httpx.Response(200, json=[])
        if method == "POST" and path == "/rest/ip/dns/set":
            if not self.ignore_changes:
                self.routes["/rest/ip/dns"].update(body)
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/add"):
            menu = path[: -len("/add")]
            assert menu in WRITABLE, path
            item = self.add(menu, body)
            return httpx.Response(200, json={"ret": item})
        if method == "PUT":
            assert path in WRITABLE, path
            assert "place-before" not in body
            item = self.add(path, body)
            row = next((r for r in self.routes.get(path, []) if r[".id"] == item), {".id": item})
            return httpx.Response(201, json=row)
        menu, _, item = path.rpartition("/")
        assert menu in WRITABLE or menu == "/rest/interface/ethernet", path
        rows = self.routes.setdefault(menu, [])
        row = next((r for r in rows if r[".id"] == item), None)
        if row is None:
            return httpx.Response(404, json={"error": 404, "message": "Not Found"})
        if method == "DELETE":
            if not self.ignore_changes:
                rows.remove(row)
                if menu == "/rest/interface/vlan":
                    self.routes["/rest/interface"] = [
                        r for r in self.routes["/rest/interface"] if r.get("name") != row["name"]
                    ]
            return httpx.Response(204)
        assert method == "PATCH", f"{method} {path}"
        if not self.ignore_changes:
            row.update({k: v for k, v in body.items() if k not in self.ignore_fields})
            self.derive(menu, row)
            if menu == "/rest/interface/ethernet":
                for r in self.routes["/rest/interface"]:
                    if r.get("name") == row["name"]:
                        r.update(body)
                        if body["disabled"] == "true":
                            r["running"] = "false"
        return httpx.Response(200, json=row)

    def add(self, menu: str, body: dict) -> str:
        item = f"*{next(self.ids):X}"
        if self.ignore_changes:
            return item
        body = {k: v for k, v in body.items() if k not in self.ignore_fields}
        before = body.pop("place-before", None)
        row = {".id": item, "disabled": "false", **body}
        if menu in (
            "/rest/ip/firewall/filter",
            "/rest/ipv6/firewall/filter",
            "/rest/interface/bridge/vlan",
        ):
            row.setdefault("dynamic", "false")
            row.setdefault("invalid", "false")
        if menu == "/rest/ip/dhcp-server/lease":
            row.update({"dynamic": "false", "status": "waiting"})
        if menu == "/rest/ip/address":
            net = ipaddress.ip_interface(body["address"]).network
            row.update(
                {
                    "network": str(net.network_address),
                    "actual-interface": body["interface"],
                    "dynamic": "false",
                    "invalid": "false",
                }
            )
        if menu == "/rest/interface/wifi" and "name" not in row:
            row["name"] = f"wifi{len(self.routes.get(menu, [])) + 1}"  # RouterOS names it
        if menu == "/rest/interface/vlan":
            self.routes["/rest/interface"].append(
                {".id": item, "name": body["name"], "type": "vlan", "running": "true"}
            )
        self.derive(menu, row)
        rows = self.routes.setdefault(menu, [])
        at = next((k for k, r in enumerate(rows) if r[".id"] == before), None) if before else None
        assert before is None or at is not None, f"no {before} to place before"
        rows.insert(len(rows) if at is None else at, row)
        return item

    def radio_monitor(self, path: str, name: str) -> list[dict]:
        """The fixture's monitor row, on the radio's fixed frequency once it has one."""
        if path not in self.routes:
            return []
        rows = [dict(r) for r in self.routes[path] if r["name"] == name]
        menu = path.removesuffix("/monitor")
        key = "frequency" if menu.endswith("wireless") else "channel.frequency"
        config = next((r for r in self.routes.get(menu, []) if r.get("name") == name), {})
        fixed = config.get(key, "")
        for r in rows:
            if fixed.isdigit() and not self.radio_stuck:
                r["channel"] = fixed + r.get("channel", "")[4:]
        return rows

    @staticmethod
    def derive(menu: str, row: dict) -> None:
        if menu == "/rest/interface/bridge/vlan":
            row["current-tagged"] = row.get("tagged", "")
            row["current-untagged"] = row.get("untagged", "")

    # --- helpers for the tests ------------------------------------------------

    def rows(self, menu: str) -> list[dict]:
        return self.routes["/rest" + menu]

    def row(self, menu: str, **match) -> dict | None:
        return next(
            (r for r in self.rows(menu) if all(r.get(k) == v for k, v in match.items())), None
        )


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
