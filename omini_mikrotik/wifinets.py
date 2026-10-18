"""Wi-Fi networks of a standalone RouterOS access point (Device.wifi_networks)
and the wifi.set and wifi.delete changes.

The ``wifi`` package (RouterOS 7.13+): every AP interface on /interface/wifi
(a radio, or an extra SSID on it with ``master-interface``) has its SSID in
``configuration.ssid``, its VLAN in ``datapath.vlan-id`` and its security in
``security.authentication-types`` (and ``security.passphrase``), set on the
interface itself or in a named profile (``configuration``, ``datapath``,
``security``: /interface/wifi/configuration, /datapath, /security); a value
on the interface wins over its profile's. The legacy ``wireless`` package:
``ssid``, ``vlan-mode=use-tag`` with ``vlan-id``, and a security profile
(/interface/wireless/security-profiles: ``mode``, ``authentication-types``).

Changes are made on the interface itself (never on a shared profile), and
not under CAPsMAN (its controller owns the interfaces). The passphrase is
written to the device only: never logged, never returned, never kept.
With the legacy package only an existing SSID is changed (its name, VLAN,
on or off): its passphrase lives in a security profile other SSIDs may
share, so a new SSID or a new passphrase is refused there.
"""

from __future__ import annotations

from typing import Any

from omini_sdk import Device, PluginError, WifiNetwork, log

from omini_mikrotik.client import Client, Failed, Forbidden, NotFound
from omini_mikrotik.common import as_list, counter, flag, names
from omini_mikrotik.journal import Journal
from omini_mikrotik.wireless import WIFI, Package, read_packages, text

BRIDGE_PORTS = "/interface/bridge/port"
AP_MODES = ("ap", "")  # the wifi package; an extra SSID may leave the mode to its profile
LEGACY_AP_MODES = ("ap-bridge", "bridge")


def security_of(types: Any) -> str | None:
    """Authentication types → WifiNetwork.security; WPA (1), EAP, or a mix
    with them is none of Omini's: empty."""
    t = set(names(types))
    if not t:
        return None
    if t == {"wpa2-psk"}:
        return "wpa2"
    if t == {"wpa3-psk"}:
        return "wpa3"
    if t == {"wpa2-psk", "wpa3-psk"}:
        return "wpa2wpa3"
    return None


class Profiles:
    """The named profiles an interface may take its settings from."""

    def __init__(self, c: Client, pkg: Package):
        self.pkg = pkg
        self.menus: dict[str, dict[str, dict[str, Any]]] = {}
        wanted = ("security-profiles",) if pkg.legacy else ("configuration", "datapath", "security")
        for kind in wanted:
            try:
                rows = as_list(c.get(f"{pkg.menu}/{kind}"))
            except (Forbidden, NotFound, Failed) as e:
                log.info("no %s/%s: %s", pkg.menu, kind, e)
                rows = []
            self.menus[kind] = {text(r.get("name")): r for r in rows if r.get("name")}

    def value(self, r: dict[str, Any], kind: str, key: str) -> str:
        """The interface's ``<kind>.<key>``, else its ``<kind>`` profile's ``<key>``."""
        own = r.get(f"{kind}.{key}")
        if own is not None and text(own) != "":
            return text(own)
        profile = self.menus.get(kind, {}).get(text(r.get(kind)), {})
        return text(profile.get(key))


def ssid_of(pkg: Package, prof: Profiles, r: dict[str, Any]) -> str:
    return text(r.get("ssid")) if pkg.legacy else prof.value(r, "configuration", "ssid")


def is_ap(pkg: Package, prof: Profiles, r: dict[str, Any]) -> bool:
    if flag(r.get("dynamic")) or not r.get("name"):
        return False
    if pkg.legacy:
        return text(r.get("mode")).lower() in LEGACY_AP_MODES
    return prof.value(r, "configuration", "mode").lower() in AP_MODES


def vlan_of(pkg: Package, prof: Profiles, r: dict[str, Any]) -> int | None:
    if pkg.legacy:
        if text(r.get("vlan-mode")) != "use-tag":
            return None
        vid = counter(r.get("vlan-id"))
    else:
        vid = counter(prof.value(r, "datapath", "vlan-id"))
    return vid if vid and 2 <= vid <= 4094 else None  # VLAN 1: the untagged LAN


def security(pkg: Package, prof: Profiles, r: dict[str, Any]) -> str | None:
    if pkg.legacy:
        p = prof.menus.get("security-profiles", {}).get(text(r.get("security-profile")), {})
        mode = text(p.get("mode"))
        if mode == "none":
            return "open"
        return security_of(p.get("authentication-types")) if mode == "dynamic-keys" else None
    return security_of(prof.value(r, "security", "authentication-types"))


