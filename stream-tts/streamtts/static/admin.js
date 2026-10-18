"use strict";
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
    if (!j.ok) toast(j.error || T("Ошибка"), true);
    return j;
  } catch (e) { toast(T("Сервер недоступен: {e}", { e: e.message }), true); return { ok: false }; }
}
async function reload() {
  const r = await fetch("/api/state");
  if (r.status === 401) { location.href = "/login"; return; }
  S = await r.json();
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
function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    wsOnline = true;
    if (m.settings) { S = m; renderAll(); }
    else if (S) {
      Object.assign(S, m);
      if (m.voices) { renderVoices(); renderTestVoices(); }   // набор установлен или готов пример
      renderLive();
    }
  };
  ws.onclose = () => { wsOnline = false; if (S) renderHeader(); setTimeout(connect, 2000); };
}

function renderAll() {
  renderSettings(); renderVoices(); renderBans(); renderGuide(); renderReward(); renderSources(); renderTestVoices();
  renderLive();
}
function renderLive() {
  renderHeader(); renderQueue(); renderLogin(); renderRewardState(); renderDemoProgress(); renderLiveStats();
  renderStreamNote(); renderServer(); renderReplyNote();
}

// --- шапка ---
function pill(text, cls, title) { return h("span", { class: "pill", title }, h("i", { class: "dot " + cls }), text); }
function renderHeader() {
  const t = S.twitch, c = S.cosy, ov = S.engine.overlay;
  $("#channel").textContent = t.login ? "· " + t.login : "";
  const tw = { ok: ["Twitch", "good"], connecting: [T("Twitch: подключение"), "warn"],
    device: [T("Twitch: ждёт входа"), "warn"], expired: [T("Twitch: вход истёк"), "bad"],
    logged_out: [T("Twitch: нет входа"), "bad"], no_client: [T("Twitch: нет Client ID"), "bad"] }[t.state] || [t.state, ""];
  const pills = [pill(...tw, t.error)];
  if (t.state === "ok") pills.push(pill("EventSub", t.ws === "connected" ? "good" : "warn", t.ws));
  pills.push(pill(c.ok ? (c.busy ? T("cosyvoice: синтез") : "cosyvoice") : T("cosyvoice недоступен"),
    c.ok ? "good" : "bad", c.ok ? c.url + runtimeInfo(c.runtime) : c.error));
  pills.push(pill(!ov.active ? T("оверлей не подключён") : ov.ready ? T("оверлей") : T("оверлей ждёт клика"),
    !ov.active ? "bad" : ov.ready ? "good" : "warn"));
  if (!wsOnline) pills.push(pill(T("нет связи с сервисом"), "bad"));
  $("#pills").replaceChildren(...pills);
  $("#enabled").checked = S.engine.enabled;

  const banner = [];
  if (t.state === "expired") banner.push(T("Вход в Twitch истёк: войдите заново на вкладке Twitch. Пока входа нет, " +
    "активации не обрабатываются, баллы за них вернутся после входа."));
  if (c.ok === false && S.setup && !S.setup.models.length)
    banner.push(h("span", {}, T("Модель синтеза не установлена: скачайте её на вкладке "),
      h("a", { href: "#server" }, T("«Сервер»")), "."));
  else if (c.ok === false) banner.push(T("cosyvoice недоступен ({url}): {error}", { url: c.url, error: c.error }));
  $("#banner").hidden = !banner.length;
  $("#banner").replaceChildren(...banner.map(b => h("div", {}, b)));
}
$("#enabled").onchange = e => api("/api/enabled", { on: e.target.checked });

// --- очередь ---
const STATES = { queued: T("ждёт"), synth: T("синтез"), ready: T("готово"), playing: T("играет") };
const SOURCE_TAGS = { test: [T("тест"), ""], command: [T("команда"), T("Команда в чате, без баллов")],
  chat: [T("чат"), T("Сообщение чата, без баллов")] };
function renderQueue() {
  const q = S.engine.queue;
  const playing = q.find(i => i.state === "playing");
  $("#now").replaceChildren(playing ? h("span", {}, h("b", {}, playing.display + ": "), playing.text,
    h("span", { class: "muted small" }, `  · ${playing.voice}`)) : T("Тишина"));
  $("#now").classList.toggle("muted", !playing);
  $("#qcount").textContent = q.length ? `(${q.length})` : "";
  $("#qempty").hidden = q.length > 0;
  $("#queue").hidden = !q.length;
  $("#queue tbody").replaceChildren(...q.map(i => h("tr", {},
    h("td", {}, i.display, SOURCE_TAGS[i.source] ? h("span", { class: "tag", title: SOURCE_TAGS[i.source][1] || null },
      SOURCE_TAGS[i.source][0]) : null),
    h("td", {}, i.voice),
    h("td", { class: "text" }, i.text),
    h("td", {}, h("span", { class: "state " + i.state }, STATES[i.state] || i.state,
      i.audio ? " " + T("{n} с", { n: fmtNum(i.audio) }) : "")),
    h("td", {}, h("button", { class: "icon danger", title: T("Убрать и вернуть баллы"),
      onclick: () => api("/api/remove", { id: i.id }) }, "✕")))));
  const ov = S.engine.overlay;
  $("#ovstate").replaceChildren(!ov.active ? h("span", { class: "bad" }, T("Оверлей не подключён: фразы ждут в очереди."))
    : ov.ready ? h("span", { class: "good" }, ov.count > 1 ? T("Подключён (окон: {n}, играет последнее).", { n: ov.count })
      : T("Подключён."))
    : h("span", { class: "warn" }, T("Подключён, но звук не включён: кликните «Включить звук» в окне оверлея.")));
  $("#journal").replaceChildren(...S.engine.journal.map(j => h("li", {}, h("time", {}, fmtClock(j.ts)), j.text)));
}
$("#skip").onclick = () => api("/api/skip");
$("#clear").onclick = async () => {
  if (await ask(T("Очистить очередь и вернуть баллы за всё в ней?"), T("Очистить"), true)) api("/api/clear");
};
$("#ovurl").textContent = location.origin + "/overlay";
$("#vurl").textContent = location.origin + "/voices/";
$$("[data-copy]").forEach(b => b.onclick = () =>
  navigator.clipboard.writeText($("#" + b.dataset.copy).textContent).then(() => toast(T("Скопировано"))));

