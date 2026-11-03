// Обучение LoRA голоса ESpeech (engines/espeech/train.py, вкладка «Голоса» → «LoRA» у голоса): набор реплик, ход
// обучения на сервере синтеза, прослушивание снимков и сохранение в голос. Отдельный файл после editor.js: записи с
// волной (Waves) и воспроизведение (edPlayUrl, edStop) — оттуда, помощники h, T, api, ask, S — из admin.js.
// Набор живёт на сервере stream-tts, здесь его копия TR.set; тексты реплик уходят туда с задержкой (trChanged).
// Ход обучения — опросом /api/train/status: раз в секунду, пока оно идёт, иначе раз в 4 с.
const TR = { voice: "", set: null, t: null, error: "", poll: 0, timer: 0, texts: {}, matrixKey: "", saving: null };
const TR_LINE = { min: 0.3, max: 30, ok: [1, 15] };   // с: реплика (engines/espeech/train.py), совет — без длинных
/** Обучение есть у выбранной модели: ESpeech, сервер объявил capabilities.train. */
function trAvailable() { return !!S && S.cosy.engine === "espeech" && !!S.cosy.capabilities.train; }
const trBusy = t => !!t && (t.state === "running" || t.state === "stopping");
const trOn = () => !$("#train").hidden;
const trMine = () => !!TR.t && TR.t.state !== "idle" && TR.t.voice === TR.voice;
const enc = encodeURIComponent;

async function trOpen(voice) {
  if (trOn()) await trFlush();
  edStop();
  Object.assign(TR, { voice, set: null, t: null, error: "", texts: {}, matrixKey: "" });
  trWaves.reset();
  $("#voices-list").hidden = $("#editor").hidden = true;
  $("#train").hidden = false;
  showTab("voices");
  scrollTo(0, 0);
  await trLoad();
}
async function trClose() {
  edStop();
  clearTimeout(TR.poll);
  await trFlush();
  $("#train").hidden = true;
  $("#voices-list").hidden = false;
}
$("#tr-close").onclick = trClose;
/** Из renderLive: модель сменилась на ту, у которой обучения нет, — страница закрывается. */
function trSync() {
  if (trOn() && S.cosy.engine !== "espeech" && !S.cosy.switching) trClose();
}

async function trLoad() {
  const r = await fetch(`/api/train?voice=${enc(TR.voice)}`);
  if (r.status === 401) { location.href = "/login"; return; }
  const j = await r.json();
  if (!j.ok) { toast(j.error, true); trClose(); return; }
  TR.set = j;
  TR.t = j.train;
  TR.error = j.error;
  renderTrain();
  trSchedule();
}
function trSchedule() {
  clearTimeout(TR.poll);
  if (trOn()) TR.poll = setTimeout(trPoll, trBusy(TR.t) ? 1000 : 4000);
}
async function trPoll() {
  if (!trOn()) return;
  try {
    const j = await (await fetch("/api/train/status")).json();
    if (j.ok) { TR.t = j.train; TR.error = ""; } else TR.error = j.error;
  } catch (e) { TR.error = T("Сервер недоступен: {e}", { e: e.message }); }
  renderTrStatus();
  trSchedule();
}
/** Ответ действия с ходом обучения: сразу на страницу. */
function trTook(r) {
  if (r && r.ok && r.train) { TR.t = r.train; renderTrStatus(); trSchedule(); }
  return r;
}

function renderTrain() {
  $("#tr-title").textContent = T("Обучение голоса {voice} (LoRA)", { voice: TR.voice });
  trWaves.render(TR.set.sources);
  renderTrItems();
  renderTrStatus();
}

