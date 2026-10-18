"""Local DNS names, Wake-on-LAN and pausing a device's internet on RouterOS:
dns.set, dns.delete, wol.send, pause.set and pause.delete, and what a
collection reports of them (Device.dns_records, dns_domain, paused).

- DNS: /ip/dns/static A records (type A, or no type: RouterOS's default).
  Omini's records carry the comment "Omini". The local domain is the
  ``domain`` the DHCP server hands out (/ip/dhcp-server/network): the LAN's,
  else the first network's that has one.
- Wake-on-LAN: the console's /tool/wol (``POST /tool/wol`` with mac and
  interface), on the interface whose address's network holds the device's IP.
- Pause: by the device's MAC, in both families, and by its IPv4 addresses.
  The firewall address list ``omini_paused`` (one entry per IPv4 address,
  comment "Omini <mac> <name>") is how a collection knows who is paused, and
  one shared forward rule drops "src-address-list=omini_paused
  dst-address-list=!omini-private" (the private networks list networks.py
  keeps). Each paused device also gets one forward rule per family dropping
  "src-mac-address=<MAC> dst-address-list=!omini-private" (/ip/firewall/filter)
  and "src-mac-address=<MAC> dst-address-list=!omini-private6"
  (/ipv6/firewall/filter), comment "Omini <mac> <name>": the MAC covers the
  addresses Omini does not know (IPv6 temporary addresses, a new IPv4).
  ``omini-private6`` (/ipv6/firewall/address-list) holds link-local
  fe80::/10, ULA fc00::/7, multicast ff00::/8 and the router's LAN-side IPv6
  prefixes (kept up to date on every pause). Every rule goes before the first
  rule of the forward chain so that established connections are dropped too.
  src-mac-address matches the frame's source MAC, which a router sees for
  devices on its own L2 networks (Ethernet, bridge, Wi-Fi); the IPv4 list
  rule still covers a device behind another hop. Only on a router: a device
  with a default route and a LAN-side network (see networks.py), so not a
  switch whose default route is its management VLAN.
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
CONNECTIONS6 = "/ipv6/firewall/connection"
FILTER6 = "/ipv6/firewall/filter"
ADDRESS_LISTS6 = "/ipv6/firewall/address-list"
ADDRESSES6 = "/ipv6/address"
NEIGHBORS6 = "/ipv6/neighbor"
PRIVATE6_LIST = "omini-private6"
LOCAL6_RANGES = ("fe80::/10", "fc00::/7", "ff00::/8")
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
    """The enabled A records (what dns.set and dns.delete manage)."""
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


def is_aaaa(r: dict[str, Any]) -> bool:
    """An AAAA record by name: Omini reads it but never changes or removes it
    (the names Omini sets stay A records)."""
    return (
        text(r.get("type")).upper() == "AAAA"
        and bool(text(r.get("name")))
        and not text(r.get("regexp"))
    )


def records6(rows: list[dict[str, Any]]) -> list[DnsRecord]:
    out = []
    for r in rows:
        if not is_aaaa(r) or not on(r):
            continue
        try:
            ip = ipaddress.IPv6Address(text(r.get("address")))
        except ValueError:
            continue
        out.append(DnsRecord(name=fqdn(r.get("name")), ip=str(ip)))
    return out


def dns_records(c: Client) -> list[DnsRecord]:
    """Every local name the router answers: enabled A and AAAA records."""
    rows = as_list(c.get(DNS_STATIC))
    both = records(rows) + records6(rows)
    return sorted(
        both, key=lambda d: (d.name, ipaddress.ip_address(d.ip).version, ipaddress.ip_address(d.ip))
    )


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
    """The shared IPv4 rule of omini_paused."""
    return [
        r
        for r in st.rules or []
        if not flag(r.get("dynamic")) and all(text(r.get(k)) == v for k, v in PAUSE_RULE.items())
    ]


def mac_rule(m: str, private: str) -> dict[str, str]:
    return {
        "chain": "forward",
        "action": "drop",
        "src-mac-address": m.upper(),
        "dst-address-list": "!" + private,
    }


def mac_rules(rules: list[dict[str, Any]] | None, m: str | None = None) -> list[dict[str, Any]]:
    """Omini's per-device rules (src-mac-address, comment 'Omini <mac> ...') of
    one family's filter; of one MAC when m is given."""
    out = []
    for r in rules or []:
        parsed = parse_comment(r.get("comment"))
        if (
            parsed is None
            or flag(r.get("dynamic"))
            or r.get("chain") != "forward"
            or text(r.get("action")) != "drop"
            or norm_mac(r.get("src-mac-address")) != parsed[0]
        ):
            continue
        if m is None or parsed[0] == m:
            out.append(r)
    return out


