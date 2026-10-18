"""The router side: Device.networks, network.set, network.delete and
reservations, against a fake RouterOS that keeps what is written (the
fixtures are a defconf router: WAN on ether1, the LAN on the bridge, VLAN 20
on the bridge)."""

import copy

import pytest
from omini_sdk import Change, PluginError

from omini_mikrotik.collect import apply, collect

KEY = "48:a9:8a:11:22:01"


def change(kind, value):
    return Change(device=KEY, kind=kind, value=value)


def net(vlan, **kw):
    value = {
        "vlan": vlan,
        "name": "Guests",
        "gateway": "192.168.40.1/24",
        "dhcp": {"enabled": True, "from": "192.168.40.100", "to": "192.168.40.200"},
        "access": "internet",
        "mdns": False,
    }
    value.update(kw)
    return value


def networks(device):
    return {n.vlan: n.model_dump(by_alias=True, exclude_none=True) for n in device.networks or []}


def filters(routeros, iface):
    return [
        (r["chain"], r["action"], r.get("dst-port") or r.get("dst-address-list") or "")
        for r in routeros.rows("/ip/firewall/filter")
        if r.get("in-interface") == iface
    ]


def test_reads_the_networks_and_reservations(routeros, cfg):
    [rt] = collect(cfg)
    nets = networks(rt)
    assert sorted(nets) == [1, 20]  # never the WAN (ether1)
    assert nets[1] == {
        "vlan": 1,
        "interface": "bridge",
        "device": "bridge",
        "gateway": "192.168.88.1/24",
        "dhcp": {"enabled": True, "from": "192.168.88.10", "to": "192.168.88.254"},
        "access": "custom",  # defconf: no rule of its own
        "mdns": False,
    }
    assert nets[20] == {
        "vlan": 20,
        "name": "IoT",
        "interface": "vlan20",
        "device": "vlan20",
        "parent": "bridge",
        "gateway": "192.168.20.1/24",
        "dhcp": {"enabled": False},
        "access": "custom",
        "mdns": False,
    }
    assert [(r.mac, r.ip, r.name) for r in rt.dhcp_reservations] == [
        ("aa:bb:cc:00:00:04", "192.168.88.20", None),
        ("aa:bb:cc:00:00:03", "192.168.88.5", "Phone"),
    ]


def test_a_device_without_a_default_route_has_no_networks(routeros, cfg):
    routeros.routes["/rest/ip/route"] = [
        r for r in routeros.routes["/rest/ip/route"] if r["dst-address"] != "0.0.0.0/0"
    ]
    [rt] = collect(cfg)
    assert rt.networks is None


def test_without_mdns_repeat_ifaces_mdns_is_unknown(routeros, cfg):
    del routeros.routes["/rest/ip/dns"]["mdns-repeat-ifaces"]  # before RouterOS 7.16
    [rt] = collect(cfg)
    assert "mdns" not in networks(rt)[20]
    with pytest.raises(PluginError, match=r"needs RouterOS 7\.16 or later"):
        apply(
            cfg,
            change(
                "network.set",
                net(20, gateway="192.168.20.1/24", dhcp=None, access="custom", mdns=True),
            ),
        )
    assert routeros.writes == []
    apply(
        cfg,
        change(
            "network.set",
            net(20, gateway="192.168.20.1/24", dhcp=None, access="custom", mdns=False),
        ),
    )