// --- реплики ---
const trLen = d => d < TR_LINE.min || d > TR_LINE.max ? "bad" : d < TR_LINE.ok[0] || d > TR_LINE.ok[1] ? "warn" : "good";
const trWaves = new Waves({
  box: $("#tr-sources"),
  url: id => `/api/train/source?voice=${enc(TR.voice)}&id=${id}`,
  bands: sid => TR.set ? TR.set.items.map((it, i) => [it, i]).filter(([it]) => it.src === sid).map(([it, i]) =>
    ({ a: it.start, b: it.end, color: "--accent", alpha: 0.16, label: "#" + (i + 1) })) : [],
  len: trLen,
  actions: s => [h("button", { type: "button", class: "primary", onclick: () => trCut(s) }, T("+ Реплика из выделения"))],
  remove: s => trDeleteSource(s),
  empty: T("Записей для нарезки нет."),
});
function renderTrSummary() {
  const s = TR.set, n = s.items.length, sec = s.items.reduce((a, it) => a + it.sec, 0);
  const noText = s.items.filter(it => !(TR.texts[it.id] ?? it.text).trim()).length;
  const el = $("#tr-summary");
  el.className = "small " + (!n ? "muted" : sec < s.recommended_sec || noText ? "warn" : "good");
  el.textContent = !n ? T("Реплик пока нет.")
    : T("Реплик: {n}, звука: {dur}.", { n, dur: fmtPos(sec) }) + " " +
      (sec < s.recommended_sec ? T("Мало: рекомендуется от 1 минуты.") : T("Объёма хватает.")) +
      (noText ? " " + T("Без текста: {n}.", { n: noText }) : "");
}
function renderTrItems() {
  renderTrSummary();
  const items = TR.set.items, srcs = new Set(TR.set.sources.map(s => s.id));
  $("#tr-clear").hidden = !items.length && !TR.set.sources.length;
  if (!items.length) {
    $("#tr-items").replaceChildren(h("tbody", {}, h("tr", {}, h("td", { class: "muted small" },
      T("Добавьте файлы реплик или вырежьте реплики из записи.")))));
    return;
  }
  $("#tr-items").replaceChildren(h("tbody", {}, ...items.map((it, i) => {
    const area = h("textarea", { class: "short grow", rows: 1, spellcheck: "false",
      placeholder: T("Ровно то, что звучит в реплике; ударение — «+» перед гласной"),
      oninput: e => { TR.texts[it.id] = e.target.value; trChanged(); } });
    area.value = TR.texts[it.id] ?? it.text;
    return h("tr", { "data-id": it.id },
      h("td", { class: "num muted small" }, i + 1),
      h("td", {}, h("button", { type: "button", class: "icon", title: T("Послушать"),
        onclick: e => edPlayUrl(`/api/train/item?voice=${enc(TR.voice)}&id=${it.id}`, e.currentTarget) }, "▶")),
      h("td", { class: "len small " + trLen(it.sec), title: it.name }, fmtSec(it.sec)),
      h("td", { class: "text" }, h("div", { class: "row" }, area,
        h("button", { type: "button", class: "small", title: T("Автоматическая расстановка ударений моделью"),
          onclick: () => trAccent(it, area) }, T("Ударения"))),
        h("div", { class: "muted small origin" }, it.name)),
      h("td", { class: "num" },
        it.src && srcs.has(it.src) ? h("button", { type: "button", class: "icon", title: T("Показать на волне"),
          onclick: () => trWaves.focus(it.src, it.start, it.end) }, "⌖") : null,
        h("button", { type: "button", class: "icon danger", title: T("Удалить реплику"), onclick: () => trDeleteItems([it.id]) },
          "✕")));
  })));
}
function trChanged() {
  clearTimeout(TR.timer);
  TR.timer = setTimeout(trFlush, 600);
  renderTrSummary();
}
/** Изменённые тексты реплик — на сервер; перед стартом обучения и уходом со страницы — сразу. */
async function trFlush() {
  clearTimeout(TR.timer);
  if (TR.saving) await TR.saving;
  const items = Object.entries(TR.texts).map(([id, text]) => ({ id, text }));
  if (!items.length || !TR.set) return true;
  TR.saving = api("/api/train/items", { voice: TR.voice, items });
  try {
    const r = await TR.saving;
    if (!r.ok) return false;
    for (const it of TR.set.items) if (it.id in TR.texts) it.text = TR.texts[it.id];
    for (const { id, text } of items) if (TR.texts[id] === text) delete TR.texts[id];
    return true;
  } finally { TR.saving = null; }
}
async function trAccent(it, area) {
  const r = await api("/api/draft/accent", { text: area.value });   // разметка модели без фильтров озвучки
  if (!r.ok) return;
  area.value = TR.texts[it.id] = r.accented;
  trChanged();
}
async function trDeleteItems(ids) {
  await trFlush();
  const r = await api("/api/train/items/delete", { voice: TR.voice, ids });
  if (!r.ok) return;
  for (const id of ids) delete TR.texts[id];
  TR.set = r.set;
  renderTrItems();
  trWaves.drawAll();
}
async function trCut(s) {
  const sel = trWaves.sel;
  if (!sel || sel.src !== s.id) { toast(T("Сначала выделите отрезок на волне этой записи"), true); return; }
  await trFlush();
  const r = await api("/api/train/cut", { voice: TR.voice, src: s.id, start: sel.a, end: sel.b });
  if (!r.ok) return;
  TR.set = r.set;
  renderTrItems();
  trWaves.drawAll();
  const row = $(`#tr-items tr[data-id="${r.item.id}"]`);
  row.scrollIntoView({ block: "nearest" });
  row.querySelector("textarea").focus();
}
async function trDeleteSource(s) {
  if (!await ask(T("Удалить запись «{file}»? Вырезанные из неё реплики останутся в наборе.", { file: s.name }),
    T("Удалить"), true)) return;
  await trFlush();
  const r = await api("/api/train/source/delete", { voice: TR.voice, id: s.id });
  if (!r.ok) return;
  trWaves.forget(s.id);
  TR.set = r.set;
  trWaves.render(TR.set.sources);
  renderTrItems();
}
$("#tr-clear").onclick = async () => {
  if (!await ask(T("Удалить набор голоса {voice}: реплики и записи для нарезки? Голос и сохранённая в нём LoRA " +
    "останутся.", { voice: TR.voice }), T("Удалить"), true)) return;
  clearTimeout(TR.timer);
  TR.texts = {};
  edStop();
  if ((await api("/api/train/clear", { voice: TR.voice })).ok) { trWaves.reset(); await trLoad(); }
};