function renderTestVoices() {
  const sel = $("#test select[name=voice]"), cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, T("как у зрителя (по нику)")),
    ...S.voices.filter(v => v.enabled).map(v => h("option", { value: v.name }, v.name)));
  sel.value = cur;
}
$("#test").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const r = await api("/api/test", Object.fromEntries(f));
  if (r.ok) toast(T("Отправлено в очередь"));
};

// --- Twitch: вход ---
let lastLogin = "", lastState = null;
function renderLogin() {
  const t = S.twitch, key = JSON.stringify([t.state, t.error, t.device, t.login, t.client_id, t.ws, guideOpen]);
  if (key === lastLogin) return;
  lastLogin = key;
  const entered = t.state !== lastState;
  if (entered && t.state === "ok" && lastState !== null && guideOpen) { guideOpen = false; renderGuide(); }  // вошли: гайд не нужен
  lastState = t.state;
  const below = placeLogin();
  const card = $("#login-card"), kids = [h("h2", {}, T("Вход в Twitch"))];
  if (t.state === "no_client") {
    kids.push(h("p", {}, below ? T("Вставьте Client ID в шаге 4 и нажмите «Проверить и войти».")
      : T("Сначала зарегистрируйте своё приложение Twitch по шагам ниже и вставьте его Client ID.")));
  } else if (t.state === "device") {
    kids.push(h("p", {}, T("Откройте страницу активации на любом устройстве, войдите аккаунтом канала и введите код:")),
      h("div", { class: "code" }, t.device.code),
      h("div", { class: "row" }, h("a", { class: "btn primary", href: t.device.uri, target: "_blank", rel: "noopener" },
        T("Открыть twitch.tv/activate")), h("span", { class: "muted small" },
        T("код действует до {time}", { time: new Date(t.device.expires * 1000).toLocaleTimeString(LOCALE,
          { hour: "2-digit", minute: "2-digit" }) }))),
      h("p", { class: "muted small" }, T("Страница сама обновится, когда вы подтвердите вход. Разрешения: управление " +
        "наградами (создать, ставить на паузу, отмечать активации) и чтение чата для команд.")));
  } else if (t.state === "ok" || t.state === "connecting") {
    kids.push(h("p", {}, t.state === "ok" ? T("Канал: ") : T("Подключение… "), h("b", {}, t.login || ""),
      t.state === "ok" ? h("span", { class: "muted" }, `  · EventSub: ${{ connected: T("подключён"),
        connecting: T("подключение…"), off: T("выключен") }[t.ws] || t.ws}`) : null),
      h("div", { class: "row" }, h("button", { onclick: async () =>
        await ask(T("Выйти из Twitch? Награда останется на канале."), T("Выйти")) && api("/api/twitch/logout") },
        T("Выйти"))));
  } else {
    if (t.state === "expired") kids.push(h("p", { class: "bad" }, T("Вход истёк: refresh-токен больше не действует.")));
    kids.push(h("p", {}, "Client ID: ", h("code", {}, t.client_id), t.client_id_env ? h("span", { class: "muted small" }, T(" (из .env)")) : null),
      h("button", { class: "primary", onclick: () => api("/api/twitch/login") }, T("Войти через Twitch")));
  }
  if (t.error) kids.push(h("p", { class: "bad" }, t.error));
  card.replaceChildren(...kids);
  $("#sources-card").hidden = t.state !== "ok";
  // Код появился после «Проверить и войти»: карточка может быть за пределами экрана
  if (entered && t.state === "device" && $("#tab-twitch").classList.contains("on"))
    card.scrollIntoView({ behavior: "smooth", block: "center" });
}
/** Пока вход не выполнен и гайд открыт, карточка входа идёт сразу под шагом с Client ID, иначе — вверху вкладки. */
function placeLogin() {
  const below = guideOpen && !["ok", "connecting"].includes(S.twitch.state), tab = $("#tab-twitch");
  if (below) { tab.prepend($("#guide-card")); $("#guide-card").after($("#login-card"), $("#sources-card")); }
  else tab.append($("#guide-card"));
  return below;
}

// --- гайд регистрации ---
const mockBar = url => h("div", { class: "bar" }, h("i"), h("i"), h("i"), h("span", {}, url));
const mockField = (label, value, hl) => h("div", { class: "field" + (hl ? " hl" : "") }, h("b", {}, label),
  h("div", { class: "input" }, value));
