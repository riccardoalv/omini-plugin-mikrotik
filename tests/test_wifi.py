import json
import logging

import pytest
from omini_sdk import Change, PluginError

from omini_mikrotik.collect import apply, collect

SECRET = "correct horse battery"


def wifi(kind, value):
    return Change(device="48:a9:8a:11:22:01", kind=kind, value=value)


def nets(rt):
    return [(n.ssid, n.vlan, n.security, n.enabled) for n in rt.wifi_networks]


def test_reads_the_wifi_networks(routeros, cfg):
    routeros.row("/interface/wifi", name="wifi1")["security.passphrase"] = SECRET
    [rt] = collect(cfg)
    # Home on both radios, once; Guests from its configuration and security
    # profiles, on its VLAN.
    assert nets(rt) == [("Home", None, "wpa2wpa3", True), ("Guests", 30, "wpa2", True)]
    assert SECRET not in json.dumps(rt.model_dump(mode="json"))


def test_renames_a_network_and_moves_it_to_another_vlan(routeros, cfg):
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "Visitors", "previous": "Guests", "vlan": 40}))
    assert routeros.writes == [
        (
            "PATCH",
            "/rest/interface/wifi/*10",
            {"configuration.ssid": "Visitors", "datapath.vlan-id": "40"},
        )
    ]
    assert ("Visitors", 40, "wpa2", True) in nets(rt)
    assert "Guests" not in [n.ssid for n in rt.wifi_networks]


def test_puts_a_network_back_on_the_untagged_lan(routeros, cfg):
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "Guests", "vlan": None}))
    assert routeros.row("/interface/wifi", name="wifi-guest")["datapath.vlan-id"] == "1"
    assert ("Guests", None, "wpa2", True) in nets(rt)


def test_changes_the_password_on_every_radio(routeros, cfg, caplog):
    caplog.set_level(logging.DEBUG)
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "Home", "password": SECRET}))
    assert [(p, b) for _, p, b in routeros.writes] == [
        ("/rest/interface/wifi/*C", {"security.passphrase": SECRET}),
        ("/rest/interface/wifi/*E", {"security.passphrase": SECRET}),
    ]
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(rt.model_dump(mode="json"))


def test_a_failed_change_is_undone_but_never_with_an_empty_password(routeros, cfg, caplog):
    caplog.set_level(logging.DEBUG)
    routeros.ignore_fields = {"datapath.vlan-id"}
    with pytest.raises(PluginError, match="did not apply the Wi-Fi network Home") as e:
        apply(cfg, wifi("wifi.set", {"ssid": "Home", "vlan": 20, "password": SECRET}))
    undo = routeros.writes[2:]
    # The VLAN is put back; the old passphrase was never shown, so it is not "restored".
    assert undo == [
        ("PATCH", "/rest/interface/wifi/*E", {"datapath.vlan-id": ""}),
        ("PATCH", "/rest/interface/wifi/*C", {"datapath.vlan-id": ""}),
    ]
    assert SECRET not in str(e.value) and SECRET not in caplog.text


def test_creates_a_network_on_every_radio(routeros, cfg):
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "IoT", "vlan": 20, "password": SECRET}))
    added = [b for m, p, b in routeros.writes if m == "PUT"]
    assert [
        (b["master-interface"], b["datapath.bridge"], b["datapath.vlan-id"]) for b in added
    ] == [
        ("wifi1", "bridge", "20"),  # its bridge port
        ("wifi2", "bridge", "20"),  # its own datapath bridge
    ]
    assert all(
        b["configuration.mode"] == "ap" and b["security.authentication-types"] == "wpa2-psk"
        for b in added
    )
    assert ("IoT", 20, "wpa2", True) in nets(rt)