def first_forward(rules: list[dict[str, Any]] | None) -> str | None:
    """The first static rule of the forward chain: the pause rules go before
    it, so they also drop what the accept (and fasttrack) rules let through."""
    for r in rules or []:
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


def own_macs(st: State) -> set[str]:
    return {norm_mac(i.get("mac-address")) for i in st.interfaces.values()} - {""}


# --- pause: IPv6 --------------------------------------------------------------------


def read_ipv6(c: Client, st: State, need: bool) -> None:
    """Reads the IPv6 addresses, filter and address lists into st. A RouterOS
    without IPv6 (the menu does not exist) leaves rules6 None; one whose user
    cannot read it fails when need (a pause would leave IPv6 open)."""
    try:
        st.rules6 = as_list(c.get(FILTER6))
    except NotFound:
        st.rules6 = None
        return
    except Forbidden as e:
        if need:
            raise PluginError(f"the RouterOS user cannot read {FILTER6}") from e
        st.rules6 = None
        return
    for menu, attr in ((ADDRESSES6, "addresses6"), (ADDRESS_LISTS6, "address_lists6")):
        try:
            setattr(st, attr, as_list(c.get(menu)))
        except (Forbidden, NotFound) as e:
            if need:
                raise PluginError(f"the RouterOS user cannot read {menu}") from e


def lan_prefixes6(st: State) -> list[str]:
    """The router's own IPv6 prefixes on the LAN side (not the WAN's), global
    ones (link-local and ULA are in the list already), as networks."""
    out: list[str] = []
    for a in st.addresses6:
        iface = text(a.get("interface"))
        if iface in st.wan or flag(a.get("disabled")) or flag(a.get("invalid")):
            continue
        try:
            net = ipaddress.ip_interface(text(a.get("address"))).network
        except ValueError:
            continue
        if net.version != 6 or net.is_link_local or net.is_multicast:
            continue
        if any(net.subnet_of(ipaddress.ip_network(r)) for r in LOCAL6_RANGES):  # type: ignore[arg-type]
            continue
        if str(net) not in out:
            out.append(str(net))
    return out


def private6_entries(st: State) -> list[dict[str, Any]]:
    return [
        a
        for a in st.address_lists6
        if a.get("list") == PRIVATE6_LIST and not flag(a.get("dynamic"))
    ]


def ensure_private6(j: Journal, st: State) -> None:
    """omini-private6: link-local, ULA, multicast and the LAN-side prefixes;
    Omini's entries of a prefix the router no longer has go."""
    want = [*LOCAL6_RANGES, *lan_prefixes6(st)]
    have = private6_entries(st)
    got = {text(a.get("address")) for a in have}
    for a in have:
        if text(a.get("address")) not in want and text(a.get("comment")).startswith(MARK):
            j.remove(ADDRESS_LISTS6, a, "an old prefix of the local IPv6 networks")
    for net in want:
        if net not in got:
            j.add(
                ADDRESS_LISTS6,
                {"list": PRIVATE6_LIST, "address": net, "comment": f"{MARK}: local networks"},
                "the list of local IPv6 networks",
            )


def private6_ok(st: State) -> bool:
    got = {text(a.get("address")) for a in private6_entries(st) if on(a)}
    return set(LOCAL6_RANGES) <= got


# --- pause: changes -----------------------------------------------------------------


def set_mac_rule(
    j: Journal, menu: str, rules: list[dict[str, Any]], m: str, private: str, comment: str
) -> None:
    """The device's rule in one family's filter, first in the forward chain."""
    have = mac_rules(rules, m)
    for r in have[1:]:
        j.remove(menu, r, f"a duplicate rule of the pause of {m}")
    if have:
        body = {**mac_rule(m, private), "comment": comment, "disabled": "false"}
        body.pop("src-mac-address")  # RouterOS answers it in its own case
        j.set(menu, have[0], body, f"the pause of {m}")
        return
    j.add(
        menu,
        {**mac_rule(m, private), "comment": comment},
        f"the pause of {m}",
        place_before=first_forward(rules),
    )


def pause_set(j: Journal, st: State, value: dict[str, Any]) -> list[str]:
    """Puts the device's addresses in omini_paused (replacing the ones it had),
    makes sure the shared rule is there, and adds the device's rules by MAC
    (IPv4 and IPv6). Answers the addresses."""
    router_only(st)
    m = norm_mac(value.get("mac"))
    if not m:
        raise PluginError(f"{value.get('mac')!r} is not a MAC address")
    if m in own_macs(st):
        raise PluginError(f"{m} is the router's own MAC address")
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
            place_before=first_forward(st.rules),
        )
    elif not any(on(r) for r in rules):
        j.set(FILTER, rules[0], {"disabled": "false"}, "the rule of paused devices")
    set_mac_rule(j, FILTER, st.rules or [], m, PRIVATE_LIST, comment)
    if st.rules6 is not None:
        ensure_private6(j, st)
        set_mac_rule(j, FILTER6, st.rules6, m, PRIVATE6_LIST, comment)
    return ips


