# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

The single source of the version is `app/__init__.py` (`__version__`); `pyproject.toml`
reads it dynamically. On every release: bump `__version__` **and** add a section here.

## [Unreleased]

## [4.3.0] - 2026-10-10

A feature release from GitHub issue reports: a history of every UPS, the cluster-wide guest
stop for clusters without Ceph, CyberPower network cards, and an installer that explains
itself.

Three settings are added: `history.enabled`, `history.retention_days` and, per Proxmox VE
host, `cluster_guest_stop` (off). The history is **on for a new installation and off after
an update** — switch it on under Settings → System. No existing config needs editing.

### Highlights
- **History tab** ([#37]): remaining runtime, load and charge of every UPS over time, with
  outages, triggers and event-log markers, zoom, a free period, an outage table and CSV
  export. Off after an update until you switch it on.
- **"Stop all guests cluster-wide first" without Ceph** ([#43]): every guest is asked to
  shut down at once before the first node goes down, instead of node by node in startup
  order — for clusters where that takes longer than the battery lasts.
- **CyberPower network cards** ([#40]) are read through their own MIB, also cards without
  RFC 1628. "Automatic" over SNMP v2c no longer fails on cards that answer a missing object
  with an error.
- **The Docker image runs on a Raspberry Pi** (64-bit and 32-bit).

### Added
- **The Docker image runs on a Raspberry Pi.** It is now published for `linux/arm64`
  (Raspberry Pi 3/4/5 on a 64-bit OS) and `linux/arm/v7` (32-bit OS) alongside
  `linux/amd64`, so the UPS management can run on a small, low-powered device instead of
  one of the machines it shuts down. Dependencies without prebuilt ARM wheels are compiled
  in a separate build stage, so the runtime image carries no compiler.
- **History tab** ([#37]): remaining runtime, load and charge of every UPS over time,
  between Dashboard and Settings. Periods from 1 hour to 90 days plus "all", or any start
  and end picked in the From/To fields (which always show the period on screen); one UPS shows
  runtime (minutes) and load/charge (percent) as two panels on a shared time axis, "all"
  compares one measure across every UPS with a status strip each. The power state is drawn
  as background bands (on battery, shutdown triggered, unreachable, bypass), warnings and
  critical events from the event log as markers (informational ones on request), the
  runtime trigger as a dashed line. Drag to zoom, double-click to zoom out, an outage table
  (start, duration, lowest runtime and charge, highest load, triggered) whose rows zoom to
  the outage, and CSV export of the raw values. New endpoints `GET /api/history`,
  `GET /api/history.csv`, `GET /api/history/info` and `DELETE /api/history`, all behind the
  login.
- **Recording the history** (`history.enabled`, `history.retention_days`, Settings →
  System): one sample per UPS a minute on mains, one per poll on battery and one at every
  change of state, kept in its own `history.db` next to the event log for 1–365 days
  (default 90; about a few MB per UPS and quarter). Written from a background thread after
  every decision of the poll has been made, with a bounded queue: a slow, full or hanging
  disk costs the history, never the shutdown. **On for a new installation; an existing
  configuration gets it off on update** (and a backup from an earlier version on import),
  so an update never starts writing to a disk nobody sized for it. Switching it off stops
  the recording and hides the tab; what was recorded stays until it ages out or is
  cleared. Not part of the configuration backup, and "Clear log" leaves it alone.
- **"Stop all guests cluster-wide first" without Ceph** ([#43]). The parallel guest stop
  the Ceph procedure has always used is now a switch of its own (`cluster_guest_stop`, off
  by default): before the first node goes down, every VM and container in the cluster
  except this appliance is asked to shut down at once, instead of each node working
  through its guests in reverse startup order as it powers off — which with many order
  groups can take longer than the battery lasts. The startup order is deliberately
  ignored, guests that do not stop in time are forced off after the configured time, and
  the same preconditions apply as with Ceph: the appliance's own guest has to be picked,
  `VM.Audit` and `VM.PowerMgmt` are needed, and where HA manages guests HA has to be
  disarmed first. The Ceph option includes the guest stop as before — the host card shows
  it ticked and locked — and the guest stop alone never touches Ceph. The budget hint, the
  shutdown preview, the self-test and the host test all follow the new switch.
- **CyberPower MIB** ([#40]): SNMP UPS devices can be read through the CPS-MIB
  (enterprise 3808) of CyberPower network cards, which covers cards that implement no
  RFC 1628 at all. "Automatic" finds it like APC PowerNet, and it can be selected by hand.
  It is always read **one value per request**: some of these cards time out on a request
  for several values while answering every value on its own. On a local network that costs
  a few hundred milliseconds per poll, and a card that stops answering still costs only one
  timeout — the first one ends the poll.
- "Test UPS" recognises a card that times out on a request for several values but answers
  them one at a time, and says to select the CyberPower MIB explicitly ([#40]). "Automatic"
  always starts with such a request, so it cannot find these cards on its own — and
  retrying every timeout to find out would double what a UPS that is really gone costs.
- **`install.sh --template-storage <name>`** ([#44]) picks the storage the Debian
  template is downloaded to; until now it was fixed to `local` with a silent fallback. A
  storage named this way is checked and refused if it cannot hold templates, the same as
  `--storage`; only the default still falls back on its own.
- **`install.sh --help`** lists every option with its default, and both READMEs and
  manuals now carry the full option table instead of a single example ([#44]).

### Fixed
- **"Automatic" MIB over SNMP v2c/v3 no longer fails on agents that refuse a missing
  object** instead of marking it. "Automatic" appends each vendor MIB's anchor object to the
  standard request; a conforming agent reports an object it lacks as missing, but a real
  CyberPower card refuses the whole request with SNMPv1's `noSuchName` — so the UPS showed
  as unreachable on every poll although it answers everything it has. A refusal caused by
  an anchor now leads to asking each anchor on its own (one round trip each, never a second
  timeout) and reading the MIB that answered. A non-conforming v2c agent that refuses one
  of the standard's own objects now falls back to the vendor MIBs, as SNMPv1 already did.
- **The cluster diagnostics no longer flag privileges nothing needs** ([#39]). A token
  scoped exactly to a plain cluster — `Sys.Audit`, without `VM.Audit` — passed the host
  test, yet the per-query panel filed the unreadable guest list as an error and unfolded in
  orange. Each query now knows which option it serves; a read that fails for an option that
  is not ticked shows as "not readable (not needed)" and leaves the panel closed. The guest
  list a token may not read is also reported as a missing privilege rather than a generic
  error when it *is* needed.

### Changed
- The shutdown preview now applies the same rules to the guest stop as the preparation
  itself: it no longer promises a guest stop that the preparation would skip because HA
  manages guests and cannot be disarmed.
- A guest stop that was asked for but cannot run is reported even when nothing else is
  left to prepare — as a WARNING without Ceph, where the nodes still stop their own guests,
  and as a CRITICAL with Ceph, as before.
- `install.sh` names the option when one that needs a value is given without it, instead
  of failing with an unbound-variable error.

[#37]: https://github.com/ffind-dev/pve-ups/issues/37
[#39]: https://github.com/ffind-dev/pve-ups/issues/39
[#40]: https://github.com/ffind-dev/pve-ups/issues/40
[#43]: https://github.com/ffind-dev/pve-ups/issues/43
[#44]: https://github.com/ffind-dev/pve-ups/issues/44

## [4.2.0] - 2026-09-17

A small release from GitHub issue reports.

Two settings are added, both defaulting to the previous behaviour: `selftest_log_ok` and,
per webhook, `extra_headers`. Nothing else in the configuration changes, and no existing
config needs editing.

### Highlights
- **A webhook you have switched off no longer has to have a URL** ([#35]). Half-prepared
  notification targets are kept as drafts; only an *active* one still insists on somewhere
  to post.
- **Webhooks can carry extra headers** ([#34]), so targets that need more than one — like
  Nextcloud Talk, which wants `OCS-APIRequest` and `Accept` alongside its authorisation —
  work without a proxy in front.
- **Password managers can fill the login form again** ([#26]). The sign-in page is a real
  form with a username field, which is what a manager's inline autofill keys on; the token
  and password fields in Settings are marked so it leaves them alone.
- **Successful self-tests can be kept out of the event log** ([#28]) without testing the
  credentials any less often. Failures, recoveries and "Test now" always report.

### Added
- `extra_headers` per webhook: additional headers as `Name: Value`, one per line, for
  targets that need more than the single authorisation header ([#34]). Deliberately not on
  the masked-secret path — these are stored and shown in plain text, and the UI says so;
  the existing header field remains the place for credentials. They override the headers
  the chosen format sets, but never the authorisation header, so an entry here cannot
  shadow the secret next to it. Unparsable lines, header-splitting attempts and the
  headers the HTTP client owns (`Host`, `Content-Length`, `Transfer-Encoding`,
  `Connection`) are dropped rather than refused, like every other import in this project.
- `selftest_log_ok` (default on, i.e. unchanged): with it off, a passing self-test no
  longer writes its quiet line to the event log ([#28]). The requested weekly/monthly
  interval would have meant verifying the credentials less often, which is the one thing
  the check exists to do — this removes the noise instead. The schedule, the failure
  events, the system journal, the dashboard verdict and `/api/status` are all unaffected,
  and two green lines are never suppressed: an explicitly requested run, and a recovery
  from a failure that was reported.

### Fixed
- **A disabled webhook without a URL no longer blocks the whole save** ([#35]). Such a card
  used to be discarded on save, so the interface had to refuse the save to stop it
  vanishing in silence — which left inventing a fake URL for a target that was switched off
  as the only way forward. A card is now kept on its name alone, and the warning is raised
  only for a webhook that is active and has nowhere to post.
- **The sign-in page works with password managers** ([#26]). It was a lone password input
  with no form and no username field, so Bitwarden's inline dropdown stayed empty while its
  manual "fill" worked — the symptom reported. It is now a real form with a fixed,
  off-screen username field and the proper `autocomplete` hints. The five secret fields in
  Settings (API token, both SNMPv3 passphrases, the NUT password, the webhook header value)
  are marked so a manager does not paste the UI password over an API token — a substitution
  that would only surface during an outage.

### Changed
- The dry-run notice in the event log no longer shares its throttle with the self-test's
  "ok" lines.

[#26]: https://github.com/ffind-dev/pve-ups/issues/26
[#28]: https://github.com/ffind-dev/pve-ups/issues/28
[#34]: https://github.com/ffind-dev/pve-ups/issues/34
[#35]: https://github.com/ffind-dev/pve-ups/issues/35

## [4.1.0] - 2026-08-31

A reliability release. Everything here came out of one review of the shutdown path, and
the most important entry is the first: it is a bug from the field ([#25]), and it made the
appliance shut a cluster down at the start of an outage instead of at the end of the
configured delay.

Nothing in the configuration changes. `/api/status` gains fields (`ups[].elapsed_source`,
which is `"own"` whenever an elapsed time is reported, `"own-unconfirmed"` while that
measurement came back from the state file and no poll has confirmed it yet, and `null`
when none could be measured; `ups[].incomplete`, `hosts[].stale_ups_ids`,
`hosts[].incomplete`, `webhooks`), and `ups[].seconds_on_battery` is now `null` while a
UPS is on mains rather than carrying whatever the device last reported.
`webhooks[].last_delivery_error` deliberately carries no URL — see below. `/api/health`
gains `webhooks_total`/`webhooks_ok` and `dry_run`, alongside `hosts_ok` and with the same
caveat: it is monitoring information and never moves `status` or the HTTP code.

Out-of-range settings are now pulled back at both ends rather than only at the bottom, so
a config carrying an absurdly large timeout runs with the default and says so.

### Highlights
- **A UPS reporting a stale "time on battery" no longer shuts everything down at once.**
  The outage is measured on the appliance's own clock instead of on the counter the device
  reports, and the running timer survives a restart of the service.
- **A UPS that answers without saying where its power comes from is an alarm, not an
  all-clear.** For SNMP and NUT alike such a reading no longer clears a running countdown,
  and it never counts as silence either.
- **A shutdown that fails is retried** — three attempts instead of one, so a busy API no
  longer leaves a machine running while the dashboard reports it as handled.
- **A cluster only takes along the nodes that asked for it**, and the three cluster
  switches are read once per cluster instead of off whichever node triggered first.
- Also: failed webhook deliveries are reported, an active dry-run says so on the
  dashboard, entries that cannot do their job are flagged, and no credential can reach the
  public status endpoint.

### Security
- **A failed notification no longer publishes the webhook's URL.** The delivery state
  introduced in this release stored the raw exception text, and httpx spells a status
  error as `Client error '401 Unauthorized' for url '<the whole thing>'`. That string was
  then served from `/api/status`, which is deliberately public and secret-free — and a
  webhook URL *is* the credential for Slack, Discord, Teams and ntfy alike. The second
  path was worse: the same snapshot travels on as the `status` field of every
  notification, so one target's URL would have been POSTed to another. What is reported
  now is the status code, or the transport error's kind, and any URL in an unforeseen
  message is masked. This applies to the event log too, which `/api/status` also carries.
  The unabridged text still goes to the system journal, which no API serves.
- **A UPS driver's password no longer travels into the public status endpoint.** The
  evidence line this release records when a trigger fires writes the last poll's readings
  into the event log, and `/api/status` serves 48 h of that log without authentication.
  For SNMP those readings are OIDs and harmless; a NUT server hands back its entire
  `LIST VAR` answer, and `upsd` publishes each driver's configuration as
  `driver.parameter.<name>` — which for a number of drivers means a plaintext password or
  an SNMPv3 passphrase. Values under a key that names a password, secret, token, community
  or key are masked now, at the moment the reading is stored as well as when the line is
  built, so any future source inherits the rule. Masked rather than dropped: that a driver
  carries a credential is itself diagnostic, and everything harmless stays readable — and
  "password" is matched in a way that spares `input.bypass.*`, which are mains readings a
  post-mortem needs and which a bare "pass" would have swallowed.

### Fixed
- **A UPS that answers without saying where its power comes from is no longer read as
  "on mains".** `on_battery` is one comparison — `power_source == "battery"` — so every
  other outcome fell to the mains branch, including the three that mean *we do not know*:
  the output-source object absent or unconvertible, an APC card reporting
  `upsBasicOutputStatus = unknown(1)`, and RFC 1628's own `upsOutputSource = other(1)`,
  which is the MIB's "none of the below" and was being read as a definite statement about
  the mains. The SNMP poller called such a reading reachable as
  soon as any other object of the profile answered, so a poll arriving mid-outage cleared
  the running on-battery timer, dropped an already fired trigger and wrote "mains power
  restored" about a device that had reported nothing of the sort. Every other object
  degrades into a threshold that simply cannot fire; this one decides the state machine,
  and it was the last fail-dangerous default left in the engine. The NUT client has always
  refused the same reading (no `ups.status`, no verdict), and the two sources now answer
  the question the same way: unreachable, which is an alarm, never a shutdown, and which
  keeps the blind countdown running on an outage that was already confirmed.
- **...and a device that answers is never mistaken for a silent one.** "Unreachable" is
  the right verdict for a reading that cannot be used, but it is not the same statement as
  "this device has gone quiet" — and the opt-in `comm_loss_shutdown_after_min` shuts the
  whole estate down on the second. So the three readings above, and the older "answers,
  but implements none of this MIB", would each have armed a *communication-loss* shutdown
  minutes later, during normal operation, on a card that had never stopped talking. Whether
  a source answered at all is now recorded separately from whether the answer was usable,
  and only real silence can arm that trigger. The alarm, the fail-safe refusal and the
  blind on-battery countdown are unchanged; the alarm says which of the two it is, rather
  than sending the operator to a network that is working.
- **A NUT status that names no power source is no longer read as "on mains" either.** The
  two entries above fixed the SNMP poller, and the NUT client was said to have refused the
  same reading all along. It refused half of it: a *missing* `ups.status` was correctly no
  verdict, but a status that is present and names neither `OL` nor `OB` — an empty value,
  a bare `CHRG`, `ALARM` or `RB`, a driver's intermediate state during a transfer — left
  the power source at "unknown" while still counting as a healthy reading. `on_battery` is
  one comparison against "battery", so that fell into the mains branch and did mid-outage
  exactly what the SNMP version did: cleared the running timer, dropped an already fired
  trigger and wrote "mains power restored" about a device that had said nothing of the
  sort. It is unreachable now, in the same words and with the same consequences — an
  alarm, never a shutdown, and the blind countdown keeps running.
- **A NUT server that answers `ERR` is not a NUT server that has gone quiet.** upsd
  replying `DATA-STALE`, `DRIVER-NOT-CONNECTED`, `ACCESS-DENIED` or `UNKNOWN-UPS` reaches
  the poller as an exception, and everything that arrived that way was filed as silence.
  So the same opt-in as above shut the whole estate down minutes later — during normal
  operation, over a wedged driver, a wrong password or a mistyped UPS name, on a server
  that had never stopped answering — and the alarm said "no response for N polls", which
  sent the operator to a network that was working. The manual has described a stale NUT
  driver as "an alarm, never a shutdown" since the feature shipped; the code now agrees. A
  protocol error is an answer, a timeout or a refused connection is not.
- **An entry that cannot be polled at all no longer *demands* the shutdown it is supposed
  to refuse.** A UPS stored without an address is never polled — the source returns "not
  configured" without touching the network — so it is unreachable and silent by
  construction, permanently. This release calls that state fail safe in so many words ("a
  standing refusal to shut down every host it feeds"), and with the communication-loss
  opt-in switched on it did the exact opposite: a configuration mistake, a backup import
  or a hand-edited `config.yaml` shut down every host that entry feeds, minutes later,
  during normal operation, without a single packet having been sent. A trigger needs
  evidence about the power, and an entry nobody ever asked has produced none. The alarm
  and the countdown on the card follow the same rule, so neither promises a shutdown that
  is not coming.
- **A cluster no longer takes along nodes that never asked to be part of one.** Membership
  was decided by the API's node list alone, so a host whose "cluster member" box was
  deliberately left unticked — its own UPS perfectly healthy — was powered off by another
  node's outage under "shut the whole cluster down as a unit", which is on by default. The
  warning built to catch exactly that ("its nodes are fed by different UPS devices") *did*
  filter on the switch and returns early below two members, so the one configuration in
  which the action over-reached was the one in which nothing said so. One membership rule
  now, shared by the preparation, the unit additions, the preview and the health checks.
  Its other half is said out loud rather than left to be discovered: such a node is still
  *reached* by the preparation — HA is disarmed for the whole cluster, and with Ceph every
  guest in it is stopped, its own included — while the shutdown stops at the ticked
  members, so it stands there without HA, without guests and, once the monitors follow,
  without storage. Nothing named it, because every neighbouring check either counts over
  the ticked members or sees a well configured, enabled target and calls that node
  covered. The self-test names it.
- **The three cluster switches are read once per cluster, not off whichever node
  triggered first.** They are edited per host card but every one of them acts on the whole
  cluster: Ceph maintenance flags, the HA manager's arm state, and whether the cluster goes
  down as a unit. The preparation took them from the candidate that happened to come first
  while the shutdown preview and the scheduled health check read them across all members —
  so with the switches set unevenly, the self-test promised Ceph flags and an HA disarm
  that the preparation then skipped, and which of the two was right depended on which UPS
  failed. They are resolved once per cluster now ("at least one member asked for it":
  skipping a step that was wanted costs the storage, running one nobody wanted costs a
  maintenance flag), and a cluster whose nodes disagree gets a warning naming both sides,
  at self-test time rather than during an outage.
- **A slow but healthy target is no longer reported as a failed self-test.** `targets`
  guarantees that a credential check never outlasts its timeout plus the grace, and the
  clients underneath broke that from the inside: three sequential requests for Proxmox VE,
  two for a Backup Server, each handed the *full* timeout. A host answering every request
  just inside it therefore hit the outer deadline and came back as "gave up" — a CRITICAL
  "Self-test FAILED" for credentials that are fine. Each request now takes a slice of the
  budget, the way the node listing already did. The same arithmetic bit the shutdown
  itself: both address forms shared one client, so a first form that hung rather than
  answering left the second five seconds, and the fallback that whole routine exists for
  could not run.
- **The appliance no longer shuts a healthy estate down on the poll after it boots.**
  Two features that are each right on their own. The on-battery timer is written to disk
  so a restart cannot lose a running countdown; the countdown keeps running when contact
  is lost mid-outage, because that is the only trigger left once the UPS goes silent. But
  this appliance shuts its *own* host down last, so every outage ends with it being
  restarted — and it comes back to a state file saying "on battery since three hours ago".
  If the UPS then missed the very first poll after that boot, which is exactly when a
  switch is still converging and an SNMP card still coming up, hours of restored elapsed
  time were measured against a ten-minute threshold and the machines that had just come
  back up were told to shut down again, during normal operation, before the device had
  said a single word. A latched trigger read from the same file did it whatever the
  thresholds were, because the branch that keeps a trigger alive while blind deliberately
  never re-derives one that is already set. Nothing restored may fire now until one poll
  of the new process has answered. That answer settles it either way and costs nothing:
  on mains everything restored is dropped, on battery the trigger is derived from the
  full restored elapsed time on that same poll — so a service restart mid-outage, where
  the UPS is reachable by definition, is unaffected. The dashboard says when a timer is
  being held rather than counting down to a shutdown that will not happen, and
  `ups[].elapsed_source` reports `"own-unconfirmed"` for it: the measurement is real, the
  conclusion is not, and those are different statements.
- **A held timer does not lock the appliance out of its own recovery.** Five places stand
  down while an outage is running — both start-up checks, the scheduled self-test, the
  manual one and "Restore cluster" — and all five read "on battery since" without asking
  whether any poll had confirmed it. A UPS that never answers again therefore meant "an
  outage is running" for good, and that is precisely the state a real cluster shutdown
  leaves behind: the appliance powers its own host off last, comes back, and the UPS
  management card is still down. The cluster inspection then never ran, so the dashboard
  showed no cluster and hid the "Restore cluster" button — the one action the operator
  needed — while the start-up check's warnings about incomplete entries, stale UPS
  assignments, duplicated API URLs, corrected settings and the battery reserve were never
  written either. An unconfirmed timer no longer counts as an outage for any of them,
  which risks nothing: it cannot fire a shutdown either, so there is no countdown for a
  self-test to delay. A confirmed one still stands everything down, as before.
- **An entry that cannot do its job is reported wherever it came from.** This release
  refuses to *save* a host without an API URL or a token, and a UPS without an address —
  in the browser. The two paths where it actually happens do not pass a form: a backup
  import and a hand-edited `config.yaml`. Such a host is stored, renders complete on the
  dashboard, and fails during the outage; such a UPS is never polled at all, which counts
  as permanently unreachable and therefore — fail safe — as a standing refusal to shut
  down every host it feeds. Nothing said so in time either: the node check answers
  "could not verify" for an entry it cannot even address, which is correctly read as *no
  verdict* rather than as a fault, so the first real signal was the next scheduled
  self-test, up to a day later. It is a critical event on the poll after the
  configuration arrives now, and a chip on the dashboard for as long as it is true.
- **A hanging webhook can no longer delay the shutdown.** Notifications are documented as
  best-effort and were guarded against every failure except taking too long: each target
  had an httpx timeout, but a read timeout bounds one read, not a server that answers a
  byte at a time — and nothing bounded the round. Since the engine awaits it from inside
  the poll loop, that time came out of the battery at the worst possible moments: before
  the hosts were evaluated at all, before the cluster preparation started, and between
  two shutdown stages, where the appliance's own host was waiting behind it. Every send
  now carries a ceiling, and a target that runs into it is marked on its card like any
  other broken one.
- **The nodes a cluster takes down with it are retried like every other host.** They have
  no trigger of their own — their cluster does — and the branch that recognises them
  skipped them before the retry was ever considered, while the preparation could not
  offer them a second time either. So the machines pulled in by "shut the whole cluster
  down as a unit" had exactly one attempt each, and the event they produced on failure
  still promised that the next poll would try again.
- **A node its cluster took along is not shut down once mains are back.** Such a node has
  no trigger of its own — its cluster is the reason it is due — so the branch that
  recognises it deliberately ignores "no reason now". It also stopped asking whether the
  episode was still running at all, and that is the half that mattered: a taken-along node
  whose first attempt had failed was re-sent on every following poll, mains back or not.
  Where every attempt of that round had failed, nothing was ever shut down and the machine
  going off was a healthy one, during normal operation. The retry now continues only while
  a node of the same cluster really is going down or is still due; otherwise the node is
  released like any other, with the same critical event saying it never went down.
- **The last chance to retry is no longer missed.** Attempts land one poll apart, and no
  poll follows the appliance's own host: it powers itself off. A node that failed earlier
  in the same sweep was therefore due for its second attempt eight seconds after there
  was nobody left to make it — so the retry could not reach the one case it was built
  for, a busy `pveproxy` answering 503. Hosts that failed earlier in a sweep are now
  retried immediately before that final stage, and the battery-reserve warning counts it.
- **A cluster nobody can reach is reported once, not every eight seconds.** With "abort
  on failure" enabled such a cluster's nodes are held back, which means they never fire
  and stay due — and they came back through the preparation on every poll, each round
  paying for a full inspection and sending another critical notification while the
  battery drained. This is the same knot untied in 4.0.0 for the preparation itself; this
  branch had been left out of it. The outcome is now latched for the episode, and a
  re-arm inspects afresh.
- **...and neither is a node that turns out not to be in a cluster at all.** The sibling
  of the entry above, and the third of the three answers an inspection can give: an
  address that is reachable and simply reports a standalone node. That is settled — it
  cannot change inside one outage — but nothing recorded it, so every poll on which such a
  host was still due (a shutdown being retried) paid for another inspection off the
  battery to be told the same thing again. It is latched for the episode now, and said
  once, because otherwise nothing in the product ever mentions that the "this host belongs
  to a cluster" tick is doing nothing on that card. It never holds the node back: "not a
  cluster" is not a failed preparation.
- **The silent failure is reported on a Backup Server too.** The warnings about stale UPS
  assignments, duplicated API URLs and corrected settings are pure configuration
  questions, but they ran at the end of the node-name check — behind an early return that
  an estate with no Proxmox VE target always takes. On a PBS-only installation the one
  failure this appliance calls completely silent stayed exactly that until the next
  scheduled self-test, up to a day after the save that caused it.
- **A host that never went down is no longer filed as merely aborted.** When mains
  returned, a machine still running only because every attempt to shut it down had failed
  was released with the same routine line as a cleanly withdrawn shutdown — "no longer
  needed", at warning level. It now says what actually happened, as a critical event.
- **A shutdown triggered while the UPS is unreachable records its evidence.** The
  readings behind a trigger are written to the event log as of this release, but only on
  the path where the device answered. The blind countdown and the communication-loss
  opt-in fire real shutdowns in precisely the situation where a post-mortem has least to
  go on, and they were writing nothing.
- **An absurdly large setting is corrected like an absurdly small one.** Only lower bounds
  were enforced. Every timeout here is awaited inside the poll loop, so a slipped digit —
  `6000` where `60` was meant — parked the decision engine for a hundred minutes per
  shutdown stage while the battery drained. The ceilings sit well above any real estate,
  so they only ever catch a typo. The per-UPS overrides carry the same bounds, and they
  were the half that had none: an override is edited on one card among several and its
  value is shown nowhere else, which is precisely where a slipped digit survives — a
  threshold ten times too large silently switched a trigger off for that device alone.
- **Webhooks get their ids filled in on every path.** Hosts and UPS devices had this;
  webhooks were assigned ids only when the settings page saved them. A hand-written
  `config.yaml` or an imported backup could therefore carry several without one, and
  since the id keys both the delivery state and the masked-secret reconcile, they shared
  a single record: each overwrote the other's result, and one was offered the other's
  auth header.
- **A crash leftover can no longer smuggle its permissions back into the config file.**
  The write to the temporary file asks for mode 0600, but that only applies when the file
  is created — and reusing a leftover is deliberately allowed, because refusing would let
  one crashed save block every future one. A `.tmp` from an older version was therefore
  rewritten, plaintext secrets and all, with the permissions it already had.
- Ticking the cluster option and saving no longer leaves the dashboard claiming there is
  no cluster until the next scheduled self-test — by default a day later — with the
  "Restore cluster" button hidden along with it. Saving a host entry that has a node name
  but no API URL or no token ID is refused with the card marked, instead of being stored
  in a state where the shutdown can only fail during an outage. The same now applies to a
  UPS card missing what makes it pollable at all — an address, or the UPS name a NUT
  server needs: stored without it, the device was never polled, counted as permanently
  unreachable, and every host it feeds was refused a shutdown for safety. The message
  names the card it means, which it did not for UPS entries. The webhook card's delivery
  warning now follows the live status while the settings page stays open, rather than
  freezing at whatever was true when the page was opened.
- **A UPS reporting a stale "time on battery" no longer triggers an immediate shutdown**
  ([#25]). The engine took the device's own counter as the authority for how long an
  outage had been running. RFC 1628 defines `upsSecondsOnBattery` only *while* on battery
  and says nothing about the value on mains, so a card that keeps the last transfer's
  figure instead of clearing it breaks no rule — and plenty do. The first poll of a fresh
  outage then read a number already past "on battery longer than" and fired at once,
  before any time had actually passed. The same value was reported as
  `seconds_on_battery` whatever the power source, which is why the status could show days
  on battery for a UPS sitting on mains. The appliance measures the outage on its own clock
  now and does not read that counter at all; the timer survives a restart through the state
  file, and where it cannot (the file is missing or older than a day) a cold start into a
  running outage counts from zero rather than believing a number it never observed. This
  also fixes the opposite failure, which was just as silent: a card reporting a permanent
  `0` pinned the elapsed time at zero and disabled the time trigger completely, including
  the blind countdown that is the only trigger left once contact is lost mid-outage.
- **A failed shutdown is retried instead of costing the machine.** Any negative result
  latched the host for the rest of the episode — a 503 from a busy pveproxy, a TCP reset,
  or the deadline expiring while the node was merely slow. That node then stayed up until
  the battery ran out while the dashboard reported the episode as handled. It is retried
  on the following polls now, three attempts in total, and the event says which attempt
  failed and when it gave up. Re-sending to a node that is already going down is harmless;
  not sending at all is not.
- **An outage that ends between two attempts closes the episode.** The gap the retry above
  opened, and the one place it was not accounted for. A failure that still has attempts
  left deliberately does not latch its host, and the branch that releases a withdrawn
  shutdown keyed on precisely that latch — so when mains came back after one failed
  attempt rather than after all three, "failed" stayed in the host's record for good.
  Nothing cleared it: the appliance reported SHUTTING_DOWN with the outage long over, the
  critical event saying the machine is still running was never written, and the automatic
  re-arm could not tidy up because, as far as it could see, nothing was latched. Only
  "Reset state" or a restart of the service got out of it.
- **A deleted host no longer keeps the episode open.** The per-host shutdown bookkeeping
  was never reconciled against the configuration, while the two places that decide whether
  an episode is over read all of it: one scans every record, the other asks whether any
  host is still latched. Removing a host that had been shut down therefore left the
  appliance permanently in SHUTTING_DOWN — which stands down the self-test, both start-up
  checks and "Restore cluster". The same reconcile now also drops a deleted webhook's
  delivery record, which a re-created id would otherwise have inherited and shown as its
  own failure.
- **A host assigned to a UPS that no longer exists is reported.** Stale ids were dropped
  silently, so a host whose assignments had all gone stale ended up with no feed, was
  never eligible, and was simply never shut down — with no event, no alarm and no failed
  test to show for it. Losing only some of them was quieter still: with the "all feeds"
  policy the redundancy requirement was then satisfied by whatever was left. Both are a
  critical event when the configuration is saved, and a chip on the dashboard.
- **One unresponsive UPS no longer freezes the whole engine.** Every source was polled in
  a single unbounded gather, and everything else — the countdowns, host eligibility, the
  staged shutdown — runs sequentially behind it. A NUT server answering one line just
  inside its timeout could hold that poll for the better part of an hour. Each poll now
  has a deadline derived from that source's own settings, so a slow but healthy device is
  unaffected while a hanging one becomes what any failed read becomes: an alarm, never a
  shutdown.
- **Out-of-range settings are corrected and reported instead of obeyed.** Nothing bounded
  the numbers and the browser's `min` attributes were never enforced, so a negative poll
  interval reached `asyncio.wait_for()` and produced a tight loop hammering the UPS.
  Values outside a sane range now fall back to their default, with a warning naming each
  one. Deliberately corrected rather than rejected: the configuration is read at start-up
  without a guard, so refusing one stored number would mean the service never comes up.
- **Asking the guests to stop is bounded, like killing them.** The force-stop round was
  given a deadline in this release; the round that politely asks first was left with only
  the HTTP client's own timeout. It runs before the grace period is measured, so a control
  plane that accepts connections and then goes quiet spent a full timeout per batch of
  eight guests off the front of the budget — on a large cluster enough to leave no grace at
  all, which force-stopped every guest at once instead of ever asking one. That is the same
  fault the deadline rework removed one step earlier, at the HA disarm.
- **A self-test or node check that raises no longer takes the whole round with it.** Both
  ran their hosts through one unguarded `gather`, so one host that managed to raise left
  every other host with a stale verdict and no failure event — and the self-test's
  exception travelled up into the poll loop's catch-all, costing that iteration its
  housekeeping too. The shutdown stages have been guarded this way since 4.1.0's retry
  work; these two now are as well.
- **The guest shutdown cannot spend the step behind it.** Two holes in the same routine,
  both on the shutdown path. The force-stop round was a sequential loop bounded only by
  the HTTP client's own timeout, so a control plane that had stopped answering cost ten
  seconds per straggler; enough of them outlasted the whole preparation, which then came
  back as a bare "gave up" and took the Ceph flags with it — the very step the reserve
  introduced in this release exists to protect. It runs concurrently and against the
  deadline now. The second is worse and quieter: an unreadable guest list was reported as
  an empty one, which is exactly what a cluster that has finished stopping looks like, so
  the preparation concluded success from a read that never happened. Unreadable and empty
  are told apart now, in the one module whose whole doctrine is that a write is confirmed
  by a read or not at all.
- **The cluster preparation no longer manufactures its own failures.** Three budgets were
  measured from the wrong moment. The guest shutdown's deadline started before the HA
  disarm rather than after it, so a disarm that legitimately took tens of seconds spent
  the guests' time — and a disarm longer than the guest budget left no grace at all, which
  force-stopped every guest instead of ever asking one to shut down. The Ceph flags, which
  run last, measured their budget against a deadline the guest stop had long exhausted, so
  they reported a failed preparation for work that had almost certainly succeeded — and
  with "abort on failure" that alone held back an entire cluster. A verification read was
  also unbounded relative to its own budget.
- **A cluster that cannot be inspected is no longer waved through in silence.** A failed
  inspection continued without an event and without recording anything, so "abort if the
  preparation fails" did not apply to it: those nodes were powered off with HA still armed
  and no maintenance flags, and the log said nothing. It is a critical event now and, with
  the option on, holds those nodes back.
- **Failed notifications are visible in the product.** A webhook that stopped working — an
  expired token, a deleted connector — reached a line in the system journal and nowhere
  else: no event, no status field, nothing in the interface. It is an event the first time
  it fails now, the webhook card says so, and the dashboard carries a line of its own —
  the settings page is not where anyone is looking while an outage runs, and an alarm
  nobody receives is one nobody acts on. The shutdown credentials have had a self-test and
  a chip for releases; the notification credentials had neither.
- **"Test" on a saved webhook no longer fails with the credentials it was given.** That
  endpoint was the only one that did not resolve the masked auth header, so it sent the
  literal placeholder and reported an authentication error for a working configuration.
- **A new host no longer inherits a deleted one's API token.** Ids for new cards were
  handed out as the lowest free number, so deleting an entry and adding another gave the
  new card the old id — and with it, the stored token of a different machine.
- **Two entries can no longer share one identity.** Duplicate ids passed through
  untouched; because the id is the runtime key, two hosts then shared a single shutdown
  latch and two UPS devices overwrote each other's reading in the same poll. Duplicates
  are split on load now, and the first entry always keeps its id.
- **Accepting a suggested node name works again.** The host test's "use this node name?"
  offer raised a `ReferenceError`, which replaced the *successful* test result with an
  error message and left the diagnostics hidden.
- **Incomplete cards are no longer discarded without a word.** A host missing its node
  name, a UPS missing both host and name, or a webhook missing its URL were dropped on
  save while the interface reported success — the entry, API token included, was gone
  after the next reload. Saving now stops and marks the card. It does the same for a
  *new* host card left without a token secret: an empty field means "unchanged" for an
  entry that already exists, but for one the appliance has never seen it means the target
  is stored with no credential at all — complete-looking on the dashboard, answering 401
  during the outage, since the node check reads that as "could not verify" and the
  self-test may be a day away.
  The stricter half applies to *enabled* entries only: a card that is switched off shuts
  nothing down, so none of its fields can fail during an outage, and demanding them would
  leave an installation upgrading from 4.0.0 — which stored such entries quite happily —
  unable to save anything at all until it had completed a host it had deliberately
  disabled. The refusal names the card it means, rather than leaving eight of them to be
  compared.
- **The battery-reserve warning reaches every installation and quotes a realistic
  number.** It sat inside the checks that only run for a Ceph cluster, so standalone
  nodes, Backup Servers and clusters without Ceph — most installations — never saw the one
  warning that says the trigger fires later than the shutdown lasts, although the stages
  and the host timeout apply to them just the same. The figure was wrong as well: it
  counted one host timeout and one cluster preparation although both run in sequence, so
  an estate with several shutdown orders was told a number several times too small. It now
  also counts the two terms that were missing entirely and are paid out of the same
  battery — the notification round, which the engine deliberately waits for between the
  shutdown stages, and the cluster inspection on a cold start — and the arithmetic it
  prints adds up to the total it prints next to it, which it did not. Two smaller terms
  were still short: a shutdown stage is bounded at the configured timeout *plus* the grace
  the appliance keeps for everything the HTTP client does not bound, and every stage emits
  its own notification, so the notification term grows with the stages instead of being a
  constant. The inspection term was short for the same reason as the stage: it is bounded
  at its budget plus that grace. All three err on the high side now, which is the only
  direction this figure may err in. The check also reads the trigger *per UPS*: the runtime threshold is overridable per
  device, so an estate whose global reserve is generous while one UPS is set to fire two
  minutes before the end was never warned at all — and the warning names the device whose
  setting it is quoting. The cluster term follows the switches rather than the tick as
  well. The cluster-wide guest stop is the largest single number in this sum and it runs
  only where Ceph was asked for, so charging it to every cluster added five minutes to a
  step that cannot happen — on its own enough to push a correctly sized three-node
  installation past the default ten-minute trigger and tell it to raise a threshold that
  was right.
- **A cold start inspects a cluster once, not once per node.** Only the self-test records
  which cluster a node belongs to, and it stands down for the whole duration of an outage.
  So an outage arriving before the first self-test — a restart, or a configuration saved
  moments earlier — made every due node read the cluster for itself: three nodes, three
  reads of up to seventeen seconds, all of it spent before the first machine was even
  asked to shut down. One reading answers for the whole cluster because it brings back the
  node list and every member is recorded from it — except when the cluster's API is the
  thing that is down, which names nobody. Those reads still have to happen (another node's
  API may be the one that works) but they go together now, so the phase costs one
  reading's time however many nodes are left.
- **The configuration file is no longer briefly world-readable.** Permissions were applied
  after writing, leaving every plaintext secret at the default mode for the length of the
  write. It is also flushed to disk before the rename now, which matters on an appliance
  built for power loss.
- Settings could not be reopened once a session had expired: the click threw and left the
  dashboard updating as though nothing were wrong. `GET /api/events` accepted an unbounded
  `limit`, which SQLite reads as "no limit". A failure to queue an NTP or timezone job
  answered an already-successful save with a server error.

### Added
- Every shutdown trigger writes the readings that caused it into the event log — the raw
  values of the poll that armed it, and whether an elapsed time could be measured at all.
  An unexpected shutdown could not be reconstructed afterwards: the reason line said what
  the engine concluded, never what it read, and the test button can only answer about a
  later and different state. The battery values lead the line and its length is capped: a
  NUT server hands back its whole variable list, which is several kilobytes, and this body
  reaches the event feed and `/api/status`.
- The trigger reason names what the device actually reported. `batteryLow`,
  `batteryInFaultCondition` and `noBatteryPresent` all normalise to "low" and all fire the
  same immediate shutdown, and the log could not tell an empty battery from a defective
  one.
- The host card says that ticking no feed boxes means "every configured UPS" — which is
  what the backend does with it, and the opposite of what an empty set of boxes looks
  like.
- **Dry-run says so where it will be seen.** It is on by default and it is the quietest
  failure there is: an appliance left in it is fully configured, passes its self-test,
  answers `/api/health` with `"ok"` and shuts nothing down. A warning pill in the stat
  grid was the only thing saying otherwise. There is now a dashboard banner — last in the
  chain, so a real outage always wins the space, and only once the wizard has been through
  once, because before that dry-run is the correct state rather than a half-finished
  commissioning — a line in the event log next to each self-test, and a `dry_run` field in
  `/api/health`, which is the endpoint an external monitor actually polls. Like `hosts_ok` and `webhooks_ok` it is monitoring information
  and never moves `status` or the HTTP code.

[#25]: https://github.com/ffind-dev/pve-ups/issues/25

## [4.0.0] - 2026-08-29

The major number marks the scope of this release — cluster support as a new core topic —
and one REST-visible break: `/api/config` now returns `notifications.webhooks` (a list)
where it returned `notifications.webhook`, which a script reading that field has to follow.
Everything else is compatible: updates from 3.x install unchanged, an existing
`config.yaml` loads as it is (the single webhook is migrated into a one-element list on
load), and `/api/status` and `/api/health` only gained fields.

The **cluster preparation ships as Beta**. Being a new core topic and being new are not in
conflict: the mechanism is deliberately conservative — every step is verified rather than
assumed, the whole sequence runs under one hard timeout, and the defaults are the safe ones
— but it has run against few real clusters so far, and the variety out there is the part no
amount of care substitutes for. It is opt-in and does nothing until switched on.

### Highlights
- **Proxmox VE clusters are prepared before the shutdown (Beta).** Marking a node as a
  cluster member disarms the HA manager once per cluster — and, with the Ceph option,
  stops every guest and sets the maintenance flags — before the first node goes down; a
  "Restore cluster" button on the dashboard undoes it. Needs Proxmox VE 9.2 or newer.
- **A cluster is shut down as a unit** (on by default): one node becoming due takes the
  whole cluster with it, in the configured order, instead of leaving half a cluster
  standing with HA disarmed.
- **Several webhooks instead of one**, each with its own format, filter and test button —
  plus new formats for Slack, Discord and ntfy and a custom template.
- **The self-test is more useful**: it runs automatically after the appliance re-arms,
  "Run self-test now" starts it on the spot, and renaming a host no longer discards its
  API token.

### Fixed
- **The appliance re-arms itself once mains are back.** A shutdown that was really sent
  latched its host for good — deliberately, so a machine that is powering down is never
  told twice — but nothing ever released that latch again. The dashboard stayed on
  "Shutting down" with every UPS long since back on mains, and, worse, a *second* outage
  shut down nothing at all: every host still counted as already fired. The scheduled
  self-test, the start-up checks and the "Restore cluster" button all stand down while a
  shutdown is in progress, so they stayed down too. Only the "Reset state" button or a
  restart of the service brought the appliance back. It now releases the latches by
  itself once every UPS has been reachable and on mains for `rearm_after_mains_min`
  minutes (new, default 5, empty = manual only as before). The delay is the point: a grid
  that dips twice in a minute must not re-arm in between, and an unreachable UPS never
  counts as "mains are back". Restoring a prepared cluster stays manual — the re-arm only
  makes that button reachable again, and says so in the event when a cluster is still
  carrying the preparation.
- **"Reset state" no longer discards the self-test results.** The shutdown state and the
  credential/node-name verdicts share one structure per host, and the button cleared all
  of it, so `/api/health` reported "never tested" until the next scheduled slot — up to a
  day later. Only the shutdown keys are dropped now.
- **A cluster without Ceph is no longer asked to set Ceph flags.** The Ceph step was passed
  through unfiltered while the HA disarm was checked against feature detection, so on a
  cluster running ZFS replication or NFS/iSCSI every outage ran the full failure path — a
  rejected bulk PUT, four rejected single PUTs, a verification loop polling into the void —
  and reported a CRITICAL "preparation FAILED" for a component that is not installed. With
  "abort on failure" enabled it held every node of that cluster back. Both steps are now
  gated on what the cluster can actually do, and *absent* is told apart from *unreadable*:
  a denied read still attempts the write rather than silently skipping it. When neither
  step applies, the outcome is a quiet log line naming why, not a failure.
- **The shutdown preparation no longer discards its own feature detection.** When no
  self-test had run yet — the normal state after a restart — the cluster was inspected and
  that reading was then thrown away in favour of an empty cache, defaulting to "disarm-ha
  is supported". On Proxmox VE 8.x that meant POSTing an endpoint that does not exist,
  mid-outage, and a cluster held back over it under "abort on failure". The fresh
  inspection is now kept and used.
- **Leftovers from an outage are visible again as soon as the appliance is back.** Cluster
  state was only ever collected by the scheduled self-test, whose latch survives a restart
  — so after the appliance shut itself down last and came back, the next run could be a day
  away. In that window the dashboard showed no cluster and the "Restore cluster" button
  stayed hidden, although the cluster was still disarmed with the maintenance flags set.
  Since restoring is deliberately manual, that button is the only prompt there is. The
  clusters are now inspected once at start-up, independently of the schedule, and anything
  left over is reported.
- **"Restore cluster" is refused while a shutdown is running.** The button appears the
  moment the preparation lands, which is mid-shutdown; arming HA and clearing the flags
  there would undo the preparation at the one moment it is doing its job. The API now
  refuses it — the same guard the manual self-test already had — and the button is hidden.
- **A privilege is only demanded where it can do something.** `Sys.Modify` is no longer
  requested for a cluster without Ceph, nor `Sys.Console` on a release without `disarm-ha`,
  so a warning about a missing privilege always means one that is really needed. Each name
  now carries its purpose — `Sys.Console (HA disarm)` — in the self-test, the host test and
  the diagnostics panel.
- **"Not every node is a configured target" no longer fires at complete setups.** The
  warning counted only hosts carrying the cluster tick, although the tick governs the
  preparation (which runs once per cluster regardless) and not whether a node gets shut
  down. Ticking one member — a perfectly reasonable setup — produced a warning claiming the
  other nodes would be left running, which was false. It now counts every enabled PVE
  target belonging to the cluster.
- **A preparation that runs out of budget reports what it managed.** The timeout cancels
  the sequence mid-write, and "gave up" alone could not be told apart from "never touched
  it" — while half prepared is precisely the state worth knowing about. The event now names
  the completed steps. Same for a timed-out restore.
- **A cluster held back by "abort on failure" is no longer prepared again on every
  poll.** Holding a cluster back leaves no host latched, and that read as "the outage is
  over" — so the preparation latch was cleared and the whole failing sequence ran again on
  the next poll: every eight seconds, each round writing to the cluster and sending a pair
  of critical notifications, while the battery drained. The outcome of the preparation is
  now latched for the episode alongside the fact that it ran, which also fixes the other
  half of the same knot: whether the nodes may go down was derived from the work of a
  single iteration, so the abort stopped applying the moment the preparation was no longer
  being attempted — and the cluster was shut down after all, one poll later.
- **A UPS entry without an id in a hand-written `config.yaml` is no longer polled in
  vain.** The engine keys its per-UPS state on that id, so the answers were read and then
  dropped and the device could never trigger. Ids are now filled in on load, the way host
  ids already were.
- **A single dropped SNMP poll no longer sends a notification.** The "network connection
  lost" event was emitted on the very first failed poll at warning severity, so it passed
  the webhook's default filter — while `unreachable_alarm_after_polls` only ever governed
  the separate "unreachable" alarm further down. A short dropout on a busy network
  therefore produced an immediate webhook. Connection loss is now notified through that
  alarm alone, i.e. only after the configured number of consecutive failed polls, and
  "connection restored" is only notified if the loss was. Both transitions are still
  written to the event log, so diagnostics are unchanged.
- **Uploading a release package failed in Safari on macOS.** Two independent causes, both
  fixed: the file picker's `accept` list only named the compound extension `.tar.gz`, which
  Safari maps to a UTI and greys the file out, and Safari's "open safe files after
  downloading" unpacks the asset to a plain `.tar`, which the frontend, the API and the
  privileged agent all rejected. All three layers now detect the archive format by its
  *content* instead of its file name, so `.tar.gz`, `.tgz`, a Safari-unpacked `.tar` and
  `.zip` are equally accepted.
- **Renaming a host no longer discards its stored API token.** Hosts were identified by
  their type and node name — an *edited* field — so correcting a node name read as "a
  different host": the masked placeholder the UI sends for an unchanged secret found
  nothing to resolve to and silently became empty. The same mismatch made the host test
  answer "Authentication failed (token invalid?)" for a perfectly valid token, which is
  exactly what happened when accepting the node name that test now suggests. Hosts carry a
  stable id, like UPS sources and webhooks already did; a payload without one still matches
  by type and name, so nothing is lost on the way to this version.
- **Two host entries with the same name no longer share one shutdown latch.** They also
  shared their self-test result and shutdown state, so a duplicated entry whose IP was
  never adjusted counted as already fired and was left running. Identity now comes from the
  id, which is unique by construction.
- **A wrong Proxmox VE node name no longer goes unnoticed until the outage.** The node name
  is used verbatim in the shutdown call `POST /nodes/<name>/status`, but nothing ever
  checked it: the connection test only asks `/version` and `/access/permissions`, and
  neither looks at the node segment. A datacenter-wide token therefore let a misspelled
  name — a capital letter, a domain suffix, a label like "Proxmox 1" — pass every check in
  the wizard and in the scheduled self-test, and fail only when the battery was already
  draining. The node name is now verified against the API's own node list, in the host test
  and in the self-test, and once more at start-up so an appliance that was just updated
  reports the problem in seconds rather than at the next scheduled slot (which, with the
  persisted schedule latch, could be a day away). On a cluster the plain node
  index is not enough — every member is "a node this API knows", so any other member's
  name would pass — hence the listing is read from `/cluster/status` where the token may
  (it marks the node that actually answered) and from `/nodes` otherwise. A name that
  belongs to a different member is now reported as such and the right one is offered for a
  click. Without `Sys.Audit` the verdict stops at "this name exists here", which is
  honest rather than wrong.
- **The browser no longer serves the previous UI after an update.** None of the static
  files carried a `Cache-Control` header, so browsers were free to cache them
  heuristically — roughly a tenth of the file's age — and a weeks-old `app.js` could be
  reused for days without a single request reaching the appliance, leaving a new backend
  paired with an old interface. `index.html` is now sent with `no-store`, and every script
  and stylesheet it references gets a `?v=` stamp derived from the file's own
  mtime and size: changed files land under a new URL, unchanged ones stay cached, and
  nothing is hard-coded or generated at build time. Unstamped requests (the manuals, a
  hand-typed asset URL) must revalidate and answer with a 304 when nothing changed.

### Added
- **Several webhooks instead of one.** `notifications.webhook` became
  `notifications.webhooks`, a list with one card per target in the wizard, each with its own
  format, severity filter and test button. An existing single webhook is migrated into a
  one-element list on load, so nothing needs to be re-entered. The sends run concurrently
  and are reported individually: one unreachable target no longer costs the others their
  notification.
- **Slack, Discord and ntfy as payload formats.** Slack posts an attachment with a
  severity colour bar, Discord an embed, ntfy a plain-text push with `Title`, `Priority`
  and `Tags` set from the event.
- **A `custom` format for everything else.** Supply the body and content type yourself and
  use `{{subject}}`, `{{body}}`, `{{severity}}`, `{{severity_upper}}`, `{{facts}}`,
  `{{facts_json}}`, `{{status_json}}`, `{{timestamp}}` and `{{version}}`. Substitution is
  literal — deliberately not an expression language — and values are JSON-escaped when the
  content type is JSON, so a quote in an event text cannot produce a malformed payload.
- **An optional authentication header per webhook**, e.g. `Authorization` for a protected
  ntfy topic or an API-key header. The value is treated as a secret: masked in the API and
  carried over unchanged when settings are saved.
- **Uploaded update packages are validated before they are queued.** The API now checks
  that the upload is a readable archive containing `pyproject.toml` and `app/__init__.py`,
  and rejects it with a clear message otherwise. Previously the package was queued
  unchecked and only the privileged root agent noticed the problem, so a broken upload
  reached the privileged path; it no longer does.
- Release packages can be dropped onto the update card, as a way around browser file
  pickers that filter the archive out.
- **Cluster preparation before a shutdown — Beta (issues #1 and #6).** Once per cluster,
  before its first node goes down, PVE-UPS disarms the HA manager, so services are not
  recovered onto nodes that are shutting down themselves. **Needs Proxmox VE 9.2 or newer**
  (`disarm-ha`); that is now said plainly on the host card, in the host test, in the
  self-test and at the top of the manuals' cluster chapter, because on 8.x the preparation
  is reduced to the Ceph flags. Marked Beta in the UI, both manuals and the READMEs while it
  gathers field experience: the mechanism is conservative, but few real clusters have run
  it. Opt-in throughout, and it changes nothing until switched on.
  The step is *verified* rather than assumed: the disarm is polled until `armed-state`
  actually reaches `disarmed`. It runs under a hard timeout, because it happens while the
  battery drains — and that timeout (`cluster_prep_timeout_s`, default 60 s) is also the
  time the disarm is really given: it is handed down step by step, so raising it does what
  it says. Below roughly half a minute a disarm usually cannot be confirmed, because the HA
  stack answers in rounds of ten seconds and every node has to release its watchdog first.
  If it does not get there, the event names the state it stopped at — "still disarming" is a
  different message from a stack that never moved. A stack that is already disarmed (a
  second outage before the cluster was restored) is left alone instead of being disarmed
  again.
  The preparation **announces itself before it starts**, naming what it will do, how many of
  the cluster's nodes have triggered and how long the nodes may wait for it: it is the one
  step that deliberately delays the shutdown, and without that line the event log jumped
  from the outage straight to a result a minute later. In dry-run mode it is logged and
  nothing is changed.
  `resource-mode` is fixed to `ignore` and is not configurable — under `freeze` the guests
  stay HA-managed, `pve-guests` skips them and the disarmed LRM no longer stops them, so
  they would be killed by the power-off.
  Availability is detected per feature rather than by version number: `disarm-ha` (9.2+) is
  read from the endpoint index — which also covers backports — and is never POSTed blindly.
  If the preparation fails, the shutdown continues by default: an armed HA manager still
  stops the guests itself, which is degraded but safe, whereas aborting would mean losing
  power uncontrolled. `cluster_abort_on_prep_failure` opts into aborting instead.
  The cluster switches sit together in a **set-apart group** on the host card — the master
  switch stays plainly visible (an earlier collapsible section made the feature hard to
  find), while its sub-options appear underneath once it is ticked, listed in the order the
  steps actually run and split where the manuals split. Membership is shown on the
  **collapsed** card too, and next to the host name on the dashboard, the way the appliance
  is marked with a star: the discovered cluster name once it is known, the plain word until
  then. `/api/status` gained `cluster` and `cluster_name` per host for it.
  **Privileges follow the ticks:** `Sys.Audit` to read the cluster and `Sys.Console` for the
  HA disarm, both on `/`. An option left off costs nothing, and both manuals give one ready
  `pveum role add` line per combination instead of a single command granting everything, so
  nobody hands out `Sys.Console` (effectively shell access to the nodes) for something they
  did not switch on. `Sys.PowerMgmt` stays on `/nodes` where the base setup puts it.
- **A cluster is shut down as a unit** (`cluster_shutdown_all`, on by default). The
  preparation is cluster-wide — it disarms HA for the whole cluster, and with the Ceph
  option it stops every guest in it — while the shutdown is per host: a node is due when
  *its* UPS devices say so. When only some UPS devices trigger, those two disagree and the
  cluster is left in halves. Observed on a four-node cluster: one UPS failed, two nodes went
  down, and the two survivors sat there with every guest stopped, HA disarmed and two of
  three Ceph monitors gone — no guests, no HA and no storage quorum. With the switch on,
  every node of the cluster goes down as soon as one of them is due, in the usual `order`
  with the appliance's host last, and the event log names why each node was taken along.
  Turn it off for a plain cluster where HA should move a single failing node's guests onto
  the others; with Ceph it should stay on. When it is off and only part of a cluster shuts
  down, that is now reported as CRITICAL instead of being discovered afterwards.
  The self-test additionally warns when the nodes of one cluster hang on UPS devices that
  can trigger independently, with different wording depending on the switch — and points at
  `comm_loss_shutdown_after_min` for the common case where the UPS management switch hangs
  on the failing UPS itself, so the device goes unreachable instead of reporting battery and
  the fail-safe (correctly) refuses to trigger on it.
- **Hyper-converged clusters (Ceph) — Beta.** With the Ceph option on, the preparation
  follows the official Proxmox order: **disarm HA, stop every guest in the cluster, then set
  the maintenance flags** (`noout,nobackfill,norecover,norebalance`).
  **Why the guests have to stop first.** Letting each node stop its own guests as it powers
  off — what a per-node shutdown does — hangs a hyper-converged cluster: with
  `size=3/min_size=2` the pool falls below `min_size` once the second node's OSDs are gone,
  the guests still running on the survivor block on IO, their shutdown never finishes and
  that node never powers off. Proxmox documents the same sequence ("Shutdown Proxmox VE +
  Ceph HCI cluster"): stop all Ceph clients, then the flags, then the nodes. Every running
  guest is asked to shut down at once (at most 8 requests in flight — asking forty guests
  serially would spend a connect timeout on each), the result is verified by re-reading
  `/cluster/resources`, and a guest that ignores the request is force-stopped after
  `cluster_guest_force_after_s` (default 120 s, empty = never force, which then fails the
  preparation and names the survivors).
  The guest stop has **no switch of its own**: it belongs to the Ceph option, because with
  Ceph it is not optional but the first step of the procedure, and a tick whose absence
  hangs the cluster during a power cut would be a trap. Without Ceph nothing of it runs.
  **`cluster_guest_shutdown_timeout_s` (default 300 s) is its own budget**, deliberately
  not a share of `cluster_prep_timeout_s`: that one is measured in HA rounds of ten seconds
  and barely varies, while this one scales with the number of guests. Sharing one number
  would have the guests eat the disarm's budget on every existing config. The settings page
  shows the resulting worst-case hold-up (disarm + guests + node shutdown), and the
  self-test warns when the runtime trigger fires later than that — it warns only, and never
  adjusts a trigger by itself.
  **This appliance's own guest** is picked from a list under Settings → *This appliance*
  (`appliance.self_vmid`/`self_node`, or "not a guest of this cluster"), never typed: a
  mistyped id would stop the appliance in the middle of an outage. The pick also derives
  the **"this host"** mark on the matching host card, so the node carrying the appliance is
  shut down last without maintaining that fact twice. Until something is picked the guest
  stop is refused outright, loudly, and the shutdown continues without it. The appliance's
  guest must **not live on Ceph storage** — it is the one guest that has to outlive the
  cluster it shuts down — which `install.sh` now enforces and the self-test, the host test
  and the dashboard report at runtime.
  The flags are *verified* rather than assumed: the bulk `PUT /cluster/ceph/flags` is
  asynchronous and only returns a worker id, so they are read back, falling back to the
  synchronous per-flag endpoint — which is also the normal path on releases without the bulk
  variant. The Ceph part is **off by default** and enabled separately from the HA disarm:
  plenty of clusters run on ZFS replication or shared NFS/iSCSI, and this is the one part
  that writes into a storage layer. On a cluster without Ceph it is skipped rather than
  attempted, and its privileges are not required.
  **MON nodes should go last.** The monmap is read from the Ceph status that was fetched
  anyway, and a shutdown order contradicting it is reported in the host test and the
  self-test — never re-sorted automatically, because `order` is explicit configuration and
  silently overriding it would make the sequence shown on the dashboard a lie.
  New privileges follow the Ceph tick: `Sys.Modify`, `VM.Audit` and `VM.PowerMgmt` (the VM
  ones accepted on `/` or `/vms`). `Datastore.Audit` is *advisory* and reported separately —
  it only buys the Ceph-storage check, so its absence is never listed as a missing
  privilege. Note that `/cluster/resources` **filters by privilege instead of refusing**:
  without `VM.Audit` it answers 200 with an empty list, so "empty" is never read as "no
  guests to stop". `POST /api/cluster/guests` serves the guest picker without needing a
  credential test first.
  Both manuals now separate the plain cluster (`#cluster`) from the hyper-converged case
  (`#cluster-ceph`), each with its own privilege table: the difference in scope and in
  required rights was too large for one section.
- **The installer refuses Ceph-backed storage for the container.** `install.sh` now reads
  the storage *type* rather than only its content flags: `rbd`/`cephfs` storages are skipped
  when one is picked automatically and refused when named with `--storage`. The appliance is
  the one guest that has to outlive the cluster it shuts down, and on Ceph it cannot — once
  the pool loses `min_size` its own disk stops answering. `--allow-ceph-storage` overrides
  it deliberately. Refused rather than prompted, because the documented install path is
  `curl … | bash`, where stdin is the script itself.
- **A self-test runs automatically after the appliance re-arms.** Right after a re-arm is
  when a leftover problem shows up — a token that expired during the outage, a node that
  never came back, a cluster still prepared — and waiting hours for the next scheduled slot
  to find that out was the wrong trade. It is queued rather than run on the spot, so it
  never delays the eligibility check of that iteration, and it waits out a new outage
  instead of being lost to it.
- **"Run self-test now" on the dashboard.** Saving settings deliberately does not fire a
  credential check, and the next scheduled slot may be hours away — so a changed token or a
  freshly enabled cluster option could not be verified on the spot. The button runs the
  host and cluster checks immediately and always writes the outcome to the event log,
  bypassing the once-a-day throttle on the quiet "ok" lines. It is refused while a UPS is
  on battery, for the same reason the scheduled run is skipped then.
- **A healthy cluster now says so.** The cluster checks previously only spoke up about
  problems, which left "checked and fine" indistinguishable from "never checked" — a
  working self-test looked like a broken one. A cluster without findings is logged quietly
  as `Cluster <name>: ok` with node count, quorum, Ceph and HA state, at the same daily
  cadence as the per-host lines.
- **"Restore cluster" on the dashboard.** Arms the HA manager again and clears the
  maintenance flags, each verified, and only offered while something is actually left to
  undo. Deliberately manual: there is no automatic re-arm, because bringing HA back while
  nodes are still booting is a judgement call.
- **Cluster awareness for Proxmox VE (read-only part).** A PVE host is marked as a cluster
  member with a flag in the cluster group on the host card; members are grouped automatically by the cluster name read from
  `/cluster/status`, so no second token and no extra configuration section is needed. The
  scheduled self-test now also checks each cluster once and warns — never worse than a
  warning — about Ceph maintenance flags still set, an HA manager still disarmed, missing
  token privileges (named individually, not as a bare 403), a missing quorum, cluster
  nodes that are not configured as targets, and a `shutdown_policy` that would fight the
  shutdown (`migrate` makes the LRM delay it; the default `conditional` recovers services
  onto nodes that are shutting down). `/api/status` and `/api/health` gained a `clusters`
  block — monitoring information only, deliberately not part of the health status or its
  HTTP code, for the same reason as `hosts_ok`.
  The **host "Test" button checks the cluster privileges too** once the option is ticked,
  instead of only `Sys.PowerMgmt`: it names every missing privilege, reports the cluster it
  found (name, node count, Ceph and the HA arm state), and points out a node that is not a
  cluster member or a `disarm-ha` endpoint that does not exist. Missing cluster privileges
  are reported as a warning rather than a failure, since the connection itself works — the
  same convention as an unconfirmed `Sys.PowerMgmt`. Without `Sys.Audit` the check says the
  membership cannot be read instead of claiming the node is standalone. Below the result, a
  **details panel** lists every cluster query with its outcome — `ok`, `not permitted`,
  `not available` or the error — mirroring the UPS test's per-object diagnostics and
  unfolding by itself when something needs attention.
  The arm state of the HA stack and the number of HA-managed guests are tracked
  **separately**: a disarmed stack is reported, and offered for restoring, even on a
  cluster where no guest is HA-managed. (Tying them together had made a disarmed HA
  manager invisible on exactly such a cluster.) The `shutdown_policy` warning stays tied to
  the guests, because with none of them there is nothing that could be recovered onto a
  node that is shutting down.
- **Output load on the UPS status card.** Read from `upsOutputPercentLoad` (RFC 1628),
  `upsAdvOutputLoad` (APC PowerNet) and `ups.load` (NUT). Informational only: it feeds no
  trigger, and a device that does not report it simply shows nothing instead of warning
  about an unavailable shutdown condition.
- **Connection tolerance is configurable in the interface.** The per-UPS query timeout and
  (for SNMP) the retry count already existed in the config file but were not reachable from
  the wizard; they now sit on the UPS card. "Report unreachable after (polls)" joins the
  threshold card with a live hint spelling out what it means in seconds at the current poll
  interval — it is what decides when a connection loss is reported.
- **A reload prompt when the running version changes.** An update restarts the service
  while open tabs keep running the interface they were loaded with. `/api/status` already
  reports the version, so a tab that sees a different one than it started with now shows a
  note with a "Reload" button instead of letting the mismatch play out silently.
- **The shutdown addresses the node behind the host entry's API URL directly.** With one
  API URL per node — what the manual has always required, because a node that is already
  powered off cannot forward the shutdown for the ones still to come — the machine behind
  that URL *is* the intended one, so the call goes to `/nodes/localhost/status` and the
  configured name can no longer misdirect it. This also removes a cluster dependency from
  the shutdown path: Proxmox handles `localhost` locally instead of resolving the node
  through the cluster, which matters precisely when nodes are dropping and quorum is
  shaky. Proxmox Backup Server has always worked this way.
- **A failed shutdown is retried once with the other form of the path** — by name if the
  direct call failed, directly if the name failed. This covers a misspelled name as well as
  a token whose `Sys.PowerMgmt` sits on `/nodes/<name>` rather than on `/nodes`, without
  having to know in advance which applies. It can only run where the shutdown had already
  failed, and it reports what happened as a critical event: the machine is down, but the
  host entry is still wrong and needs fixing.
- **A warning when several host entries share one API URL** — shown while editing and
  repeated by the self-test, since a restored or hand-edited config never passes the form.
  That configuration is the one case in which the node name still decides where a shutdown
  lands, so both the warning and the stricter check apply there.
- `/api/health` reports `hosts_node_ok`, and `/api/status` a `node_state` per host. The
  dashboard shows it as a chip next to the host name, because that is the one place the
  mismatch would otherwise stay invisible: where an entry has its API URL to itself the
  self-test rightly counts it as working, so nothing else on the page says anything.

### Changed
- The cluster self-test warning about nodes without a shutdown target now names both sides
  of the comparison — the cluster's node names and the configured entries — plus the node
  an entry most likely meant, instead of only counting them. Entries that are merely
  disabled are reported as disabled rather than as misnamed.
- The host test checks the node name against the API, offers the right one for a click and
  fills an empty field — including the case where the name belongs to another member of the
  same cluster, where the node behind this API URL is the offer. The verdict is also shown
  as the same chip the dashboard uses, instead of only as a clause in the result sentence.
  An invalid name (a domain suffix, for instance) is reported without a request at all:
  Proxmox rejects it in its own parameter check.

## [3.5.0] - 2026-08-23

### Added
- **Proxmox Backup Server as a shutdown target.** Each host now has a type (Proxmox VE or
  Proxmox Backup Server) that decides how it is talked to. PBS uses its own
  `PBSAPIToken=<id>:<secret>` header scheme and needs the `Sys.PowerManagement` privilege
  on `/system/status`, which is why entering one as a PVE host used to fail as an invalid
  token. The node name is a free label for PBS entries: PBS ignores the node in the API
  path, so the shutdown always addresses `/nodes/localhost/status`.
- **Self-test results per host.** The scheduled credential check now records its outcome
  for every target (`credentials_ok`, `power_mgmt_ok`, `last_test_at`, `last_test_error`).
  They appear in `/api/status` and in the dashboard's host table, so an expired token is
  visible long before an outage needs it.
- `/api/health` reports the shutdown targets: `hosts_total`, `hosts_ok`,
  `hosts_selftest_ok` and `hosts_selftest_at`. The `status` field and the HTTP code are
  deliberately unchanged — they still track the engine, not the credentials.
- **Live preview of the shutdown sequence** under the host list in the wizard. It shows the
  order the engine would actually use, including which hosts share a stage and therefore go
  at the same time — previously that was only readable in a tooltip, and the staged
  behaviour was not visible at all. The *Order* field now also says `0 = first` in its
  label, and it is hidden while *This host* is ticked: that flag is the first sort key, so
  a marked host is last whatever number it carries. The value is kept, so unticking
  restores it.

### Changed
- **Hosts are shut down in stages instead of strictly one after another.** Targets sharing
  a shutdown `order` are now commanded at the same time, and the appliance's own host still
  forms the final stage. Together with a hard per-target deadline this means one machine
  that stops responding can no longer delay the other hosts, the poll loop or the battery
  countdown.
- The feed diagram shows a host's name only; the product name is no longer prefixed, which
  kept overflowing the node. The type stays visible as a chip in the dashboard's host table
  and in the host card's heading, shortened there to `Proxmox BS` so a Backup Server row is
  no wider than a Proxmox VE one.
- *This host* is hidden for Proxmox Backup Server entries in LXC deployments, where the
  combination is impossible — an LXC never runs on a Backup Server. It stays available in
  Docker deployments, where the container may genuinely sit on the PBS and the mark decides
  whether it shuts itself down last.

## [3.4.0] - 2026-08-19

### Added
- **Selectable webhook payload format.** The webhook is no longer tied to one JSON shape.
  Besides `JSON (full status)` (unchanged default) it can now post a **Microsoft Teams**
  adaptive card — the message envelope an incoming webhook / "post to a channel when a
  webhook request is received" workflow expects — or a short, human-readable **plain text**
  status (`text/plain`). Each format is one entry in a table in `app/notify.py`, so further
  target systems are a table row rather than new code.
- **Severity filter for notifications.** A new *Send from level* setting decides which
  events reach the webhook at all: all events, warnings and critical (new default), or
  critical only. Everything below the threshold still goes to the event log.
- **"Send test notification" button** in the notification settings: posts a sample message
  with the values currently entered — without saving first, and regardless of the severity
  filter — and reports the HTTP result (`POST /api/test/webhook`).

### Changed
- **Info events are no longer sent by default.** Existing installations move to the new
  *warnings and critical* default and therefore receive fewer messages; set the filter back
  to *All events (including info)* for the previous behaviour.
- `Host …: shutdown sent` and `Host …: shutdown aborted` are logged as **warnings** instead
  of info — an executed or withdrawn shutdown is not routine, and both now pass the default
  notification filter. They appear amber in the event log from now on.
- Webhook posts check the HTTP status: a `404`/`401` from the target is written to the
  process log instead of passing silently. Notification failures still never affect the
  shutdown logic.
- Documentation touch-ups: the Proxmox user and API token in a cluster, NUT on a
  QNAP/Synology NAS, and community-scripts.org as an additional listing.

## [3.3.0] - 2026-08-06

### Added
- **Vendor MIB support, starting with APC PowerNet.** An SNMP UPS is no longer read only
  through RFC 1628. Every SNMP UPS has a new **MIB** setting — `Automatic` (default),
  `RFC 1628 (standard)` or `APC PowerNet` — and the poller carries a table-driven profile
  per MIB, so further vendors are a table rather than new code.
- `Automatic` needs no configuration and is what existing UPS entries get on update: the
  regular poll carries the vendor anchor object alongside the standard ones, and the UPS
  is read on the vendor MIB as soon as it answers there. Under SNMPv1, where a single
  missing object aborts the whole request, the poller retries on the vendor MIB instead.
- This makes APC cards work that could not be used before: Schneider only supports
  RFC 1628 on Network Management Card 2 (AP9630/AP9631/AP9635) from firmware sumx/sy
  v5.1.7 on, while the older NMC1 cards (AP9617/AP9618/AP9619) speak PowerNet only.
- Cards that support both are now more accurate as well. `upsAdvBatteryRunTimeRemaining`
  is reported in hundredths of a second instead of whole minutes, and PowerNet reports a
  self-test (`onBatteryTest`) as its own state — RFC 1628 cannot tell one apart from a
  real outage.
- The UPS test walks both MIBs in `Automatic` mode and says which one it settled on and
  why, so "nothing works" becomes "this card has no RFC 1628, but PowerNet answers every
  object". The dashboard names the MIB a UPS is being read on.

## [3.2.0] - 2026-07-26

### Added
- **UPS sources**: a UPS is no longer necessarily an SNMP device. Every UPS entry now has
  a "Read via" selector, and the second source is a **NUT server** (Network UPS Tools,
  TCP 3493) — the answer for UPS devices with no network card, i.e. everything attached
  by USB or a serial cable. Any existing `upsd` works: the UPS server built into a
  Synology/QNAP/TrueNAS NAS, a Raspberry Pi, an OPNsense box, or a NUT install on a
  Proxmox host. Configuration is host, port, the UPS name from upsd's `ups.conf`, and
  optionally a user name and password.
- PVE-UPS acts strictly as a *read-only* NUT client: it only ever sends `LIST VAR`, never
  a command, and never runs `upsmon`. NUT is used as a device driver — the shutdown
  decision, the thresholds and the host policy stay in this appliance, so there are still
  no config files to write and no shutdown scripting on any host.
- The UPS test now names the trigger conditions the device cannot feed at all ("This UPS
  does not report: runtime remaining, battery charge"), because a threshold that can never
  fire is more dangerous than a visible error. Applies to both source types — plenty of
  NUT drivers omit `battery.runtime`, and plenty of SNMP cards omit
  `upsEstimatedMinutesRemaining`.
- `type` field in each UPS entry of the `/api/status` snapshot, and the dashboard card
  says which source a UPS is read through.
- `tests/nutsim.py`, a fake `upsd` for development and tests — the NUT counterpart of
  snmpsim + `snmpdata/`. Runnable standalone (`python -m tests.nutsim --scenario battery`)
  to click through the wizard without any UPS hardware.

### Changed
- `POST /api/test/ups` replaces `POST /api/test/snmp` as the wizard's test endpoint and
  handles every source type. The old path stays as an alias and behaves identically.
- Event-log wording and trigger reasons no longer say "SNMP" where they apply to any
  source ("No response for 3 polls …" instead of "No SNMP response for 3 polls …").
  The fail-safe behaviour itself is unchanged.
- The per-object test diagnosis was generalised from "per SNMP object (OID)" to "per
  object" and gained two outcomes: `missing` (the driver does not publish this variable)
  and `stale` (upsd answers, but its driver has lost contact with the UPS).
- Configuration schema: UPS entries carry an explicit `type` (`snmp` or `nut`). Existing
  configuration files and backups are migrated on load — an entry without `type` is read
  as SNMP, exactly as before — and the field is written out on the next save.

### Security
- A NUT server that reports stale data (`ERR DATA-STALE`, `ERR DRIVER-NOT-CONNECTED`) is
  treated as unreachable rather than as a valid reading. `upsd` keeps serving the last
  known values when its driver dies, and accepting those would have meant reporting
  "on mains" through an outage. The same applies when a driver omits `ups.status`
  entirely: the UPS counts as unreachable (alarm, never a shutdown) instead of silently
  looking healthy.

## [3.1.0] - 2026-07-26

### Added
- SNMP test now reports every RFC 1628 object individually, in an expandable "Details per
  SNMP object (OID)" block below the result line: value or
  `noSuchObject`/`noSuchInstance`/error per OID, plus a short overall diagnosis. It unfolds
  on its own when something is wrong. Each object is queried with its own GET, so one
  unimplemented OID no longer hides the state of all the others — notably the SNMPv1 case
  where a single missing object aborts the whole multi-object GET and the regular poll
  fails entirely.
- Configurable self-test interval (15 min, 30 min, 1 h, 2 h, 3 h, 6 h, 12 h, 24 h),
  anchored at the self-test start time, so the resulting times of day are fixed and
  predictable (start 09:00 every 6 h -> 09:00, 15:00, 21:00, 03:00). Existing
  configurations keep the previous daily cadence (24 h default).
- `next_selftest_at` in the `/api/status` appliance snapshot (naive local time).
- Optional Docker deployment alongside the LXC install: a `Dockerfile` and
  `docker-compose.example.yml`, with images published to
  `ghcr.io/ffind-dev/pve-ups` on each release tag. Configuration and event log
  persist via two volumes (`/etc/pve-usv`, `/var/lib/pve-usv`); the LXC path
  remains the default and unaffected.
- Deployment-mode flag `PVE_USV_DEPLOYMENT` (`lxc` default / `docker`). In Docker
  mode there is no privileged companion agent, so the NTP/timezone fields and the
  in-app update uploader are hidden in the wizard, and `POST /api/update/upload`
  returns HTTP 501 with guidance to pull a new image tag and recreate the container.
- Links to the GitHub project in the appliance's own documentation: a GitHub button in
  both manuals' top bar, a link row in the footer of the manuals and the web UI
  (repository, releases, issues, security policy), and the previously plain-text
  references to the releases page and the changelog are now real links.
- Docker section of both manuals now carries a self-contained Compose snippet, so the
  manuals stay the only documentation needed inside a release.

### Changed
- `selftest_hour` is now the *start time* of the self-test schedule rather than a plain
  daily hour, and the self-test itself changed in three ways that a short interval makes
  necessary: the slot that last ran is persisted in `engine-state.json` so a service
  restart no longer re-triggers the test, the hosts are queried concurrently instead of
  one after another (five unreachable hosts stalled the poll loop for 50 s), and a
  successful run is written to the event log at most once a day plus whenever it recovers
  from a failure. Failures are reported and notified as before. While a UPS is on battery
  the self-test is skipped entirely — the countdown has priority. The last result
  (`last_selftest_at`/`last_selftest_ok`) is persisted with the schedule latch, so
  `/api/status` keeps reporting it across a restart.
- `selftest_hour` and `selftest_interval_min` are normalised rather than rejected on load:
  an out-of-range hour is clamped to 0-23 and an unsupported interval falls back to daily,
  so a backup from another version always imports.
- `docker-compose.example.yml` sets `TZ` (the self-test schedule is interpreted in the
  container's local time, and the timezone cannot be set from the web UI in Docker mode)
  and adds `start_period: 15s` to the health check, matching the `Dockerfile`.
- Release workflow additionally tags the image with the bare version
  (`ghcr.io/ffind-dev/pve-ups:3.1.0`) next to the tag name (`:v3.1.0`) and `:latest`, so
  the image can be pinned with the usual registry convention.
- README (EN/DE) and both manuals warn about Docker's default address pool
  (`172.17.0.0/16` – `172.31.0.0/16`) shadowing networks in that range, which would cut
  the container off from a UPS or Proxmox host there.

### Fixed
- **SNMPv3 with encryption (authPriv) never worked.** Every poll failed with
  `Ciphering services not available`, while authNoPriv and v1/v2c were fine: pysnmp 7.x
  ships no ciphers of its own and delegates DES/3DES/AES to the `cryptography` package,
  which it declares only in its dev extra — so nothing installed it. It is now a declared
  dependency and comes along automatically, including on existing installations, because
  applying an update re-runs `pip install`. All privacy protocols were affected equally,
  so switching from DES to AES was no workaround; authNoPriv was. The dependency is capped
  below `cryptography` 50 because pysnmp's AES still uses an API that 49 deprecates and
  announces for removal; a test guards the exact symbols it relies on.
- The same failure is now reported as what it is instead of as "unreachable": it is
  detected before anything is sent, and the message names the missing package and the
  authNoPriv fallback — the previous wording sent users looking for firewall problems
  even though no packet ever left the appliance.

## [3.0.0] - 2026-07-15

First public release, under the public name **PVE-UPS** (technical identifiers — package,
paths, systemd units, env vars — deliberately stay `pve-usv` so in-place updates from 2.x
keep working).

### Added
- Bilingual web UI: English (default) and German. The language is picked automatically
  from the browser language and is easy to extend
  (`app/web/i18n/<lang>.js` + one `<script>` tag).
- English user manual (`manual.html`) alongside the German one (`handbuch.html`), same
  structure and anchors; the ?-icon and all deep links open the manual matching the UI
  language.
- i18n consistency tests (`tests/test_i18n.py`): key parity between the dictionaries,
  placeholder parity, and referenced-key checks for `index.html`/`app.js`.
- Installation and updates via GitHub releases: install one-liner downloads
  `install.sh` from the latest release; the release tarball doubles as the update package
  for the web UI uploader.

### Changed
- **Breaking:** all backend texts — event log entries, trigger reasons, webhook messages,
  API error details — are now uniformly English, regardless of the UI language.
- Webhook subject prefix is now `[PVE-UPS]`.
- Visible product name is PVE-UPS (UI, documentation, webhook); internal names stay
  `pve-usv`.

### Fixed
- The UPS card and the outage banner no longer show the time-based countdown once a UPS
  has triggered (battery low, runtime or charge threshold) — those conditions fire
  immediately and the ticking countdown wrongly suggested the shutdown would wait for
  it. The banner now also explains when triggered UPS are waiting for a host's
  AND/OR policy.

### Removed
- **Breaking:** e-mail (SMTP) notifications. The webhook remains and covers notification
  needs; a legacy `notifications.smtp` config key is ignored on load and dropped on the
  next save.

[Unreleased]: https://github.com/ffind-dev/pve-ups/compare/v4.3.0...HEAD
[4.3.0]: https://github.com/ffind-dev/pve-ups/compare/v4.2.0...v4.3.0
[4.2.0]: https://github.com/ffind-dev/pve-ups/compare/v4.1.0...v4.2.0
[4.1.0]: https://github.com/ffind-dev/pve-ups/compare/v4.0.0...v4.1.0
[4.0.0]: https://github.com/ffind-dev/pve-ups/compare/v3.5.0...v4.0.0
[3.5.0]: https://github.com/ffind-dev/pve-ups/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/ffind-dev/pve-ups/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/ffind-dev/pve-ups/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/ffind-dev/pve-ups/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/ffind-dev/pve-ups/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/ffind-dev/pve-ups/releases/tag/v3.0.0
