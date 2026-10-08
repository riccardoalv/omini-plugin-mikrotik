"""Minimal RouterOS v7 REST API client (HTTP basic auth).

https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API
Every value in a reply is a string, even numbers and booleans.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from omini_sdk import PluginError


class Forbidden(Exception):
    """The user's group lacks a policy for this menu."""


class NotFound(Exception):
    """The menu does not exist on this device (package or feature missing)."""


class Client:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ):
        url = url.strip().rstrip("/")
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        if url.endswith("/rest"):
            url = url[: -len("/rest")]
        self.base = url
        self.http = httpx.Client(
            base_url=url + "/rest",
            auth=(username, password),
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "omini-plugin-mikrotik"},
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def get(self, path: str) -> Any:
        """Reads a menu, e.g. ``/interface`` (a list) or ``/system/resource`` (one object)."""
        return self._send("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        """Runs a console command. Only read-only commands are ever sent
        (``/interface/ethernet/monitor`` with ``once``)."""
        return self._send("POST", path, body)

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = self.http.request(method, path, json=body)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}: {e}") from e
        except httpx.TimeoutException as e:
            raise PluginError(f"{self.base} did not answer in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed: {e}") from e
        if r.status_code == 401:
            raise PluginError("RouterOS rejected the username or password")
        if r.status_code == 403:
            raise Forbidden(path)
        if r.status_code in (400, 404):
            # 400 is also what RouterOS answers for a menu this version lacks
            # ("no such command or directory").
            raise NotFound(path)
        if r.status_code >= 400:
            raise PluginError(f"RouterOS answered {r.status_code} for {path}")
        if not r.content:
            return None
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it a RouterOS v7 address "
                "with the www-ssl service enabled?"
            ) from e