function copyBtn(text) {
  return h("button", { type: "button", class: "icon", title: T("Копировать"),
    onclick: () => navigator.clipboard.writeText(text).then(() => toast(T("Скопировано"))) }, "⧉");
}
function guideSteps() {
  const name = (S.twitch.login || T("канал")) + "-stream-tts";
  return [
    { title: T("Включите двухфакторную аутентификацию"),
      body: [h("p", {}, T("Без 2FA консоль разработчика Twitch не даст зарегистрировать приложение.")),
        h("a", { class: "btn", href: "https://www.twitch.tv/settings/security", target: "_blank", rel: "noopener" },
          T("Настройки безопасности Twitch ↗")),
        h("p", { class: "muted small" }, T("Раздел «Безопасность и конфиденциальность» → «Двухфакторная аутентификация»."))],
      mock: h("div", { class: "mock" }, mockBar("twitch.tv/settings/security"), h("div", { class: "side" },
        h("div", {}, h("div", {}, T("Профиль")), h("div", {}, T("Каналы и видео")),
          h("div", { class: "hl" }, T("Безопасность"))),
        h("div", { class: "body" }, h("b", {}, T("Безопасность")),
          h("div", {}, T("Пароль")), h("div", { class: "hl" }, T("Двухфакторная аутентификация") + "  ",
            h("span", { class: "btnm" }, T("Включить"))))))
    },
    { title: T("Откройте консоль разработчика"),
      body: [h("p", {}, T("Войдите тем же аккаунтом. Ссылка ведёт сразу на форму «Register Your Application» " +
        "(в консоли: Applications → «+ Register Your Application»).")),
        h("a", { class: "btn", href: "https://dev.twitch.tv/console/apps/create", target: "_blank", rel: "noopener" },
          "dev.twitch.tv/console ↗"),
        h("p", { class: "muted small" }, T("Консоль открывается в новой вкладке: внутри другой страницы Twitch её не показывает."))],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps"), h("div", { class: "side" },
        h("div", {}, h("div", {}, "Dashboard"), h("div", { style: "color:var(--ink)" }, "Applications"), h("div", {}, "Extensions")),
        h("div", { class: "body" }, h("b", {}, "Applications"),
          h("div", {}, h("span", { class: "btnm hl" }, "+ Register Your Application")))))
    },
    { title: T("Заполните форму"),
      body: [h("ul", {},
          h("li", {}, "Name: ", h("code", {}, name), " ", copyBtn(name),
            h("div", { class: "muted small" }, T("Имя должно быть уникальным во всём Twitch. Занято — добавьте что-нибудь."))),
          h("li", {}, "OAuth Redirect URLs: ", h("code", {}, "http://localhost"), " ", copyBtn("http://localhost"),
            " " + T("и кнопка Add"), h("div", { class: "muted small" },
              T("Поле обязательное, но для входа по коду не используется. Twitch может написать, " +
              "что адрес должен быть HTTPS: к localhost это не относится, http://localhost он принимает."))),
          h("li", {}, T("Category: любая, например Broadcaster Suite.")),
          h("li", {}, h("b", { class: "warn" }, "Client Type: Public. "),
            T("Тип клиента после создания, вероятно, не поменять: с Confidential вход по коду не работает, " +
            "придётся регистрировать приложение заново."))),
        h("p", {}, T("Нажмите Create."))],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps/create"), h("div", { class: "body" },
        h("b", {}, "Register Your Application"),
        mockField("Name", name), mockField("OAuth Redirect URLs", "http://localhost"),
        mockField("Category", "Broadcaster Suite"),
        h("div", { class: "field hl" }, h("b", {}, "Client Type"),
          h("div", {}, h("span", { class: "radio" }, "Confidential   "), h("span", { class: "radio on" }, "Public"))),
        h("div", {}, h("span", { class: "btnm" }, "Create"))))
    },
    { title: T("Скопируйте Client ID"),
      body: [h("p", {}, T("В списке приложений нажмите Manage у нового приложения и скопируйте Client ID. " +
        "Client Secret не нужен.")),
        h("form", { class: "row", onsubmit: async e => {
          e.preventDefault();
          const r = await api("/api/twitch/client_id", { client_id: e.target.client_id.value });
          if (r.ok) { toast(T("Client ID принят, войдите кодом")); reload(); }
        } }, h("input", { name: "client_id", class: "grow", placeholder: "Client ID", value: S.twitch.client_id || "",
          autocomplete: "off", spellcheck: "false" }), h("button", { class: "primary" }, T("Проверить и войти"))),
        h("p", { class: "muted small" }, T("Сервис сразу пробует начать вход: если Twitch не примет ID, здесь будет " +
          "ошибка. ID можно задать и в .env (TWITCH_CLIENT_ID)."))],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps/…"), h("div", { class: "body" },
        h("b", {}, name), mockField("OAuth Redirect URLs", "http://localhost"),
        h("div", { class: "field hl" }, h("b", {}, "Client ID"),
          h("div", { class: "row" }, h("div", { class: "input grow" }, "abcd1234efgh5678…"), h("span", { class: "btnm" }, "Copy"))),
        mockField("Client Type", "Public")))
    },
  ];
}
let guideOpen = null;
function renderGuide() {
  if (guideOpen === null) guideOpen = !S.twitch.client_id;
  $("#guide").hidden = !guideOpen;
  $("#guide-card > p").hidden = !guideOpen;
  $("#guide-toggle").textContent = guideOpen ? T("Свернуть") : T("Показать");
  $("#guide").replaceChildren(...guideSteps().map(s => h("li", {},
    h("div", {}, h("h3", {}, s.title), ...s.body), s.mock)));
}
$("#guide-toggle").onclick = () => { guideOpen = !guideOpen; renderGuide(); renderLogin(); };

// --- способы заказа: награда, команда, все сообщения чата ---
const ACCESS_OPTS = [["all", T("все")], ["subs", T("сабы, VIP и модеры")], ["vip", T("VIP и модеры")],
  ["mods", T("только модеры")]];
