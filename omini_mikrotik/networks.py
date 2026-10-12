"""A VLAN's network on a RouterOS router (the router side): its VLAN
interface, gateway address, DHCP server and access rules; network.set,
network.delete, reservation.set and reservation.delete.

- The networks are the statically addressed, private, LAN-side interfaces of
  a router (a device with a default route): VLAN interfaces (their VLAN) and
  the untagged LAN (a bridge or a port: VLAN 1). The WAN is never one.
- DHCP: the /ip/dhcp-server on the interface, its /ip/pool and the
  /ip/dhcp-server/network of the subnet (the gateway is handed out as router
  and DNS server).
- Access: the /ip/firewall/filter rules whose in-interface is the network's
  interface, read as one of three presets (else "custom"). Omini's rules are
  placed before the first drop rule of their chain (the defconf "drop all not
  coming from LAN" and "drop all from WAN not DSTNATed"), and their comments
  start with "Omini".
- mDNS: /ip/dns mdns-repeat-ifaces (RouterOS 7.16 and later).
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from omini_sdk import PluginError

from omini_mikrotik.client import Client, Forbidden, NotFound
from omini_mikrotik.common import as_list, as_object, flag, mac, names, vlan_ids
from omini_mikrotik.journal import Journal
from omini_mikrotik.vlans import BRIDGE_VLANS, shared

INTERFACES = "/interface"
VLAN_INTERFACES = "/interface/vlan"
BRIDGES = "/interface/bridge"
BRIDGE_PORTS = "/interface/bridge/port"
ADDRESSES = "/ip/address"
POOLS = "/ip/pool"
SERVERS = "/ip/dhcp-server"
DHCP_NETWORKS = "/ip/dhcp-server/network"
LEASES = "/ip/dhcp-server/lease"
FILTER = "/ip/firewall/filter"
ADDRESS_LISTS = "/ip/firewall/address-list"
DNS = "/ip/dns"
MDNS = "mdns-repeat-ifaces"

PRIVATE_LIST = "omini-private"
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
PREFIX = "Omini"
PRESETS = ("internet", "all", "local")

# Rule properties that are not matchers (what a rule looks at).
RULE_META = {
    ".id",
    ".nextid",
    "chain",
    "action",
    "comment",
    "disabled",
    "dynamic",
    "invalid",
    "bytes",
    "packets",
    "log",
    "log-prefix",
    "in-interface",
}


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def on(row: dict[str, Any]) -> bool:
    return not flag(row.get("disabled")) and not flag(row.get("invalid"))


@dataclass
class State:
    """What the router has, read once per change or collection."""

    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)  # name → /interface row
    vlan_ifaces: list[dict[str, Any]] = field(default_factory=list)
    bridges: list[dict[str, Any]] = field(default_factory=list)
    bridge_ports: list[dict[str, Any]] = field(default_factory=list)
    bridge_vlans: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)
    wan: set[str] = field(default_factory=set)
    servers: list[dict[str, Any]] | None = None  # None: cannot be read
    pools: list[dict[str, Any]] = field(default_factory=list)
    dhcp_networks: list[dict[str, Any]] = field(default_factory=list)
    leases: list[dict[str, Any]] | None = None
    rules: list[dict[str, Any]] | None = None
    address_lists: list[dict[str, Any]] = field(default_factory=list)
    dns: dict[str, Any] | None = None
    # IPv6 (read only for a pause, see services.read_ipv6): None when RouterOS has
    # no IPv6 firewall (the package is off).
    addresses6: list[dict[str, Any]] = field(default_factory=list)
    rules6: list[dict[str, Any]] | None = None
    address_lists6: list[dict[str, Any]] = field(default_factory=list)

    def vlan_iface(self, name: str) -> dict[str, Any] | None:
        return next((v for v in self.vlan_ifaces if v.get("name") == name), None)

    def bridge_of(self, port: str) -> str | None:
        return next(
            (str(p.get("bridge")) for p in self.bridge_ports if p.get("interface") == port), None
        )

    def is_bridge(self, name: str) -> bool:
        return any(b.get("name") == name for b in self.bridges)

    def filtering(self, bridge: str) -> bool:
        return any(b.get("name") == bridge and flag(b.get("vlan-filtering")) for b in self.bridges)


def get_list(c: Client, path: str) -> list[dict[str, Any]] | None:
    try:
        return as_list(c.get(path))
    except (Forbidden, NotFound):
        return None


def read_state(
    c: Client,
    wan: set[str],
    raw: dict[str, Any] | None = None,
    leases: list[dict[str, Any]] | None = None,
) -> State:
    """The router as the REST API shows it. raw: the menus a collection already
    read (interfaces, vlans, bridges, bridge_ports, bridge_vlans, addresses);
    leases: the DHCP leases it read."""
    if raw is None:
        raw = {
            "interfaces": get_list(c, INTERFACES) or [],
            "vlans": get_list(c, VLAN_INTERFACES) or [],
            "bridges": get_list(c, BRIDGES) or [],
            "bridge_ports": get_list(c, BRIDGE_PORTS) or [],
            "bridge_vlans": get_list(c, BRIDGE_VLANS) or [],
            "addresses": get_list(c, ADDRESSES) or [],
        }
    st = State(
        interfaces={r["name"]: r for r in raw["interfaces"] if r.get("name")},
        vlan_ifaces=raw["vlans"],
        bridges=raw["bridges"],
        bridge_ports=raw["bridge_ports"],
        bridge_vlans=raw["bridge_vlans"],
        addresses=raw["addresses"],
        wan=set(wan),
    )
    st.servers = get_list(c, SERVERS)
    if st.servers is not None:
        st.pools = get_list(c, POOLS) or []
        st.dhcp_networks = get_list(c, DHCP_NETWORKS) or []
    st.leases = leases if leases is not None else get_list(c, LEASES)
    st.rules = get_list(c, FILTER)
    if st.rules is not None:
        st.address_lists = get_list(c, ADDRESS_LISTS) or []
    with contextlib.suppress(Forbidden, NotFound):
        st.dns = as_object(c.get(DNS))
    return st


# --- reading -----------------------------------------------------------------


@dataclass
class Net:
    vlan: int
    interface: str
    parent: str | None
    gateway: str
    address: dict[str, Any]


def nets(st: State) -> list[Net]:
    """Each VLAN's network: the private, static, LAN-side addresses. VLAN
    interfaces serve their VLAN; a bridge (before a port) is the untagged LAN."""
    if not st.wan:
        return []  # not a router: its address is how it is managed
    out: dict[int, Net] = {}
    rank = {"vlan": 0, "bridge": 1, "ether": 2, "bond": 2}
    rows = []
    for a in st.addresses:
        iface = text(a.get("interface"))
        kind = text((st.interfaces.get(iface) or {}).get("type"))
        if not on(a) or flag(a.get("dynamic")) or iface in st.wan or kind not in rank:
            continue
        try:
            gw = ipaddress.ip_interface(text(a.get("address")))
        except ValueError:
            continue
        if gw.version != 4 or not gw.ip.is_private or gw.network.prefixlen >= 31:
            continue
        rows.append((rank[kind], iface, kind, str(gw), a))
    for _, iface, kind, gw, a in sorted(rows, key=lambda r: r[0]):
        if kind == "vlan":
            v = st.vlan_iface(iface) or {}
            vid, parent = int(text(v.get("vlan-id")) or 0), text(v.get("interface")) or None
        else:
            vid, parent = 1, None
        if 1 <= vid <= 4094 and vid not in out:
            out[vid] = Net(vid, iface, parent, gw, a)
    return [out[k] for k in sorted(out)]


def name_of(st: State, n: Net) -> str | None:
    """The interface's comment, else the address's."""
    iface = st.vlan_iface(n.interface) or st.interfaces.get(n.interface) or {}
    return text(iface.get("comment")) or text(n.address.get("comment")) or None


def first_range(ranges: str) -> tuple[str, str] | None:
    """'10.0.0.10-10.0.0.20,10.0.0.30' → ('10.0.0.10', '10.0.0.20'); a CIDR → its hosts."""
    first = ranges.split(",")[0].strip()
    if not first:
        return None
    if "-" in first:
        lo, hi = (p.strip() for p in first.split("-", 1))
        return lo, hi
    if "/" in first:
        with contextlib.suppress(ValueError):
            net = ipaddress.ip_network(first, strict=False)
            hosts = list(net.hosts()) if net.num_addresses <= 65536 else []
            if hosts:
                return str(hosts[0]), str(hosts[-1])
        return None
    return first, first


def server_on(st: State, iface: str) -> dict[str, Any] | None:
    """The DHCP server on the interface, an enabled one first."""
    found = [s for s in st.servers or [] if s.get("interface") == iface]
    found.sort(key=lambda s: bool(flag(s.get("disabled"))))
    return found[0] if found else None


def pool_named(st: State, name: str) -> dict[str, Any] | None:
    return next((p for p in st.pools if p.get("name") == name), None)


def dhcp_of(st: State, n: Net) -> dict[str, Any] | None:
    if st.servers is None:
        return None
    s = server_on(st, n.interface)
    if s is None or not on(s):
        return {"enabled": False}
    pool = pool_named(st, text(s.get("address-pool")))
    rng = first_range(text((pool or {}).get("ranges")))
    if not rng:
        return {"enabled": True}
    return {"enabled": True, "from": rng[0], "to": rng[1]}


def rules_on(st: State, iface: str) -> list[dict[str, Any]]:
    return [
        r
        for r in st.rules or []
        if r.get("in-interface") == iface
        and r.get("chain") in ("input", "forward")
        and not flag(r.get("disabled"))
        and not flag(r.get("dynamic"))
    ]


def matchers(r: dict[str, Any]) -> dict[str, str]:
    return {k: text(v) for k, v in r.items() if k not in RULE_META and text(v)}


def rule_shape(r: dict[str, Any]) -> str:
    """A rule as one of the presets' pieces: dns, in_accept, in_drop,
    fwd_accept, fwd_drop, fwd_private; anything else is other."""
    action = text(r.get("action"))
    kind = "drop" if action in ("drop", "reject") else "accept" if action == "accept" else ""
    m = matchers(r)
    if not kind:
        return "other"
    if r.get("chain") == "input":
        if (
            kind == "accept"
            and set(m) == {"protocol", "dst-port"}
            and m["protocol"] in ("udp", "tcp")
            and m["dst-port"] == "53"
        ):
            return "dns"
        return f"in_{kind}" if not m else "other"
    if not m:
        return f"fwd_{kind}"
    if kind == "drop" and m == {"dst-address-list": PRIVATE_LIST}:
        return "fwd_private"
    return "other"


def access_of(st: State, iface: str) -> str | None:
    """Which preset the interface's rules are, else custom."""
    if st.rules is None:
        return None
    shapes = [rule_shape(r) for r in rules_on(st, iface)]
    if not shapes or "other" in shapes:
        return "custom"
    dns = "dns" in shapes
    inp = [s for s in shapes if s.startswith("in_")]
    fwd = [s for s in shapes if s.startswith("fwd_")]
    if inp == ["in_accept"] and fwd == ["fwd_accept"]:
        return "all"
    if dns and inp == ["in_drop"] and fwd == ["fwd_private", "fwd_accept"]:
        return "internet"
    if dns and inp == ["in_drop"] and fwd == ["fwd_drop"]:
        return "local"
    return "custom"


