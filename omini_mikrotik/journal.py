"""The writes of one change, kept so they can be undone.

RouterOS applies (and keeps) every write as soon as it is made, and its own
rollback (safe mode) belongs to a console session, which the REST API does
not have. So a change goes through a Journal: every add, set and remove is
noted, and if a later write fails or the device does not read back as asked,
``rollback`` undoes them in reverse order (best effort, each undo on its own).
"""

from __future__ import annotations

from typing import Any

from omini_sdk import PluginError, log

from omini_mikrotik.client import Client, Forbidden, NotFound

WRITE_POLICY = (
    "the RouterOS user's group lacks the write policy: add it "
    "(/user group set <group> policy=read,write,api,rest-api) to let Omini "
    "change the device"
)

# Properties RouterOS reports but does not take back when an item is added.
READ_ONLY = {
    ".id",
    ".nextid",
    "dynamic",
    "invalid",
    "inactive",
    "running",
    "bytes",
    "packets",
    "status",
    "actual-interface",
    "current-tagged",
    "current-untagged",
    "creation-time",
    "dhcp",
    "last-seen",
    "expires-after",
    "active-address",
    "active-mac-address",
    "active-client-id",
    "active-server",
    "host-name",
    "radius",
    "blocked",
}


def text(value: Any) -> str:
    return "" if value is None else str(value)


class Journal:
    def __init__(self, c: Client):
        self.c = c
        self.undo: list[tuple[str, str, Any]] = []

    def _write(self, what: str, fn: Any) -> Any:
        try:
            return fn()
        except Forbidden as e:
            raise PluginError(WRITE_POLICY) from e
        except NotFound as e:
            detail = e.args[1] if len(e.args) > 1 and e.args[1] else "bad request"
            raise PluginError(f"RouterOS refused {what}: {detail}") from e

    def add(
        self, menu: str, body: dict[str, str], what: str, place_before: str | None = None
    ) -> str:
        """Adds an item (PUT), or with place_before runs ``add`` (POST
        <menu>/add) so the item lands before that one; answers its .id."""
        if place_before:
            ret = self._write(
                what, lambda: self.c.post(f"{menu}/add", {**body, "place-before": place_before})
            )
            item = text(ret.get("ret") if isinstance(ret, dict) else "")
        else:
            row = self._write(what, lambda: self.c.put(menu, body))
            item = text(row.get(".id") if isinstance(row, dict) else "")
        if not item:
            raise PluginError(f"RouterOS did not say which item it added for {what}")
        self.undo.append(("remove", menu, item))
        return item

    def set(self, menu: str, row: dict[str, Any], body: dict[str, str], what: str) -> bool:
        """Sets the properties of body that differ from row (PATCH); False
        when nothing differed (nothing is sent)."""
        changed = {k: v for k, v in body.items() if text(row.get(k)) != v}
        if not changed:
            return False
        item = text(row.get(".id"))
        self._write(what, lambda: self.c.patch(f"{menu}/{item}", changed))
        self.undo.append(("set", f"{menu}/{item}", {k: text(row.get(k)) for k in changed}))
        row.update(changed)
        return True

    def set_menu(self, menu: str, current: dict[str, Any], body: dict[str, str], what: str) -> bool:
        """Sets properties of a menu that is one object (/ip/dns): POST <menu>/set."""
        changed = {k: v for k, v in body.items() if text(current.get(k)) != v}
        if not changed:
            return False
        self._write(what, lambda: self.c.post(f"{menu}/set", changed))
        self.undo.append(("set_menu", menu, {k: text(current.get(k)) for k in changed}))
        current.update(changed)
        return True

    def remove(self, menu: str, row: dict[str, Any], what: str) -> None:
        item = text(row.get(".id"))
        self._write(what, lambda: self.c.delete(f"{menu}/{item}"))
        again = {k: text(v) for k, v in row.items() if k not in READ_ONLY and text(v) != ""}
        self.undo.append(("add", menu, again))

    def rollback(self) -> None:
        """Undoes every write, newest first; one that fails is logged and skipped."""
        for op, path, data in reversed(self.undo):
            try:
                if op == "remove":
                    self.c.delete(f"{path}/{data}")
                elif op == "set":
                    self.c.patch(path, data)
                elif op == "set_menu":
                    self.c.post(f"{path}/set", data)
                else:
                    self.c.put(path, data)
            except Exception:
                log.exception("could not undo %s on %s", op, path)
        self.undo = []
