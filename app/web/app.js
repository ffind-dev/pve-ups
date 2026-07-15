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
  ["login", "firstrun", "dashboard", "settings"].forEach((v) => {
    const el = $(v); if (el) el.hidden = (v !== view);
  });
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));
}

// --- bootstrap --------------------------------------------------------------
let pollTimer = null;

async function boot() {
  const s = await api("/api/session");
  if (!s.password_set) { show("firstrun"); return; }
  if (!s.authenticated) { show("login"); return; }
  $("logoutBtn").hidden = false;
  enterApp();
}

function enterApp() {
  $("mainNav").hidden = false;
  show("dashboard");
  startDashboard();
}

// --- auth -------------------------------------------------------------------
$("setPwBtn").onclick = async () => {
  try {
    await api("/api/password", "POST", { new_password: $("newPw").value });
    location.reload();
  } catch (e) { $("firstErr").textContent = e.message; }
};

$("loginBtn").onclick = async () => {
  try {
    await api("/api/login", "POST", { password: $("loginPw").value });
    $("logoutBtn").hidden = false;
    enterApp();
  } catch (e) { $("loginErr").textContent = e.message; }
};
$("loginPw").addEventListener("keydown", (e) => { if (e.key === "Enter") $("loginBtn").click(); });

$("logoutBtn").onclick = async () => { await api("/api/logout", "POST"); location.reload(); };

// --- tabs -------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = async () => {
    const v = t.dataset.view;
    if (v === "settings") { await loadConfig(); }
    show(v);
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
  const trig = u.triggered
    ? `<div class="stat"><span>${esc(t("ups.trigger"))}</span><b class="crit-text">${esc(u.trigger_reason || t("ups.triggered"))}</b></div>` : "";
  const cd = u.countdown_remaining_s != null
    ? `<div class="stat"><span>${esc(t("ups.countdown"))}</span><b>${u.countdown_remaining_s} s</b></div>` : "";
  const clr = u.comm_loss_remaining_s != null
    ? `<div class="stat"><span>${esc(t("ups.commLossIn"))}</span><b>${u.comm_loss_remaining_s} s</b></div>` : "";
  return `<div class="card ups-card is-${upsStatusCls(u)}">
    <div class="card-h"><h3><svg class="icon batt-ic"><use href="#i-battery"></use></svg>${esc(u.name)}</h3>${statIc}</div>
    <div class="hero-meta"><span>${esc(t("ups.source"))} ${src}</span><span class="faint">·</span><span>${esc(model) || "–"}</span></div>
    <div class="metric" style="margin-top:8px">
      <span class="k">${esc(t("ups.charge"))} ${pct === null ? "–" : pct + " %"}</span>
      <div class="gauge"><div class="gauge-fill${gcls}" style="width:${gw}"></div></div>
    </div>
    <div class="stat"><span>${esc(t("ups.runtime"))}</span><b>${fmt(u.runtime_remaining_min, " min")}</b></div>
    <div class="stat"><span>${esc(t("ups.battery"))}</span><b>${esc(u.battery_status)}</b></div>
    ${cd}${clr}${trig}
    <div class="stat"><span>${esc(t("ups.lastPoll"))}</span><b>${u.last_poll ? new Date(u.last_poll).toLocaleTimeString() : "–"}</b></div>
  </div>`;
}

