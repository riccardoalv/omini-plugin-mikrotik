"""Turns RouterOS REST API answers into Omini devices.

RouterOS returns every value as a string ("true", "1500", "1Gbps", "1w2d3h"),
so everything is parsed here; what cannot be parsed stays None.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar
from urllib.parse import urlparse

from omini_sdk import (
    ArpEntry,
    Config,
    Device,
    DhcpLease,
    FdbEntry,
    Firmware,
    Gateway,
    Interface,
    Neighbor,
    PluginError,
    PortVlans,
    Storage,
    Temperature,
    Transceiver,
    Vlan,
    WirelessClient,
    log,
)

from omini_mikrotik.client import Client, Forbidden, NotFound

T = TypeVar("T")

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def client_from(cfg: Config) -> Client:
    url, user, password = cfg.str("url"), cfg.str("username"), cfg.str("password")
    if not url or not user:
        raise PluginError("the address and username are required")
    return Client(url, user, password, verify_tls=cfg.bool("verify_tls", False))


# --- parsing helpers -------------------------------------------------------


def mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", ":")
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else None


def number(value: Any) -> float | None:
    """'35' → 35.0, '-5.792dBm' → -5.792, '3.237V' → 3.237, '' → None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(\.\d+)?", value)
        if m:
            return float(m.group())
    return None


def counter(value: Any) -> int | None:
    n = number(value)
    return int(n) if n is not None and n >= 0 else None


def flag(value: Any) -> bool | None:
    """RouterOS booleans: 'true'/'false' (REST), 'yes'/'no' (some monitors)."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().lower()
    if v in ("true", "yes"):
        return True
    if v in ("false", "no"):
        return False
    return None


RATE_UNITS = {"": 1e-6, "k": 1e-3, "m": 1, "g": 1e3, "t": 1e6}


def rate_mbps(value: Any) -> float | None:
    """'1Gbps' → 1000, '2.5Gbps' → 2500, '866.7Mbps-80MHz/2S/SGI' → 866.7, '54Mbps' → 54."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)bps", value, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) * RATE_UNITS[m.group(2).lower()]


def speed_setting(value: Any) -> int | None:
    """Ethernet speed setting: '1G-baseT-full' → 1000, '100M-baseT-half' → 100,
    '10G-baseSR-LR' → 10000."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)([MG])-", value)
    if not m:
        return None
    v = float(m.group(1))
    return int(v * 1000) if m.group(2) == "G" else int(v)


DURATION = re.compile(r"(\d+)([wdhms])")
SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def duration_s(value: Any) -> int | None:
    """'1w2d3h4m5s' → seconds; also '2d03:04:05' and '00:09:57'."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    total = 0
    m = re.search(r"(\d+):(\d{2}):(\d{2})$", v)
    if m:
        h, mi, s = (int(x) for x in m.groups())
        total = h * 3600 + mi * 60 + s
        v = v[: m.start()]
    parts = DURATION.findall(v)
    if not parts and not m:
        return None
    total += sum(int(n) * SECONDS[u] for n, u in parts)
    return total


