"use strict";

const $ = (id) => document.getElementById(id);
const SECRET_PLACEHOLDER = "**********";
const svgIcon = (id) => `<svg class="icon"><use href="#${id}"></use></svg>`;

// --- theme (light / dark / auto), persisted client-side, offline -------------
const THEMES = ["auto", "light", "dark"];
const THEME_ICONS = { auto: "i-monitor", light: "i-sun", dark: "i-moon" };
function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  const btn = $("themeBtn"), use = $("themeUse");
  if (use) use.setAttribute("href", "#" + THEME_ICONS[mode]);
  if (btn) btn.title = t("theme." + mode);
}
function initTheme() {
  const saved = localStorage.getItem("pve-usv-theme");
  applyTheme(THEMES.includes(saved) ? saved : "auto");
}
initTheme();
document.addEventListener("DOMContentLoaded", () => {
  const btn = $("themeBtn");
  if (!btn) return;
  btn.onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme") || "auto";
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    localStorage.setItem("pve-usv-theme", next);
    applyTheme(next);
  };
  applyTheme(document.documentElement.getAttribute("data-theme") || "auto");
});

async function api(path, method = "GET", body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function show(view) {
  ["login", "firstrun", "dashboard", "history", "settings"].forEach((v) => {
    const el = $(v); if (el) el.hidden = (v !== view);
  });
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.view === view));
  // The History tab trades the reading width for chart width (style.css: .view-wide).
  document.body.classList.toggle("view-wide", view === "history");
  // history.js loads after this file; its refresh timer stops whenever the tab is left.
  if (view !== "history" && typeof leaveHistory === "function") leaveHistory();
}

// --- bootstrap --------------------------------------------------------------
let pollTimer = null;
let deployment = "lxc";  // "lxc" (default) or "docker" - set from /api/session in boot()
// Version this page was loaded with. A different one in /api/status means the service
// restarted into an update underneath us, so this app.js no longer matches the backend.
let bootVersion = null;

async function boot() {
  const s = await api("/api/session");
  deployment = s.deployment || "lxc";
  if (!s.password_set) { show("firstrun"); return; }
  if (!s.authenticated) { show("login"); return; }
  $("logoutBtn").hidden = false;
  enterApp();
}

function enterApp() {
  $("mainNav").hidden = false;
  show("dashboard");
  applyDeploymentMode();
  startDashboard();
}

// In Docker deployments there is no privileged agent, so the NTP/timezone fields and
// the in-app update uploader have no effect - hide them and show guidance instead.
function applyDeploymentMode() {
  if (deployment !== "docker") return;
  const timeRow = $("sysTimeRow"); if (timeRow) timeRow.hidden = true;
  const timeNote = $("sysDockerNote"); if (timeNote) timeNote.hidden = false;
  const updBtnRow = $("updBtnRow"); if (updBtnRow) updBtnRow.hidden = true;
  const updDiagWrap = $("updDiagWrap"); if (updDiagWrap) updDiagWrap.hidden = true;
  const updNote = $("updDockerNote"); if (updNote) updNote.hidden = false;
}

// --- auth -------------------------------------------------------------------
// Both of these are <form onsubmit> rather than <button onclick>, and that is what makes
// password managers offer to fill and to save (see index.html). It also covers the Enter
// key for free, which used to need a keydown listener of its own.
$("firstrunForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/password", "POST", { new_password: $("newPw").value });
    location.reload();
  } catch (err) { $("firstErr").textContent = err.message; }
};

$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", "POST", { password: $("loginPw").value });
    $("logoutBtn").hidden = false;
    enterApp();
  } catch (err) { $("loginErr").textContent = err.message; }
};

$("logoutBtn").onclick = async () => { await api("/api/logout", "POST"); location.reload(); };

// --- tabs -------------------------------------------------------------------
// Parameter named tab, not t: t() is the i18n lookup, and shadowing it inside a handler
// that now calls t() would be the same trap loadConfig() used to carry.
document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = async () => {
    const v = tab.dataset.view;
    // Guarded: /api/config needs a session while /api/status does not, so an expired
    // cookie used to make "Settings" do nothing at all — loadConfig() rejected, show()
    // never ran, and the dashboard kept ticking as if everything were fine.
    try {
      if (v === "settings") { await loadConfig(); }
    } catch (e) {
      $("saveMsg").textContent = "✗ " + e.message;
      await boot();          // most likely the session expired: back to the login
      return;
    }
    show(v);
    if (v === "history") await enterHistory();
  };
});

// --- dashboard --------------------------------------------------------------
function startDashboard() {
  refreshStatus();
  refreshEvents();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { refreshStatus(); refreshEvents(); }, 3000);
}

function fmt(v, suffix = "") { return (v === null || v === undefined) ? "–" : v + suffix; }

// Localized labels for the engine/host status enums (display only — raw values
// unchanged). Unknown enum values are shown raw instead of a dictionary key.
const ENGINE_STATES = ["ONLINE", "ON_BATTERY", "SHUTDOWN_PENDING", "SHUTTING_DOWN"];
const SHUTDOWN_STATES = ["idle", "sent", "failed"];
function engineStateLabel(s) { return ENGINE_STATES.includes(s) ? t("state.engine." + s) : s; }
function shutdownStateLabel(s) { return SHUTDOWN_STATES.includes(s) ? t("state.shutdown." + s) : (s || "–"); }
// Short label for tight spots (dashboard chip, host card heading) — "Proxmox BS" instead
// of the full product name, so a PBS row is no wider than a PVE one. The type dropdown
// keeps the spelled-out name, where being unambiguous matters more than width.
// HOST_TYPES is declared further down (with the other config tables); a config written
// before the type existed reports none, which reads as the only target type back then.
function hostTypeLabel(ty) {
  const known = HOST_TYPES.some(([v]) => v === ty);
  return known ? t("htype." + ty + "Short") : (ty || t("htype.pveShort"));
}

// Seconds -> "1 d 3 h 20 min" (compact, readable uptime).
function fmtUptime(s) {
  if (s === null || s === undefined) return "–";
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const parts = [];
  if (d) parts.push(d + " d");
  if (h || d) parts.push(h + " h");
  parts.push(m + " min");
  return parts.join(" ");
}

function pill(text, cls) { return `<span class="pill ${cls}">${esc(text)}</span>`; }