def test_creates_a_network(routeros, cfg):
    [rt] = apply(cfg, change("network.set", net(40)))
    assert networks(rt)[40] == {
        "vlan": 40,
        "name": "Guests",
        "interface": "vlan40",
        "device": "vlan40",
        "parent": "bridge",
        "gateway": "192.168.40.1/24",
        "dhcp": {"enabled": True, "from": "192.168.40.100", "to": "192.168.40.200"},
        "access": "internet",
        "mdns": False,
    }
    # The bridge is a tagged member of the VLAN (VLAN filtering is on).
    vlan = routeros.row("/interface/bridge/vlan", **{"vlan-ids": "40"})
    assert vlan["tagged"] == "bridge" and vlan["comment"] == "Guests"
    assert routeros.row("/interface/vlan", name="vlan40")["interface"] == "bridge"
    assert routeros.row("/ip/address", interface="vlan40")["address"] == "192.168.40.1/24"
    server = routeros.row("/ip/dhcp-server", interface="vlan40")
    assert server["name"] == "omini-vlan40" and server["comment"] == "Omini: Guests"
    pool = routeros.row("/ip/pool", name=server["address-pool"])
    assert pool["ranges"] == "192.168.40.100-192.168.40.200"
    dn = routeros.row("/ip/dhcp-server/network", address="192.168.40.0/24")
    assert (dn["gateway"], dn["dns-server"]) == ("192.168.40.1", "192.168.40.1")
    assert {a["address"] for a in routeros.rows("/ip/firewall/address-list")} == {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    }
    # Rules right before the defconf drops of their chain, marked as Omini's.
    rules = routeros.rows("/ip/firewall/filter")
    comments = [r["comment"] for r in rules]
    assert comments == [
        "defconf: accept established,related,untracked",
        "defconf: drop invalid",
        "defconf: accept ICMP",
        "Omini: Guests DNS",
        "Omini: Guests DNS",
        "Omini: Guests not to the router",
        "defconf: drop all not coming from LAN",
        "defconf: fasttrack",
        "defconf: accept established,related, untracked",
        "defconf: drop invalid",
        "Omini: Guests not to other networks",
        "Omini: Guests to the internet",
        "defconf: drop all from WAN not DSTNATed",
    ]
    # Firewall rules go through add (place-before); the rest through PUT.
    adds = [w for w in routeros.writes if w[1] == "/rest/ip/firewall/filter/add"]
    assert len(adds) == 5 and {w[2]["place-before"] for w in adds} == {"*4", "*8"}


def test_a_new_dhcp_server_gets_its_own_pool(routeros, cfg):
    routeros.rows("/ip/pool").append({".id": "*9", "name": "", "ranges": "10.0.0.2-10.0.0.9"})
    apply(cfg, change("network.set", net(40)))
    server = routeros.row("/ip/dhcp-server", interface="vlan40")
    assert server["address-pool"] == "omini-vlan40"
    assert routeros.row("/ip/pool", **{".id": "*9"})["ranges"] == "10.0.0.2-10.0.0.9"


def test_a_new_vlan_on_a_bridge_port_tags_that_port(routeros, cfg):
    [rt] = apply(cfg, change("network.set", net(40, parent="sfp-sfpplus1", access="all")))
    assert networks(rt)[40]["parent"] == "bridge"  # a VLAN interface rides on the bridge
    vlan = routeros.row("/interface/bridge/vlan", **{"vlan-ids": "40"})
    assert vlan["tagged"] == "bridge,sfp-sfpplus1"
    assert filters(routeros, "vlan40") == [("input", "accept", ""), ("forward", "accept", "")]


def test_changes_a_network_in_place(routeros, cfg):
    value = net(20, name="Cameras", gateway="192.168.21.1/24", access="local")
    value["dhcp"] = {"enabled": True, "from": "192.168.21.10", "to": "192.168.21.99"}
    [rt] = apply(cfg, change("network.set", value))
    n = networks(rt)[20]
    assert (n["name"], n["gateway"], n["access"]) == ("Cameras", "192.168.21.1/24", "local")
    assert n["dhcp"] == {"enabled": True, "from": "192.168.21.10", "to": "192.168.21.99"}
    assert routeros.row("/ip/address", interface="vlan20")[".id"] == "*2"  # the same address, set
    assert filters(routeros, "vlan20") == [
        ("input", "accept", "53"),
        ("input", "accept", "53"),
        ("input", "drop", ""),
        ("forward", "drop", ""),
    ]
    # From local to all: Omini's old rules go, the new ones come.
    [rt] = apply(cfg, change("network.set", dict(value, access="all")))
    assert networks(rt)[20]["access"] == "all"
    assert filters(routeros, "vlan20") == [("input", "accept", ""), ("forward", "accept", "")]
    # DHCP off: the server is turned off, not removed.
    [rt] = apply(cfg, change("network.set", dict(value, access="all", dhcp={"enabled": False})))
    assert networks(rt)[20]["dhcp"] == {"enabled": False}
    assert routeros.row("/ip/dhcp-server", interface="vlan20")["disabled"] == "true"