/** Файлы — на сервер по одному сырым телом; kind: item (реплика) или source (запись для нарезки). */
async function trUpload(f, kind) {
  try {
    const r = await fetch("/api/train/upload", { method: "POST", body: f, headers: { "Content-Type": "application/octet-stream",
      "X-Filename": enc(f.name), "X-Voice": enc(TR.voice), "X-Kind": kind } });
    const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
    if (!j.ok) { toast(T("{file}: {e}", { file: f.name, e: j.error || T("Ошибка") }), true); return null; }
    return j;
  } catch (e) { toast(T("Сервер недоступен: {e}", { e: e.message }), true); return null; }
}
const stem = name => name.replace(/\.[^.]*$/, "").toLowerCase();
$("#tr-files").onchange = async e => {
  const files = [...e.target.files], state = $("#tr-upload-state");
  e.target.value = "";
  await trFlush();
  // пары «имя.wav» + «имя.txt»: текст подставляется в реплику
  const txt = new Map(files.filter(f => /\.txt$/i.test(f.name)).map(f => [stem(f.name), f]));
  const sounds = files.filter(f => !/\.txt$/i.test(f.name));
  const lonely = [...txt.keys()].filter(k => !sounds.some(f => stem(f.name) === k));
  if (lonely.length) toast(T("Нет звука к текстам: {files}", { files: lonely.map(k => txt.get(k).name).join(", ") }), true);
  let i = 0;
  for (const f of sounds) {
    state.textContent = T("Загружаю {file} ({i} из {n})…", { file: f.name, i: ++i, n: sounds.length });
    const j = await trUpload(f, "item");
    if (!j) continue;
    TR.set = j.set;
    const t = txt.get(stem(f.name));
    if (t) TR.texts[j.item.id] = (await t.text()).replace(/^﻿/, "").trim();
  }
  state.textContent = "";
  await trFlush();
  renderTrItems();
  trWaves.drawAll();
};
$("#tr-source-file").onchange = async e => {
  const files = [...e.target.files], state = $("#tr-upload-state");
  e.target.value = "";
  await trFlush();
  for (const f of files) {
    state.textContent = T("Загружаю {file}…", { file: f.name });
    const j = await trUpload(f, "source");
    if (!j) continue;
    if (j.cut) toast(T("{file}: взяты первые 15 минут", { file: f.name }));
    TR.set = j.set;
  }
  state.textContent = "";
  trWaves.render(TR.set.sources);
  renderTrItems();
};

