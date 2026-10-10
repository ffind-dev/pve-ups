// History tab (#37): runtime, load and charge of every UPS over time, as inline SVG.
// Vanilla JS without a chart library, like the rest of the UI (offline, no build step).
// Loaded after app.js and uses its helpers: $, t, esc, api, show.
//
// Two views. One UPS: two panels on a shared time axis — runtime in minutes on top,
// load and charge in percent below (two panels rather than one chart with two y-axes,
// which would invite reading minutes against percent). "All": one measure at a time, one
// line per UPS, and a status strip per UPS underneath instead of overlapping backgrounds.
// Copyright 2026 Florian Finder

const HIST_RANGES = ["1h", "6h", "24h", "7d", "30d", "90d", "all"];
const HIST_RANGE_S = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000 };
const HIST_PREF_KEY = "pve-usv-history";
const HIST_REFRESH_MS = 60000;
const HIST_SERIES = 8;              // --series-1..8 in style.css, assigned in that order
const SVG_NS = "http://www.w3.org/2000/svg";

const hist = {
  // Per-viewer conveniences only (see histLoadPrefs); the data always comes from the API.
  prefs: { range: "24h", ups: null, metric: "runtime", runtime: true, load: true, charge: true, info: false },
  zoom: null,       // {from, to} while zoomed in, else null
  data: null,       // last /api/history answer
  timer: null,
  seq: 0,           // request counter: an answer that arrives late never overwrites a newer one
  geo: null,        // geometry of the last render, for the pointer handlers
  drag: null,
};

function histLoadPrefs() {
  try { Object.assign(hist.prefs, JSON.parse(localStorage.getItem(HIST_PREF_KEY) || "{}")); } catch (_) { /* private mode */ }
}
function histSavePrefs() {
  try { localStorage.setItem(HIST_PREF_KEY, JSON.stringify(hist.prefs)); } catch (_) { /* private mode */ }
}

// --- lifecycle (called from app.js) ------------------------------------------------
async function enterHistory() {
  histLoadPrefs();
  $("h_range").setAttribute("aria-label", t("hist.rangeLabel"));
  $("h_ups").setAttribute("aria-label", t("hist.upsLabel"));
  $("h_svg").setAttribute("aria-label", t("hist.chartLabel"));
  $("h_info").checked = !!hist.prefs.info;
  await histFetch();
  clearInterval(hist.timer);
  // A zoomed view is a deliberate look at the past: it is not refreshed underneath you.
  hist.timer = setInterval(() => { if (!hist.zoom) histFetch(); }, HIST_REFRESH_MS);
}

function leaveHistory() {
  clearInterval(hist.timer);
  hist.timer = null;
  histHideTip();
}

function histQuery() {
  return hist.zoom
    ? `from=${Math.floor(hist.zoom.from)}&to=${Math.ceil(hist.zoom.to)}`
    : `range=${encodeURIComponent(hist.prefs.range)}`;
}

async function histFetch() {
  const seq = ++hist.seq;
  const wrap = $("h_chartwrap");
  wrap.classList.add("loading");          // keep the old frame, dimmed, while loading
  let d;
  try {
    d = await api("/api/history?" + histQuery());
  } catch (e) {
    if (seq === hist.seq) { wrap.classList.remove("loading"); $("h_note").textContent = String(e.message || e); }
    return;
  }
  if (seq !== hist.seq) return;
  wrap.classList.remove("loading");
  if (!d.enabled) { leaveHistory(); show("dashboard"); return; }
  hist.data = d;
  histRender();
}

// --- small helpers -------------------------------------------------------------------
function svgEl(name, attrs, parent) {
  const el = document.createElementNS(SVG_NS, name);
  for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) el.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(el);
  return el;
}

function seriesVar(i) { return `var(--series-${(i % HIST_SERIES) + 1})`; }

