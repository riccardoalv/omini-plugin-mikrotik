"""VLANs of a bridge with VLAN filtering (the switch side): port.vlans,
vlan.set and vlan.delete.

A port's VLANs are its bridge port's ``pvid`` (the untagged VLAN) and
``frame-types``, and the bridge VLAN entries (/interface/bridge/vlan) that
list it as tagged. RouterOS adds a port to its PVID's VLAN as untagged by
itself, so untagged lists are never filled in, only cleared of the port
when it leaves a VLAN. A VLAN's name is its bridge VLAN entry's comment.

Omini never turns VLAN filtering on: done without the right VLAN table, it
cuts the device off. When it is off, the change is refused with how to do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omini_sdk import PluginError

from omini_mikrotik.client import Client, Forbidden, NotFound
from omini_mikrotik.common import as_list, flag, names, vlan_ids
from omini_mikrotik.journal import Journal

BRIDGES = "/interface/bridge"
BRIDGE_PORTS = "/interface/bridge/port"
BRIDGE_VLANS = "/interface/bridge/vlan"
VLAN_INTERFACES = "/interface/vlan"

ACCESS = "admit-only-untagged-and-priority-tagged"
TRUNK = "admit-all"
TAGGED_ONLY = "admit-only-vlan-tagged"


def filtering_off(bridge: str) -> PluginError:
    return PluginError(
        f"VLAN filtering is off on {bridge}, and Omini does not turn it on (with the "
        "wrong VLAN table it cuts the device off). Turn it on yourself in WinBox or "
        f"WebFig: Bridge → {bridge} → VLAN → VLAN Filtering, then try again"
    )


@dataclass
class L2:
    bridges: list[dict[str, Any]]
    ports: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    vlan_ifaces: list[dict[str, Any]]

    def filtering(self, bridge: str) -> bool:
        return any(b.get("name") == bridge and flag(b.get("vlan-filtering")) for b in self.bridges)

    def static(self, bridge: str) -> list[dict[str, Any]]:
        """The bridge's own VLAN entries (dynamic ones cannot be changed)."""
        return [
            e
            for e in self.entries
            if e.get("bridge") == bridge
            and not flag(e.get("dynamic"))
            and not flag(e.get("disabled"))
        ]

    def covering(self, bridge: str, vid: int) -> list[dict[str, Any]]:
        return [e for e in self.static(bridge) if vid in vlan_ids(e.get("vlan-ids"))]


def read(c: Client) -> L2:
    try:
        return L2(
            bridges=as_list(c.get(BRIDGES)),
            ports=as_list(c.get(BRIDGE_PORTS)),
            entries=as_list(c.get(BRIDGE_VLANS)),
            vlan_ifaces=as_list(c.get(VLAN_INTERFACES)),
        )
    except Forbidden as e:
        raise PluginError("the RouterOS user cannot read the bridge settings") from e
    except NotFound as e:
        raise PluginError("this device has no bridge menus") from e


def shared(e: dict[str, Any]) -> PluginError:
    return PluginError(
        f"VLANs {e.get('vlan-ids')} share one entry of the bridge VLAN table, and "
        "Omini changes entries of a single VLAN only: split it into one entry per "
        "VLAN in WinBox (Bridge → VLANs), then try again"
    )


def membership(st: L2, port: dict[str, Any]) -> tuple[int | None, list[int], list[int]]:
    """A bridge port's VLANs as configured: (untagged, tagged, VLANs whose
    entry lists it as untagged by hand)."""
    name, bridge = port.get("interface"), port.get("bridge")
    frames = port.get("frame-types") or TRUNK
    pvid = int(port.get("pvid") or 1)
    tagged: set[int] = set()
    by_hand: set[int] = set()
    for e in st.static(bridge or ""):
        ids = vlan_ids(e.get("vlan-ids"))
        if name in names(e.get("tagged")):
            tagged.update(ids)
        if name in names(e.get("untagged")):
            by_hand.update(ids)
    return (None if frames == TAGGED_ONLY else pvid), sorted(tagged), sorted(by_hand)


def wanted(value: Any) -> tuple[int | None, list[int]]:
    if not isinstance(value, dict):
        raise PluginError(f"cannot apply port.vlans with value {value!r}")
    untagged = value.get("untagged")
    tagged = value.get("tagged") or []
    try:
        u = int(untagged) if untagged is not None else None
        t = sorted({int(v) for v in tagged})
    except (TypeError, ValueError) as e:
        raise PluginError(f"cannot apply port.vlans with value {value!r}") from e
    if any(not 1 <= v <= 4094 for v in [*t, *([u] if u is not None else [])]):
        raise PluginError("VLAN ids go from 1 to 4094")
    if u is not None and u in t:
        raise PluginError(f"VLAN {u} cannot be both untagged and tagged on one port")
    if u is None and not t:
        raise PluginError("a port needs an untagged VLAN or at least one tagged VLAN")
    return u, t