def as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def as_object(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def enabled(row: dict[str, Any]) -> bool:
    return not flag(row.get("disabled")) and not flag(row.get("invalid"))


def first_name(value: Any) -> str | None:
    """'ether2,bridge' → 'ether2' (neighbors list the port, then its bridge)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split(",")[0].strip() or None


def names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str):
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def vlan_ids(value: Any) -> list[int]:
    """'10,20,30-32' → [10, 20, 30, 31, 32]."""
    out: list[int] = []
    for part in names(value):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        out.extend(v for v in range(lo, min(hi, 4094) + 1) if 1 <= v <= 4094)
    return out


# --- optional data -----------------------------------------------------------


def optional(what: str, missing: list[str], fn: Callable[[], T], default: T) -> T:
    """Optional data: a missing policy, package or menu never fails the collection."""
    try:
        return fn()
    except Forbidden:
        log.warning("no permission to read %s", what)
        missing.append(what)
    except NotFound:
        log.info("%s not available on this device", what)
    except PluginError:
        raise
    except Exception:
        log.exception("could not read %s", what)
    return default


# --- interfaces ----------------------------------------------------------------

TUNNELS = (
    "pppoe-out",
    "pppoe-in",
    "pptp-out",
    "pptp-in",
    "l2tp-out",
    "l2tp-in",
    "sstp-out",
    "sstp-in",
    "ovpn-out",
    "ovpn-in",
    "wg",
    "gre",
    "gre6",
    "eoip",
    "eoipv6",
    "ipip",
    "ipipv6",
    "6to4",
    "vxlan",
    "zerotier",
    "lte",
)


def interface_type(kind: str) -> str:
    if kind == "ether":
        return "ethernet"
    if kind in ("wlan", "wifi", "wifiwave2", "wireless"):
        return "wireless"
    if kind == "bridge":
        return "bridge"
    if kind == "vlan":
        return "vlan"
    if kind == "bond":
        return "lag"
    if kind == "loopback":
        return "loopback"
    if kind in TUNNELS:
        return "tunnel"
    return "other"


def connector(name: str, sfp_type: Any = None) -> str | None:
    """By the port's factory name (users rename ports): sfp1, sfp-sfpplus1,
    sfp28-1 → SFP; qsfpplus1-1, qsfp28-1-1 → QSFP; ether1 → RJ45; combo ports
    are unknown unless a module is in."""
    n = name.lower()
    if n.startswith("qsfp"):
        return "qsfp"
    if n.startswith("sfp"):
        return "sfp"
    if n.startswith("ether"):
        return "rj45"
    if isinstance(sfp_type, str) and sfp_type:
        return "qsfp" if sfp_type.upper().startswith("QSFP") else "sfp"
    return None


def transceiver(m: dict[str, Any]) -> Transceiver | None:
    """SFP DOM readings from the ethernet monitor ('33C', '3.237V', '2mA', '-5.792dBm', '850nm')."""
    if not flag(m.get("sfp-module-present")):
        return None
    wavelength = number(m.get("sfp-wavelength"))
    return Transceiver(
        vendor=(m.get("sfp-vendor-name") or "").strip() or None,
        part=(m.get("sfp-vendor-part-number") or "").strip() or None,
        serial=(m.get("sfp-vendor-serial") or "").strip() or None,
        wavelength_nm=wavelength if wavelength and wavelength > 0 else None,
        temperature_c=number(m.get("sfp-temperature")),
        voltage_v=number(m.get("sfp-supply-voltage")),
        bias_ma=number(m.get("sfp-tx-bias-current")),
        tx_power_dbm=number(m.get("sfp-tx-power")),
        rx_power_dbm=number(m.get("sfp-rx-power")),
    )


def ethernet_monitor(c: Client, ports: list[str]) -> dict[str, dict[str, Any]]:
    """Link rate, duplex and SFP diagnostics of every Ethernet port.
    ``monitor`` is a console command, so the REST API runs it with a POST;
    with ``once`` it only reads the current state and returns."""
    if not ports:
        return {}
    data = as_list(c.post("/interface/ethernet/monitor", {"numbers": ",".join(ports), "once": ""}))
    out: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(data):
        name = row.get("name") or (ports[i] if i < len(ports) else None)
        if name:
            out[name] = row
    return out


def read_interfaces(c: Client, missing: list[str]) -> dict[str, Any]:
    """Every menu the ports are built from (optional ones skipped)."""
    return {
        "interfaces": as_list(c.get("/interface")),
        "ethernet": optional(
            "ethernet ports", missing, lambda: as_list(c.get("/interface/ethernet")), []
        ),
        "bridges": optional("bridges", missing, lambda: as_list(c.get("/interface/bridge")), []),
        "bridge_ports": optional(
            "bridge ports", missing, lambda: as_list(c.get("/interface/bridge/port")), []
        ),
        "bridge_vlans": optional(
            "bridge VLANs", missing, lambda: as_list(c.get("/interface/bridge/vlan")), []
        ),
        "vlans": optional(
            "VLAN interfaces", missing, lambda: as_list(c.get("/interface/vlan")), []
        ),
        "bonds": optional("bonding", missing, lambda: as_list(c.get("/interface/bonding")), []),
        "pppoe": optional(
            "PPPoE clients", missing, lambda: as_list(c.get("/interface/pppoe-client")), []
        ),
        "addresses": optional("IP addresses", missing, lambda: as_list(c.get("/ip/address")), []),
    }


def build_interfaces(
    raw: dict[str, Any], monitor: dict[str, dict[str, Any]], wan: set[str]
) -> list[Interface]:
    ethernet = {r.get("name"): r for r in raw["ethernet"] if r.get("name")}
    vlans = {r.get("name"): r for r in raw["vlans"] if r.get("name")}
    pppoe = {r.get("name"): r for r in raw["pppoe"] if r.get("name")}
    bonds = {r.get("name"): r for r in raw["bonds"] if r.get("name")}
    filtering = {
        r.get("name") for r in raw["bridges"] if r.get("name") and flag(r.get("vlan-filtering"))
    }
    members: dict[str, list[str]] = {}
    pvid: dict[str, int] = {}
    for p in raw["bridge_ports"]:
        port, bridge = p.get("interface"), p.get("bridge")
        if not port or not bridge or not enabled(p):
            continue
        members.setdefault(bridge, []).append(port)
        n = counter(p.get("pvid"))
        if bridge in filtering and n and 1 <= n <= 4094:
            pvid[port] = n
    tagged: dict[str, set[int]] = {}
    for v in raw["bridge_vlans"]:
        if v.get("bridge") not in filtering or not enabled(v):
            continue
        ids = vlan_ids(v.get("vlan-ids"))
        ports = names(v.get("current-tagged") or v.get("tagged"))
        for port in ports:
            tagged.setdefault(port, set()).update(ids)
    ips: dict[str, list[str]] = {}
    for a in raw["addresses"]:
        if a.get("interface") and a.get("address") and enabled(a):
            ips.setdefault(a["interface"], []).append(a["address"])

    rows = [r for r in raw["interfaces"] if r.get("name")]
    speeds: dict[str, int] = {}
    for r in rows:
        name = r["name"]
        if r.get("type") != "ether" or not flag(r.get("running")):
            continue
        mon = monitor.get(name) or {}
        speed = rate_mbps(mon.get("rate"))
        eth = ethernet.get(name) or {}
        if not speed and flag(eth.get("auto-negotiation")) is False:
            speed = speed_setting(eth.get("speed"))
        if speed:
            speeds[name] = int(speed)

    def parent_of(name: str) -> str | None:
        if name in vlans:
            return vlans[name].get("interface") or None
        if name in pppoe:
            return pppoe[name].get("interface") or None
        return None

    def link_speed(name: str) -> int | None:
        """Speed of a port, following VLANs and PPPoE down to the physical port."""
        for _ in range(4):
            if name in speeds:
                return speeds[name]
            nxt = parent_of(name)
            if not nxt:
                return None
            name = nxt
        return None

    out: list[Interface] = []
    for r in rows:
        name = r["name"]
        kind = interface_type(str(r.get("type") or ""))
        up = bool(flag(r.get("running"))) and not flag(r.get("disabled"))
        mon = monitor.get(name) or {}
        factory = r.get("default-name") or (ethernet.get(name) or {}).get("default-name") or name
        speed = speeds.get(name)
        port_members: list[str] | None = None
        if kind == "bridge":
            port_members = sorted(members.get(name, [])) or None
            speed = max((speeds[m] for m in members.get(name, []) if m in speeds), default=None)
        elif kind == "lag":
            port_members = names((bonds.get(name) or {}).get("slaves")) or None
        is_wan = name in wan
        if is_wan and not speed:
            speed = link_speed(name)
        duplex = None
        if kind == "ethernet" and up:
            fd = flag(mon.get("full-duplex"))
            duplex = "full" if fd else "half" if fd is False else None
        port_vlans = None
        if name in pvid or name in tagged:
            port_vlans = PortVlans(
                untagged=pvid.get(name),
                tagged=sorted(tagged.get(name, set())) or None,
            )
        vlan_id = counter((vlans.get(name) or {}).get("vlan-id")) if kind == "vlan" else None
        out.append(
            Interface(
                name=name,
                description=(r.get("comment") or "").strip() or None,
                type=kind,
                connector=connector(factory, mon.get("sfp-type")) if kind == "ethernet" else None,
                mac=mac(r.get("mac-address")),
                ips=ips.get(name) or None,
                wan=True if is_wan else None,
                parent=parent_of(name),
                members=port_members,
                up=up,
                speed_mbps=speed if up and speed else None,
                duplex=duplex,
                rx_bytes=counter(r.get("rx-byte")),
                tx_bytes=counter(r.get("tx-byte")),
                rx_errors=counter(r.get("rx-error")),
                tx_errors=counter(r.get("tx-error")),
                vlan=vlan_id if vlan_id and 1 <= vlan_id <= 4094 else None,
                vlans=port_vlans,
                transceiver=transceiver(mon) if kind == "ethernet" else None,
            )
        )
    return out


def device_vlans(raw: dict[str, Any]) -> list[Vlan]:
    """VLAN interfaces (with their subnet) and VLANs of the bridge VLAN table."""
    subnets: dict[str, str] = {}
    for a in raw["addresses"]:
        try:
            net = ipaddress.ip_interface(a.get("address") or "").network
        except ValueError:
            continue
        if a.get("interface") and net.version == 4 and enabled(a):
            subnets.setdefault(a["interface"], str(net))
    out: dict[int, Vlan] = {}
    for v in raw["vlans"]:
        n = counter(v.get("vlan-id"))
        name = v.get("name")
        if not n or not 1 <= n <= 4094 or n in out:
            continue
        out[n] = Vlan(id=n, name=name or None, interface=name or None, subnet=subnets.get(name))
    for v in raw["bridge_vlans"]:
        for n in vlan_ids(v.get("vlan-ids")):
            out.setdefault(n, Vlan(id=n))
    return [out[k] for k in sorted(out)]


# --- routes and gateways -----------------------------------------------------------


def default_routes(c: Client) -> list[dict[str, Any]]:
    return [
        r
        for r in as_list(c.get("/ip/route"))
        if r.get("dst-address") == "0.0.0.0/0"
        and r.get("routing-table", "main") == "main"
        and not flag(r.get("disabled"))
    ]


def route_interface(
    r: dict[str, Any], addresses: list[dict[str, Any]], known: set[str]
) -> str | None:
    """'203.0.113.1%ether1' → ether1; a gateway that is an interface (pppoe-out1);
    else the interface whose subnet holds the gateway address."""
    for key in ("immediate-gw", "gateway"):
        value = str(r.get(key) or "")
        if "%" in value:
            return value.split("%", 1)[1] or None
    gw = str(r.get("gateway") or "")
    if gw in known:
        return gw
    try:
        ip = ipaddress.ip_address(gw)
    except ValueError:
        return None
    for a in addresses:
        try:
            if ip in ipaddress.ip_interface(a.get("address") or "").network:
                return a.get("interface")
        except ValueError:
            continue
    return None


def gateways(
    routes: list[dict[str, Any]], addresses: list[dict[str, Any]], known: set[str]
) -> list[Gateway]:
    out = []
    for r in routes:
        iface = route_interface(r, addresses, known)
        gw = str(r.get("gateway") or "").split("%", 1)[0]
        try:
            address = str(ipaddress.ip_address(gw))
        except ValueError:
            address = None
        active = flag(r.get("active"))
        out.append(
            Gateway(
                name=(r.get("comment") or "").strip() or iface or gw or "default",
                interface=iface,
                address=address,
                status="up" if active else "down" if active is False else "unknown",
            )
        )
    return out


# --- clients -------------------------------------------------------------------------


def arp(c: Client) -> list[ArpEntry]:
    out = []
    for r in as_list(c.get("/ip/arp")):
        m = mac(r.get("mac-address"))
        status = str(r.get("status") or "")
        if not m or not r.get("address") or flag(r.get("invalid")) or flag(r.get("disabled")):
            continue
        if status in ("failed", "incomplete") or flag(r.get("complete")) is False:
            continue
        out.append(ArpEntry(ip=r["address"], mac=m, interface=r.get("interface") or None))
    return out


def leases(c: Client, now: datetime) -> list[DhcpLease]:
    out = []
    for r in as_list(c.get("/ip/dhcp-server/lease")):
        if r.get("status") != "bound" or flag(r.get("disabled")):
            continue
        m = mac(r.get("active-mac-address") or r.get("mac-address"))
        ip = r.get("active-address") or r.get("address")
        if not m or not ip or ":" in ip:
            continue
        expires = duration_s(r.get("expires-after"))
        out.append(
            DhcpLease(
                ip=ip,
                mac=m,
                hostname=(r.get("host-name") or "").strip() or None,
                expires_at=now + timedelta(seconds=expires) if expires else None,
            )
        )
    return sorted(out, key=lambda lease: tuple(int(p) for p in lease.ip.split(".")))


def fdb(c: Client, bridges: set[str]) -> list[FdbEntry]:
    """The bridge's learned MACs (its own MACs, ``local``, are left out)."""
    out = []
    for r in as_list(c.get("/interface/bridge/host")):
        m = mac(r.get("mac-address"))
        port = r.get("on-interface")
        if not m or not port or flag(r.get("local")) or port in bridges:
            continue
        vid = counter(r.get("vid"))
        out.append(FdbEntry(mac=m, port=port, vlan=vid if vid and vid <= 4095 else None))
    return out


def neighbors(c: Client) -> list[Neighbor]:
    """MNDP / LLDP / CDP neighbors. ``discovered-by`` may list several protocols:
    LLDP is preferred, then CDP, then MNDP."""
    out = []
    for r in as_list(c.get("/ip/neighbor")):
        port = first_name(r.get("interface"))
        if not port:
            continue
        by = names(r.get("discovered-by"))
        protocol = next((p for p in ("lldp", "cdp", "mndp") if p in by), None)
        platform = (
            " ".join(v for v in (r.get("platform"), r.get("board"), r.get("version")) if v).strip()
            or (r.get("system-description") or "").strip()
        )
        out.append(
            Neighbor(
                local_port=port,
                protocol=protocol,
                remote_name=(r.get("identity") or "").strip() or None,
                remote_port=(r.get("interface-name") or "").strip() or None,
                remote_mac=mac(r.get("mac-address")),
                remote_ip=r.get("address4") or r.get("address") or None,
                remote_platform=platform or None,
            )
        )
    return out


def band(value: Any) -> str | None:
    """'2ghz-b/g/n' / '2ghz-ax' → 2.4ghz; '5ghz-ac' → 5ghz; '6ghz-ax' → 6ghz."""
    if not isinstance(value, str):
        return None
    v = value.lower()
    if v.startswith(("2ghz", "2.4ghz")):
        return "2.4ghz"
    if v.startswith("5ghz"):
        return "5ghz"
    if v.startswith("6ghz"):
        return "6ghz"
    return None


BAND_LABEL = {"2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz"}


def wifi_label(ssid: str | None, b: str | None, interface: str | None) -> str | None:
    """'Home · 5 GHz', the network a client is on, as Omini draws it."""
    if ssid and b:
        return f"{ssid} · {BAND_LABEL[b]}"
    return ssid or interface


def signal(value: Any) -> int | None:
    """'-60', '-60@HT20-7' → -60."""
    n = number(value)
    return int(n) if n is not None and n <= 0 else None


def legacy_wireless(c: Client) -> list[WirelessClient]:
    """The ``wireless`` package (802.11n/ac): SSID and band come from the interface."""
    rows = as_list(c.get("/interface/wireless/registration-table"))
    if not rows:
        return []
    ifaces: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Forbidden, NotFound):
        ifaces = {r.get("name"): r for r in as_list(c.get("/interface/wireless"))}
    out = []
    for r in rows:
        m = mac(r.get("mac-address"))
        if not m or flag(r.get("ap")):  # another AP (WDS / bridge peer), not a client
            continue
        name = r.get("interface")
        iface = ifaces.get(name) or {}
        ssid = (iface.get("ssid") or "").strip() or None
        b = band(iface.get("band"))
        out.append(
            WirelessClient(
                mac=m,
                interface=wifi_label(ssid, b, name),
                ssid=ssid,
                band=b,
                signal_dbm=signal(r.get("signal-strength")),
                tx_rate_mbps=rate_mbps(r.get("tx-rate")),
                rx_rate_mbps=rate_mbps(r.get("rx-rate")),
            )
        )
    return out


