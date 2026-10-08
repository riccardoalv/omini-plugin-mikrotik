import pytest
from omini_sdk import PluginError

from omini_mikrotik.collect import (
    collect,
    duration_s,
    mac,
    rate_mbps,
    speed_setting,
    vlan_ids,
)
from omini_mikrotik.collect import test as connection_test


def test_reads_the_router(routeros, cfg):
    [rt] = collect(cfg)
    assert rt.key == "48:a9:8a:11:22:01"  # ether1's MAC, normalized
    assert rt.name == "core-router" and rt.host == "192.168.88.1"
    assert rt.role == "router" and rt.vendor == "MikroTik"
    assert rt.model == "RB5009UG+S+" and rt.serial == "HD9081234AB"
    assert rt.os_version == "RouterOS 7.15.3"
    assert rt.uptime_s == ((7 + 2) * 24 + 3) * 3600 + 4 * 60 + 5
    assert rt.cpu_pct == 7 and rt.mem_pct == 25.0
    assert rt.ips == ["192.168.20.1", "192.168.88.1", "203.0.113.10"]


def test_ports(routeros, cfg):
    [rt] = collect(cfg)
    ports = {i.name: i for i in rt.interfaces}
    wan, lan, office, sfp = ports["ether1"], ports["ether2"], ports["office"], ports["sfp-sfpplus1"]
    assert (wan.type, wan.connector, wan.up, wan.speed_mbps, wan.duplex) == (
        "ethernet",
        "rj45",
        True,
        2500,
        "full",
    )
    assert wan.wan is True and wan.description == "ISP" and wan.rx_bytes == 98765432100
    assert wan.ips == ["203.0.113.10/24"]
    assert lan.speed_mbps == 1000 and lan.rx_errors == 2 and lan.wan is None
    # Renamed port: the connector comes from its factory name (ether3).
    assert (office.connector, office.up, office.speed_mbps) == ("rj45", False, None)
    assert (sfp.connector, sfp.speed_mbps, sfp.description) == ("sfp", 10000, "Uplink to the rack")
    bridge = ports["bridge"]
    assert bridge.type == "bridge" and bridge.speed_mbps == 10000  # fastest member up
    assert bridge.members == ["ether2", "office", "sfp-sfpplus1", "wifi1"]
    vlan = ports["vlan20"]
    assert (vlan.type, vlan.vlan, vlan.parent, vlan.ips) == (
        "vlan",
        20,
        "bridge",
        ["192.168.20.1/24"],
    )
    assert ports["wifi1"].type == "wireless" and ports["lo"].type == "loopback"
    assert ports["lo"].mac is None  # 00:00:00:00:00:00


def test_sfp_diagnostics(routeros, cfg):
    [rt] = collect(cfg)
    t = {i.name: i for i in rt.interfaces}["sfp-sfpplus1"].transceiver
    assert (t.vendor, t.part, t.serial, t.wavelength_nm) == (
        "Mikrotik",
        "S+85DLC03D",
        "SG85M31401687",
        850,
    )
    assert (t.temperature_c, t.voltage_v, t.bias_ma, t.tx_power_dbm, t.rx_power_dbm) == (
        33,
        3.237,
        6,
        -2.5,
        -3.1,
    )
    assert {i.name: i for i in rt.interfaces}["ether1"].transceiver is None


def test_monitor_is_one_read_only_post(routeros, cfg):
    collect(cfg)
    posts = [c for c in routeros.calls if c[0] == "POST"]
    assert posts == [("POST", "/rest/interface/ethernet/monitor")]


def test_vlans(routeros, cfg):
    [rt] = collect(cfg)
    ports = {i.name: i for i in rt.interfaces}
    assert ports["office"].vlans.untagged == 20 and ports["office"].vlans.tagged is None
    sfp = ports["sfp-sfpplus1"].vlans
    assert sfp.untagged == 1 and [v.root for v in sfp.tagged] == [20, 30, 31]
    assert ports["ether1"].vlans is None  # not a bridge port
    assert [(v.id, v.name, v.subnet) for v in rt.vlans] == [
        (20, "vlan20", "192.168.20.0/24"),
        (30, None, None),
        (31, None, None),
    ]


