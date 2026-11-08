"use strict";
/** Вкладка «Очередь»: фразы, пропуск и очистка, тестовая фраза, «Как прочитает». */
// --- очередь ---
const STATES = { queued: T("ждёт"), synth: T("синтез"), ready: T("готово"), playing: T("играет") };
const SOURCE_TAGS = { test: [T("тест"), ""], command: [T("команда"), T("Команда в чате, без баллов")],
  chat: [T("чат"), T("Сообщение чата, без баллов")] };
function renderQueue() {
  const q = S.engine.queue;
  const playing = q.find(i => i.state === "playing");
  const paused = S.engine.paused;
  $("#now").replaceChildren(playing ? h("span", {}, paused ? h("span", { class: "tag" }, T("пауза")) : null,
    h("b", {}, playing.display + ": "), playing.text,
    h("span", { class: "muted small" }, `  · ${playing.voice}`))
    : paused ? T("Пауза: следующая фраза ждёт") : T("Ничего не воспроизводится"));
  $("#pause").textContent = paused ? T("▶ Продолжить") : T("⏸ Пауза");
  $("#pause").classList.toggle("primary", paused);
  if (Date.now() - volumeDrag > 1500) $("#volume").value = S.engine.volume;
  $("#volume-val").textContent = $("#volume").value + "%";
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
  $("#ovstate").replaceChildren(!ov.active ? h("span", { class: "bad" }, T("Источник в OBS не подключён, фразы ждут в очереди"))
    : ov.ready ? h("span", { class: "good" }, ov.count > 1 ? T("Подключён (окон: {n}, играет последнее).", { n: ov.count })
      : T("Подключён."))
    : h("span", { class: "warn" }, T("Подключён, но звук не включён: кликните «Включить звук» в окне оверлея.")));
  $("#journal").replaceChildren(...S.engine.journal.map(j => h("li", {}, h("time", {}, fmtClock(j.ts)), j.text)));
}
$("#skip").onclick = () => api("/api/skip");
$("#pause").onclick = () => api("/api/pause", { on: !S.engine.paused });
// громкость: пока ползунок тянут, состояние с сервера его не двигает; запросы — не чаще раза в 120 мс
let volumeDrag = 0, volumeTimer = null;
$("#volume").oninput = () => {
  volumeDrag = Date.now();
  $("#volume-val").textContent = $("#volume").value + "%";
  volumeTimer = volumeTimer || setTimeout(() => { volumeTimer = null; api("/api/volume", { value: +$("#volume").value }); }, 120);
};
$("#volume").onchange = () => { volumeDrag = Date.now(); api("/api/volume", { value: +$("#volume").value }); };
$("#clear").onclick = async () => {
  if (await ask(T("Очистить очередь и вернуть баллы за всё в ней?"), T("Очистить"), true)) api("/api/clear");
};
$("#ovurl").textContent = location.origin + "/overlay";
$("#dockurl").textContent = location.origin + "/dock";
$("#vurl").textContent = location.origin + "/voices/";
$$("[data-copy]").forEach(b => b.onclick = () =>
  navigator.clipboard.writeText($("#" + b.dataset.copy).textContent).then(() => toast(T("Скопировано"))));

// --- страница голосов в интернете (туннель Cloudflare, tunnel.py) ---
function renderTunnel() {
  const t = S.tunnel, d = t.download;
  const mb = n => Math.round(n / 1048576);
  const text = {
    off: [T("Выключено"), "muted"],
    download: [d && d.total ? T("Скачивание cloudflared: {done} из {total} МБ", { done: mb(d.done), total: mb(d.total) })
      : T("Скачивание cloudflared"), ""],
    starting: [T("Подключение к Cloudflare…"), ""],
    on: [T("Открыта для зрителей"), "good"],
    error: [T("Ошибка: {e}. Повтор через несколько секунд.", { e: t.error }), "bad"],
  }[t.state] || [t.state, ""];
  $("#tunnel-state").replaceChildren(h("span", { class: text[1] || null }, text[0]));
  $("#tunnel-toggle").textContent = t.on ? T("Закрыть")
    : t.have ? T("Открыть") : T("Открыть (скачать {n} МБ)", { n: mb(t.size) });
  $("#tunnel-toggle").classList.toggle("primary", !t.on);
  $("#tunnel-link").hidden = !t.url;
  $("#tunnel-url").href = t.url || "#";
  $("#tunnel-url").textContent = t.url;
}
$("#tunnel-toggle").onclick = () => api("/api/tunnel", { on: !S.tunnel.on });