async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); } catch (_) { return; }
  $("version").textContent = "v" + s.appliance.version;

  const a = s.appliance, sd = s.shutdown, upses = s.ups || [];

  $("d_ups_grid").innerHTML = upses.map(upsCardHtml).join("")
    || `<div class='card'><p class='empty'>${esc(t("ups.none"))}</p></div>`;

  const stateLbl = engineStateLabel(a.engine_state);
  $("d_state").innerHTML = a.engine_state === "ONLINE" ? pill(stateLbl, "ok")
    : a.engine_state === "ON_BATTERY" ? pill(stateLbl, "warn") : pill(stateLbl, "crit");
  $("d_mode").innerHTML = a.dry_run ? pill("DRY-RUN", "warn") : pill(t("mode.armed"), "ok");
  $("d_trig").textContent = sd.triggered ? t("common.yes") : t("common.no");
  $("d_reason").textContent = sd.reason || "–";
  $("d_countdown").textContent = fmt(sd.countdown_remaining_s, " s");
  $("d_uptime").textContent = fmtUptime(a.uptime_s);

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
    if (sd.countdown_remaining_s != null) {
      setBanner("warn", "i-alert", esc(t("banner.unreachCountdown", { s: fmt(sd.countdown_remaining_s, " s") })));
    } else if (sd.comm_loss_remaining_s != null) {
      setBanner("warn", "i-alert", esc(t("banner.unreachCommLoss", { s: fmt(sd.comm_loss_remaining_s, " s") })));
    } else {
      setBanner("warn", "i-alert", esc(t("banner.unreachAlarm")));
    }
    chip = { cls: "warn", text: t("chip.unreach") };
  } else { b.hidden = true; }

  const nav = $("navStatus");
  nav.hidden = false;
  nav.className = "statuschip " + chip.cls;
  $("navStatusText").textContent = chip.text;

  const rows = s.hosts.map((h) => {
    const st = h.shutdown_state;
    const cls = st === "sent" ? "ok" : st === "failed" ? "crit" : h.eligible ? "warn" : "muted";
    const feeds = (h.feeds || []).map((f) => `<span class="chip ${f.triggered ? "crit" : "muted"}">${esc(f.name)}</span>`).join(" ")
      || `<span class='muted'>${esc(t("hosts.allUps"))}</span>`;
    const policy = h.ups_policy === "any" ? t("hosts.policyOr") : t("hosts.policyAnd");
    const stLbl = shutdownStateLabel(st);
    const star = h.this_host
      ? ` <span class='chip star' title="${esc(t("hosts.thisChipTitle"))}">${esc(t("hosts.thisChip"))}</span>` : "";
    return `<tr><td>${esc(h.name)}${star}</td>
      <td>${feeds} <span class="muted">(${esc(policy)})</span></td>
      <td>${pill(stLbl, cls)}</td><td class="muted">${esc(h.last_error || "")}</td></tr>`;
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
const TRISTATE = [["", t("tristate.global")], ["on", t("tristate.on")], ["off", t("tristate.off")]];
const opts = (list, val) => list.map(([v, l]) => `<option value="${v}" ${v === val ? "selected" : ""}>${l}</option>`).join("");
const triVal = (v) => v === true ? "on" : v === false ? "off" : "";

function nextUpsId() {
  const ids = Array.from(document.querySelectorAll("#upsList .u_id")).map((i) => i.value);
  let n = 1;
  while (ids.includes("ups" + n)) n += 1;
  return "ups" + n;
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
  const commPh = u.community === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "public";
  const authPh = u.v3_auth_pass === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "";
  const privPh = u.v3_priv_pass === SECRET_PLACEHOLDER ? t("cfg.unchanged") : "";
  div.innerHTML = `
    <summary class="cfg-head">${svgIcon("i-battery")}<span class="cfg-title u_sum_name"></span><span class="cfg-sub u_sum_host"></span></summary>
    <input type="hidden" class="u_id" value="${esc(id)}" />
    <div class="row">
      <label title="${esc(t("cfg.nameTitle"))}">${esc(t("cfg.name"))} <input class="u_name" value="${esc(u.name || "")}" placeholder="${esc(t("cfg.upsNamePh", { id }))}" /></label>
      <label title="${esc(t("cfg.hostipTitle"))}">${esc(t("cfg.hostip"))} <input class="u_host" value="${esc(u.host || "")}" placeholder="10.0.0.9" /></label>
      <label title="${esc(t("cfg.portTitle"))}">${esc(t("cfg.port"))} <input class="u_port" type="number" value="${u.port || 161}" /></label>
      <label title="${esc(t("cfg.versionTitle"))}">${esc(t("cfg.version"))} <select class="u_version">${opts([["v1", "v1"], ["v2c", "v2c"], ["v3", "v3"]], u.version || "v2c")}</select></label>
    </div>
    <div class="u_v2c">
      <label title="${esc(t("cfg.communityTitle"))}">${esc(t("cfg.community"))} <input class="u_community" placeholder="${esc(commPh)}" /></label>
    </div>
    <div class="u_v3" hidden>
      <div class="row">
        <label title="${esc(t("cfg.v3userTitle"))}">${esc(t("cfg.v3user"))} <input class="u_v3_user" value="${esc(u.v3_user || "")}" /></label>
        <label title="${esc(t("cfg.v3authTitle"))}">${esc(t("cfg.v3auth"))} <select class="u_v3_auth_proto">${opts(AUTH_PROTOS, u.v3_auth_proto || "sha")}</select></label>
        <label title="${esc(t("cfg.v3authpwTitle"))}">${esc(t("cfg.v3authpw"))} <input class="u_v3_auth_pass" type="password" placeholder="${esc(authPh)}" /></label>
      </div>
      <div class="row">
        <label title="${esc(t("cfg.v3privTitle"))}">${esc(t("cfg.v3priv"))} <select class="u_v3_priv_proto">${opts(PRIV_PROTOS, u.v3_priv_proto || "aes")}</select></label>
        <label title="${esc(t("cfg.v3privpwTitle"))}">${esc(t("cfg.v3privpw"))} <input class="u_v3_priv_pass" type="password" placeholder="${esc(privPh)}" /></label>
      </div>
    </div>
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
      <button class="btn-ghost btn-sm u_test" style="flex:0 0 auto">${esc(t("cfg.testSnmp"))}</button>
      <button class="btn-ghost btn-sm u_del" style="flex:0 0 auto">${esc(t("cfg.removeUps"))}</button>
      <span class="muted u_msg"></span>
    </div>`;
  const toggleVer = () => {
    const v3 = div.querySelector(".u_version").value === "v3";
    div.querySelector(".u_v3").hidden = !v3;
    div.querySelector(".u_v2c").hidden = v3;
  };
  const updSum = () => {
    const nm = div.querySelector(".u_name").value.trim() || t("cfg.upsNamePh", { id });
    const hs = div.querySelector(".u_host").value.trim();
    div.querySelector(".u_sum_name").textContent = nm;
    div.querySelector(".u_sum_host").textContent = hs ? "· " + hs : "";
  };
  div.querySelector(".u_version").onchange = toggleVer;
  div.querySelector(".u_name").oninput = () => { updSum(); renderHostUpsCheckboxes(); drawConfigTopology(); };
  div.querySelector(".u_host").oninput = updSum;
  div.querySelector(".u_test").onclick = () => testUps(div);
  div.querySelector(".u_del").onclick = () => { div.remove(); renderHostUpsCheckboxes(); drawConfigTopology(); };
  $("upsList").appendChild(div);
  toggleVer();
  updSum();
}

$("addUpsBtn").onclick = () => { addUpsCard({}, true); renderHostUpsCheckboxes(); drawConfigTopology(); };

function upsFromCard(div) {
  const q = (s) => div.querySelector(s);
  const numOr = (s) => { const v = q(s).value.trim(); return v === "" ? null : Number(v); };
  const tri = (s) => { const v = q(s).value; return v === "on" ? true : v === "off" ? false : null; };
  const comm = q(".u_community").value, ap = q(".u_v3_auth_pass").value, pp = q(".u_v3_priv_pass").value;
  return {
    id: q(".u_id").value,
    name: q(".u_name").value.trim(),
    host: q(".u_host").value.trim(),
    port: Number(q(".u_port").value || 161),
    version: q(".u_version").value,
    community: comm === "" ? SECRET_PLACEHOLDER : comm,
    v3_user: q(".u_v3_user").value,
    v3_auth_proto: q(".u_v3_auth_proto").value,
    v3_auth_pass: ap === "" ? SECRET_PLACEHOLDER : ap,
    v3_priv_proto: q(".u_v3_priv_proto").value,
    v3_priv_pass: pp === "" ? SECRET_PLACEHOLDER : pp,
    overrides: {
      on_battery_seconds: numOr(".o_obs"),
      runtime_below_minutes: numOr(".o_rbm"),
      charge_below_percent: numOr(".o_cbp"),
      on_battery_low: tri(".o_obl"),
      comm_loss_shutdown_after_min: numOr(".o_clm"),
      keep_shutdown_on_comm_loss: tri(".o_ksc"),
    },
  };
}

function currentUpsList() {
  return Array.from(document.querySelectorAll("#upsList .ups-cfg"))
    .map(upsFromCard).filter((u) => u.host || u.name);
}

async function testUps(div) {
  const msg = div.querySelector(".u_msg");
  msg.textContent = t("msg.testing");
  try {
    const r = await api("/api/test/snmp", "POST", upsFromCard(div));
    msg.textContent = r.reachable
      ? t("snmp.ok", { src: r.power_source, batt: r.battery_status, min: r.runtime_remaining_min, pct: r.battery_charge_pct })
      : t("snmp.fail", { err: r.error || "" });
  } catch (e) { msg.textContent = "✗ " + e.message; }
}

async function loadConfig() {
  currentConfig = await api("/api/config");
  const c = currentConfig;
  setChk("s_dry_run", c.dry_run);

  renderUps(c.ups || []);
  renderHosts(c.hosts || []);
  renderHostUpsCheckboxes();

  const t = c.thresholds;
  setVal("th_on_battery_seconds", t.on_battery_seconds);
  setVal("th_runtime_below_minutes", t.runtime_below_minutes);
  setVal("th_charge_below_percent", t.charge_below_percent);
  setChk("th_on_battery_low", t.on_battery_low);
  setVal("th_poll_interval_normal_s", t.poll_interval_normal_s);
  setVal("th_poll_interval_battery_s", t.poll_interval_battery_s);
  setVal("th_host_shutdown_timeout_s", t.host_shutdown_timeout_s);
  setVal("th_comm_loss_shutdown_after_min", t.comm_loss_shutdown_after_min);
  setChk("th_keep_shutdown_on_comm_loss", t.keep_shutdown_on_comm_loss);

  setVal("ntp_server", c.ntp_server);
  setVal("tz_timezone", c.timezone);
  setChk("selftest_enabled", c.selftest_enabled);
  setVal("selftest_hour", c.selftest_hour);

  const wh = c.notifications.webhook;
  setChk("webhook_enabled", wh.enabled); setVal("webhook_url", wh.url);

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
  el.innerHTML = `
    <summary class="cfg-head">${svgIcon("i-server")}<span class="cfg-title h_sum_name"></span><span class="cfg-sub h_sum_meta"></span></summary>
    <div class="row">
      <label title="${esc(t("host.nodeTitle"))}">${esc(t("host.node"))} <input class="h_name" value="${esc(h.name || "")}" placeholder="pve01" /></label>
      <label title="${esc(t("host.apiurlTitle"))}">${esc(t("host.apiurl"))} <input class="h_url" value="${esc(h.api_url || "")}" placeholder="https://10.0.0.10:8006" /></label>
    </div>
    <div class="row">
      <label title="${esc(t("host.tokenIdTitle"))}">${esc(t("host.tokenId"))} <input class="h_token_id" value="${esc(h.token_id || "")}" placeholder="ups@pve!shutdown" /></label>
      <label title="${esc(t("host.tokenSecretTitle"))}">${esc(t("host.tokenSecret"))} <input class="h_token_secret" type="password" placeholder="${esc(secretSet ? t("cfg.unchanged") : t("host.tokenSecretPh"))}" /></label>
    </div>
    <div class="feedsblock" title="${esc(t("host.feedsTitle"))}">
      <span class="cfg-label">${esc(t("host.feeds"))}</span>
      <div class="h_feeds"></div>
    </div>
    <div class="row hostflags">
      <label title="${esc(t("host.policyTitle"))}">${esc(t("host.policy"))} <select class="h_policy"><option value="all">${esc(t("hosts.policyAnd"))}</option><option value="any">${esc(t("hosts.policyOr"))}</option></select></label>
      <label title="${esc(t("host.orderTitle"))}">${esc(t("host.order"))} <input class="h_order" type="number" value="${h.order || 0}" /></label>
      <label class="chkline" title="${esc(t("host.verifyTitle"))}"><input class="h_verify" type="checkbox" ${h.verify_tls ? "checked" : ""} /> ${esc(t("host.verify"))}</label>
      <label class="chkline" title="${esc(t("host.thisTitle"))}"><input class="h_this" type="checkbox" ${h.this_host ? "checked" : ""} /> ${esc(t("host.this"))}</label>
      <label class="chkline" title="${esc(t("host.enabledTitle"))}"><input class="h_enabled" type="checkbox" ${h.enabled !== false ? "checked" : ""} /> ${esc(t("host.enabled"))}</label>
    </div>
    <div class="row" style="margin:0;align-items:center">
      <button class="btn-ghost btn-sm h_test" style="flex:0 0 auto">${esc(t("host.test"))}</button>
      <button class="btn-ghost btn-sm h_del" style="flex:0 0 auto">${esc(t("host.remove"))}</button>
      <span class="muted h_msg"></span>
    </div>`;
  el.querySelector(".h_policy").value = h.ups_policy || "all";
  const updSum = () => {
    const nm = el.querySelector(".h_name").value.trim() || t("host.newName");
    const isThis = el.querySelector(".h_this").checked;
    const en = el.querySelector(".h_enabled").checked;
    el.querySelector(".h_sum_name").textContent = nm + (isThis ? " ★" : "");
    el.querySelector(".h_sum_meta").textContent = en ? "" : t("host.inactive");
  };
  el.querySelector(".h_del").onclick = () => { el.remove(); drawConfigTopology(); };
  el.querySelector(".h_test").onclick = () => testHost(el);
  el.querySelector(".h_name").oninput = () => { updSum(); drawConfigTopology(); };
  el.querySelector(".h_this").onchange = () => { updSum(); drawConfigTopology(); };
  el.querySelector(".h_enabled").onchange = updSum;
  $("hostRows").appendChild(el);
  updSum();
}
$("addHostBtn").onclick = () => { addHostRow({}, true, true); renderHostUpsCheckboxes(); drawConfigTopology(); };

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
    cell.querySelectorAll(".h_feed").forEach((c) => { c.onchange = drawConfigTopology; });
  });
}

