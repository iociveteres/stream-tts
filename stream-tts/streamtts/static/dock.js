"use strict";
/** Док-панель OBS (/dock): озвучка вкл/выкл, пауза, скип всей очереди, общая громкость, очередь.
 *  Состояние — сокет /api/ws?dock=1 (web/state.py, dock_state): только очередь, без тяжёлого полного состояния.
 *  Самостоятельная страница: скрипты админки (admin.js) сюда не грузятся. */
const $ = s => document.querySelector(s);

/** DOM без innerHTML: ники и тексты зрителей — только в текстовые узлы. */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}

// Иконки — свои пути (контур 24×24), разметка постоянная
const ICONS = {
  power: '<path d="M12 3v9"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M10.3 2.6h3.4l.5 2.6 1.9.8 2.2-1.5 2.4 2.4-1.5 2.2.8 1.9 2.6.5v3.4l-2.6.5-.8 1.9 1.5 2.2-2.4 2.4-2.2-1.5-1.9.8-.5 2.6h-3.4l-.5-2.6-1.9-.8-2.2 1.5-2.4-2.4 1.5-2.2-.8-1.9-2.6-.5v-3.4l2.6-.5.8-1.9-1.5-2.2 2.4-2.4 2.2 1.5 1.9-.8z"/>',
  pause: '<rect x="6" y="4.5" width="4" height="15" rx="1"/><rect x="14" y="4.5" width="4" height="15" rx="1"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  skip: '<path d="M5 5l10 7-10 7z"/><path d="M19 5v14"/>',
  volume: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
  mute: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
};
function icon(name) {
  const t = document.createElement("template");
  t.innerHTML = `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
  return t.content.firstChild;
}
document.querySelectorAll("[data-icon]").forEach(el => el.append(icon(el.dataset.icon)));

let S = null, clockSkew = 0, ws = null, connected = false;

function toast(msg) {
  const el = h("div", { class: "bad" }, msg);
  $("#toast").append(el);
  setTimeout(() => el.remove(), 6000);
}

async function api(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login?next=/dock"; return { ok: false }; }
    const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    if (!j.ok) toast(j.error || T("Ошибка"));
    return j;
  } catch (e) { toast(T("Сервер недоступен: {e}", { e: e.message })); return { ok: false }; }
}

// --- подтверждение вторым нажатием: confirm() во встроенном браузере OBS ненадёжен, а скип возвращает баллы ---
let armed = null;   // {id, timer}
function twice(id, fn) {
  if (armed && armed.id === id) { clearTimeout(armed.timer); armed = null; render(); return fn(); }
  if (armed) clearTimeout(armed.timer);
  armed = { id, timer: setTimeout(() => { armed = null; render(); }, 3000) };
  render();
}
const isArmed = id => !!armed && armed.id === id;

// --- громкость: пока ползунок тянут, состояние с сервера его не двигает ---
let dragging = 0, volTimer = null, volSent = null;
function sendVolume(v) {
  if (v === volSent) return;
  volSent = v;
  api("/api/volume", { value: v });
}
$("#volume").addEventListener("input", e => {
  const v = Number(e.target.value);
  dragging = Date.now();
  showVolume(v);
  if (!volTimer) volTimer = setTimeout(() => { volTimer = null; sendVolume(Number($("#volume").value)); }, 120);
});
$("#volume").addEventListener("change", e => { dragging = Date.now(); sendVolume(Number(e.target.value)); });
function showVolume(v) {
  $("#vval").textContent = v + "%";
  $("#vicon").replaceChildren(icon(v ? "volume" : "mute"));
}

// --- кнопки ---
$("#enabled").onclick = () => {
  const on = !S.engine.enabled;
  // выключение очищает очередь с возвратом баллов: при непустой очереди — вторым нажатием
  if (!on && S.engine.queue.length) return twice("enabled", () => api("/api/enabled", { on }));
  api("/api/enabled", { on });
};
$("#pause").onclick = () => api("/api/pause", { on: !S.engine.paused });
$("#clear").onclick = () => S.engine.queue.length && twice("clear", () => api("/api/clear"));

// --- отрисовка ---
const STATES = { queued: T("ждёт"), synth: T("синтез"), ready: T("готово") };
function ago(age) {
  // age — секунды на момент снимка (S.ts); clockSkew — разница часов браузера и сервера
  const sec = Math.max(0, Date.now() / 1000 - clockSkew - S.ts + age);
  if (sec < 60) return T("только что");
  if (sec < 3600) return T("{n} мин назад", { n: Math.floor(sec / 60) });
  return T("{n} ч назад", { n: Math.floor(sec / 3600) });
}
const opened = new Set();   // развёрнутые тексты: перерисовка их не сворачивает

function card(it) {
  const now = it.state === "playing", e = S.engine;
  const tag = now ? h("span", { class: "tag now" }, h("span", { class: "eq" + (e.paused ? "" : " on") },
    h("i"), h("i"), h("i")), T("Сейчас")) : h("span", { class: "tag " + it.state }, STATES[it.state] || it.state);
  const acts = h("div", { class: "acts" },
    now ? h("button", { type: "button", title: e.paused ? T("Продолжить") : T("Пауза"),
      "aria-label": e.paused ? T("Продолжить") : T("Пауза"),
      onclick: () => api("/api/pause", { on: !e.paused }) }, icon(e.paused ? "play" : "pause")) : null,
    h("button", { type: "button", title: T("Убрать и вернуть баллы"), "aria-label": T("Убрать и вернуть баллы"),
      onclick: () => api("/api/remove", { id: it.id }) }, icon("x")));
  const text = h("div", { class: "text" + (opened.has(it.id) ? " open" : ""), title: it.text,
    onclick: ev => { ev.currentTarget.classList.toggle("open");
      opened.has(it.id) ? opened.delete(it.id) : opened.add(it.id); } }, it.text);
  return h("div", { class: "item" + (now ? " now" : "") },
    h("div", { class: "head" }, tag, h("span", { class: "who" }, it.display),
      h("span", { class: "meta" }, `${ago(it.age)} · ${it.voice}`), acts),
    text);
}

function notice() {
  const e = S.engine, ov = e.overlay;
  if (!connected) return [T("Нет связи с stream-tts, переподключение…"), "bad"];
  if (S.cosy && S.cosy.ok === false) return [T("Сервер синтеза недоступен: {e}", { e: S.cosy.error || "—" }), "bad"];
  if (!ov.active) return [T("Источник в OBS не подключён, фразы ждут в очереди"), "bad"];
  if (!ov.ready) return [T("Подключён, но звук не включён: кликните «Включить звук» в окне оверлея."), "warn"];
  return null;
}

function render() {
  if (!S) return;
  const e = S.engine, q = e.queue;
  const en = $("#enabled");
  en.className = (e.enabled ? "on" : "") + (isArmed("enabled") ? " confirm" : "");
  en.replaceChildren(icon("power"), h("span", {}, isArmed("enabled") ? T("Выключить и очистить?")
    : e.enabled ? T("Озвучка вкл") : T("Озвучка выкл")));
  const p = $("#pause");
  p.classList.toggle("paused", e.paused);
  p.replaceChildren(icon(e.paused ? "play" : "pause"), h("span", {}, e.paused ? T("Продолжить") : T("Пауза")));
  const c = $("#clear");
  c.classList.toggle("confirm", isArmed("clear"));
  c.disabled = !q.length;
  c.replaceChildren(icon("skip"), h("span", {}, isArmed("clear") ? T("Точно?") : T("Скип все")),
    h("span", { class: "badge" }, q.length));
  if (Date.now() - dragging > 1500) {
    $("#volume").value = e.volume;
    showVolume(e.volume);
  }
  const n = notice();
  $("#notice").replaceChildren(n ? h("p", { class: "notice n-" + n[1] }, n[0]) : "");
  // в счётчике — кто ждёт, текущая фраза отдельно (карточка «Сейчас»)
  $("#qn").textContent = q.filter(i => i.state !== "playing").length;
  for (const id of opened) if (!q.some(i => i.id === id)) opened.delete(id);
  $("#queue").replaceChildren(...q.map(card));
  $("#empty").hidden = q.length > 0;
}
setInterval(render, 15000);   // «только что» → «1 мин назад»

// --- состояние ---
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws?dock=1");
  ws.onopen = () => { connected = true; };
  ws.onmessage = m => {
    S = JSON.parse(m.data);
    clockSkew = Date.now() / 1000 - S.ts;
    connected = true;
    render();
  };
  ws.onclose = e => {
    if (e.code === 4001) return location.reload();   // сменили пароль: со старой cookie — на страницу входа
    connected = false;
    render();
    // без входа сокет не откроется вовсе (401): проверить и уйти на страницу входа
    fetch("/api/state", { method: "HEAD" }).then(r => { if (r.status === 401) location.href = "/login?next=/dock"; })
      .catch(() => {});
    setTimeout(connect, 2000);
  };
}
connect();
