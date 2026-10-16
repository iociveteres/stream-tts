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
const fmtClock = ts => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtNum = (x, d = 1) => x == null ? "—" : Number(x).toLocaleString("ru-RU", { maximumFractionDigits: d });

let S = null;

function toast(msg, bad) {
  const el = h("div", { class: bad ? "bad" : "" }, msg);
  $("#toast").append(el);
  setTimeout(() => el.remove(), bad ? 7000 : 2500);
}

async function api(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login"; return { ok: false }; }
    const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    if (!j.ok) toast(j.error || "Ошибка", true);
    return j;
  } catch (e) { toast("Сервер недоступен: " + e.message, true); return { ok: false }; }
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
    if (m.settings) { S = m; renderAll(); } else if (S) { Object.assign(S, m); renderLive(); }
  };
  ws.onclose = () => { wsOnline = false; if (S) renderHeader(); setTimeout(connect, 2000); };
}

function renderAll() {
  renderSettings(); renderVoices(); renderBans(); renderGuide(); renderReward(); renderTestVoices();
  renderLive();
}
function renderLive() {
  renderHeader(); renderQueue(); renderLogin(); renderRewardState(); renderDemoProgress(); renderLiveStats();
  renderStreamNote();
}

// --- шапка ---
function pill(text, cls, title) { return h("span", { class: "pill", title }, h("i", { class: "dot " + cls }), text); }
function renderHeader() {
  const t = S.twitch, c = S.cosy, ov = S.engine.overlay;
  $("#channel").textContent = t.login ? "· " + t.login : "";
  const tw = { ok: ["Twitch", "good"], connecting: ["Twitch: подключение", "warn"], device: ["Twitch: ждёт входа", "warn"],
    expired: ["Twitch: вход истёк", "bad"], logged_out: ["Twitch: нет входа", "bad"],
    no_client: ["Twitch: нет Client ID", "bad"] }[t.state] || [t.state, ""];
  const pills = [pill(...tw, t.error)];
  if (t.state === "ok") pills.push(pill("EventSub", t.ws === "connected" ? "good" : "warn", t.ws));
  pills.push(pill(c.ok ? (c.busy ? "cosyvoice: синтез" : "cosyvoice") : "cosyvoice недоступен",
    c.ok ? "good" : "bad", c.ok ? c.url : c.error));
  pills.push(pill(!ov.active ? "оверлей не подключён" : ov.ready ? "оверлей" : "оверлей ждёт клика",
    !ov.active ? "bad" : ov.ready ? "good" : "warn"));
  if (!wsOnline) pills.push(pill("нет связи с сервисом", "bad"));
  $("#pills").replaceChildren(...pills);
  $("#enabled").checked = S.engine.enabled;

  const banner = [];
  if (t.state === "expired") banner.push("Вход в Twitch истёк: войдите заново на вкладке Twitch. Пока входа нет, " +
    "активации не обрабатываются, баллы за них вернутся после входа.");
  if (c.ok === false) banner.push(`cosyvoice недоступен (${c.url}): ${c.error}`);
  $("#banner").hidden = !banner.length;
  $("#banner").replaceChildren(...banner.map(b => h("div", {}, b)));
}
$("#enabled").onchange = e => api("/api/enabled", { on: e.target.checked });

