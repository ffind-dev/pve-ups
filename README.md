# PVE-UPS

**GUI-based UPS shutdown appliance for Proxmox VE — a NUT alternative with a web wizard
and no config files.**

*Deutsche Fassung: [README.de.md](README.de.md)*

PVE-UPS monitors one or more **UPS devices with an SNMP network card (standard RFC 1628)**
and, on a power outage, shuts down one or more **standalone Proxmox VE hosts** in an
orderly fashion — the modern replacement for vendor-locked appliances such as APC
PowerChute Network Shutdown. Everything is configured through a **web wizard**; monitoring
is available as **REST/JSON**.

## Why not NUT?

[NUT](https://networkupstools.org/) is powerful, but for the common "shut my Proxmox hosts
down when the UPS runs low" case it means driver matching, `upsd`/`upsmon` config files and
custom shutdown scripting on every host. PVE-UPS takes the appliance approach instead:

- **One LXC, one installer** — an unprivileged Debian container (~256 MB RAM) created by a
  single command on the PVE host.
- **No config files** — a web wizard with test buttons for every step; settings apply live.
- **No agents on the hosts** — shutdown goes through the Proxmox API using a dedicated,
  revocable **API token** with only the `Sys.PowerMgmt` privilege. No root SSH anywhere.
- **Vendor-neutral** — reads only the standard RFC 1628 UPS MIB via SNMP v1/v2c/v3
  (pure-Python, no net-snmp).

## Screenshots

*Dashboard during a power outage — one UPS on battery, shutdown countdown running:*

![Dashboard during a power outage](Screenshots/dashboard.png)

<details>
<summary>More screenshots (UPS status, feed diagram, UPS &amp; host settings)</summary>

*UPS status cards:*

![UPS status](Screenshots/ups-status.png)

*Live power-feed diagram (UPS → host):*

![Power feed diagram](Screenshots/power-feed.png)

*UPS settings with per-UPS threshold overrides:*

![UPS settings](Screenshots/ups-settings.png)

*Host settings (API token, feeds, AND/OR logic):*

![Host settings](Screenshots/host-settings.png)

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

Then open the web UI at **`http://<container-ip>:8080`**:
1. Set the UI password.
2. Walk through the wizard (UPS devices → hosts → thresholds → optional webhook).
3. While **dry-run** is active nothing is shut down — ideal for testing.
4. When everything checks out: **disable dry-run** (mode "ARMED").

> The LXC typically runs on one of the protected hosts. Mark that host as **"This host"**
> in the host list — it is then guaranteed to shut down last.

## Connecting a Proxmox host (API token)

The appliance shuts hosts down through the Proxmox API — no root SSH, no agent on the
host. Each host needs a dedicated user with a **single privilege** (`Sys.PowerMgmt`) and
an API token. Run once per host in the node shell (as root):

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

- Leave **Verify TLS** off as long as the host uses Proxmox's self-signed certificate.
- The token is revocable at any time: `pveum user token remove ups@pve shutdown`.

## Features

- **Multiple UPS devices** per instance with host↔UPS mapping and per-host logic
  (**AND** = redundant power supplies, **OR** = split load), including a live feed diagram.
- **SNMP v1/v2c and v3** (authPriv), RFC 1628 UPS MIB, read-only.
- **Web wizard** for UPS devices, hosts, thresholds and notifications — with test buttons.
- **Bilingual UI**: English (default) and German, picked automatically from the browser
  language; user manual built in (both languages).
- Per-UPS **threshold overrides** on top of the global defaults.
- **Webhook notifications** (HTTP POST with subject/body/status JSON) on notable events.
- **REST status** (`/api/status`, `/api/health`) — read-only, no auth, no secrets;
  event log of the last 48 h included. Event/webhook texts are uniformly English.
- **Config export/import**, NTP/timezone setup, daily Proxmox connectivity self-test,
  in-place **updates via package upload** in the web UI.

## Safety model

- **Fail-safe by default:** a lost SNMP connection is *not* a confirmed power outage —
  it raises an alarm and never shuts anything down. Two explicit opt-ins refine this:
  continuing a confirmed on-battery countdown through a connection loss (default on),
  and treating a prolonged pure communication loss as an outage (default off).
- **Dry-run by default:** after installation the engine only logs what it would do.
  A **test shutdown** simulates the shutdown order without any effect.
- A confirmed trigger and the on-battery countdown are **persisted to disk** and survive
  a service restart.
- **"Own host last":** the host carrying the appliance is always shut down last.
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

> **Note:** the product name is PVE-UPS, but service and paths are technically named
> `pve-usv` (`systemctl status pve-usv`, `/etc/pve-usv/config.yaml`,
> `/var/lib/pve-usv/`). This is intentional and keeps existing installations compatible.

## Developing / testing without hardware

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # unit tests, no hardware needed

# simulate a UPS (separate terminal); snapshots in ./snmpdata/:
snmpsim-command-responder --data-dir=./snmpdata --agent-udpv4-endpoint=127.0.0.1:1161
PVE_USV_CONFIG=./dev-config.yaml PVE_USV_DB=./dev-events.db python -m app.main
# UI: http://127.0.0.1:8080 — SNMP host 127.0.0.1, port 1161,
#   community "public"  -> mains (100 %)
#   community "battery" -> outage (battery, 22 %, 3 min) -> triggers fire
```

## Limits / assumptions

- Standalone hosts only (no cluster/HA-manager interaction) — a possible future extension.
- Reads exclusively the standard RFC 1628 UPS MIB (vendor-independent).

## License

MIT — Copyright © 2026 Florian Finder. See [LICENSE](LICENSE).