function esc(s) {
  return (s === null || s === undefined ? "" : String(s))
    .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// status class for a UPS (drives card border, diagram colours)
function upsStatusCls(u) {
  if (!u.reachable) return "unreach";
  if (u.triggered) return "trig";
  if (u.power_source === "battery") return "batt";
  return "ok";
}

function upsCardHtml(u) {
  // Source as a slim coloured label (no pill) to avoid the dot/text offset.
  const src = u.power_source === "mains" ? `<b class="src ok">${esc(t("ups.srcMains"))}</b>`
    : u.power_source === "battery" ? `<b class="src warn">${esc(t("ups.srcBattery"))}</b>`
    : `<b class="src muted">${esc(u.power_source || "?")}</b>`;
  // Status shown by colour + icon: plug = mains, bolt = battery, power = triggered,
  // alert = unreachable. Reachability colour is also on the card's left border.
  const statId = !u.reachable ? "i-alert" : u.triggered ? "i-power"
    : u.power_source === "battery" ? "i-bolt" : "i-plug";
  const statTip = !u.reachable ? t("ups.tipUnreach") : u.triggered ? t("ups.tipTrig")
    : u.power_source === "battery" ? t("ups.tipBattery") : t("ups.tipMains");
  const statCls = !u.reachable ? "crit" : u.triggered ? "crit"
    : u.power_source === "battery" ? "warn" : "ok";
  const statIc = `<span class="stat-ic ${statCls}" title="${esc(statTip)}">${svgIcon(statId)}</span>`;
  const pct = (u.battery_charge_pct === null || u.battery_charge_pct === undefined) ? null : u.battery_charge_pct;
  const gw = pct === null ? "0%" : Math.max(0, Math.min(100, pct)) + "%";
  const gcls = pct === null ? "" : pct <= 30 ? " crit" : pct <= 60 ? " warn" : "";
  const model = [u.manufacturer, u.model].filter(Boolean).join(" ");
  // Name the MIB the poll settled on: with "auto" the user cannot otherwise tell whether
  // the UPS is being read on the standard or on its vendor MIB.
  const via = u.mib
    ? t("ups.viaMib", { src: t("src." + (u.type || "snmp")), mib: t("mib." + u.mib) })
    : t("ups.via", { src: t("src." + (u.type || "snmp")) });
  const trig = u.triggered
    ? `<div class="stat"><span>${esc(t("ups.trigger"))}</span><b class="crit-text">${esc(u.trigger_reason || t("ups.triggered"))}</b></div>` : "";
  const cd = u.countdown_remaining_s != null
    ? `<div class="stat"><span>${esc(t("ups.countdown"))}</span><b>${u.countdown_remaining_s} s</b></div>` : "";
  const clr = u.comm_loss_remaining_s != null
    ? `<div class="stat"><span>${esc(t("ups.commLossIn"))}</span><b>${u.comm_loss_remaining_s} s</b></div>` : "";
  // Stored without an address (or, for NUT, without the ups.conf section name), so it is
  // never polled at all. That renders identically to a device that is simply not
  // answering — while the consequence is the same either way: fail safe means every host
  // this UPS feeds is refused a shutdown. Said in its own words, next to the name.
  const incomplete = u.incomplete
    ? ` <span class='chip crit' title="${esc(t("ups.incompleteTitle", { what: u.incomplete }))}">${
        esc(t("ups.incomplete"))}</span>`
    : "";
  return `<div class="card ups-card is-${upsStatusCls(u)}">
    <div class="card-h"><h3><svg class="icon batt-ic"><use href="#i-battery"></use></svg>${esc(u.name)}${incomplete}</h3>${statIc}</div>
    <div class="hero-meta"><span>${esc(t("ups.source"))} ${src}</span><span class="faint">·</span><span>${esc(model) || "–"}</span><span class="faint">·</span><span class="muted">${esc(via)}</span></div>
    <div class="metric" style="margin-top:8px">
      <span class="k">${esc(t("ups.charge"))} ${pct === null ? "–" : pct + " %"}</span>
      <div class="gauge"><div class="gauge-fill${gcls}" style="width:${gw}"></div></div>
    </div>
    <div class="stat"><span>${esc(t("ups.runtime"))}</span><b>${fmt(u.runtime_remaining_min, " min")}</b></div>
    ${u.load_pct === null || u.load_pct === undefined ? ""
      : `<div class="stat" title="${esc(t("ups.loadTitle"))}"><span>${esc(t("ups.load"))}</span><b>${u.load_pct} %</b></div>`}
    <div class="stat"><span>${esc(t("ups.battery"))}</span><b>${esc(u.battery_status)}</b></div>
    ${cd}${clr}${trig}
    <div class="stat"><span>${esc(t("ups.lastPoll"))}</span><b>${u.last_poll ? new Date(u.last_poll).toLocaleTimeString() : "–"}</b></div>
  </div>`;
}

// An update restarts the service while open tabs keep running the old app.js against
// the new backend. Ask for a reload instead of letting the mismatch play out silently;
// the reload is enough because "/" is never cached and the assets carry fresh stamps.
function checkAppVersion(version) {
  if (!version) return;
  if (bootVersion === null) { bootVersion = version; return; }
  const el = $("reloadNote");
  if (!el || version === bootVersion || !el.hidden) return;  // build the note once
  el.innerHTML = svgIcon("i-info") + "<span>" + esc(t("reload.newVersion", { v: version })) +
    "</span> <button class=\"btn-ghost btn-sm\" id=\"reloadBtn\"></button>";
  $("reloadBtn").textContent = t("reload.btn");
  $("reloadBtn").onclick = () => location.reload();
  el.hidden = false;
}

// Last snapshot, kept so the settings view can show things only /api/status knows —
// currently how each webhook's last delivery went.
let lastStatus = null;

async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); } catch (_) { return; }
  lastStatus = s;
  // Cheap and idempotent: it is a no-op unless webhook cards are on screen, and it is
  // what keeps their delivery note current while the settings view stays open.
  updateWebhookDelivery();
  $("version").textContent = "v" + s.appliance.version;
  // The tab exists only while the history is recorded; switched off elsewhere (another
  // browser, a backup import), an open History view falls back to the dashboard.
  const histOn = !!s.appliance.history_enabled;
  $("historyTab").hidden = !histOn;
  if (!histOn && !$("history").hidden) show("dashboard");
  checkAppVersion(s.appliance.version);

  const a = s.appliance, sd = s.shutdown, upses = s.ups || [];

  $("d_ups_grid").innerHTML = upses.map(upsCardHtml).join("")
    || `<div class='card'><p class='empty'>${esc(t("ups.none"))}</p></div>`;

  const stateLbl = engineStateLabel(a.engine_state);
  $("d_state").innerHTML = a.engine_state === "ONLINE" ? pill(stateLbl, "ok")
    : a.engine_state === "ON_BATTERY" ? pill(stateLbl, "warn") : pill(stateLbl, "crit");
  $("d_mode").innerHTML = a.dry_run ? pill("DRY-RUN", "warn") : pill(t("mode.armed"), "ok");
  // A notification target that has stopped working was only visible on its settings card,
  // and the person watching an outage is looking at THIS page. Notifications stay
  // best-effort — a failing one never delays or affects a shutdown — but an alarm nobody
  // receives is an alarm nobody acts on, so it is worth a line where it will be seen.
  // The row disappears again by itself once a send succeeds.
  const deadHooks = (s.webhooks || [])
    .filter((w) => w.enabled && w.last_delivery_ok === false);
  const notifRow = $("d_notifRow");
  if (notifRow) {
    notifRow.hidden = deadHooks.length === 0;
    if (deadHooks.length) {
      const names = deadHooks.map((w) => w.name || w.id).join(", ");
      $("d_notif").innerHTML = `<span class="chip warn" title="${
        esc(t("notif.deadChipTitle", { names }))}">${
        esc(t("notif.deadChip", { n: deadHooks.length }))}</span>`;
    }
  }
  $("d_trig").textContent = sd.triggered ? t("common.yes") : t("common.no");
  $("d_reason").textContent = sd.reason || "–";
  $("d_countdown").textContent = fmt(sd.countdown_remaining_s, " s");
  $("d_uptime").textContent = fmtUptime(a.uptime_s);
  renderClusters(s.clusters, sd.triggered);

  // banner + header status chip (aggregate across all UPS)
  const anyBattery = upses.some((u) => u.power_source === "battery");
  const anyUnreachable = upses.some((u) => !u.reachable);
  const b = $("banner");
  const setBanner = (cls, ic, text) => { b.hidden = false; b.className = "banner " + cls; b.innerHTML = svgIcon(ic) + "<span>" + text + "</span>"; };
  let chip = { cls: "ok", text: t("chip.mains") };
  if (sd.triggered) {
    setBanner("crit", "i-power", esc(t("banner.trig", { reason: sd.reason || "" })));
    chip = { cls: "crit", text: t("chip.trig") };
  } else if (anyBattery) {
    const n = upses.filter((u) => u.power_source === "battery").length;
    const m = upses.filter((u) => u.triggered).length;
    let txt = t("banner.outage", { n });
    if (sd.countdown_remaining_s != null) txt += t("banner.outageCountdown", { s: sd.countdown_remaining_s });
    txt += ".";
    // Some UPS already demand a shutdown, but no host is due yet (AND policy waiting
    // for the remaining feeds) — say so instead of leaving the wait unexplained.
    if (m > 0) txt += " " + t("banner.outageTriggered", { m });
    setBanner("warn", "i-bolt", esc(txt));
    chip = { cls: "warn", text: t("chip.battery") };
  } else if (anyUnreachable) {
    // A timer read back from the state file that no poll of this process has confirmed:
    // it is shown, but it cannot fire, and the countdown is deliberately null. Said in
    // its own words rather than through "alarm (no shutdown)" — that one is true here and
    // still answers a different question than the one being asked, which is why an outage
    // is on screen while nothing is counting down.
    const held = upses.filter((u) => u.elapsed_source === "own-unconfirmed");
    if (held.length) {
      setBanner("warn", "i-alert", esc(t("banner.restoredUnconfirmed",
                                         { names: held.map((u) => u.name).join(", ") })));
    } else if (sd.countdown_remaining_s != null) {
      setBanner("warn", "i-alert", esc(t("banner.unreachCountdown", { s: fmt(sd.countdown_remaining_s, " s") })));
    } else if (sd.comm_loss_remaining_s != null) {
      setBanner("warn", "i-alert", esc(t("banner.unreachCommLoss", { s: fmt(sd.comm_loss_remaining_s, " s") })));
    } else {
      setBanner("warn", "i-alert", esc(t("banner.unreachAlarm")));
    }
    chip = { cls: "warn", text: t("chip.unreach") };
  } else if (a.dry_run && a.config_valid) {
    // Last in the chain on purpose: a real outage or a fired trigger always wins the
    // banner. But with nothing else to report, this is the one thing worth the space —
    // an appliance left in dry-run is fully configured, self-tests green, answers
    // /api/health with "ok" and shuts nothing down. A pill in the stat grid was the only
    // thing saying so.
    //
    // config_valid, like the matching self-test line (see _run_selftest): before the
    // wizard has been through once, dry-run is not a half-finished commissioning but the
    // correct state, and greeting a fresh installation with a warning banner about its
    // own default teaches the operator to read past this one.
    setBanner("warn", "i-alert", esc(t("banner.dryRun")));
  } else { b.hidden = true; }

  const nav = $("navStatus");
  nav.hidden = false;
  nav.className = "statuschip " + chip.cls;
  $("navStatusText").textContent = chip.text;

  rememberClusterNames(s.hosts);

  const rows = s.hosts.map((h) => {
    const st = h.shutdown_state;
    const cls = st === "sent" ? "ok" : st === "failed" ? "crit" : h.eligible ? "warn" : "muted";
    const feeds = (h.feeds || []).map((f) => `<span class="chip ${f.triggered ? "crit" : "muted"}">${esc(f.name)}</span>`).join(" ")
      || `<span class='muted'>${esc(t("hosts.allUps"))}</span>`;
    const policy = h.ups_policy === "any" ? t("hosts.policyOr") : t("hosts.policyAnd");
    const stLbl = shutdownStateLabel(st);
    const star = h.this_host
      ? ` <span class='chip star' title="${esc(t("hosts.thisChipTitle"))}">${esc(t("hosts.thisChip"))}</span>` : "";
    const kind = ` <span class='chip muted'>${esc(hostTypeLabel(h.type))}</span>`;
    // Same reasoning as the ★: membership decides whether this node gets a cluster-wide
    // preparation before it goes down, so it belongs next to the name.
    const clus = h.cluster
      ? ` <span class='chip muted' title="${esc(t("hosts.clusterChipTitle"))}">${esc(h.cluster_name || t("host.sumCluster"))}</span>` : "";
    // A node name the API does not know. It no longer misdirects the shutdown (that goes
    // to the machine behind the API URL), but it labels this host in every event and on
    // this very row — and with its own API URL the self-test counts the entry as fine, so
    // without this chip the mismatch would live in the event log alone.
    const nodeBad = ["wrong", "invalid", "proxied"].includes(h.node_state)
      ? ` <span class='chip warn' title="${esc(t("nodest.chipTitle"))}">${esc(t("nodest." + h.node_state))}</span>` : "";
    // A credential that broke long before any outage would otherwise stay invisible:
    // show the self-test complaint whenever there is no fresher shutdown error.
    // The one failure that is otherwise completely silent: every UPS this host names is
    // gone, so it has no feed left, is never eligible, and is simply never shut down.
    // It looks exactly like a host waiting for an outage that has not come.
    const stale = (h.stale_ups_ids || []).length
      ? ` <span class='chip crit' title="${esc(t("hosts.staleFeedTitle",
          { ids: (h.stale_ups_ids || []).join(", ") }))}">${esc(t("hosts.staleFeed"))}</span>`
      : "";
    // Stored without what the shutdown needs to reach it — an API URL, a token id, a
    // token secret. The browser refuses to save such a card, but a backup import and a
    // hand-edited config.yaml never pass that check, and nothing else says so in time:
    // the node check answers "unverified" for an entry it cannot even address, which is
    // read as no verdict rather than as a fault.
    const incomplete = h.incomplete
      ? ` <span class='chip crit' title="${esc(t("hosts.incompleteTitle",
          { what: h.incomplete }))}">${esc(t("hosts.incomplete"))}</span>`
      : "";
    const err = h.last_error || h.last_test_error || "";
    return `<tr><td>${esc(h.name)}${kind}${clus}${nodeBad}${stale}${incomplete}${star}</td>
      <td>${feeds} <span class="muted">(${esc(policy)})</span></td>
      <td>${pill(stLbl, cls)}</td><td class="muted">${esc(err)}</td></tr>`;
  }).join("");
  $("d_hosts").innerHTML = rows || `<tr><td class='empty' colspan='4'>${esc(t("hosts.none"))}</td></tr>`;

  // live topology on the dashboard
  $("d_topo_card").hidden = upses.length === 0;
  if (upses.length) {
    const statusMap = {};
    upses.forEach((u) => { statusMap[u.id] = { power_source: u.power_source, reachable: u.reachable, triggered: u.triggered }; });
    drawTopology($("topoDiagramDash"), upses.map((u) => ({ id: u.id, name: u.name })), s.hosts, statusMap);
  }
}

async function refreshEvents() {
  let ev;
  try { ev = await api("/api/events?limit=50"); } catch (_) { return; }
  // esc() everywhere: event details carry device-/network-supplied strings (SNMP error
  // texts, Proxmox responses) — never trust them as HTML.
  $("d_events").innerHTML = ev.map((e) =>
    `<tr><td>${new Date(e.ts).toLocaleString()}</td>
      <td><span class="sev sev-${esc(e.severity)}">${esc(e.severity)}</span></td>
      <td>${esc(e.event)}</td><td class="muted">${esc(e.detail || "")}</td></tr>`).join("")
    || `<tr><td class='empty' colspan='4'>${esc(t("events.none"))}</td></tr>`;
}

$("testShutdownBtn").onclick = async () => {
  if (!confirm(t("confirm.testShutdown"))) return;
  try { const r = await api("/api/test/shutdown", "POST"); $("actionMsg").textContent = r.message; }
  catch (e) { $("actionMsg").textContent = e.message; }
  refreshEvents();
};
$("resetBtn").onclick = async () => {
  try { await api("/api/reset", "POST"); $("actionMsg").textContent = t("msg.reset"); }
  catch (e) { $("actionMsg").textContent = e.message; }
  refreshStatus();
};
const _apReload = document.getElementById("ap_reload");
if (_apReload) _apReload.onclick = loadApplianceGuests;
const _apPick = document.getElementById("ap_self_pick");
if (_apPick) _apPick.onchange = onApplianceChange;
["th_cluster_prep_timeout_s", "th_cluster_guest_shutdown_timeout_s",
 "th_host_shutdown_timeout_s"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", updateClusterBudgetHint);
});

$("selftestBtn").onclick = async () => {
  $("actionMsg").textContent = t("msg.selftesting");
  try {
    const r = await api("/api/selftest/run", "POST");
    $("actionMsg").textContent = (r.ok ? "✓ " : "✗ ") + r.message;
  } catch (e) { $("actionMsg").textContent = "✗ " + e.message; }
  refreshStatus();
  refreshEvents();
};

$("restoreClusterBtn").onclick = async () => {
  if (!confirm(t("confirm.restoreCluster"))) return;
  $("actionMsg").textContent = t("msg.restoringCluster");
  try {
    const r = await api("/api/cluster/restore", "POST");
    $("actionMsg").textContent = (r.ok ? "✓ " : "✗ ") + r.message;
  } catch (e) { $("actionMsg").textContent = "✗ " + e.message; }
  refreshStatus();
  refreshEvents();
};

// Cluster name per host, keyed by the host id (HostConfig.key on the backend). It is
// discovered from the API, not configured, so the settings cards can only learn it from a
// status refresh — hence this cache and the redraw below. Keyed on the id rather than the
// node name so that correcting a name does not orphan the entry.
const CLUSTER_NAMES = {};

function rememberClusterNames(hosts) {
  let changed = false;
  (hosts || []).forEach((h) => {
    if (!h.cluster_name || !h.id) return;
    if (CLUSTER_NAMES[h.id] !== h.cluster_name) {
      CLUSTER_NAMES[h.id] = h.cluster_name;
      changed = true;
    }
  });
  // Only on a real change: the summary is rewritten on every keystroke otherwise.
  if (changed) {
    document.querySelectorAll("#hostRows .host-cfg").forEach((el) => {
      if (el._updSum) el._updSum();
    });
  }
}

// The button only appears while there is actually something to undo — an armed cluster
// with clean flags needs no action, and offering one would invite pointless clicks.
// It is also hidden during a shutdown: needs_recovery turns true the moment the
// preparation lands, and undoing it there would arm HA while the nodes are powering off.
// The API refuses that too; this only keeps the button from inviting the attempt.
function renderClusters(clusters, shuttingDown) {
  const btn = $("restoreClusterBtn");
  const help = $("clusterStateHelp");
  if (!btn || !help) return;
  const list = clusters || [];
  const stale = list.filter((c) => c.needs_recovery);
  btn.hidden = stale.length === 0 || !!shuttingDown;
  if (!list.length) { help.hidden = true; return; }
  help.hidden = false;
  help.innerHTML = list.map((c) => {
    const bits = [];
    if (!c.quorate) bits.push(t("cluster.noQuorum"));
    if (c.ceph_flags_set && c.ceph_flags_set.length) {
      bits.push(t("cluster.cephFlags", { flags: c.ceph_flags_set.join(", ") }));
    }
    if (c.ha_armed_state && c.ha_armed_state !== "armed") {
      bits.push(t("cluster.haDisarmed", { state: c.ha_armed_state }));
    }
    // Two deployment mistakes that only bite during an outage, so they belong where the
    // operator actually looks. On Ceph storage is always worth saying (it is only ever
    // true on a Ceph cluster); an unknown own guest only where the guest stop is on and
    // waits for it — elsewhere no guests are stopped and the question does not arise.
    if (c.self_guest_on_ceph === true) bits.push(t("cluster.selfOnCeph"));
    else if (c.self_guest_missing) bits.push(t("cluster.selfUnknown"));
    const state = bits.length ? bits.join(" · ") : t("cluster.ok");
    const guests = c.guests_readable
      ? " · " + t("cluster.guests", { running: c.guests_running, total: c.guests_total })
      : "";
    return `<b>${esc(c.name)}</b> (${c.nodes_online}/${(c.nodes || []).length} `
      + `${esc(t("cluster.nodes"))}${esc(guests)}): ${esc(state)}`;
  }).join("<br>");
}

$("clearLogBtn").onclick = async () => {
  if (!confirm(t("confirm.clearLog"))) return;
  try { await api("/api/events", "DELETE"); } catch (e) { $("actionMsg").textContent = e.message; }
  refreshEvents();
};

// --- settings ---------------------------------------------------------------
let currentConfig = null;

function setVal(id, v) { const el = $(id); if (el) el.value = (v === null || v === undefined) ? "" : v; }
function setChk(id, v) { const el = $(id); if (el) el.checked = !!v; }
function getVal(id) { return $(id).value; }
function getNum(id) { const v = $(id).value.trim(); return v === "" ? null : Number(v); }
function getChk(id) { return $(id).checked; }