// --- очередь ---
const STATES = { queued: "ждёт", synth: "синтез", ready: "готово", playing: "играет" };
function renderQueue() {
  const q = S.engine.queue;
  const playing = q.find(i => i.state === "playing");
  $("#now").replaceChildren(playing ? h("span", {}, h("b", {}, playing.display + ": "), playing.text,
    h("span", { class: "muted small" }, `  · ${playing.voice}`)) : "Тишина");
  $("#now").classList.toggle("muted", !playing);
  $("#qcount").textContent = q.length ? `(${q.length})` : "";
  $("#qempty").hidden = q.length > 0;
  $("#queue").hidden = !q.length;
  $("#queue tbody").replaceChildren(...q.map(i => h("tr", {},
    h("td", {}, i.display, i.test ? h("span", { class: "tag" }, "тест") : null),
    h("td", {}, i.voice),
    h("td", { class: "text" }, i.text),
    h("td", {}, h("span", { class: "state " + i.state }, STATES[i.state] || i.state,
      i.audio ? ` ${fmtNum(i.audio)} с` : "")),
    h("td", {}, h("button", { class: "icon danger", title: "Убрать и вернуть баллы",
      onclick: () => api("/api/remove", { id: i.id }) }, "✕")))));
  const ov = S.engine.overlay;
  $("#ovstate").replaceChildren(!ov.active ? h("span", { class: "bad" }, "Оверлей не подключён: фразы ждут в очереди.")
    : ov.ready ? h("span", { class: "good" }, `Подключён${ov.count > 1 ? ` (окон: ${ov.count}, играет последнее)` : ""}.`)
    : h("span", { class: "warn" }, "Подключён, но звук не включён: кликните «Включить звук» в окне оверлея."));
  $("#journal").replaceChildren(...S.engine.journal.map(j => h("li", {}, h("time", {}, fmtClock(j.ts)), j.text)));
}
$("#skip").onclick = () => api("/api/skip");
$("#clear").onclick = () => { if (confirm("Очистить очередь и вернуть баллы за всё в ней?")) api("/api/clear"); };
$("#ovurl").textContent = location.origin + "/overlay";
$("#vurl").textContent = location.origin + "/voices/";
$$("[data-copy]").forEach(b => b.onclick = () =>
  navigator.clipboard.writeText($("#" + b.dataset.copy).textContent).then(() => toast("Скопировано")));

function renderTestVoices() {
  const sel = $("#test select[name=voice]"), cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "как у зрителя (по нику)"),
    ...S.voices.filter(v => v.enabled).map(v => h("option", { value: v.name }, v.name)));
  sel.value = cur;
}
$("#test").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const r = await api("/api/test", Object.fromEntries(f));
  if (r.ok) toast("Отправлено в очередь");
};

// --- Twitch: вход ---
let lastLogin = "";
function renderLogin() {
  const t = S.twitch, key = JSON.stringify([t.state, t.error, t.device, t.login, t.client_id, t.ws]);
  if (key === lastLogin) return;
  lastLogin = key;
  const card = $("#login-card"), kids = [h("h2", {}, "Вход в Twitch")];
  if (t.state === "no_client") {
    kids.push(h("p", {}, "Сначала зарегистрируйте своё приложение Twitch по шагам ниже и вставьте его Client ID."));
  } else if (t.state === "device") {
    kids.push(h("p", {}, "Откройте страницу активации на любом устройстве, войдите аккаунтом канала и введите код:"),
      h("div", { class: "code" }, t.device.code),
      h("div", { class: "row" }, h("a", { class: "btn primary", href: t.device.uri, target: "_blank", rel: "noopener" },
        "Открыть twitch.tv/activate"), h("span", { class: "muted small" },
        `код действует до ${new Date(t.device.expires * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`)),
      h("p", { class: "muted small" }, "Страница сама обновится, когда вы подтвердите вход. Разрешения: управление наградами " +
        "(создать, ставить на паузу, отмечать активации) и чтение чата для команд."));
  } else if (t.state === "ok" || t.state === "connecting") {
    kids.push(h("p", {}, t.state === "ok" ? "Канал: " : "Подключение… ", h("b", {}, t.login || ""),
      t.state === "ok" ? h("span", { class: "muted" }, `  · EventSub: ${{ connected: "подключён",
        connecting: "подключение…", off: "выключен" }[t.ws] || t.ws}`) : null),
      h("div", { class: "row" }, h("button", { onclick: () => confirm("Выйти из Twitch? Награда останется на канале.") &&
        api("/api/twitch/logout") }, "Выйти")));
  } else {
    if (t.state === "expired") kids.push(h("p", { class: "bad" }, "Вход истёк: refresh-токен больше не действует."));
    kids.push(h("p", {}, "Client ID: ", h("code", {}, t.client_id), t.client_id_env ? h("span", { class: "muted small" }, " (из .env)") : null),
      h("button", { class: "primary", onclick: () => api("/api/twitch/login") }, "Войти через Twitch"));
  }
  if (t.error) kids.push(h("p", { class: "bad" }, t.error));
  card.replaceChildren(...kids);
  $("#reward-card").hidden = t.state !== "ok";
}