$$("#sources-card .src").forEach(box => {
  box.querySelector(".src-body").append(h("div", { class: "src-common" },
    h("label", {}, T("Кто может"), h("select", { "data-k": "access" },
      ACCESS_OPTS.map(([v, t]) => h("option", { value: v }, t)))),
    h("label", {}, T("Всегда доступно: ники через запятую"), h("textarea", { "data-k": "allow", rows: 1,
      spellcheck: "false", translate: "no" }))));
  box.addEventListener("change", async e => {
    const k = e.target.dataset.k;
    if (!k) return;   // поля награды сохраняются кнопкой формы
    const el = e.target, v = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
    const r = await api("/api/source", { source: box.dataset.src, [k]: v });
    if (r.ok) { S.settings.sources[box.dataset.src] = r.source; toast(T("Сохранено")); }
    renderSources(true);
  });
});
function renderSources(force) {
  const src = S.settings.sources;
  $$("#sources-card .src").forEach(box => {
    const c = src[box.dataset.src];
    box.querySelector(".src-body").hidden = !c.on;
    for (const el of box.querySelectorAll("[data-k]")) {
      if (el === document.activeElement && !force) continue;
      const v = c[el.dataset.k];
      if (el.type === "checkbox") el.checked = !!v; else el.value = Array.isArray(v) ? v.join(", ") : v;
    }
  });
  $("#cmd-example").textContent = T("Зритель пишет в чат: {cmd} привет, чат!", { cmd: src.command.name });
  $("#reply").value = S.settings.reply;
  lastReply = "";
  renderReplyNote();
}
$("#reply").onchange = async e => {
  if ((await api("/api/settings", { reply: e.target.value })).ok) { S.settings.reply = e.target.value; toast(T("Сохранено")); }
  renderSources(true);
};
const REPLY_SCOPE = "user:write:chat";
let lastReply = "";
function renderReplyNote() {
  const t = S.twitch, mode = S.settings.reply, key = JSON.stringify([mode, t.scopes, t.reply_error, t.state]);
  if (key === lastReply) return;
  lastReply = key;
  const kids = [];
  if (mode !== "off" && t.state === "ok" && !(t.scopes || []).includes(REPLY_SCOPE))
    kids.push(h("div", { class: "warn" }, T("Вход в Twitch был до появления ответов: выйдите и войдите снова, " +
      "чтобы разрешить {scope}. Пока ответов не будет.", { scope: REPLY_SCOPE })));
  kids.push(h("div", { class: "muted" }, {
    off: T("Отказ виден только в журнале, за награду баллы возвращаются молча."),
    chat: T("Ответ приходит от аккаунта канала: на команду — ответом на сообщение, на награду — с @ником. " +
      "Одному зрителю не чаще раза в 30 с, о бане не сообщается."),
  }[mode]));
  if (t.reply_error && mode !== "off") kids.push(h("div", { class: "bad" }, t.reply_error));
  $("#reply-note").replaceChildren(...kids);
}

// --- награда ---
const REWARD_KEYS = ["title", "cost", "cooldown", "prompt"];
let rewardSavedUntil = 0;   // «Сохранено» на кнопке, мс; renderRewardState идёт каждые 2 с и не должен его стереть
function renderReward() {
  const r = S.twitch.reward, f = $("#reward");
  for (const k of REWARD_KEYS) f[k].value = r[k] ?? "";
  renderRewardState();
}
const rewardDirty = () => REWARD_KEYS.some(k => $("#reward")[k].value !== String(S.twitch.reward[k] ?? ""));
function renderRewardState() {
  const r = S.twitch.reward, btn = $("#reward-save"), saved = r.exists && Date.now() < rewardSavedUntil;
  btn.textContent = !r.exists ? T("Создать награду") : saved ? T("Сохранено") : T("Сохранить");
  btn.disabled = r.exists && (saved || !rewardDirty());
  $("#reward-delete").hidden = !r.exists;
  $("#reward-state").textContent = !r.exists ? T("Награда ещё не создана.") :
    r.paused ? T("Награда на паузе (озвучка выключена).") : T("Награда активна.");
}
$("#reward").oninput = () => { rewardSavedUntil = 0; renderRewardState(); };
$("#reward").onsubmit = async e => {
  e.preventDefault();
  const f = e.target, existed = S.twitch.reward.exists;
  const r = await api("/api/reward", { title: f.title.value, cost: +f.cost.value, cooldown: +f.cooldown.value,
    prompt: f.prompt.value });
  if (!r.ok) return;
  if (!existed) toast(T("Награда создана"));
  rewardSavedUntil = Date.now() + 3000;
  setTimeout(() => S && renderRewardState(), 3050);
  reload();
};
$("#reward-delete").onclick = async () => {
  if (await ask(T("Удалить награду с канала?"), T("Удалить"), true) && (await api("/api/reward/delete")).ok) reload();
};

