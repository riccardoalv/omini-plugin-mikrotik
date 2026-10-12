"""Local DNS names, Wake-on-LAN, paused devices, STP, bonds and the
configuration backup, against the fake RouterOS (a defconf router: WAN on
ether1, the LAN on the bridge, VLAN 20 on the bridge)."""

import pytest
from omini_sdk import Change, PluginError

from omini_mikrotik.backup import clean
from omini_mikrotik.collect import apply, backup, bridge_id, collect

KEY = "48:a9:8a:11:22:01"
TV = "aa:bb:cc:00:00:01"


def change(kind, value):
    return Change(device=KEY, kind=kind, value=value)


def ifaces(device):
    return {i.name: i for i in device.interfaces}


# --- DNS -------------------------------------------------------------------------


def test_reads_the_local_names_and_domain(routeros, cfg):
    [rt] = collect(cfg)
    # A records only: no regexp, CNAME, AAAA or disabled entry; names lowercase.
    assert [(r.name, r.ip) for r in rt.dns_records] == [
        ("nas.lan", "192.168.88.20"),
        ("router.lan", "192.168.88.1"),
    ]
    assert rt.dns_domain == "lan"  # the LAN's DHCP network hands it out


def test_dns_domain_unknown_without_one(routeros, cfg):
    routeros.rows("/ip/dhcp-server/network")[0]["domain"] = ""
    [rt] = collect(cfg)
    assert rt.dns_domain is None


def test_creates_a_name_marked_as_omini(routeros, cfg):
    [rt] = apply(cfg, change("dns.set", {"name": "Printer.lan.", "ip": "192.168.88.30"}))
    row = routeros.row("/ip/dns/static", name="printer.lan", address="192.168.88.30")
    assert row is not None and row["comment"] == "Omini"
    assert ("printer.lan", "192.168.88.30") in [(r.name, r.ip) for r in rt.dns_records]


def test_changes_the_address_of_a_name(routeros, cfg):
    before = len(routeros.rows("/ip/dns/static"))
    apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.88.21"}))
    assert routeros.row("/ip/dns/static", name="nas.lan")["address"] == "192.168.88.21"
    assert len(routeros.rows("/ip/dns/static")) == before  # in place
    assert routeros.writes == [("PATCH", "/rest/ip/dns/static/*2", {"address": "192.168.88.21"})]


def test_a_name_has_one_address(routeros, cfg):
    routeros.rows("/ip/dns/static").append(
        {".id": "*9", "name": "nas.lan", "address": "192.168.88.99", "disabled": "false"}
    )
    [rt] = apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.88.20"}))
    assert [r.ip for r in rt.dns_records if r.name == "nas.lan"] == ["192.168.88.20"]


def test_deletes_a_name(routeros, cfg):
    [rt] = apply(cfg, change("dns.delete", {"name": "nas.lan"}))
    assert routeros.row("/ip/dns/static", name="nas.lan") is None
    assert "nas.lan" not in [r.name for r in rt.dns_records]
    assert routeros.row("/ip/dns/static", name="www.lan") is not None  # the CNAME stays


def test_dns_refusals_and_undo(routeros, cfg):
    with pytest.raises(PluginError, match="not a DNS name"):
        apply(cfg, change("dns.set", {"name": "bad name", "ip": "192.168.88.30"}))
    with pytest.raises(PluginError, match="not an IPv4 address"):
        apply(cfg, change("dns.set", {"name": "a.lan", "ip": "fd00::1"}))
    assert routeros.writes == []
    routeros.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not apply the DNS name a\.lan"):
        apply(cfg, change("dns.set", {"name": "a.lan", "ip": "192.168.88.30"}))
    routeros.ignore_changes = False
    routeros.read_only = True
    with pytest.raises(PluginError, match="lacks the write policy"):
        apply(cfg, change("dns.delete", {"name": "nas.lan"}))


# --- Wake-on-LAN -----------------------------------------------------------------------


def test_wakes_on_the_interface_of_the_address(routeros, cfg):
    apply(cfg, change("wol.send", {"mac": "aa-bb-cc-00-00-07", "ip": "192.168.20.40"}))
    apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:08"}))  # no address: the LAN
    assert routeros.wol == [
        {"mac": "AA:BB:CC:00:00:07", "interface": "vlan20"},
        {"mac": "AA:BB:CC:00:00:08", "interface": "bridge"},
    ]


