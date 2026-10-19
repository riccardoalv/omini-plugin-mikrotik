"""Wi-Fi radios of a RouterOS access point (Device.radios, wireless_uplink)
and the wifi.channel change.

Two packages: ``wifi`` (wifiwave2, RouterOS 7.13+: /interface/wifi, with
``channel.frequency``, ``channel.band``, ``channel.width`` and
``configuration.mode``) and the legacy ``wireless`` (/interface/wireless,
with ``frequency``, ``band``, ``channel-width`` and ``mode``). A radio is an
interface without ``master-interface`` (the others are extra SSIDs on it);
dynamic interfaces (a CAPsMAN controller's remote radios) are not this
device's. The channel a radio is on now comes from the read-only monitor
(``POST <menu>/monitor`` with ``once``), else from its fixed frequency.

A radio managed by CAPsMAN takes its channel from the controller: it is
reported as automatic (the controller picks it) and wifi.channel refuses it.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from omini_sdk import Device, PluginError, log
from omini_sdk.changes import apply_verify
from omini_sdk.models import Radio

from omini_mikrotik.client import Client, Forbidden, NotFound, Unavailable
from omini_mikrotik.common import as_list, as_object, flag, names, number
from omini_mikrotik.journal import Journal

WIFI = "/interface/wifi"
WIRELESS = "/interface/wireless"
WIDTHS = (20, 40, 80, 160, 320)
SLEEP = time.sleep  # replaced in tests
VERIFY_READS = 8  # monitor reads, one second apart, for the radio to move


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def band_of(value: Any) -> str | None:
    """'2ghz-ax' / '2ghz-b/g/n' → 2.4ghz; '5ghz-ac' → 5ghz; '6ghz-ax' → 6ghz."""
    v = text(value).lower()
    if v.startswith(("2ghz", "2.4ghz")):
        return "2.4ghz"
    if v.startswith("5ghz"):
        return "5ghz"
    if v.startswith("6ghz"):
        return "6ghz"
    return None


def band_of_freq(mhz: int) -> str | None:
    if 2400 <= mhz <= 2500:
        return "2.4ghz"
    if 5150 <= mhz <= 5925:
        return "5ghz"
    if 5925 < mhz <= 7125:
        return "6ghz"
    return None


def freq_to_channel(mhz: int | None, band: str | None) -> int | None:
    if mhz is None:
        return None
    b = band or band_of_freq(mhz)
    if b == "2.4ghz":
        if mhz == 2484:
            return 14
        ch = (mhz - 2407) // 5
        return ch if 1 <= ch <= 13 and (mhz - 2407) % 5 == 0 else None
    if b == "5ghz":
        ch = (mhz - 5000) // 5
        return ch if 1 <= ch <= 196 and (mhz - 5000) % 5 == 0 else None
    if b == "6ghz":
        ch = (mhz - 5950) // 5
        return ch if 1 <= ch <= 233 and (mhz - 5950) % 5 == 0 else None
    return None


def channel_to_freq(ch: int, band: str) -> int | None:
    if band == "2.4ghz":
        return 2484 if ch == 14 else 2407 + 5 * ch if 1 <= ch <= 13 else None
    if band == "5ghz":
        return 5000 + 5 * ch if 32 <= ch <= 196 else None
    if band == "6ghz":
        return 5950 + 5 * ch if 1 <= ch <= 233 else None
    return None


def single_freq(value: Any) -> int | None:
    """A fixed frequency: '2437' → 2437; 'auto', '' (unset), a list
    '2412,2437' or a range '5180-5240' (RouterOS picks among them) → None."""
    v = text(value)
    return int(v) if re.fullmatch(r"\d{4}", v) else None


def width_of(value: Any) -> int | None:
    """'20mhz' → 20, '20/40mhz-Ce' → 40, '20/40/80mhz' → 80, '20/40/80/160mhz' → 160."""
    m = re.match(r"\s*([\d/]+)\s*mhz", text(value), re.IGNORECASE)
    if not m:
        return None
    widths = [int(x) for x in m.group(1).split("/") if x.isdigit()]
    w = max(widths, default=0)
    return w if w in WIDTHS else None


def monitor_freq(m: dict[str, Any]) -> int | None:
    """The monitor's 'channel' ('2437/ax/Ce', '5180/20-Ce/ac') or 'frequency'."""
    for key in ("channel", "frequency"):
        mm = re.match(r"\s*(\d{4})", text(m.get(key)))
        if mm:
            return int(mm.group(1))
    return None


@dataclass
class Package:
    """One package's interfaces, as a radio reads and changes them."""

    menu: str  # WIFI or WIRELESS
    rows: list[dict[str, Any]]
    cap: dict[str, Any] = field(default_factory=dict)

    @property
    def legacy(self) -> bool:
        return self.menu == WIRELESS

    def mode(self, r: dict[str, Any]) -> str:
        return text(r.get("mode" if self.legacy else "configuration.mode")).lower()

    def band(self, r: dict[str, Any]) -> str | None:
        return band_of(r.get("band" if self.legacy else "channel.band"))

    def freq(self, r: dict[str, Any]) -> int | None:
        return single_freq(r.get("frequency" if self.legacy else "channel.frequency"))

    def width(self, r: dict[str, Any]) -> int | None:
        return width_of(r.get("channel-width" if self.legacy else "channel.width"))

    def radios(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r.get("name") and not text(r.get("master-interface")) and not flag(r.get("dynamic"))
        ]

    def managed(self, r: dict[str, Any]) -> bool:
        """Under CAPsMAN: the legacy CAP lists the interface; with the wifi
        package the interface's manager is capsman, or the CAP is on and the
        interface is not kept local."""
        cap_on = bool(flag(self.cap.get("enabled")))
        if self.legacy:
            return cap_on and text(r.get("name")) in names(self.cap.get("interfaces"))
        manager = text(r.get("configuration.manager")).lower()
        return manager == "capsman" or (cap_on and manager != "local")


