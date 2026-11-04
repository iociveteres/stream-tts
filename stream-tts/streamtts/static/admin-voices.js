"use strict";
/** Вкладка «Голоса»: список, прослушивание, включение, примеры голосов. */
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