def wifi(c: Client) -> list[WirelessClient]:
    """The ``wifi`` package (wifiwave2, RouterOS 7.13+): the registration table
    has the SSID, band and current traffic."""
    rows = as_list(c.get("/interface/wifi/registration-table"))
    if not rows:
        return []
    ifaces: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Forbidden, NotFound):
        ifaces = {r.get("name"): r for r in as_list(c.get("/interface/wifi"))}
    out = []
    for r in rows:
        m = mac(r.get("mac-address"))
        if not m:
            continue
        name = r.get("interface")
        iface = ifaces.get(name) or {}
        ssid = (r.get("ssid") or iface.get("configuration.ssid") or "").strip() or None
        b = (
            band(r.get("band"))
            or band(iface.get("channel.band"))
            or band(iface.get("configuration.band"))
        )
        tx_bps, rx_bps = counter(r.get("tx-bits-per-second")), counter(r.get("rx-bits-per-second"))
        out.append(
            WirelessClient(
                mac=m,
                interface=wifi_label(ssid, b, name),
                ssid=ssid,
                band=b,
                signal_dbm=signal(r.get("signal")),
                tx_rate_mbps=rate_mbps(r.get("tx-rate")),
                rx_rate_mbps=rate_mbps(r.get("rx-rate")),
                # The AP's transmit is what the client downloads.
                rx_bps=tx_bps,
                tx_bps=rx_bps,
            )
        )
    return out


