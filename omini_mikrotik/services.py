"""Local DNS names, Wake-on-LAN and pausing a device's internet on RouterOS:
dns.set, dns.delete, wol.send, pause.set and pause.delete, and what a
collection reports of them (Device.dns_records, dns_domain, paused).

- DNS: /ip/dns/static A records (type A, or no type: RouterOS's default).
  Omini's records carry the comment "Omini". The local domain is the
  ``domain`` the DHCP server hands out (/ip/dhcp-server/network): the LAN's,
  else the first network's that has one.
- Wake-on-LAN: the console's /tool/wol (``POST /tool/wol`` with mac and
  interface), on the interface whose address's network holds the device's IP.
- Pause: the firewall address list ``omini_paused`` (one entry per address,
  comment "Omini <mac> <name>") and one forward filter rule dropping
  "src-address-list=omini_paused dst-address-list=!omini-private" (the
  private networks list networks.py keeps), placed before the first rule of
  the forward chain so that established connections are dropped too. Only on
  a router: a device with a default route and a LAN-side network (see
  networks.py), so not a switch whose default route is its management VLAN.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_sdk import DnsRecord, PausedDevice, PluginError, log

from omini_mikrotik.client import Client, Forbidden, NotFound
from omini_mikrotik.common import as_list, flag
from omini_mikrotik.journal import Journal
from omini_mikrotik.networks import (
    ADDRESS_LISTS,
    FILTER,
    PRIVATE_LIST,
    State,
    ensure_private_list,
    nets,
    norm_mac,
    on,
    text,
)

DNS_STATIC = "/ip/dns/static"
WOL = "/tool/wol"
CONNECTIONS = "/ip/firewall/connection"
MARK = "Omini"
PAUSED_LIST = "omini_paused"
PAUSE_RULE = {
    "chain": "forward",
    "action": "drop",
    "src-address-list": PAUSED_LIST,
    "dst-address-list": "!" + PRIVATE_LIST,
}
MAX_CUTS = 2000  # connections removed per pause, at most

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)


# --- DNS -----------------------------------------------------------------------


def fqdn(value: Any) -> str:
    return text(value).lower().rstrip(".")


def is_a(r: dict[str, Any]) -> bool:
    """An A record by name (not a regexp, CNAME, forward or other type)."""
    kind = text(r.get("type")).upper()
    return kind in ("", "A") and bool(text(r.get("name"))) and not text(r.get("regexp"))


def read_static(c: Client) -> list[dict[str, Any]]:
    try:
        return as_list(c.get(DNS_STATIC))
    except Forbidden as e:
        raise PluginError("the RouterOS user cannot read /ip/dns/static") from e
    except NotFound as e:
        raise PluginError("this device has no /ip/dns/static menu") from e


def records(rows: list[dict[str, Any]]) -> list[DnsRecord]:
    out = []
    for r in rows:
        if not is_a(r) or not on(r):
            continue
        try:
            ip = ipaddress.IPv4Address(text(r.get("address")))
        except ValueError:
            continue
        out.append(DnsRecord(name=fqdn(r.get("name")), ip=str(ip)))
    return sorted(out, key=lambda d: (d.name, ipaddress.IPv4Address(d.ip)))


def dns_records(c: Client) -> list[DnsRecord]:
    """Every local name the router answers (enabled A records)."""
    return records(as_list(c.get(DNS_STATIC)))


def dns_domain(st: State) -> str | None:
    """The domain the DHCP server hands out: the LAN's network's, else the first one's."""
    with_domain = [d for d in st.dhcp_networks if fqdn(d.get("domain"))]
    if not with_domain:
        return None
    lan = next((n for n in nets(st) if n.vlan == 1), None)
    if lan is not None:
        net = ipaddress.ip_interface(lan.gateway).network
        for d in with_domain:
            try:
                if ipaddress.ip_network(text(d.get("address")), strict=False) == net:
                    return fqdn(d.get("domain"))
            except ValueError:
                continue
    return fqdn(with_domain[0].get("domain"))


def check_name(value: Any) -> str:
    name = fqdn(value)
    if not HOSTNAME.match(name):
        raise PluginError(f"{value!r} is not a DNS name")
    return name


def check_ip(value: Any) -> str:
    try:
        return str(ipaddress.IPv4Address(text(value)))
    except ValueError as e:
        raise PluginError(f"{value!r} is not an IPv4 address") from e


def named(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in rows if is_a(r) and fqdn(r.get("name")) == name]