def mdns_of(st: State, iface: str) -> bool | None:
    if st.dns is None or MDNS not in st.dns:
        return None
    return iface in names(st.dns.get(MDNS))


def networks(st: State) -> list[dict[str, Any]]:
    return [
        {
            "vlan": n.vlan,
            "name": name_of(st, n),
            "interface": n.interface,
            "device": n.interface,
            "parent": n.parent,
            "gateway": n.gateway,
            "dhcp": dhcp_of(st, n),
            "access": access_of(st, n.interface),
            "mdns": mdns_of(st, n.interface),
        }
        for n in nets(st)
    ]


def reservations(st: State) -> list[dict[str, Any]]:
    """Static leases (a fixed address for a MAC)."""
    out = []
    for r in st.leases or []:
        m = mac(r.get("mac-address"))
        ip = text(r.get("address"))
        if flag(r.get("dynamic")) is not False or flag(r.get("disabled")) or not m:
            continue
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        out.append({"mac": m, "ip": ip, "name": text(r.get("comment")) or None})
    return out


# --- changes -------------------------------------------------------------------


def label(name: str, iface: str) -> str:
    return name or iface


def unique(taken: set[str], want: str) -> str:
    out, k = want, 2
    while out in taken:
        out, k = f"{want}-{k}", k + 1
    return out