function hostFromRow(tr) {
  const secret = tr.querySelector(".h_token_secret").value;
  return {
    name: tr.querySelector(".h_name").value.trim(),
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
  };
}

async function testHost(el) {
  const msg = el.querySelector(".h_msg");
  msg.textContent = t("msg.testing");
  try {
    const r = await api("/api/test/host", "POST", hostFromRow(el));
    msg.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
  } catch (e) { msg.textContent = "✗ " + e.message; }
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
    this_host: tr.querySelector(".h_this").checked,
    ups_ids: Array.from(tr.querySelectorAll(".h_feed")).filter((c) => c.checked).map((c) => c.value),
  })).filter((h) => h.name);
  drawTopology($("topoDiagram"), upsMeta(), hosts, null);
}

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
    ups: currentUpsList(),
    hosts,
    thresholds: {
      on_battery_seconds: getNum("th_on_battery_seconds"),
      runtime_below_minutes: getNum("th_runtime_below_minutes"),
      charge_below_percent: getNum("th_charge_below_percent"),
      on_battery_low: getChk("th_on_battery_low"),
      poll_interval_normal_s: getNum("th_poll_interval_normal_s") || 30,
      poll_interval_battery_s: getNum("th_poll_interval_battery_s") || 8,
      unreachable_alarm_after_polls: currentConfig.thresholds.unreachable_alarm_after_polls,
      host_shutdown_timeout_s: getNum("th_host_shutdown_timeout_s") || 60,
      comm_loss_shutdown_after_min: getNum("th_comm_loss_shutdown_after_min"),
      keep_shutdown_on_comm_loss: getChk("th_keep_shutdown_on_comm_loss"),
    },
    notifications: {
      webhook: { enabled: getChk("webhook_enabled"), url: getVal("webhook_url").trim() },
    },
  };
}

$("saveBtn").onclick = async () => {
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

$("updateBtn").onclick = () => $("updateFile").click();
$("updateFile").onchange = async () => {
  const file = $("updateFile").files[0];
  if (!file) return;
  if (!confirm(t("confirm.update"))) {
    $("updateFile").value = ""; return;
  }
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
  $("updateFile").value = "";
};

boot().catch((e) => { document.body.innerHTML = "<pre style='padding:20px'>" + esc(t("err.prefix") + e.message) + "</pre>"; });
