# omini-plugin-mikrotik

[Omini](https://github.com/riccardoalv/omini) plugin for **MikroTik RouterOS** routers, switches and access points, through the official RouterOS v7 [REST API](https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API). It reads the device and, when you ask from Omini, changes it: enables or disables an Ethernet port, sets a bridge port's VLANs and the bridge's VLANs, and creates, changes or removes a router's VLAN networks (interface, gateway, DHCP, access rules, mDNS) and DHCP reservations (see [What it changes](#what-it-changes)). It changes nothing else, never turns VLAN filtering on and never starts an update check.

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

## What it changes

Only what you ask from Omini, one change at a time. Every change is **applied, read back and checked**; if RouterOS refuses a step or does not read back as asked, the plugin **undoes the steps it already took** (newest first) and Omini shows the error. RouterOS keeps every change as soon as it is applied (there is no separate saved configuration), so there is no save step. RouterOS's own rollback (*safe mode*) belongs to a terminal session, which the REST API does not have: the plugin's undo takes its place.

Writes use the REST API's `PUT` (the console's `add`), `PATCH` (`set`), `DELETE` (`remove`) and `POST` for two commands: `<menu>/add` with `place-before` (firewall rules) and `/ip/dns/set`.

### Enable or disable a port (`port.enabled`)

On Ethernet ports (`/interface/ethernet`: `ether*`, `sfp*`, `qsfp*`, renamed or not). The plugin:

1. reads `GET /rest/interface/ethernet` to find the port by name and its `.id` (nothing is sent if the port is already in the state asked for);
2. sends `PATCH /rest/interface/ethernet/<.id>` with `{"disabled": "true"}` (disable) or `{"disabled": "false"}` (enable): the console's `/interface/ethernet set <port> disabled=yes|no`. Only `disabled` is sent; speed, auto-negotiation, comment, PoE and the port's bridge membership stay as they are;
3. reads `GET /rest/interface/ethernet` again and fails with an error if the port's `disabled` is not what was asked;
4. reads the device again (the same requests as a collection) for the map.

### A port's VLANs, the bridge's VLANs (`port.vlans`, `vlan.set`, `vlan.delete`)

On a **bridge with VLAN filtering** (`/interface/bridge` `vlan-filtering=yes`), the RouterOS way of running VLANs on a switch (CRS3xx/5xx, hEX, RB5009...). Omini offers it on the bridge's ports (`/interface/bridge/port`, bonds included).

- **A port's VLANs** (`port.vlans`): an access port (one untagged VLAN), or a trunk (tagged VLANs, with or without an untagged native VLAN). The plugin sets the bridge port's `pvid` (the untagged VLAN) and `frame-types` (`admit-only-untagged-and-priority-tagged` for an access port, `admit-all` for a trunk with a native VLAN, `admit-only-vlan-tagged` for one without) with `PATCH /rest/interface/bridge/port/<.id>`, and puts the port in, or takes it out of, the `tagged` list of each entry of the bridge VLAN table (`PATCH /rest/interface/bridge/vlan/<.id>`; a tagged VLAN without an entry gets one, `PUT /rest/interface/bridge/vlan`). RouterOS makes a port an untagged member of its PVID's VLAN by itself, so `untagged` lists are only ever cleared of the port when it leaves a VLAN. VLANs the port joins are written first, the port's PVID next, what it leaves last.
- **Create or rename a VLAN** (`vlan.set`): an entry of the bridge VLAN table (`PUT /rest/interface/bridge/vlan` with `bridge`, `vlan-ids` and the name as `comment`; no port yet), or a new `comment` for an existing one. Omini shows a VLAN's name from that comment, else its VLAN interface's comment, else the interface's name.
- **Delete a VLAN** (`vlan.delete`): `DELETE /rest/interface/bridge/vlan/<.id>`. Omini refuses VLAN 1 and a VLAN a port still uses; the plugin also refuses a VLAN the device has an interface on (a network: remove the network first).

