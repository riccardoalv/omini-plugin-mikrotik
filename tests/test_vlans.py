"""The switch side: port.vlans, vlan.set and vlan.delete on a bridge with VLAN
filtering, against a fake RouterOS that keeps what is written."""

import pytest
from omini_sdk import Change, PluginError

from omini_mikrotik.collect import apply, collect

KEY = "48:a9:8a:11:22:01"


def change(kind, value, port=None):
    return Change(device=KEY, kind=kind, port=port, value=value)


def vlans_of(device, port):
    v = next(i for i in device.interfaces if i.name == port).vlans
    if v is None:
        return None
    return v.untagged, [t.root for t in v.tagged or []]


def entry(routeros, vid):
    return routeros.row("/interface/bridge/vlan", **{"vlan-ids": vid})


def port_row(routeros, name):
    return routeros.row("/interface/bridge/port", interface=name)


def test_collect_reports_the_vlans_of_bridge_ports_only(routeros, cfg):
    [rt] = collect(cfg)
    assert vlans_of(rt, "office") == (20, [])
    assert vlans_of(rt, "sfp-sfpplus1") == (1, [20, 30, 31])
    assert vlans_of(rt, "bridge") is None  # the bridge itself is no port to change
    assert vlans_of(rt, "ether1") is None
    # A trunk without a native VLAN has no untagged one, whatever its PVID.
    port_row(routeros, "sfp-sfpplus1")["frame-types"] = "admit-only-vlan-tagged"
    [rt] = collect(cfg)
    assert vlans_of(rt, "sfp-sfpplus1") == (None, [20, 30, 31])


def test_without_vlan_filtering_ports_have_no_vlans(routeros, cfg):
    routeros.rows("/interface/bridge")[0]["vlan-filtering"] = "false"
    [rt] = collect(cfg)
    assert all(i.vlans is None for i in rt.interfaces)


def test_makes_a_port_an_access_port(routeros, cfg):
    [rt] = apply(cfg, change("port.vlans", {"untagged": 20, "tagged": None}, "ether2"))
    assert routeros.writes == [
        (
            "PATCH",
            "/rest/interface/bridge/port/*0",
            {"frame-types": "admit-only-untagged-and-priority-tagged", "pvid": "20"},
        )
    ]
    assert vlans_of(rt, "ether2") == (20, [])


def test_makes_a_port_a_trunk_with_a_native_vlan(routeros, cfg):
    [rt] = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20]}, "ether2"))
    # ether2 was admit-all on PVID 1 already: only the VLAN 20 entry changes.
    assert routeros.writes == [
        ("PATCH", "/rest/interface/bridge/vlan/*1", {"tagged": "bridge,sfp-sfpplus1,ether2"})
    ]
    assert vlans_of(rt, "ether2") == (1, [20])


def test_a_down_port_reports_and_verifies_its_configured_vlans(routeros, cfg):
    # RouterOS leaves a port that is down out of current-tagged.
    for vid in ("20", "30-31"):
        e = entry(routeros, vid)
        e["current-tagged"] = ",".join(p for p in e["tagged"].split(",") if p != "sfp-sfpplus1")
    [rt] = collect(cfg)
    assert vlans_of(rt, "sfp-sfpplus1") == (1, [20, 30, 31])
    [rt] = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20]}, "office"))
    assert vlans_of(rt, "office") == (1, [20])


def test_an_access_port_becomes_a_tagged_only_trunk(routeros, cfg):
    [rt] = apply(cfg, change("port.vlans", {"untagged": None, "tagged": [20]}, "office"))
    e = entry(routeros, "20")
    assert e["tagged"] == "bridge,sfp-sfpplus1,office" and e["untagged"] == ""
    assert port_row(routeros, "office")["frame-types"] == "admit-only-vlan-tagged"
    assert port_row(routeros, "office")["pvid"] == "20"  # left as it was
    assert vlans_of(rt, "office") == (None, [20])
    # Tagged first, then the port, then what it leaves: never cut off in between.
    assert [w[1] for w in routeros.writes] == [
        "/rest/interface/bridge/vlan/*1",
        "/rest/interface/bridge/port/*1",
    ]


def test_a_tagged_vlan_without_an_entry_gets_one(routeros, cfg):
    routeros.rows("/interface/bridge/vlan").remove(entry(routeros, "20"))
    [rt] = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20]}, "ether2"))
    assert routeros.writes[0] == (
        "PUT",
        "/rest/interface/bridge/vlan",
        {"bridge": "bridge", "vlan-ids": "20", "tagged": "ether2"},
    )
    assert vlans_of(rt, "ether2") == (1, [20])


def test_a_port_joins_a_whole_range_entry(routeros, cfg):
    [rt] = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [30, 31]}, "ether2"))
    assert entry(routeros, "30-31")["tagged"] == "sfp-sfpplus1,ether2"
    assert vlans_of(rt, "ether2") == (1, [30, 31])