// --- гайд регистрации ---
const mockBar = url => h("div", { class: "bar" }, h("i"), h("i"), h("i"), h("span", {}, url));
const mockField = (label, value, hl) => h("div", { class: "field" + (hl ? " hl" : "") }, h("b", {}, label),
  h("div", { class: "input" }, value));
function copyBtn(text) {
  return h("button", { type: "button", class: "icon", title: "Копировать",
    onclick: () => navigator.clipboard.writeText(text).then(() => toast("Скопировано")) }, "⧉");
}
function guideSteps() {
  const name = (S.twitch.login || "канал") + "-stream-tts";
  return [
    { title: "Включите двухфакторную аутентификацию",
      body: [h("p", {}, "Без 2FA консоль разработчика Twitch не даст зарегистрировать приложение."),
        h("a", { class: "btn", href: "https://www.twitch.tv/settings/security", target: "_blank", rel: "noopener" },
          "Настройки безопасности Twitch ↗"),
        h("p", { class: "muted small" }, "Раздел «Безопасность и конфиденциальность» → «Двухфакторная аутентификация».")],
      mock: h("div", { class: "mock" }, mockBar("twitch.tv/settings/security"), h("div", { class: "side" },
        h("div", {}, h("div", {}, "Профиль"), h("div", {}, "Каналы и видео"), h("div", { class: "hl" }, "Безопасность")),
        h("div", { class: "body" }, h("b", {}, "Безопасность"),
          h("div", {}, "Пароль"), h("div", { class: "hl" }, "Двухфакторная аутентификация  ",
            h("span", { class: "btnm" }, "Включить")))))
    },
    { title: "Откройте консоль разработчика",
      body: [h("p", {}, "Войдите тем же аккаунтом. Ссылка ведёт сразу на форму «Register Your Application» " +
        "(в консоли: Applications → «+ Register Your Application»)."),
        h("a", { class: "btn", href: "https://dev.twitch.tv/console/apps/create", target: "_blank", rel: "noopener" },
          "dev.twitch.tv/console ↗"),
        h("p", { class: "muted small" }, "Консоль открывается в новой вкладке: внутри другой страницы Twitch её не показывает.")],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps"), h("div", { class: "side" },
        h("div", {}, h("div", {}, "Dashboard"), h("div", { style: "color:var(--ink)" }, "Applications"), h("div", {}, "Extensions")),
        h("div", { class: "body" }, h("b", {}, "Applications"),
          h("div", {}, h("span", { class: "btnm hl" }, "+ Register Your Application")))))
    },
    { title: "Заполните форму",
      body: [h("ul", {},
          h("li", {}, "Name: ", h("code", {}, name), " ", copyBtn(name),
            h("div", { class: "muted small" }, "Имя должно быть уникальным во всём Twitch. Занято — добавьте что-нибудь.")),
          h("li", {}, "OAuth Redirect URLs: ", h("code", {}, "http://localhost"), " ", copyBtn("http://localhost"),
            " и кнопка Add", h("div", { class: "muted small" }, "Поле обязательное, но для входа по коду не используется.")),
          h("li", {}, "Category: любая, например Broadcaster Suite."),
          h("li", {}, h("b", { class: "warn" }, "Client Type: Public. "),
            "Тип клиента после создания, вероятно, не поменять: с Confidential вход по коду не работает, " +
            "придётся регистрировать приложение заново.")),
        h("p", {}, "Нажмите Create.")],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps/create"), h("div", { class: "body" },
        h("b", {}, "Register Your Application"),
        mockField("Name", name), mockField("OAuth Redirect URLs", "http://localhost"),
        mockField("Category", "Broadcaster Suite"),
        h("div", { class: "field hl" }, h("b", {}, "Client Type"),
          h("div", {}, h("span", { class: "radio" }, "Confidential   "), h("span", { class: "radio on" }, "Public"))),
        h("div", {}, h("span", { class: "btnm" }, "Create"))))
    },
    { title: "Скопируйте Client ID",
      body: [h("p", {}, "В списке приложений нажмите Manage у нового приложения и скопируйте Client ID. " +
        "Client Secret не нужен."),
        h("form", { class: "row", onsubmit: async e => {
          e.preventDefault();
          const r = await api("/api/twitch/client_id", { client_id: e.target.client_id.value });
          if (r.ok) { toast("Client ID принят, войдите кодом"); api("/api/twitch/guide", { step: 4, done: true }).then(reload); }
        } }, h("input", { name: "client_id", class: "grow", placeholder: "Client ID", value: S.twitch.client_id || "",
          autocomplete: "off", spellcheck: "false" }), h("button", { class: "primary" }, "Проверить и войти")),
        h("p", { class: "muted small" }, "Сервис сразу пробует начать вход: если Twitch не примет ID, здесь будет ошибка. " +
          "ID можно задать и в .env (TWITCH_CLIENT_ID).")],
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
  const done = new Set(S.settings.guide_done);
  if (S.twitch.client_id) done.add(4);
  if (guideOpen === null) guideOpen = !S.twitch.client_id;
  $("#guide").hidden = !guideOpen;
  $("#guide-card > p").hidden = !guideOpen;
  $("#guide-toggle").textContent = guideOpen ? "Свернуть" : "Показать";
  $("#guide").replaceChildren(...guideSteps().map((s, i) => h("li", { class: done.has(i + 1) ? "done" : "" },
    h("div", {}, h("h3", {}, s.title), ...s.body,
      i < 3 ? h("label", { class: "check mt" }, h("input", { type: "checkbox", checked: done.has(i + 1),
        onchange: e => api("/api/twitch/guide", { step: i + 1, done: e.target.checked }).then(reload) }), "Готово") : null),
    s.mock)));
}
$("#guide-toggle").onclick = () => { guideOpen = !guideOpen; renderGuide(); };