// --- голоса ---
let pctx = null, psrc = null, pbtn = null;
async function preview(v, btn) {
  if (psrc) { psrc.onended = null; psrc.stop(); psrc = null; pbtn.textContent = "▶"; if (pbtn === btn) { pbtn = null; return; } }
  pctx = pctx || new AudioContext();
  btn.textContent = "…";
  pbtn = btn;
  try {
    const r = await fetch("/api/preview?name=" + encodeURIComponent(v.name));
    if (!r.ok) throw new Error(await r.text());
    const buf = await pctx.decodeAudioData(await r.arrayBuffer());
    if (pbtn !== btn) return;
    const gain = pctx.createGain();
    gain.gain.value = 10 ** (v.gain_db / 20);   // пример выровнен до −16 LUFS, поправка поверх
    psrc = pctx.createBufferSource();
    psrc.buffer = buf;
    psrc.connect(gain).connect(pctx.destination);
    psrc.onended = () => { btn.textContent = "▶"; psrc = null; pbtn = null; };
    psrc.start();
    btn.textContent = "■";
  } catch (e) { btn.textContent = "▶"; pbtn = null; toast(T("Пример: {e}", { e: e.message }), true); }
}
let voicesStale = false;
function renderVoices() {
  const tbody = $("#voices tbody");
  if (tbody.contains(document.activeElement)) { voicesStale = true; return; }   // ползунок громкости в руке
  voicesStale = false;
  tbody.replaceChildren(...S.voices.map(v => {
    const out = h("output", {}, (v.gain_db > 0 ? "+" : "") + v.gain_db);
    const tr = h("tr", { class: v.enabled ? "" : "off", "data-name": v.name },
      h("td", {}, h("input", { type: "checkbox", checked: v.enabled, "aria-label": T("включён"),
        onchange: async e => { v.enabled = e.target.checked; tr.className = v.enabled ? "" : "off";
          await api("/api/voice", { name: v.name, enabled: v.enabled }); renderTestVoices(); } })),
      h("td", {}, v.name, v.post ? h("span", { class: "tag", title: v.post }, T("эффект")) : null,
        v.demo ? null : h("span", { class: "tag", title: T("Пример ещё не синтезирован") }, T("без примера"))),
      h("td", {}, h("button", { class: "icon", title: T("Послушать пример с поправкой громкости"),
        onclick: e => preview(v, e.currentTarget) }, "▶")),
      // calib_db — служебное: усиление для стриминга, измеренное по примеру; видно только в подсказке
      h("td", { title: v.calib_db == null ? null : T("Автоматическое выравнивание: {db} дБ",
          { db: (v.calib_db > 0 ? "+" : "") + fmtNum(v.calib_db) }) }, h("div", { class: "gain" }, h("input", {
        type: "range", min: -12, max: 12, step: 0.5, value: v.gain_db, "aria-label": T("поправка громкости {voice}", { voice: v.name }),
        oninput: e => { out.textContent = (+e.target.value > 0 ? "+" : "") + e.target.value; },
        onchange: e => { v.gain_db = +e.target.value; api("/api/voice", { name: v.name, gain_db: v.gain_db }); } }), out)));
    return tr;
  }));
  filterVoices();
}
$("#voices tbody").addEventListener("focusout", () => setTimeout(() => voicesStale && renderVoices()));
function filterVoices() {
  const q = $("#vsearch").value.trim().toLowerCase();
  $$("#voices tbody tr").forEach(tr => tr.hidden = !tr.dataset.name.toLowerCase().includes(q));
  $("#vall-on").textContent = q ? T("Включить найденные") : T("Включить все");
  $("#vall-off").textContent = q ? T("Выключить найденные") : T("Выключить все");
}
$("#vsearch").oninput = filterVoices;
// Кнопки действуют на голоса, найденные поиском (без поиска — на все)
const shownVoices = () => $$("#voices tbody tr").filter(tr => !tr.hidden).map(tr => tr.dataset.name);
$("#vall-on").onclick = async () => { await api("/api/voice", { names: shownVoices(), enabled: true }); reload(); };
$("#vall-off").onclick = async () => {
  const names = shownVoices();
  if (!names.length) return;
  const msg = names.length === S.voices.length
    ? T("Выключить все голоса? Пока ни один не включён, активации будут возвращаться.")
    : T("Выключить найденные голоса ({n})?", { n: names.length });
  if (await ask(msg, T("Выключить"), true)) { await api("/api/voice", { names, enabled: false }); reload(); }
};
$("#demo-form").onsubmit = async e => {
  e.preventDefault();
  if ((await api("/api/settings", { demo_phrase: e.target.demo_phrase.value })).ok) { toast(T("Сохранено, примеры пересинтезируются")); reload(); }
};
$("#demo-rebuild").onclick = async () => {
  if (await ask(T("Пересинтезировать примеры всех включённых голосов? Заодно обновится выравнивание громкости."),
      T("Пересинтезировать"))) {
    await api("/api/demos/rebuild"); reload(); }
};
function renderDemoProgress() {
  const d = S.demos;
  $("#demo-progress").textContent = d.todo ? T("Готовятся примеры: осталось {n}", { n: d.todo }) +
    (d.current ? T(", сейчас {voice}", { voice: d.current }) : T(" (ждут свободного cosyvoice)"))
    : T("Примеры всех включённых голосов готовы.");
  if (d.error) $("#demo-progress").append(h("span", { class: "bad" }, "  " + T("Ошибка: {e}", { e: d.error })));
}

// --- настройки ---
// --- сервер (режим managed): модель, устройство, наборы голосов ---
const fmtMB = b => T("{n} МБ", { n: fmtNum(b / 1048576, 0) });
const fmtDur = s => s < 60 ? T("{n} с", { n: Math.ceil(s) }) : T("{n} мин", { n: Math.round(s / 60) });
const BACKEND_NAMES = { auto: T("Автоматически"), cuda: T("Видеокарта NVIDIA (CUDA)"), vulkan: T("Видеокарта (Vulkan)"),
  cpu: T("Процессор") };
const RT_STATES = { waiting: T("ждёт"), starting: T("запускается"), running: T("работает"), crashed: T("упал, перезапуск"),
  stopped: T("остановлен") };
// Примеры звучания квантов: один плеер на всю вкладку, кнопки переживают перерисовку по ws
const sample = new Audio();
let sampleKey = "";
sample.onended = sample.onerror = () => { sampleKey = ""; renderServer(); };
function playSample(key) {
  if (sampleKey === key) { sample.pause(); sampleKey = ""; }
  else { sample.src = `/static/quants/${key}.mp3`; sample.play().catch(() => {}); sampleKey = key; }
  renderServer();
}
const fmtGB = mb => fmtNum(mb / 1024, 1);
/** Скорость синтеза по rtf (время синтеза / длина звука): ×3,2 — втрое быстрее звучания, ×0,45 — медленнее. */
const fmtSpeed = rtf => rtf == null || !(rtf > 0) ? "—" : "×" + fmtNum(1 / rtf, 1 / rtf < 1 ? 2 : 1);

