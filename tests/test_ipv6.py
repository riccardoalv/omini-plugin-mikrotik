"""IPv6 as far as RouterOS's REST API gives it: AAAA names, DHCPv6 bindings,
IPv6 default routes and the router's own IPv6 addresses."""

from omini_sdk import Change

from omini_mikrotik.collect import apply, collect, duid_mac


def change(kind, value):
    return Change(device="48:a9:8a:11:22:01", kind=kind, value=value)


def test_reads_aaaa_names_next_to_the_a_ones(routeros, cfg):
    [rt] = collect(cfg)
    assert ("printer.lan", "fd00::30") in [(r.name, r.ip) for r in rt.dns_records]


def test_dns_changes_never_touch_aaaa_records(routeros, cfg):
    routeros.rows("/ip/dns/static").append(
        {
            ".id": "*7",
            "name": "nas.lan",
            "address": "2001:db8:bd7c:8800::20",
            "type": "AAAA",
            "dynamic": "false",
            "disabled": "false",
        }
    )
    [rt] = apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.88.21"}))
    assert sorted(r.ip for r in rt.dns_records if r.name == "nas.lan") == [
        "192.168.88.21",
        "2001:db8:bd7c:8800::20",
    ]
    [rt] = apply(cfg, change("dns.delete", {"name": "nas.lan"}))
    # The A record is gone; the AAAA record someone else made stays.
    assert routeros.row("/ip/dns/static", name="nas.lan", type="AAAA") is not None
    assert [r.ip for r in rt.dns_records if r.name == "nas.lan"] == ["2001:db8:bd7c:8800::20"]
    assert all(m != "DELETE" or not p.endswith("/*7") for m, p, _ in routeros.writes)


def test_reads_dhcpv6_bindings_as_leases(routeros, cfg):
    [rt] = collect(cfg)
    v6 = [(lease.ip, lease.mac) for lease in rt.dhcp_leases if ":" in lease.ip]
    # Addresses only (not the delegated /64), bound, with a MAC from the DUID
    # (LL or LLT; a DUID-UUID has none).
    assert v6 == [
        ("2001:db8:bd7c:8800::20", "aa:bb:cc:00:00:20"),
        ("2001:db8:bd7c:8800::22", "aa:bb:cc:00:00:22"),
    ]
    first = next(lease for lease in rt.dhcp_leases if lease.ip == "2001:db8:bd7c:8800::20")
    assert first.expires_at is not None
    # The IPv4 leases are still there, first.
    assert "." in rt.dhcp_leases[0].ip


def test_a_binding_with_its_own_mac_uses_it(routeros, cfg):
    routeros.rows("/ipv6/dhcp-server/binding")[0]["mac-address"] = "AA:BB:CC:00:00:99"
    [rt] = collect(cfg)
    assert ("2001:db8:bd7c:8800::20", "aa:bb:cc:00:00:99") in [
        (lease.ip, lease.mac) for lease in rt.dhcp_leases
    ]


def test_duid_mac():
    assert duid_mac("0x00030001aabbcc000020") == "aa:bb:cc:00:00:20"
    assert duid_mac("00:01:00:01:2c:3a:1b:2c:aa:bb:cc:00:00:21") == "aa:bb:cc:00:00:21"
    assert duid_mac("0x0002000000090c") is None and duid_mac("") is None and duid_mac(None) is None


def test_reads_the_ipv6_default_route_as_a_gateway(routeros, cfg):
    [rt] = collect(cfg)
    gw = {g.name: g for g in rt.gateways}
    assert (gw["ether1 IPv6"].interface, gw["ether1 IPv6"].address, gw["ether1 IPv6"].status) == (
        "ether1",
        "fe80::1",
        "up",
    )
    assert "ether1" in gw  # the IPv4 one keeps its name
    assert not any(g.address == "2001:db8:ffff::1" for g in rt.gateways)  # not the main table


def test_reports_its_own_ipv6_addresses(routeros, cfg):
    [rt] = collect(cfg)
    assert rt.ips == [
        "192.168.20.1",
        "192.168.88.1",
        "203.0.113.10",
        "2001:db8:bd7c:8800::1",
        "2001:db8:bd7c:8814::1",
        "2001:db8:ffff:10::2",
        "fd00:88::1",
    ]
    ports = {i.name: i for i in rt.interfaces}
    assert ports["ether1"].ips == ["203.0.113.10/24", "2001:db8:ffff:10::2/64"]
    assert ports["bridge"].ips == ["192.168.88.1/24", "2001:db8:bd7c:8800::1/64", "fd00:88::1/64"]


def test_ipv6_is_optional(routeros, cfg):
    for menu in ("/rest/ipv6/address", "/rest/ipv6/route", "/rest/ipv6/dhcp-server/binding"):
        routeros.forbidden.add(menu)
    [rt] = collect(cfg)
    assert rt.ips == ["192.168.20.1", "192.168.88.1", "203.0.113.10"]
    assert all(":" not in lease.ip for lease in rt.dhcp_leases)
