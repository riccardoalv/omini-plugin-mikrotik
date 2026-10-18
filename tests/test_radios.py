import pytest
from omini_sdk import Change, PluginError

import omini_mikrotik.wireless as wireless
from omini_mikrotik.collect import apply, collect
from omini_mikrotik.wireless import channel_to_freq, freq_to_channel, width_of


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(wireless, "SLEEP", lambda s: None)


def channel(radio, ch):
    return Change(
        device="48:a9:8a:11:22:01", kind="wifi.channel", value={"radio": radio, "channel": ch}
    )


def radios(rt):
    return {r.name: r for r in rt.radios}


def test_reads_the_radios(routeros, cfg):
    [rt] = collect(cfg)
    r = radios(rt)
    five, two = r["wifi1"], r["wifi2"]
    # No fixed frequency: automatic, on the channel the monitor says.
    assert (five.band, five.channel, five.width_mhz, five.auto, five.tx_power_dbm) == (
        "5ghz",
        36,
        80,
        True,
        23,
    )
    assert (two.band, two.channel, two.width_mhz, two.auto, two.enabled) == (
        "2.4ghz",
        6,
        20,
        False,
        True,
    )
    assert rt.wireless_uplink is False


def test_monitor_posts_are_read_only(routeros, cfg):
    collect(cfg)
    posts = {p for m, p in routeros.calls if m == "POST"}
    assert "/rest/interface/wifi/monitor" in posts
    assert not routeros.writes


def test_a_station_is_a_wireless_uplink(routeros, cfg):
    routeros.rows("/interface/wifi").append(
        {
            ".id": "*F",
            "name": "wifi3",
            "master-interface": "wifi2",
            "configuration.mode": "station-bridge",
            "disabled": "false",
        }
    )
    [rt] = collect(cfg)
    assert rt.wireless_uplink is True
    assert "wifi3" not in radios(rt)  # a virtual interface is no radio


def test_legacy_wireless_radios(routeros, cfg):
    del routeros.routes["/rest/interface/wifi"]
    routeros.routes["/rest/interface/wireless"] = [
        {
            ".id": "*5",
            "name": "wlan1",
            "mode": "ap-bridge",
            "band": "2ghz-b/g/n",
            "frequency": "auto",
            "channel-width": "20/40mhz-XX",
            "ssid": "Home",
            "disabled": "false",
            "running": "true",
        },
        {
            ".id": "*6",
            "name": "wlan2",
            "mode": "station",
            "band": "5ghz-a/n/ac",
            "frequency": "5745",
            "channel-width": "20/40/80mhz-XXXX",
            "disabled": "false",
            "running": "false",
        },
    ]
    routeros.routes["/rest/interface/wireless/monitor"] = [
        {"name": "wlan1", "status": "running-ap", "channel": "2412/20-Ce/gn", "frequency": "2412"}
    ]
    [rt] = collect(cfg)
    r = radios(rt)
    assert (r["wlan1"].channel, r["wlan1"].auto, r["wlan1"].width_mhz) == (1, True, 40)
    assert (r["wlan2"].band, r["wlan2"].channel, r["wlan2"].auto) == ("5ghz", 149, False)
    assert rt.wireless_uplink is True


def test_capsman_radios_are_automatic(routeros, cfg):
    routeros.routes["/rest/interface/wifi/cap"] = {
        "enabled": "true",
        "discovery-interfaces": "bridge",
    }
    [rt] = collect(cfg)
    assert all(r.auto for r in rt.radios)


def test_radios_are_optional(routeros, cfg):
    routeros.forbidden.add("/rest/interface/wifi")
    [rt] = collect(cfg)
    assert rt.radios is None and rt.wireless_uplink is None and rt.interfaces


def test_moves_a_radio(routeros, cfg):
    [rt] = apply(cfg, channel("wifi2", 11))
    assert routeros.writes == [("PATCH", "/rest/interface/wifi/*E", {"channel.frequency": "2462"})]
    assert radios(rt)["wifi2"].channel == 11


def test_moves_an_automatic_radio_when_asked(routeros, cfg):
    [rt] = apply(cfg, channel("wifi1", 44))
    assert routeros.row("/interface/wifi", name="wifi1")["channel.frequency"] == "5220"
    assert (radios(rt)["wifi1"].channel, radios(rt)["wifi1"].auto) == (44, False)


def test_a_radio_that_does_not_move_is_undone(routeros, cfg):
    routeros.radio_stuck = True
    with pytest.raises(PluginError, match="did not come up on channel 1"):
        apply(cfg, channel("wifi2", 1))
    assert routeros.row("/interface/wifi", name="wifi2")["channel.frequency"] == "2437"


def test_refuses_capsman_and_bad_channels(routeros, cfg):
    routeros.row("/interface/wifi", name="wifi2")["configuration.manager"] = "capsman"
    with pytest.raises(PluginError, match="managed by CAPsMAN"):
        apply(cfg, channel("wifi2", 1))
    with pytest.raises(PluginError, match="not a 5 GHz channel"):
        apply(cfg, channel("wifi1", 6))
    with pytest.raises(PluginError, match="no Wi-Fi radio named wifi9"):
        apply(cfg, channel("wifi9", 6))
    assert not routeros.writes


def test_legacy_channel_change(routeros, cfg):
    del routeros.routes["/rest/interface/wifi"]
    routeros.routes["/rest/interface/wireless"] = [
        {
            ".id": "*5",
            "name": "wlan1",
            "mode": "ap-bridge",
            "band": "2ghz-b/g/n",
            "frequency": "auto",
            "disabled": "false",
            "running": "true",
        }
    ]
    routeros.routes["/rest/interface/wireless/monitor"] = [
        {"name": "wlan1", "channel": "2412/20-Ce/gn"}
    ]
    [rt] = apply(cfg, channel("wlan1", 6))
    assert routeros.writes == [("PATCH", "/rest/interface/wireless/*5", {"frequency": "2437"})]
    assert radios(rt)["wlan1"].channel == 6


def test_frequencies():
    assert [freq_to_channel(f, None) for f in (2412, 2437, 2462, 2484, 5180, 5745)] == [
        1,
        6,
        11,
        14,
        36,
        149,
    ]
    assert freq_to_channel(5955, "6ghz") == 1 and freq_to_channel(2413, None) is None
    assert channel_to_freq(11, "2.4ghz") == 2462 and channel_to_freq(36, "5ghz") == 5180
    assert channel_to_freq(15, "2.4ghz") is None
    assert width_of("20/40mhz-Ce") == 40 and width_of("10mhz") is None
