# omini-plugin-mikrotik

[Omini](https://github.com/riccardoalv/omini) plugin for **MikroTik RouterOS** routers, switches and access points, through the official RouterOS v7 [REST API](https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API). Read-only: it never changes the device's configuration and never starts an update check.

> **Experimental.** This plugin was built from MikroTik's documentation and tested against answers shaped like the documented ones, **not on real hardware** yet. Reports from MikroTik owners (and anonymized answers for the test fixtures) are welcome.

## What it reads

| Data | Used for |
|---|---|
| Identity, model, serial, RouterOS version, CPU load, memory, uptime, flash use | Device details and health |
| Temperatures (`/system/health`) | Health (hottest CPU temperature) |
| Pending updates: RouterOS (from the device's **own last check**) and RouterBOOT firmware | Health (*update pending*) |
| Interfaces: name, comment, type, MAC, running, traffic and error counters | Ports on the map, traffic |
| Ethernet link rate and duplex, **SFP diagnostics** (vendor, part, serial, wavelength, temperature, voltage, bias, TX/RX power) | Link speed, port front view (RJ45 / SFP / QSFP) |
| Bridges and their ports, bonding members | Bridge members and speed |
| Bridge VLAN table (PVID, tagged ports), VLAN interfaces with their subnets | Port VLAN membership, VLAN list |
| Bridge host table (MAC → port, VLAN) | Which client sits behind which port |
| ARP table, bound DHCP leases | Clients and their names |
| Neighbors (MNDP, LLDP, CDP) | Links between devices |
| Wi-Fi clients (`wifi` / wifiwave2 and legacy `wireless`): SSID, band, signal, link rates, current traffic (`wifi` only) | Wi-Fi clients on the map |
| Default routes of the main table | WAN uplinks (`wan` interface) and gateway status |

Role: **router** when the device has a default route; otherwise **ap** if it has Wi-Fi interfaces, else **switch**.

## Requirements

- **RouterOS 7.1 or later** (the REST API does not exist on RouterOS 6).
- The **www-ssl** service enabled, with a certificate (the REST API is served by the web server). Plain HTTP (`www`) also works but sends the password unencrypted: use it only on a trusted network.

## Create a read-only user

Run in a RouterOS terminal (WinBox → New Terminal, or SSH):

```routeros
# 1. A group that can only read, through the API and REST API.
/user group add name=omini-read policy=read,api,rest-api comment="Omini (read-only)"

# 2. A user in that group. Restrict "address" to the Omini host's IP.
/user add name=omini group=omini-read password="a-long-random-password" address=192.168.88.10/32

# 3. HTTPS for the REST API (skip if www-ssl already has a certificate).
/certificate add name=omini-https common-name=router.lan days-valid=3650
/certificate sign omini-https
/ip service set www-ssl certificate=omini-https disabled=no
```

The group has no `write`, `policy`, `reboot`, `test`, `sensitive` or login policies other than `api` and `rest-api` ([user group policies](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User)). If your RouterOS version does not offer the `rest-api` policy, create the group with `read,api` (not verified on hardware).

Then, in Omini, **Integrations → Add → MikroTik RouterOS**:

| Field | Value |
|---|---|
| Address | `https://<router IP>` (e.g. `https://192.168.88.1`) |
| Username | `omini` |
| Password | the user's password (stored encrypted, never logged) |
| Verify the TLS certificate | off for the self-signed certificate above; on if the router has a trusted one |

**Test connection** says which parts the user cannot read, if any.

## How it works

Every request is a `GET` to `https://<router>/rest/<menu>` with HTTP Basic authentication, except one: the Ethernet **monitor** is a console command, which the REST API runs with a `POST`. The plugin sends `POST /rest/interface/ethernet/monitor` with `{"numbers": "<every ethernet port>", "once": ""}`: `once` reads the current state a single time and returns. It changes nothing. RouterOS returns every value as a string (`"true"`, `"1Gbps"`, `"1w2d3h"`); the plugin parses them and leaves anything it cannot parse empty.

| Menu | Read | Docs |
|---|---|---|
| `/system/resource` | version, board, CPU load, memory, uptime, flash (**required**) | [Resource](https://help.mikrotik.com/docs/display/ROS/Resource) |
| `/system/identity` | device name | [Identity](https://help.mikrotik.com/docs/display/ROS/Identity) |
| `/system/routerboard` | model, serial, RouterBOOT current/upgrade firmware | [RouterBOARD](https://help.mikrotik.com/docs/display/ROS/RouterBOARD) |
| `/system/package/update` | installed version, latest version **only if already known** (`check-for-updates` is never called) | [Upgrading and installation](https://help.mikrotik.com/docs/display/ROS/Upgrading+and+installation) |
| `/system/health` | temperatures | [Health](https://help.mikrotik.com/docs/display/ROS/Health) |
| `/interface` | ports, counters | [Interface stats and monitor-traffic](https://help.mikrotik.com/docs/display/ROS/Interface+stats+and+monitor-traffic) |
| `/interface/ethernet`, `POST /interface/ethernet/monitor` | factory names, speed setting, link rate, duplex, SFP DOM | [Ethernet](https://help.mikrotik.com/docs/spaces/ROS/pages/8323191/Ethernet) |
| `/interface/bridge`, `/interface/bridge/port`, `/interface/bridge/vlan`, `/interface/bridge/host` | bridges, members, VLAN filtering, MAC table (`local` entries left out) | [Bridging and Switching](https://help.mikrotik.com/docs/display/ROS/Bridging+and+Switching) |
| `/interface/vlan` | VLAN id and parent | [VLAN](https://help.mikrotik.com/docs/display/ROS/VLAN) |
| `/interface/bonding` | LAG members | [Bonding](https://help.mikrotik.com/docs/display/ROS/Bonding) |
| `/interface/pppoe-client` | the port a PPPoE WAN runs on | [PPPoE](https://help.mikrotik.com/docs/display/ROS/PPPoE) |
| `/ip/address` | interface IPs, VLAN subnets | [IP Addressing](https://help.mikrotik.com/docs/display/ROS/IP+Addressing) |
| `/ip/route` | default routes of the `main` table → WAN interface and gateway status (`active`) | [IP Routing](https://help.mikrotik.com/docs/display/ROS/IP+Routing) |
| `/ip/arp` | ARP (failed / incomplete entries left out) | [ARP](https://help.mikrotik.com/docs/display/ROS/ARP) |
| `/ip/dhcp-server/lease` | leases with `status=bound` | [DHCP](https://help.mikrotik.com/docs/display/ROS/DHCP) |
| `/ip/neighbor` | MNDP / LLDP / CDP neighbors | [Neighbor discovery](https://help.mikrotik.com/docs/spaces/ROS/pages/24805517/Neighbor+discovery) |
| `/interface/wifi`, `/interface/wifi/registration-table` | Wi-Fi clients (`wifi` package, RouterOS 7.13+) | [WiFi](https://help.mikrotik.com/docs/spaces/ROS/pages/224559120/WiFi) |
| `/interface/wireless`, `/interface/wireless/registration-table` | Wi-Fi clients (legacy `wireless` package) | [Wireless Interface](https://help.mikrotik.com/docs/spaces/ROS/pages/8978446/Wireless+Interface) |

Only `/system/resource` is required. Every other menu is optional: one that is missing (package not installed), forbidden or failing is skipped and the rest of the collection goes on. Requests time out after 10 s.

Mapping notes:

- **Device key:** the factory MAC of `ether1` (stable even when the bridge MAC changes), else the lowest port MAC.
- **Port connector:** from the port's **factory name** (users rename ports): `ether*` → RJ45, `sfp*` / `sfp-sfpplus*` / `sfp28-*` → SFP, `qsfp*` → QSFP; a combo port is SFP or QSFP when a module is in.
- **Link speed:** the monitor's `rate` while the link is up; a port with auto-negotiation off falls back to its speed setting. A bridge runs at its fastest member's speed; a WAN on a VLAN or PPPoE follows its parent down to the physical port.
- **VLANs:** only for bridges with `vlan-filtering=yes`: untagged = the bridge port's PVID, tagged = the bridge VLAN entries that list the port.
- **Wi-Fi clients** are labeled "SSID · band" (e.g. `Home · 5 GHz`). With the `wifi` package, the AP's `tx-bits-per-second` is the client's download.
- **Updates:** RouterOS knows the latest version only after a check (by hand, or a script). When `latest-version` is absent, *update available* stays unknown. A RouterBOOT firmware newer than the running one (`upgrade-firmware` ≠ `current-firmware`) counts as one pending update.

## Known limitations

- **Not tested on hardware** (see above). Details that could not be verified without a device:
  - the exact REST output of `ethernet/monitor` (field names are the documented console ones; values are parsed with or without units);
  - whether the `read` policy is enough to run the monitor through REST on every version (if not, link speed and SFP diagnostics are skipped, the rest still works);
  - the property names of `/interface/wifi` in REST (`configuration.ssid`, `channel.band`) when the SSID is set through a configuration profile — the registration table's own `ssid`/`band` are used first;
  - the error status RouterOS returns for a menu the user's group cannot read (403 is treated as "no permission", 400/404 as "not available").
- **CAPsMAN:** clients of APs managed by CAPsMAN (`/caps-man/registration-table`) are not read yet; add each AP as its own integration.
- **Gateway latency / loss:** RouterOS has no per-gateway latency in its route table; gateways show only up/down (from the route's `active` flag).
- **IPv6** routes and leases are not read.
- **Traffic** is the average between two collections, computed by Omini from the counters.

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against answers shaped like RouterOS's (tests/fixtures)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-mikrotik make run` in the Omini repository; every collection uses the current code.

## License

MIT
