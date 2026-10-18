from omini_mikrotik.collect import collect
from omini_mikrotik.forwards import port_range


def fwd(rt):
    return [
        (
            f.protocol,
            f.external_port,
            f.external_port_end,
            f.internal_ip,
            f.internal_port,
            f.interface,
            f.enabled,
            f.upnp,
        )
        for f in rt.port_forwards
    ]


def test_reads_the_port_forwards(routeros, cfg):
    [rt] = collect(cfg)
    # dst-nat rules of the dstnat chain with a protocol and port: not the
    # masquerade, the 1:1 NAT (no port) or the redirect to the router itself.
    assert fwd(rt) == [
        ("tcp", 443, None, "192.168.88.20", 8443, "ether1", True, False),
        ("udp", 27015, 27030, "192.168.88.40", None, "WAN", False, False),
        ("tcp", 80, None, "192.168.88.20", None, "ether1", True, False),
        ("tcp", 8080, None, "192.168.88.20", None, "ether1", True, False),
        ("udp", 3074, None, "192.168.88.41", 3074, "ether1", True, True),  # UPnP: dynamic
    ]
    nas, game = rt.port_forwards[0], rt.port_forwards[1]
    assert nas.description == "NAS web" and nas.source is None
    assert game.source == "198.51.100.0/24"


def test_reads_ipv6_forwards_when_the_router_has_them(routeros, cfg):
    routeros.routes["/rest/ipv6/firewall/nat"] = [
        {
            ".id": "*1",
            "chain": "dstnat",
            "action": "dst-nat",
            "protocol": "tcp",
            "dst-port": "22",
            "to-addresses": "2001:db8:bd7c:8800::20",
            "to-ports": "22",
            "dynamic": "false",
            "disabled": "false",
        }
    ]
    [rt] = collect(cfg)
    assert rt.port_forwards[-1].internal_ip == "2001:db8:bd7c:8800::20"


def test_port_forwards_are_optional(routeros, cfg):
    routeros.forbidden.add("/rest/ip/firewall/nat")
    [rt] = collect(cfg)
    assert rt.port_forwards is None and rt.interfaces


def test_port_range():
    assert port_range("8080") == (8080, None)
    assert port_range("1000-2000") == (1000, 2000)
    assert port_range("5-5") == (5, None)
    assert port_range("0") is None and port_range("2000-1000") is None
    assert port_range("http") is None