def parse_gateway(value: Any) -> ipaddress.IPv4Interface:
    try:
        gw = ipaddress.ip_interface(text(value))
    except ValueError as e:
        raise PluginError(f"{value!r} is not an address with its prefix (192.168.30.1/24)") from e
    if gw.version != 4 or not gw.ip.is_private or "/" not in text(value):
        raise PluginError("the gateway must be a private IPv4 address with its prefix")
    if gw.network.prefixlen >= 31 or gw.ip in (
        gw.network.network_address,
        gw.network.broadcast_address,
    ):
        raise PluginError(f"{value} is not a host address of its network")
    return gw  # type: ignore[return-value]


def check_dhcp(gw: ipaddress.IPv4Interface, dhcp: Any) -> dict[str, Any] | None:
    if not dhcp or not dhcp.get("enabled"):
        return None
    try:
        lo, hi = (
            ipaddress.IPv4Address(text(dhcp.get("from"))),
            ipaddress.IPv4Address(text(dhcp.get("to"))),
        )
    except ValueError as e:
        raise PluginError("the DHCP range needs a first and a last address") from e
    if lo > hi or lo not in gw.network or hi not in gw.network:
        raise PluginError(f"the DHCP range {lo}-{hi} is not inside {gw.network}")
    if lo <= gw.ip <= hi:
        raise PluginError(f"the DHCP range {lo}-{hi} holds the gateway {gw.ip}")
    return {"from": str(lo), "to": str(hi)}