def read_packages(c: Client) -> list[Package]:
    """The packages this device has (a missing one is skipped; a forbidden
    read raises Forbidden, like any optional read)."""
    out = []
    for menu in (WIFI, WIRELESS):
        try:
            rows = as_list(c.get(menu))
        except NotFound:
            continue
        cap: dict[str, Any] = {}
        with contextlib.suppress(Forbidden, NotFound, Unavailable):
            cap = as_object(c.get(f"{menu}/cap"))
        out.append(Package(menu, rows, cap))
    return out


def monitor(c: Client, menu: str, name: str) -> dict[str, Any]:
    """One read of the radio's state (``once``: it returns at once and changes nothing)."""
    rows = as_list(c.post(f"{menu}/monitor", {"numbers": name, "once": ""}))
    return rows[0] if rows else {}


def try_monitor(c: Client, menu: str, name: str) -> dict[str, Any] | None:
    try:
        return monitor(c, menu, name)
    except (Forbidden, NotFound, Unavailable) as e:
        log.info("no monitor of %s: %s", name, e)
        return None


def radio(c: Client, pkg: Package, r: dict[str, Any]) -> Radio | None:
    name = text(r.get("name"))
    disabled = flag(r.get("disabled"))
    fixed = pkg.freq(r)
    m = try_monitor(c, pkg.menu, name) if flag(r.get("running")) else None
    now = monitor_freq(m or {})
    band = pkg.band(r) or band_of_freq(now or fixed or 0)
    if band is None:
        return None
    managed = pkg.managed(r)
    power = number((m or {}).get("tx-power"))
    return Radio(
        name=name,
        band=band,
        channel=freq_to_channel(now or fixed, band),
        width_mhz=pkg.width(r),
        auto=True if managed else fixed is None,
        tx_power_dbm=power,
        enabled=None if disabled is None else not disabled,
    )


def station(pkg: Package, r: dict[str, Any]) -> bool:
    return pkg.mode(r).startswith("station") and not flag(r.get("disabled"))


def radios_field(c: Client, pkgs: list[Package]) -> dict[str, Any]:
    """{"radios", "wireless_uplink"} for an Omini that knows the fields; none
    when the device has no Wi-Fi (or its interfaces could not be read)."""
    if "radios" not in Device.model_fields or not any(p.rows for p in pkgs):
        return {}
    out = [x for p in pkgs for r in p.radios() if (x := radio(c, p, r)) is not None]
    uplink = any(station(p, r) for p in pkgs for r in p.rows)
    return {"radios": out or None, "wireless_uplink": uplink}


# --- wifi.channel ------------------------------------------------------------------


def find_radio(c: Client, name: str) -> tuple[Package, dict[str, Any]]:
    try:
        pkgs = read_packages(c)
    except Forbidden as e:
        raise PluginError("the RouterOS user cannot read the Wi-Fi interfaces") from e
    for p in pkgs:
        for r in p.radios():
            if text(r.get("name")) == name:
                return p, r
    raise PluginError(f"the device has no Wi-Fi radio named {name or '(none)'}")


def set_channel(c: Client, j: Journal, value: Any) -> None:
    """Sets the radio's fixed frequency (``channel.frequency`` with the wifi
    package, ``frequency`` with the legacy one) to the channel's, then reads
    the interface and its monitor again until the radio is on it."""
    if not isinstance(value, dict):
        raise PluginError(f"this plugin cannot apply wifi.channel with value {value!r}")
    name = text(value.get("radio"))
    try:
        ch = int(value.get("channel"))
    except (TypeError, ValueError) as e:
        raise PluginError(f"{value.get('channel')!r} is not a Wi-Fi channel") from e
    pkg, row = find_radio(c, name)
    if pkg.managed(row):
        raise PluginError(
            f"{name} is managed by CAPsMAN: change its channel on the CAPsMAN controller"
        )
    m = try_monitor(c, pkg.menu, name) if flag(row.get("running")) else None
    band = pkg.band(row) or band_of_freq(monitor_freq(m or {}) or pkg.freq(row) or 0)
    if band is None:
        raise PluginError(f"RouterOS did not say the band of {name}: nothing was changed")
    freq = channel_to_freq(ch, band)
    if freq is None:
        raise PluginError(f"{ch} is not a {band.replace('ghz', ' GHz')} channel")
    key = "frequency" if pkg.legacy else "channel.frequency"
    j.set(pkg.menu, row, {key: str(freq)}, f"the channel of {name}")
    _, after = find_radio(c, name)
    if pkg.freq(after) != freq:
        raise PluginError(f"RouterOS did not set {name} to channel {ch}")
    if flag(after.get("disabled")) or m is None:
        return  # a radio that is off, or without a monitor: its setting is all there is
    # Read the monitor (once a second, VERIFY_READS times) until the radio is
    # on it; the Journal undoes the change in apply() when it is not.
    apply_verify(
        lambda: None,
        lambda: monitor_freq(try_monitor(c, pkg.menu, name) or {}) == freq,
        timeout=VERIFY_READS - 1,
        interval=1,
        message=f"{name} did not come up on channel {ch}",
        sleep=lambda s: SLEEP(s),
    )
