"use strict";
/** Перевод страниц (stream-tts/streamtts/i18n.py): ключ — русская строка, словарь window.I18N сервер вставляет
 *  в страницу, когда язык не русский. T() — для строк из кода, подстановки {имя} как в Python.
 *  translatePage() — для разметки: текстовые узлы, title/placeholder/aria-label/value, а у элементов с data-t
 *  innerHTML целиком (абзацы со ссылками и <code>). translate="no" — не трогать. */
const LANG = document.documentElement.lang || "ru";
const LOCALE = LANG === "en" ? "en-GB" : "ru-RU";
const I18N_MISSING = new Set();   // ключи без перевода: для проверки из консоли
const RU = /[а-яё]/i;

// В разработке: режим правки строк (ui-edit.js, сервер с STREAM_TTS_UI_EDITS) — правки поверх русских строк.
// UI_KEYS: показанный текст → исходная строка (правленые и с подстановками), UI_NODES: узел или data-t → исходная.
const EDITS = LANG === "ru" ? window.UI_EDITS || null : null;
const UI_KEYS = new Map(), UI_NODES = new WeakMap();
const uiKeys = () => { for (const [k, e] of Object.entries(EDITS || {})) if (e.text) UI_KEYS.set(e.text.replace(/\s+/g, " ").trim(), k); };
uiKeys();   // строки, которые сервер подменил сам (tr(): ошибки, настройки модели), приходят уже правлеными

const norm = s => s.replace(/\s+/g, " ").trim();
function lookup(s) {
  const k = norm(s);
  if (EDITS) {
    if (!Object.hasOwn(EDITS, k)) return null;
    UI_KEYS.set(norm(EDITS[k].text), k);
    return EDITS[k].text;
  }
  if (window.I18N && Object.hasOwn(window.I18N, k)) return window.I18N[k];
  if (RU.test(k)) I18N_MISSING.add(k);
  return null;
}
const keepSpace = (s, r) => s.match(/^\s*/)[0] + r + s.match(/\s*$/)[0];   // «Канал: » + имя
function T(s, kw) {
  let r = s;
  if ((LANG !== "ru" || EDITS) && s != null) { const t = lookup(String(s)); if (t != null) r = keepSpace(String(s), t); }
  if (kw) {
    r = String(r).replace(/\{(\w+)\}/g, (m, k) => k in kw ? kw[k] : m);
    if (EDITS) UI_KEYS.set(norm(r), norm(String(s)));
  }
  return r;
}

const ATTRS = ["title", "placeholder", "aria-label", "value"];
function translatePage(root = document.documentElement) {
  if (LANG === "ru" && !EDITS) return;
  const skip = el => el.closest("script, style, textarea, [translate=no]");
  for (const el of root.querySelectorAll("[data-t]")) {
    if (skip(el)) continue;
    const k = norm(el.innerHTML), r = lookup(k);
    if (r != null) { el.innerHTML = r; if (EDITS) UI_NODES.set(el, k); }
  }
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!RU.test(n.data) || skip(n.parentElement) || n.parentElement.closest("[data-t]")) continue;
    const k = norm(n.data), r = lookup(k);
    if (r != null) { n.data = keepSpace(n.data, r); if (EDITS) UI_NODES.set(n, k); }
  }
  for (const el of root.querySelectorAll(ATTRS.map(a => `[${a}]`).join(","))) {
    if (skip(el)) continue;
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v && RU.test(v)) { const r = lookup(v); if (r != null) el.setAttribute(a, r); }
    }
  }
}
translatePage();