// ===== UPS devices (dynamic list) ==========================================
// t() is available at parse time (i18n.js loads before app.js).
const AUTH_PROTOS = [["none", t("proto.none")], ["md5", "MD5"], ["sha", "SHA"], ["sha256", "SHA-256"], ["sha512", "SHA-512"]];
const PRIV_PROTOS = [["none", t("proto.none")], ["des", "DES"], ["aes", "AES-128"], ["aes256", "AES-256"]];
// Must match config.UpsSourceType; tests/test_i18n.py keeps the labels in both dictionaries.
const SOURCE_TYPES = [["snmp", t("src.snmp")], ["nut", t("src.nut")]];
// Must match config.SnmpMib; tests/test_i18n.py keeps the labels in both dictionaries.
const SNMP_MIBS = [["auto", t("mib.auto")], ["rfc1628", t("mib.rfc1628")], ["apc", t("mib.apc")],
  ["cyberpower", t("mib.cyberpower")]];
const DEFAULT_PORTS = { snmp: 161, nut: 3493 };
// Must match config.HostType; tests/test_i18n.py keeps the labels in both dictionaries.
const HOST_TYPES = [["pve", t("htype.pve")], ["pbs", t("htype.pbs")]];
const HOST_DEFAULT_PORTS = { pve: 8006, pbs: 8007 };
const TRISTATE = [["", t("tristate.global")], ["on", t("tristate.on")], ["off", t("tristate.off")]];
// Order and values mirror config.WebhookFormat / WebhookLevel (tests/test_i18n.py checks it).
const WEBHOOK_FORMATS = [["json", t("whfmt.json")], ["teams", t("whfmt.teams")], ["text", t("whfmt.text")], ["slack", t("whfmt.slack")], ["discord", t("whfmt.discord")], ["ntfy", t("whfmt.ntfy")], ["custom", t("whfmt.custom")]];
const WEBHOOK_LEVELS = [["info", t("whlvl.info")], ["warning", t("whlvl.warning")], ["critical", t("whlvl.critical")]];
const opts = (list, val) => list.map(([v, l]) => `<option value="${v}" ${v === val ? "selected" : ""}>${l}</option>`).join("");
const triVal = (v) => v === true ? "on" : v === false ? "off" : "";

// A fresh id for a card the user just added — never one the backend still holds a secret
// for.
//
// Reading the lowest free "upsN"/"hostN" off the FORM alone was the wrong half of the
// answer: delete host2, add a host, and the new card is handed host2 again. The backend
// then matches that id against the STORED host2 and copies its API token into the new
// entry (an empty secret field means "unchanged"), so a brand-new shutdown target arrives
// pre-loaded with a different machine's credentials, pointed at a different IP.
//
// So the last loaded configuration is consulted as well — it still lists the entry that
// was just removed from the form, which is exactly the one that must not be handed out
// again. Once the deletion has been saved, loadConfig() refreshes it and the number is
// genuinely free: there is no stored secret left for _reconcile_secret() to resolve the
// placeholder to, so it resolves to "".
//
// Deliberately not a random suffix, which was the first fix here and solved it by making
// the id unreadable: an entry without a name falls back to its id (UpsBase.label,
// WebhookConfig.label), so the dashboard, the topology, the feed checkboxes, the event log
// and every webhook message would have called it "ups-a3f9k2".
//
// What this does NOT cover, and a random suffix would have: a second browser tab whose
// configuration predates an entry another tab added and saved. That tab sees the number as
// free on both counts and can hand it out again. It is a narrow case and already a losing
// one — a whole-config POST from a stale tab overwrites the other tab's work regardless —
// and for the entry where it would matter most it is closed anyway: incompleteCards()
// refuses to save a host card whose id the loaded configuration does not know without a
// token secret typed into it, so nothing is left for the backend to fill in.
//
// assign_*_ids() on the backend leaves a non-empty id alone either way, so whatever is
// picked here survives the round trip unchanged.
function newCardId(prefix, selector, stored) {
  const taken = new Set(Array.from(document.querySelectorAll(selector)).map((el) => el.value));
  (stored || []).forEach((entry) => { if (entry && entry.id) taken.add(entry.id); });
  let n = 1;
  while (taken.has(prefix + n)) n += 1;
  return prefix + n;
}

function nextUpsId() {
  return newCardId("ups", "#upsList .u_id", currentConfig && currentConfig.ups);
}

function upsMeta() {
  return Array.from(document.querySelectorAll("#upsList .ups-cfg")).map((d) => ({
    id: d.querySelector(".u_id").value,
    name: d.querySelector(".u_name").value.trim() || d.querySelector(".u_id").value,
  }));
}

function renderUps(list) {
  $("upsList").innerHTML = "";
  (list || []).forEach((u) => addUpsCard(u, false));  // loaded cards start collapsed
  if (!list || list.length === 0) addUpsCard({}, true);  // first-run card stays open
}

function addUpsCard(u, open) {
  u = u || {};
  const ov = u.overrides || {};
  const id = u.id || nextUpsId();
  const div = document.createElement("details");
  div.className = "ups-cfg";
  if (open !== false) div.open = true;
  const type = u.type || "snmp";
  const commPh = u.community === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "public";
  const authPh = u.v3_auth_pass === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "";
  const privPh = u.v3_priv_pass === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "";
  const nutPh = u.password === SECRET_PLACEHOLDER ? t("cfg.unchanged") : t("cfg.nutPwPh");
  div.innerHTML = `
    <summary class="cfg-head">${svgIcon("i-battery")}<span class="cfg-title u_sum_name"></span><span class="cfg-sub u_sum_host"></span></summary>
    <input type="hidden" class="u_id" value="${esc(id)}" />
    <div class="row">
      <label title="${esc(t("cfg.nameTitle"))}">${esc(t("cfg.name"))} <input class="u_name" value="${esc(u.name || "")}" placeholder="${esc(t("cfg.upsNamePh", { id }))}" /></label>
      <label title="${esc(t("cfg.srcTypeTitle"))}">${esc(t("cfg.srcType"))} <select class="u_type">${opts(SOURCE_TYPES, type)}</select></label>
      <label title="${esc(t("cfg.hostipTitle"))}">${esc(t("cfg.hostip"))} <input class="u_host" value="${esc(u.host || "")}" placeholder="10.0.0.9" /></label>
      <label title="${esc(t("cfg.portTitle"))}">${esc(t("cfg.port"))} <input class="u_port" type="number" value="${u.port || DEFAULT_PORTS[type]}" /></label>
    </div>
    <div class="u_snmp">
      <div class="row">
        <label title="${esc(t("cfg.versionTitle"))}">${esc(t("cfg.version"))} <select class="u_version">${opts([["v1", "v1"], ["v2c", "v2c"], ["v3", "v3"]], u.version || "v2c")}</select></label>
        <label title="${esc(t("cfg.mibTitle"))}">${esc(t("cfg.mib"))} <select class="u_mib">${opts(SNMP_MIBS, u.mib || "auto")}</select></label>
      </div>
      <div class="u_v2c">
        <label title="${esc(t("cfg.communityTitle"))}">${esc(t("cfg.community"))} <input class="u_community" placeholder="${esc(commPh)}" /></label>
      </div>
      <div class="u_v3" hidden>
        <div class="row">
          <label title="${esc(t("cfg.v3userTitle"))}">${esc(t("cfg.v3user"))} <input class="u_v3_user" value="${esc(u.v3_user || "")}" /></label>
          <label title="${esc(t("cfg.v3authTitle"))}">${esc(t("cfg.v3auth"))} <select class="u_v3_auth_proto">${opts(AUTH_PROTOS, u.v3_auth_proto || "sha")}</select></label>
          <label title="${esc(t("cfg.v3authpwTitle"))}">${esc(t("cfg.v3authpw"))} <input class="u_v3_auth_pass" type="password" autocomplete="new-password" placeholder="${esc(authPh)}" /></label>
        </div>
        <div class="row">
          <label title="${esc(t("cfg.v3privTitle"))}">${esc(t("cfg.v3priv"))} <select class="u_v3_priv_proto">${opts(PRIV_PROTOS, u.v3_priv_proto || "aes")}</select></label>
          <label title="${esc(t("cfg.v3privpwTitle"))}">${esc(t("cfg.v3privpw"))} <input class="u_v3_priv_pass" type="password" autocomplete="new-password" placeholder="${esc(privPh)}" /></label>
        </div>
      </div>
    </div>
    <div class="u_nut" hidden>
      <div class="row">
        <label title="${esc(t("cfg.nutNameTitle"))}">${esc(t("cfg.nutName"))} <input class="u_ups_name" value="${esc(u.ups_name || "")}" placeholder="ups" /></label>
        <label title="${esc(t("cfg.nutUserTitle"))}">${esc(t("cfg.nutUser"))} <input class="u_nut_user" value="${esc(u.username || "")}" /></label>
        <label title="${esc(t("cfg.nutPwTitle"))}">${esc(t("cfg.nutPw"))} <input class="u_nut_pass" type="password" autocomplete="new-password" placeholder="${esc(nutPh)}" /></label>
      </div>
      <p class="muted" style="margin:.15rem 0 0">${esc(t("cfg.nutHint"))}</p>
    </div>
    <div class="row u_tuning">
      <label title="${esc(t("cfg.timeoutTitle"))}">${esc(t("cfg.timeout"))} <input class="u_timeout" type="number" step="0.5" min="0.5" value="${u.timeout_s ?? 3}" /></label>
      <label class="u_retries_wrap" title="${esc(t("cfg.retriesTitle"))}">${esc(t("cfg.retries"))} <input class="u_retries" type="number" min="0" value="${u.retries ?? 1}" /></label>
    </div>
    <p class="help u_tuning_help">${esc(t("cfg.tuningHelp"))}</p>
    <details class="u_over">
      <summary>${esc(t("cfg.overrideSummary"))} <span class="muted">${esc(t("cfg.overrideGlobal"))}</span></summary>
      <div class="row">
        <label title="${esc(t("cfg.oObsTitle"))}">${esc(t("cfg.oObs"))} <input class="o_obs" type="number" value="${ov.on_battery_seconds ?? ""}" /></label>
        <label title="${esc(t("cfg.oRbmTitle"))}">${esc(t("cfg.oRbm"))} <input class="o_rbm" type="number" value="${ov.runtime_below_minutes ?? ""}" /></label>
        <label title="${esc(t("cfg.oCbpTitle"))}">${esc(t("cfg.oCbp"))} <input class="o_cbp" type="number" value="${ov.charge_below_percent ?? ""}" /></label>
      </div>
      <div class="row">
        <label title="${esc(t("cfg.oOblTitle"))}">${esc(t("cfg.oObl"))} <select class="o_obl">${opts(TRISTATE, triVal(ov.on_battery_low))}</select></label>
        <label title="${esc(t("cfg.oClmTitle"))}">${esc(t("cfg.oClm"))} <input class="o_clm" type="number" value="${ov.comm_loss_shutdown_after_min ?? ""}" /></label>
        <label title="${esc(t("cfg.oKscTitle"))}">${esc(t("cfg.oKsc"))} <select class="o_ksc">${opts(TRISTATE, triVal(ov.keep_shutdown_on_comm_loss))}</select></label>
      </div>
    </details>
    <div class="row" style="margin:0;align-items:center">
      <button class="btn-ghost btn-sm u_test" style="flex:0 0 auto">${esc(t("cfg.testUps"))}</button>
      <button class="btn-ghost btn-sm u_del" style="flex:0 0 auto">${esc(t("cfg.removeUps"))}</button>
      <span class="muted u_msg"></span>
    </div>
    <details class="help u_diagwrap" hidden style="margin-top:.5rem">
      <summary>${esc(t("probe.diag"))}</summary>
      <pre class="u_diag" style="white-space:pre-wrap;overflow:auto;max-height:16rem;font-family:monospace;font-size:.85em"></pre>
    </details>`;
  const toggleVer = () => {
    const v3 = div.querySelector(".u_version").value === "v3";
    div.querySelector(".u_v3").hidden = !v3;
    div.querySelector(".u_v2c").hidden = v3;
  };
  const toggleType = () => {
    const ty = div.querySelector(".u_type").value;
    div.querySelector(".u_snmp").hidden = ty !== "snmp";
    div.querySelector(".u_nut").hidden = ty !== "nut";
    // Retries are an SNMP notion; the NUT client talks TCP and has a timeout only.
    div.querySelector(".u_retries_wrap").hidden = ty !== "snmp";
    updSum();
  };
  const updSum = () => {
    const nm = div.querySelector(".u_name").value.trim() || t("cfg.upsNamePh", { id });
    const hs = div.querySelector(".u_host").value.trim();
    const ty = div.querySelector(".u_type").value;
    div.querySelector(".u_sum_name").textContent = nm;
    div.querySelector(".u_sum_host").textContent = hs ? "· " + hs + " (" + t("src." + ty) + ")" : "";
  };
  div.querySelector(".u_version").onchange = toggleVer;
  div.querySelector(".u_type").onchange = () => {
    // Carry the port over to the new type's default, unless the user typed their own.
    const ty = div.querySelector(".u_type").value;
    const portEl = div.querySelector(".u_port");
    const known = Object.values(DEFAULT_PORTS).includes(Number(portEl.value));
    if (!portEl.value || known) portEl.value = DEFAULT_PORTS[ty];
    toggleType();
  };
  div.querySelector(".u_name").oninput = () => { updSum(); renderHostUpsCheckboxes(); drawConfigTopology(); };
  div.querySelector(".u_host").oninput = updSum;
  div.querySelector(".u_test").onclick = () => testUps(div);
  div.querySelector(".u_del").onclick = () => { div.remove(); renderHostUpsCheckboxes(); drawConfigTopology(); };
  $("upsList").appendChild(div);
  toggleVer();
  toggleType();
}

$("addUpsBtn").onclick = () => { addUpsCard({}, true); renderHostUpsCheckboxes(); drawConfigTopology(); };