def lan_parent(st: State) -> str:
    """Where a new VLAN goes by default: the LAN's bridge, else the one bridge
    with VLAN filtering."""
    lan = next((n for n in nets(st) if n.vlan == 1), None)
    if lan and st.is_bridge(lan.interface):
        return lan.interface
    on_ = [text(b.get("name")) for b in st.bridges if flag(b.get("vlan-filtering"))]
    if len(on_) == 1:
        return on_[0]
    return lan.interface if lan else ""


def ensure_bridge_vlan(
    j: Journal, st: State, bridge: str, vid: int, port: str | None, name: str
) -> None:
    """With VLAN filtering, the bridge itself (and the port the VLAN rides on)
    must be tagged members of the VLAN for the router to reach it."""
    if not st.filtering(bridge):
        return
    want = [bridge] + ([port] if port else [])
    entries = [
        e
        for e in st.bridge_vlans
        if e.get("bridge") == bridge
        and not flag(e.get("dynamic"))
        and not flag(e.get("disabled"))
        and vid in vlan_ids(e.get("vlan-ids"))
    ]
    if not entries:
        body = {"bridge": bridge, "vlan-ids": str(vid), "tagged": ",".join(want)}
        if name:
            body["comment"] = name
        j.add(BRIDGE_VLANS, body, f"VLAN {vid} on {bridge}")
        return
    e = entries[0]
    tagged = names(e.get("tagged"))
    missing = [p for p in want if p not in tagged]
    if not missing:
        return
    if len(vlan_ids(e.get("vlan-ids"))) > 1:
        raise shared(e)
    j.set(BRIDGE_VLANS, e, {"tagged": ",".join(tagged + missing)}, f"VLAN {vid} on {bridge}")


def create_vlan_iface(j: Journal, st: State, vid: int, parent: str, name: str) -> str:
    """The VLAN interface for vid on parent (a bridge port puts it on its
    bridge, the port then tagged); an existing one is reused."""
    if not parent or parent not in st.interfaces:
        raise PluginError(
            f"no port to put VLAN {vid} on: name the router's port toward the switches"
            if not parent
            else f"the router has no interface named {parent}"
        )
    port: str | None = None
    on_ = parent
    if not st.is_bridge(parent) and st.bridge_of(parent):
        port, on_ = parent, st.bridge_of(parent) or parent
    if st.is_bridge(on_):
        ensure_bridge_vlan(j, st, on_, vid, port, name)
    existing = next(
        (
            v
            for v in st.vlan_ifaces
            if text(v.get("vlan-id")) == str(vid) and v.get("interface") == on_
        ),
        None,
    )
    if existing is not None:
        return text(existing.get("name"))
    iface = unique(set(st.interfaces), f"vlan{vid}")
    body = {"name": iface, "vlan-id": str(vid), "interface": on_}
    if name:
        body["comment"] = name
    j.add(VLAN_INTERFACES, body, f"the VLAN {vid} interface")
    st.interfaces[iface] = {"name": iface, "type": "vlan"}
    st.vlan_ifaces.append({"name": iface, "vlan-id": str(vid), "interface": on_, **body})
    return iface


