"""RouterOS v7 REST API client (HTTP basic auth), on the SDK's HTTP client:
timeouts, retries when the router refuses a connection or is busy, and the
SDK's errors (``Unauthorized``, ``Forbidden``, ``NotFound``, ``Unavailable``).

https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API
Every value in a reply is a string, even numbers and booleans.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from omini_sdk import PluginError
from omini_sdk import http as sdk_http
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import Forbidden, HttpError, NotFound, Unauthorized, Unavailable

__all__ = ["Client", "Forbidden", "HttpError", "NotFound", "Unavailable", "detail_of"]


class Client(HttpClient):
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(
            url,
            name="RouterOS",
            api="/rest",
            strip=("/rest",),
            auth=(username, password),
            verify_tls=verify_tls,
            timeout=timeout,
            user_agent="omini-plugin-mikrotik",
            unauthorized="RouterOS rejected the username or password",
            transport=transport,
            sleep=sleep,
        )

    def get(self, path: str) -> Any:  # type: ignore[override]
        """Reads a menu, e.g. ``/interface`` (a list) or ``/system/resource`` (one object)."""
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:  # type: ignore[override]
        """Runs a console command. A collection only sends read-only ones
        (``/interface/ethernet/monitor``, ``/interface/bridge/monitor`` and
        ``/interface/wifi/monitor`` or ``/interface/wireless/monitor``, with
        ``once``), a backup ``/execute`` of ``/export terse``; a change Omini
        asked for may send ``add`` (with ``place-before``), ``/ip/dns/set`` or
        ``/tool/wol``."""
        return self.request("POST", path, json=body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:  # type: ignore[override]
        """Sets properties of one item, e.g. ``/interface/ethernet/*1`` with
        ``{"disabled": "true"}`` (the console's ``set``): the properties not
        sent stay as they are. Used only to apply a change Omini asked for."""
        return self.request("PATCH", path, json=body)

    def put(self, path: str, body: dict[str, Any]) -> Any:  # type: ignore[override]
        """Adds an item to a menu (the console's ``add``); RouterOS answers the
        new item with its ``.id``. Used only to apply a change Omini asked for."""
        return self.request("PUT", path, json=body)

    def delete(self, path: str) -> Any:  # type: ignore[override]
        """Removes one item, e.g. ``/ip/address/*3`` (the console's ``remove``).
        Used only to apply a change Omini asked for."""
        return self.request("DELETE", path)

    def send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        # RouterOS's PUT is the console's ``add``: one that timed out may
        # have added the item, so (like a POST) it is never sent again.
        idempotent = sdk_http.IDEMPOTENT
        if method.upper() == "PUT":
            sdk_http.IDEMPOTENT = idempotent - {"PUT"}
        try:
            return super().send(method, path, **kw)
        except Unavailable as e:
            if isinstance(e.__cause__, httpx.TimeoutException):
                raise Unavailable(
                    f"{self.base} did not answer {path} in time", method=method, resource=path
                ) from e.__cause__
            raise
        finally:
            sdk_http.IDEMPOTENT = idempotent

    def detail(self, r: httpx.Response) -> str:
        return error_detail(r) or super().detail(r)

    def check(self, r: httpx.Response, method: str, path: str) -> None:
        status = r.status_code
        if status < 400:
            return
        detail = error_detail(r)
        where = {"status": status, "method": method, "resource": path}
        if status == 401:
            raise Unauthorized(self.unauthorized, **where)
        if status == 403 or "permission" in detail.lower():
            e: HttpError = Forbidden(
                f"the RouterOS user may not use {path}" + (f" ({detail})" if detail else ""),
                **where,
            )
        elif status in (400, 404, 410):
            # 400 is also what RouterOS answers for a menu this version lacks
            # ("no such command or directory"), and for a refused write.
            e = NotFound(
                f"RouterOS has no {path}"
                if method == "GET"
                else f"RouterOS refused {method} {path}: {detail or 'bad request'}",
                **where,
            )
        else:
            super().check(r, method, path)
            return
        e.detail = detail  # type: ignore[attr-defined]
        raise e

    def json(self, r: httpx.Response, path: str = "") -> Any:
        if not r.content:
            return None
        try:
            return r.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it a RouterOS v7 address "
                "with the www-ssl service enabled?"
            ) from e


def detail_of(e: BaseException, default: str = "bad request") -> str:
    """RouterOS's own words for a refused request (its ``detail``)."""
    return getattr(e, "detail", "") or default


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