def test_an_entry_shared_by_several_vlans_is_not_split(routeros, cfg):
    with pytest.raises(PluginError, match="share one entry"):
        apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20, 30]}, "ether2"))
    with pytest.raises(PluginError, match=r"split it .*WinBox"):
        apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20, 31]}, "sfp-sfpplus1"))
    assert routeros.writes == []
    # Leaving the whole range is fine: only this port changes.
    [rt] = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20]}, "sfp-sfpplus1"))
    assert entry(routeros, "30-31")["tagged"] == ""
    assert vlans_of(rt, "sfp-sfpplus1") == (1, [20])


def test_vlan_filtering_off_is_refused_with_how_to_turn_it_on(routeros, cfg):
    routeros.rows("/interface/bridge")[0]["vlan-filtering"] = "false"
    for c in (
        change("port.vlans", {"untagged": 20}, "ether2"),
        change("vlan.set", {"id": 40, "name": "Cameras"}),
    ):
        with pytest.raises(PluginError) as e:
            apply(cfg, c)
        assert "VLAN filtering is off on bridge" in str(e.value)
        assert "WinBox or WebFig: Bridge → bridge → VLAN → VLAN Filtering" in str(e.value)
    assert routeros.writes == []  # never turned on by Omini


def test_bad_values_and_ports(routeros, cfg):
    with pytest.raises(PluginError, match="not a bridge port"):
        apply(cfg, change("port.vlans", {"untagged": 20}, "ether1"))
    with pytest.raises(PluginError, match="both untagged and tagged"):
        apply(cfg, change("port.vlans", {"untagged": 20, "tagged": [20]}, "ether2"))
    with pytest.raises(PluginError, match="needs an untagged VLAN"):
        apply(cfg, change("port.vlans", {"untagged": None, "tagged": []}, "ether2"))
    with pytest.raises(PluginError, match="1 to 4094"):
        apply(cfg, change("port.vlans", {"untagged": 5000}, "ether2"))
    assert routeros.writes == []


def test_a_refused_write_undoes_the_earlier_ones(routeros, cfg):
    routeros.fail["PATCH /rest/interface/bridge/port/*1"] = "failure: bad frame types"
    with pytest.raises(PluginError, match="RouterOS refused the VLANs of office: failure"):
        apply(cfg, change("port.vlans", {"untagged": 20, "tagged": [30, 31]}, "office"))
    # The range entry got office tagged, then the port failed: the entry is back.
    assert entry(routeros, "30-31")["tagged"] == "sfp-sfpplus1"
    assert routeros.writes[-1] == (
        "PATCH",
        "/rest/interface/bridge/vlan/*2",
        {"tagged": "sfp-sfpplus1"},
    )


def test_an_error_when_routeros_did_not_take_it(routeros, cfg):
    routeros.ignore_changes = True
    with pytest.raises(PluginError, match="did not apply the VLANs of ether2"):
        apply(cfg, change("port.vlans", {"untagged": 20}, "ether2"))


def test_creates_and_renames_a_vlan(routeros, cfg):
    [rt] = apply(cfg, change("vlan.set", {"id": 40, "name": "Cameras"}))
    assert routeros.writes == [
        (
            "PUT",
            "/rest/interface/bridge/vlan",
            {"bridge": "bridge", "vlan-ids": "40", "comment": "Cameras"},
        )
    ]
    assert (40, "Cameras") in [(v.id, v.name) for v in rt.vlans]
    [rt] = apply(cfg, change("vlan.set", {"id": 20, "name": "IoT"}))
    assert routeros.writes[-1] == ("PATCH", "/rest/interface/bridge/vlan/*1", {"comment": "IoT"})
    assert (20, "IoT") in [(v.id, v.name) for v in rt.vlans]  # before the interface's name
    with pytest.raises(PluginError, match="share one entry"):
        apply(cfg, change("vlan.set", {"id": 30, "name": "Lab"}))


def test_deletes_a_vlan(routeros, cfg):
    apply(cfg, change("vlan.set", {"id": 40, "name": "Cameras"}))
    item = entry(routeros, "40")[".id"]
    [rt] = apply(cfg, change("vlan.delete", {"id": 40}))
    assert routeros.writes[-1] == ("DELETE", f"/rest/interface/bridge/vlan/{item}", None)
    assert 40 not in [v.id for v in rt.vlans]


def test_deleting_a_vlan_the_router_has_a_network_on_is_refused(routeros, cfg):
    with pytest.raises(PluginError, match=r"interface on this device \(vlan20\)"):
        apply(cfg, change("vlan.delete", {"id": 20}))
    with pytest.raises(PluginError, match="share one entry"):
        apply(cfg, change("vlan.delete", {"id": 30}))
    with pytest.raises(PluginError, match="has no VLAN 50"):
        apply(cfg, change("vlan.delete", {"id": 50}))
    assert routeros.writes == []


def test_a_user_without_the_write_policy(routeros, cfg):
    routeros.read_only = True
    with pytest.raises(PluginError, match="lacks the write policy"):
        apply(cfg, change("vlan.set", {"id": 40, "name": "Cameras"}))