def set_dhcp(
    j: Journal, st: State, iface: str, gw: ipaddress.IPv4Interface, old_gw: str, dhcp: Any, lbl: str
) -> None:
    rng = check_dhcp(gw, dhcp)
    if st.servers is None:
        if rng:
            raise PluginError("the RouterOS user cannot read /ip/dhcp-server")
        return
    server = server_on(st, iface)
    if rng is None:
        if server is not None and on(server):
            j.set(SERVERS, server, {"disabled": "true"}, f"turning DHCP off on {iface}")
        return
    ranges = f"{rng['from']}-{rng['to']}"
    pool = pool_named(st, text((server or {}).get("address-pool")))
    if pool is not None:
        j.set(POOLS, pool, {"ranges": ranges}, f"the DHCP range of {iface}")
    else:
        pool_name = unique({text(p.get("name")) for p in st.pools}, f"omini-{iface}")
        j.add(
            POOLS,
            {"name": pool_name, "ranges": ranges, "comment": f"{PREFIX}: {lbl}"},
            "the DHCP pool",
        )
        st.pools.append({"name": pool_name, "ranges": ranges})
        if server is not None:
            j.set(SERVERS, server, {"address-pool": pool_name}, f"the DHCP pool of {iface}")
    if server is None:
        name = unique({text(s.get("name")) for s in st.servers}, f"omini-{iface}")
        j.add(
            SERVERS,
            {
                "name": name,
                "interface": iface,
                "address-pool": pool_name,
                "comment": f"{PREFIX}: {lbl}",
                "disabled": "false",
            },
            f"the DHCP server of {iface}",
        )
    elif not on(server):
        j.set(SERVERS, server, {"disabled": "false"}, f"turning DHCP on on {iface}")
    net, ip = str(gw.network), str(gw.ip)
    old_net = str(ipaddress.ip_interface(old_gw).network) if old_gw else net
    old_ip = old_gw.split("/")[0] if old_gw else ip
    dn = next((d for d in st.dhcp_networks if d.get("address") in (net, old_net)), None)
    if dn is None:
        j.add(
            DHCP_NETWORKS,
            {"address": net, "gateway": ip, "dns-server": ip, "comment": f"{PREFIX}: {lbl}"},
            f"the DHCP network of {iface}",
        )
        return
    body = {"address": net, "gateway": ip}
    # Empty: RouterOS hands out itself. Else only the old gateway follows the new one.
    if old_ip != ip and text(dn.get("dns-server")) == old_ip:
        body["dns-server"] = ip
    j.set(DHCP_NETWORKS, dn, body, f"the DHCP network of {iface}")


def preset_rules(access: str, iface: str, lbl: str) -> list[dict[str, str]]:
    def rule(chain: str, action: str, what: str, **extra: str) -> dict[str, str]:
        return {
            "chain": chain,
            "action": action,
            "in-interface": iface,
            **extra,
            "comment": f"{PREFIX}: {lbl} {what}",
        }

    if access == "all":
        return [
            rule("input", "accept", "to the router"),
            rule("forward", "accept", "to everything"),
        ]
    dns = [
        rule("input", "accept", "DNS", protocol="udp", **{"dst-port": "53"}),
        rule("input", "accept", "DNS", protocol="tcp", **{"dst-port": "53"}),
        rule("input", "drop", "not to the router"),
    ]
    if access == "internet":
        return [
            *dns,
            rule("forward", "drop", "not to other networks", **{"dst-address-list": PRIVATE_LIST}),
            rule("forward", "accept", "to the internet"),
        ]
    return [*dns, rule("forward", "drop", "only its own network")]


def anchor(rules: list[dict[str, Any]], chain: str, gone: set[str]) -> str | None:
    """The first drop rule of the chain (not the one for invalid packets):
    Omini's rules go right before it."""
    for r in rules:
        if r.get("chain") != chain or text(r.get(".id")) in gone or flag(r.get("dynamic")):
            continue
        if text(r.get("action")) in ("drop", "reject") and "invalid" not in text(
            r.get("connection-state")
        ):
            return text(r.get(".id"))
    return None


def ensure_private_list(j: Journal, st: State) -> None:
    have = {text(a.get("address")) for a in st.address_lists if a.get("list") == PRIVATE_LIST}
    for net in PRIVATE_RANGES:
        if net not in have:
            j.add(
                ADDRESS_LISTS,
                {"list": PRIVATE_LIST, "address": net, "comment": f"{PREFIX}: private networks"},
                "the list of private networks",
            )