def test_refuses_a_new_network_without_password_and_bad_values(routeros, cfg):
    with pytest.raises(PluginError, match="password is needed"):
        apply(cfg, wifi("wifi.set", {"ssid": "IoT"}))
    with pytest.raises(PluginError, match="8 to 63"):
        apply(cfg, wifi("wifi.set", {"ssid": "Home", "password": "short"}))
    with pytest.raises(PluginError, match="no Wi-Fi network named Nope"):
        apply(cfg, wifi("wifi.set", {"ssid": "Other", "previous": "Nope"}))
    with pytest.raises(PluginError, match="not an SSID"):
        apply(cfg, wifi("wifi.set", {"ssid": "x" * 33}))
    assert not routeros.writes


def test_turns_a_network_off(routeros, cfg):
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "Guests", "vlan": 30, "enabled": False}))
    assert ("Guests", 30, "wpa2", False) in nets(rt)


def test_deletes_an_extra_ssid(routeros, cfg):
    [rt] = apply(cfg, wifi("wifi.delete", {"ssid": "Guests"}))
    assert routeros.writes == [("DELETE", "/rest/interface/wifi/*10", None)]
    assert [n.ssid for n in rt.wifi_networks] == ["Home"]


def test_a_radio_network_is_turned_off_not_removed(routeros, cfg):
    with pytest.raises(PluginError, match="also carries Guests"):
        apply(cfg, wifi("wifi.delete", {"ssid": "Home"}))
    assert not routeros.writes
    apply(cfg, wifi("wifi.delete", {"ssid": "Guests"}))
    apply(cfg, wifi("wifi.delete", {"ssid": "Home"}))
    assert routeros.row("/interface/wifi", name="wifi1")["disabled"] == "true"
    assert routeros.row("/interface/wifi", name="wifi2")["disabled"] == "true"


def test_refuses_capsman(routeros, cfg):
    routeros.routes["/rest/interface/wifi/cap"] = {"enabled": "true"}
    with pytest.raises(PluginError, match="managed by CAPsMAN"):
        apply(cfg, wifi("wifi.set", {"ssid": "Guests", "vlan": 40}))
    with pytest.raises(PluginError, match="managed by CAPsMAN"):
        apply(cfg, wifi("wifi.delete", {"ssid": "Guests"}))
    assert not routeros.writes


def legacy(routeros):
    del routeros.routes["/rest/interface/wifi"]
    routeros.routes["/rest/interface/wireless"] = [
        {
            ".id": "*5",
            "name": "wlan1",
            "mode": "ap-bridge",
            "band": "2ghz-b/g/n",
            "frequency": "2412",
            "ssid": "Home",
            "security-profile": "default",
            "vlan-mode": "no-tag",
            "vlan-id": "1",
            "disabled": "false",
            "running": "true",
        },
        {
            ".id": "*6",
            "name": "wlan2",
            "master-interface": "wlan1",
            "mode": "ap-bridge",
            "ssid": "Guests",
            "security-profile": "guest",
            "vlan-mode": "use-tag",
            "vlan-id": "30",
            "disabled": "false",
        },
    ]
    routeros.routes["/rest/interface/wireless/security-profiles"] = [
        {
            ".id": "*0",
            "name": "default",
            "mode": "dynamic-keys",
            "authentication-types": "wpa2-psk",
        },
        {".id": "*1", "name": "guest", "mode": "none", "authentication-types": ""},
    ]


def test_legacy_networks(routeros, cfg):
    legacy(routeros)
    [rt] = collect(cfg)
    assert nets(rt) == [("Home", None, "wpa2", True), ("Guests", 30, "open", True)]


def test_legacy_changes(routeros, cfg):
    legacy(routeros)
    [rt] = apply(cfg, wifi("wifi.set", {"ssid": "Visitors", "previous": "Guests", "vlan": 40}))
    assert routeros.writes == [
        ("PATCH", "/rest/interface/wireless/*6", {"ssid": "Visitors", "vlan-id": "40"})
    ]
    assert ("Visitors", 40, "open", True) in nets(rt)
    with pytest.raises(PluginError, match="security profile"):
        apply(cfg, wifi("wifi.set", {"ssid": "Home", "password": SECRET}))
    with pytest.raises(PluginError, match="create a new Wi-Fi network in WinBox"):
        apply(cfg, wifi("wifi.set", {"ssid": "New", "password": SECRET}))