// --- награда ---
function renderReward() {
  const r = S.twitch.reward, f = $("#reward");
  for (const k of ["title", "cost", "cooldown", "prompt"]) f[k].value = r[k] ?? "";
  renderRewardState();
}
function renderRewardState() {
  const r = S.twitch.reward;
  $("#reward-save").textContent = r.exists ? "Сохранить" : "Создать награду";
  $("#reward-delete").hidden = !r.exists;
  $("#reward-state").textContent = !r.exists ? "Награда ещё не создана." :
    r.paused ? "Награда на паузе (озвучка выключена)." : "Награда активна.";
}
$("#reward").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const r = await api("/api/reward", { title: f.title.value, cost: +f.cost.value, cooldown: +f.cooldown.value,
    prompt: f.prompt.value });
  if (r.ok) { toast("Награда сохранена"); reload(); }
};
$("#reward-delete").onclick = async () => {
  if (confirm("Удалить награду с канала?") && (await api("/api/reward/delete")).ok) reload();
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
  } catch (e) { btn.textContent = "▶"; pbtn = null; toast("Пример: " + e.message, true); }
}
function renderVoices() {
  $("#voices tbody").replaceChildren(...S.voices.map(v => {
    const out = h("output", {}, (v.gain_db > 0 ? "+" : "") + v.gain_db);
    const tr = h("tr", { class: v.enabled ? "" : "off", "data-name": v.name },
      h("td", {}, h("input", { type: "checkbox", checked: v.enabled, "aria-label": "включён",
        onchange: async e => { v.enabled = e.target.checked; tr.className = v.enabled ? "" : "off";
          await api("/api/voice", { name: v.name, enabled: v.enabled }); renderTestVoices(); } })),
      h("td", {}, v.name, v.post ? h("span", { class: "tag", title: v.post }, "эффект") : null,
        v.demo ? null : h("span", { class: "tag", title: "Пример ещё не синтезирован" }, "без примера")),
      h("td", {}, h("button", { class: "icon", title: "Послушать пример с поправкой громкости",
        onclick: e => preview(v, e.currentTarget) }, "▶")),
      h("td", {}, h("div", { class: "gain" }, h("input", { type: "range", min: -12, max: 12, step: 0.5, value: v.gain_db,
        "aria-label": "поправка громкости " + v.name,
        oninput: e => { out.textContent = (+e.target.value > 0 ? "+" : "") + e.target.value; },
        onchange: e => { v.gain_db = +e.target.value; api("/api/voice", { name: v.name, gain_db: v.gain_db }); } }), out)),
      h("td", { class: "num muted" }, v.calib_db == null ? "—" : (v.calib_db > 0 ? "+" : "") + fmtNum(v.calib_db) + " дБ"));
    return tr;
  }));
  filterVoices();
  $("#demo-form").demo_phrase.value = S.settings.demo_phrase;
}
function filterVoices() {
  const q = $("#vsearch").value.trim().toLowerCase();
  $$("#voices tbody tr").forEach(tr => tr.hidden = !tr.dataset.name.includes(q));
}
$("#vsearch").oninput = filterVoices;
$("#vall-on").onclick = async () => { await api("/api/voice", { all: true, enabled: true }); reload(); };
$("#vall-off").onclick = async () => {
  if (confirm("Выключить все голоса? Пока ни один не включён, активации будут возвращаться.")) {
    await api("/api/voice", { all: true, enabled: false }); reload(); }
};
$("#demo-form").onsubmit = async e => {
  e.preventDefault();
  if ((await api("/api/settings", { demo_phrase: e.target.demo_phrase.value })).ok) { toast("Сохранено, примеры пересинтезируются"); reload(); }
};
$("#demo-rebuild").onclick = async () => {
  if (confirm("Пересинтезировать примеры всех включённых голосов? Заодно обновится калибровка громкости.")) {
    await api("/api/demos/rebuild"); reload(); }
};
function renderDemoProgress() {
  const d = S.demos;
  $("#demo-progress").textContent = d.todo ? `Готовятся примеры: осталось ${d.todo}` +
    (d.current ? `, сейчас ${d.current}` : " (ждут свободного cosyvoice)") : "Примеры всех включённых голосов готовы.";
  if (d.error) $("#demo-progress").append(h("span", { class: "bad" }, "  Ошибка: " + d.error));
}