def set_access(j: Journal, st: State, iface: str, access: Any, lbl: str) -> None:
    """Replaces the interface's rules with the preset; custom (or none) leaves them."""
    if access not in PRESETS or access_of(st, iface) == access:
        return
    if st.rules is None:
        raise PluginError("the RouterOS user cannot read /ip/firewall/filter")
    if access == "internet":
        ensure_private_list(j, st)
    gone: set[str] = set()
    for r in rules_on(st, iface):
        j.remove(FILTER, r, f"the old rules of {iface}")
        gone.add(text(r.get(".id")))
    for r in preset_rules(access, iface, lbl):
        j.add(
            FILTER,
            r,
            f"the access rules of {iface}",
            place_before=anchor(st.rules, r["chain"], gone),
        )


def set_mdns(j: Journal, st: State, iface: str, want: Any) -> None:
    if want is None:
        return
    if st.dns is None or MDNS not in st.dns:
        if want:
            raise PluginError(
                "discovery between networks (mDNS) needs RouterOS 7.16 or later "
                "(IP → DNS → mDNS Repeat Interfaces)"
            )
        return
    current = names(st.dns.get(MDNS))
    if (iface in current) == bool(want):
        return
    new = [i for i in current if i != iface] + ([iface] if want else [])
    j.set_menu(DNS, st.dns, {MDNS: ",".join(new)}, "the mDNS repeater")


def guard_management(gateway: str, mgmt: str, what: str) -> None:
    if gateway.split("/")[0] == mgmt:
        raise PluginError(
            f"Omini reaches the router through {mgmt}: {what} would cut it off. "
            "Change it from WinBox instead"
        )


def apply_set(j: Journal, st: State, want: dict[str, Any], mgmt: str) -> None:
    vid = int(want["vlan"])
    if not 1 <= vid <= 4094:
        raise PluginError("VLAN ids go from 1 to 4094")
    name = text(want.get("name"))
    gw = parse_gateway(want.get("gateway"))
    check_dhcp(gw, want.get("dhcp"))  # refused before anything is written
    if want.get("mdns") and (st.dns is None or MDNS not in st.dns):
        set_mdns(j, st, "", True)
    existing = next((n for n in nets(st) if n.vlan == vid), None)
    if existing is None and vid == 1:
        raise PluginError("the router has no LAN to change")
    if existing is None and not st.wan:
        raise PluginError(
            "this device does not route (it has no default route): it has no networks"
        )
    old_gw = ""
    if existing is not None:
        iface = existing.interface
        old_gw = existing.gateway
        if old_gw != str(gw):
            guard_management(old_gw, mgmt, "changing its address")
            j.set(ADDRESSES, existing.address, {"address": str(gw)}, f"the gateway of {iface}")
        v = st.vlan_iface(iface)
        if name and v is not None:
            j.set(VLAN_INTERFACES, v, {"comment": name}, f"the name of {iface}")
    else:
        for n in nets(st):
            if ipaddress.ip_interface(n.gateway).network.overlaps(gw.network):
                raise PluginError(
                    f"{gw.network} overlaps the network of VLAN {n.vlan} ({n.gateway})"
                )
        parent = text(want.get("parent")) or lan_parent(st)
        iface = create_vlan_iface(j, st, vid, parent, name)
        j.add(
            ADDRESSES,
            {"address": str(gw), "interface": iface, "comment": f"{PREFIX}: {label(name, iface)}"},
            f"the gateway of {iface}",
        )
    lbl = label(name or (name_of(st, existing) or "" if existing else ""), iface)
    set_dhcp(j, st, iface, gw, old_gw, want.get("dhcp"), lbl)
    set_access(j, st, iface, want.get("access"), lbl)
    set_mdns(j, st, iface, want.get("mdns"))