def dns_set(c: Client, j: Journal, value: dict[str, Any]) -> None:
    """Creates the A record, or changes the address of the one with that name
    (other A records of the same name are removed: one name, one address)."""
    name, ip = check_name(value.get("name")), check_ip(value.get("ip"))
    same = named(read_static(c), name)
    if same:
        j.set(DNS_STATIC, same[0], {"address": ip, "disabled": "false"}, f"the DNS name {name}")
        for r in same[1:]:
            j.remove(DNS_STATIC, r, f"the DNS name {name}")
    else:
        j.add(DNS_STATIC, {"name": name, "address": ip, "comment": MARK}, f"the DNS name {name}")
    have = [d.ip for d in records(read_static(c)) if d.name == name]
    if have != [ip]:
        raise PluginError(f"RouterOS did not apply the DNS name {name}")


def dns_delete(c: Client, j: Journal, value: dict[str, Any]) -> None:
    name = check_name(value.get("name"))
    for r in named(read_static(c), name):
        j.remove(DNS_STATIC, r, f"the DNS name {name}")
    if named(read_static(c), name):
        raise PluginError(f"RouterOS did not remove the DNS name {name}")


# --- Wake-on-LAN -------------------------------------------------------------------


def interface_for(st: State, ip: str | None) -> str:
    """The interface whose address's network holds ip; without ip, the LAN."""
    if not ip:
        lan = next((n for n in nets(st) if n.vlan == 1), None)
        if lan is None:
            raise PluginError("the router has no LAN: say which address the device had")
        return lan.interface
    want = ipaddress.IPv4Address(check_ip(ip))
    found = []
    for a in st.addresses:
        if not on(a):
            continue
        try:
            addr = ipaddress.ip_interface(text(a.get("address")))
        except ValueError:
            continue
        if addr.version == 4 and want in addr.network and text(a.get("interface")):
            found.append((addr.network.prefixlen, text(a.get("interface"))))
    if not found:
        raise PluginError(f"the router has no network that holds {want}")
    return max(found)[1]  # the most specific network


def wol_send(c: Client, st: State, value: dict[str, Any]) -> None:
    m = norm_mac(value.get("mac"))
    if not m:
        raise PluginError(f"{value.get('mac')!r} is not a MAC address")
    iface = interface_for(st, text(value.get("ip")) or None)
    try:
        c.post(WOL, {"mac": m.upper(), "interface": iface})
    except Forbidden as e:
        raise PluginError(
            "the RouterOS user's group cannot run /tool/wol: give it the write policy "
            "(/user group set <group> policy=read,write,api,rest-api)"
        ) from e
    except NotFound as e:
        detail = e.args[1] if len(e.args) > 1 and e.args[1] else "bad request"
        raise PluginError(f"RouterOS refused Wake-on-LAN on {iface}: {detail}") from e


# --- pause -------------------------------------------------------------------------


def comment_of(m: str, name: str) -> str:
    return f"{MARK} {m} {name}".strip()


def parse_comment(value: Any) -> tuple[str, str] | None:
    """'Omini aa:bb:cc:dd:ee:ff Living room TV' → (mac, name)."""
    parts = text(value).split(" ", 2)
    if len(parts) < 2 or parts[0] != MARK:
        return None
    m = norm_mac(parts[1])
    return (m, parts[2].strip() if len(parts) > 2 else "") if m else None


def entries(st: State) -> list[dict[str, Any]]:
    return [
        a for a in st.address_lists if a.get("list") == PAUSED_LIST and not flag(a.get("dynamic"))
    ]


def entries_of(st: State, m: str) -> list[dict[str, Any]]:
    return [a for a in entries(st) if (parse_comment(a.get("comment")) or ("",))[0] == m]


def paused(st: State) -> list[PausedDevice]:
    """The devices in omini_paused (enabled entries), by the MAC in each entry's comment."""
    if not nets(st):
        return []
    by_mac: dict[str, tuple[list[str], str]] = {}
    for a in entries(st):
        parsed = parse_comment(a.get("comment"))
        ip = text(a.get("address"))
        if parsed is None or not ip or flag(a.get("disabled")):
            continue
        m, name = parsed
        ips, _ = by_mac.setdefault(m, ([], name))
        if ip not in ips:
            ips.append(ip)
    return [
        PausedDevice(mac=m, ips=sorted(ips, key=sort_ip), name=name or None)
        for m, (ips, name) in sorted(by_mac.items())
    ]


def sort_ip(ip: str) -> tuple[int, str]:
    try:
        return int(ipaddress.ip_address(ip.split("/")[0])), ip
    except ValueError:
        return 0, ip


def pause_rules(st: State) -> list[dict[str, Any]]:
    return [
        r
        for r in st.rules or []
        if not flag(r.get("dynamic")) and all(text(r.get(k)) == v for k, v in PAUSE_RULE.items())
    ]


def first_forward(st: State) -> str | None:
    """The first static rule of the forward chain: the pause rule goes before
    it, so it also drops what the accept (and fasttrack) rules let through."""
    for r in st.rules or []:
        if r.get("chain") == "forward" and not flag(r.get("dynamic")):
            return text(r.get(".id"))
    return None


