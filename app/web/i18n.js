"use strict";
// Tiny i18n runtime (no build step, works offline / file://).
// Dictionaries register themselves on window.I18N (i18n/<lang>.js, loaded before this
// file). Language resolution is automatic only: browser language prefix -> "en".
// Adding a language = one dictionary file + one <script> tag in index.html.
// Copyright 2026 Florian Finder

window.I18N = window.I18N || {};

function resolveLang() {
  const nav = (navigator.language || "en").toLowerCase();
  for (const code of Object.keys(I18N)) {
    if (nav === code || nav.startsWith(code + "-")) return code;
  }
  return "en";
}

const LANG = resolveLang();

// t("key") / t("key", {name: value}) — falls back to English, then to the key itself.
// Values are strings with {name} placeholders; a value may also be a function(params).
function t(key, params) {
  let v = (I18N[LANG] && I18N[LANG][key]);
  if (v === undefined) v = (I18N.en && I18N.en[key]);
  if (v === undefined) { console.warn("i18n: missing key", key); return key; }
  if (typeof v === "function") return v(params || {});
  if (params) {
    for (const [k, val] of Object.entries(params)) {
      v = v.split("{" + k + "}").join(String(val));
    }
  }
  return v;
}

// Translate the static DOM: data-i18n (textContent), data-i18n-title (title attr),
// data-i18n-placeholder, data-i18n-html (innerHTML — only for trusted dictionary
// strings containing markup). Also wires manual deep links (data-manual="anchor")
// to the language-specific manual file and sets <html lang>.
function applyTranslations(root) {
  const r = root || document;
  r.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  r.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
  r.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  r.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  const manual = t("manual.file");
  r.querySelectorAll("[data-manual]").forEach((el) => {
    const anchor = el.dataset.manual;
    el.href = "/" + manual + (anchor ? "#" + anchor : "");
  });
  document.documentElement.lang = LANG;
}

document.addEventListener("DOMContentLoaded", () => {
  applyTranslations();
});
