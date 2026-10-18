"use strict";
/** Перевод страниц (stream-tts/streamtts/i18n.py): ключ — русская строка, словарь window.I18N сервер вставляет
 *  в страницу, когда язык не русский. T() — для строк из кода, подстановки {имя} как в Python.
 *  translatePage() — для разметки: текстовые узлы, title/placeholder/aria-label/value, а у элементов с data-t
 *  innerHTML целиком (абзацы со ссылками и <code>). translate="no" — не трогать. */
const LANG = document.documentElement.lang || "ru";
const LOCALE = LANG === "en" ? "en-GB" : "ru-RU";
const I18N_MISSING = new Set();   // ключи без перевода: для проверки из консоли
const RU = /[а-яё]/i;

const norm = s => s.replace(/\s+/g, " ").trim();
function lookup(s) {
  const k = norm(s);
  if (window.I18N && Object.hasOwn(window.I18N, k)) return window.I18N[k];
  if (RU.test(k)) I18N_MISSING.add(k);
  return null;
}
const keepSpace = (s, r) => s.match(/^\s*/)[0] + r + s.match(/\s*$/)[0];   // «Канал: » + имя
function T(s, kw) {
  let r = s;
  if (LANG !== "ru" && s != null) { const t = lookup(String(s)); if (t != null) r = keepSpace(String(s), t); }
  if (kw) r = String(r).replace(/\{(\w+)\}/g, (m, k) => k in kw ? kw[k] : m);
  return r;
}

const ATTRS = ["title", "placeholder", "aria-label", "value"];
function translatePage(root = document.documentElement) {
  if (LANG === "ru") return;
  const skip = el => el.closest("script, style, textarea, [translate=no]");
  for (const el of root.querySelectorAll("[data-t]")) {
    if (skip(el)) continue;
    const r = lookup(el.innerHTML);
    if (r != null) el.innerHTML = r;
  }
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!RU.test(n.data) || skip(n.parentElement) || n.parentElement.closest("[data-t]")) continue;
    const r = lookup(n.data);
    if (r != null) n.data = keepSpace(n.data, r);
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