// --- настройки ---
function renderSettings() {
  const f = $("#settings");
  for (const el of f.elements) {
    if (!el.name || !(el.name in S.settings)) continue;
    if (el.type === "checkbox") el.checked = !!S.settings[el.name]; else el.value = S.settings[el.name];
  }
  const c = S.config;
  $("#server-info").replaceChildren(
    h("div", {}, "cosyvoice: ", h("code", {}, S.cosy.url)),
    h("div", {}, `Интерфейс: ${c.bind}:${c.port}, пароль ${c.password ? "задан" : "не задан (доступ только с этого ПК)"}`));
  $("#logout-link").hidden = !c.password;
}
$("#settings").onchange = async e => {
  const el = e.target;
  if (!el.name) return;
  const v = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
  if ((await api("/api/settings", { [el.name]: v })).ok) { S.settings[el.name] = v; toast("Сохранено"); }
};
function renderStreamNote() {
  const rtf = S.engine.rtf, note = $("#stream-note");
  note.classList.toggle("warn", rtf != null && rtf > 0.9);
  note.dataset.rtf = rtf ?? "";
  const extra = rtf == null ? "" : rtf > 0.9
    ? ` Сейчас синтез в ${fmtNum(rtf)} раза медленнее реального времени: стриминг будет заикаться.`
    : ` Сейчас синтез в ${fmtNum(1 / rtf)} раза быстрее реального времени.`;
  note.textContent = "На CPU синтез медленнее реального времени, и стриминг там будет заикаться: выключите его. " +
    "Голоса с эффектом (post) всегда синтезируются целиком." + extra;
}
function renderBans() {
  $("#bans").replaceChildren(...S.bans.map(b => h("li", {}, b, h("button", { title: "Разбанить",
    onclick: async () => { await api("/api/ban", { login: b, on: false }); reload(); } }, "✕"))));
  if (!S.bans.length) $("#bans").append(h("li", { class: "muted", style: "padding:2px 12px" }, "никого"));
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
  if (r.ok) { $("#prof-out").textContent = r.sample ? "→ " + r.sample : "Сохранено"; toast("Список корней сохранён"); }
};

// --- статистика ---
function tile(k, v, s) { return h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
  s ? h("div", { class: "s" }, s) : null); }
