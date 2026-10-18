"""Port forwards (Device.port_forwards): what the router exposes to the internet.

A port forward on RouterOS is a ``dstnat`` rule with ``action=dst-nat`` in
/ip/firewall/nat (and /ipv6/firewall/nat, RouterOS 7.1+): ``protocol``,
``dst-port`` (one port, a range "1000-2000" or a list "80,443"),
``in-interface`` or ``in-interface-list``, ``to-addresses`` and ``to-ports``.
The UPnP service (/ip/upnp) opens its mappings as **dynamic** dst-nat rules
in the same table, so a dynamic rule is one a device opened by itself.
Read-only: Omini never changes them.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_sdk import log
from omini_sdk.models import PortForward

from omini_mikrotik.client import Client, Failed, Forbidden, NotFound
from omini_mikrotik.common import as_list, flag

NAT = "/ip/firewall/nat"
NAT6 = "/ipv6/firewall/nat"
PROTOCOLS = {"tcp": "tcp", "6": "tcp", "udp": "udp", "17": "udp"}


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def port_range(value: str) -> tuple[int, int | None] | None:
    """'8080' → (8080, None), '1000-2000' → (1000, 2000); else None."""
    m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", value.strip())
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    if not 1 <= lo <= 65535 or (hi is not None and not lo <= hi <= 65535):
        return None
    return lo, (hi if hi is not None and hi != lo else None)


def first_address(value: str) -> str | None:
    """'192.168.88.10', '192.168.88.10-192.168.88.12' → the first address."""
    v = value.split("-", 1)[0].strip() if value.count(":") < 2 else value.strip()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return None


def source(value: str) -> str | None:
    """A src-address (address or prefix); a negated one ('!10.0.0.0/8') is not a source."""
    if not value or value.startswith("!"):
        return None
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None


def forwards(rows: list[dict[str, Any]]) -> list[PortForward]:
    out: list[PortForward] = []
    for r in rows:
        if text(r.get("chain")) != "dstnat" or text(r.get("action")) != "dst-nat":
            continue
        protocol = PROTOCOLS.get(text(r.get("protocol")).lower())
        if protocol is None:  # a 1:1 NAT or another protocol: no port to report
            continue
        internal = first_address(text(r.get("to-addresses")))
        to_port = port_range(text(r.get("to-ports")).split(",")[0]) if r.get("to-ports") else None
        dynamic = bool(flag(r.get("dynamic")))
        on = not flag(r.get("disabled")) and not flag(r.get("invalid"))
        iface = text(r.get("in-interface")) or text(r.get("in-interface-list")) or None
        for part in text(r.get("dst-port")).split(","):
            ext = port_range(part)
            if ext is None:
                continue
            out.append(
                PortForward(
                    protocol=protocol,
                    external_port=ext[0],
                    external_port_end=ext[1],
                    internal_ip=internal,
                    internal_port=to_port[0] if to_port else None,
                    source=source(text(r.get("src-address"))),
                    interface=iface if iface and not iface.startswith("!") else None,
                    description=text(r.get("comment")) or None,
                    enabled=on,
                    upnp=dynamic,
                )
            )
    return out


def port_forwards(c: Client) -> list[PortForward]:
    """The IPv4 table (required for this read) and the IPv6 one when RouterOS has it."""
    rows = as_list(c.get(NAT))
    out = forwards(rows)
    try:
        out += forwards(as_list(c.get(NAT6)))
    except (Forbidden, NotFound, Failed) as e:  # older RouterOS, IPv6 package off
        log.info("no IPv6 NAT table: %s", e)
    return out