function niceMax(v) {
  if (!(v > 0)) return 10;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min` + (s % 60 && s < 600 ? ` ${s % 60} s` : "");
}

function fmtTime(ts, withDate) {
  const d = new Date(ts * 1000);
  return withDate
    ? d.toLocaleString([], { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
  172800, 604800, 1209600, 2592000];

function timeTicks(x0, x1, width) {
  const want = Math.max(2, Math.floor(width / 110));
  const span = x1 - x0;
  const step = TICK_STEPS.find((s) => span / s <= want) || TICK_STEPS[TICK_STEPS.length - 1];
  // Aligned to LOCAL time, so day ticks sit on midnight where the reader lives.
  const off = new Date(x0 * 1000).getTimezoneOffset() * 60;
  const ticks = [];
  for (let tk = Math.ceil((x0 - off) / step) * step + off; tk <= x1; tk += step) ticks.push(tk);
  return { ticks, step };
}

function tickLabel(ts, step) {
  const d = new Date(ts * 1000);
  if (step >= 86400 || (d.getHours() === 0 && d.getMinutes() === 0 && step >= 3600)) {
    return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Index of the point closest to ts (points sorted by time), or -1.
function nearestIdx(points, ts) {
  let lo = 0, hi = points.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < ts) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1][0] - ts) <= Math.abs(points[lo][0] - ts)) lo -= 1;
  return lo;
}

function segmentAt(segments, ts) {
  for (const s of segments) if (s[0] <= ts && ts <= s[1]) return s[2];
  return null;
}

const KIND_LABEL = {
  mains: "hist.stMains", battery: "hist.stBattery", triggered: "hist.stTriggered",
  unreachable: "hist.stUnreachable", bypass: "hist.stBypass", other: "hist.stOther",
};

// --- toolbar ---------------------------------------------------------------------------
function histSelection(d) {
  const ids = d.ups.map((u) => u.id);
  let sel = hist.prefs.ups;
  if (ids.length <= 1) sel = ids[0] || null;
  else if (sel !== "all" && !ids.includes(sel)) sel = "all";
  return sel;
}

function segButton(label, active, onclick, opts = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "hseg-btn" + (active ? " active" : "");
  b.setAttribute("aria-pressed", active ? "true" : "false");
  if (opts.disabled) b.disabled = true;
  if (opts.title) b.title = opts.title;
  if (opts.color) {
    const k = document.createElement("span");
    k.className = "key";
    k.style.borderColor = opts.color;
    b.appendChild(k);
  }
  b.appendChild(document.createTextNode(label));
  b.onclick = onclick;
  return b;
}

function renderHistToolbar(d, sel) {
  const range = $("h_range");
  range.replaceChildren();
  const span = d.retention_days * 86400;
  for (const r of HIST_RANGES) {
    const tooLong = r !== "all" && HIST_RANGE_S[r] > span + 3600;
    range.appendChild(segButton(t("hist.range." + r), !hist.zoom && hist.prefs.range === r, () => {
      hist.prefs.range = r; hist.zoom = null; histSavePrefs(); histFetch();
    }, { disabled: tooLong, title: tooLong ? t("hist.rangeTooLong", { days: d.retention_days }) : "" }));
  }
  $("h_zoomreset").hidden = !hist.zoom;

  const upsBox = $("h_ups");
  upsBox.replaceChildren();
  upsBox.hidden = d.ups.length <= 1;
  if (d.ups.length > 1) {
    d.ups.forEach((u, i) => upsBox.appendChild(segButton(u.name, sel === u.id, () => {
      hist.prefs.ups = u.id; histSavePrefs(); histRender();
    }, { color: seriesVar(i) })));
    upsBox.appendChild(segButton(t("hist.allUps"), sel === "all", () => {
      hist.prefs.ups = "all"; histSavePrefs(); histRender();
    }));
  }

  const box = $("h_series");
  box.replaceChildren();
  if (sel === "all") {
    // One measure at a time across all UPS devices: lines with different units in one
    // chart would only invite comparing them.
    const seg = document.createElement("div");
    seg.className = "hseg";
    for (const m of ["runtime", "load", "charge"]) {
      seg.appendChild(segButton(t("hist.m." + m), hist.prefs.metric === m, () => {
        hist.prefs.metric = m; histSavePrefs(); histRender();
      }));
    }
    box.appendChild(seg);
  } else {
    for (const m of ["runtime", "load", "charge"]) {
      const lbl = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!hist.prefs[m];
      cb.onchange = () => { hist.prefs[m] = cb.checked; histSavePrefs(); histRender(); };
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + t("hist.m." + m)));
      box.appendChild(lbl);
    }
  }
}

// --- rendering --------------------------------------------------------------------------
function histRender() {
  const d = hist.data;
  if (!d) return;
  histHideTip();
  const sel = histSelection(d);
  renderHistToolbar(d, sel);
  renderOutages(d);
  histFillSpan(d);

  const svg = $("h_svg");
  svg.replaceChildren();
  const note = $("h_note");
  if (!d.ups.length) {
    svg.setAttribute("height", "0");
    $("h_legend").replaceChildren();
    note.textContent = t("hist.noUps");
    return;
  }

  const W = Math.max(320, $("h_chartwrap").clientWidth || 800);
  const narrow = W < 560;
  const all = sel === "all";
  const shown = all ? d.ups : d.ups.filter((u) => u.id === sel);
  const colorOf = (u) => seriesVar(d.ups.indexOf(u));
  const L = 46;
  // Room on the right for direct labels at the line ends (up to four lines).
  const R = narrow ? 12 : (all ? 104 : 64);
  const PW = W - L - R;
  const x0 = d.from, x1 = d.to;
  const X = (ts) => L + ((ts - x0) / Math.max(1, x1 - x0)) * PW;
  const gapLimit = Math.max(300, 3 * (d.bucket_s || 60));

  const defs = svgEl("defs", {}, svg);
  const pat = svgEl("pattern", { id: "h_hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
  svgEl("line", { x1: 0, y1: 0, x2: 0, y2: 6, class: "hb-hatch" }, pat);

  // Panels to draw: [{title, unit, max, series:[{u, idx, label, color, dashed}], bands}]
  const panels = [];
  if (all) {
    const m = hist.prefs.metric;
    const idx = { runtime: 1, charge: 2, load: 3 }[m];
    let max = m === "runtime" ? 0 : 100;
    if (m === "runtime") {
      for (const u of shown) {
        for (const p of u.points) if (p[1] != null) max = Math.max(max, p[1]);
        if (u.threshold_runtime_min) max = Math.max(max, u.threshold_runtime_min);
      }
      max = niceMax(max * 1.1);
    }
    panels.push({
      title: t("hist.p." + m), max, h: narrow ? 220 : 280, bands: null,
      thresholds: m === "runtime" ? shown.filter((u) => u.threshold_runtime_min).map((u) => ({ v: u.threshold_runtime_min, color: colorOf(u) })) : [],
      series: shown.map((u) => ({ u, idx, label: u.name, color: colorOf(u) })),
    });
  } else {
    const u = shown[0];
    if (hist.prefs.runtime) {
      let max = 0;
      for (const p of u.points) if (p[1] != null) max = Math.max(max, p[1]);
      if (u.threshold_runtime_min) max = Math.max(max, u.threshold_runtime_min);
      panels.push({
        title: t("hist.p.runtime"), max: niceMax(max * 1.1), h: narrow ? 170 : 210, bands: u,
        thresholds: u.threshold_runtime_min ? [{ v: u.threshold_runtime_min, color: null }] : [],
        series: [{ u, idx: 1, label: t("hist.m.runtime"), color: seriesVar(0), direct: false }],
      });
    }
    const pct = [];
    if (hist.prefs.load) pct.push({ u, idx: 3, label: t("hist.m.load"), color: seriesVar(0) });
    if (hist.prefs.charge) pct.push({ u, idx: 2, label: t("hist.m.charge"), color: seriesVar(1), dashed: true });
    if (pct.length) panels.push({ title: t("hist.p.percent"), max: 100, h: narrow ? 120 : 150, bands: u, thresholds: [], series: pct });
  }

  const TOP = 6, GAP = 26, AXIS = 22;
  let y = TOP;
  for (const p of panels) { p.y = y + 16; y = p.y + p.h + GAP; }
  const strips = all ? shown : [];
  const stripTop = y - GAP + 10;
  const STRIP = 15, STRIP_GAP = 4;
  const stripsH = strips.length ? strips.length * (STRIP + STRIP_GAP) + 4 : 0;
  const plotBottom = (panels.length ? panels[panels.length - 1].y + panels[panels.length - 1].h : TOP + 20);
  const axisY = (strips.length ? stripTop + stripsH : plotBottom) + 4;
  const H = axisY + AXIS;
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  // Time grid + axis labels, shared by every panel.
  const { ticks, step } = timeTicks(x0, x1, PW);
  for (const tk of ticks) {
    const x = X(tk);
    svgEl("line", { x1: x, x2: x, y1: TOP + 14, y2: axisY - 2, class: "hgrid" }, svg);
    const tx = svgEl("text", { x, y: axisY + 13, "text-anchor": "middle", class: "haxis" }, svg);
    tx.textContent = tickLabel(tk, step);
  }

  let anyPoint = false;
  for (const p of panels) {
    const Y = (v) => p.y + p.h - (Math.max(0, Math.min(p.max, v)) / p.max) * p.h;
    p.Y = Y;
    // Status bands behind the lines (one UPS only — "all" uses the strips below).
    if (p.bands) {
      for (const s of p.bands.segments) {
        if (s[2] === "mains") continue;
        const a = X(Math.max(s[0], x0)), b = X(Math.min(s[1], x1));
        // At least 2 px: a ten-minute outage in a 90-day view must still be there.
        svgEl("rect", { x: a, y: p.y, width: Math.max(2, b - a), height: p.h,
          class: s[2] === "unreachable" ? "" : "hb-" + s[2],
          fill: s[2] === "unreachable" ? "url(#h_hatch)" : null }, svg);
      }
    }
    const title = svgEl("text", { x: L, y: p.y - 5, class: "htitle" }, svg);
    title.textContent = p.title;
    for (const f of [0, 0.25, 0.5, 0.75, 1]) {
      const v = p.max * f;
      const yy = Y(v);
      svgEl("line", { x1: L, x2: L + PW, y1: yy, y2: yy, class: "hgrid" }, svg);
      const lab = svgEl("text", { x: L - 6, y: yy + 4, "text-anchor": "end", class: "haxis" }, svg);
      lab.textContent = Number.isInteger(v) ? String(v) : v.toFixed(1);
    }
    // The runtime trigger. Where every UPS shares the same value — the usual case — it is
    // one neutral, labelled line; only differing values get one line per UPS in its colour.
    const same = new Set(p.thresholds.map((th) => th.v)).size === 1;
    for (const th of same ? p.thresholds.slice(0, 1) : p.thresholds) {
      const yy = Y(th.v);
      const ln = svgEl("line", { x1: L, x2: L + PW, y1: yy, y2: yy, class: "hthresh" }, svg);
      if (th.color && !same) ln.style.stroke = th.color;
    }
    if (p.thresholds.length && same) {
      const lab = svgEl("text", { x: L + PW - 4, y: Y(p.thresholds[0].v) - 4, "text-anchor": "end", class: "hlabel" }, svg);
      lab.textContent = t("hist.threshold", { min: p.thresholds[0].v });
    }
    // Lines, broken where data is missing rather than drawn across the hole.
    const ends = [];
    for (const s of p.series) {
      let dstr = "", prevTs = null, last = null;
      for (const pt of s.u.points) {
        const v = pt[s.idx];
        if (v == null) { prevTs = null; continue; }
        anyPoint = true;
        const cmd = prevTs == null || pt[0] - prevTs > gapLimit ? "M" : "L";
        dstr += `${cmd}${X(pt[0]).toFixed(1)},${Y(v).toFixed(1)}`;
        prevTs = pt[0];
        last = pt;
      }
      if (dstr) {
        const path = svgEl("path", { d: dstr, class: "hline" + (s.dashed ? " dashed" : "") }, svg);
        path.style.stroke = s.color;
      }
      if (last && !narrow && p.series.length > 1 && p.series.length <= 4) {
        ends.push({ y: Y(last[s.idx]), label: s.label, x: X(last[0]) });
      }
    }
    // Direct labels at the line ends, nudged apart so they never overlap.
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
    for (const e of ends) {
      const tx = svgEl("text", { x: L + PW + 6, y: e.y + 4, class: "hlabel" }, svg);
      tx.textContent = e.label.length > 14 ? e.label.slice(0, 13) + "…" : e.label;
    }
  }

  // One status strip per UPS in the "all" view.
  strips.forEach((u, i) => {
    const sy = stripTop + i * (STRIP + STRIP_GAP);
    // Mains first, everything else on top and at least 2 px wide, so a short outage in a
    // long range is not painted over by the mains stretch next to it.
    for (const pass of [true, false]) {
      for (const s of u.segments) {
        if ((s[2] === "mains") !== pass) continue;
        const a = X(Math.max(s[0], x0)), b = X(Math.min(s[1], x1));
        if (pass && b - a < 0.5) continue;
        svgEl("rect", { x: a, y: sy, width: pass ? b - a : Math.max(2, b - a), height: STRIP,
          class: "hstrip" + (s[2] === "unreachable" ? "" : " hb-" + s[2]),
          fill: s[2] === "unreachable" ? "url(#h_hatch)" : null }, svg);
      }
    }
    // The name inside the strip, so it needs no margin of its own (and survives a phone).
    const lab = svgEl("text", { x: L + 5, y: sy + STRIP - 4, class: "hstriplabel" }, svg);
    lab.textContent = u.name;
  });

  // Event markers across the whole height, warnings and criticals by default.
  const events = (d.events || []).filter((e) => e.severity !== "info" || hist.prefs.info)
    .filter((e) => e.ts >= x0 && e.ts <= x1);
  const markTop = TOP + 14, markBottom = axisY - 2;
  // Markers closer than 6 px share one stroke; the tooltip lists them all.
  const clusters = [];
  for (const e of events) {
    const x = X(e.ts);
    const c = clusters[clusters.length - 1];
    if (c && x - c.x < 6) { c.events.push(e); if (e.severity === "critical") c.sev = "critical"; }
    else clusters.push({ x, events: [e], sev: e.severity });
  }
  for (const c of clusters) {
    svgEl("line", { x1: c.x, x2: c.x, y1: markTop, y2: markBottom, class: "hmark " + c.sev }, svg);
    svgEl("path", { d: `M${c.x - 4},${markTop - 6}L${c.x + 4},${markTop - 6}L${c.x},${markTop}Z`, class: "hmarktip " + c.sev }, svg);
  }

  // Interaction layer: crosshair, tooltip, drag-to-zoom.
  const cross = svgEl("line", { x1: 0, x2: 0, y1: markTop, y2: markBottom, class: "hcross", visibility: "hidden" }, svg);
  const selRect = svgEl("rect", { x: 0, y: markTop, width: 0, height: markBottom - markTop, class: "hsel", visibility: "hidden" }, svg);
  const dots = svgEl("g", {}, svg);
  const overlay = svgEl("rect", { x: L, y: markTop - 8, width: PW, height: markBottom - markTop + 8,
    class: "hoverlay", tabindex: 0 }, svg);
  hist.geo = { L, PW, x0, x1, X, panels, shown, all, clusters, cross, selRect, dots, overlay, W };
  wireHistPointer(overlay);

  renderHistLegend(d, sel, shown, colorOf, clusters.length > 0);
  const empty = !anyPoint && !shown.some((u) => u.segments.length);
  note.textContent = empty ? t("hist.noData") : (hist.zoom ? t("hist.zoomed") : t("hist.hint"));
}

function renderHistLegend(d, sel, shown, colorOf, hasMarks) {
  const box = $("h_legend");
  box.replaceChildren();
  const item = (cls, label, color) => {
    const s = document.createElement("span");
    s.className = "lg";
    const k = document.createElement("span");
    k.className = cls;
    if (color) k.style.borderColor = color;
    s.appendChild(k);
    s.appendChild(document.createTextNode(label));
    box.appendChild(s);
  };
  if (sel === "all") {
    shown.forEach((u) => item("ln", u.name, colorOf(u)));
  } else {
    if (hist.prefs.runtime) item("ln", t("hist.m.runtime"), seriesVar(0));
    if (hist.prefs.load) item("ln", t("hist.m.load"), seriesVar(0));
    if (hist.prefs.charge) item("ln dashed", t("hist.m.charge"), seriesVar(1));
  }
  // Only the states that actually occur in view: a legend of things that are not there
  // is one more thing to read.
  const kinds = new Set();
  for (const u of shown) for (const s of u.segments) kinds.add(s[2]);
  if (sel === "all" && kinds.has("mains")) item("sw hb-mains", t(KIND_LABEL.mains));
  for (const k of ["battery", "triggered", "bypass", "unreachable"]) {
    if (kinds.has(k)) item("sw hb-" + k, t(KIND_LABEL[k]));
  }
  if (hasMarks) {
    item("mk warning", t("hist.lgWarning"));
    item("mk critical", t("hist.lgCritical"));
  }
}

// --- tooltip, crosshair, zoom ------------------------------------------------------------
function histHideTip() {
  const tip = $("h_tip");
  if (tip) tip.hidden = true;
  if (hist.geo) {
    hist.geo.cross.setAttribute("visibility", "hidden");
    hist.geo.dots.replaceChildren();
  }
}

function histShowAt(px) {
  const g = hist.geo;
  if (!g) return;
  px = Math.max(g.L, Math.min(g.L + g.PW, px));
  const ts = g.x0 + ((px - g.L) / g.PW) * (g.x1 - g.x0);
  g.cross.setAttribute("x1", px);
  g.cross.setAttribute("x2", px);
  g.cross.setAttribute("visibility", "visible");
  g.dots.replaceChildren();

  const tip = $("h_tip");
  tip.replaceChildren();
  const head = document.createElement("div");
  head.className = "tt-time";
  head.textContent = new Date(ts * 1000).toLocaleString();
  tip.appendChild(head);
  const row = (value, label, color, dashed) => {
    const r = document.createElement("div");
    r.className = "tt-row";
    if (color) {
      const k = document.createElement("span");
      k.className = "tt-key";
      k.style.borderColor = color;
      if (dashed) k.style.borderTopStyle = "dashed";
      r.appendChild(k);
    }
    const b = document.createElement("b");
    b.textContent = value;
    r.appendChild(b);
    r.appendChild(document.createTextNode(label));
    tip.appendChild(r);
  };
  const gapLimit = Math.max(300, 3 * ((hist.data && hist.data.bucket_s) || 60));
  for (const p of g.panels) {
    for (const s of p.series) {
      const i = nearestIdx(s.u.points, ts);
      const pt = i >= 0 ? s.u.points[i] : null;
      const v = pt && Math.abs(pt[0] - ts) <= gapLimit ? pt[s.idx] : null;
      const unit = s.idx === 1 ? " min" : " %";
      row(v == null ? "–" : v + unit, s.label, s.color, s.dashed);
      if (v != null) {
        const dot = svgEl("circle", { cx: g.X(pt[0]), cy: p.Y(v), r: 4, class: "hdot" }, g.dots);
        dot.style.fill = s.color;
      }
    }
  }
  for (const u of g.shown) {
    const kind = segmentAt(u.segments, ts);
    const label = kind ? t(KIND_LABEL[kind] || "hist.stOther") : t("hist.stNoData");
    row(label, g.all ? u.name : "", null);
  }
  const near = g.clusters.filter((c) => Math.abs(c.x - px) <= 6);
  for (const c of near) {
    for (const e of c.events) {
      const ev = document.createElement("div");
      ev.className = "tt-ev";
      const sev = document.createElement("span");
      sev.className = e.severity;
      sev.textContent = fmtTime(e.ts, false) + " ";
      const b = document.createElement("b");
      b.textContent = e.event;
      ev.appendChild(sev);
      ev.appendChild(b);
      if (e.detail) {
        const det = document.createElement("div");
        det.textContent = e.detail.length > 220 ? e.detail.slice(0, 219) + "…" : e.detail;
        ev.appendChild(det);
      }
      tip.appendChild(ev);
    }
  }
  tip.hidden = false;
  // Beside the crosshair, flipped to the left near the right edge, kept inside the box.
  const wrapW = $("h_chartwrap").clientWidth;
  const scale = wrapW / g.W;
  const tw = tip.offsetWidth;
  let left = px * scale + 14;
  if (left + tw > wrapW) left = Math.max(0, px * scale - tw - 14);
  tip.style.left = left + "px";
  tip.style.top = "8px";
}

function wireHistPointer(overlay) {
  const toPx = (ev) => {
    const r = overlay.ownerSVGElement.getBoundingClientRect();
    return ((ev.clientX - r.left) / r.width) * hist.geo.W;
  };
  overlay.onpointerdown = (ev) => {
    if (ev.button !== 0) return;
    hist.drag = { x: toPx(ev) };
    histShowAt(hist.drag.x);  // a tap on a touch screen has no hover
    overlay.setPointerCapture(ev.pointerId);
  };
  overlay.onpointermove = (ev) => {
    const px = toPx(ev);
    if (hist.drag && Math.abs(px - hist.drag.x) > 4) {
      const a = Math.min(px, hist.drag.x), b = Math.max(px, hist.drag.x);
      const g = hist.geo;
      g.selRect.setAttribute("x", Math.max(g.L, a));
      g.selRect.setAttribute("width", Math.min(g.L + g.PW, b) - Math.max(g.L, a));
      g.selRect.setAttribute("visibility", "visible");
    }
    histShowAt(px);
  };
  overlay.onpointerup = (ev) => {
    const g = hist.geo;
    if (!hist.drag) return;
    const px = toPx(ev);
    const a = Math.min(px, hist.drag.x), b = Math.max(px, hist.drag.x);
    hist.drag = null;
    g.selRect.setAttribute("visibility", "hidden");
    if (b - a < 8) return;
    const ts = (x) => g.x0 + ((Math.max(g.L, Math.min(g.L + g.PW, x)) - g.L) / g.PW) * (g.x1 - g.x0);
    histZoom(ts(a), ts(b));
  };
  overlay.onpointerleave = () => { if (!hist.drag) histHideTip(); };
  overlay.ondblclick = () => { if (hist.zoom) histResetZoom(); };
  // Keyboard: the arrow keys walk the crosshair, Escape hides it.
  let kx = null;
  overlay.onkeydown = (ev) => {
    const g = hist.geo;
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      kx = (kx == null ? g.L + g.PW : kx) + (ev.key === "ArrowLeft" ? -1 : 1) * g.PW / 60;
      kx = Math.max(g.L, Math.min(g.L + g.PW, kx));
      histShowAt(kx);
      ev.preventDefault();
    } else if (ev.key === "Escape") { kx = null; histHideTip(); }
  };
  overlay.onblur = () => { kx = null; histHideTip(); };
}

// --- free period (Von / Bis) ------------------------------------------------------------
// <input type="datetime-local"> speaks local wall-clock time without a zone: built from
// the local Date parts here, and read back with new Date(...), which also takes it as local.
function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fromLocalInput(value) {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

// The fields always show the period on screen — a preset, a zoom or a typed one alike.
function histFillSpan(d) {
  const from = $("h_from"), to = $("h_to");
  const now = Math.floor(Date.now() / 1000);
  const floor = Math.max(now - d.retention_days * 86400, d.oldest || 0);
  for (const el of [from, to]) {
    el.min = toLocalInput(floor);
    el.max = toLocalInput(now);
  }
  // Not while someone is typing in them: the minute refresh would overwrite the input.
  if (document.activeElement === from || document.activeElement === to) return;
  from.value = toLocalInput(d.from);
  to.value = toLocalInput(d.to);
}

function histApplySpan() {
  const from = fromLocalInput($("h_from").value);
  const to = fromLocalInput($("h_to").value);
  if (from == null || to == null || from >= to) {
    $("h_note").textContent = t("hist.spanInvalid");
    return;
  }
  // The server clamps to "now" and to the retention; the fields then show what it chose.
  histZoom(from, to);
}

function histZoom(from, to) {
  if (to - from < 60) { const mid = (from + to) / 2; from = mid - 30; to = mid + 30; }
  hist.zoom = { from, to };
  histFetch();
}

function histResetZoom() {
  hist.zoom = null;
  histFetch();
}

// --- outage table ---------------------------------------------------------------------------
function renderOutages(d) {
  const tbody = document.querySelector("#h_outages tbody");
  tbody.replaceChildren();
  const list = d.outages || [];
  // An empty period says so in a line of its own: inside a seven-column table the
  // sentence would be cut off by the horizontal scroll on a phone.
  $("h_outages").closest(".table-wrap").hidden = !list.length;
  $("h_noout").hidden = !!list.length;
  if (!list.length) return;
  const pct = (v) => (v == null ? "–" : v + " %");
  for (const e of list) {
    const tr = document.createElement("tr");
    const cells = [
      fmtTime(e.start, true),
      e.name,
      fmtDur(e.duration_s) + (e.ongoing ? " · " + t("hist.ongoing") : ""),
      e.min_runtime == null ? "–" : e.min_runtime + " min",
      pct(e.min_charge),
      pct(e.max_load),
      e.triggered ? t("hist.yes") : t("hist.no"),
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      if (i === 6 && e.triggered) {
        const chip = document.createElement("span");
        chip.className = "chip crit";
        chip.textContent = c;
        td.appendChild(chip);
      } else td.textContent = c;
      tr.appendChild(td);
    });
    tr.title = t("hist.zoomTo");
    tr.onclick = () => {
      // Some room either side, so the switch to battery and back is visible in context.
      const margin = Math.max(300, (e.end - e.start) * 0.25);
      if (d.ups.length > 1) hist.prefs.ups = e.ups_id;
      histSavePrefs();
      histZoom(e.start - margin, e.end + margin);
    };
    tbody.appendChild(tr);
  }
}

// --- static wiring ---------------------------------------------------------------------------
$("h_zoomreset").onclick = histResetZoom;
$("h_apply").onclick = histApplySpan;
for (const id of ["h_from", "h_to"]) {
  $(id).addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); histApplySpan(); } });
}
$("h_info").onchange = () => { hist.prefs.info = $("h_info").checked; histSavePrefs(); histRender(); };
$("h_csv").onclick = () => {
  const a = document.createElement("a");
  a.href = "/api/history.csv?" + histQuery();
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};
let histResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(histResizeTimer);
  histResizeTimer = setTimeout(() => { if (!$("history").hidden && hist.data) histRender(); }, 150);
});