// --- страница голосов на GitHub Pages (pages.py): вход по коду, один шаг включения Pages, адрес ---
let lastPages = "";
function renderPages() {
  const p = S.pages, key = JSON.stringify(p);
  if (!p || key === lastPages) return;   // перерисовка — только при изменениях: кнопки не пересоздаются на каждом пуше
  lastPages = key;
  const link = (href, text, cls) => h("a", { href, target: "_blank", rel: "noopener noreferrer", class: cls || null }, text);
  const kids = [];
  if (!p.client) {
    kids.push(h("p", { class: "muted" }, T("Вход через GitHub в этой сборке не настроен")));
  } else if (p.state === "device" && p.device) {
    kids.push(h("p", {}, T("Откройте страницу GitHub, войдите своим аккаунтом и введите код:")),
      h("div", { class: "code" }, p.device.code),
      h("div", { class: "row" }, link(p.device.uri, T("Открыть github.com/login/device"), "btn primary"),
        copyBtn(p.device.code), h("span", { class: "muted small grow" }, T("код действует до {time}",
          { time: new Date(p.device.expires * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) })),
        h("button", { onclick: () => api("/api/pages/logout") }, T("Отмена"))),
      h("p", { class: "muted small" }, T("Страница сама обновится, когда вы подтвердите вход. Разрешение — запись в " +
        "публичные репозитории: программа создаст репозиторий tts-voices и будет выкладывать в него страницу.")));
  } else if (!p.login) {
    kids.push(h("div", { class: "row" }, h("span", { class: "grow muted" }, T("Не подключено")),
      h("button", { class: "primary", onclick: () => api("/api/pages/login") }, T("Подключить GitHub"))));
  } else {
    kids.push(h("div", { class: "row" }, h("span", { class: "grow" }, T("Репозиторий: "), link(p.repo_url, p.repo)),
      h("button", { onclick: () => api("/api/pages/publish") }, T("Опубликовать сейчас")),
      h("button", { onclick: async () => await ask(T("Отключить GitHub? Страница и репозиторий останутся на GitHub, " +
        "но перестанут обновляться."), T("Отключить")) && api("/api/pages/logout") }, T("Отключить"))));
    if (p.checked && !p.pages)
      kids.push(h("div", { class: "hint info mt" }, h("b", {}, T("Последний шаг — включите Pages (один раз)")),
        h("ol", {}, h("li", {}, link(p.settings_url, T("Откройте настройки Pages репозитория"))),
          h("li", {}, T("В «Branch» выберите {branch} и папку / (root), нажмите Save.", { branch: p.branch })),
          h("li", {}, T("Программа заметит это сама, адрес появится здесь.")))));
    if (p.url)
      kids.push(h("div", { class: "row mt" }, link(p.url, p.url, "grow"), copyBtn(p.url)));
    const line = p.state === "working" ? [T("Публикация…"), ""]
      : p.state === "error" ? [T("Ошибка: {e}. Повтор через минуту.", { e: p.error }), "bad"]
      : !p.checked ? [T("Проверка репозитория…"), "muted"]
      : p.pending ? [T("Есть изменения — опубликую, когда голоса и примеры перестанут меняться."), "muted"]
      : p.published ? [T("Опубликовано в {time}. GitHub обновляет страницу за минуту-две.",
        { time: fmtClock(p.published) }), "good"]
      : [T("Страница на GitHub актуальна."), "good"];
    kids.push(h("p", { class: "small " + line[1] }, line[0]));
  }
  if (p.error && p.state !== "error") kids.push(h("p", { class: "bad" }, p.error));
  $("#pages").replaceChildren(...kids);
}

function renderTestVoices() {
  const sel = $("#test select[name=voice]"), cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, T("как у зрителя (по нику)")),
    ...S.voices.filter(v => v.enabled).map(v => h("option", { value: v.name }, v.name)));
  sel.value = cur;
}
$("#test-accent").onclick = async () => {
  const out = $("#accent-out"), text = $("#test").text.value;
  const r = await api("/api/accent", { text });
  out.hidden = !r.ok;
  if (!r.ok) return;
  // слово со словарём ударений — щелчком: под выводом появляется поле для правки «+»
  const fix = h("div", { class: "row mt", hidden: true });
  const words = S.cosy.capabilities.user_accents
    ? r.accented.split(/([А-Яа-яЁё+]+(?:-[А-Яа-яЁё+]+)*)/).map((part, i) => i % 2 ? h("button", { type: "button",
        class: "link", title: T("Поправить ударение"), onclick: () => editAccent(fix, part) }, part) : part)
    : [r.accented];
  out.replaceChildren(...[h("div", {}, h("span", { class: "muted" }, T("Модель прочитает: ")), ...words),
    r.normalized !== r.text ? h("div", { class: "muted" }, T("После нормализации: {text}", { text: r.normalized })) : null,
    fix].filter(Boolean));
};
function editAccent(box, word) {
  const input = h("input", { value: word.toLowerCase(), maxlength: 64, spellcheck: "false", class: "grow",
    "aria-label": T("Слово с ударением") });
  const save = async () => { if (await addAccent(input.value)) $("#test-accent").click(); };
  input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); save(); } };
  box.replaceChildren(input, h("button", { type: "button", class: "primary", onclick: save }, T("В словарь")),
    h("button", { type: "button", onclick: () => { box.hidden = true; } }, T("Отмена")));
  box.hidden = false;
  input.focus();
}
$("#test").onsubmit = async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const r = await api("/api/test", Object.fromEntries(f));
  if (r.ok) toast(T("Отправлено в очередь"));
};