def pause_delete(j: Journal, st: State, value: dict[str, Any]) -> None:
    """Takes the device out of omini_paused and removes its rules; the shared
    rule and omini-private6 go with the last device (omini-private stays:
    networks use it). A device paused before 0.3.1 (IPv4 list only) resumes
    the same way."""
    router_only(st)
    m = norm_mac(value.get("mac"))
    if not m:
        raise PluginError(f"{value.get('mac')!r} is not a MAC address")
    have = entries_of(st, m)
    for a in have:
        j.remove(ADDRESS_LISTS, a, f"the pause of {m}")
    for r in mac_rules(st.rules, m):
        j.remove(FILTER, r, f"the pause of {m}")
    for r in mac_rules(st.rules6, m):
        j.remove(FILTER6, r, f"the pause of {m}")
    if any(all(a is not h for h in have) for a in entries(st)):
        return
    for r in pause_rules(st):
        j.remove(FILTER, r, "the rule of paused devices")
    for a in private6_entries(st):
        if text(a.get("comment")).startswith(MARK):
            j.remove(ADDRESS_LISTS6, a, "the list of local IPv6 networks")


def rule_on(rules: list[dict[str, Any]] | None, m: str, private: str) -> bool:
    return any(
        on(r) and text(r.get("dst-address-list")) == "!" + private for r in mac_rules(rules, m)
    )


def pause_matches(st: State, value: dict[str, Any], ips: list[str]) -> bool:
    m = norm_mac(value.get("mac"))
    got = next((p for p in paused(st) if p.mac == m), None)
    if got is None or set(got.ips or []) != set(ips) or not any(on(r) for r in pause_rules(st)):
        return False
    if not rule_on(st.rules, m, PRIVATE_LIST):
        return False
    return st.rules6 is None or (rule_on(st.rules6, m, PRIVATE6_LIST) and private6_ok(st))


def pause_gone(st: State, value: dict[str, Any]) -> bool:
    m = norm_mac(value.get("mac"))
    if any(p.mac == m for p in paused(st)) or mac_rules(st.rules, m) or mac_rules(st.rules6, m):
        return False
    return bool(entries(st)) or not pause_rules(st)


# --- pause: open connections ----------------------------------------------------------


def conn_source(value: Any) -> str:
    """The address of a connection's src-address: '192.168.88.10:51234',
    '192.168.88.10' (ICMP), '[2001:db8::1]:443' or '2001:db8::1' → the IP."""
    src = text(value)
    if src.startswith("["):
        return src[1:].split("]", 1)[0]
    if src.count(":") == 1:
        return src.split(":")[0]
    return src


def device_ips6(c: Client, m: str) -> set[str]:
    """The device's IPv6 addresses the router knows (/ipv6/neighbor), but
    link-local ones (local traffic is not cut)."""
    try:
        rows = as_list(c.get(NEIGHBORS6))
    except Exception:
        return set()
    out = set()
    for r in rows:
        if norm_mac(r.get("mac-address")) != m:
            continue
        try:
            ip = ipaddress.IPv6Address(text(r.get("address")).split("%")[0])
        except ValueError:
            continue
        if not ip.is_link_local:
            out.add(str(ip))
    return out


def cut_in(c: Client, menu: str, want: set[str], budget: int) -> int:
    cut = 0
    try:
        rows = as_list(c.get(f"{menu}?.proplist=.id,src-address"))
    except Exception:
        log.warning("could not read the firewall connections to cut them")
        return 0
    for r in rows:
        src = conn_source(r.get("src-address"))
        try:
            src = str(ipaddress.ip_address(src))
        except ValueError:
            continue
        if src not in want or not text(r.get(".id")):
            continue
        try:
            c.delete(f"{menu}/{text(r.get('.id'))}")
            cut += 1
        except (Forbidden, NotFound):
            continue  # gone already, or not allowed: the rule still drops what is new
        except Exception:
            log.warning("could not remove a connection of a paused device")
            break
        if cut >= budget:
            break
    return cut


def cut_connections(c: Client, ips: list[str], m: str = "", ipv6: bool = False) -> int:
    """Removes the connections from these addresses, and with ipv6 the ones
    from the device's IPv6 addresses (best effort: the router keeps working
    either way, and the rules drop the next packets). Answers how many were
    removed."""
    cut = cut_in(c, CONNECTIONS, set(ips), MAX_CUTS)
    if ipv6 and m and cut < MAX_CUTS:
        mine = device_ips6(c, m)
        if mine:
            cut += cut_in(c, CONNECTIONS6, mine, MAX_CUTS - cut)
    return cut