function renderLiveStats() {
  if (!$("#tab-stats").classList.contains("on")) return;
  const q = S.engine.queue, playing = q.find(i => i.state === "playing"), g = S.gpu;
  $("#live").replaceChildren(
    tile("В очереди", q.length, q.length ? `синтезируется: ${q.filter(i => i.state === "synth").length}` : ""),
    tile("Играет", playing ? playing.display : "—", playing ? playing.voice : ""),
    tile("cosyvoice", S.cosy.ok ? "доступен" : "нет", S.cosy.ok ? (S.cosy.busy || "свободен") : S.cosy.error),
    g ? tile("GPU", `${g.util}%`, `${fmtNum(g.mem_used / 1024)} / ${fmtNum(g.mem_total / 1024)} ГБ · ${g.temp} °C`)
      : tile("GPU", "нет данных", "nvidia-smi недоступен в контейнере"),
    tile("Скорость синтеза", S.engine.rtf == null ? "—" : `${fmtNum(S.engine.rtf, 2)}×`, "время синтеза / длина звука"));
}
async function loadStats() {
  renderLiveStats();
  const hours = +$("#period").value;
  const r = await fetch(`/api/stats?hours=${hours}&test=${$("#with-test").checked ? 1 : 0}`);
  if (!r.ok) return;
  const st = await r.json();
  const t = st.total;
  $("#kpi").replaceChildren(
    tile("Активаций", t.n || 0),
    tile("Сыграно", t.played || 0),
    tile("Возвратов", t.refunded || 0, t.n ? `${fmtNum(100 * (t.refunded || 0) / t.n, 0)}%` : ""),
    tile("Ожидание до звука", st.wait_ms == null ? "—" : `${fmtNum(st.wait_ms / 1000)} с`, "в среднем"),
    tile("Синтез / длина аудио", st.synth_ratio == null ? "—" : `${fmtNum(st.synth_ratio, 2)}×`,
      st.avg_audio_ms ? `средняя фраза ${fmtNum(st.avg_audio_ms / 1000)} с` : ""));
  drawHours(st, hours);
  bars("#top-voices", st.voices.map(x => [x.voice, x.n]));
  bars("#top-viewers", st.viewers.map(x => [x.login, x.n]));
  bars("#reasons", st.reasons.map(x => [x.reason, x.n]));
}
$("#period").onchange = loadStats;
$("#with-test").onchange = loadStats;
setInterval(() => { if ($("#tab-stats").classList.contains("on")) loadStats(); }, 60000);

function bars(sel, rows) {
  const max = Math.max(1, ...rows.map(r => r[1]));
  $(sel).replaceChildren(...(rows.length ? rows.map(([name, n]) => h("div", { class: "b" },
    h("span", { class: "t", title: name }, name), h("span", { class: "n" }, n),
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${100 * n / max}%` }))))
    : [h("span", { class: "muted" }, "нет данных")]));
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
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Активации по часам" });
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
        : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
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
      tip.replaceChildren(h("div", {}, d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
        h("div", {}, `сыграно: ${c.p}`), h("div", {}, `возврат: ${c.f}`));
      tip.style.left = Math.min(innerWidth - 160, e.clientX + 12) + "px"; tip.style.top = (e.clientY + 12) + "px";
    });
    hit.addEventListener("mouseleave", () => tip.hidden = true);
    s.append(hit);
  });
  box.replaceChildren(s);
  const active = cols.filter(c => c.p + c.f);
  $("#hours-table").replaceChildren(active.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Час"),
    h("th", { class: "num" }, "Сыграно"), h("th", { class: "num" }, "Возврат"))),
    h("tbody", {}, ...active.map(c => h("tr", {}, h("td", {}, new Date(c.t * 1000).toLocaleString("ru-RU",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
      h("td", { class: "num" }, c.p), h("td", { class: "num" }, c.f))))) : h("p", { class: "muted" }, "Активаций не было."));
}
function niceStep(max) {
  const raw = max / 4, p = 10 ** Math.floor(Math.log10(raw)), n = raw / p;
  return Math.max(1, (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p);
}

// --- старт ---
reload().then(() => { showTab(location.hash.slice(1) || (S.twitch.state === "ok" ? "queue" : "twitch")); connect(); });
