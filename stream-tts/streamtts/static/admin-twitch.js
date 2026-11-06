"use strict";
/** Вкладка «Twitch»: режим подключения, вход, гайд регистрации приложения, способы заказа, ответ
 *  зрителю, награда. */
// --- Twitch: режим подключения ---
let pendingMode = "";   // «Только чтение чата» выбрано, но канал ещё не введён: режим на сервере прежний
const twitchMode = () => pendingMode || S.twitch.mode;
$$("#mode-card input[name=twitch-mode]").forEach(r => r.onchange = async () => {
  pendingMode = "";
  if (r.value === "chat" && !S.twitch.chat.channel) pendingMode = "chat";
  else if ((await api("/api/twitch/mode", { mode: r.value })).ok) await reload();
  lastLogin = "";
  renderLogin(); renderSources(true);
  if (pendingMode) $("#chat-form").channel.focus();
});
$("#chat-form").onsubmit = async e => {
  e.preventDefault();
  if (!(await api("/api/twitch/mode", { mode: "chat", channel: e.target.channel.value })).ok) return;
  pendingMode = "";
  await reload();
  e.target.channel.value = S.twitch.chat.channel;   // ссылка или #ник → ник, как его понял сервер
  lastLogin = "";
  renderLogin(); renderSources(true);
};
$("#chat-form").channel.oninput = () => renderChatState();
function renderChatState() {
  const c = S.twitch.chat, f = $("#chat-form"), chat = twitchMode() === "chat";
  f.hidden = !chat;
  $("#chat-state").hidden = !chat || !c.channel || pendingMode === "chat";
  if (!chat) return;
  if (document.activeElement !== f.channel && f.dataset.saved !== c.channel) f.channel.value = c.channel;
  f.dataset.saved = c.channel;
  f.querySelector("button").disabled = !pendingMode && f.channel.value.trim().toLowerCase() === c.channel
    && c.state !== "not_found";
  const st = { ok: [T("Подключено к #{ch}", { ch: c.channel }), "good"], connecting: [T("Подключение…"), "warn"],
    not_found: [c.error, "bad"] }[c.state] || ["", ""];
  $("#chat-state").replaceChildren(h("span", { class: st[1] }, st[0]),
    c.state === "connecting" && c.error ? h("span", { class: "muted small" }, " · " + c.error) : "");
}

// --- Twitch: вход ---
let lastLogin = "", lastState = null;
function renderLogin() {
  const t = S.twitch, key = JSON.stringify([twitchMode(), t.chat, t.state, t.error, t.device, t.login, t.client_id,
    t.ws, guideOpen]);
  if (key === lastLogin) return;
  lastLogin = key;
  const chat = twitchMode() === "chat";
  for (const r of $$("#mode-card input[name=twitch-mode]")) r.checked = r.value === twitchMode();
  renderChatState();
  $("#login-card").hidden = $("#guide-card").hidden = chat;
  if (chat) {
    $("#sources-card").hidden = false;
    $("#tab-twitch").prepend($("#mode-card"));
    return;
  }
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
  tab.prepend($("#mode-card"));
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
  const name = (S.twitch.login || T("имя_канала")) + "-stream-tts";
  return [
    { title: T("Включите двухфакторную аутентификацию"),
      body: [h("p", {}, T("2FA необходима, чтобы зарегистрировать приложение через консоль разработчика Twitch.")),
        h("a", { class: "btn", href: "https://www.twitch.tv/settings/security", target: "_blank", rel: "noopener" },
          T("Настройки безопасности Twitch ↗")),
        h("p", { class: "muted small" }, T("«Безопасность и конфиденциальность» → «Двухфакторная аутентификация»."))],
      mock: h("div", { class: "mock" }, mockBar("twitch.tv/settings/security"), h("div", { class: "side" },
        h("div", {}, h("div", {}, T("Профиль")), h("div", {}, T("Каналы и видео")),
          h("div", { class: "hl" }, T("Безопасность"))),
        h("div", { class: "body" }, h("b", {}, T("Безопасность")),
          h("div", {}, T("Пароль")), h("div", { class: "hl" }, T("Двухфакторная аутентификация") + "  ",
            h("span", { class: "btnm" }, T("Включить"))))))
    },
    { title: T("Откройте консоль разработчика"),
      body: [h("p", {}, T("Войдите тем же аккаунтом. Ссылка ведёт сразу на форму «Register Your Application».")),
        h("a", { class: "btn", href: "https://dev.twitch.tv/console/apps/create", target: "_blank", rel: "noopener" },
          "dev.twitch.tv/console ↗"),
        h("p", { class: "muted small" }, "«Applications» → «+ Register Your Application»")],
      mock: h("div", { class: "mock" }, mockBar("dev.twitch.tv/console/apps"), h("div", { class: "side" },
        h("div", {}, h("div", {}, "Dashboard"), h("div", { style: "color:var(--ink)" }, "Applications"), h("div", {}, "Extensions")),
        h("div", { class: "body" }, h("b", {}, "Applications"),
          h("div", {}, h("span", { class: "btnm hl" }, "+ Register Your Application")))))
    },
    { title: T("Заполните форму"),
      body: [h("ul", {},
          h("li", {}, "Name: ", h("code", {}, name), " ", copyBtn(name),
            h("div", { class: "muted small" }, T("Имя должно быть уникальным, занято — добавьте что-нибудь."))),
          h("li", {}, "OAuth Redirect URLs: ", h("code", {}, "http://localhost"), " ", copyBtn("http://localhost"),
            " " + T("и кнопка Add"), h("div", { class: "muted small" },
              T("Поле обязательное, но для входа по коду не используется. Twitch может написать, " +
              "что адрес должен быть HTTPS. К localhost это не относится, http://localhost будет работать."))),
          h("li", {}, T("Category: любая, например Broadcaster Suite.")),
          h("li", {}, h("b", { class: "warn" }, "Client Type: Public. "),
            T("Тип клиента после создания, вероятно, не поменять. С Confidential вход по коду не работает, " +
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
  const src = S.settings.sources, chat = twitchMode() === "chat";
  $$("#sources-card .src").forEach(box => {
    const c = src[box.dataset.src], off = chat && box.dataset.src === "reward";   // награде нужен вход
    box.querySelector(".src-body").hidden = !c.on || off;
    box.querySelector(".src-head input").disabled = off;
    for (const el of box.querySelectorAll("[data-k]")) {
      if (el === document.activeElement && !force) continue;
      const v = c[el.dataset.k];
      if (el.type === "checkbox") el.checked = !!v && !off; else el.value = Array.isArray(v) ? v.join(", ") : v;
    }
  });
  $("#reply-box").hidden = chat;   // писать в чат без входа нельзя
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
  if (twitchMode() === "chat") { $("#reward-state").textContent = T("нужно приложение Twitch"); return; }
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