def set_port_vlans(c: Client, j: Journal, port_name: str, value: Any) -> None:
    """Replaces the port's VLANs: its PVID and frame types, and its place in
    the tagged list of every entry of its bridge (creating the entry of a
    tagged VLAN that has none). Read again to check, undone if not as asked."""
    untagged, tagged = wanted(value)
    st = read(c)
    port = next((p for p in st.ports if p.get("interface") == port_name), None)
    if port is None or not port.get(".id"):
        raise PluginError(
            f"{port_name or '(none)'} is not a bridge port: "
            "Omini changes the VLANs of bridge ports only"
        )
    bridge = str(port.get("bridge") or "")
    if not st.filtering(bridge):
        raise filtering_off(bridge)
    plan: list[tuple[dict[str, Any], dict[str, str]]] = []
    for e in st.static(bridge):
        ids = vlan_ids(e.get("vlan-ids"))
        if not ids:
            continue
        inside = [v in tagged for v in ids]
        if any(inside) and not all(inside):
            raise shared(e)
        now_tagged, now_untagged = names(e.get("tagged")), names(e.get("untagged"))
        new_tagged = [p for p in now_tagged if p != port_name]
        if all(inside):
            new_tagged.append(port_name)
        new_untagged = [p for p in now_untagged if p != port_name or ids == [untagged]]
        body = {}
        if set(new_tagged) != set(now_tagged):
            body["tagged"] = ",".join(new_tagged)
        if new_untagged != now_untagged:
            body["untagged"] = ",".join(new_untagged)
        if body:
            plan.append((e, body))
    missing = [v for v in tagged if not st.covering(bridge, v)]
    # Tagged VLANs first, then the PVID, then what the port leaves: the port
    # keeps the VLANs it shares with the new setting all along.
    for v in missing:
        j.add(
            BRIDGE_VLANS,
            {"bridge": bridge, "vlan-ids": str(v), "tagged": port_name},
            f"adding VLAN {v} to {port_name}",
        )
    adds = [(e, b) for e, b in plan if port_name in names(b.get("tagged", e.get("tagged")))]
    for e, body in adds:
        j.set(BRIDGE_VLANS, e, body, f"adding VLAN {e.get('vlan-ids')} to {port_name}")
    frames = ACCESS if untagged is not None and not tagged else TRUNK if untagged else TAGGED_ONLY
    port_body = {"frame-types": frames}
    if untagged is not None:
        port_body["pvid"] = str(untagged)
    j.set(BRIDGE_PORTS, port, port_body, f"the VLANs of {port_name}")
    for e, body in plan:
        if (e, body) not in adds:
            j.set(BRIDGE_VLANS, e, body, f"removing VLAN {e.get('vlan-ids')} from {port_name}")
    after = read(c)
    again = next((p for p in after.ports if p.get("interface") == port_name), None)
    if again is None:
        raise PluginError(f"RouterOS did not apply the VLANs of {port_name}")
    u, t, by_hand = membership(after, again)
    if u != untagged or t != tagged or any(v != untagged for v in by_hand):
        raise PluginError(f"RouterOS did not apply the VLANs of {port_name}")


def the_bridge(st: L2, vid: int) -> str:
    """The bridge a VLAN belongs to: the one with VLAN filtering (the one
    already holding the VLAN if several have it)."""
    if not st.bridges:
        raise PluginError("the device has no bridge: its VLANs cannot be changed")
    on = [str(b.get("name")) for b in st.bridges if flag(b.get("vlan-filtering"))]
    if not on:
        raise filtering_off(str(st.bridges[0].get("name") or "bridge"))
    holding = [b for b in on if st.covering(b, vid)]
    if holding:
        return holding[0]
    if len(on) > 1:
        raise PluginError(
            f"the device has several bridges with VLAN filtering ({', '.join(on)}): "
            f"add VLAN {vid} in WinBox (Bridge → VLANs)"
        )
    return on[0]


def vlan_id(value: Any, key: str = "id") -> int:
    try:
        vid = int(value[key])
    except (TypeError, ValueError, KeyError) as e:
        raise PluginError(f"no VLAN {key} in {value!r}") from e
    if not 1 <= vid <= 4094:
        raise PluginError("VLAN ids go from 1 to 4094")
    return vid


def set_vlan(c: Client, j: Journal, value: Any) -> None:
    """Creates the VLAN (an entry of the bridge VLAN table, with no port yet),
    or renames it (the entry's comment)."""
    vid = vlan_id(value)
    name = str(value.get("name") or "").strip()
    st = read(c)
    bridge = the_bridge(st, vid)
    entries = st.covering(bridge, vid)
    if entries:
        entry = entries[0]
        if len(vlan_ids(entry.get("vlan-ids"))) > 1:
            if not name:
                return
            raise shared(entry)
        j.set(BRIDGE_VLANS, entry, {"comment": name}, f"the name of VLAN {vid}")
    else:
        body = {"bridge": bridge, "vlan-ids": str(vid)}
        if name:
            body["comment"] = name
        j.add(BRIDGE_VLANS, body, f"VLAN {vid}")
    after = read(c).covering(bridge, vid)
    if not after or str(after[0].get("comment") or "").strip() != name:
        raise PluginError(f"RouterOS did not apply VLAN {vid}")


def delete_vlan(c: Client, j: Journal, value: Any) -> None:
    """Removes the VLAN's entry from the bridge VLAN table. A VLAN the router
    has an interface on (a network) is refused: that is network.delete."""
    vid = vlan_id(value)
    if vid == 1:
        raise PluginError("VLAN 1 cannot be deleted")
    st = read(c)
    iface = next(
        (
            v
            for v in st.vlan_ifaces
            if str(v.get("vlan-id")) == str(vid) and not flag(v.get("disabled"))
        ),
        None,
    )
    if iface is not None:
        raise PluginError(
            f"VLAN {vid} has an interface on this device ({iface.get('name')}): "
            "remove its network first"
        )
    entries = [e for b in st.bridges for e in st.covering(str(b.get("name")), vid)]
    if not entries:
        raise PluginError(f"the bridge VLAN table has no VLAN {vid}")
    for e in entries:
        if len(vlan_ids(e.get("vlan-ids"))) > 1:
            raise shared(e)
    for e in entries:
        j.remove(BRIDGE_VLANS, e, f"the removal of VLAN {vid}")
    after = read(c)
    if any(after.covering(str(b.get("name")), vid) for b in after.bridges):
        raise PluginError(f"RouterOS did not remove VLAN {vid}")