# --- system --------------------------------------------------------------------------


def resource(c: Client) -> dict[str, Any]:
    try:
        data = c.get("/system/resource")
    except Forbidden as e:
        raise PluginError(
            "connected, but the user cannot read /system/resource: "
            "its group needs the read, api and rest-api policies"
        ) from e
    except NotFound as e:
        raise PluginError(
            f"{c.base} has no RouterOS REST API: it needs RouterOS v7.1 or later "
            "with the www-ssl (or www) service enabled"
        ) from e
    info = as_object(data)
    if "version" not in info:
        raise PluginError(f"{c.base} does not look like RouterOS (no version in /system/resource)")
    return info


def os_version(version: Any) -> str | None:
    """'7.15.3 (stable)' → 'RouterOS 7.15.3'."""
    if not isinstance(version, str) or not version.strip():
        return None
    return "RouterOS " + version.split()[0]


def memory_pct(info: dict[str, Any]) -> float | None:
    total, free = number(info.get("total-memory")), number(info.get("free-memory"))
    if not total or free is None:
        return None
    return round((total - free) / total * 100, 1)


def cpu_pct(info: dict[str, Any]) -> float | None:
    n = number(info.get("cpu-load"))
    return n if n is not None and 0 <= n <= 100 else None


def storage(info: dict[str, Any]) -> list[Storage]:
    """The flash (or disk) RouterOS runs from."""
    total, free = counter(info.get("total-hdd-space")), counter(info.get("free-hdd-space"))
    if not total or free is None:
        return []
    return [Storage(mount="flash", total_bytes=total, used_bytes=max(total - free, 0))]


