import pytest
from omini_sdk import Change, PluginError

from omini_mikrotik.collect import apply, collect


def change(port="ether2", value=False, kind="port.enabled"):
    return Change(device="48:a9:8a:11:22:01", kind=kind, port=port, value=value)


def enabled(devices, port):
    return next(i.enabled for i in devices[0].interfaces if i.name == port)


def ethernet(routeros, name):
    return next(r for r in routeros.routes["/rest/interface/ethernet"] if r["name"] == name)


def test_reads_whether_each_port_is_enabled(routeros, cfg):
    ethernet(routeros, "office")["disabled"] = "true"
    next(r for r in routeros.routes["/rest/interface"] if r["name"] == "office")["disabled"] = (
        "true"
    )
    [rt] = collect(cfg)
    ports = {i.name: i for i in rt.interfaces}
    assert ports["ether1"].enabled is True and ports["bridge"].enabled is True
    assert ports["office"].enabled is False and ports["office"].up is False


def test_disables_a_port_keeping_its_other_settings(routeros, cfg):
    before = dict(ethernet(routeros, "ether2"))
    devices = apply(cfg, change())
    # Only the disabled property, on the port's own item (by .id).
    assert routeros.patches == [("/rest/interface/ethernet/*2", {"disabled": "true"})]
    after = ethernet(routeros, "ether2")
    assert {k: v for k, v in after.items() if k != "disabled"} == {
        k: v for k, v in before.items() if k != "disabled"
    }
    assert enabled(devices, "ether2") is False
    # Read again after the change: the port's list, then the whole collection.
    patch = routeros.calls.index(("PATCH", "/rest/interface/ethernet/*2"))
    assert ("GET", "/rest/interface/ethernet") in routeros.calls[patch + 1 :]


def test_enables_a_renamed_port(routeros, cfg):
    ethernet(routeros, "office")["disabled"] = "true"
    devices = apply(cfg, change("office", True))
    assert routeros.patches == [("/rest/interface/ethernet/*3", {"disabled": "false"})]
    assert enabled(devices, "office") is True


def test_an_error_when_routeros_did_not_take_it(routeros, cfg):
    routeros.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not apply the change to ether2 \(undone\)"):
        apply(cfg, change())
    assert ethernet(routeros, "ether2")["disabled"] == "false"


def test_says_when_the_change_could_not_be_undone(routeros, cfg):
    import httpx

    routeros.ignore_changes = True
    write = routeros.write
    patches = []

    def refuse_the_undo(method, path, body):
        patches.append(path)
        if len(patches) > 1:
            return httpx.Response(500, json={"error": 500, "message": "Internal Error"})
        return write(method, path, body)

    routeros.write = refuse_the_undo
    with pytest.raises(PluginError, match=r"could not be undone: check it on the device"):
        apply(cfg, change())


def test_nothing_written_is_not_undone(routeros, cfg):
    with pytest.raises(PluginError) as e:
        apply(cfg, change("ether12"))
    assert "undone" not in str(e.value)


def test_nothing_is_sent_when_the_port_is_already_so(routeros, cfg):
    devices = apply(cfg, change(value=True))
    assert routeros.patches == []
    assert not any(m == "PATCH" for m, _ in routeros.calls)
    assert enabled(devices, "ether2") is True


def test_unknown_ports_and_changes(routeros, cfg):
    with pytest.raises(PluginError, match="no Ethernet port named ether12"):
        apply(cfg, change("ether12"))
    with pytest.raises(PluginError, match="no Ethernet port named bridge"):
        apply(cfg, change("bridge"))  # not a physical port
    with pytest.raises(PluginError, match="cannot apply"):
        apply(cfg, change(value="off"))
    assert not any(m == "PATCH" for m, _ in routeros.calls)


def test_a_user_without_the_write_policy(routeros, cfg):
    routeros.read_only = True
    with pytest.raises(PluginError, match="lacks the write policy") as e:
        apply(cfg, change())
    assert "policy=read,write,api,rest-api" in str(e.value)
    assert routeros.patches == []