def test_wake_refusals(routeros, cfg):
    with pytest.raises(PluginError, match=r"no network that holds 10\.9\.9\.9"):
        apply(cfg, change("wol.send", {"mac": TV, "ip": "10.9.9.9"}))
    with pytest.raises(PluginError, match="not a MAC"):
        apply(cfg, change("wol.send", {"mac": "nope"}))
    routeros.read_only = True
    with pytest.raises(PluginError, match="cannot run /tool/wol"):
        apply(cfg, change("wol.send", {"mac": TV}))
    assert routeros.wol == []


# --- pause -------------------------------------------------------------------------


def paused_list(routeros):
    return [
        (a["address"], a["comment"])
        for a in routeros.rows("/ip/firewall/address-list")
        if a["list"] == "omini_paused"
    ]


def pause_rules(routeros):
    return [
        r
        for r in routeros.rows("/ip/firewall/filter")
        if r.get("src-address-list") == "omini_paused"
    ]


def test_pauses_a_device(routeros, cfg):
    value = {"mac": TV, "ips": ["192.168.88.10"], "name": "Living room TV"}
    [rt] = apply(cfg, change("pause.set", value))
    assert [p.model_dump(exclude_none=True) for p in rt.paused] == [value]
    assert paused_list(routeros) == [("192.168.88.10", f"Omini {TV} Living room TV")]
    # One rule, first in the forward chain (before fasttrack and "accept established").
    [rule] = pause_rules(routeros)
    assert rule["dst-address-list"] == "!omini-private" and rule["action"] == "drop"
    forward = [r for r in routeros.rows("/ip/firewall/filter") if r["chain"] == "forward"]
    assert forward[0] is rule
    private = {
        a["address"]
        for a in routeros.rows("/ip/firewall/address-list")
        if a["list"] == "omini-private"
    }
    assert private == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
    # Its open connections are cut (TCP and ICMP from it; not another device's).
    gone = {p for m, p, _ in routeros.writes if m == "DELETE" and "connection" in p}
    assert gone == {f"/rest/ip/firewall/connection/*{n}" for n in (1001, 1002, 1004)}


def test_a_new_address_replaces_the_old_one(routeros, cfg):
    apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.10"], "name": "TV"}))
    other = {"mac": "aa:bb:cc:00:00:02", "ips": ["192.168.88.11"], "name": "Console"}
    apply(cfg, change("pause.set", other))
    [rt] = apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.12"], "name": "TV"}))
    assert {p.mac: p.ips for p in rt.paused} == {
        TV: ["192.168.88.12"],
        "aa:bb:cc:00:00:02": ["192.168.88.11"],
    }
    assert len(pause_rules(routeros)) == 1


def test_resuming_the_last_device_removes_the_rule(routeros, cfg):
    apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.10"], "name": "TV"}))
    other = {"mac": "aa:bb:cc:00:00:02", "ips": ["192.168.88.11"], "name": ""}
    apply(cfg, change("pause.set", other))
    [rt] = apply(cfg, change("pause.delete", {"mac": TV}))
    assert [p.mac for p in rt.paused] == ["aa:bb:cc:00:00:02"]
    assert len(pause_rules(routeros)) == 1
    [rt] = apply(cfg, change("pause.delete", {"mac": "aa:bb:cc:00:00:02"}))
    assert rt.paused is None and pause_rules(routeros) == [] and paused_list(routeros) == []


def test_pause_refusals(routeros, cfg):
    with pytest.raises(PluginError, match="router's own address"):
        apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.1"]}))
    with pytest.raises(PluginError, match="no address to pause"):
        apply(cfg, change("pause.set", {"mac": TV, "ips": []}))
    assert routeros.writes == []
    routeros.routes["/rest/ip/route"] = []  # not a router
    with pytest.raises(PluginError, match="does not route"):
        apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.10"]}))