def temperature_kind(name: str) -> str:
    n = name.lower()
    if n.startswith("cpu"):
        return "cpu"
    if n.startswith(("board", "switch", "phy", "sfp")):
        return "board"
    return "other"


def temperatures(c: Client) -> list[Temperature]:
    """RouterOS 7: a list of {name, value, type}; type 'C' is a temperature.
    RouterOS 6 answered one object ({temperature, cpu-temperature}); both work."""
    data = c.get("/system/health")
    rows = as_list(data)
    out = []
    if rows and "name" in rows[0]:
        for r in rows:
            name = str(r.get("name") or "")
            unit = str(r.get("type") or "")
            if unit.upper() not in ("C", "°C") and "temperature" not in name:
                continue
            celsius = number(r.get("value"))
            if celsius is None:
                continue
            out.append(Temperature(sensor=name, kind=temperature_kind(name), celsius=celsius))
        return out
    for key, value in as_object(data).items():
        if "temperature" in key and (celsius := number(value)) is not None:
            out.append(Temperature(sensor=key, kind=temperature_kind(key), celsius=celsius))
    return out


def firmware(c: Client, info: dict[str, Any], board: dict[str, Any]) -> Firmware | None:
    """What RouterOS knows from its own last update check (Omini never starts
    one: check-for-updates is never called). The latest version is only known
    when someone or a script checked. A RouterBOOT firmware newer than the one
    running (``upgrade-firmware`` > ``current-firmware``) counts as one pending update."""
    update: dict[str, Any] = {}
    with contextlib.suppress(Forbidden, NotFound):
        update = as_object(c.get("/system/package/update"))
    current = (update.get("installed-version") or "").strip() or None
    if not current and isinstance(info.get("version"), str):
        current = info["version"].split()[0]
    latest = (update.get("latest-version") or "").strip() or None
    available: bool | None = None
    count = 0
    if latest and current:
        available = latest != current
        count += 1 if available else 0
    status = str(update.get("status") or "").lower()
    if "new version is available" in status:
        available = True
        count = max(count, 1)
    elif "already up to date" in status and available is None:
        available = False
    rb_current = (board.get("current-firmware") or "").strip()
    rb_upgrade = (board.get("upgrade-firmware") or "").strip()
    if rb_current and rb_upgrade and rb_current != rb_upgrade:
        available = True
        count += 1
    if not current:
        return None
    return Firmware(
        current=current,
        latest=latest,
        update_available=available,
        updates=count if available is not None else None,
    )