let backendKey = "";
function renderServer() {
  const st = S.setup, rt = S.cosy.runtime;
  $('#tabs [data-tab="server"]').hidden = !st;
  if (!st) return;
  $("#models-dir").textContent = st.models_dir;
  $("#voices-dir").textContent = st.voices_dir;
  const d = st.download, running = d.state === "running";
  // Модели: варианты квантования с примерами и установленные файлы (в т. ч. положенные вручную)
  const use = m => m === st.model ? h("span", { class: "tag" }, T("используется"))
    : h("button", { disabled: rt.locked.model, onclick: () => api("/api/model/use", { name: m }) }, T("Использовать"));
  const catalog = st.quants.map(q => h("li", { class: q.file === st.model ? "on" : "" },
    h("div", { class: "grow" },
      h("b", {}, q.quant), q.quant === st.default_quant ? h("span", { class: "tag" }, T("рекомендуется")) : null,
      h("div", { class: "small", title: T("Замер на видеокарте NVIDIA; на Vulkan примерно на 0,5 ГБ больше") },
        T("{size} · видеопамять {min}–{max} ГБ", { size: fmtMB(q.size), min: fmtGB(q.vram), max: fmtGB(q.vram_max) })),
      h("div", { class: "row samples" }, st.samples.map(([voice, label]) => {
        const key = `${voice}-${q.quant}`;
        return h("button", { type: "button", class: "small" + (sampleKey === key ? " on" : ""),
          title: `«${st.sample_phrase}»`, onclick: () => playSample(key) }, (sampleKey === key ? "■ " : "▶ ") + label);
      }))),
    st.models.includes(q.file) ? use(q.file)
      : running && d.quant === q.quant ? h("span", { class: "tag" }, T("скачивается"))
      : h("button", { class: "primary", disabled: running, onclick: () =>
          api("/api/model/download", { quant: q.quant, frontend: $("#frontend").checked }) }, T("Скачать"))));
  const known = st.quants.map(q => q.file);
  const other = st.models.filter(m => !known.includes(m)).map(m => h("li", { class: m === st.model ? "on" : "" },
    h("span", { class: "grow" }, m), use(m)));
  const empty = st.models.length ? [] : [h("li", { class: "muted" },
    T("Модель не установлена: послушайте примеры и скачайте вариант."))];
  $("#models").replaceChildren(...empty, ...catalog, ...other);
  // Загрузка
  $("#model-cancel").hidden = !running;
  $("#dl").hidden = d.state === "idle";
  $("#dl-fill").style.width = (d.total ? Math.min(100, 100 * d.done / d.total) : 0) + "%";
  const left = d.speed > 0 ? " · " + T("осталось {time}", { time: fmtDur((d.total - d.done) / d.speed) }) : "";
  $("#dl-text").className = "small " + ({ error: "bad", done: "good" }[d.state] || "");
  $("#dl-text").textContent = {
    running: T("{file}: {done} из {total}", { file: d.file || T("подготовка"), done: fmtMB(d.done), total: fmtMB(d.total) }) +
      (d.speed > 0 ? " · " + T("{n} МБ/с", { n: fmtNum(d.speed / 1048576) }) : "") + left,
    done: T("Готово: {quant} скачана, сервер перезапускается.", { quant: d.quant }),
    cancelled: T("Загрузка отменена на {done} из {total}: «Скачать» продолжит с этого места.",
      { done: fmtMB(d.done), total: fmtMB(d.total) }),
    error: T("Ошибка: {e}. «Скачать» продолжит с того же места.", { e: d.error }),
  }[d.state] || "";
  // Устройство: перерисовка только при смене вариантов, чтобы не сбивать выбор
  const key = JSON.stringify([rt.backends, rt.prefer, rt.locked.backend]);
  if (key !== backendKey) {
    backendKey = key;
    const have = k => k === "auto" || k === "cpu" || rt.backends.some(b => b.toLowerCase().startsWith(k));
    $("#backend-form").replaceChildren(...Object.entries(BACKEND_NAMES).map(([k, name]) =>
      h("label", { class: have(k) ? "" : "na" }, h("input", { type: "radio", name: "backend", value: k,
        checked: rt.prefer === k, disabled: rt.locked.backend || !have(k) }),
        h("span", {}, name, have(k) ? "" : h("span", { class: "muted small" }, T(" — не найдено"))))));
  }
  $("#backend-lock").hidden = !rt.locked.backend;
  $("#backend-lock").textContent = T("Устройство задано переменной COSYVOICE_BACKEND.");
  // Наборы голосов
  $("#sets").replaceChildren(...(st.sets.length ? st.sets.map(s => h("li", {},
    h("span", { class: "grow" }, h("b", {}, s.name), h("span", { class: "muted small" },
      T(" — голосов: {n}: {voices}", { n: s.voices.length,
        voices: s.voices.slice(0, 8).join(", ") + (s.voices.length > 8 ? "…" : "") }))),
    h("button", { class: "icon danger", title: T("Удалить набор"), onclick: () =>
      ask(T("Удалить набор «{name}» (голосов: {n})?", { name: s.name, n: s.voices.length }), T("Удалить"), true)
        .then(yes => yes && api("/api/voicesets/delete", { name: s.name }))
    }, "✕"))) : [h("li", { class: "muted" }, T("Наборов нет."))]));
  renderBench(rt);
  // Состояние
  const row = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
  $("#runtime-info").replaceChildren(h("dl", {},
    row(T("Состояние"), h("span", { class: { running: "good", crashed: "bad", waiting: "warn" }[rt.state] || "" },
      RT_STATES[rt.state] || rt.state)),
    rt.error ? row(T("Причина"), rt.error) : null,
    row(T("Устройство"), rt.backend || "—"), row(T("Модель"), rt.model || "—"), row(T("Голосов"), rt.voices),
    row(T("Адрес"), S.cosy.url), rt.restarts ? row(T("Перезапусков"), rt.restarts) : null,
    row(T("Программа"), rt.server)));
}
$("#model-cancel").onclick = () => api("/api/model/cancel");

