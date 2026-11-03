// Редактор голоса из записи (editor.py, вкладка «Голоса» → «+ Добавить голос»): записи и их волна, варианты
// образца, тестовые фразы, сохранение. Отдельный файл после admin.js (его помощники — h, T, api, S — общие).
// Записи с волной (Waves) и воспроизведение берёт и страница обучения LoRA (train.js). Особенности модели —
// D.engine из /api/draft (пределы длины, пакеты, ударения), у ESpeech ещё кнопка «Ударения» и карточка «Темп речи».
// Черновик живёт на сервере; здесь его копия D. Варианты правятся на месте и уходят на сервер целиком с задержкой
// (edChanged), перед синтезом и сохранением — сразу (edFlush): сервер синтезирует то, что у него записано.
const pkg = id => (S.packages || []).find(p => p.id === id);
const musicReady = () => !!(pkg("music") && pkg("music").installed && pkg("music").usable);
let D = null;
// Модель черновика (engines/<id>/editor.py): пределы и совет по длине образца, с; пакеты редактора — без фронтенда
// CosyVoice голос не создать
const edEngine = () => (D && D.engine) || { id: S.cosy.engine, min: 2, max: 30, ok: [10, 20],
  packages: S.cosy.engine === "cosyvoice" ? ["frontend", "music"] : ["music"] };
const edPackages = () => edEngine().packages;
const TEMPO_MAX = 60, SPEED_MAX = 30;   // с и %: отрезок и поправка темпа ESpeech (engines/espeech/editor.py)
const ed = { busy: new Set(), synthAll: false, timer: 0, saving: null };
let actx = null, edSrc = null, edBtn = null, edPlay = null, edAnim = 0;
const audioCtx = () => actx || (actx = new AudioContext());
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const fmtSec = x => T("{n} с", { n: fmtNum(x, 1) });
const fmtPos = t => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;
const lenClass = (d, e = edEngine()) => d < e.min || d > e.max ? "bad" : d < e.ok[0] || d > e.ok[1] ? "warn" : "good";
const vLabel = v => T("В{n}", { n: D.variants.indexOf(v) + 1 });

// Воспроизведение: одно на страницу (редактор и обучение); у записи на волне бежит курсор
function edStop() {
  if (edSrc) { edSrc.onended = null; try { edSrc.stop(); } catch { /* уже остановлен */ } edSrc = null; }
  if (edBtn) { edBtn.textContent = edBtn.dataset.label; edBtn.classList.remove("playing"); edBtn = null; }
  const p = edPlay;
  edPlay = null;
  cancelAnimationFrame(edAnim);
  if (p) p.waves.draw(p.src);
}
/** Играет buf с from до to; src — запись виджета waves, на волне которой бежит курсор. */
function edPlayBuffer(buf, btn, from = 0, to = buf.duration, src = null, waves = null) {
  edStop();
  const ctx = audioCtx();
  edSrc = ctx.createBufferSource();
  edSrc.buffer = buf;
  edSrc.connect(ctx.destination);
  edSrc.onended = () => { edSrc = null; edStop(); };
  edSrc.start(0, from, Math.max(0.05, to - from));
  if (btn) { btn.dataset.label ??= btn.textContent; btn.textContent = "■"; btn.classList.add("playing"); edBtn = btn; }
  if (src && waves) {
    edPlay = { waves, src, start: ctx.currentTime, from };
    const tick = () => { waves.draw(src); edAnim = requestAnimationFrame(tick); };
    tick();
  }
}
async function edPlayUrl(url, btn) {
  if (edBtn === btn) { edStop(); return false; }
  edStop();
  btn.dataset.label ??= btn.textContent;
  btn.textContent = "…";
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await r.text());
    const buf = await audioCtx().decodeAudioData(await r.arrayBuffer());
    edPlayBuffer(buf, btn);
    return true;
  } catch (e) { btn.textContent = btn.dataset.label; toast(e.message, true); return false; }
}