# --- plugin entry points --------------------------------------------------------------


def device_key(ifaces: list[Interface], raw: list[dict[str, Any]], host: str) -> str:
    """ether1's factory MAC (stable), else the lowest port MAC."""
    for r in raw:
        if r.get("default-name") == "ether1" and mac(r.get("mac-address")):
            return mac(r.get("mac-address")) or host
    port_macs = sorted(i.mac for i in ifaces if i.mac and i.type == "ethernet")
    if port_macs:
        return port_macs[0]
    macs = sorted(i.mac for i in ifaces if i.mac)
    return macs[0] if macs else host


def collect(cfg: Config) -> list[Device]:
    c = client_from(cfg)
    try:
        info = resource(c)
        missing: list[str] = []
        identity = optional("identity", missing, lambda: as_object(c.get("/system/identity")), {})
        board = optional(
            "routerboard", missing, lambda: as_object(c.get("/system/routerboard")), {}
        )
        raw = optional(
            "interfaces",
            missing,
            lambda: read_interfaces(c, missing),
            None,
        )
        routes = optional("routes", missing, lambda: default_routes(c), [])
        ifaces: list[Interface] = []
        gws: list[Gateway] = []
        bridges: set[str] = set()
        vlans: list[Vlan] = []
        if raw:
            known = {r.get("name") for r in raw["interfaces"] if r.get("name")}
            gws = gateways(routes, raw["addresses"], known)
            wan = {g.interface for g in gws if g.interface}
            ports = [
                r["name"]
                for r in raw["interfaces"]
                if r.get("name") and r.get("type") == "ether" and not flag(r.get("disabled"))
            ]
            monitor = optional("ethernet monitor", missing, lambda: ethernet_monitor(c, ports), {})
            ifaces = build_interfaces(raw, monitor, wan)
            bridges = {i.name for i in ifaces if i.type == "bridge"}
            vlans = device_vlans(raw)
        now = datetime.now(timezone.utc)
        clients = optional("wifi clients", missing, lambda: wifi(c), []) + optional(
            "wireless clients", missing, lambda: legacy_wireless(c), []
        )
        host = urlparse(c.base).hostname or c.base
        has_wireless = any(i.type == "wireless" for i in ifaces)
        role = "router" if routes else "ap" if has_wireless else "switch"
        model = board.get("model") or info.get("board-name") or None
        ips = sorted(
            {ip.split("/")[0] for i in ifaces for ip in (i.ips or []) if "." in ip},
            key=lambda ip: tuple(int(p) for p in ip.split(".")),
        )
        device = Device(
            key=device_key(ifaces, raw["interfaces"] if raw else [], host),
            name=(identity.get("name") or "").strip() or host,
            host=host,
            role=role,
            vendor="MikroTik",
            model=model,
            os_version=os_version(info.get("version")),
            serial=(board.get("serial-number") or "").strip() or None,
            uptime_s=duration_s(info.get("uptime")),
            cpu_pct=cpu_pct(info),
            mem_pct=memory_pct(info),
            macs=sorted({i.mac for i in ifaces if i.mac}) or None,
            ips=ips or None,
            interfaces=ifaces or None,
            neighbors=optional("neighbors", missing, lambda: neighbors(c), []) or None,
            fdb=optional("bridge hosts", missing, lambda: fdb(c, bridges), []) or None,
            arp=optional("ARP", missing, lambda: arp(c), []) or None,
            dhcp_leases=optional("DHCP leases", missing, lambda: leases(c, now), []) or None,
            wireless_clients=clients or None,
            gateways=gws or None,
            vlans=vlans or None,
            temperatures=optional("health", missing, lambda: temperatures(c), []) or None,
            storage=storage(info) or None,
            firmware=optional("updates", missing, lambda: firmware(c, info, board), None),
        )
        if missing:
            log.warning("could not read (permission): %s", ", ".join(sorted(set(missing))))
        return [device]
    finally:
        c.close()


def test(cfg: Config) -> str:
    c = client_from(cfg)
    try:
        info = resource(c)
        missing: list[str] = []
        identity = optional("identity", missing, lambda: as_object(c.get("/system/identity")), {})
        optional("interfaces", missing, lambda: c.get("/interface"), None)
        optional("ARP", missing, lambda: c.get("/ip/arp"), None)
        optional("routes", missing, lambda: c.get("/ip/route"), None)
        name = (identity.get("name") or "").strip() or c.base
        details = ", ".join(
            v for v in (os_version(info.get("version")), info.get("board-name")) if v
        )
        msg = f"Connected to {name} ({details})" if details else f"Connected to {name}"
        if missing:
            msg += ". Cannot read: " + ", ".join(sorted(set(missing)))
        return msg
    finally:
        c.close()
