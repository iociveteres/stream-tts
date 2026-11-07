"use strict";
/** Админка stream-tts: общее для всех вкладок — DOM (h, $), запросы (api, reload), вкладки, WebSocket
 *  состояния, подсказки, шапка. Скрипты вкладок — admin-*.js, порядок загрузки — в admin.html: этот первый,
 *  admin-start.js последний; область у них общая (классические скрипты). */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/** DOM без innerHTML: всё, что пришло от зрителей, попадает только в текстовые узлы. */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const svg = (tag, attrs) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
};
const fmtClock = ts => new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtNum = (x, d = 1) => x == null ? "—" : Number(x).toLocaleString(LOCALE, { maximumFractionDigits: d });

let S = null;

function toast(msg, bad) {
  const el = h("div", { class: bad ? "bad" : "" }, msg);
  $("#toast").append(el);
  setTimeout(() => el.remove(), bad ? 7000 : 2500);
}

/** Подтверждение окном на странице: confirm() во встроенных браузерах (панель Claude, некоторые оболочки)
 *  молча возвращает false, и действие не выполнялось. */
function ask(msg, yes = T("Да"), danger = false) {
  const dlg = $("#ask");
  $("#ask-text").textContent = msg;
  $("#ask-yes").textContent = yes;
  $("#ask-yes").className = danger ? "danger" : "primary";
  dlg.showModal();
  // Ответ — по кнопкам, а не по событию close: в панели браузера Claude оно приходило не всегда
  return new Promise(res => {
    const done = v => { dlg.oncancel = $("#ask-yes").onclick = $("#ask-no").onclick = null; dlg.close(); res(v); };
    $("#ask-yes").onclick = () => done(true);
    $("#ask-no").onclick = () => done(false);
    dlg.oncancel = e => { e.preventDefault(); done(false); };   // Esc
  });
}

async function api(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login"; return { ok: false }; }
    const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    // перезапуск сервера синтеза выбросит несохранённые результаты обучения LoRA (train.py): спросить и повторить
    if (j.confirm && !(body && body.force)) {
      if (await ask(j.error, T("Продолжить"), true)) return api(path, { ...body, force: true });
      return j;
    }
    if (!j.ok) toast(j.error || T("Ошибка"), true);
    return j;
  } catch (e) { toast(T("Сервер недоступен: {e}", { e: e.message }), true); return { ok: false }; }
}
async function reload() {
  const r = await fetch("/api/state");
  if (r.status === 401) { location.href = "/login"; return; }
  S = await r.json();
  trackLoad(S.load);
  renderAll();
}

// --- вкладки ---
function showTab(t) {
  if (!$("#tab-" + t)) t = "queue";
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + t));
  history.replaceState(null, "", "#" + t);
  if (t === "stats") loadStats();
  if (t === "settings") loadProfanity();
}
$$("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
addEventListener("hashchange", () => S && showTab(location.hash.slice(1)));

// --- WebSocket: состояние раз в 2 с и сразу после изменений ---
let wsOnline = false;
let authChanged = false;   // эта вкладка сменила пароль: закрытие сокета 4001 — не повод перезагружаться
function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    wsOnline = true;
    trackLoad(m.load);
    if (m.settings) { S = m; authChanged = false; renderAll(); }
    else if (S) {
      Object.assign(S, m);
      if (m.voices) { renderVoices(); renderTestVoices(); }   // набор установлен или готов пример
      renderLive();
    }
  };
  ws.onclose = e => {
    // 4001 — сменился пароль (web/__init__.py, post_password): вкладка, которая его меняла, уже с новой cookie и
    // переподключается за полным состоянием; остальные перезагружаются — на вход или с новыми настройками доступа
    if (e.code === 4001 && !authChanged) { location.reload(); return; }
    wsOnline = false; if (S) renderHeader(); setTimeout(connect, e.code === 4001 ? 0 : 2000);
  };
}

function renderAll() {
  renderHints(); renderSettings(); renderVoices(); renderBans(); renderGuide(); renderReward(); renderSources(); renderTestVoices();
  renderLive();
}
function renderLive() {
  renderHeader(); renderQueue(); renderTunnel(); renderPages(); renderLogin(); renderRewardState(); renderDemoProgress(); renderLiveStats();
  renderStreamNote(); renderServer(); renderDownloads(); renderSets(); renderReplyNote(); renderUpdate(); edSync(); trSync();
}

// --- подсказки: <… data-hint="id"> закрываются крестиком, «Показать подсказки» в шапке возвращает все.
// Скрытые — в настройках (hidden_hints), а не в браузере: админку открывают из разных мест.
const hiddenHints = () => (S && S.settings.hidden_hints) || [];
for (const el of $$("[data-hint]")) el.prepend(h("button", { type: "button", class: "icon hint-x",
  title: T("Скрыть подсказку"), "aria-label": T("Скрыть подсказку"),
  onclick: () => setHints([...hiddenHints(), el.dataset.hint]) }, "✕"));
