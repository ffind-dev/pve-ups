# PVE-UPS

**GUI-based UPS shutdown appliance for Proxmox VE — a NUT alternative with a web wizard
and no config files.**

*Deutsche Fassung: [README.de.md](README.de.md)*

PVE-UPS monitors one or more UPS devices — **with an SNMP network card (standard RFC 1628
or a vendor MIB: APC PowerNet, CyberPower)** or **through a NUT server**, which is how USB and
serial UPS devices are read — and, on a power outage, shuts down one or more **Proxmox VE
hosts** (and, if you want, a **Proxmox Backup Server**) in an orderly fashion. The modern
replacement for vendor-locked appliances such as APC PowerChute Network Shutdown.
Everything is configured through a **web wizard**; monitoring is available as **REST/JSON**.

> [!IMPORTANT]
> PVE-UPS is built for an **internal (management) network**. It holds API tokens that can
> power off servers, so run it like any other management tool: reachable only from trusted
> networks, never exposed to the internet, behind the same firewalling and access control
> as the Proxmox web interfaces. Note that `/api/status` and `/api/health` are deliberately
> readable without a login so monitoring can poll them.

## Why not NUT?

[NUT](https://networkupstools.org/) has excellent hardware support, but for the common
"shut my Proxmox hosts down when the UPS runs low" case it means `upsd`/`upsmon` config
files and custom shutdown scripting on every host. PVE-UPS takes the appliance approach
instead — and where NUT hardware support is what you need, it uses NUT as a *driver*
rather than replacing it:

- **One LXC, one installer** — an unprivileged Debian container (~256 MB RAM) created by a
  single command on the PVE host.
- **No config files** — a web wizard with test buttons for every step; settings apply live.
- **No agents on the hosts** — shutdown goes through the Proxmox API using a dedicated,
  revocable **API token** with only the `Sys.PowerMgmt` privilege. No root SSH anywhere.
- **Vendor-neutral, but not naive about vendors** — the standard RFC 1628 UPS MIB via
  SNMP v1/v2c/v3 (pure-Python, no net-snmp), automatically switching to a vendor MIB where
  the standard falls short (APC PowerNet, CyberPower), or any existing NUT server as a
  read-only client.
- **NUT stays a driver, never the brain** — PVE-UPS only ever reads variables from `upsd`.
  No `upsmon`, no `upssched`, no shutdown scripts: the thresholds, the host policy and the
  decision stay in the appliance, where you can see them.

## Screenshots

*Dashboard during a power outage — one UPS on battery, shutdown countdown running, with
the live power-feed diagram underneath:*

![Dashboard during a power outage](Screenshots/dashboard.png)

<details>
<summary>More screenshots (UPS status &amp; settings, hosts, cluster preparation, webhooks)</summary>

*UPS status cards — power source, the MIB actually in use, charge, remaining runtime:*

![UPS status](Screenshots/ups-status.png)

*A UPS entry: read via SNMP (network card) or a NUT server, MIB selection, query timeout
and retries, optional per-UPS thresholds:*

![UPS settings](Screenshots/ups-settings.png)

*Host settings — Proxmox VE or Backup Server per entry, API token, which UPS devices feed
it, AND/OR logic, and the resulting feed diagram:*

![Host settings](Screenshots/host-settings.png)

*Live preview of the shutdown sequence: hosts sharing an order go at the same time, and
the appliance's own host is always last:*

![Shutdown sequence preview](Screenshots/shutdown-sequence-preview.png)

*Cluster options per host (Beta): shut the cluster down as a unit, disarm the HA manager,
and prepare a hyper-converged (Ceph) cluster:*

![Cluster options on a host](Screenshots/host-settings-cluster.png)

*Cluster preparation — the timeouts each step gets, the worst-case battery time they add
up to, and the guest this appliance itself runs in:*

![Cluster preparation settings](Screenshots/cluster-preparation.png)

*Webhooks: one per target, each with its own format (Slack, Discord, Teams, ntfy, status
JSON or a custom template), severity filter and test button:*

![Webhook settings](Screenshots/webhook-settings.png)

</details>

## Installation

Run in the **Proxmox node shell** (web UI → node → `>_ Shell`, as root). The script
downloads the latest release, unpacks it and creates the LXC:

```bash
bash -c "$(curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh)"
# with options, e.g. a static IP:
curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh | bash -s -- \
  --ctid 950 --ip 10.0.0.50/24 --gateway 10.0.0.1 --hostname pve-usv
```

All options are optional; `--help` lists them with their defaults
(`curl … | bash -s -- --help`):

| Option | Default | Meaning |
|---|---|---|
| `--ctid <id>` | `950` | ID of the new container |
| `--hostname <name>` | `pve-usv` | Hostname of the container |
| `--storage <name>` | automatic | Storage for the container disk (content `rootdir`). Without it: `local-lvm`, then `local-zfs`, then the first other non-Ceph storage |
| `--template-storage <name>` | `local` | Storage the Debian 12 template is downloaded to (content `vztmpl`). The default falls back to another suitable storage; a named one is refused if it cannot hold templates |
| `--bridge <bridge>` | `vmbr0` | Network bridge |
| `--ip <dhcp\|address/cidr>` | `dhcp` | `dhcp` or a static address such as `10.0.0.50/24` |
| `--gateway <ip>` | – | Default gateway, only used with a static `--ip` |
| `--memory <MB>` | `256` | RAM of the container |
| `--disk <GB>` | `4` | Size of the container disk |
| `--allow-ceph-storage` | off | Accept a Ceph-backed `--storage` (see below) |

On a **Ceph cluster** the installer refuses a Ceph-backed rootfs storage (and skips one
when picking automatically): this container has to keep running while the cluster it is
shutting down goes away, which on Ceph it cannot — once the pool loses `min_size` its own
disk stops answering. Pick a local storage, or override with `--allow-ceph-storage`.

PVE-UPS is also listed on [community-scripts.org](https://community-scripts.org/) (search
for "PVE-UPS") — a community-maintained collection of Proxmox helper scripts. The one-liner
above stays the reference path.

Then open the web UI at **`http://<container-ip>:8080`**:
1. Set the UI password.
2. Walk through the wizard (UPS devices → hosts → thresholds → optional webhook).
3. While **dry-run** is active nothing is shut down — ideal for testing.
4. When everything checks out: **disable dry-run** (mode "ARMED").

> The LXC typically runs on one of the protected hosts. Mark that host as **"This host"**
> in the host list — it is then guaranteed to shut down last. With the cluster-wide guest
> stop (or on a Ceph cluster), pick this container under *Triggers → This appliance*
> instead: the mark then follows that selection, and the guest stop knows which guest it
> must never stop.

## Docker (alternative deployment)

Prefer not to run an LXC? A prebuilt image is published on every release to
[`ghcr.io/ffind-dev/pve-ups`](https://github.com/ffind-dev/pve-ups/pkgs/container/pve-ups).
It is multi-arch — `linux/amd64`, `linux/arm64` and `linux/arm/v7` — so it also runs on a
Raspberry Pi 3/4/5 with either a 64-bit or a 32-bit OS; Docker picks the right variant:

```bash
curl -fsSLO https://raw.githubusercontent.com/ffind-dev/pve-ups/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
```

This mounts two named volumes (`/etc/pve-usv` for the config, `/var/lib/pve-usv` for the
event log, history and state) so data survives container recreation. Open
`http://<container-host>:8080` and run through the wizard as usual.

**Docker mode has two differences from the LXC deployment**, because there is no
privileged companion process (no systemd) inside the image:
- **No in-app updates.** The "Upload update package" button is hidden; update by
  pulling a new image tag and recreating the container
  (`docker compose pull && docker compose up -d`). Config and event log persist in the
  volumes.
- **No NTP/timezone management from the wizard.** Time and timezone are the
  Docker host's/orchestrator's responsibility — **set `TZ` on the container** (the example
  Compose file does). The self-test schedule runs in the container's local time, and
  without `TZ` the container runs in UTC.

> **If your network lies within `172.17.0.0/16` – `172.31.0.0/16`**, move Docker's default
> address pool *before* starting the container — Docker claims that range for its bridges,
> and the container would no longer reach a UPS or Proxmox host in it. In
> `/etc/docker/daemon.json`:
> `{"bip":"10.210.0.1/24","default-address-pools":[{"base":"10.211.0.0/16","size":24}]}`,
> then `systemctl restart docker`.

Everything else (SNMP polling, Proxmox shutdown, thresholds, webhook, self-test) works
identically to the LXC deployment. The LXC install (above) remains the primary, fully
self-updating path.

## Connecting the hosts (API token)

The appliance shuts hosts down through the Proxmox API — no root SSH, no agent on the
host. Each host entry has a **type**, because Proxmox VE and Proxmox Backup Server use
different token schemes and privilege names:

| | Proxmox VE | Proxmox Backup Server |
|---|---|---|
| API port | 8006 | 8007 |
| Privilege | `Sys.PowerMgmt` | `Sys.PowerManagement` |
| Granted on | `/nodes` | `/system/status` |
| "Node" field | a label; must match a node name | a free label (PBS ignores it) |
| API URL | one per node — it decides which machine is shut down | one per server |

Picking the wrong type is the most common cause of a failing test: a Backup Server entered
as "Proxmox VE" rejects the request outright and reports *"Authentication failed (token
invalid?)"*, however valid the token is.

**Give every host entry its own API URL.** The shutdown is sent to the node behind the URL
you entered, so that is what decides which machine goes down — and a node that is already
powered off cannot forward the shutdown for the ones still to come. The "Node" name is
checked against the API and labels the host everywhere in the UI, but it no longer steers
the shutdown. Two entries sharing one URL are the exception, and PVE-UPS warns about it.

### Proxmox VE

It needs a dedicated user with a **single privilege** (`Sys.PowerMgmt`) and an API
token. Run **once** in a node shell (as root):

```bash
# 1) dedicated user (PVE realm)
pveum user add ups@pve

# 2) role that carries only the power-management privilege
pveum role add UpsShutdown -privs "Sys.PowerMgmt"

# 3) grant the role on /nodes (or narrower: /nodes/<name>)
pveum acl modify /nodes -user ups@pve -role UpsShutdown

# 4) create the API token — privilege separation OFF, so the token inherits the privilege
pveum user token add ups@pve shutdown --privsep 0
```

The last command prints the **token ID** (`ups@pve!shutdown`) and the **secret** (a UUID,
shown only this once — copy it now). Enter both in the wizard under **Proxmox hosts**
(API URL is `https://<host-ip>:8006`) and check the connection with **Test**.

- **In a cluster, run the commands only once, on any node.** Users, API tokens and ACLs
  live at datacenter level (`/etc/pve`) and are therefore valid on every node — enter the
  *same* token ID and secret for each node you add. On standalone hosts that share no
  cluster, repeat the commands per host.
- Give every host entry **its own API URL** (`https://<this-node-ip>:8006`). The API would
  proxy a request to another node, but a node that has already been shut down cannot proxy
  for the ones still to come.
- Leave **Verify TLS** off as long as the host uses Proxmox's self-signed certificate.
- The token is revocable at any time: `pveum user token remove ups@pve shutdown`.

### Proxmox Backup Server

Run this in the shell of the Backup Server. The ACL is granted **twice** — once to the
user, once to the token:

```bash
proxmox-backup-manager user create ups@pbs
proxmox-backup-manager user generate-token ups@pbs shutdown
proxmox-backup-manager acl update /system/status Admin --auth-id 'ups@pbs'
proxmox-backup-manager acl update /system/status Admin --auth-id 'ups@pbs!shutdown'
```

Enter the API URL as `https://<pbs-ip>:8007` and check with **Test**. Three things differ
from Proxmox VE and explain the commands above:

- **Both ACL entries are required.** A token's permissions are computed from its own ACL
  entries and then intersected with those of its user, so a token can never exceed its
  user. Granting only one leaves the token without the privilege.
- **The role is `Admin`.** PBS has no fine-grained power-management role —
  `Sys.PowerManagement` is only carried by `Admin`. Granting it on `/system/status` rather
  than `/` keeps the scope as narrow as PBS allows.
- **The "Node" field is only a label.** PBS ignores the node in the API path, so the
  shutdown always addresses `/nodes/localhost/status`; nothing has to match the hostname.

> [!WARNING]
> **Consider carefully whether to attach a Backup Server here at all.** The token above is
> an `Admin` token, and PVE-UPS has to reach the PBS API from the very environment it
> protects. That widens the path from a compromised virtualisation host to the backups —
> which are exactly what should survive such a compromise. For production environments we
> **recommend against** attaching PBS to the same PVE-UPS instance that manages the PVE
> hosts. Alternatives: give the Backup Server its own shutdown path (a separate PVE-UPS
> instance, reachable only from a separate management segment), or let it ride out short
> outages on its own UPS runtime.

## Features

- **Multiple UPS devices** per instance with host↔UPS mapping and per-host logic
  (**AND** = redundant power supplies, **OR** = split load), including a live feed diagram.
- **Two target types**, mixable in one instance: **Proxmox VE** nodes and a **Proxmox
  Backup Server** — selected per host entry, each with its own token scheme and privilege
  check (but see the warning above before attaching a PBS in production).
- **Two UPS sources**, freely mixable in one instance:
  - **SNMP v1/v2c and v3** (authPriv), read-only. Reads the standard RFC 1628 UPS MIB or a
    **vendor MIB** — currently **APC PowerNet**, which is what makes APC cards work that
    implement RFC 1628 partially (NMC2 below firmware sumx/sy v5.1.7) or not at all (NMC1:
    AP9617/AP9618/AP9619), and **CyberPower** (CPS-MIB, RMCARD family), read one value per
    request for cards that do not answer a request for several values at once. Picked
    automatically per UPS; selectable by hand.
  - **NUT server** (TCP 3493) as a read-only client — for UPS devices without a network
    card. Works with the UPS server built into a Synology/QNAP/TrueNAS NAS, a Raspberry
    Pi, OPNsense, or a NUT install on a Proxmox host. QNAP and Synology prescribe their own
    values (QNAP: UPS name `qnapups`, user `admin`, password `123456`; Synology: UPS name
    and user `ups`, no password) and only answer hosts on their permitted-devices list —
    see the manual, section 5.
- **Web wizard** for UPS devices, hosts, thresholds and notifications — with test buttons;
  the UPS test breaks its result down per object, so a missing OID or NUT variable, wrong
  credentials and a blocked port are told apart at a glance. It also names the trigger
  conditions the device cannot feed at all, so no threshold is left silently dead.
- **Bilingual UI**: English (default) and German, picked automatically from the browser
  language; user manual built in (both languages).
- Per-UPS **threshold overrides** on top of the global defaults.
- **History tab**: remaining runtime, load and charge of every UPS over time (1 hour to
  90 days, or any start and end you pick), with outages, triggers and unreachable stretches as coloured bands, event-log
  markers, drag-to-zoom, an outage table and CSV export. Kept for a configurable number of
  days in its own small database and written in the background, so it never touches the
  shutdown path; can be switched off. On for new installations, off after an update until
  switched on.
- **Proxmox VE cluster preparation** (**Beta**, needs Proxmox VE **9.2+**): once per
  cluster, before its first node goes down, the HA manager is disarmed, so services are not
  recovered onto nodes that are shutting down themselves. Verified rather than assumed, under
  a hard timeout, and a **“Restore cluster”** button undoes it afterwards. Because that
  preparation is cluster-wide while the shutdown is per host, **“shut the whole cluster down
  as a unit”** (on by default) takes every node of the cluster down as soon as one of them is
  due — otherwise a single failing UPS leaves the cluster in halves. Optionally, **every
  guest in the cluster is asked to shut down at once** before the first node goes down
  (“Stop all guests cluster-wide first”, off by default): faster than each node working
  through its guests in reverse startup order when the battery is short, at the price of
  ignoring that order. Marked Beta while it
  gathers field experience; opt-in throughout, and reports are welcome via
  [issues](https://github.com/ffind-dev/pve-ups/issues).
- **Hyper-converged clusters (Ceph)** (**Beta**): with the Ceph option on, PVE-UPS follows
  the official Proxmox order — disarm HA, then **stop every guest in the cluster**, then set
  the maintenance flags (`noout,nobackfill,norecover,norebalance`). Stopping the guests first
  is what makes such a cluster survivable: node by node the pool falls below `min_size` while
  guests are still running, their IO blocks and the last node never powers off. **Off by
  default** and skipped entirely on clusters without Ceph. It needs three more privileges,
  the appliance's own guest is picked from a list so it is never stopped, and that guest must
  not live on Ceph storage — the installer refuses that. Leave “as a unit” on here: a half
  shut-down hyper-converged cluster loses its monitor quorum and has no working storage.
- **Webhook notifications** on notable events — as many targets as you like, each with its
  own format (full status JSON, **Microsoft Teams**, **Slack**, **Discord**, **ntfy**,
  plain text, or a **custom template** with placeholder substitution), severity filter,
  optional authentication header plus any extra headers the target needs (Nextcloud Talk
  and the like), and a test-send button. Sends run in parallel and under a
  common ceiling, so one unreachable target costs neither the others their notification
  nor the shutdown its battery. A target that stops working is reported like a broken
  shutdown credential: an event, a note on its card and a line on the dashboard — an
  alarm nobody receives is one nobody acts on.
- **REST status** (`/api/status`, `/api/health`) — read-only, no auth, no secrets;
  event log of the last 48 h included. `/api/health` also counts how many shutdown targets
  passed their last self-test, how many notification targets are delivering, and whether
  dry-run is still on — all three are monitoring information and never change the reported
  status or the HTTP code. Event/webhook texts are uniformly English.
- **Config export/import**, NTP/timezone setup, scheduled connectivity self-test per target
  (start time plus an interval from 15 min to 24 h; the result is kept per host and shown
  on the dashboard), in-place **updates via package upload** in the web UI.

## Safety model

- **Fail-safe by default:** losing contact with the UPS is *not* a confirmed power outage —
  it raises an alarm and never shuts anything down. The same holds for a NUT server that
  answers with stale data because its driver died: that counts as unreachable, never as
  "on mains". Two explicit opt-ins refine this:
  continuing a confirmed on-battery countdown through a connection loss (default on),
  and treating a prolonged pure communication loss as an outage (default off) — that
  second one fires on silence only, so a device that answers with something unusable is an
  alarm but never a communication loss.
- **Dry-run by default:** after installation the engine only logs what it would do.
  A **test shutdown** simulates the shutdown order without any effect. Because that is the
  quietest failure there is — fully configured, self-test green, and nothing ever shuts
  down — the dashboard, the event log and `/api/health` all say so while it is on.
- **The outage is timed on this appliance's own clock**, never on the counter the UPS
  reports: RFC 1628 leaves that value undefined while on mains, and cards that keep the
  last transfer's figure used to fire the shutdown in the first second of an outage.
- **A failed shutdown is retried** (three attempts) instead of costing the machine: a 503
  from a busy `pveproxy` or an expired deadline is not proof that the node is going down.
- A confirmed trigger and the on-battery countdown are **persisted to disk** and survive
  a service restart. Nothing read back from that file may act on its own, though — it
  waits for one poll of the new process to confirm the outage is still running.
- **Ready for the next outage on its own:** a shutdown that was really sent stays latched,
  so a machine on its way down is never commanded twice. The latch is released once every
  UPS has been reachable and on mains for five minutes (configurable, or off for the
  manual "Reset state" button only) — a grid that dips twice in a minute must not re-arm in
  between, and an unreachable UPS never counts as mains being back.
- **"Own host last":** the host carrying the appliance is always shut down last.
- **Targets cannot block each other:** hosts sharing a shutdown order are commanded at the
  same time, each call has a hard deadline, and the appliance's own host still forms the
  final stage — so one machine that stops responding cannot delay the others or the
  battery countdown.
- The app runs **unprivileged**; a slim privileged companion applies updates and
  NTP/timezone changes. Secrets never leave the appliance via the API.

## Default triggers

**One** matching condition is enough (all editable in the wizard; empty field = off):

| Condition | Default |
|---|---|
| On battery longer than | 600 s |
| Runtime below | 10 min |
| Charge below | 30 % |
| UPS reports `battery low/depleted` | on |

Poll interval: 30 s on mains, 8 s on battery.

## Updates

Download the release asset (`pve-usv-<version>.tar.gz`) from the
[releases page](https://github.com/ffind-dev/pve-ups/releases) and upload it in the web UI
under **Update**. The configuration is preserved; the service restarts automatically.
Updating from 2.x works the same way (see the manual for the two behaviour changes).
Running in Docker instead? See [Docker](#docker-alternative-deployment) above —
updates there work by pulling a new image tag.

> **Note:** the product name is PVE-UPS, but service and paths are technically named
> `pve-usv` (`systemctl status pve-usv`, `/etc/pve-usv/config.yaml`,
> `/var/lib/pve-usv/`). This is intentional and keeps existing installations compatible.

## Developing / testing without hardware

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # unit tests, no hardware needed

# simulate an SNMP UPS (separate terminal); snapshots in ./snmpdata/:
snmpsim-command-responder --data-dir=./snmpdata --agent-udpv4-endpoint=127.0.0.1:1161

# ... or simulate a NUT server:
python -m tests.nutsim --port 3493 --scenario battery   # mains | battery | low | sparse

PVE_USV_CONFIG=./dev-config.yaml PVE_USV_DB=./dev-events.db python -m app.main
# UI: http://127.0.0.1:8080
#   SNMP: host 127.0.0.1, port 1161, community "public"  -> mains (100 %)
#                                    community "battery" -> outage -> triggers fire
#         The APC snapshots carry PowerNet OIDs only, i.e. a card without RFC 1628:
#                                    community "apc"         -> mains, MIB resolves to APC
#                                    community "apc-battery"  -> outage on the APC MIB
#         Same for CyberPower (CPS-MIB only, read one object per request):
#                                    community "cyberpower"         -> mains, MIB resolves to CyberPower
#                                    community "cyberpower-battery" -> outage on the CyberPower MIB
#   NUT:  host 127.0.0.1, port 3493, UPS name "ups"
```

## Limits / assumptions

- Hosts are shut down **individually**, each through its own API. For a **Proxmox VE
  cluster** the shutdown can be prepared once per cluster beforehand (HA disarm, the
  cluster-wide guest shutdown and the Ceph maintenance flags, see the feature list); beyond
  that PVE-UPS does not resolve dependencies between nodes and does not manage quorum. Node
  order stays manual — a MON node that should go last is reported, never re-sorted — and the
  guests are stopped in parallel rather than in their `startup` order. The preparation starts
  as soon as the *first* node of a cluster is due, which is why “shut the whole cluster down
  as a unit” exists: it is what keeps the shutdown and the preparation talking about the same
  set of machines. That preparation is **Beta** in 4.0.0: the mechanism verifies every step
  and defaults to the safe choices, but it has seen few real clusters so far.
- Shutdown targets are **Proxmox VE and Proxmox Backup Server**. Proxmox Mail Gateway and
  Datacenter Manager are not implemented: each speaks its own token scheme, and shipping
  untested support would be worse than none.
- Reads the standard RFC 1628 UPS MIB, the APC PowerNet and CyberPower MIBs, or a NUT
  server's variables.
  Other vendor MIBs are not implemented yet — a device outside those needs either RFC 1628
  or a NUT driver. There is no direct USB/serial support in the appliance itself; a locally
  attached UPS is reached through a NUT server.
- The NUT protocol is unencrypted. Use it inside a trusted network, or point it at an
  `upsd` listening on the loopback interface of the same machine.
- In the optional Docker deployment, in-app updates and NTP/timezone management are not
  available (see [Docker](#docker-alternative-deployment)) — everything else is identical.

## License

MIT — Copyright © 2026 Florian Finder. See [LICENSE](LICENSE).

<sub>Developed with AI assistance.</sub>