def test_custom_access_leaves_the_rules_alone(routeros, cfg):
    routeros.rows("/ip/firewall/filter").insert(
        0,
        {
            ".id": "*50",
            "chain": "forward",
            "action": "accept",
            "in-interface": "vlan20",
            "dst-address": "192.168.88.10",
            "disabled": "false",
            "dynamic": "false",
        },
    )
    [rt] = collect(cfg)
    assert networks(rt)[20]["access"] == "custom"
    before = copy.deepcopy(routeros.rows("/ip/firewall/filter"))
    apply(
        cfg, change("network.set", net(20, gateway="192.168.20.1/24", dhcp=None, access="custom"))
    )
    assert routeros.rows("/ip/firewall/filter") == before


def test_the_lan_can_change_but_not_its_address_nor_go(routeros, cfg):
    lan = net(1, name="", gateway="192.168.88.1/24", access="custom", mdns=None)
    lan["dhcp"] = {"enabled": True, "from": "192.168.88.50", "to": "192.168.88.99"}
    [rt] = apply(cfg, change("network.set", lan))
    assert networks(rt)[1]["dhcp"]["from"] == "192.168.88.50"
    assert routeros.writes == [
        ("PATCH", "/rest/ip/pool/*1", {"ranges": "192.168.88.50-192.168.88.99"})
    ]
    # Omini reaches the router on 192.168.88.1: its address is never changed.
    with pytest.raises(PluginError, match=r"Omini reaches the router through 192\.168\.88\.1"):
        apply(cfg, change("network.set", dict(lan, gateway="192.168.88.2/24")))
    with pytest.raises(PluginError, match="the LAN cannot be removed"):
        apply(cfg, change("network.delete", {"vlan": 1}))
    assert len(routeros.writes) == 1


def test_bad_networks_are_refused(routeros, cfg):
    with pytest.raises(PluginError, match="private IPv4"):
        apply(cfg, change("network.set", net(40, gateway="8.8.8.1/24")))
    with pytest.raises(PluginError, match="overlaps the network of VLAN 20"):
        apply(cfg, change("network.set", net(40, gateway="192.168.20.129/25", dhcp=None)))
    with pytest.raises(PluginError, match="not inside"):
        apply(
            cfg,
            change(
                "network.set", net(40, dhcp={"enabled": True, "from": "10.0.0.1", "to": "10.0.0.9"})
            ),
        )
    with pytest.raises(PluginError, match="share one entry"):
        apply(cfg, change("network.set", net(30, gateway="192.168.30.1/24", dhcp=None)))
    assert routeros.writes == []


def test_a_refused_write_undoes_the_earlier_ones(routeros, cfg):
    before = copy.deepcopy(routeros.routes)
    routeros.fail["POST /rest/ip/firewall/filter/add"] = "failure: bad rule"
    with pytest.raises(PluginError, match="RouterOS refused the access rules of vlan40: failure"):
        apply(cfg, change("network.set", net(40)))
    assert routeros.writes  # the VLAN, address and DHCP were written, then removed again
    assert routeros.routes == before


def test_an_error_when_routeros_did_not_take_it(routeros, cfg):
    routeros.ignore_changes = True
    with pytest.raises(PluginError, match="did not apply the network of VLAN 40"):
        apply(cfg, change("network.set", net(40)))