function upsFromCard(div) {
  const q = (s) => div.querySelector(s);
  const numOr = (s) => { const v = q(s).value.trim(); return v === "" ? null : Number(v); };
  const tri = (s) => { const v = q(s).value; return v === "on" ? true : v === "off" ? false : null; };
  const type = q(".u_type").value;
  // Only the fields of the selected type are sent; the backend picks the model by "type".
  const base = {
    id: q(".u_id").value,
    name: q(".u_name").value.trim(),
    type,
    host: q(".u_host").value.trim(),
    port: Number(q(".u_port").value || DEFAULT_PORTS[type]),
    timeout_s: Number(q(".u_timeout").value || 3),
    overrides: {
      on_battery_seconds: numOr(".o_obs"),
      runtime_below_minutes: numOr(".o_rbm"),
      charge_below_percent: numOr(".o_cbp"),
      on_battery_low: tri(".o_obl"),
      comm_loss_shutdown_after_min: numOr(".o_clm"),
      keep_shutdown_on_comm_loss: tri(".o_ksc"),
    },
  };
  if (type === "nut") {
    const np = q(".u_nut_pass").value;
    return Object.assign(base, {
      ups_name: q(".u_ups_name").value.trim(),
      username: q(".u_nut_user").value.trim(),
      password: np === "" ? SECRET_PLACEHOLDER : np,
    });
  }
  const comm = q(".u_community").value, ap = q(".u_v3_auth_pass").value, pp = q(".u_v3_priv_pass").value;
  return Object.assign(base, {
    version: q(".u_version").value,
    mib: q(".u_mib").value,
    retries: Number(q(".u_retries").value || 0),
    community: comm === "" ? SECRET_PLACEHOLDER : comm,
    v3_user: q(".u_v3_user").value,
    v3_auth_proto: q(".u_v3_auth_proto").value,
    v3_auth_pass: ap === "" ? SECRET_PLACEHOLDER : ap,
    v3_priv_proto: q(".u_v3_priv_proto").value,
    v3_priv_pass: pp === "" ? SECRET_PLACEHOLDER : pp,
  });
}

function currentUpsList() {
  return Array.from(document.querySelectorAll("#upsList .ups-cfg"))
    .map(upsFromCard).filter((u) => u.host || u.name);
}

// One line per object (RFC 1628 OID or NUT variable), so a user can see exactly which one
// the UPS is missing. Status words come from the dictionary, the summary is the backend's
// English text (like every other API message).
function renderProbe(p) {
  const lines = (p.entries || []).map((e) => {
    const st = t("probe.st." + e.status, { err: e.error || "" });
    // 32 fits the longest vendor OID (APC PowerNet is 29 chars) plus a gap.
    return e.name.padEnd(32) + (e.oid || "").padEnd(e.oid ? 32 : 0) + st
      + (e.status === "ok" ? " = " + e.value : "");
  });
  const head = [t("probe.head", { ok: p.ok_count, n: p.total })];
  // A threshold that can never fire is worse than a visible error: say so up front.
  if ((p.missing_triggers || []).length) {
    head.push(t("probe.missingTriggers", {
      list: p.missing_triggers.map((x) => t("probe.trg." + x)).join(", "),
    }));
  }
  return head.concat([p.summary || "", ""]).concat(lines).join("\n");
}

async function testUps(div) {
  const msg = div.querySelector(".u_msg");
  const wrap = div.querySelector(".u_diagwrap");
  const pre = div.querySelector(".u_diag");
  msg.textContent = t("msg.testing");
  wrap.hidden = true;
  try {
    const r = await api("/api/test/ups", "POST", upsFromCard(div));
    msg.textContent = r.reachable
      ? t("probe.ok", { src: r.power_source, batt: r.battery_status, min: r.runtime_remaining_min, pct: r.battery_charge_pct })
      : t("probe.fail", { err: r.error || "" });
    if (r.probe) {
      const trouble = !r.reachable || r.probe.ok_count < r.probe.total
        || (r.probe.missing_triggers || []).length > 0;
      pre.textContent = renderProbe(r.probe);
      wrap.hidden = false;
      wrap.open = trouble;  // unfold on its own exactly when there is something to see
      // classList, not className: the latter would drop the u_diagwrap selector class.
      wrap.classList.toggle("warnnote", trouble);
      wrap.classList.toggle("help", !trouble);
    }
  } catch (e) { msg.textContent = "✗ " + e.message; wrap.hidden = true; }
}

// "3 polls" says nothing on its own — spell out what it means in seconds at the current
// mains interval, since this value alone decides when a connection loss is notified.
function updateUnreachableHint() {
  const el = $("th_unreachable_hint");
  if (!el) return;
  const polls = Number(($("th_unreachable_alarm_after_polls") || {}).value || 0);
  const every = Number(($("th_poll_interval_normal_s") || {}).value || 0);
  el.textContent = polls > 0 && every > 0
    ? t("th.unreachableHint", { polls, every, total: polls * every })
    : "";
}

// The cluster thresholds only mean anything once a host is marked as a cluster member —
// showing them otherwise would suggest the appliance does something it does not.
function syncClusterThresholds() {
  const block = $("clusterThresholds");
  if (!block) return;
  block.hidden = !document.querySelector("#hostRows .h_cluster:checked");
  const box = $("applianceBox");
  // Only the guest stop (on its own, or as part of the Ceph option) stops guests, and only
  // then does it matter which guest we are.
  if (box) box.hidden = !guestStopRow();
  updateClusterBudgetHint();
}

// First host card on which the cluster-wide guest stop is in effect: ticked by itself, or
// included by the Ceph option. Only cards that are cluster members count.
function guestStopRow() {
  return [...document.querySelectorAll("#hostRows .host-cfg")].find((el) => {
    const on = (cls) => { const c = el.querySelector(cls); return !!(c && c.checked); };
    return on(".h_cluster") && (on(".h_cluster_ceph") || on(".h_cluster_guest_stop"));
  }) || null;
}

// --- the appliance's own guest ----------------------------------------------
// Picked from a list, never typed: a wrong vmid means the appliance shuts ITSELF down in
// the middle of an outage. applianceGuests holds the last list the API returned.
let applianceGuests = [];
// What the config actually holds. The list needs a reachable cluster, so the settings
// page routinely opens without one — and then the node behind the stored vmid is only
// known from here. Without this fallback, opening settings and pressing Save would write
// an empty self_node back and silently unpick the node that has to shut down last.
let applianceStored = { vmid: null, node: "", external: false };

function applianceChoice() {
  const sel = $("ap_self_pick");
  if (!sel) return { ...applianceStored };
  if (sel.value === "external") return { vmid: null, node: "", external: true };
  if (!sel.value) return { vmid: null, node: "", external: false };
  const vmid = Number(sel.value);
  const g = applianceGuests.find((x) => x.vmid === vmid);
  const node = g ? g.node : (applianceStored.vmid === vmid ? applianceStored.node : "");
  return { vmid, node, external: false };
}

// The node carrying the appliance is exactly the one that has to be shut down last, so
// the "this host" tick is derived from the pick rather than maintained twice. Without a
// pick (standalone PVE, PBS, Docker, bare metal) it stays a manual switch.
function syncThisHostFromAppliance(el) {
  const chk = el.querySelector(".h_this");
  const note = el.querySelector(".h_thisderived");
  const { node, external } = applianceChoice();
  if (!node || external) {
    chk.disabled = false;
    note.hidden = true;
    return;
  }
  const mine = el.querySelector(".h_name").value.trim() === node;
  chk.checked = mine;
  chk.disabled = true;
  note.hidden = !mine;
}

function renderApplianceGuests(sel, guests, chosen) {
  applianceGuests = guests || [];
  sel.innerHTML = "";
  const add = (value, label) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
  };
  add("", t("appl.pickNone"));
  applianceGuests.forEach((g) =>
    add(String(g.vmid), `${g.vmid} — ${g.name || "?"} (${g.type === "lxc" ? "CT" : "VM"}, ${g.node})`));
  add("external", t("appl.pickExternal"));
  // A previously stored vmid that is no longer in the list must NOT silently fall back to
  // "none": that reads as "nothing configured" when the truth is "the selection is gone".
  if (chosen.external) sel.value = "external";
  else if (chosen.vmid != null) {
    if (!applianceGuests.some((g) => g.vmid === chosen.vmid))
      add(String(chosen.vmid), t("appl.pickMissing", { vmid: chosen.vmid }));
    sel.value = String(chosen.vmid);
  } else sel.value = "";
}

async function loadApplianceGuests() {
  const state = $("ap_self_state");
  const row = guestStopRow();
  if (!row) { state.textContent = t("appl.needHost"); return; }
  state.textContent = t("appl.loading");
  try {
    const r = await api("/api/cluster/guests", "POST", hostFromRow(row));
    if (!r.ok) { state.textContent = r.message || t("appl.failed"); return; }
    renderApplianceGuests($("ap_self_pick"), r.guests, applianceChoice());
    const det = r.detected || {};
    state.textContent = det.label
      ? t("appl.detected", { guest: det.label })
      : r.message;
    onApplianceChange();
  } catch (e) {
    state.textContent = String(e.message || e);
  }
}

function onApplianceChange() {
  document.querySelectorAll("#hostRows .host-cfg").forEach((el) => {
    syncThisHostFromAppliance(el);
    if (el._updSum) el._updSum();
  });
  renderShutdownSequence();
}

// The whole sequence now holds the battery for the disarm, the guests and the nodes.
// Shown as one number because that is what has to fit inside the trigger. The guest term
// only where the guest stop is on, exactly like engine.shutdown_budget() counts it.
function updateClusterBudgetHint() {
  const el = $("th_cluster_total_hint");
  if (!el) return;
  const prep = getNum("th_cluster_prep_timeout_s") || 60;
  const guests = guestStopRow() ? (getNum("th_cluster_guest_shutdown_timeout_s") || 300) : 0;
  const nodes = getNum("th_host_shutdown_timeout_s") || 60;
  el.textContent = t("th.clusterTotalHint", { total: prep + guests + nodes });
}

async function loadConfig() {
  currentConfig = await api("/api/config");
  const c = currentConfig;
  setChk("s_dry_run", c.dry_run);

  renderUps(c.ups || []);
  renderHosts(c.hosts || []);
  renderHostUpsCheckboxes();

  // Named th, not t: t() is the i18n lookup, and a const named t here shadows it
  // for the whole function body (temporal dead zone, so above this line too).
  const th = c.thresholds;
  setVal("th_on_battery_seconds", th.on_battery_seconds);
  setVal("th_runtime_below_minutes", th.runtime_below_minutes);
  setVal("th_charge_below_percent", th.charge_below_percent);
  setChk("th_on_battery_low", th.on_battery_low);
  setVal("th_poll_interval_normal_s", th.poll_interval_normal_s);
  setVal("th_poll_interval_battery_s", th.poll_interval_battery_s);
  setVal("th_host_shutdown_timeout_s", th.host_shutdown_timeout_s);
  setVal("th_unreachable_alarm_after_polls", th.unreachable_alarm_after_polls);
  setVal("th_comm_loss_shutdown_after_min", th.comm_loss_shutdown_after_min);
  setVal("th_rearm_after_mains_min", th.rearm_after_mains_min);
  setChk("th_keep_shutdown_on_comm_loss", th.keep_shutdown_on_comm_loss);
  setVal("th_cluster_prep_timeout_s", th.cluster_prep_timeout_s);
  setChk("th_cluster_abort_on_prep_failure", th.cluster_abort_on_prep_failure);
  setVal("th_cluster_guest_shutdown_timeout_s", th.cluster_guest_shutdown_timeout_s);
  setVal("th_cluster_guest_force_after_s", th.cluster_guest_force_after_s);
  const ap = c.appliance || {};
  applianceStored = { vmid: ap.self_vmid ?? null, node: ap.self_node || "",
                      external: !!ap.self_external };
  // Rendered from the stored value alone: the list needs a reachable cluster, and a
  // settings page that cannot open without one would be worse than a single entry saying
  // what is currently selected. "Reload" fetches the rest.
  renderApplianceGuests($("ap_self_pick"), applianceGuests, applianceStored);
  updateUnreachableHint();
  updateClusterBudgetHint();
  syncClusterThresholds();
  onApplianceChange();

  setVal("ntp_server", c.ntp_server);
  setVal("tz_timezone", c.timezone);
  setChk("selftest_enabled", c.selftest_enabled);
  setVal("selftest_hour", c.selftest_hour);
  setVal("selftest_interval_min", c.selftest_interval_min);
  // A config written before this field existed has no value for it; the backend default is
  // "on", so treat an absent one as on rather than letting the checkbox render unticked and
  // then save that back as an opt-out the user never made.
  setChk("selftest_log_ok", c.selftest_log_ok !== false);
  const hc = c.history || {};
  setChk("hist_enabled", hc.enabled !== false);
  setVal("hist_retention_days", hc.retention_days || 90);
  refreshHistoryInfo();

  renderWebhooks(c.notifications.webhooks || []);

  drawConfigTopology();
  refreshUpdateStatus();
}

function renderHosts(hosts) {
  const container = $("hostRows");
  container.innerHTML = "";
  hosts.forEach((h) => addHostRow(h, false, false));  // loaded cards start collapsed
  if (hosts.length === 0) addHostRow({}, true, true);  // first-run card stays open
}