// --- обучение ---
const fmtMin = s => s == null ? "—" : s < 60 ? T("{n} с", { n: Math.round(s) }) : T("{n} мин", { n: Math.max(1, Math.round(s / 60)) });
function renderTrStatus() {
  if (!TR.set) return;
  const t = TR.t, mine = trMine(), busy = trBusy(t), other = t && t.state !== "idle" && t.voice !== TR.voice;
  // сервер занят другим голосом: одно обучение за раз
  $("#tr-other").hidden = !other && !(t && t.lost);
  $("#tr-other").replaceChildren(...(other ? [
    T("На сервере синтеза — обучение голоса {voice}: одно обучение за раз. Сохраните или сбросьте его результаты на " +
      "его странице.", { voice: t.voice }), " ",
    h("button", { type: "button", class: "link", onclick: () => trOpen(t.voice) }, T("Открыть"))]
    : t && t.lost ? [T("Результаты обучения голоса {voice} пропали: сервер синтеза перезапускался, а они хранятся " +
      "только в его памяти.", { voice: t.lost })] : []));
  const L = (t && t.limits) || {}, g = t && t.gpu, need = L.vram_need_mb;
  $("#tr-steps").placeholder = TR.set.est_steps ? T("по объёму, ≈{n}", { n: TR.set.est_steps }) : T("по объёму");
  $("#tr-steps").disabled = $("#tr-ckpt").disabled = busy;
  // «экономия видеопамяти» — gradient checkpointing: память втрое меньше, шаг в 1,4 раза дольше
  const vram = $("#tr-vram");
  vram.className = "small " + (g && g.checkpointing_advised && !$("#tr-ckpt").checked ? "warn" : "muted");
  vram.textContent = !t ? (TR.error || "")
    : (need ? T("Без экономии обучению нужно около {a} ГБ видеопамяти сверх модели, с экономией — около {b} ГБ, но " +
      "шаги в 1,4 раза дольше; результат тот же.", { a: fmtGB(need.default), b: fmtGB(need.checkpointing) }) : "") +
      (g ? " " + T("Свободно сейчас: {free} из {total} ГБ.", { free: fmtGB(g.free_mb), total: fmtGB(g.total_mb) }) +
        (g.checkpointing_advised ? " " + T("Этого мало — включите экономию видеопамяти.") : "")
        : " " + T("Сервер синтеза работает на процессоре: обучение будет очень долгим."));
  $("#tr-start").disabled = busy || !t || !TR.set.items.length;
  $("#tr-start").textContent = mine && !busy ? T("Начать заново") : T("Начать обучение");
  $("#tr-stop").hidden = !(mine && busy);
  $("#tr-stop").disabled = t && t.state === "stopping";
  const st = $("#tr-state");
  st.className = "small " + (mine && t.state === "error" ? "bad" : mine && t.state === "done" ? "good" : "muted");
  st.textContent = !t ? "" : !mine ? "" : {
    running: T("Идёт обучение"), stopping: T("Останавливается после текущего шага…"),
    done: { steps: T("Обучение закончено"), stopped: T("Остановлено на шаге {n}", { n: t.step }),
      unload: T("Остановлено выгрузкой модели на шаге {n}", { n: t.step }) }[t.finished] || T("Готово"),
    error: t.text || t.error }[t.state] || t.state;
  // ход
  $("#tr-progress").hidden = !mine;
  if (mine) {
    $("#tr-fill").style.width = (t.steps ? Math.min(100, 100 * t.step / t.steps) : 0) + "%";
    const parts = [T("Шаг {step} из {steps}", { step: t.step, steps: t.steps })];
    if (busy && t.eta_sec != null) parts.push(T("осталось ~{t}", { t: fmtMin(t.eta_sec) }));
    if (t.elapsed_sec != null) parts.push(T("прошло {t}", { t: fmtMin(t.elapsed_sec) }));
    if (t.loss != null) parts.push(T("лосс {v}", { v: fmtNum(t.loss, 4) }));
    if (t.s_per_step) parts.push(T("{v} с/шаг", { v: fmtNum(t.s_per_step, 2) }));
    if (t.memory && t.memory.reserved_mb) parts.push(T("видеопамять обучения {v} ГБ", { v: fmtGB(t.memory.reserved_mb) }));
    const d = t.data || {};
    if (d.train_items) parts.push(T("реплик в обучении {n}, в проверке {m}", { n: d.train_items, m: d.val_items || 0 }));
    $("#tr-progress-text").textContent = parts.join(" · ");
  }
  const notes = [];
  if (mine && t.overfit) notes.push(h("p", { class: "small warn" }, T("Пора остановиться: проверочный лосс растёт с шага " +
    "{step}, лучший снимок — шаг {best}. Остановите обучение и сравните «лучший» и «последний» на слух.",
    { step: t.overfit_step, best: t.best ? t.best.step : "—" })));
  if (mine && t.val_text) notes.push(h("p", { class: "small muted" }, t.val_text));
  if (mine && t.error === "oom" && !t.checkpointing) notes.push(h("p", { class: "row" },
    h("button", { type: "button", class: "primary", onclick: () => { $("#tr-ckpt").checked = true; trStart(); } },
      T("Включить экономию и начать снова"))));
  $("#tr-notes").replaceChildren(...notes);
  $("#tr-charts").hidden = !mine || !(t.loss_history.length || t.val_history.length);
  const val = mine && !!(t.data && t.data.val_items);   // без отложенных реплик проверки нет: график не нужен
  $("#tr-val").parentElement.hidden = !val;
  $("#tr-charts").classList.toggle("one", !val);
  if (!$("#tr-charts").hidden) {
    trChart($("#tr-loss"), t.loss_history.map(p => [p.step, p.loss]), t.steps, "var(--series-1)", T("Лосс обучения"));
    if (val) trChart($("#tr-val"), t.val_history.map(p => [p.step, p.val]), t.steps, "var(--series-2)",
      T("Проверочный лосс"), t.best, t.overfit_step);
  }
  renderTrResult();
}

