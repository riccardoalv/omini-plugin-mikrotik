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


class Failed(PluginError):
    """RouterOS did not answer in time or answered a server error (5xx): a
    required read fails with it, an optional one is skipped."""


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
        """Runs a console command. A collection only sends read-only ones
        (``/interface/ethernet/monitor``, ``/interface/bridge/monitor`` and
        ``/interface/wifi/monitor`` or ``/interface/wireless/monitor``, with
        ``once``), a backup ``/execute`` of ``/export terse``; a change Omini
        asked for may send ``add`` (with ``place-before``), ``/ip/dns/set`` or
        ``/tool/wol``."""
        return self._send("POST", path, body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        """Sets properties of one item, e.g. ``/interface/ethernet/*1`` with
        ``{"disabled": "true"}`` (the console's ``set``): the properties not
        sent stay as they are. Used only to apply a change Omini asked for."""
        return self._send("PATCH", path, body)

    def put(self, path: str, body: dict[str, Any]) -> Any:
        """Adds an item to a menu (the console's ``add``); RouterOS answers the
        new item with its ``.id``. Used only to apply a change Omini asked for."""
        return self._send("PUT", path, body)

    def delete(self, path: str) -> Any:
        """Removes one item, e.g. ``/ip/address/*3`` (the console's ``remove``).
        Used only to apply a change Omini asked for."""
        return self._send("DELETE", path)

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = self.http.request(method, path, json=body)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}: {e}") from e
        except httpx.TimeoutException as e:
            raise Failed(f"{self.base} did not answer {path} in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed: {e}") from e
        if r.status_code == 401:
            raise PluginError("RouterOS rejected the username or password")
        detail = error_detail(r)
        if r.status_code == 403 or "permission" in detail.lower():
            raise Forbidden(path, detail)
        if r.status_code in (400, 404):
            # 400 is also what RouterOS answers for a menu this version lacks
            # ("no such command or directory").
            raise NotFound(path, detail)
        if r.status_code >= 400:
            raise Failed(f"RouterOS answered {r.status_code} for {path}")
        if not r.content:
            return None
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it a RouterOS v7 address "
                "with the www-ssl service enabled?"
            ) from e


def error_detail(r: httpx.Response) -> str:
    """The ``detail`` (else ``message``) of a RouterOS error answer, or ''."""
    if r.status_code < 400:
        return ""
    try:
        body = r.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("detail") or body.get("message") or "")