def test_a_pause_routeros_did_not_take_is_undone(routeros, cfg):
    routeros.fail["POST /rest/ip/firewall/filter/add"] = "failure: bad rule"
    with pytest.raises(PluginError, match="bad rule"):
        apply(cfg, change("pause.set", {"mac": TV, "ips": ["192.168.88.10"]}))
    assert paused_list(routeros) == [] and pause_rules(routeros) == []
    assert not any(a["list"] == "omini-private" for a in routeros.rows("/ip/firewall/address-list"))


# --- STP and bonds ------------------------------------------------------------------------


def test_reads_stp_roles_and_the_root_bridge(routeros, cfg):
    [rt] = collect(cfg)
    ports = ifaces(rt)
    stp = {n: ports[n].stp.model_dump() for n in ("ether2", "office", "sfp-sfpplus1", "wifi1")}
    assert stp == {
        "ether2": {"state": "forwarding", "role": "designated"},
        "office": {"state": "disabled", "role": "disabled"},
        "sfp-sfpplus1": {"state": "forwarding", "role": "root"},
        "wifi1": {"state": "blocking", "role": "alternate"},
    }
    assert ports["ether1"].stp is None and ports["bridge"].stp is None
    assert rt.stp_root == "4096.64:d1:54:00:00:01"


def test_no_stp_when_the_bridge_runs_none(routeros, cfg):
    routeros.rows("/interface/bridge")[0]["protocol-mode"] = "none"
    [rt] = collect(cfg)
    assert all(i.stp is None for i in rt.interfaces) and rt.stp_root is None
    assert ("POST", "/rest/interface/bridge/monitor") not in routeros.calls


def test_bridge_ids():
    assert bridge_id("0x8000.48:A9:8A:11:22:02") == "32768.48:a9:8a:11:22:02"
    assert bridge_id("4096.48:a9:8a:11:22:02") == "4096.48:a9:8a:11:22:02"
    assert bridge_id("") is None and bridge_id("none") is None


def test_bond_members_report_their_lag(routeros, cfg):
    routeros.routes["/rest/interface"].append(
        {".id": "*20", "name": "bond1", "type": "bond", "running": "true", "disabled": "false"}
    )
    routeros.routes["/rest/interface/bonding"] = [
        {".id": "*20", "name": "bond1", "slaves": "ether2,office", "mode": "802.3ad"}
    ]
    [rt] = collect(cfg)
    ports = ifaces(rt)
    assert ports["bond1"].type == "lag" and ports["bond1"].members == ["ether2", "office"]
    assert ports["ether2"].lag == "bond1" and ports["office"].lag == "bond1"
    assert ports["ether1"].lag is None


# --- backup -------------------------------------------------------------------------------


def test_backup_is_the_export_without_its_time_stamp(routeros, cfg):
    [b] = backup(cfg)
    assert b.device == KEY and b.format == "text"
    assert not b.content.startswith("# 2024")
    assert b.content.startswith("# software id = 7XKD-AB12\n")
    assert "/ip dns static add address=192.168.88.20" in b.content
    assert routeros.executes == [{"script": "/export terse", "as-string": ""}]
    # Exported again a minute later, nothing changed: the same backup.
    routeros.export = routeros.export.replace("14:17:45", "14:18:51")
    assert backup(cfg) == [b]
    # A configuration change: a different one.
    routeros.export += "/ip dns static add address=192.168.88.30 name=tv.lan\n"
    assert backup(cfg)[0].content != b.content


def test_backup_falls_back_to_as_string_true(routeros, cfg):
    routeros.execute_answers[""] = (400, {"error": 400, "detail": "expected end of command"})
    [b] = backup(cfg)
    assert "/system identity set name=core-router" in b.content
    assert [e["as-string"] for e in routeros.executes] == ["", "true"]


def test_backup_errors(routeros, cfg):
    routeros.forbidden.add("/rest/execute")
    with pytest.raises(PluginError, match="cannot export the configuration"):
        backup(cfg)
    routeros.forbidden.clear()
    routeros.execute_answers = {"": (200, {"ret": ""}), "true": (200, [])}
    with pytest.raises(PluginError, match="did not answer the configuration export"):
        backup(cfg)


def test_clean_export():
    raw = "# nov/29/2024 14:17:45 by RouterOS 7.10\r\n# software id = X\r\n/a b  \r\n\r\n"
    assert clean(raw) == "# software id = X\n/a b\n"