**VLAN filtering is never turned on by Omini**: done without the right VLAN table, it cuts the device off. When it is off, these changes are refused with how to turn it on yourself: in WinBox or WebFig, *Bridge → (the bridge) → VLAN → VLAN Filtering* (console: `/interface bridge set bridge vlan-filtering=yes`), after the bridge VLAN table lets you keep reaching the device.

An entry of the bridge VLAN table may hold several VLANs (`vlan-ids=30-31` or `10,20`). A port can join or leave all of them at once, but Omini does not split such an entry: a change that needs one VLAN of it apart from the others (a port in VLAN 30 but not 31, renaming VLAN 30, deleting it) is refused, and the message says to split the entry in WinBox (*Bridge → VLANs*).

### A router's networks (`network.set`, `network.delete`)

On a **router** (a device with a default route), Omini shows each VLAN's network: every private, statically addressed interface on the LAN side (never the WAN, the interface of a default route, nor a tunnel): a VLAN interface (`/interface/vlan`) serves its VLAN, the bridge (or a port) with the LAN address is the untagged LAN, VLAN 1. For each: its name (the VLAN interface's comment, else the address's), the gateway (`/ip/address`), DHCP (the `/ip/dhcp-server` on the interface and the first range of its `/ip/pool`), access and mDNS (below).

**Create or change a network** (`network.set`):

1. A new VLAN gets a VLAN interface `vlan<id>` (`PUT /rest/interface/vlan`, the name as `comment`) on the router's bridge: the one with the LAN, else the one with VLAN filtering, or the port you name. A port that is a bridge member puts the VLAN on its bridge, and that port becomes a tagged member of the VLAN. With VLAN filtering on, the bridge itself (the router's CPU) is added to the VLAN's `tagged` list (`/interface/bridge/vlan`), so the router reaches it. The trunk toward the switches is the switch side: give its port the VLAN with `port.vlans`.
2. The gateway: `PUT /rest/ip/address` (a change: `PATCH` of the same address).
3. DHCP: a pool `omini-<interface>` (`/ip/pool`), a server `omini-<interface>` (`/ip/dhcp-server`) and the network (`/ip/dhcp-server/network`) with the gateway as router and DNS server. An existing server keeps its pool, whose `ranges` are set. Turning DHCP off disables the server (`disabled=yes`); it is not removed.
4. Access: the network's firewall rules (below).
5. mDNS: the interface joins or leaves `/ip/dns` `mdns-repeat-ifaces` (`POST /rest/ip/dns/set`), RouterOS's mDNS repeater (RouterOS **7.16** or later; before that, turning discovery on is refused, off is a no-op).

A network already there is changed in place. The LAN (VLAN 1) is never removed, and **the address Omini reaches the router through is never changed or removed** (do it from WinBox).

**Access** is read from, and written to, the `/ip/firewall/filter` rules whose `in-interface` is the network's interface:

| Access | Rules (in order) |
|---|---|
| Internet only (`internet`) | input: accept UDP and TCP 53 (DNS), drop the rest; forward: drop to the address list `omini-private` (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), accept the rest |
| Everything (`all`) | input: accept; forward: accept |
| Own network only (`local`) | input: accept UDP and TCP 53 (DNS), drop the rest; forward: drop |