function addHostRow(h, isNew, open) {
  h = h || {};
  const el = document.createElement("details");
  el.className = "host-cfg";
  if (open !== false) el.open = true;
  const secretSet = h.token_secret === SECRET_PLACEHOLDER;
  // Remember the desired feeds so renderHostUpsCheckboxes() can preselect them.
  el.dataset.feeds = h.ups_ids ? JSON.stringify(h.ups_ids) : (isNew ? "ALL" : "[]");
  const type = h.type || "pve";
  // Stable identity: what the backend matches the stored token secret on, and what the
  // engine latches a shutdown against. A new card gets one here so it survives the first
  // save; an existing one keeps whatever the backend assigned.
  const hostId = h.id || nextHostId();
  // Note on the type="password" field below (and on its four siblings — the two SNMPv3
  // passphrases, the NUT password and the webhook auth value): every one of them carries
  // autocomplete="new-password". Giving the login form a username field so Bitwarden can
  // recognise it (see index.html) also makes every other password box on the site a
  // candidate, and the UI password silently pasted over an API token would be discovered
  // during an outage. "new-password" is the value managers actually honour here; "off" is
  // widely ignored.
  el.innerHTML = `
    <summary class="cfg-head">${svgIcon("i-server")}<span class="cfg-title h_sum_name"></span><span class="cfg-sub h_sum_meta"></span></summary>
    <input type="hidden" class="h_id" value="${esc(hostId)}" />
    <div class="row">
      <label title="${esc(t("host.typeTitle"))}">${esc(t("host.type"))} <select class="h_type">${opts(HOST_TYPES, type)}</select></label>
      <label class="h_namelbl" title=""><span class="h_namecap"></span> <input class="h_name" value="${esc(h.name || "")}" /></label>
      <label title="${esc(t("host.apiurlTitle"))}">${esc(t("host.apiurl"))} <input class="h_url" value="${esc(h.api_url || "")}" /></label>
    </div>
    <div class="row">
      <label title="${esc(t("host.tokenIdTitle"))}">${esc(t("host.tokenId"))} <input class="h_token_id" value="${esc(h.token_id || "")}" /></label>
      <label title="${esc(t("host.tokenSecretTitle"))}">${esc(t("host.tokenSecret"))} <input class="h_token_secret" type="password" autocomplete="new-password" placeholder="${esc(secretSet ? t("cfg.unchanged") : t("host.tokenSecretPh"))}" /></label>
    </div>
    <p class="warnnote h_dupurl" hidden>${esc(t("host.dupUrl"))}</p>
    <p class="help h_hint"><span class="h_hinttext"></span> <a class="h_hintdoc" data-manual="token-pve" target="_blank" rel="noopener"></a></p>
    <div class="feedsblock" title="${esc(t("host.feedsTitle"))}">
      <span class="cfg-label">${esc(t("host.feeds"))}</span>
      <div class="h_feeds"></div>
      <p class="help h_feedsall" hidden>${esc(t("host.feedsAllHint"))}</p>
    </div>
    <div class="row hostflags">
      <label title="${esc(t("host.policyTitle"))}">${esc(t("host.policy"))} <select class="h_policy"><option value="all">${esc(t("hosts.policyAnd"))}</option><option value="any">${esc(t("hosts.policyOr"))}</option></select></label>
      <label class="h_orderlbl" title="${esc(t("host.orderTitle"))}">${esc(t("host.order"))} <input class="h_order" type="number" value="${h.order || 0}" /></label>
      <label class="chkline" title="${esc(t("host.verifyTitle"))}"><input class="h_verify" type="checkbox" ${h.verify_tls ? "checked" : ""} /> ${esc(t("host.verify"))}</label>
      <label class="chkline h_thislbl" title="${esc(t("host.thisTitle"))}"><input class="h_this" type="checkbox" ${h.this_host ? "checked" : ""} /> ${esc(t("host.this"))}</label>
      <span class="help h_thisderived" hidden>${esc(t("host.thisHostDerived"))}</span>
      <label class="chkline" title="${esc(t("host.enabledTitle"))}"><input class="h_enabled" type="checkbox" ${h.enabled !== false ? "checked" : ""} /> ${esc(t("host.enabled"))}</label>
    </div>
    <!-- The cluster switches as one set-apart group, never a collapsible one:
         hiding the master switch behind a fold made the whole feature hard to find, while
         leaving it loose among the other per-host flags gave its sub-options nothing
         to belong to. The group is always visible (on PVE); only the sub-options follow
         the tick.
         The two sub-switches are listed in the order the steps actually run — HA disarm
         first, then the Ceph part — because that order is load-bearing (disarming after
         the guests would let the HA manager restart them) and a list that contradicts it
         teaches the wrong mental model. -->
    <div class="clusterbox">
      <span class="cfg-label">${esc(t("host.clusterBox"))} <span class="chip beta">${esc(t("common.beta"))}</span></span>
      <label class="chkline h_clusterlbl" title="${esc(t("host.clusterTitle"))}"><input class="h_cluster" type="checkbox" ${h.cluster ? "checked" : ""} /> ${esc(t("host.cluster"))}</label>
      <div class="h_clusteropts">
        <p class="warnnote"><svg class="icon"><use href="#i-alert"></use></svg>
          <span>${esc(t("host.clusterNeeds92"))}</span></p>
        <!-- Defaults to ON (see PveHostConfig.cluster_shutdown_all), hence "!== false".
             It keeps the shutdown in step with the preparation, which is cluster-wide
             either way — see the comment on the config field. -->
        <label class="chkline" title="${esc(t("host.clusterShutdownAllTitle"))}"><input class="h_cluster_shutdown_all" type="checkbox" ${h.cluster_shutdown_all !== false ? "checked" : ""} /> ${esc(t("host.clusterShutdownAll"))}</label>
        <p class="help h_unitnote">${esc(t("host.clusterShutdownAllHelp"))}</p>
        <p class="warnnote h_unitwarn" hidden><svg class="icon"><use href="#i-alert"></use></svg>
          <span>${esc(t("host.clusterShutdownAllWarn"))}</span></p>
        <label class="chkline" title="${esc(t("host.clusterHaTitle"))}"><input class="h_cluster_ha_disarm" type="checkbox" ${h.cluster_ha_disarm !== false ? "checked" : ""} /> ${esc(t("host.clusterHa"))}</label>
        <!-- The cluster-wide guest stop (#43): runs after the HA disarm and before the
             Ceph flags, hence its place between the two. Defaults to OFF (see
             PveHostConfig.cluster_guest_stop). With Ceph ticked it is shown ticked and
             locked — the Ceph option includes it — while the card's own value is kept in
             data-own, so unticking Ceph brings back what was set before. -->
        <label class="chkline" title="${esc(t("host.clusterGuestsTitle"))}"><input class="h_cluster_guest_stop" type="checkbox" ${h.cluster_guest_stop ? "checked" : ""} data-own="${h.cluster_guest_stop ? "1" : ""}" /> ${esc(t("host.clusterGuests"))}</label>
        <p class="help h_guestnote" hidden>${esc(t("host.clusterGuestsHelp"))}
          <a data-manual="cluster-guests" target="_blank" rel="noopener">${esc(t("host.clusterGuestsDoc"))}</a></p>
        <p class="help h_guestbyceph" hidden>${esc(t("host.clusterGuestsByCeph"))}</p>
        <p class="help">${esc(t("host.clusterPrivHint"))}
          <a data-manual="cluster" target="_blank" rel="noopener">${esc(t("host.clusterPrivDoc"))}</a></p>
        <p class="help">${esc(t("host.clusterHelp"))}</p>
        <!-- The Ceph part is set apart because it is a different order of magnitude: it
             stops every guest in the cluster, not just HA. The manuals split at exactly
             this line too (#cluster vs #cluster-ceph). -->
        <hr class="clustersep"/>
        <span class="cfg-label">${esc(t("host.clusterCephGroup"))}</span>
        <!-- Ceph defaults to OFF (see PveHostConfig.cluster_ceph), so plain truthiness —
             "!== false" would tick it on every newly added card. This one switch covers
             the whole hyper-converged procedure: stopping every guest first, then the
             flags. It switches the guest stop above on by itself — with Ceph it is not
             optional, and a tick whose absence hangs the cluster is a trap. -->
        <label class="chkline" title="${esc(t("host.clusterCephTitle"))}"><input class="h_cluster_ceph" type="checkbox" ${h.cluster_ceph ? "checked" : ""} /> ${esc(t("host.clusterCeph"))}</label>
        <p class="help h_cephnote">${esc(t("host.clusterCephHelp"))}
          <a data-manual="cluster-ceph" target="_blank" rel="noopener">${esc(t("host.clusterCephDoc"))}</a></p>
        <p class="help">${esc(t("host.clusterBetaNote"))}
          <a href="https://github.com/ffind-dev/pve-ups/issues" target="_blank" rel="noopener">${esc(t("host.clusterBetaLink"))}</a></p>
      </div>
    </div>
    <div class="row" style="margin:0;align-items:center">
      <button class="btn-ghost btn-sm h_test" style="flex:0 0 auto">${esc(t("host.test"))}</button>
      <button class="btn-ghost btn-sm h_del" style="flex:0 0 auto">${esc(t("host.remove"))}</button>
      <span class="muted h_msg"></span>
    </div>
    <details class="help h_diagwrap" hidden style="margin-top:.5rem">
      <summary>${esc(t("cprobe.diag"))}</summary>
      <pre class="h_diag" style="white-space:pre-wrap;overflow:auto;max-height:16rem;font-family:monospace;font-size:.85em"></pre>
    </details>`;
  el.querySelector(".h_policy").value = h.ups_policy || "all";
  const updSum = () => {
    const nm = el.querySelector(".h_name").value.trim() || t("host.newName");
    const isThis = el.querySelector(".h_this").checked;
    const en = el.querySelector(".h_enabled").checked;
    const ty = el.querySelector(".h_type").value;
    el.querySelector(".h_sum_name").textContent = nm + (isThis ? " ★" : "");
    // Cluster membership belongs on the collapsed card for the same reason the ★ does:
    // it changes what happens at shutdown and should not need unfolding to be seen. The
    // discovered name is used when the engine has one, the plain word until then.
    const inCluster = ty !== "pbs" && el.querySelector(".h_cluster").checked;
    const cname = inCluster ? (CLUSTER_NAMES[el.querySelector(".h_id").value] || "") : "";
    el.querySelector(".h_sum_meta").textContent =
      "· " + hostTypeLabel(ty)
      + (inCluster ? " · " + (cname || t("host.sumCluster")) : "")
      + (en ? "" : " " + t("host.inactive"));
  };
  // Kept on the element so a later status refresh can redraw every summary: the cluster
  // name arrives from the API, not from the form.
  el._updSum = updSum;
  // Everything that reads differently per product: the name field is a real node name on
  // PVE but only a label on PBS, and each has its own port, token realm and manual anchor.
  const toggleHostType = () => {
    const ty = el.querySelector(".h_type").value;
    const pbs = ty === "pbs";
    const lbl = el.querySelector(".h_namelbl");
    lbl.title = t(pbs ? "host.nameTitle" : "host.nodeTitle");
    el.querySelector(".h_namecap").textContent = t(pbs ? "host.name" : "host.node");
    el.querySelector(".h_name").placeholder = pbs ? "Backup-Server" : "pve01";
    el.querySelector(".h_url").placeholder = pbs ? "https://10.0.0.20:8007" : "https://10.0.0.10:8006";
    el.querySelector(".h_token_id").placeholder = pbs ? "ups@pbs!shutdown" : "ups@pve!shutdown";
    el.querySelector(".h_hinttext").textContent = t("htype." + ty + "Help");
    const doc = el.querySelector(".h_hintdoc");
    doc.textContent = t("host.hintDoc");
    doc.dataset.manual = pbs ? "token-pbs" : "token-pve";
    applyTranslations(el);  // rewrites the doc link to the current language's manual
    syncFlags();
  };
  // Which of the flags can apply at all. Order matters here: the "this host" rule may
  // clear the tick, and the order field has to come back in the same pass.
  const syncFlags = () => {
    const pbs = el.querySelector(".h_type").value === "pbs";
    // An LXC never runs on a Backup Server, so "this host" cannot apply there. In a
    // Docker deployment the container may well sit on the PBS — and then the mark
    // matters, or the appliance kills itself midway through the sequence.
    const impossible = pbs && deployment !== "docker";
    el.querySelector(".h_thislbl").hidden = impossible;
    // Clear it as well: hostFromRow() still reads the checkbox, and a hidden tick
    // would keep being submitted.
    if (impossible) el.querySelector(".h_this").checked = false;
    // "This host" is the first sort key, so it beats the number outright — a marked
    // host is last whatever it says. Hide the field rather than let it suggest an
    // effect it does not have. The value is kept, so unticking restores it.
    el.querySelector(".h_orderlbl").hidden = el.querySelector(".h_this").checked;
    // A Backup Server is never part of a PVE cluster, so the whole group is PVE-only.
    // Unlike "this host" the values are NOT cleared: switching a card back to PVE should
    // find the cluster settings as they were.
    el.querySelector(".clusterbox").hidden = pbs;
    // The sub-switches only mean anything once the host is marked as a cluster member.
    el.querySelector(".h_clusteropts").hidden = !el.querySelector(".h_cluster").checked;
    // The Ceph note describes what the tick does; showing it while the tick is off would
    // read as a description of the cluster rather than of the option.
    const ceph = el.querySelector(".h_cluster_ceph").checked;
    el.querySelector(".h_cephnote").hidden = !ceph;
    // The Ceph option includes the guest stop: show it ticked and locked, but keep the
    // card's own value for the moment Ceph is unticked again.
    const gs = el.querySelector(".h_cluster_guest_stop");
    if (ceph && !gs.disabled) {
      gs.dataset.own = gs.checked ? "1" : "";
      gs.checked = true;
      gs.disabled = true;
    } else if (!ceph && gs.disabled) {
      gs.checked = gs.dataset.own === "1";
      gs.disabled = false;
    }
    el.querySelector(".h_guestbyceph").hidden = !ceph;
    el.querySelector(".h_guestnote").hidden = ceph || !gs.checked;
    // Only a problem when the two halves can disagree: the preparation is cluster-wide,
    // so a partial shutdown is what leaves nodes stranded. Worth shouting about once the
    // guests are stopped cluster-wide, which then happens for nothing on the survivors.
    el.querySelector(".h_unitwarn").hidden =
      el.querySelector(".h_cluster_shutdown_all").checked || !gs.checked;
    syncThisHostFromAppliance(el);
    updSum();
  };
  el.querySelector(".h_del").onclick = () => {
    el.remove();
    drawConfigTopology();
    syncDuplicateUrls();
  };
  el.querySelector(".h_test").onclick = () => testHost(el);
  el.querySelector(".h_name").oninput = () => { updSum(); drawConfigTopology(); };
  el.querySelector(".h_url").oninput = syncDuplicateUrls;
  el.querySelector(".h_this").onchange = () => { syncFlags(); drawConfigTopology(); };
  el.querySelector(".h_cluster").onchange = () => { syncFlags(); syncClusterThresholds(); };
  el.querySelector(".h_cluster_ceph").onchange = () => { syncFlags(); syncClusterThresholds(); };
  el.querySelector(".h_cluster_guest_stop").onchange = () => {
    syncFlags();
    syncClusterThresholds();
  };
  el.querySelector(".h_cluster_shutdown_all").onchange = syncFlags;
  // Order and "active" do not touch the diagram, but they do change the shutdown
  // sequence shown below it.
  el.querySelector(".h_order").oninput = renderShutdownSequence;
  el.querySelector(".h_enabled").onchange = () => {
    updSum();
    drawConfigTopology();
    syncDuplicateUrls();
  };
  el.querySelector(".h_type").onchange = () => {
    // Carry the URL over to the new type's default port, unless the user typed their own.
    const ty = el.querySelector(".h_type").value;
    const urlEl = el.querySelector(".h_url");
    const known = Object.values(HOST_DEFAULT_PORTS).map(String);
    urlEl.value = urlEl.value.replace(/:(\d+)\/?$/, (m, port) =>
      known.includes(port) ? ":" + HOST_DEFAULT_PORTS[ty] : m);
    toggleHostType();
    drawConfigTopology();
    syncDuplicateUrls();
  };
  $("hostRows").appendChild(el);
  toggleHostType();
  syncDuplicateUrls();
}
$("addHostBtn").onclick = () => { addHostRow({}, true, true); renderHostUpsCheckboxes(); drawConfigTopology(); };