def test_clients(routeros, cfg):
    [rt] = collect(cfg)
    assert [(a.ip, a.mac, a.interface) for a in rt.arp] == [
        ("192.168.88.10", "aa:bb:cc:00:00:01", "bridge"),
        ("203.0.113.1", "00:11:22:33:44:55", "ether1"),
    ]
    assert [(lease.ip, lease.mac, lease.hostname) for lease in rt.dhcp_leases] == [
        ("192.168.88.5", "aa:bb:cc:00:00:03", None),
        ("192.168.88.10", "aa:bb:cc:00:00:01", "laptop"),
    ]  # the "waiting" lease is left out
    assert rt.dhcp_leases[1].expires_at is not None
    assert [(f.mac, f.port, f.vlan) for f in rt.fdb] == [
        ("aa:bb:cc:00:00:01", "ether2", 1),
        ("aa:bb:cc:00:00:02", "sfp-sfpplus1", 20),
        ("aa:bb:cc:00:00:03", "wifi1", 1),
    ]  # the bridge's own (local) MAC is left out


def test_wifi_clients(routeros, cfg):
    [rt] = collect(cfg)
    [w] = rt.wireless_clients
    assert (w.mac, w.interface, w.ssid, w.band, w.signal_dbm) == (
        "aa:bb:cc:00:00:03",
        "Home · 5 GHz",
        "Home",
        "5ghz",
        -58,
    )
    assert (w.tx_rate_mbps, w.rx_rate_mbps) == (1200, 864.8)
    assert (w.rx_bps, w.tx_bps) == (1_500_000, 250_000)  # the AP's tx is the client's download


def test_legacy_wireless_clients(routeros, cfg):
    del routeros.routes["/rest/interface/wifi/registration-table"]
    routeros.routes["/rest/interface/wireless"] = [
        {"name": "wlan1", "ssid": "Office", "band": "2ghz-b/g/n", "disabled": "false"}
    ]
    routeros.routes["/rest/interface/wireless/registration-table"] = [
        {
            "interface": "wlan1",
            "mac-address": "AA:BB:CC:00:00:07",
            "ap": "false",
            "signal-strength": "-71@HT20-7",
            "tx-rate": "130Mbps-20MHz/2S/SGI",
            "rx-rate": "65Mbps-20MHz/1S",
        },
        {"interface": "wlan1", "mac-address": "AA:BB:CC:00:00:08", "ap": "true"},
    ]
    [rt] = collect(cfg)
    [w] = rt.wireless_clients
    assert (w.mac, w.interface, w.band, w.signal_dbm, w.tx_rate_mbps, w.rx_rate_mbps) == (
        "aa:bb:cc:00:00:07",
        "Office · 2.4 GHz",
        "2.4ghz",
        -71,
        130,
        65,
    )


def test_neighbors(routeros, cfg):
    [rt] = collect(cfg)
    sw, cisco = rt.neighbors
    assert (sw.local_port, sw.protocol, sw.remote_name, sw.remote_port, sw.remote_mac) == (
        "sfp-sfpplus1",
        "lldp",
        "rack-switch",
        "sfp-sfpplus1",
        "48:a9:8a:99:00:01",
    )
    assert sw.remote_ip == "192.168.88.2"
    assert sw.remote_platform == "MikroTik CRS309-1G-8S+ 7.15.3 (stable)"
    assert (cisco.local_port, cisco.protocol, cisco.remote_name, cisco.remote_mac) == (
        "ether2",
        "cdp",
        None,
        "aa:bb:cc:00:00:30",
    )
    assert cisco.remote_platform == "Cisco IOS Software"