/** Записи с волной: выделение мышью, колесо — масштаб, Shift + колесо — прокрутка, щелчок — послушать с этого
 *  места. Общие у редактора и страницы обучения (train.js). o: box — контейнер; url(id) — WAV записи;
 *  bands(id) — отметки на волне [{a, b, color (переменная CSS), alpha, label, y}] по порядку рисования;
 *  len(секунды) — класс длины выделения (good / warn / bad); actions(запись) — кнопки под волной;
 *  remove(запись) — удалить; empty — текст, когда записей нет. */
class Waves {
  constructor(o) {
    this.o = o;
    this.sources = [];
    this.reset();
    addEventListener("resize", () => { if (o.box.offsetParent) this.drawAll(); });
  }
  reset() { this.bufs = {}; this.views = {}; this.sel = null; }
  forget(id) {
    delete this.bufs[id];
    delete this.views[id];
    if (this.sel && this.sel.src === id) this.sel = null;
  }
  render(sources) {
    this.sources = sources;
    const box = this.o.box;
    if (!sources.length) { box.replaceChildren(h("p", { class: "muted small" }, this.o.empty)); return; }
    box.replaceChildren(...sources.map(s => {
      const canvas = h("canvas", { class: "wave", "data-src": s.id });
      this.wire(canvas, s);
      return h("div", { class: "source" },
        h("div", { class: "row between" },
          h("div", {}, h("b", {}, s.name), " ", h("span", { class: "muted small" }, fmtPos(s.dur))),
          h("div", { class: "row" },
            h("button", { type: "button", class: "icon", title: T("Вся запись"), onclick: () => {
              delete this.views[s.id]; this.draw(s.id); } }, "⤢"),
            h("button", { type: "button", class: "icon danger", title: T("Удалить запись"), onclick: () => this.o.remove(s) },
              "✕"))),
        canvas,
        h("div", { class: "row" },
          h("button", { type: "button", onclick: e => this.playSel(s, e.currentTarget) }, "▶ " + T("Выделение")),
          ...this.o.actions(s),
          h("span", { class: "small", "data-sel": s.id })));
    }));
    for (const s of sources) {
      this.showSel(s.id);
      this.load(s.id).then(() => this.draw(s.id)).catch(e => toast(T("{file}: {e}", { file: s.name, e: e.message }), true));
    }
  }
  async load(id) {
    if (!this.bufs[id]) {
      const r = await fetch(this.o.url(id));
      if (!r.ok) throw new Error(r.statusText);
      this.bufs[id] = await audioCtx().decodeAudioData(await r.arrayBuffer());
    }
    return this.bufs[id];
  }
  view(s) { return this.views[s.id] || { a: 0, b: s.dur }; }
  setView(s, a, b) {
    const span = clamp(b - a, Math.min(1, s.dur), s.dur);
    a = clamp(a, 0, s.dur - span);
    this.views[s.id] = { a, b: a + span };
  }
  drawAll() { this.sources.forEach(s => this.draw(s.id)); }
  draw(sid) {
    const c = this.o.box.querySelector(`canvas.wave[data-src="${sid}"]`), s = this.sources.find(x => x.id === sid);
    const buf = this.bufs[sid];
    if (!c || !s) return;
    const w = c.clientWidth, hh = c.clientHeight, dpr = devicePixelRatio || 1;
    if (!w) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(hh * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); }
    const g = c.getContext("2d"), css = getComputedStyle(document.body), col = n => css.getPropertyValue(n).trim();
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hh);
    g.font = "11px Segoe UI, system-ui, sans-serif";
    const v = this.view(s), X = t => (t - v.a) / (v.b - v.a) * w;
    const band = (a, b, color, alpha) => { g.globalAlpha = alpha; g.fillStyle = color; g.fillRect(X(a), 0, Math.max(1, X(b) - X(a)), hh); g.globalAlpha = 1; };
    for (const m of this.o.bands(sid)) {
      band(m.a, m.b, col(m.color), m.alpha);
      if (m.label) { g.fillStyle = col(m.color); g.fillText(m.label, X(m.a) + 3, m.y || 12); }
    }
    if (this.sel && this.sel.src === sid) band(this.sel.a, this.sel.b, col("--warn"), 0.3);
    if (!buf) { g.fillStyle = col("--muted"); g.fillText(T("загрузка…"), 8, hh / 2); return; }
    // Пики по столбцам: на длинной записи в пикселе десятки тысяч отсчётов, хватает каждого ~200-го
    const data = buf.getChannelData(0), sr = buf.sampleRate, per = (v.b - v.a) * sr / w;
    const stride = Math.max(1, Math.floor(per / 200)), mid = hh / 2;
    g.fillStyle = col("--ink-2");
    for (let x = 0; x < w; x++) {
      const i0 = Math.floor(v.a * sr + x * per), i1 = Math.min(data.length, Math.floor(v.a * sr + (x + 1) * per));
      let lo = 0, hi = 0;
      for (let i = i0; i < i1; i += stride) { const d = data[i]; if (d < lo) lo = d; else if (d > hi) hi = d; }
      g.fillRect(x, mid - hi * mid, 1, Math.max(1, (hi - lo) * mid));
    }
    // Шкала времени
    g.fillStyle = col("--muted");
    const span = v.b - v.a, step = [0.5, 1, 2, 5, 10, 30, 60, 120, 300].find(st => span / st <= 10) || 600;
    for (let t = Math.ceil(v.a / step) * step; t < v.b; t += step) { g.fillRect(X(t), hh - 4, 1, 4); g.fillText(fmtPos(t), X(t) + 2, hh - 5); }
    if (edPlay && edPlay.waves === this && edPlay.src === sid) {
      const t = edPlay.from + audioCtx().currentTime - edPlay.start;
      g.fillStyle = col("--bad");
      g.fillRect(X(t), 0, 1.5, hh);
    }
  }
  wire(c, s) {
    let down = null;
    const tAt = e => { const r = c.getBoundingClientRect(), v = this.view(s); return clamp(v.a + (e.clientX - r.left) / r.width * (v.b - v.a), 0, s.dur); };
    c.onpointerdown = e => { c.setPointerCapture(e.pointerId); down = { x: e.clientX, t: tAt(e) }; };
    c.onpointermove = e => {
      if (!down || Math.abs(e.clientX - down.x) <= 3) return;
      const t = tAt(e);
      this.sel = { src: s.id, a: Math.min(down.t, t), b: Math.max(down.t, t) };
      this.sources.forEach(x => { this.draw(x.id); this.showSel(x.id); });
    };
    c.onpointerup = async e => {
      if (down && Math.abs(e.clientX - down.x) <= 3) {   // щелчок: послушать с этого места
        const t = down.t;
        edPlayBuffer(await this.load(s.id), null, t, s.dur, s.id, this);
      }
      down = null;
    };
    c.onwheel = e => {
      e.preventDefault();
      const v = this.view(s), span = v.b - v.a;
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const d = (e.shiftKey ? e.deltaY : e.deltaX) / c.clientWidth * span;
        this.setView(s, v.a + d, v.b + d);
      } else {
        const t = tAt(e), f = (t - v.a) / span, ns = clamp(span * (e.deltaY > 0 ? 1.25 : 0.8), 1, s.dur);
        this.setView(s, t - f * ns, t - f * ns + ns);
      }
      this.draw(s.id);
    };
  }
  showSel(sid) {
    const el = this.o.box.querySelector(`[data-sel="${sid}"]`);
    if (!el) return;
    const sel = this.sel && this.sel.src === sid ? this.sel : null;
    el.className = "small " + (sel ? this.o.len(sel.b - sel.a) : "muted");
    el.textContent = sel ? `${fmtPos(sel.a)}–${fmtPos(sel.b)} · ${fmtSec(sel.b - sel.a)}` : T("Выделите отрезок мышью");
  }
  async playSel(s, btn) {
    if (edBtn === btn) { edStop(); return; }
    const sel = this.sel && this.sel.src === s.id ? this.sel : this.view(s);
    edPlayBuffer(await this.load(s.id), btn, sel.a, sel.b, s.id, this);
  }
  /** Выделить отрезок записи src и показать его на волне с запасом по краям. */
  focus(src, a, b) {
    const s = this.sources.find(x => x.id === src);
    if (!s) return;
    this.sel = { src, a, b };
    const pad = Math.max(1, (b - a) * 0.25);
    this.setView(s, a - pad, b + pad);
    this.sources.forEach(x => { this.draw(x.id); this.showSel(x.id); });
    this.o.box.querySelector(`canvas.wave[data-src="${src}"]`).scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

async function edLoad() {
  const r = await fetch("/api/draft");
  if (r.status === 401) { location.href = "/login"; return; }
  D = await r.json();
  renderEditor();
}
/** Из renderLive: открытый редактор перечитывает черновик после смены модели (другие пределы, кэш, поля). */
function edSync() {
  if (D && D.engine && !$("#editor").hidden && !S.cosy.switching && S.cosy.engine !== D.engine.id && !ed.loading) {
    ed.loading = true;
    edFlush().then(edLoad).finally(() => { ed.loading = false; });
  }
}
$("#ed-open").onclick = async () => {
  $("#voices-list").hidden = true;
  $("#editor").hidden = false;
  await edLoad();
};
$("#ed-close").onclick = async () => {
  edStop();
  await edFlush();
  $("#editor").hidden = true;
  $("#voices-list").hidden = false;
};
$("#ed-discard").onclick = async () => {
  if (!await ask(T("Удалить черновик: записи, варианты и их синтез? Сохранённые голоса останутся."), T("Удалить"), true))
    return;
  clearTimeout(ed.timer);
  edStop();
  await api("/api/draft/discard");
  edWaves.reset();
  await edLoad();
};

/** Изменения черновика уходят на сервер через 0,6 с после последней правки. */
function edChanged() {
  clearTimeout(ed.timer);
  ed.timer = setTimeout(edFlush, 600);
}
async function edFlush() {
  clearTimeout(ed.timer);
  ed.timer = 0;
  if (ed.saving) await ed.saving;
  ed.saving = (async () => {
    const body = { variants: D.variants.map(({ ready, ...v }) => v), tempo: D.tempo || null, speed: D.speed || 0,
      compare: D.compare, name: D.name, set: D.set };
    const r = await api("/api/draft", body);
    if (!r.ok) return false;
    // готовность синтеза — с сервера (у изменённого варианта новый ключ), остальное остаётся как в форме
    const ready = Object.fromEntries(r.draft.variants.map(v => [v.id, v.ready]));
    D.variants.forEach(v => { v.ready = ready[v.id] || []; });
    D.compare_ready = r.draft.compare_ready;
    renderMatrix();
    return true;
  })();
  try { return await ed.saving; } finally { ed.saving = null; }
}

function renderEditor() {
  const e = edEngine();
  // подсказки модели: <… data-engine="cosyvoice"> видны только у неё
  for (const el of $("#editor").querySelectorAll("[data-engine]")) el.hidden = el.dataset.engine !== e.id;
  $("#ed-title").textContent = T("Новый голос {engine}", { engine: e.title || S.cosy.title });
  edWaves.render(D.sources);
  renderEdVariants();
  renderEdTempo();
  renderMatrix();
  $("#ed-compare").replaceChildren(h("option", { value: "" }, T("— без сравнения —")),
    ...S.voices.map(v => h("option", { value: v.name, selected: v.name === D.compare }, v.name)));
  $("#ed-phrases").value = S.settings.test_phrases.join("\n");
  const f = $("#ed-save");
  f.name.value = D.name;
  f.set.value = D.set;
  $("#ed-sets").replaceChildren(...D.sets.map(n => h("option", { value: n })));
  renderSaveVariants();
}

// Записи и волна: отрезок для темпа ESpeech — под образцом, варианты — поверх
const edWaves = new Waves({
  box: $("#ed-sources"),
  url: id => `/api/draft/source/${id}.wav`,
  bands: sid => D ? [
    ...(D.tempo && D.tempo.src === sid && edEngine().id === "espeech"
      ? [{ a: D.tempo.start, b: D.tempo.end, color: "--good", alpha: 0.1, label: T("темп"), y: 26 }] : []),
    ...D.variants.filter(x => x.src === sid).map(x => ({ a: x.start, b: x.end, color: "--accent", alpha: 0.16, label: vLabel(x) })),
  ] : [],
  len: d => lenClass(d),
  actions: s => [h("button", { type: "button", class: "primary", onclick: () => addVariant(s) }, T("+ Вариант из выделения"))],
  remove: s => edDeleteSource(s),
  empty: T("Записей пока нет."),
});
async function edDeleteSource(s) {
  const n = D.variants.filter(v => v.src === s.id).length;
  if (!await ask(n ? T("Удалить запись «{file}» и её варианты ({n})?", { file: s.name, n })
    : T("Удалить запись «{file}»?", { file: s.name }), T("Удалить"), true)) return;
  await edFlush();
  const r = await api("/api/draft/source/delete", { id: s.id });
  if (!r.ok) return;
  edWaves.forget(s.id);
  D = r.draft;
  renderEditor();
}
const r2 = x => Math.round(x * 100) / 100;
function addVariant(s) {
  const sel = edWaves.sel;
  if (!sel || sel.src !== s.id) { toast(T("Сначала выделите отрезок на волне этой записи"), true); return; }
  const v = { id: "v" + Date.now().toString(36), src: s.id, start: r2(sel.a), end: r2(sel.b), text: "", denoise: false,
    music: false, ready: [] };
  D.variants.push(v);
  renderEdVariants();
  renderMatrix();
  renderSaveVariants();
  edWaves.draw(s.id);
  edChanged();
  $(`.variant[data-id="${v.id}"] textarea`).focus();
}
/** Отрезок a–b записи src: выделить, приблизить и прокрутить к нему. */
const focusRange = (src, a, b) => edWaves.focus(src, a, b);

// Варианты образца
function renderEdVariants() {
  const box = $("#ed-variants");
  if (!D.variants.length) {
    box.replaceChildren(h("p", { class: "muted small" },
      T("Вариантов пока нет. Выделите отрезок на волне записи и нажмите «+ Вариант из выделения».")));
    return;
  }
  box.replaceChildren(...D.variants.map(v => {
    const src = D.sources.find(s => s.id === v.src) || { name: "?", dur: 0 };
    const dur = h("span", { class: "small" });
    const upd = () => { const d = v.end - v.start; dur.textContent = fmtSec(d); dur.className = "small " + lenClass(d); };
    upd();
    const num = (k, label) => h("label", { class: "inline small" }, label, h("input", { type: "number", class: "num",
      step: 0.1, min: 0, max: src.dur, value: v[k], onchange: e => {
        v[k] = clamp(+e.target.value || 0, 0, src.dur); upd(); edWaves.draw(v.src); renderSaveVariants(); edChanged(); } }));
    const esp = edEngine().id === "espeech";
    const text = h("textarea", { class: "short" + (esp ? " grow" : ""), rows: 2, placeholder: esp
        ? T("Расшифровка отрезка")
        : T("Расшифровка отрезка, слово в слово"),
      oninput: e => { v.text = e.target.value; edChanged(); } });
    text.value = v.text;
    return h("div", { class: "variant", "data-id": v.id },
      h("div", { class: "row" },
        h("b", {}, vLabel(v)), h("span", { class: "muted small" }, src.name),
        num("start", T("от, с")), num("end", T("до, с")), dur,
        // музыка убирается до шумоподавления (editor.sample); без пакета галочку можно только снять
        h("label", { class: "check small", title: musicReady() ? T("Голос отделяется от музыки (Demucs) один раз, " +
            "около 10 секунд на 20 секунд записи") : T("Нужен пакет «Очистка образца от музыки»") },
          h("input", { type: "checkbox", checked: !!v.music, disabled: !musicReady() && !v.music,
            onchange: e => { v.music = e.target.checked; edChanged(); } }), T("убрать музыку")),
        h("label", { class: "check small" }, h("input", { type: "checkbox", checked: v.denoise,
          onchange: e => { v.denoise = e.target.checked; edChanged(); } }), T("шумоподавление")),
        h("span", { class: "grow" }),
        h("button", { type: "button", class: "icon", title: T("Показать на волне"), onclick: () => focusRange(v.src, v.start, v.end) }, "⌖"),
        h("button", { type: "button", title: T("Послушать образец с обработкой"), onclick: async e => {
          const btn = e.currentTarget;
          await edFlush();
          edPlayUrl(`/api/draft/sample?variant=${encodeURIComponent(v.id)}`, btn);
        } }, "▶ " + T("Образец")),
        h("button", { type: "button", class: "icon danger", title: T("Удалить вариант"), onclick: () => {
          D.variants = D.variants.filter(x => x !== v);
          renderEdVariants(); renderMatrix(); renderSaveVariants(); edWaves.drawAll(); edChanged();
        } }, "✕")),
      esp ? h("div", { class: "row" }, text, h("button", { type: "button",
        title: T("Автоматическая расстановка ударений моделью"),
        onclick: () => edAccent(v, text) }, T("Ударения"))) : text);
  }));
}
/** Расшифровка варианта — разметкой модели (POST /api/draft/accent: без фильтров озвучки). */
async function edAccent(v, area) {
  const r = await api("/api/draft/accent", { text: area.value });
  if (!r.ok) return;
  v.text = area.value = r.accented;
  renderSaveVariants();
  edChanged();
}
// Темп речи ESpeech (engines/espeech/editor.py): общий для вариантов отрезок с расшифровкой (без него — по образцу
// варианта) и поправка ползунком
function renderEdTempo() {
  const box = $("#ed-tempo");
  if (edEngine().id !== "espeech") { box.replaceChildren(); return; }
  const t = D.tempo, take = h("button", { type: "button", class: t ? null : "primary", onclick: takeTempo },
    t ? T("Взять выделение") : T("+ Отрезок из выделения"));
  let seg;
  if (!t) seg = h("div", { class: "row" }, h("span", { class: "small muted" }, T("Как в образце")), take);
  else {
    const src = D.sources.find(s => s.id === t.src) || { name: "?" }, rate = h("span", { class: "small muted" });
    const upd = () => {   // букв в секунду — для сравнения на глаз, сервер считает кадры на байт
      const n = (t.text.match(/\p{L}/gu) || []).length;
      rate.textContent = n ? T("{n} букв/с", { n: fmtNum(n / (t.end - t.start), 1) }) : "";
    };
    upd();
    const area = h("textarea", { class: "short", rows: 2, placeholder: T("Расшифровка отрезка"),
      oninput: e => { t.text = e.target.value; upd(); edChanged(); } });
    area.value = t.text;
    seg = h("div", { class: "variant" },
      h("div", { class: "row" },
        h("b", {}, T("Отрезок")), h("span", { class: "muted small" }, src.name),
        h("span", { class: "small" }, `${fmtPos(t.start)}–${fmtPos(t.end)} · ${fmtSec(t.end - t.start)}`),
        h("button", { type: "button", title: T("Послушать отрезок"), onclick: e => playTempo(t, e.currentTarget) }, "▶"),
        h("button", { type: "button", class: "icon", title: T("Показать на волне"),
          onclick: () => focusRange(t.src, t.start, t.end) }, "⌖"),
        rate, h("span", { class: "grow" }), take,
        h("button", { type: "button", class: "icon danger", title: T("Убрать отрезок"), onclick: () => {
          D.tempo = null; renderEdTempo(); edWaves.draw(t.src); edChanged(); } }, "✕")),
      area);
  }
  const speed = D.speed || 0, fmt = x => x > 0 ? `+${x} %` : x < 0 ? `−${-x} %` : "0 %", val = h("b", {}, fmt(speed));
  box.replaceChildren(seg, h("div", { class: "row mt" },
    h("span", { class: "small" }, T("Медленнее")),
    h("input", { type: "range", class: "speed", min: -SPEED_MAX, max: SPEED_MAX, step: 5, value: speed,
      "aria-label": T("Поправка темпа"), oninput: e => { val.textContent = fmt(+e.target.value); },
      onchange: e => { D.speed = +e.target.value; renderEdTempo(); edChanged(); } }),
    h("span", { class: "small" }, T("Быстрее")), val,
    speed ? h("button", { type: "button", onclick: () => { D.speed = 0; renderEdTempo(); edChanged(); } },
      T("Сбросить")) : null));
}
function takeTempo() {
  const sel = edWaves.sel;
  if (!sel) { toast(T("Сначала выделите на волне отрезок для темпа"), true); return; }
  if (sel.b - sel.a > TEMPO_MAX) { toast(T("Отрезок для темпа длиннее {n} с", { n: TEMPO_MAX }), true); return; }
  D.tempo = { src: sel.src, start: r2(sel.a), end: r2(sel.b), text: D.tempo ? D.tempo.text : "" };
  renderEdTempo(); edWaves.drawAll(); edChanged();
  $("#ed-tempo textarea").focus();
}
async function playTempo(t, btn) {
  if (edBtn === btn) { edStop(); return; }
  edPlayBuffer(await edWaves.load(t.src), btn, t.start, t.end, t.src, edWaves);
}

// Тестовые фразы: строки — фразы, столбцы — варианты и голос для сравнения
function edColumns() {
  const cols = D.variants.map(v => ({ v, label: vLabel(v), ready: v.ready || [] }));
  if (D.compare) cols.push({ voice: D.compare, label: D.compare, ready: D.compare_ready || [] });
  return cols;
}
const cellUrl = (c, pi) => c.v ? `/api/draft/synth?variant=${encodeURIComponent(c.v.id)}&phrase=${pi}`
  : `/api/draft/synth?voice=${encodeURIComponent(c.voice)}&phrase=${pi}`;
const cellKey = (c, pi) => (c.v ? c.v.id : "ref:" + c.voice) + ":" + pi;
function markReady(c, pi) {
  const list = c.v ? (c.v.ready ||= []) : (D.compare_ready ||= []);
  if (!list.includes(pi)) list.push(pi);
}
function renderMatrix() {
  const cols = edColumns(), phrases = S.settings.test_phrases;
  if (!cols.length) {
    $("#ed-matrix").replaceChildren(h("tbody", {}, h("tr", {}, h("td", { class: "muted small" },
      T("Добавьте вариант образца, чтобы послушать его на тестовых фразах.")))));
    return;
  }
  $("#ed-matrix").replaceChildren(
    h("thead", {}, h("tr", {}, h("th", {}, T("Фраза")),
      ...cols.map(c => h("th", { class: "cell" + (c.voice ? " ref" : ""), title: c.voice ? T("Готовый голос для сравнения") : null },
        c.label)))),
    h("tbody", {}, ...phrases.map((p, pi) => h("tr", {}, h("td", { class: "phrase" }, p),
      ...cols.map(c => {
        const key = cellKey(c, pi), ready = c.ready.includes(pi);
        return h("td", { class: "cell" }, h("button", { type: "button", class: "icon" + (ready ? " ready" : ""),
          title: ready ? T("Послушать") : T("Синтезировать и послушать"),
          onclick: async e => {
            const btn = e.currentTarget;
            if (edBtn !== btn) await edFlush();
            if (await edPlayUrl(cellUrl(c, pi), btn)) { markReady(c, pi); btn.classList.add("ready"); }
          } }, ed.busy.has(key) ? "…" : "▶"));
      })))));
}
$("#ed-compare").onchange = async e => { D.compare = e.target.value; await edFlush(); };
$("#ed-synth-all").onclick = async () => {
  const btn = $("#ed-synth-all");
  if (ed.synthAll) { ed.synthAll = false; return; }
  if (!await edFlush()) return;
  const jobs = edColumns().flatMap(c => S.settings.test_phrases.map((_, pi) => ({ c, pi })))
    .filter(j => !j.c.ready.includes(j.pi));
  if (!jobs.length) { toast(T("Всё уже синтезировано")); return; }
  ed.synthAll = true;
  btn.textContent = T("Остановить");
  let done = 0;
  for (const j of jobs) {
    if (!ed.synthAll || $("#editor").hidden) break;
    $("#ed-progress").textContent = T("Синтез {i} из {n}…", { i: done + 1, n: jobs.length });
    const key = cellKey(j.c, j.pi);
    ed.busy.add(key);
    renderMatrix();
    try {
      const r = await fetch(cellUrl(j.c, j.pi));
      if (!r.ok) throw new Error(await r.text());
      await r.arrayBuffer();
      markReady(j.c, j.pi);
      done++;
    } catch (e) { toast(e.message, true); break; }
    finally { ed.busy.delete(key); renderMatrix(); }
  }
  ed.synthAll = false;
  btn.textContent = T("Синтезировать всё");
  $("#ed-progress").textContent = done ? T("Готово: {n}", { n: done }) : "";
};
$("#ed-phrases-save").onclick = async () => {
  const lines = $("#ed-phrases").value.split("\n").map(x => x.trim()).filter(Boolean);
  if (!(await api("/api/settings", { test_phrases: lines })).ok) return;
  toast(T("Сохранено"));
  await reload();
  await edLoad();
};

// Сохранение
function renderSaveVariants() {
  const sel = $("#ed-save").variant, was = sel.value;
  sel.replaceChildren(...D.variants.map(v => h("option", { value: v.id },
    `${vLabel(v)} · ${fmtSec(v.end - v.start)}` + (v.text ? ` · ${v.text.slice(0, 40)}${v.text.length > 40 ? "…" : ""}` : ""))));
  if (D.variants.some(v => v.id === was)) sel.value = was;
}
$("#ed-save").name.oninput = e => { D.name = e.target.value; edChanged(); };
$("#ed-save").set.oninput = e => { D.set = e.target.value; edChanged(); };
$("#ed-save").onsubmit = async e => {
  e.preventDefault();
  const f = e.target, btn = f.querySelector("button.primary"), out = $("#ed-save-result");
  btn.disabled = true;
  out.className = "small muted";
  out.textContent = T("Сохраняю…");
  try {
    if (!await edFlush()) { out.textContent = ""; return; }
    const r = await api("/api/draft/save", { variant: f.variant.value, name: f.name.value, set: f.set.value });
    if (!r.ok) { out.textContent = ""; return; }
    out.className = "small good";
    out.textContent = T("Голос {name} сохранён в набор {set}. Пример для списка голосов синтезируется сам.",
      { name: r.name, set: r.set });
    if (trAvailable()) out.append(" ", h("button", { type: "button", class: "link", onclick: () => trOpen(r.name) },
      T("Обучить LoRA на репликах этого голоса")));
    toast(T("Голос {name} добавлен", { name: r.name }));
  } finally { btn.disabled = false; }
};

// Загрузка записей
$("#ed-file").onchange = async e => {
  const files = [...e.target.files], state = $("#ed-upload-state");
  e.target.value = "";
  await edFlush();
  for (const f of files) {
    state.textContent = T("Загружаю {file}…", { file: f.name });
    try {
      const r = await fetch("/api/draft/upload", { method: "POST", body: f,
        headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(f.name) } });
      const j = await r.json().catch(() => ({ ok: false, error: r.statusText }));
      if (!j.ok) { toast(T("{file}: {e}", { file: f.name, e: j.error || T("Ошибка") }), true); continue; }
      if (j.cut) toast(T("{file}: взяты первые 15 минут", { file: f.name }));
      D.sources = j.draft.sources;
    } catch (err) { toast(T("Сервер недоступен: {e}", { e: err.message }), true); }
  }
  state.textContent = "";
  edWaves.render(D.sources);
};