// Two enabled entries behind one API URL are almost always a copy-paste slip (row
// duplicated, IP not adjusted) — and the one case where the shutdown still has to be
// addressed by the configured node name, because PVE's proxying is then the only thing
// telling the entries apart. Flagged while editing, and again by the self-test for configs
// that arrive by backup import and never pass through this form.
function syncDuplicateUrls() {
  const rows = Array.from(document.querySelectorAll("#hostRows .host-cfg"));
  const count = {};
  rows.forEach((tr) => {
    if (!tr.querySelector(".h_enabled").checked) return;
    const url = tr.querySelector(".h_url").value.trim().replace(/\/+$/, "").toLowerCase();
    if (url) count[url] = (count[url] || 0) + 1;
  });
  rows.forEach((tr) => {
    const url = tr.querySelector(".h_url").value.trim().replace(/\/+$/, "").toLowerCase();
    const dupe = tr.querySelector(".h_enabled").checked && count[url] > 1;
    tr.querySelector(".h_dupurl").hidden = !dupe;
  });
}

// (Re)build the per-host UPS feed checkboxes from the current UPS list, preserving selection.
function renderHostUpsCheckboxes() {
  const ups = upsMeta();
  document.querySelectorAll("#hostRows .host-cfg").forEach((tr) => {
    const cell = tr.querySelector(".h_feeds");
    if (!cell) return;
    let selected;
    const existing = cell.querySelectorAll(".h_feed");
    if (existing.length) {
      selected = new Set(Array.from(existing).filter((c) => c.checked).map((c) => c.value));
    } else if (tr.dataset.feeds === "ALL") {
      selected = new Set(ups.map((u) => u.id));
    } else {
      selected = new Set(JSON.parse(tr.dataset.feeds || "[]"));
    }
    cell.innerHTML = ups.map((u) =>
      `<label class="feedchk"><input type="checkbox" class="h_feed" value="${esc(u.id)}" ${selected.has(u.id) ? "checked" : ""}/> ${esc(u.name)}</label>`
    ).join("") || `<span class='muted'>${esc(t("host.noUps"))}</span>`;
    // Nothing ticked means "every configured UPS" on the backend (see feed_ids_for), and
    // the dashboard renders it exactly that way — while this panel showed an empty set of
    // boxes, which reads as the opposite. Say it instead of leaving it to be discovered.
    const hint = tr.querySelector(".h_feedsall");
    if (hint) {
      hint.hidden = !ups.length
        || cell.querySelectorAll(".h_feed:checked").length > 0;
    }
    cell.querySelectorAll(".h_feed").forEach((c) => {
      c.onchange = () => {
        if (hint) {
          hint.hidden = cell.querySelectorAll(".h_feed:checked").length > 0;
        }
        drawConfigTopology();
      };
    });
  });
}

function nextHostId() {
  return newCardId("host", "#hostRows .h_id", currentConfig && currentConfig.hosts);
}

function hostFromRow(tr) {
  const secret = tr.querySelector(".h_token_secret").value;
  return {
    id: tr.querySelector(".h_id").value,
    name: tr.querySelector(".h_name").value.trim(),
    type: tr.querySelector(".h_type").value,
    api_url: tr.querySelector(".h_url").value.trim(),
    method: "api_token",
    token_id: tr.querySelector(".h_token_id").value.trim(),
    token_secret: secret === "" ? SECRET_PLACEHOLDER : secret,
    verify_tls: tr.querySelector(".h_verify").checked,
    this_host: tr.querySelector(".h_this").checked,
    order: Number(tr.querySelector(".h_order").value || 0),
    enabled: tr.querySelector(".h_enabled").checked,
    ups_ids: Array.from(tr.querySelectorAll(".h_feed")).filter((c) => c.checked).map((c) => c.value),
    ups_policy: tr.querySelector(".h_policy").value,
    // Cluster preparation. Sent for PBS entries too (as plain false/defaults): the
    // backend drops unknown fields per type, and omitting them here would silently
    // revert the setting on every save.
    cluster: tr.querySelector(".h_cluster").checked,
    cluster_shutdown_all: tr.querySelector(".h_cluster_shutdown_all").checked,
    cluster_ceph: tr.querySelector(".h_cluster_ceph").checked,
    // The card's own value, not the locked display: with Ceph ticked the box shows the
    // guest stop on regardless (see syncFlags), and saving that would make it stick once
    // Ceph is unticked.
    cluster_guest_stop: (() => {
      const gs = tr.querySelector(".h_cluster_guest_stop");
      return gs.disabled ? gs.dataset.own === "1" : gs.checked;
    })(),
    cluster_ha_disarm: tr.querySelector(".h_cluster_ha_disarm").checked,
  };
}

// Mirror of renderProbe() for the cluster endpoints: one line per call, so a failing
// cluster check shows WHAT answered what instead of only a summary sentence.
function renderClusterProbe(c) {
  const lines = (c.entries || []).map((e) => {
    const st = t("cprobe.st." + e.status, { err: e.error || "" });
    // 30 fits the longest path (/cluster/ha/status/current is 27) plus a gap.
    return e.name.padEnd(30) + st + (e.value ? " = " + e.value : "");
  });
  const head = [];
  if (c.missing_privileges && c.missing_privileges.length) {
    head.push(t("cprobe.missingPrivs", { list: c.missing_privileges.join(", ") }));
  }
  return head.concat(lines).join("\n");
}

// The node name is the one field nothing else validates — it goes verbatim into
// /nodes/<name>/status, so a wrong one passes every check and fails during the outage.
// An empty field is filled in (nothing is lost), a filled-in one only ever gets an offer:
// silently rewriting a name the user typed could shut down a different machine.
function applyNodeCheck(el, msg, n) {
  if (!n || !n.readable || n.match || !n.suggestion) return;
  const input = el.querySelector(".h_name");
  const take = () => {
    input.value = n.suggestion;
    // el._updSum(), never a bare updSum(): this is a top-level function while updSum is a
    // const scoped to addHostRow. The bare call threw a ReferenceError that testHost's
    // catch then displayed *in place of* the successful test result.
    if (el._updSum) el._updSum();
    drawConfigTopology();
  };
  if (!input.value.trim()) {
    take();
    msg.append(" " + t("host.nodeFilled", { node: n.suggestion }));
    return;
  }
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = t("host.nodeSuggest", { node: n.suggestion });
  link.onclick = (ev) => { ev.preventDefault(); take(); link.remove(); };
  msg.append(" ");
  msg.append(link);
}

// The verdict itself, as the chip the dashboard already uses. The sentence is in the
// test message, but a wrong node name reads like just another clause there — and it is
// the one setting that fails only during a real outage.
function applyNodeState(msg, state) {
  if (!["wrong", "invalid", "proxied"].includes(state)) return;
  const chip = document.createElement("span");
  chip.className = "chip warn";
  chip.title = t("nodest.chipTitle");
  chip.textContent = t("nodest." + state);
  msg.append(" ");
  msg.append(chip);
}

async function testHost(el) {
  const msg = el.querySelector(".h_msg");
  const wrap = el.querySelector(".h_diagwrap");
  const pre = el.querySelector(".h_diag");
  msg.textContent = t("msg.testing");
  wrap.hidden = true;
  try {
    const r = await api("/api/test/host", "POST", hostFromRow(el));
    msg.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
    applyNodeState(msg, r.node_state);
    applyNodeCheck(el, msg, r.node_check);
    const c = r.cluster;
    if (c && (c.entries || []).length) {
      // Unfold on its own exactly when there is something to look at.
      const trouble = !r.ok || !c.reachable || !c.is_cluster || c.ha_disarmed
        || (c.missing_privileges || []).length > 0
        || c.entries.some((e) => e.status === "denied" || e.status === "error");
      pre.textContent = renderClusterProbe(c);
      wrap.hidden = false;
      wrap.open = trouble;
      // classList, not className: the latter would drop the h_diagwrap selector class.
      wrap.classList.toggle("warnnote", trouble);
      wrap.classList.toggle("help", !trouble);
    }
  } catch (e) { msg.textContent = "✗ " + e.message; wrap.hidden = true; }
}

// ===== topology diagram (UPS -> Host) ======================================
function drawTopology(svg, ups, hosts, statusMap) {
  if (!svg) return;
  const NH = 30, GAP = 16, TOP = 10, NW = 150;
  const W = svg.clientWidth || 560;
  const leftX = 6, rightX = Math.max(leftX + NW + 40, W - NW - 6);
  const rows = Math.max(ups.length, hosts.length, 1);
  const H = TOP + rows * (NH + GAP);
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const upsY = {}, hostY = [];
  ups.forEach((u, i) => { upsY[u.id] = TOP + i * (NH + GAP); });
  hosts.forEach((h, j) => { hostY[j] = TOP + j * (NH + GAP); });
  const allIds = ups.map((u) => u.id);
  const lineCls = (id) => {
    const st = statusMap && statusMap[id];
    if (!st) return "";
    if (st.triggered) return "crit";
    if (!st.reachable) return "muted";
    if (st.power_source === "battery") return "warn";
    return "ok";
  };
  let out = "";
  // connection lines first (under the nodes); data-ups/data-host correlate hover highlights
  hosts.forEach((h, j) => {
    const feeds = (h.ups_ids && h.ups_ids.length) ? h.ups_ids : allIds;
    feeds.forEach((id) => {
      if (upsY[id] === undefined) return;
      const y1 = upsY[id] + NH / 2, y2 = hostY[j] + NH / 2;
      out += `<path class="topo-line ${lineCls(id)}" data-ups="${esc(id)}" data-host="${j}" d="M${leftX + NW} ${y1} C ${(leftX + NW + rightX) / 2} ${y1}, ${(leftX + NW + rightX) / 2} ${y2}, ${rightX} ${y2}" />`;
    });
  });
  // UPS nodes (left)
  ups.forEach((u) => {
    const y = upsY[u.id], cls = statusMap ? "is-" + (lineCls(u.id) || "ok") : "";
    out += `<g class="topo-node ${cls}" data-ups="${esc(u.id)}"><rect x="${leftX}" y="${y}" width="${NW}" height="${NH}" rx="6"/>` +
      `<text x="${leftX + 10}" y="${y + NH / 2 + 4}">${esc(u.name)}</text></g>`;
  });
  // Host nodes (right)
  hosts.forEach((h, j) => {
    const y = hostY[j];
    // Name only: the nodes are NW wide and a product name in front pushes it out. The
    // type is shown where there is room for it (dashboard chip, host card heading).
    const label = esc(h.name) + (h.this_host ? " ★" : "");
    out += `<g class="topo-node host" data-host="${j}"><rect x="${rightX}" y="${y}" width="${NW}" height="${NH}" rx="6"/>` +
      `<text x="${rightX + 10}" y="${y + NH / 2 + 4}">${label}</text></g>`;
  });
  svg.innerHTML = out;
  wireTopoHover(svg);
}