def test_gateways(routeros, cfg):
    [rt] = collect(cfg)
    assert [(g.name, g.interface, g.address, g.status) for g in rt.gateways] == [
        ("ether1", "ether1", "203.0.113.1", "up"),
        ("Backup LTE", None, "198.51.100.1", "down"),
    ]  # the default route of another routing table is not an internet uplink


def test_pppoe_wan_follows_its_port(routeros, cfg):
    routeros.routes["/rest/ip/route"] = [
        {
            "dst-address": "0.0.0.0/0",
            "gateway": "pppoe-out1",
            "immediate-gw": "pppoe-out1",
            "routing-table": "main",
            "active": "true",
            "disabled": "false",
        }
    ]
    routeros.routes["/rest/interface"].append(
        {
            "name": "pppoe-out1",
            "type": "pppoe-out",
            "running": "true",
            "disabled": "false",
            "rx-byte": "10",
            "tx-byte": "20",
        }
    )
    routeros.routes["/rest/interface/pppoe-client"] = [
        {"name": "pppoe-out1", "interface": "ether1", "disabled": "false", "running": "true"}
    ]
    [rt] = collect(cfg)
    ppp = {i.name: i for i in rt.interfaces}["pppoe-out1"]
    assert (ppp.type, ppp.wan, ppp.parent, ppp.speed_mbps) == ("tunnel", True, "ether1", 2500)
    assert [(g.name, g.interface, g.address) for g in rt.gateways] == [
        ("pppoe-out1", "pppoe-out1", None)
    ]


def test_health_and_updates(routeros, cfg):
    [rt] = collect(cfg)
    assert [(t.sensor, t.kind, t.celsius) for t in rt.temperatures] == [
        ("cpu-temperature", "cpu", 48),
        ("board-temperature1", "board", 41),
    ]
    [disk] = rt.storage
    assert (disk.mount, disk.total_bytes, disk.used_bytes) == ("flash", 1073741824, 167772160)
    fw = rt.firmware
    assert (fw.current, fw.latest, fw.update_available, fw.updates) == ("7.15.3", "7.16.1", True, 2)
    assert fw.checked_at is None  # RouterOS does not say when it checked
    assert ("GET", "/rest/system/package/update") in routeros.calls
    assert not any("check-for-updates" in path for _, path in routeros.calls)


def test_firmware_without_a_check(routeros, cfg):
    routeros.routes["/rest/system/package/update"] = {
        "channel": "stable",
        "installed-version": "7.15.3",
    }
    routeros.routes["/rest/system/routerboard"]["upgrade-firmware"] = "7.14.3"
    [rt] = collect(cfg)
    assert (rt.firmware.current, rt.firmware.latest, rt.firmware.update_available) == (
        "7.15.3",
        None,
        None,
    )


def test_routeros6_health_object(routeros, cfg):
    routeros.routes["/rest/system/health"] = {"voltage": "24", "temperature": "39"}
    [rt] = collect(cfg)
    assert [(t.sensor, t.kind, t.celsius) for t in rt.temperatures] == [
        ("temperature", "other", 39)
    ]


def test_optional_menus_do_not_fail_the_collection(routeros, cfg):
    routeros.forbidden = {"/rest/ip/neighbor", "/rest/system/health", "/rest/ip/dhcp-server"}
    del routeros.routes["/rest/interface/bridge/vlan"]
    routeros.routes.pop("/rest/interface/ethernet/monitor")
    routeros.routes["/rest/interface/ethernet/monitor"] = []
    [rt] = collect(cfg)
    assert rt.neighbors is None and rt.temperatures is None and rt.dhcp_leases is None
    assert rt.arp and rt.interfaces and rt.fdb
    sfp = {i.name: i for i in rt.interfaces}["sfp-sfpplus1"]
    assert sfp.speed_mbps == 10000  # fixed speed (no auto-negotiation) without the monitor
    assert sfp.vlans.tagged is None  # no bridge VLAN table: only the PVID is known
    msg = connection_test(cfg)
    assert msg == "Connected to core-router (RouterOS 7.15.3, RB5009UG+S+)"
    routeros.forbidden.add("/rest/ip/arp")
    assert connection_test(cfg).endswith(". Cannot read: ARP")