/** Линия по шагам: pts [[шаг, значение]], best {step, val} — точкой, overfit — штрихом «пора остановиться». */
function trChart(box, pts, steps, color, label, best = null, overfit = null) {
  const W = Math.max(280, box.clientWidth || 400), H = 160, pad = { l: 52, r: 10, t: 10, b: 22 };
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
  if (!pts.length) {
    const tx = svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" });
    tx.textContent = T("нет данных");
    s.append(tx);
    box.replaceChildren(s);
    return;
  }
  let lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
  if (hi - lo < 1e-4) { lo -= 0.001; hi += 0.001; }
  const m = (hi - lo) * 0.1;
  lo -= m; hi += m;
  const X = v => pad.l + (W - pad.l - pad.r) * v / Math.max(1, steps), Y = v => pad.t + (H - pad.t - pad.b) * (hi - v) / (hi - lo);
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3;
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v), class: "grid" }));
    const tx = svg("text", { x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" });
    tx.textContent = fmtNum(v, 3);
    s.append(tx);
  }
  for (const v of [0, steps / 2, steps]) {
    const tx = svg("text", { x: X(v), y: H - 6, "text-anchor": v ? v === steps ? "end" : "middle" : "start" });
    tx.textContent = fmtNum(Math.round(v), 0);
    s.append(tx);
  }
  if (overfit != null) s.append(svg("line", { x1: X(overfit), x2: X(overfit), y1: pad.t, y2: H - pad.b, class: "cross" }));
  const d = pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ");
  s.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  if (pts.length === 1) s.append(svg("circle", { cx: X(pts[0][0]), cy: Y(pts[0][1]), r: 3, fill: color }));
  if (best) {
    const c = svg("circle", { cx: X(best.step), cy: Y(best.val), r: 5, fill: "none", stroke: "var(--good)", "stroke-width": 2 });
    const tt = svg("title", {});
    tt.textContent = T("Лучший: шаг {step}, {val}", { step: best.step, val: fmtNum(best.val, 4) });
    c.append(tt);
    s.append(c);
  }
  box.replaceChildren(s);
}

async function trStart() {
  if (!await trFlush()) return;
  const raw = $("#tr-steps").value.trim();
  const body = { voice: TR.voice, steps: raw ? +raw : null, checkpointing: $("#tr-ckpt").checked };
  $("#tr-start").disabled = true;
  try {
    let r = await api0("/api/train/start", body);
    if (!r.ok && r.code === "unsaved") {
      if (!await ask(T("Есть несохранённые результаты прошлого обучения: сбросить их и начать новое?"), T("Сбросить и начать"), true))
        return;
      r = await api0("/api/train/start", { ...body, discard: true });
    }
    if (!r.ok) { toast(r.error || T("Ошибка"), true); return; }
    TR.matrixKey = "";
    trTook(r);
    toast(T("Обучение началось"));
  } finally { renderTrStatus(); }
}
/** Как api(), но без всплывающей ошибки: её разбирает вызывающий. */
async function api0(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login"; return { ok: false }; }
    return await r.json().catch(() => ({ ok: false, error: r.statusText }));
  } catch (e) { return { ok: false, error: T("Сервер недоступен: {e}", { e: e.message }) }; }
}
$("#tr-start").onclick = trStart;
$("#tr-ckpt").onchange = renderTrStatus;
$("#tr-stop").onclick = async () => {
  $("#tr-stop").disabled = true;
  trTook(await api("/api/train/stop"));
};