// Hover a UPS or host node -> highlight its connection lines and the opposite-side
// nodes, dim everything else. Helps trace dependencies in large environments.
function cssEsc(v) { return (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/"/g, '\\"'); }

function wireTopoHover(svg) {
  const clear = () => {
    svg.classList.remove("hover-active");
    svg.querySelectorAll(".hl").forEach((e) => e.classList.remove("hl"));
  };
  svg.querySelectorAll(".topo-node").forEach((node) => {
    node.addEventListener("mouseenter", () => {
      clear();
      const ups = node.getAttribute("data-ups");
      const host = node.getAttribute("data-host");
      const related = [node];
      const lines = ups !== null
        ? svg.querySelectorAll(`.topo-line[data-ups="${cssEsc(ups)}"]`)
        : svg.querySelectorAll(`.topo-line[data-host="${cssEsc(host)}"]`);
      lines.forEach((l) => {
        related.push(l);
        const oppSel = ups !== null
          ? `.topo-node[data-host="${cssEsc(l.getAttribute("data-host"))}"]`
          : `.topo-node[data-ups="${cssEsc(l.getAttribute("data-ups"))}"]`;
        svg.querySelectorAll(oppSel).forEach((n) => related.push(n));
      });
      related.forEach((e) => e.classList.add("hl"));
      svg.classList.add("hover-active");
    });
    node.addEventListener("mouseleave", clear);
  });
}

function drawConfigTopology() {
  const hosts = Array.from(document.querySelectorAll("#hostRows .host-cfg")).map((tr) => ({
    name: tr.querySelector(".h_name").value.trim(),
    type: tr.querySelector(".h_type").value,
    this_host: tr.querySelector(".h_this").checked,
    ups_ids: Array.from(tr.querySelectorAll(".h_feed")).filter((c) => c.checked).map((c) => c.value),
  })).filter((h) => h.name);
  drawTopology($("topoDiagram"), upsMeta(), hosts, null);
  renderShutdownSequence();
}

// Preview of the order the engine would actually use, live from the form. Mirrors
// AppConfig.ordered_hosts(): sort by (this_host, order, name), then group — hosts
// sharing a stage are commanded at the same time, and "this host" forms the last
// stage on its own. Without this, neither "which number goes first" nor the staged
// behaviour is visible anywhere in the UI.
function renderShutdownSequence() {
  const el = $("shutdownSeq");
  if (!el) return;
  const hosts = Array.from(document.querySelectorAll("#hostRows .host-cfg"))
    .map((tr) => ({
      name: tr.querySelector(".h_name").value.trim(),
      order: Number(tr.querySelector(".h_order").value || 0),
      this_host: tr.querySelector(".h_this").checked,
      enabled: tr.querySelector(".h_enabled").checked,
    }))
    .filter((h) => h.name && h.enabled)
    .sort((a, b) => (a.this_host - b.this_host) || (a.order - b.order)
      || a.name.localeCompare(b.name));

  if (!hosts.length) {
    el.innerHTML = `<b>${esc(t("hosts.seq"))}</b> <span class="muted">${esc(t("hosts.seqNone"))}</span>`;
    return;
  }
  const stages = [];
  hosts.forEach((h) => {
    const prev = stages[stages.length - 1];
    const same = prev && prev[0].this_host === h.this_host && prev[0].order === h.order;
    if (same) prev.push(h); else stages.push([h]);
  });
  const chain = stages.map((stage, i) =>
    `<span class="chip muted">${i + 1}.</span> ` +
    stage.map((h) => esc(h.name) + (h.this_host ? " ★" : "")).join(" + ")
  ).join(" &rarr; ");
  el.innerHTML = `<b>${esc(t("hosts.seq"))}</b> ${chain}<br>`
    + `<span class="muted">${esc(t("hosts.seqHint"))}</span>`;
}

// ===== notifications =======================================================
// One card per webhook, mirroring the UPS cards: several targets can be configured and
// each is tested on its own.
function nextWebhookId() {
  return newCardId("webhook", "#webhookList .w_id",
                   currentConfig && (currentConfig.notifications || {}).webhooks);
}

function renderWebhooks(list) {
  $("webhookList").innerHTML = "";
  (list || []).forEach((w) => addWebhookCard(w, false));  // loaded cards start collapsed
  updateWebhookDelivery();
}

// Mark every webhook card whose last delivery failed, from the current snapshot.
//
// Called both after rendering and from refreshStatus(), so the note tracks reality while
// the settings page stays open — the same treatment the host chips on the dashboard get.
function updateWebhookDelivery() {
  const seen = (lastStatus && lastStatus.webhooks) || [];
  document.querySelectorAll("#webhookList .ups-cfg").forEach((card) => {
    const note = card.querySelector(".w_deliv");
    const idEl = card.querySelector(".w_id");
    if (!note || !idEl) return;
    const state = seen.find((x) => x.id === idEl.value);
    const failed = state && state.last_delivery_ok === false;
    note.hidden = !failed;
    note.innerHTML = failed
      ? `${svgIcon("i-alert")}<span>${esc(t("notif.lastFailed",
          { err: state.last_delivery_error || "?" }))}</span>`
      : "";
  });
}

function addWebhookCard(w, open) {
  w = w || {};
  const id = w.id || nextWebhookId();
  const div = document.createElement("details");
  div.className = "ups-cfg";
  if (open !== false) div.open = true;
  const authPh = w.auth_header_value === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "";
  // Stored as a mapping, edited as "Name: Value" lines. The textarea sends its raw text
  // back and config.parse_extra_headers() does the parsing, so there is exactly one place
  // that decides what a valid header is — and the server stays robust against a stale
  // cached app.js, like every other field here.
  const extraLines = Object.entries(w.extra_headers || {})
    .map(([k, v]) => `${k}: ${v}`).join("\n");
  // A webhook that stopped working used to do so in complete silence — the shutdown
  // credentials next door have had a self-test, a chip and an event for releases, while
  // a failed notification reached journald and nowhere else.
  //
  // Rendered empty here and filled by updateWebhookDelivery() below, which also runs on
  // every status poll: built once from lastStatus, the note could only ever show what
  // was true at the moment the settings page was opened — a target that broke while the
  // page was open stayed unmarked, and one that recovered stayed accused.
  div.innerHTML = `
    <summary class="cfg-head">${svgIcon("i-bell")}<span class="cfg-title w_sum_name"></span><span class="cfg-sub w_sum_url"></span></summary>
    <input type="hidden" class="w_id" value="${esc(id)}" />
    <p class="warnnote w_deliv" hidden></p>
    <label class="switch" title="${esc(t("notif.webhookTitle"))}"><input type="checkbox" class="w_enabled" ${w.enabled ? "checked" : ""} /><span>${esc(t("notif.webhookEnabled"))}</span></label>
    <div class="row">
      <label title="${esc(t("notif.nameTitle"))}">${esc(t("cfg.name"))} <input class="w_name" value="${esc(w.name || "")}" placeholder="${esc(id)}" /></label>
      <label title="${esc(t("notif.urlTitle"))}">${esc(t("notif.url"))} <input class="w_url" value="${esc(w.url || "")}" placeholder="https://..." /></label>
    </div>
    <div class="row">
      <label title="${esc(t("notif.formatTitle"))}">${esc(t("notif.format"))} <select class="w_format">${opts(WEBHOOK_FORMATS, w.format || "json")}</select></label>
      <label title="${esc(t("notif.minSeverityTitle"))}">${esc(t("notif.minSeverity"))} <select class="w_min_severity">${opts(WEBHOOK_LEVELS, w.min_severity || "warning")}</select></label>
    </div>
    <p class="help w_help"></p>
    <div class="w_custom" hidden>
      <label title="${esc(t("notif.contentTypeTitle"))}">${esc(t("notif.contentType"))} <input class="w_content_type" value="${esc(w.content_type || "application/json")}" /></label>
      <label title="${esc(t("notif.templateTitle"))}">${esc(t("notif.template"))}
        <textarea class="w_template" rows="5" style="font-family:monospace;font-size:.85em">${esc(w.template || "")}</textarea></label>
      <p class="help">${esc(t("notif.templateHelp"))}</p>
    </div>
    <details class="w_auth">
      <summary>${esc(t("notif.authSummary"))} <span class="muted">${esc(t("notif.authOptional"))}</span></summary>
      <div class="row">
        <label title="${esc(t("notif.authNameTitle"))}">${esc(t("notif.authName"))} <input class="w_auth_name" value="${esc(w.auth_header_name || "")}" placeholder="Authorization" /></label>
        <label title="${esc(t("notif.authValueTitle"))}">${esc(t("notif.authValue"))} <input class="w_auth_value" type="password" autocomplete="new-password" placeholder="${esc(authPh)}" /></label>
      </div>
      <label title="${esc(t("notif.extraHeadersTitle"))}">${esc(t("notif.extraHeaders"))}
        <textarea class="w_extra_headers" rows="3" placeholder="OCS-APIRequest: true" style="font-family:monospace;font-size:.85em">${esc(extraLines)}</textarea></label>
      <p class="help">${esc(t("notif.extraHeadersHelp"))}</p>
    </details>
    <div class="row" style="margin:0;align-items:center">
      <button class="btn-ghost btn-sm w_test" style="flex:0 0 auto" title="${esc(t("notif.testTitle"))}">${esc(t("notif.test"))}</button>
      <button class="btn-ghost btn-sm w_del" style="flex:0 0 auto">${esc(t("notif.removeWebhook"))}</button>
      <span class="muted w_msg"></span>
    </div>`;
  const showHelp = () => {
    // Dynamic key (like t("mib." + …) above); tests/test_i18n.py keeps whfmt.*Help complete.
    const fmt = div.querySelector(".w_format").value;
    div.querySelector(".w_help").textContent = t("whfmt." + fmt + "Help");
    div.querySelector(".w_custom").hidden = fmt !== "custom";
  };
  const updSum = () => {
    const nm = div.querySelector(".w_name").value.trim() || id;
    const url = div.querySelector(".w_url").value.trim();
    const on = div.querySelector(".w_enabled").checked;
    div.querySelector(".w_sum_name").textContent = nm + (on ? "" : " (" + t("notif.disabled") + ")");
    div.querySelector(".w_sum_url").textContent = url ? "· " + url.slice(0, 60) : "";
  };
  div.querySelector(".w_format").onchange = showHelp;
  div.querySelector(".w_name").oninput = updSum;
  div.querySelector(".w_url").oninput = updSum;
  div.querySelector(".w_enabled").onchange = updSum;
  div.querySelector(".w_test").onclick = (e) => { e.preventDefault(); testWebhook(div); };
  div.querySelector(".w_del").onclick = (e) => { e.preventDefault(); div.remove(); };
  $("webhookList").appendChild(div);
  showHelp();
  updSum();
}

function webhookFromCard(div) {
  const q = (s) => div.querySelector(s);
  const av = q(".w_auth_value").value;
  return {
    id: q(".w_id").value,
    name: q(".w_name").value.trim(),
    enabled: q(".w_enabled").checked,
    url: q(".w_url").value.trim(),
    format: q(".w_format").value,
    min_severity: q(".w_min_severity").value,
    template: q(".w_template").value,
    content_type: q(".w_content_type").value.trim() || "application/json",
    auth_header_name: q(".w_auth_name").value.trim(),
    // Empty means "unchanged" — same convention as every other secret in this UI.
    auth_header_value: av === "" ? SECRET_PLACEHOLDER : av,
    // Raw text on purpose (see addWebhookCard): the server parses it.
    extra_headers: q(".w_extra_headers").value,
  };
}

// A card survives on a URL *or* a name. Requiring the URL made a disabled, half-prepared
// entry impossible to save at all (#35): the card was dropped here, so incompleteCards()
// had to refuse the save to stop it vanishing in silence, and the way out was to invent a
// URL for a webhook that was switched off anyway. A name is enough to mean "keep this" —
// notify() sends nothing without a URL (see notify.py: `h.enabled and h.url`), so an
// unfinished draft costs nothing. With neither, the card carries no information at all and
// is still discarded.
function currentWebhookList() {
  return Array.from(document.querySelectorAll("#webhookList .ups-cfg"))
    .map(webhookFromCard).filter((w) => w.url || w.name);
}

async function testWebhook(div) {
  const msg = div.querySelector(".w_msg");
  msg.textContent = t("msg.testing");
  try {
    // Sends with the values entered here, saved or not — and regardless of the level
    // filter, because the user asked for this one explicitly.
    const r = await api("/api/test/webhook", "POST", webhookFromCard(div));
    msg.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
  } catch (e) { msg.textContent = "✗ " + e.message; }
}

$("addWebhookBtn").onclick = (e) => { e.preventDefault(); addWebhookCard({}, true); };

// A "?" inside a <summary> would fold the card on click — the link alone must win.
document.querySelectorAll("details > summary .doclink")
  .forEach((a) => { a.onclick = (e) => e.stopPropagation(); });

function buildConfig() {
  const hosts = Array.from(document.querySelectorAll("#hostRows .host-cfg"))
    .map(hostFromRow).filter((h) => h.name);
  return {
    dry_run: getChk("s_dry_run"),
    configured: true,
    ntp_server: getVal("ntp_server").trim(),
    timezone: getVal("tz_timezone").trim(),
    selftest_enabled: getChk("selftest_enabled"),
    selftest_hour: getNum("selftest_hour") ?? 9,
    // Must be sent: the server rebuilds the config from this payload alone, so a missing
    // field would silently fall back to its default on every save.
    selftest_interval_min: getNum("selftest_interval_min") ?? 1440,
    selftest_log_ok: getChk("selftest_log_ok"),
    ups: currentUpsList(),
    hosts,
    thresholds: {
      on_battery_seconds: getNum("th_on_battery_seconds"),
      runtime_below_minutes: getNum("th_runtime_below_minutes"),
      charge_below_percent: getNum("th_charge_below_percent"),
      on_battery_low: getChk("th_on_battery_low"),
      poll_interval_normal_s: getNum("th_poll_interval_normal_s") || 30,
      poll_interval_battery_s: getNum("th_poll_interval_battery_s") || 8,
      unreachable_alarm_after_polls:
        getNum("th_unreachable_alarm_after_polls")
        || currentConfig.thresholds.unreachable_alarm_after_polls,
      host_shutdown_timeout_s: getNum("th_host_shutdown_timeout_s") || 60,
      comm_loss_shutdown_after_min: getNum("th_comm_loss_shutdown_after_min"),
      // An empty field means "never" (manual reset only) — null, which is what the
      // backend reads as off. 0 is a valid value: re-arm on the first mains poll.
      rearm_after_mains_min: getNum("th_rearm_after_mains_min"),
      keep_shutdown_on_comm_loss: getChk("th_keep_shutdown_on_comm_loss"),
      cluster_prep_timeout_s: getNum("th_cluster_prep_timeout_s") || 60,
      cluster_abort_on_prep_failure: getChk("th_cluster_abort_on_prep_failure"),
      cluster_guest_shutdown_timeout_s:
        getNum("th_cluster_guest_shutdown_timeout_s") || 300,
      // Empty means "never force" — null, which is what the backend reads as off.
      cluster_guest_force_after_s: getNum("th_cluster_guest_force_after_s"),
    },
    appliance: {
      self_vmid: applianceChoice().vmid,
      self_node: applianceChoice().node,
      self_external: applianceChoice().external,
    },
    notifications: { webhooks: currentWebhookList() },
    history: {
      enabled: getChk("hist_enabled"),
      retention_days: getNum("hist_retention_days") || 90,
    },
  };
}

// Size and age of the stored history, next to its switch. Informational only: a failed
// read leaves the line empty rather than blocking the settings page.
async function refreshHistoryInfo() {
  const el = $("hist_size");
  if (!el) return;
  try {
    const i = await api("/api/history/info");
    if (i.size_bytes == null) { el.textContent = ""; return; }
    const mb = i.size_bytes / 1048576;
    el.textContent = t("sys.historySize", {
      size: mb >= 10 ? mb.toFixed(0) + " MB" : mb >= 0.1 ? mb.toFixed(1) + " MB" : Math.ceil(i.size_bytes / 1024) + " KB",
      since: i.oldest ? new Date(i.oldest * 1000).toLocaleDateString() : "–",
    });
  } catch (_) { el.textContent = ""; }
}

$("hist_clear").onclick = async () => {
  if (!confirm(t("confirm.clearHistory"))) return;
  try {
    await api("/api/history", "DELETE");
  } catch (e) { $("hist_size").textContent = "✗ " + e.message; return; }
  refreshHistoryInfo();
};

// Cards that would be saved in a state that cannot work, with the reason to show for each.
//
// Two kinds. The first is what buildConfig() would silently DROP: its three filters
// discard anything missing its one identifying field, and they used to do it in silence —
// fill in a host's URL, token id and token secret, forget the node name, press Save,
// "Saved ✓", and the card is gone (secret included) as soon as loadConfig() re-renders.
//
// The second is what it would happily KEEP although the shutdown can never reach it: a
// host without an API URL or without a token id is stored, looks complete on the
// dashboard, and only fails — at the next self-test if one runs, otherwise during the
// outage. That is the whole failure mode this appliance exists to prevent, so it is
// refused at the one moment the operator is looking at the field.
//
// The SECOND kind applies to enabled cards only. A disabled entry shuts nothing down —
// ordered_hosts() skips it — so none of its fields can fail during an outage, and
// demanding them would leave an installation upgrading from 4.0.0 (which stored such
// entries quite happily) unable to save anything at all until it had filled in a host it
// deliberately switched off. The FIRST kind still applies to every card: a nameless one
// is discarded on save whether it is enabled or not, and losing a stored API token is
// not something a checkbox should license.
function incompleteCards() {
  const bad = [];
  // Ids already on disk. An empty secret field means "unchanged" for those, so only a card
  // the backend has never seen can be judged on it — see hostFromRow(), which sends the
  // placeholder for an empty field, and main._reconcile_secret(), which resolves that to
  // the stored value or, for an unknown id, to "".
  const stored = new Set((currentConfig.hosts || []).map((h) => h.id).filter(Boolean));
  document.querySelectorAll("#hostRows .host-cfg").forEach((el, i) => {
    const isNew = !stored.has(el.querySelector(".h_id").value);
    const name = el.querySelector(".h_name").value.trim();
    // What to call the card in the message. With eight of them "a host entry" is not an
    // answer to "which one?" — the highlight scrolls to it, but the sentence should stand
    // on its own in the save bar too.
    const who = name || t("save.cardNo", { n: i + 1 });
    if (!name) {
      bad.push([el, t("save.needHostName", { who })]);
      return;
    }
    if (!el.querySelector(".h_enabled").checked) return;
    if (!el.querySelector(".h_url").value.trim()) {
      bad.push([el, t("save.needHostUrl", { who })]);
    } else if (!el.querySelector(".h_token_id").value.trim()) {
      bad.push([el, t("save.needHostToken", { who })]);
    } else if (isNew && !el.querySelector(".h_token_secret").value) {
      // The same class as the two above, and the last one still open: a new host saved
      // without a secret is stored with an empty one, looks complete on the dashboard and
      // answers 401 — during the outage, because the node check reads that as "could not
      // verify" and the self-test may be a day away.
      bad.push([el, t("save.needHostSecret", { who })]);
    }
  });
  document.querySelectorAll("#upsList .ups-cfg").forEach((el, i) => {
    const u = upsFromCard(el);
    const who = u.name || u.host || t("save.cardNo", { n: i + 1 });
    // First kind: currentUpsList() drops a card with neither, so it would vanish on save.
    if (!u.host && !u.name) {
      bad.push([el, t("save.needUps", { who })]);
      return;
    }
    // Second kind, the same class as the host fields above: what the card is missing here
    // is not cosmetic but what makes it pollable at all. UpsBase.configured stays false
    // without it, poll() answers "not configured", and the device is then permanently
    // unreachable — which is an alarm AND, being fail safe, a standing refusal to shut
    // down every host this UPS feeds. Silent until an outage, exactly like the host ones.
    if (!u.host) {
      bad.push([el, t("save.needUpsHost", { who })]);
    } else if (u.type === "nut" && !u.ups_name) {
      bad.push([el, t("save.needUpsName", { who })]);
    }
  });
  // Enabled cards only, like the second kind in the host block above. A webhook that is
  // switched on but has nowhere to post is the familiar shape: it looks configured, sends
  // nothing, and says so only once an event should have gone out. A DISABLED one is a
  // draft — a card named while the chat system's URL is still being fetched — and
  // currentWebhookList() now keeps it (it survives on its name alone), so there is nothing
  // to lose and no reason to stand between the operator and the save button.
  document.querySelectorAll("#webhookList .ups-cfg").forEach((el, i) => {
    if (el.querySelector(".w_enabled").checked && !el.querySelector(".w_url").value.trim()) {
      const name = el.querySelector(".w_name").value.trim();
      bad.push([el, t("save.needWebhookUrl",
                      { who: name || t("save.cardNo", { n: i + 1 }) })]);
    }
  });
  return bad;
}

$("saveBtn").onclick = async () => {
  document.querySelectorAll(".invalid").forEach((el) => el.classList.remove("invalid"));
  const bad = incompleteCards();
  if (bad.length) {
    bad.forEach(([el]) => {
      el.classList.add("invalid");
      if (el.tagName === "DETAILS") el.open = true;
    });
    bad[0][0].scrollIntoView({ block: "center", behavior: "smooth" });
    $("saveMsg").textContent = "✗ " + bad[0][1];
    return;
  }
  $("saveMsg").textContent = t("msg.saving");
  try {
    currentConfig = await api("/api/config", "POST", buildConfig());
    $("saveMsg").textContent = t("msg.saved");
    await loadConfig();
  } catch (e) { $("saveMsg").textContent = "✗ " + e.message; }
};

$("changePwBtn").onclick = async () => {
  try {
    await api("/api/password", "POST", {
      current_password: getVal("cur_pw"), new_password: getVal("chg_pw"),
    });
    $("pwMsg").textContent = t("msg.pwChanged");
    $("cur_pw").value = ""; $("chg_pw").value = "";
  } catch (e) { $("pwMsg").textContent = "✗ " + e.message; }
};

// --- backup: export / import ------------------------------------------------
$("exportBtn").onclick = async () => {
  $("backupMsg").textContent = t("msg.exporting");
  try {
    const res = await fetch("/api/config/export");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const dispo = res.headers.get("Content-Disposition") || "";
    const m = dispo.match(/filename="([^"]+)"/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : "pve-usv-config.json";
    a.click();
    URL.revokeObjectURL(a.href);
    $("backupMsg").textContent = t("msg.exported");
  } catch (e) { $("backupMsg").textContent = "✗ " + e.message; }
};

$("importBtn").onclick = () => $("importFile").click();
$("importFile").onchange = async () => {
  const file = $("importFile").files[0];
  if (!file) return;
  if (!confirm(t("confirm.import"))) {
    $("importFile").value = ""; return;
  }
  $("backupMsg").textContent = t("msg.importing");
  try {
    const data = JSON.parse(await file.text());
    currentConfig = await api("/api/config/import", "POST", data);
    await loadConfig();
    $("backupMsg").textContent = t("msg.imported");
  } catch (e) { $("backupMsg").textContent = "✗ " + e.message; }
  $("importFile").value = "";
};

// --- updater ----------------------------------------------------------------
let activeUpdateJob = null;  // job_id of the upload we are currently tracking
let updateStartedAt = null;  // ms timestamp of the current upload, for stuck-queue detection

const UPDATE_STUCK_HINT = t("upd.stuckHint");

async function refreshUpdateStatus() {
  let r;
  try { r = await api("/api/update/status"); } catch (_) { return null; }
  $("upd_version").textContent = "v" + r.version;
  if (deployment === "docker") return r;  // no agent queue/log to render; UI shows guidance instead

  // diagnose block (read-only): queue + agent log tail
  const diag = $("upd_diag");
  if (diag) {
    const pend = (r.pending && r.pending.length)
      ? t("upd.queueN", { n: r.pending.length, list: r.pending.join(", ") })
      : t("upd.queueEmpty");
    diag.textContent = pend + "\n\n" + (r.log_tail || t("upd.noLog"));
  }

  // Only show a result for the CURRENT upload (activeUpdateJob, else the last uploaded).
  const cur = activeUpdateJob || r.last_job;
  const res = r.result;
  const el = $("upd_result");
  if (res && cur && res.job_id === cur) {
    const ok = res.ok;
    const vb = res.version_before, va = res.version_after;
    const ver = (vb || va) ? ` [${vb || "?"} → ${va || "?"}]` : "";
    el.textContent = (ok ? "✓ " : "✗ ") + (res.message || "") + ver +
      (res.ts ? " (" + res.ts + ")" : "");
    el.className = ok ? "help" : "warnnote";
  } else if (r.pending && r.pending.length) {
    // A stuck queue is the classic symptom of a missing/inactive queue-drainer (timer not
    // installed after a cross-version bootstrap). Surface the one-time recovery command
    // instead of a perpetual "in queue" message once the drainer looks inactive or the job
    // has been waiting too long.
    const stalled = r.agent_drainer === false ||
      (updateStartedAt && (Date.now() - updateStartedAt) > 45000);
    if (stalled) {
      el.textContent = UPDATE_STUCK_HINT;
      el.className = "warnnote";
    } else {
      el.textContent = t("upd.inQueue");
      el.className = "help";
    }
  } else {
    el.textContent = "";  // never present an older job's result as the current outcome
    el.className = "help";
  }
  return r;
}

function pollUpdate(jobId, prevVersion, tries = 0) {
  // The service restarts mid-update, so transient fetch failures are expected. Resolve
  // once THIS job's result is in, or the running version changed (restart completed).
  setTimeout(async () => {
    const r = await refreshUpdateStatus();
    const mine = r && r.result && r.result.job_id === jobId;
    const restarted = r && r.version && prevVersion && r.version !== prevVersion;
    if (mine) {
      $("updateMsg").textContent = (r.result.ok === false)
        ? t("upd.failed")
        : t("upd.applied", { v: r.version });
      refreshEvents();
      return;
    }
    if (restarted) {
      $("updateMsg").textContent = t("upd.restarted", { v: r.version });
      refreshEvents();
      return;
    }
    if (tries < 60) pollUpdate(jobId, prevVersion, tries + 1);
    else $("updateMsg").textContent = t("upd.noReply") + UPDATE_STUCK_HINT;
  }, 3000);
}

// One upload path for both the file picker and drag & drop. The backend validates the
// archive by content, so nothing here needs to second-guess the file name.
async function uploadUpdatePackage(file) {
  if (!file) return;
  if (!confirm(t("confirm.update"))) return;
  $("updateMsg").textContent = t("msg.uploading");
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/update/upload", { method: "POST", body: form });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
    activeUpdateJob = data.job_id;
    updateStartedAt = Date.now();
    let info = t("upd.uploaded", { v: data.package_version || t("upd.unknown") });
    if (data.same_version) {
      info += t("upd.sameVersion", { v: data.running_version });
    }
    $("updateMsg").textContent = info;
    pollUpdate(data.job_id, data.running_version);
  } catch (e) { $("updateMsg").textContent = "✗ " + e.message; }
}

$("updateBtn").onclick = () => $("updateFile").click();
$("updateFile").onchange = async () => {
  await uploadUpdatePackage($("updateFile").files[0]);
  $("updateFile").value = "";
};

// Keep the "N polls ≈ M s" hint honest while either input is being edited.
["th_unreachable_alarm_after_polls", "th_poll_interval_normal_s"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("input", updateUnreachableHint);
});

// Drag & drop onto the update card — the way out when a browser's file picker greys the
// package out (Safari and compound extensions, see the accept list in index.html).
(function initUpdateDrop() {
  const card = $("updCard");
  if (!card) return;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach((ev) =>
    card.addEventListener(ev, (e) => { stop(e); card.classList.add("dropping"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    card.addEventListener(ev, (e) => { stop(e); card.classList.remove("dropping"); })
  );
  card.addEventListener("drop", (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadUpdatePackage(file);
  });
})();

boot().catch((e) => { document.body.innerHTML = "<pre style='padding:20px'>" + esc(t("err.prefix") + e.message) + "</pre>"; });