Rules that are none of these read as **custom**, and a network set to *custom* keeps its rules untouched. Choosing a preset replaces every rule of that interface. Omini's rules (comments start with `Omini:`) go in with `POST /rest/ip/firewall/filter/add` and `place-before` the first drop rule of their chain (with the default configuration: before *defconf: drop all not coming from LAN* and *defconf: drop all from WAN not DSTNATed*), so the default *accept established, related* rules still come first; a router without a drop rule gets them at the end. The address list is created when missing (`/ip/firewall/address-list`, comment `Omini: private networks`). DNS for the network needs the router's DNS to answer it (`/ip/dns allow-remote-requests=yes`, the default configuration's setting); the plugin does not change it.

**Remove a network** (`network.delete`): its firewall rules, its place in the mDNS repeater, its DHCP server, the DHCP network of its subnet and the pool (only Omini's own, `Omini:` comment, and only when no other server uses it), its addresses and its VLAN interface; the bridge leaves the VLAN's `tagged` list. The VLAN itself stays in the bridge VLAN table (switch ports may still carry it): delete it with `vlan.delete`.

### DHCP reservations (`reservation.set`, `reservation.delete`)

A fixed address for a MAC is a **static lease** (`/ip/dhcp-server/lease`, `dynamic=no`): `PUT` with `mac-address`, `address`, the `server` of the network that holds the address and the name as `comment`; a MAC that already has one gets it changed (`PATCH`); removing it is `DELETE`. Omini shows the static leases as the router's reservations.

## Requirements

- **RouterOS 7.1 or later** (the REST API does not exist on RouterOS 6).
- The **www-ssl** service enabled, with a certificate (the REST API is served by the web server). Plain HTTP (`www`) also works but sends the password unencrypted: use it only on a trusted network.

## Create a user

Run in a RouterOS terminal (WinBox → New Terminal, or SSH):

```routeros
# 1. A group that reads and can change settings (write: for the changes
#    you ask from Omini), through the API and REST API only.
/user group add name=omini policy=read,write,api,rest-api comment="Omini"

# 2. A user in that group. Restrict "address" to the Omini host's IP.
/user add name=omini group=omini password="a-long-random-password" address=192.168.88.10/32

# 3. HTTPS for the REST API (skip if www-ssl already has a certificate).
/certificate add name=omini-https common-name=router.lan days-valid=3650
/certificate sign omini-https
/ip service set www-ssl certificate=omini-https disabled=no
```

The group has no `policy`, `reboot`, `test`, `sensitive`, `password` or login policies other than `api` and `rest-api` ([user group policies](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User)). The `write` policy covers every change (ports, bridge VLANs, VLAN interfaces, addresses, DHCP, firewall rules and address lists, the mDNS repeater): RouterOS has no finer policy for these menus. The plugin only writes what a change you ask for needs (see [What it changes](#what-it-changes)).

**Read-only instead:** create the group with `policy=read,api,rest-api`. The map works the same; every change then fails with a message saying the group needs the `write` policy (add it later with `/user group set omini policy=read,write,api,rest-api`).

If your RouterOS version does not offer the `rest-api` policy, leave it out (not verified on hardware).

Then, in Omini, **Integrations → Add → MikroTik RouterOS**:

| Field | Value |
|---|---|
| Address | `https://<router IP>` (e.g. `https://192.168.88.1`) |
| Username | `omini` |
| Password | the user's password (stored encrypted, never logged) |
| Verify the TLS certificate | off for the self-signed certificate above; on if the router has a trusted one |

**Test connection** says which parts the user cannot read, if any.

## How it works

A collection only reads: every request is a `GET` to `https://<router>/rest/<menu>` with HTTP Basic authentication, except one: the Ethernet **monitor** is a console command, which the REST API runs with a `POST`. The plugin sends `POST /rest/interface/ethernet/monitor` with `{"numbers": "<every ethernet port>", "once": ""}`: `once` reads the current state a single time and returns. It changes nothing. RouterOS returns every value as a string (`"true"`, `"1Gbps"`, `"1w2d3h"`); the plugin parses them and leaves anything it cannot parse empty.

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
| `/ip/dhcp-server/lease` | leases with `status=bound`; static leases (reservations) | [DHCP](https://help.mikrotik.com/docs/display/ROS/DHCP) |
| `/ip/dhcp-server`, `/ip/pool` | the DHCP server of each network and its range (routers) | [DHCP](https://help.mikrotik.com/docs/display/ROS/DHCP) |
| `/ip/firewall/filter` | the rules of each network (its access; routers) | [Filter](https://help.mikrotik.com/docs/display/ROS/Filter) |
| `/ip/dns` | `mdns-repeat-ifaces`: the networks in the mDNS repeater (routers) | [DNS](https://help.mikrotik.com/docs/display/ROS/DNS) |
| `/ip/neighbor` | MNDP / LLDP / CDP neighbors | [Neighbor discovery](https://help.mikrotik.com/docs/spaces/ROS/pages/24805517/Neighbor+discovery) |
| `/interface/wifi`, `/interface/wifi/registration-table` | Wi-Fi clients (`wifi` package, RouterOS 7.13+) | [WiFi](https://help.mikrotik.com/docs/spaces/ROS/pages/224559120/WiFi) |
| `/interface/wireless`, `/interface/wireless/registration-table` | Wi-Fi clients (legacy `wireless` package) | [Wireless Interface](https://help.mikrotik.com/docs/spaces/ROS/pages/8978446/Wireless+Interface) |

Only `/system/resource` is required. Every other menu is optional: one that is missing (package not installed), forbidden or failing is skipped and the rest of the collection goes on. Requests time out after 10 s.

Mapping notes:

- **Device key:** the factory MAC of `ether1` (stable even when the bridge MAC changes), else the lowest port MAC.
- **Port connector:** from the port's **factory name** (users rename ports): `ether*` → RJ45, `sfp*` / `sfp-sfpplus*` / `sfp28-*` → SFP, `qsfp*` → QSFP; a combo port is SFP or QSFP when a module is in.
- **Link speed:** the monitor's `rate` while the link is up; a port with auto-negotiation off falls back to its speed setting. A bridge runs at its fastest member's speed; a WAN on a VLAN or PPPoE follows its parent down to the physical port.
- **VLANs:** only for the ports of bridges with `vlan-filtering=yes`: untagged = the bridge port's PVID (none when its `frame-types` admit only tagged frames), tagged = the bridge VLAN entries that list the port. The bridge itself and other interfaces report no VLANs (Omini does not offer to change them).
- **Wi-Fi clients** are labeled "SSID · band" (e.g. `Home · 5 GHz`). With the `wifi` package, the AP's `tx-bits-per-second` is the client's download.
- **Updates:** RouterOS knows the latest version only after a check (by hand, or a script). When `latest-version` is absent, *update available* stays unknown. A RouterBOOT firmware newer than the running one (`upgrade-firmware` ≠ `current-firmware`) counts as one pending update.

## Known limitations

- **Not tested on hardware** (see above). Details that could not be verified without a device:
  - the exact REST output of `ethernet/monitor` (field names are the documented console ones; values are parsed with or without units);
  - whether the `read` policy is enough to run the monitor through REST on every version (if not, link speed and SFP diagnostics are skipped, the rest still works);
  - the property names of `/interface/wifi` in REST (`configuration.ssid`, `channel.band`) when the SSID is set through a configuration profile — the registration table's own `ssid`/`band` are used first;
  - the error status RouterOS returns for a menu the user's group cannot read (403 is treated as "no permission", 400/404 as "not available").
  - the status and message RouterOS returns when a group without `write` sends a `PATCH` (a 403, or a 400 whose detail mentions permissions, is reported as the missing `write` policy);
  - that `PATCH /rest/interface/ethernet/<.id>` with only `disabled` leaves every other property of the port untouched (it is the documented equivalent of `set`; tested against the emulator only).
  - the shapes of the writes beyond `PATCH`: `PUT /rest/<menu>` answering the new item with its `.id`, `POST /rest/ip/firewall/filter/add` taking `place-before` and answering `{"ret": "<.id>"}`, `DELETE` answering nothing, and `POST /rest/ip/dns/set` (documented REST mappings of `add`, `remove` and `set`; tested against the emulator only);
  - the name and format of `/ip/dns` `mdns-repeat-ifaces` (RouterOS 7.16 and later; a comma-separated interface list);
  - how a firewall rule reads in REST: a rule counts as one of Omini's presets only when it has no other matcher than its `in-interface` (and `protocol` and `dst-port=53` for DNS); properties RouterOS may add with an empty or default value are ignored, but one with a value makes the access read as *custom*.
- **Undo** is best effort: a firewall rule put back after a failed change goes back at the end of its chain, and properties RouterOS only reports are not restored.
- **Bridge VLAN entries holding several VLANs** are not split (see [What it changes](#what-it-changes)).
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