def test_deletes_a_network(routeros, cfg):
    apply(cfg, change("network.set", net(40, mdns=True)))
    assert routeros.routes["/rest/ip/dns"]["mdns-repeat-ifaces"] == "vlan40"
    [rt] = apply(cfg, change("network.delete", {"vlan": 40}))
    assert 40 not in networks(rt)
    assert routeros.row("/interface/vlan", name="vlan40") is None
    assert routeros.row("/ip/address", interface="vlan40") is None
    assert routeros.row("/ip/dhcp-server", interface="vlan40") is None
    assert routeros.row("/ip/pool", name="omini-vlan40") is None
    assert routeros.row("/ip/dhcp-server/network", address="192.168.40.0/24") is None
    assert filters(routeros, "vlan40") == []
    assert routeros.routes["/rest/ip/dns"]["mdns-repeat-ifaces"] == ""
    # The VLAN stays in the bridge's table (switch ports may use it), without the bridge.
    assert routeros.row("/interface/bridge/vlan", **{"vlan-ids": "40"})["tagged"] == ""
    with pytest.raises(PluginError, match="serves no network on VLAN 40"):
        apply(cfg, change("network.delete", {"vlan": 40}))


def test_turns_mdns_on_and_off(routeros, cfg):
    on = net(20, gateway="192.168.20.1/24", dhcp=None, access="custom", mdns=True)
    [rt] = apply(cfg, change("network.set", on))
    assert routeros.writes[-1] == ("POST", "/rest/ip/dns/set", {"mdns-repeat-ifaces": "vlan20"})
    assert networks(rt)[20]["mdns"] is True
    [rt] = apply(cfg, change("network.set", dict(on, mdns=False)))
    assert networks(rt)[20]["mdns"] is False


def test_reservations(routeros, cfg):
    value = {"mac": "AA:BB:CC:00:00:01", "ip": "192.168.88.50", "name": "Laptop"}
    [rt] = apply(cfg, change("reservation.set", value))
    assert routeros.writes == [
        (
            "PUT",
            "/rest/ip/dhcp-server/lease",
            {
                "mac-address": "AA:BB:CC:00:00:01",
                "address": "192.168.88.50",
                "server": "defconf",
                "comment": "Laptop",
            },
        )
    ]
    assert ("aa:bb:cc:00:00:01", "192.168.88.50", "Laptop") in [
        (r.mac, r.ip, r.name) for r in rt.dhcp_reservations
    ]
    [rt] = apply(
        cfg,
        change(
            "reservation.set", {"mac": "aa:bb:cc:00:00:03", "ip": "192.168.88.6", "name": "Phone"}
        ),
    )
    assert routeros.writes[-1] == (
        "PATCH",
        "/rest/ip/dhcp-server/lease/*3",
        {"address": "192.168.88.6"},
    )
    [rt] = apply(cfg, change("reservation.delete", {"mac": "aa:bb:cc:00:00:03"}))
    assert "aa:bb:cc:00:00:03" not in [r.mac for r in rt.dhcp_reservations]
    with pytest.raises(PluginError, match=r"no DHCP server serves 192\.168\.20\.5"):
        apply(cfg, change("reservation.set", {"mac": "aa:bb:cc:00:00:09", "ip": "192.168.20.5"}))


def test_a_reservation_whose_name_did_not_stick_fails(routeros, cfg):
    routeros.ignore_fields = {"comment"}
    value = {"mac": "aa:bb:cc:00:00:03", "ip": "192.168.88.5", "name": "Tablet"}
    with pytest.raises(PluginError, match="did not apply the reservation of aa:bb:cc:00:00:03"):
        apply(cfg, change("reservation.set", value))
    routeros.ignore_fields = set()
    [rt] = apply(cfg, change("reservation.set", value))
    assert ("aa:bb:cc:00:00:03", "192.168.88.5", "Tablet") in [
        (r.mac, r.ip, r.name) for r in rt.dhcp_reservations
    ]


def test_a_change_without_its_vlan_is_refused(routeros, cfg):
    for kind, value in (
        ("network.set", {"gateway": "192.168.40.1/24"}),
        ("network.set", net("forty")),
        ("network.delete", {}),
        ("network.delete", {"vlan": None}),
    ):
        with pytest.raises(PluginError, match="no VLAN id"):
            apply(cfg, change(kind, value))
    with pytest.raises(PluginError, match="VLAN ids go from 1 to 4094"):
        apply(cfg, change("network.set", net(5000)))
    with pytest.raises(PluginError, match="dhcp must be"):
        apply(cfg, change("network.set", net(40, dhcp=True)))
    assert routeros.writes == []