function renderHints() {
  const off = new Set(hiddenHints());
  let any = false;
  for (const el of $$("[data-hint]")) {
    const x = off.has(el.dataset.hint);
    el.classList.toggle("hint-off", x);
    any ||= x;
  }
  $("#hints-show").hidden = !any;
}
async function setHints(ids) {
  if ((await api("/api/settings", { hidden_hints: ids })).ok) { S.settings.hidden_hints = ids; renderHints(); }
}
$("#hints-show").onclick = () => setHints([]);

// --- шапка ---
function pill(text, cls, title) { return h("span", { class: "pill", title }, h("i", { class: "dot " + cls }), text); }
function renderHeader() {
  const t = S.twitch, c = S.cosy, ov = S.engine.overlay;
  const chat = t.mode === "chat", login = chat ? t.chat.channel : t.login;
  $("#channel").textContent = login ? "· " + login : "";
  const tw = chat ? { ok: [T("чат: только чтение"), "good"], connecting: [T("чат: подключение"), "warn"],
    not_found: [T("чат: канал не найден"), "bad"], off: [T("чат: канал не задан"), "bad"] }[t.chat.state]
  : { ok: ["Twitch", "good"], connecting: [T("Twitch: подключение"), "warn"],
    device: [T("Twitch: ждёт входа"), "warn"], expired: [T("Twitch: вход истёк"), "bad"],
    logged_out: [T("Twitch: нет входа"), "bad"], no_client: [T("Twitch: нет Client ID"), "bad"] }[t.state] || [t.state, ""];
  const pills = [pill(...tw, chat ? t.chat.error : t.error)];
  if (t.state === "ok") pills.push(pill("EventSub", t.ws === "connected" ? "good" : "warn", t.ws));
  pills.push(pill(c.ok ? (c.busy ? T("{engine}: синтез", { engine: c.title }) : c.title)
    : T("{engine} недоступен", { engine: c.title }), c.ok ? "good" : "bad", c.ok ? c.url + runtimeInfo(c.runtime) : c.error));
  const tr = c.training;   // обучение LoRA на сервере (train.js): щелчок — его страница
  if (tr && tr.state !== "idle" && c.engine === "espeech") {
    const busy = tr.state === "running" || tr.state === "stopping";
    pills.push(h("button", { type: "button", class: "pill", title: T("Обучение LoRA голоса {voice}", { voice: tr.voice }),
      onclick: () => trOpen(tr.voice) }, h("i", { class: "dot " + (busy ? "warn" : tr.state === "error" ? "bad" : "good") }),
      busy ? T("LoRA {voice}: {pct}%", { voice: tr.voice, pct: tr.steps ? Math.floor(100 * tr.step / tr.steps) : 0 })
        : T("LoRA {voice}: готово", { voice: tr.voice })));
  }
  pills.push(pill(!ov.active ? T("оверлей не подключён") : ov.ready ? T("оверлей") : T("оверлей ждёт клика"),
    !ov.active ? "bad" : ov.ready ? "good" : "warn"));
  if (!wsOnline) pills.push(pill(T("нет связи с сервисом"), "bad"));
  $("#pills").replaceChildren(...pills);
  $("#enabled").checked = S.engine.enabled;
  $("#test-accent").hidden = !c.capabilities.accent;      // «Как прочитает» — у моделей с POST /accent (ESpeech)
  $("#ed-open").hidden = c.engine !== "cosyvoice" && !c.capabilities.extract;   // голос из записи (editor.js)

  const banner = [];
  if (t.state === "expired") banner.push(T("Вход в Twitch истёк: войдите заново на вкладке Twitch. Пока входа нет, " +
    "активации не обрабатываются, баллы за них вернутся после входа."));
  if (c.switching) banner.push(T("Смена модели синтеза: сервер перезапускается, озвучка ждёт в очереди."));
  else if (c.ok === false && S.setup && !engineReady(c.picked))
    banner.push(h("span", {}, T("Модель синтеза не скачана: скачайте её на вкладке "),
      h("a", { href: "#downloads" }, T("«Загрузки»")), "."));
  else if (c.ok === false && c.runtime && c.runtime.unloaded)
    banner.push(h("span", {}, T("Модель синтеза выгружена, озвучка не работает. Загрузить — на вкладке "),
      h("a", { href: "#stats" }, T("«Статистика»")), "."));
  else if (c.ok === false) banner.push(T("{engine} недоступен ({url}): {error}", { engine: c.title, url: c.url,
    error: c.error }));
  $("#banner").hidden = !banner.length;
  $("#banner").replaceChildren(...banner.map(b => h("div", {}, b)));
}
$("#enabled").onchange = e => api("/api/enabled", { on: e.target.checked });
