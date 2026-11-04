"use strict";
/** Вкладка «Настройки»: настройки озвучки, обновления, баны, список мата. */
// --- настройки ---
// Встроенный сервер: бэкенд, модель, число голосов
function runtimeInfo(rt) {
  if (!rt) return "";
  const backend = { cpu: "CPU" }[rt.backend.toLowerCase()] || rt.backend.replace(/\d+$/, "") || "…";
  return T(" — встроенный, {backend}{model}, голосов: {n}", { backend, model: rt.model ? ", " + rt.model : "",
    n: rt.voices });
}
function renderSettings() {
  $("#demo-form").demo_phrase.value = S.settings.demo_phrase;
  const f = $("#settings");
  for (const el of f.elements) {
    if (!el.name || !(el.name in S.settings)) continue;
    if (el.type === "checkbox") el.checked = !!S.settings[el.name]; else el.value = S.settings[el.name];
  }
  const c = S.config;
  $("#server-info").replaceChildren(
    h("div", {}, S.cosy.title + ": ", h("code", {}, S.cosy.url), runtimeInfo(S.cosy.runtime)),
    h("div", {}, T(c.password ? "Интерфейс: {addr}, пароль задан" : "Интерфейс: {addr}, пароль не задан (доступ только с этого ПК)",
      { addr: `${c.bind}:${c.port}` })));
  $("#logout-link").hidden = !c.password;
  $("#cosy-settings").hidden = S.cosy.engine !== "cosyvoice";   // chunk_tokens и max_token_text_ratio — CosyVoice
}
$("#settings").onchange = async e => {
  const el = e.target;
  if (!el.name) return;
  const v = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
  if ((await api("/api/settings", { [el.name]: v })).ok) { S.settings[el.name] = v; toast(T("Сохранено")); }
};
// --- обновления (updates.py): карточка в настройках и плашка под вкладками ---
let updateKey = "";
function renderUpdate() {
  const u = S.update, key = JSON.stringify(u);
  if (key === updateKey) return;
  updateKey = key;
  const l = u.latest;
  $("#update-version").textContent = u.version;
  $("#update-check").value = u.check;
  $("#update-check").disabled = !u.available;
  $("#update-now").disabled = !u.available || u.checking;
  const lines = [];
  if (!u.available) {}   // новую версию проверяет только программа для Windows: кнопки неактивны, без пояснения
  else if (u.checking) lines.push(h("div", { class: "muted" }, T("Проверяю…")));
  else {
    if (u.newer) lines.push(h("div", {},
      h("b", { class: "good" }, T("Доступна версия {version}", { version: l.version })),
      l.date ? " " + T("от {date}", { date: l.date }) : "", " · ",
      h("a", { href: l.url, target: "_blank", rel: "noopener" }, T("Скачать на GitHub")),
      l.notes ? h("details", {}, h("summary", { class: "muted" }, T("Что нового")), h("pre", { class: "notes" }, l.notes))
        : null));
    else if (l) lines.push(h("div", {}, T("Это последняя версия.")));
    if (u.error) lines.push(h("div", { class: "bad" }, T("Не удалось проверить: {error}", { error: u.error })));
    if (u.checked) lines.push(h("div", { class: "muted" }, T("Последняя проверка: {date}",
      { date: new Date(u.checked * 1000).toLocaleString(LOCALE, { day: "numeric", month: "long", hour: "2-digit",
        minute: "2-digit" }) })));
  }
  $("#update-status").replaceChildren(...lines);

  const show = u.available && u.newer && l.version !== u.dismissed;
  $("#update-banner").hidden = !show;
  $("#update-banner").replaceChildren(...!show ? [] : [
    T("Вышла новая версия StreamTTS: {version}.", { version: l.version }), " ",
    h("a", { href: "#settings" }, T("Подробнее")), " · ",
    h("a", { href: l.url, target: "_blank", rel: "noopener" }, T("Скачать")), " · ",
    h("a", { href: "#", onclick: e => { e.preventDefault(); api("/api/update/dismiss", { version: l.version }); } },
      T("Скрыть до следующей версии"))]);
}
$("#update-check").onchange = async e => {
  if ((await api("/api/settings", { update_check: e.target.value })).ok) toast(T("Сохранено"));
};
$("#update-now").onclick = async () => {
  $("#update-now").disabled = true;
  const j = await api("/api/update/check");
  if (!j.ok) { $("#update-now").disabled = false; return; }
  S.update = j.update;
  renderUpdate();
  const u = j.update;
  if (u.error) toast(T("Не удалось проверить: {error}", { error: u.error }), true);
  else toast(u.newer ? T("Доступна версия {version}", { version: u.latest.version }) : T("Это последняя версия."));
};

function renderStreamNote() {
  const rtf = S.engine.rtf, note = $("#stream-note");
  note.hidden = !(rtf > 0.9);
  if (!note.hidden) note.textContent = T("Скорость синтеза сейчас {speed}: стриминг будет заикаться, лучше его выключить.",
    { speed: fmtRtf(rtf) });
}
function renderBans() {
  $("#bans").replaceChildren(...S.bans.map(b => h("li", {}, b, h("button", { title: T("Разбанить"),
    onclick: async () => { await api("/api/ban", { login: b, on: false }); reload(); } }, "✕"))));
  if (!S.bans.length) $("#bans").append(h("li", { class: "muted", style: "padding:2px 12px" }, T("никого")));
}
$("#ban").onsubmit = async e => {
  e.preventDefault();
  if ((await api("/api/ban", { login: e.target.login.value, on: true })).ok) { e.target.reset(); reload(); }
};
let profLoaded = false;
async function loadProfanity() {
  if (profLoaded) return;
  const r = await fetch("/api/profanity");
  if (r.ok) { $("#prof").value = (await r.json()).text; profLoaded = true; }
}
$("#prof-save").onclick = async () => {
  const r = await api("/api/profanity", { text: $("#prof").value, sample: $("#prof-sample").value });
  if (r.ok) { $("#prof-out").textContent = r.sample ? "→ " + r.sample : T("Сохранено"); toast(T("Список корней сохранён")); }
};