// Скорость синтеза (bench.py): результаты по устройствам и моделям, текущее выделено
function benchVerdict(rtf) {
  return rtf <= 0.5 ? [T("хватает для стрима с запасом"), "good"] : rtf < 0.9 ? [T("хватает для стрима, впритык"), "warn"]
    : [T("без запаса или медленнее реального времени: для стрима не годится, стриминг выключить"), "bad"];
}
function renderBench(rt) {
  const b = S.bench, running = b.state === "running";
  const rows = [...b.results].sort((x, y) => x.rtf - y.rtf);
  const quant = m => (m.match(/_(Q\w+|F16|BF16)\.gguf$/i) || [, m])[1];
  $("#bench-table").replaceChildren(rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "bench" },
    h("thead", {}, h("tr", {}, h("th", {}, T("Устройство")), h("th", {}, T("Модель")),
      h("th", { class: "num", title: T("Во сколько раз синтез быстрее звучания фразы") }, T("Скорость")),
      h("th", { class: "num", title: T("Через сколько секунд начинает звучать длинная фраза при стриминге") },
        T("Первый звук")))),
    h("tbody", {}, ...rows.map(r => {
      const [verdict, cls] = benchVerdict(r.rtf);
      return h("tr", { class: r.backend === rt.backend && r.model === rt.model ? "on" : "",
        title: T("{date}, фраза {audio} с, кусок {chunk} токенов", { date: new Date(r.ts * 1000).toLocaleString(LOCALE),
          audio: fmtNum(r.audio), chunk: r.chunk }) },
        h("td", {}, r.backend), h("td", {}, quant(r.model)),
        h("td", { class: "num " + cls, title: verdict }, fmtSpeed(r.rtf)),
        h("td", { class: "num" }, r.first == null ? "—" : T("{n} с", { n: fmtNum(r.first) })));
    }))))
    : h("p", { class: "muted small" }, T("Замеров ещё нет.")),
    ...(rows.length ? [h("p", { class: "muted small" }, T("×4 — синтез в 4 раза быстрее звучания фразы, ×0,5 — вдвое " +
      "медленнее. Для стрима нужно больше ×1, лучше ×2 и выше (зелёный)."))] : []));   // replaceChildren(null) пишет «null»
  const many = rt.backends.some(n => /^(cuda|vulkan)/i.test(n));
  $("#bench-all").hidden = !many || rt.locked.backend;
  $("#bench-one").disabled = $("#bench-all").disabled = running || !S.cosy.ok;
  $("#bench-state").className = "small " + (b.state === "error" ? "bad" : "muted");
  $("#bench-state").textContent = running ? T("Идёт проверка: {step}…", { step: b.step })
    : b.state === "error" ? T("Ошибка: {e}", { e: b.error }) : "";
}
$("#bench-one").onclick = () => api("/api/bench", { all: false });
$("#bench-all").onclick = async () => {
  if (await ask(T("Сервер синтеза перезапустится на каждом устройстве, на процессоре проверка займёт пару минут. " +
    "Озвучка всё это время ждёт в очереди."), T("Сравнить"))) api("/api/bench", { all: true });
};
$("#backend-form").onchange = e => api("/api/backend", { backend: e.target.value });
$("#set-import").onclick = async () => {
  const f = $("#set-file").files[0], out = $("#set-result");
  if (!f) { toast(T("Выберите zip с набором"), true); return; }
  const btn = $("#set-import");
  btn.disabled = true;
  out.className = "small muted";
  out.textContent = T("Устанавливаю {file}…", { file: f.name });
  try {
    const r = await fetch("/api/voicesets/import", { method: "POST", body: f,
      headers: { "Content-Type": "application/zip", "X-Filename": encodeURIComponent(f.name) } });
    const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    if (!j.ok) { out.className = "small bad"; out.textContent = j.error || T("Ошибка"); return; }
    out.className = "small good";
    out.textContent = T("Набор «{name}»: установлено голосов {n}", { name: j.name, n: j.added.length }) +
      (j.skipped.length ? T(", пропущено {n}: {voices}", { n: j.skipped.length, voices: j.skipped.join(", ") }) : "") +
      (j.ignored.length ? T(". Не голоса, пропущены: {files}", { files: j.ignored.join(", ") }) : "") + ".";
    $("#set-file").value = "";
  } catch (e) { out.className = "small bad"; out.textContent = T("Сервер недоступен: {e}", { e: e.message }); }
  finally { btn.disabled = false; }
};

// Встроенный сервер (режим managed): бэкенд, модель, число голосов
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
    h("div", {}, "cosyvoice: ", h("code", {}, S.cosy.url), runtimeInfo(S.cosy.runtime)),
    h("div", {}, T(c.password ? "Интерфейс: {addr}, пароль задан" : "Интерфейс: {addr}, пароль не задан (доступ только с этого ПК)",
      { addr: `${c.bind}:${c.port}` })));
  $("#logout-link").hidden = !c.password;
}
$("#settings").onchange = async e => {
  const el = e.target;
  if (!el.name) return;
  const v = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
  if ((await api("/api/settings", { [el.name]: v })).ok) { S.settings[el.name] = v; toast(T("Сохранено")); }
};
function renderStreamNote() {
  const rtf = S.engine.rtf, note = $("#stream-note");
  note.hidden = !(rtf > 0.9);
  if (!note.hidden) note.textContent = T("Скорость синтеза сейчас {speed}: стриминг будет заикаться, лучше его выключить.",
    { speed: fmtSpeed(rtf) });
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

// --- статистика ---
function tile(k, v, s) { return h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
  s ? h("div", { class: "s" }, s) : null); }
function renderLiveStats() {
  if (!$("#tab-stats").classList.contains("on")) return;
  const q = S.engine.queue, playing = q.find(i => i.state === "playing"), g = S.gpu;
  $("#live").replaceChildren(
    tile(T("В очереди"), q.length, q.length ? T("синтезируется: {n}", { n: q.filter(i => i.state === "synth").length }) : ""),
    tile(T("Играет"), playing ? playing.display : "—", playing ? playing.voice : ""),
    tile("cosyvoice", S.cosy.ok ? T("доступен") : T("нет"), S.cosy.ok ? (S.cosy.busy || T("свободен")) : S.cosy.error),
    g ? tile("GPU", `${g.util}%`, T("{used} / {total} ГБ · {temp} °C",
      { used: fmtNum(g.mem_used / 1024), total: fmtNum(g.mem_total / 1024), temp: g.temp }))
      : tile("GPU", T("нет данных"), T("nvidia-smi недоступен в контейнере")),
    tile(T("Скорость синтеза"), fmtSpeed(S.engine.rtf), T("последние фразы, во сколько раз быстрее звучания")));
}
async function loadStats() {
  renderLiveStats();
  const hours = +$("#period").value;
  const r = await fetch(`/api/stats?hours=${hours}&test=${$("#with-test").checked ? 1 : 0}`);
  if (!r.ok) return;
  const st = await r.json();
  const t = st.total;
  $("#kpi").replaceChildren(
    tile(T("Активаций"), t.n || 0),
    tile(T("Сыграно"), t.played || 0),
    tile(T("Возвратов"), t.refunded || 0, t.n ? `${fmtNum(100 * (t.refunded || 0) / t.n, 0)}%` : ""),
    tile(T("Ожидание до звука"), st.wait_ms == null ? "—" : T("{n} с", { n: fmtNum(st.wait_ms / 1000) }), T("в среднем")),
    tile(T("Скорость синтеза"), fmtSpeed(st.synth_ratio),
      st.avg_audio_ms ? T("средняя фраза {n} с", { n: fmtNum(st.avg_audio_ms / 1000) }) : ""));
  drawHours(st, hours);
  bars("#top-voices", st.voices.map(x => [x.voice, x.n]));
  bars("#top-viewers", st.viewers.map(x => [x.login, x.n]));
  const SOURCE_NAMES = { reward: T("награда"), command: T("команда"), chat: T("чат"), test: T("тест") };
  bars("#top-sources", st.sources.map(x => [SOURCE_NAMES[x.source] || x.source, x.n]));
  bars("#reasons", st.reasons.map(x => [T(x.reason), x.n]));   // причины в базе по-русски (engine.R_*)
}
$("#period").onchange = loadStats;
$("#with-test").onchange = loadStats;
setInterval(() => { if ($("#tab-stats").classList.contains("on")) loadStats(); }, 60000);

