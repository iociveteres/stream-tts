"use strict";
/** Вкладка «Очередь»: фразы, пропуск и очистка, тестовая фраза, «Как прочитает». */
// --- очередь ---
const STATES = { queued: T("ждёт"), synth: T("синтез"), ready: T("готово"), playing: T("играет") };
const SOURCE_TAGS = { test: [T("тест"), ""], command: [T("команда"), T("Команда в чате, без баллов")],
  chat: [T("чат"), T("Сообщение чата, без баллов")] };
function renderQueue() {
  const q = S.engine.queue;
  const playing = q.find(i => i.state === "playing");
  $("#now").replaceChildren(playing ? h("span", {}, h("b", {}, playing.display + ": "), playing.text,
    h("span", { class: "muted small" }, `  · ${playing.voice}`)) : T("Ничего не воспроизводится"));
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