def apply_delete(j: Journal, st: State, vid: int, mgmt: str) -> None:
    if vid == 1:
        raise PluginError("the LAN cannot be removed from Omini")
    n = next((n for n in nets(st) if n.vlan == vid), None)
    if n is None:
        raise PluginError(f"the router serves no network on VLAN {vid}")
    guard_management(n.gateway, mgmt, "removing this network")
    iface = n.interface
    for r in rules_on(st, iface):
        j.remove(FILTER, r, f"the rules of {iface}")
    set_mdns(j, st, iface, False)
    net = str(ipaddress.ip_interface(n.gateway).network)
    for s in [s for s in st.servers or [] if s.get("interface") == iface]:
        j.remove(SERVERS, s, f"the DHCP server of {iface}")
        pool = pool_named(st, text(s.get("address-pool")))
        others = [
            o
            for o in st.servers or []
            if o is not s and o.get("address-pool") == s.get("address-pool")
        ]
        if pool is not None and not others and text(pool.get("comment")).startswith(PREFIX):
            j.remove(POOLS, pool, f"the DHCP pool of {iface}")
    for d in st.dhcp_networks:
        if d.get("address") == net:
            j.remove(DHCP_NETWORKS, d, f"the DHCP network of {iface}")
    for a in st.addresses:
        if a.get("interface") == iface and not flag(a.get("dynamic")):
            j.remove(ADDRESSES, a, f"the address of {iface}")
    v = st.vlan_iface(iface)
    if v is not None:
        j.remove(VLAN_INTERFACES, v, f"the interface {iface}")
        bridge = text(v.get("interface"))
        for e in st.bridge_vlans:
            if (
                e.get("bridge") == bridge
                and not flag(e.get("dynamic"))
                and vlan_ids(e.get("vlan-ids")) == [vid]
                and bridge in names(e.get("tagged"))
            ):
                rest = [p for p in names(e.get("tagged")) if p != bridge]
                j.set(BRIDGE_VLANS, e, {"tagged": ",".join(rest)}, f"VLAN {vid} on {bridge}")


def matches(st: State, want: dict[str, Any]) -> bool:
    """Whether the router serves want as asked."""
    have = next((n for n in networks(st) if n["vlan"] == int(want["vlan"])), None)
    if have is None or have["gateway"] != str(ipaddress.ip_interface(text(want.get("gateway")))):
        return False
    d, h = want.get("dhcp") or {"enabled": False}, have.get("dhcp") or {"enabled": False}
    if bool(d.get("enabled")) != bool(h.get("enabled")):
        return False
    if d.get("enabled") and (d.get("from"), d.get("to")) != (h.get("from"), h.get("to")):
        return False
    access = want.get("access")
    if access in PRESETS and have["access"] != access:
        return False
    mdns = want.get("mdns")
    return mdns is None or have.get("mdns") is None or bool(have["mdns"]) == bool(mdns)


# --- reservations ----------------------------------------------------------------


def norm_mac(value: Any) -> str:
    raw = re.sub(r"[^0-9a-f]", "", text(value).lower())
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)) if len(raw) == 12 else ""


def server_for(st: State, ip: ipaddress.IPv4Address) -> str:
    """The DHCP server of the network that holds ip."""
    for n in nets(st):
        if ip in ipaddress.ip_interface(n.gateway).network:
            s = server_on(st, n.interface)
            if s is not None and on(s):
                return text(s.get("name"))
    raise PluginError(f"no DHCP server serves {ip}: turn DHCP on in that network first")


def static_leases(st: State, m: str) -> list[dict[str, Any]]:
    return [
        r
        for r in st.leases or []
        if flag(r.get("dynamic")) is False and norm_mac(r.get("mac-address")) == m
    ]


def reserve(j: Journal, st: State, want: dict[str, Any]) -> None:
    m = norm_mac(want.get("mac"))
    if not m:
        raise PluginError(f"{want.get('mac')!r} is not a MAC address")
    try:
        ip = ipaddress.IPv4Address(text(want.get("ip")))
    except ValueError as e:
        raise PluginError(f"{want.get('ip')!r} is not an IPv4 address") from e
    if st.leases is None:
        raise PluginError("the RouterOS user cannot read /ip/dhcp-server/lease")
    body = {"address": str(ip), "server": server_for(st, ip), "comment": text(want.get("name"))}
    current = static_leases(st, m)
    if current:
        j.set(LEASES, current[0], body, f"the reservation of {m}")
    else:
        j.add(LEASES, {"mac-address": m.upper(), **body}, f"the reservation of {m}")


def unreserve(j: Journal, st: State, value: Any) -> None:
    m = norm_mac(value)
    for r in static_leases(st, m):
        j.remove(LEASES, r, f"the reservation of {m}")