// --- результат: тестовые фразы со снимками, сохранение, сброс ---
function trColumns() {
  const t = TR.t, snaps = (trMine() && t.snapshots) || [];
  const cols = [{ adapter: "none", label: T("Без LoRA") }];
  if (snaps.includes("best")) cols.push({ adapter: "best", label: T("Лучший, шаг {n}", { n: t.best.step }) });
  if (snaps.includes("last")) cols.push({ adapter: "last", label: trBusy(t) ? T("Последний, идёт") : T("Последний, шаг {n}", { n: t.step }) });
  return cols;
}
function renderTrResult() {
  const t = TR.t, mine = trMine(), busy = trBusy(t), snaps = (mine && t.snapshots) || [];
  const cols = trColumns(), phrases = S.settings.test_phrases;
  // перерисовка — только когда меняются столбцы: иначе опрос раз в секунду сбивал бы кнопку, которая сейчас играет
  const key = JSON.stringify([TR.voice, cols.map(c => c.label), phrases]);
  if (key !== TR.matrixKey) {
    TR.matrixKey = key;
    $("#tr-matrix").replaceChildren(
      h("thead", {}, h("tr", {}, h("th", {}, T("Фраза")), ...cols.map(c => h("th", { class: "cell" }, c.label)))),
      h("tbody", {}, ...phrases.map((p, pi) => h("tr", {}, h("td", { class: "phrase" }, p),
        ...cols.map(c => h("td", { class: "cell" }, h("button", { type: "button", class: "icon", title: T("Послушать"),
          onclick: e => edPlayUrl(`/api/train/synth?voice=${enc(TR.voice)}&adapter=${c.adapter}&phrase=${pi}`,
            e.currentTarget) }, "▶")))))));
  }
  const sel = $("#tr-snapshot"), was = sel.value || (snaps.includes("best") ? "best" : "last");
  const names = { best: T("лучший"), last: T("последний") };
  sel.replaceChildren(...snaps.map(k => h("option", { value: k }, names[k])));
  if (snaps.includes(was)) sel.value = was;
  $("#tr-save").disabled = busy || !snaps.length;
  $("#tr-snapshot").disabled = busy || !snaps.length;
  $("#tr-discard").hidden = !mine;
  $("#tr-discard").disabled = false;
  const saved = mine && t.saved;
  $("#tr-saved").className = "small " + (saved ? "good" : snaps.length && !busy ? "warn" : "muted");
  $("#tr-saved").textContent = saved ? T("В голосе — снимок «{s}» (шаг {n}). Прежний файл: {backup}",
      { s: names[saved.snapshot], n: saved.step, backup: saved.backup })
    : snaps.length && !busy ? T("Результат ещё не сохранён в голос: перезапуск сервера синтеза его выбросит.")
    : busy ? T("Сохранить можно после остановки или конца обучения.") : "";
}
$("#tr-save").onclick = async () => {
  const snapshot = $("#tr-snapshot").value;
  $("#tr-save").disabled = true;
  try {
    const r = await api("/api/train/save", { snapshot });
    if (!r.ok) return;
    toast(T("Голос {voice} сохранён со снимком «{s}»", { voice: r.voice, s: snapshot === "best" ? T("лучший") : T("последний") }));
    await trPoll();
  } finally { renderTrResult(); }
};
$("#tr-discard").onclick = async () => {
  const t = TR.t;
  const msg = trBusy(t) ? T("Остановить обучение и выбросить его результаты?")
    : t.saved ? T("Убрать результаты обучения из памяти сервера? Сохранённый в голос снимок останется.")
    : T("Выбросить результаты обучения? Они не сохранены в голос.");
  if (!await ask(msg, T("Сбросить"), true)) return;
  edStop();
  TR.matrixKey = "";
  trTook(await api("/api/train/discard"));
};