def router_only(st: State) -> None:
    if not nets(st):
        raise PluginError(
            "this device does not route (no default route, or no network besides the one "
            "it is reached through): it cannot pause a device"
        )
    if st.rules is None:
        raise PluginError("the RouterOS user cannot read /ip/firewall/filter")


def own_addresses(st: State) -> set[str]:
    out = set()
    for a in st.addresses:
        try:
            out.add(str(ipaddress.ip_interface(text(a.get("address"))).ip))
        except ValueError:
            continue
    return out


def pause_set(j: Journal, st: State, value: dict[str, Any]) -> list[str]:
    """Puts the device's addresses in omini_paused (replacing the ones it had)
    and makes sure the rule is there. Answers the addresses."""
    router_only(st)
    m = norm_mac(value.get("mac"))
    if not m:
        raise PluginError(f"{value.get('mac')!r} is not a MAC address")
    ips = sorted({check_ip(ip) for ip in value.get("ips") or []}, key=sort_ip)
    if not ips:
        raise PluginError(f"{m} has no address to pause")
    mine = own_addresses(st) & set(ips)
    if mine:
        raise PluginError(f"{', '.join(sorted(mine))} is the router's own address")
    name = text(value.get("name"))
    comment = comment_of(m, name)
    have = entries_of(st, m)
    for a in have:
        if text(a.get("address")) not in ips:
            j.remove(ADDRESS_LISTS, a, f"the paused addresses of {m}")
    kept = {text(a.get("address")) for a in have if text(a.get("address")) in ips}
    for a in have:
        if text(a.get("address")) in ips:
            j.set(ADDRESS_LISTS, a, {"comment": comment, "disabled": "false"}, f"the pause of {m}")
    for a in entries(st):
        # An address another entry holds (the device got another one's old IP).
        if all(a is not h for h in have) and text(a.get("address")) in ips:
            j.remove(ADDRESS_LISTS, a, f"the paused address {a.get('address')}")
    for ip in ips:
        if ip not in kept:
            j.add(
                ADDRESS_LISTS,
                {"list": PAUSED_LIST, "address": ip, "comment": comment},
                f"the pause of {m}",
            )
    ensure_private_list(j, st)
    rules = pause_rules(st)
    if not rules:
        j.add(
            FILTER,
            {**PAUSE_RULE, "comment": f"{MARK}: paused devices"},
            "the rule of paused devices",
            place_before=first_forward(st),
        )
    elif not any(on(r) for r in rules):
        j.set(FILTER, rules[0], {"disabled": "false"}, "the rule of paused devices")
    return ips


def pause_delete(j: Journal, st: State, value: dict[str, Any]) -> None:
    """Takes the device's addresses out of omini_paused; the rule goes with the
    last entry."""
    router_only(st)
    m = norm_mac(value.get("mac"))
    if not m:
        raise PluginError(f"{value.get('mac')!r} is not a MAC address")
    have = entries_of(st, m)
    for a in have:
        j.remove(ADDRESS_LISTS, a, f"the pause of {m}")
    if all(any(a is h for h in have) for a in entries(st)):
        for r in pause_rules(st):
            j.remove(FILTER, r, "the rule of paused devices")


def pause_matches(st: State, value: dict[str, Any], ips: list[str]) -> bool:
    m = norm_mac(value.get("mac"))
    got = next((p for p in paused(st) if p.mac == m), None)
    return (
        got is not None and set(got.ips or []) == set(ips) and any(on(r) for r in pause_rules(st))
    )


def pause_gone(st: State, value: dict[str, Any]) -> bool:
    m = norm_mac(value.get("mac"))
    if any(p.mac == m for p in paused(st)):
        return False
    return bool(entries(st)) or not pause_rules(st)


def cut_connections(c: Client, ips: list[str]) -> int:
    """Removes the connections from these addresses (best effort: the router
    keeps working either way, and the rule drops the next packets). Answers how
    many were removed."""
    want = set(ips)
    cut = 0
    try:
        rows = as_list(c.get(f"{CONNECTIONS}?.proplist=.id,src-address"))
    except Exception:
        log.warning("could not read the firewall connections to cut them")
        return 0
    for r in rows:
        src = text(r.get("src-address"))  # '192.168.88.10:51234' (no port for ICMP)
        if src.count(":") == 1:
            src = src.split(":")[0]
        if src not in want or not text(r.get(".id")):
            continue
        try:
            c.delete(f"{CONNECTIONS}/{text(r.get('.id'))}")
            cut += 1
        except (Forbidden, NotFound):
            continue  # gone already, or not allowed: the rule still drops what is new
        except Exception:
            log.warning("could not remove a connection of a paused device")
            break
        if cut >= MAX_CUTS:
            break
    return cut
