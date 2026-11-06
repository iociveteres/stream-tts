"use strict";
/** Вкладка «Голоса»: список, прослушивание, включение, примеры голосов, наборы голосов (zip) встроенного сервера. */
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
        v.demo ? null : h("span", { class: "tag", title: T("Пример ещё не синтезирован") }, T("без примера")),
        trAvailable() ? h("button", { type: "button", class: "small lora", title: T("Обучить голос на репликах (LoRA)"),
          onclick: () => trOpen(v.name) }, "LoRA") : null),
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

// --- наборы голосов (voicesets.py): zip ставит и удаляет встроенный сервер, в тестах с готовым сервером их нет ---
// Набор — по записи на модель: коллекция «модель + источник» ставится и удаляется отдельно
let setsKey = "";
function renderSets() {
  const st = S.setup;
  $("#sets-card").hidden = !st;
  const key = JSON.stringify(st && [st.sets, st.voices_dir]);
  if (!st || key === setsKey) return;
  setsKey = key;
  $("#voices-dir").textContent = st.voices_dir;
  $("#sets").replaceChildren(...(st.sets.length ? st.sets.map(s => h("li", {},
    h("span", { class: "grow" }, h("b", {}, s.name), h("span", { class: "muted" }, " · " + s.title),
      h("span", { class: "muted small" }, T(" — голосов: {n}: {voices}", { n: s.voices.length,
        voices: s.voices.slice(0, 8).join(", ") + (s.voices.length > 8 ? "…" : "") }))),
    h("button", { class: "icon danger", title: T("Удалить набор"), onclick: () =>
      ask(T("Удалить набор «{name}» (голосов: {n})?", { name: `${s.name} · ${s.title}`, n: s.voices.length }),
        T("Удалить"), true)
        .then(yes => yes && api("/api/voicesets/delete", { name: s.name, model: s.model }))
    }, "✕"))) : [h("li", { class: "muted" }, T("Наборов нет."))]));
}
$("#set-import").onclick = async () => {
  const f = $("#set-file").files[0], out = $("#set-result");
  if (!f) { toast(T("Выберите zip с набором"), true); return; }
  const btn = $("#set-import");
  btn.disabled = true;
  out.className = "small muted";
  out.textContent = T("Устанавливаю {file}…", { file: f.name });
  try {
    const send = force => fetch("/api/voicesets/import", { method: "POST", body: f, headers: { "Content-Type": "application/zip",
      "X-Filename": encodeURIComponent(f.name), ...(force ? { "X-Force": "1" } : {}) } });
    let r = await send(false), j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    // несохранённые результаты обучения LoRA: установка перезапустит сервер синтеза
    if (j.confirm && await ask(j.error, T("Продолжить"), true)) {
      r = await send(true);
      j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    }
    if (!j.ok) { out.className = "small bad"; out.textContent = j.error || T("Ошибка"); return; }
    out.className = "small good";
    out.textContent = T("Набор «{name}»: установлено голосов {n}", { name: j.name, n: j.added.length }) +
      (j.skipped.length ? T(", пропущено {n}: {voices}", { n: j.skipped.length, voices: j.skipped.join(", ") }) : "") +
      (j.ignored.length ? T(". Не голоса, пропущены: {files}", { files: j.ignored.join(", ") }) : "") + ".";
    $("#set-file").value = "";
  } catch (e) { out.className = "small bad"; out.textContent = T("Сервер недоступен: {e}", { e: e.message }); }
  finally { btn.disabled = false; }
};
