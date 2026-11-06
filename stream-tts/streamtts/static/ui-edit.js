"use strict";
/** В разработке: режим правки строк интерфейса (сервер с STREAM_TTS_UI_EDITS, запуск — stream-tts/ui-edit.py,
 *  docs/stream-tts.md «Правка текста интерфейса»). Кнопка «✎ Текст» внизу слева включает режим: щелчок по надписи
 *  открывает окно с её строками (текст, подсказка title, placeholder, пункты списка), правка уходит на сервер
 *  (POST /api/ui-edit) и сразу видна на странице. Alt+щелчок открывает окно и без режима. Строки, которых не видно
 *  (ошибки, подтверждения), — поиском по словарю в панели. Пустой вариант — строку убрать.
 *  Исходную строку узла дают UI_NODES и UI_KEYS из i18n.js; правки — общий объект EDITS (window.UI_EDITS). */
(() => {
  const ATTRS = { title: "подсказка", placeholder: "подсказка в поле", "aria-label": "aria-label", value: "надпись" };
  const LETTER = /\p{L}/u, TEMPLATE = /\{\w+\}/;
  const own = el => el.closest("[data-ui-edit]");
  const current = k => Object.hasOwn(EDITS, k) ? EDITS[k].text : k;
  // пустой текст — только по узлу: иначе убранная строка («» в правках) совпала бы со всеми пробелами между тегами
  const keyOf = (node, text) => UI_NODES.get(node) || (norm(text) && (UI_KEYS.get(norm(text)) || norm(text)));
  let on = false, hovered = null, catalog = [];

  fetch("/static/en.json").then(r => r.json()).then(d => { catalog = Object.keys(d); }).catch(() => {});

  // --- строки элемента ---
  const keyOfEl = el => UI_NODES.get(el) || UI_KEYS.get(norm(el.innerHTML)) || norm(el.innerHTML);
  const attrsOf = e => Object.keys(ATTRS).filter(a => a !== "value" || e.matches("input[type=button], input[type=submit]"));

  function rowsFor(target) {
    const rows = new Map();   // ключ → {key, kind}: одинаковые строки в одном окне — одной строкой
    const add = (key, kind) => { if (key && !rows.has(key)) rows.set(key, { key, kind }); };
    const addText = n => add(keyOf(n, n.data), "текст");
    const addAttrs = e => {
      for (const a of attrsOf(e)) { const v = e.getAttribute(a); if (v && LETTER.test(v)) add(keyOf(null, v), ATTRS[a]); }
    };
    const dt = target.closest("[data-t]");
    if (dt) add(keyOfEl(dt), "абзац (HTML)");
    else {
      // сам элемент, а если в нём нет строк — до четырёх предков вверх
      for (let e = target, i = 0; e && e !== document.body && i < 5 && !rows.size; e = e.parentElement, i++) {
        for (const n of e.childNodes) if (n.nodeType === Node.TEXT_NODE && LETTER.test(n.data)) addText(n);
        addAttrs(e);
        if (e.tagName === "SELECT") for (const o of e.options) for (const n of o.childNodes) if (LETTER.test(n.data)) addText(n);
      }
    }
    const tip = target.closest("[title]");   // подсказка предка видна при наведении на этот элемент
    if (tip) addAttrs(tip);
    return [...rows.values()];
  }

  /** Новый вариант строки во все места страницы, где она показана. Строки с подстановками на странице уже с
   *  подставленными значениями: они обновятся при следующей отрисовке. */
  function applyAll(key, t) {
    if (TEMPLATE.test(key)) return;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const p = n.parentElement;
      if (p.closest("script, style, textarea, [data-t], [data-ui-edit]") || keyOf(n, n.data) !== key) continue;
      n.data = keepSpace(n.data, t);
      UI_NODES.set(n, key);
    }
    for (const el of document.querySelectorAll("[data-t]")) if (keyOfEl(el) === key) { el.innerHTML = t; UI_NODES.set(el, key); }
    for (const el of document.querySelectorAll(Object.keys(ATTRS).map(a => `[${a}]`).join(","))) {
      if (own(el)) continue;
      for (const a of attrsOf(el)) { const v = el.getAttribute(a); if (v && keyOf(null, v) === key) el.setAttribute(a, t); }
    }
  }

  // --- окно правки ---
  const dlg = h("dialog", { id: "ui-edit-dlg", "data-ui-edit": "", translate: "no" });
  document.body.append(dlg);

  function open(rows) {
    if (!rows.length) { toast("Здесь нет строк интерфейса"); return; }
    const fields = rows.map(r => {
      const e = EDITS[r.key];
      const text = h("textarea", { rows: Math.min(8, Math.max(2, Math.ceil(current(r.key).length / 70))) });
      text.value = current(r.key);
      const note = h("input", { placeholder: "заметка (необязательно)", value: e?.note || "" });
      const unknown = !catalog.includes(r.key) && !e;
      return { r, text, note, el: h("div", { class: "ui-edit-row" },
        h("div", { class: "ui-edit-head" }, h("b", {}, r.kind), e ? h("span", { class: "ui-edit-mark" }, "изменено") : null,
          e ? h("button", { type: "button", onclick: () => save([{ r, text: null }]) }, "Вернуть исходную") : null),
        e ? h("div", { class: "ui-edit-orig" }, "было: " + r.key) : null,
        unknown ? h("div", { class: "muted" }, "Нет в словаре: склеена из частей или с подставленными значениями.") : null,
        text, note) };
    });
    dlg.replaceChildren(h("h3", {}, "Правка текста"),
      h("p", { class: "muted" }, "Пустое поле — убрать строку. Строки с {подстановками} обновятся при следующей отрисовке."),
      ...fields.map(f => f.el),
      h("div", { class: "row end-x" },
        h("button", { type: "button", onclick: () => dlg.close() }, "Отмена"),
        h("button", { type: "button", class: "primary", onclick: () => save(fields.map(f => ({ r: f.r, text: f.text.value, note: f.note.value }))
          .filter(c => c.text !== current(c.r.key) || c.note !== (EDITS[c.r.key]?.note || ""))) }, "Сохранить")));
    dlg.showModal();
    fields[0].text.focus();
  }

  async function save(changes) {
    const where = $("#tabs button.on")?.textContent || "";
    let j = null;
    for (const c of changes) {
      j = await api("/api/ui-edit", { key: c.r.key, text: c.text, note: c.note || "", where });
      if (!j.ok) return;
    }
    if (j) {
      for (const k of Object.keys(EDITS)) delete EDITS[k];
      Object.assign(EDITS, j.edits);
      uiKeys();
      for (const c of changes) applyAll(c.r.key, current(c.r.key));
      toast(`Сохранено: ${changes.length}`);
      list();
    }
    dlg.close();
  }

  // --- панель: включение режима, поиск по словарю, список правок ---
  const toggle = h("button", { type: "button", onclick: () => setOn(!on) }, "✎ Текст");
  const count = h("span", { class: "muted" });
  const search = h("input", { type: "search", placeholder: "найти строку…", oninput: () => list() });
  const items = h("div", { class: "ui-edit-list" });
  const body = h("div", { hidden: true }, search, items);
  document.body.append(h("div", { id: "ui-edit-panel", "data-ui-edit": "", translate: "no" },
    h("div", { class: "row" }, toggle, count), body));

  function list() {
    const n = Object.keys(EDITS).length;
    count.textContent = n ? `правок: ${n}` : "";
    const q = norm(search.value).toLowerCase();
    const keys = q ? [...new Set([...Object.keys(EDITS), ...catalog])].filter(k => k.toLowerCase().includes(q) ||
      (EDITS[k]?.text || "").toLowerCase().includes(q)).slice(0, 40) : Object.keys(EDITS).reverse();
    items.replaceChildren(...keys.map(k => h("button", { type: "button", class: EDITS[k] ? "edited" : "",
      title: k, onclick: () => open([{ key: k, kind: "строка" }]) },
      EDITS[k] ? (EDITS[k].text || "∅ убрано") : k)));
    if (!keys.length) items.append(h("div", { class: "muted" }, q ? "ничего не нашлось" : "правок пока нет"));
  }

  function setOn(v) {
    on = v;
    toggle.classList.toggle("primary", on);
    body.hidden = !on;
    document.body.classList.toggle("ui-edit-on", on);
    if (!on) unhover();
    if (on) list();
  }

  // --- перехват щелчков: в режиме правки элементы страницы не срабатывают ---
  const active = e => (on || e.altKey) && !own(e.target) && !dlg.open;
  // класс подсветки снимается до чтения строк: иначе он попадёт в innerHTML абзаца data-t, то есть в ключ
  const unhover = () => {
    if (hovered) { hovered.classList.remove("ui-edit-hover"); if (!hovered.classList.length) hovered.removeAttribute("class"); }
    hovered = null;
  };
  document.addEventListener("mouseover", e => {
    if (!on) return;
    unhover();
    if (own(e.target)) return;
    hovered = e.target;
    hovered.classList.add("ui-edit-hover");
  }, true);
  // pointerdown приходит и от выключенных кнопок, click — нет
  document.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !active(e)) return;
    e.preventDefault(); e.stopPropagation();
    unhover();
    open(rowsFor(e.target));
  }, true);
  for (const type of ["mousedown", "click", "dblclick"]) {
    document.addEventListener(type, e => {
      if (on && !own(e.target) || e.altKey && !own(e.target)) { e.preventDefault(); e.stopPropagation(); }
    }, true);
  }
  document.addEventListener("keydown", e => { if (e.key === "Escape" && on && !dlg.open) setOn(false); });
  list();
})();
