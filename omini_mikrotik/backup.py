"""The device's configuration as RouterOS's own text export (``/export terse``),
for Omini's configuration history (never restored).

The REST API has no ``/export`` menu that answers text, so the export runs as
a script through ``POST /rest/execute`` with ``as-string``, which waits for
the script and answers its output as ``{"ret": "<text>"}``. RouterOS 7
leaves sensitive values (passwords, keys, secrets) out of an export unless
``show-sensitive`` is given, and the plugin never gives it. The first line
(``# 2024-11-29 14:17:45 by RouterOS 7.16.2``, the time of the export) is
dropped, so two exports of an unchanged configuration are equal. The content
is never logged.
"""

from __future__ import annotations

import re
from typing import Any

from omini_sdk import PluginError, log

from omini_mikrotik.client import Client, Forbidden, NotFound

SCRIPT = "/export terse"
# Flag values RouterOS may take for as-string ("" as for `once`, else "true").
AS_STRING = ("", "true")
STAMP = re.compile(r"^#\s.*\d{1,2}:\d{2}:\d{2}\s+by RouterOS\b.*$")

EXPORT_POLICY = (
    "the RouterOS user's group cannot export the configuration through /execute: "
    "give it the read, write, api and rest-api policies "
    "(/user group set <group> policy=read,write,api,rest-api)"
)


def output(ret: Any) -> str | None:
    """The text of an /execute answer: {"ret": "..."}, [{"ret": "..."}] or a string."""
    if isinstance(ret, list) and ret and isinstance(ret[0], dict):
        ret = ret[0]
    if isinstance(ret, dict):
        ret = ret.get("ret")
    return ret if isinstance(ret, str) else None


def export(c: Client) -> str:
    """Runs /export terse and answers its text, deterministic."""
    forbidden = False
    for flag in AS_STRING:
        try:
            text = output(c.post("/execute", {"script": SCRIPT, "as-string": flag}))
        except Forbidden:
            forbidden = True
            continue
        except NotFound:
            continue
        if text and text.strip():
            return clean(text)
    if forbidden:
        raise PluginError(EXPORT_POLICY)
    log.warning("RouterOS answered no text for /export through /execute")
    raise PluginError("RouterOS did not answer the configuration export (/export terse)")


def clean(text: str) -> str:
    """Drops the export's time stamp (its first comment line), trailing spaces
    and blank lines at the end; LF line ends."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    header = True
    for line in lines:
        if header and line.startswith("#"):
            if STAMP.match(line):
                continue
        else:
            header = False
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out) + "\n"