def aps(c: Client, pkg: Package) -> tuple[Profiles, list[dict[str, Any]]]:
    prof = Profiles(c, pkg)
    return prof, [r for r in pkg.rows if is_ap(pkg, prof, r) and ssid_of(pkg, prof, r)]


def networks(c: Client, pkgs: list[Package]) -> list[WifiNetwork]:
    """Each SSID the device broadcasts, once whatever the radios: on when one
    of its interfaces is."""
    out: dict[str, dict[str, Any]] = {}
    for pkg in pkgs:
        prof, rows = aps(c, pkg)
        for r in rows:
            ssid = ssid_of(pkg, prof, r)
            on = not flag(r.get("disabled"))
            if ssid in out:
                out[ssid]["enabled"] = out[ssid]["enabled"] or on
                continue
            out[ssid] = {
                "ssid": ssid,
                "vlan": vlan_of(pkg, prof, r),
                "security": security(pkg, prof, r),
                "enabled": on,
            }
    return [WifiNetwork(**v) for v in out.values()]


def networks_field(c: Client, pkgs: list[Package]) -> dict[str, Any]:
    if "wifi_networks" not in Device.model_fields or not any(p.rows for p in pkgs):
        return {}
    try:
        return {"wifi_networks": networks(c, pkgs) or None}
    except PluginError:
        raise
    except Exception:
        log.exception("could not read the Wi-Fi networks")
        return {}


# --- changes -------------------------------------------------------------------


def packages(c: Client) -> list[Package]:
    try:
        pkgs = [p for p in read_packages(c) if p.rows]
    except Forbidden as e:
        raise PluginError("the RouterOS user cannot read the Wi-Fi interfaces") from e
    if not pkgs:
        raise PluginError("the device has no Wi-Fi interfaces")
    return pkgs


def check_ssid(value: Any) -> str:
    ssid = text(value)
    if not 1 <= len(ssid.encode()) <= 32:
        raise PluginError(f"{value!r} is not an SSID (1 to 32 bytes)")
    return ssid