function bars(sel, rows) {
  const max = Math.max(1, ...rows.map(r => r[1]));
  $(sel).replaceChildren(...(rows.length ? rows.map(([name, n]) => h("div", { class: "b" },
    h("span", { class: "t", title: name }, name), h("span", { class: "n" }, n),
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${100 * n / max}%` }))))
    : [h("span", { class: "muted" }, T("нет данных"))]));
}

function drawHours(st, hours) {
  const box = $("#hours"), W = Math.max(320, box.clientWidth || 800), H = 220;
  const pad = { l: 34, r: 8, t: 8, b: 22 };
  const now = Math.floor(Date.now() / 3600000) * 3600;
  const start = now - (hours - 1) * 3600;
  const map = new Map(st.by_hour.map(r => [r.hour, r]));
  const cols = [];
  for (let t = start; t <= now; t += 3600) { const r = map.get(t) || {}; cols.push({ t, p: r.played || 0, f: r.refunded || 0 }); }
  const max = Math.max(1, ...cols.map(c => c.p + c.f));
  const step = niceStep(max), top = Math.ceil(max / step) * step;
  const cw = (W - pad.l - pad.r) / cols.length, gap = cw >= 6 ? 2 : 0, bw = Math.max(1, cw - gap);
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": T("Активации по часам") });
  for (let v = 0; v <= top; v += step) {
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "grid" }));
    const tx = svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" }); tx.textContent = v; s.append(tx);
  }
  const tip = $("#tip");
  cols.forEach((c, i) => {
    const x = pad.l + i * cw + gap / 2;
    const d = new Date(c.t * 1000);
    const label = hours <= 24 ? d.getHours() % 3 === 0 : d.getHours() === 0 && (hours <= 168 || d.getDate() % 5 === 1);
    if (label) {
      const tx = svg("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle" });
      tx.textContent = hours <= 24 ? `${String(d.getHours()).padStart(2, "0")}:00`
        : d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" });
      s.append(tx);
    }
    // сыграно снизу, возврат сверху; между сегментами зазор 2px, верх последнего сегмента скруглён
    const segs = [[c.p, "var(--series-1)"], [c.f, "var(--series-2)"]].filter(z => z[0] > 0);
    let base = 0;
    segs.forEach(([v, color], k) => {
      const y0 = y(base), y1 = y(base + v) + (k > 0 && gap ? 2 : 0);
      const hgt = Math.max(1, y0 - y1), last = k === segs.length - 1, rr = last ? Math.min(4, bw / 2, hgt) : 0;
      s.append(svg("path", { fill: color, d: `M${x},${y0} V${y1 + rr} Q${x},${y1} ${x + rr},${y1} H${x + bw - rr} ` +
        `Q${x + bw},${y1} ${x + bw},${y1 + rr} V${y0} Z` }));
      base += v;
    });
    const hit = svg("rect", { x: pad.l + i * cw, y: pad.t, width: cw, height: H - pad.t - pad.b, fill: "transparent" });
    hit.addEventListener("mousemove", e => {
      tip.hidden = false;
      tip.replaceChildren(h("div", {}, d.toLocaleString(LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
        h("div", {}, T("сыграно: {n}", { n: c.p })), h("div", {}, T("возврат: {n}", { n: c.f })));
      tip.style.left = Math.min(innerWidth - 160, e.clientX + 12) + "px"; tip.style.top = (e.clientY + 12) + "px";
    });
    hit.addEventListener("mouseleave", () => tip.hidden = true);
    s.append(hit);
  });
  box.replaceChildren(s);
  const active = cols.filter(c => c.p + c.f);
  $("#hours-table").replaceChildren(active.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, T("Час")),
    h("th", { class: "num" }, T("Сыграно")), h("th", { class: "num" }, T("Возврат")))),
    h("tbody", {}, ...active.map(c => h("tr", {}, h("td", {}, new Date(c.t * 1000).toLocaleString(LOCALE,
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
      h("td", { class: "num" }, c.p), h("td", { class: "num" }, c.f))))) : h("p", { class: "muted" }, T("Активаций не было.")));
}
function niceStep(max) {
  const raw = max / 4, p = 10 ** Math.floor(Math.log10(raw)), n = raw / p;
  return Math.max(1, (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p);
}

// --- язык: страница перезагружается, тексты для зрителей по умолчанию сервер меняет сам (i18n.py) ---
$("#lang").value = LANG;
$("#lang").onchange = async e => {
  if ((await api("/api/settings", { lang: e.target.value })).ok) location.reload();
  else e.target.value = LANG;
};
$("#prof-sample").placeholder = T("проверить фразу");   // у поля translate="no": образец мата остаётся русским

// --- старт ---
// Первый запуск: без модели — сразу на вкладку «Сервер»
const firstTab = () => S.setup && !S.setup.models.length ? "server" : S.twitch.state === "ok" ? "queue" : "twitch";
reload().then(() => { showTab(location.hash.slice(1) || firstTab()); connect(); });