def test_a_failing_monitor_does_not_fail_the_collection(routeros, cfg, monkeypatch):
    import omini_mikrotik.collect as m

    def boom(*_):
        raise RuntimeError("unexpected answer")

    monkeypatch.setattr(m, "ethernet_monitor", boom)
    [rt] = collect(cfg)
    ports = {i.name: i for i in rt.interfaces}
    assert ports["ether1"].speed_mbps is None and ports["ether2"].up is True


def test_switch_and_ap_roles(routeros, cfg):
    routeros.routes["/rest/ip/route"] = []
    [rt] = collect(cfg)
    assert rt.role == "ap" and rt.gateways is None  # has Wi-Fi, no default route
    routeros.routes["/rest/interface"] = [
        r for r in routeros.routes["/rest/interface"] if r["type"] != "wifi"
    ]
    [rt] = collect(cfg)
    assert rt.role == "switch"


def test_connection_test(routeros, cfg):
    assert connection_test(cfg) == "Connected to core-router (RouterOS 7.15.3, RB5009UG+S+)"


def test_errors_the_user_can_act_on(routeros, cfg):
    routeros.auth = ("omini", "other")
    with pytest.raises(PluginError, match="rejected the username or password"):
        collect(cfg)
    with pytest.raises(PluginError, match="rejected"):
        connection_test(cfg)
    routeros.auth = ("omini", "secret")
    routeros.forbidden = {"/rest/system/resource"}
    with pytest.raises(PluginError, match="read, api and rest-api"):
        collect(cfg)
    routeros.forbidden = set()
    del routeros.routes["/rest/system/resource"]
    with pytest.raises(PluginError, match=r"RouterOS v7\.1 or later"):
        collect(cfg)


def test_password_never_in_errors(routeros, cfg):
    routeros.auth = ("omini", "other")
    with pytest.raises(PluginError) as e:
        collect(cfg)
    assert "secret" not in str(e.value)


def test_not_routeros(monkeypatch, cfg):
    import httpx

    import omini_mikrotik.collect as m
    from omini_mikrotik.client import Client

    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>hello</html>"))
    monkeypatch.setattr(m, "Client", lambda *a, **k: Client(*a, **k, transport=transport))
    with pytest.raises(PluginError, match="did not answer with JSON"):
        collect(cfg)


def test_unreachable(monkeypatch, cfg):
    import httpx

    import omini_mikrotik.collect as m
    from omini_mikrotik.client import Client

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(refuse)
    monkeypatch.setattr(m, "Client", lambda *a, **k: Client(*a, **k, transport=transport))
    with pytest.raises(PluginError, match="cannot connect"):
        connection_test(cfg)


def test_required_fields():
    from omini_sdk import Config

    with pytest.raises(PluginError, match="required"):
        collect(Config({"url": "https://192.168.88.1"}))


def test_parsers():
    assert mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert mac(" 48:A9:8A:11:22:01 ") == "48:a9:8a:11:22:01"
    assert mac("00:00:00:00:00:00") is None and mac("not a mac") is None and mac(None) is None
    assert rate_mbps("1Gbps") == 1000 and rate_mbps("2.5Gbps") == 2500
    assert rate_mbps("100Mbps") == 100 and rate_mbps("54Mbps-20MHz") == 54
    assert rate_mbps("") is None and rate_mbps(None) is None
    assert speed_setting("1G-baseT-full") == 1000 and speed_setting("100M-baseT-half") == 100
    assert speed_setting("2.5G-baseT") == 2500 and speed_setting("auto") is None
    assert duration_s("1w2d3h4m5s") == 788645 and duration_s("9m57s") == 597
    assert duration_s("2d03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert duration_s("00:09:57") == 597 and duration_s("") is None and duration_s("never") is None
    assert vlan_ids("10,20,30-32") == [10, 20, 30, 31, 32] and vlan_ids("") == []