def check_vlan(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4094:
        raise PluginError(f"{value!r} is not a VLAN id (1 to 4094)")
    return None if value == 1 else value


def check_password(value: Any) -> str:
    password = "" if value is None else str(value)
    if password and not 8 <= len(password) <= 63:
        raise PluginError("the Wi-Fi password must be 8 to 63 characters")
    return password


def named(c: Client, pkg: Package, ssid: str) -> tuple[Profiles, list[dict[str, Any]]]:
    prof, rows = aps(c, pkg)
    return prof, [r for r in rows if ssid_of(pkg, prof, r) == ssid]


def refuse_capsman(pkg: Package, rows: list[dict[str, Any]], ssid: str) -> None:
    if any(pkg.managed(r) for r in rows):
        raise PluginError(
            f"{ssid} is on interfaces managed by CAPsMAN: change it on the CAPsMAN controller"
        )


def bridge_of(c: Client, radio: dict[str, Any]) -> str:
    """The bridge a new SSID joins: its radio's datapath bridge, else the bridge the radio is in."""
    own = text(radio.get("datapath.bridge"))
    if own:
        return own
    try:
        ports = as_list(c.get(BRIDGE_PORTS))
    except (Forbidden, NotFound) as e:
        raise PluginError("the RouterOS user cannot read the bridge ports") from e
    bridge = next(
        (text(p.get("bridge")) for p in ports if p.get("interface") == radio.get("name")), ""
    )
    if not bridge:
        raise PluginError(
            f"{radio.get('name')} is in no bridge: a new Wi-Fi network would reach no network"
        )
    return bridge


def wifi_set(c: Client, j: Journal, value: Any) -> None:
    if not isinstance(value, dict):
        raise PluginError("wifi.set needs a Wi-Fi network")
    ssid = check_ssid(value.get("ssid"))
    previous = text(value.get("previous")) or ssid
    vid = check_vlan(value.get("vlan"))
    password = check_password(value.get("password"))
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise PluginError(f"{enabled!r} is not on or off")
    pkgs = packages(c)
    found = [(p, *named(c, p, previous)) for p in pkgs]
    # The package that has the network; a new one goes on the wifi package
    # when both are there.
    pkg, prof, rows = next((f for f in found if f[2]), found[0])
    if not rows and previous != ssid:
        raise PluginError(f"the device has no Wi-Fi network named {previous}")
    refuse_capsman(pkg, rows, previous)
    if pkg.legacy:
        legacy_set(j, pkg, rows, ssid, vid, password, enabled)
    elif rows:
        for r in rows:
            body: dict[str, str] = {}
            if ssid_of(pkg, prof, r) != ssid:
                body["configuration.ssid"] = ssid
            if vlan_of(pkg, prof, r) != vid:
                body["datapath.vlan-id"] = str(vid or 1)
            if password:
                body["security.passphrase"] = password
                if not prof.value(r, "security", "authentication-types"):
                    body["security.authentication-types"] = "wpa2-psk"
            if enabled is not None:
                body["disabled"] = "false" if enabled else "true"
            j.set(WIFI, r, body, f"the Wi-Fi network {ssid}", secret=("security.passphrase",))
    else:
        create(c, j, pkg, prof, ssid, vid, password, enabled)
    verify_set(c, pkg, ssid, previous, vid, enabled)


def legacy_set(
    j: Journal,
    pkg: Package,
    rows: list[dict[str, Any]],
    ssid: str,
    vid: int | None,
    password: str,
    enabled: bool | None,
) -> None:
    if not rows:
        raise PluginError(
            "the legacy wireless package keeps passwords in security profiles: "
            "create a new Wi-Fi network in WinBox or WebFig"
        )
    if password:
        raise PluginError(
            "the legacy wireless package keeps the password in a security profile other "
            "networks may share: change it in WinBox or WebFig"
        )
    for r in rows:
        body = {"ssid": ssid}
        body.update(
            {"vlan-mode": "use-tag", "vlan-id": str(vid)} if vid else {"vlan-mode": "no-tag"}
        )
        if enabled is not None:
            body["disabled"] = "false" if enabled else "true"
        j.set(pkg.menu, r, body, f"the Wi-Fi network {ssid}")


def create(
    c: Client,
    j: Journal,
    pkg: Package,
    prof: Profiles,
    ssid: str,
    vid: int | None,
    password: str,
    enabled: bool | None,
) -> None:
    """One extra SSID on every radio in AP mode (not under CAPsMAN), in the
    radio's bridge, with WPA2 (WPA3 on 6 GHz, which requires it)."""
    if not password:
        raise PluginError(f"a password is needed to create the Wi-Fi network {ssid}")
    radios = [
        r
        for r in pkg.radios()
        if prof.value(r, "configuration", "mode").lower() in AP_MODES and not pkg.managed(r)
    ]
    if not radios:
        raise PluginError("the device has no radio in AP mode outside CAPsMAN")
    for radio in radios:
        body = {
            "master-interface": text(radio.get("name")),
            "configuration.ssid": ssid,
            "configuration.mode": "ap",
            "security.authentication-types": (
                "wpa3-psk" if pkg.band(radio) == "6ghz" else "wpa2-psk"
            ),
            "security.passphrase": password,
            "datapath.bridge": bridge_of(c, radio),
            "disabled": "true" if enabled is False else "false",
        }
        if vid:
            body["datapath.vlan-id"] = str(vid)
        j.add(WIFI, body, f"the Wi-Fi network {ssid} on {radio.get('name')}")


def verify_set(
    c: Client, pkg: Package, ssid: str, previous: str, vid: int | None, enabled: bool | None
) -> None:
    pkg = next(p for p in packages(c) if p.menu == pkg.menu)
    got = {n.ssid: n for n in networks(c, [pkg])}
    n = got.get(ssid)
    if (
        n is None
        or n.vlan != vid
        or (enabled is not None and n.enabled is not enabled)
        or (previous != ssid and previous in got)
    ):
        raise PluginError(f"RouterOS did not apply the Wi-Fi network {ssid}")


def wifi_delete(c: Client, j: Journal, value: Any) -> None:
    """Removes the SSID's extra interfaces; a radio that carries it is turned
    off instead (a radio cannot be removed), unless it also carries another
    SSID that is on."""
    if not isinstance(value, dict):
        raise PluginError("wifi.delete needs {ssid}")
    ssid = check_ssid(value.get("ssid"))
    for pkg in packages(c):
        prof, rows = named(c, pkg, ssid)
        refuse_capsman(pkg, rows, ssid)
        for r in rows:
            name = text(r.get("name"))
            if text(r.get("master-interface")):
                j.remove(pkg.menu, r, f"the Wi-Fi network {ssid} on {name}")
                continue
            others = [
                ssid_of(pkg, prof, o)
                for o in pkg.rows
                if text(o.get("master-interface")) == name
                and not flag(o.get("disabled"))
                and ssid_of(pkg, prof, o) != ssid
            ]
            if others:
                raise PluginError(
                    f"{ssid} is the network of radio {name}, which also carries "
                    f"{', '.join(others)}: rename it instead"
                )
            j.set(pkg.menu, r, {"disabled": "true"}, f"the Wi-Fi network {ssid} on {name}")
    for pkg in packages(c):
        _, rows = named(c, pkg, ssid)
        if any(not flag(r.get("disabled")) for r in rows):
            raise PluginError(f"RouterOS did not remove the Wi-Fi network {ssid}")
