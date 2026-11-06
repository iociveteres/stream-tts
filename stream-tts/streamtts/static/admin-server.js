"use strict";
/** Вкладка «Сервер»: модель синтеза и её вариант (кванты CosyVoice3), настройки модели, словарь ударений, устройство,
 *  скорость, состояние встроенного сервера. Всё, что качается, — на «Загрузках» (admin-downloads.js): здесь о нехватке
 *  одна строка со ссылкой туда. */
const BACKEND_NAMES = { auto: T("Автоматически"), cuda: T("Видеокарта NVIDIA (CUDA)"), vulkan: T("Видеокарта (Vulkan)"),
  cpu: T("Процессор") };
const RT_STATES = { waiting: T("ждёт"), starting: T("запускается"), running: T("работает"), crashed: T("упал, перезапуск"),
  stopped: T("остановлен") };
const fmtGB = mb => fmtNum(mb / 1024, 1);
/** RTF — время синтеза / длина звука, везде два знака: 0,31 (меньше 1 — быстрее звучания). */
const fmtRtfNum = rtf => rtf == null || !(rtf > 0) ? "—"
  : Number(rtf).toLocaleString(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtRtf = rtf => rtf == null || !(rtf > 0) ? "—" : "RTF " + fmtRtfNum(rtf);

/** Строка-подсказка; link — со ссылкой на «Загрузки», info — синяя (не нехватка, а ход загрузки или справка). */
function hintTo(el, text, { info = false, link = true } = {}) {
  el.hidden = !text;
  if (!text) return;
  el.className = "hint small mt" + (info ? " info" : "");
  el.replaceChildren(text, ...(link ? [" ", h("a", { href: "#downloads" }, T("Загрузки"))] : []));
}

let backendKey = "", variantKey = "";
function renderServer() {
  const st = S.setup, rt = S.cosy.runtime, c = S.cosy;
  const choice = c.engines.filter(e => e.available).length > 1;
  $('#tabs [data-tab="server"]').hidden = !st && !choice && !c.params.length;
  renderEngine();
  $$("#tab-server .managed").forEach(el => el.hidden = !st);
  if (!st) { $("#engine-hint").hidden = $("#variant-box").hidden = true; return; }
  // Нехватка модели: до первой загрузки модель не выбрана; выбранную могли удалить — сервер ждёт её
  hintTo($("#engine-hint"), !c.picked ? T("Модель не скачана.")
    : !engineReady(c.picked) ? T("{engine} не скачана: сервер ждёт модель.", { engine: engineInfo(c.picked).title }) : "");
  // Вариант CosyVoice3 — из скачанных квантов (и положенных вручную *.gguf)
  const vkey = JSON.stringify([c.engine, st.models, st.model, rt.locked.model]);
  if (vkey !== variantKey) {
    variantKey = vkey;
    $("#variant-box").hidden = c.engine !== "cosyvoice" || !st.models.length;
    $("#variant").disabled = st.models.length < 2 || rt.locked.model;
    $("#variant").replaceChildren(...st.models.map(m => h("option", { value: m, selected: m === st.model },
      st.quants.some(q => q.file === m) ? quantOf(m) : m)));
  }
  // Устройство: перерисовка только при смене вариантов, чтобы не сбивать выбор
  const key = JSON.stringify([rt.backends, rt.prefer, rt.locked.backend, rt.devices]);
  if (key !== backendKey) {
    backendKey = key;
    const have = k => k === "auto" || k === "cpu" || rt.backends.some(b => b.toLowerCase().startsWith(k));
    const names = Object.entries(BACKEND_NAMES).filter(([k]) => rt.devices.includes(k));
    $("#backend-form").replaceChildren(...names.map(([k, name]) =>
      h("label", { class: have(k) ? "" : "na" }, h("input", { type: "radio", name: "backend", value: k,
        checked: rt.prefer === k, disabled: rt.locked.backend || !have(k) }),
        h("span", {}, name, have(k) ? "" : h("span", { class: "muted small" }, T(" — не найдено"))))));
  }
  // модель с одним устройством (Supertonic 3 — только процессор): выбирать нечего, совет про видеокарту не к ней
  const only = rt.devices.length === 1;
  $("#backend-about").hidden = only;
  $("#backend-only").hidden = !only;
  $("#backend-only").textContent = only ? T("{engine} работает только на процессоре.", { engine: c.title }) : "";
  $("#backend-lock").hidden = !rt.locked.backend;
  $("#backend-lock").textContent = T("Устройство задано переменной {var}.", { var: rt.device_env });
  const cu = st.cuda, d = dlRunning();
  if (only) hintTo($("#backend-hint"), "");
  else if (d && d.id === "cuda") hintTo($("#backend-hint"), T("CUDA: {pct}%.", { pct: dlPct(d) }), { info: true });
  else if (cu.gpu && !cu.installed) hintTo($("#backend-hint"), T("CUDA не скачана."));
  else if (cu.docker && !cu.gpu && !cu.installed) hintTo($("#backend-hint"), T("Контейнер не видит видеокарту NVIDIA."),
    { info: true, link: false });
  else hintTo($("#backend-hint"), "");
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
$("#variant").onchange = async e => {
  if (!(await api("/api/model/use", { name: e.target.value })).ok) { variantKey = ""; renderServer(); }
};

// Модель синтеза (engines/): выбор и её настройки по схеме params из /status сервера
let engineKey = "";
function renderEngine() {
  const c = S.cosy, key = JSON.stringify([c.engines, c.picked, c.switching, c.params]);
  if (key === engineKey) return;
  engineKey = key;
  // до первой загрузки ни одна не выбрана; нескачанную выбрать нельзя (why — «не скачана»)
  const can = e => e.available && e.ready;
  $("#engine-form").replaceChildren(...c.engines.map(e => h("label", { class: can(e) ? "" : "na" },
    h("input", { type: "radio", name: "engine", value: e.id, checked: e.id === c.picked,
      disabled: !can(e) || !!c.switching }),
    h("span", {}, e.title, can(e) ? "" : h("span", { class: "muted small" }, " — " + e.why)))));
  $("#model-params").replaceChildren(...c.params.map(p => h("label", { class: "mt", title: p.help },
    p.title + " ", p.type === "choice"
      ? h("select", { name: p.key, onchange: e => saveParam(p, e.target.value) },
          ...p.choices.map(v => h("option", { value: v, selected: v === p.value },
            v === p.default ? T("{v} (по умолчанию)", { v }) : String(v))))
      : h("input", { name: p.key, value: p.value, onchange: e => saveParam(p, e.target.value) }))),
    ...c.params.filter(p => p.help).map(p => h("p", { class: "muted small" }, p.help)));
  $("#engine-state").className = "small muted";
  $("#engine-state").textContent = c.switching ? T("Смена модели: {engine}…",
    { engine: (c.engines.find(e => e.id === c.switching) || {}).title || c.switching }) : "";
  $("#accents-card").hidden = !c.capabilities.user_accents;
  if (c.capabilities.user_accents && c.ok) loadAccents();
}

// Словарь ударений пользователя (capabilities.user_accents): хранит сервер модели, здесь — его копия
let accents = [];
async function loadAccents() {
  const r = await fetch("/api/accents").then(r => r.json()).catch(() => ({ ok: false }));
  if (r.ok) { accents = r.words; renderAccents(); }
}
function renderAccents() {
  const q = $("#accents-filter").value.trim().toLowerCase().replaceAll("+", "");
  const shown = accents.filter(w => !q || w.word.includes(q));
  $("#accents-list").replaceChildren(...shown.map(w => h("li", {}, h("span", { translate: "no" }, w.accented),
    h("button", { type: "button", title: T("Убрать из словаря"), onclick: async () => {
      const r = await api("/api/accents/delete", { word: w.word });
      if (r.ok) { accents = r.words; renderAccents(); }
    } }, "✕"))),
    ...(accents.length ? [] : [h("li", { class: "muted small" }, T("Словарь пуст"))]));
}
/** Слово в словарь; → удалось ли. */
async function addAccent(accented) {
  const r = await api("/api/accents", { accented });
  if (!r.ok) return false;
  accents = r.words;
  renderAccents();
  toast(T("В словаре: {word}", { word: r.accented }));
  return true;
}
$("#accents-filter").oninput = renderAccents;
$("#accents-form").onsubmit = async e => {
  e.preventDefault();
  if (await addAccent(e.target.accented.value)) e.target.accented.value = "";
};
async function saveParam(p, v) {
  if ((await api("/api/settings", { [p.key]: p.type === "choice" && typeof p.choices[0] === "number" ? +v : v })).ok)
    toast(T("Сохранено"));
}
$("#engine-form").onchange = async e => {
  const engine = (S.cosy.engines.find(x => x.id === e.target.value) || {}).title;
  if (!await ask(T("Сменить модель синтеза на {engine}? Сервер синтеза перезапустится, озвучка подождёт в очереди.",
    { engine }), T("Сменить")) || !(await api("/api/engine", { engine: e.target.value })).ok) {
    engineKey = ""; renderEngine();   // отказ (идёт обучение LoRA и т. п.): выбор — как был
  }
};

// Скорость синтеза (bench.py): результаты по устройствам и моделям, текущее выделено
function benchVerdict(rtf) {
  return rtf <= 0.5 ? [T("хватает для стрима с запасом"), "good"] : rtf < 0.9 ? [T("хватает для стрима, впритык"), "warn"]
    : [T("без запаса или медленнее реального времени: для стрима не годится, стриминг выключить"), "bad"];
}
function renderBench(rt) {
  const b = S.bench, running = b.state === "running";
  const rows = [...b.results].sort((x, y) => x.rtf - y.rtf);
  const quant = m => (m.match(/_(Q\w+|F16|BF16)\.gguf$/i) || [, m])[1];
  // у ESpeech и Supertonic замер зависит от числа шагов (bench_info клиента)
  const steps = S.cosy.params.find(p => p.param === "nfe_steps" || p.param === "total_step");
  $("#bench-table").replaceChildren(rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "bench" },
    h("thead", {}, h("tr", {}, h("th", {}, T("Устройство")), h("th", {}, T("Модель")),
      h("th", { class: "num", title: T("Время синтеза / длина фразы. Меньше — быстрее") }, "RTF"),
      h("th", { class: "num", title: T("Через сколько секунд начинает звучать длинная фраза при стриминге") },
        T("Первый звук")))),
    h("tbody", {}, ...rows.map(r => {
      const [verdict, cls] = benchVerdict(r.rtf);
      const date = new Date(r.ts * 1000).toLocaleString(LOCALE);
      const on = r.backend === rt.backend && r.model === rt.model && (r.steps == null || steps && r.steps === steps.value);
      return h("tr", { class: on ? "on" : "",
        title: r.steps != null ? T("{date}, фраза {audio} с, шагов {steps}", { date, audio: fmtNum(r.audio), steps: r.steps })
          : T("{date}, фраза {audio} с, кусок {chunk} токенов", { date, audio: fmtNum(r.audio), chunk: r.chunk }) },
        h("td", {}, r.backend), h("td", {}, quant(r.model) + (r.steps != null ? T(", {n} шагов", { n: r.steps }) : "")),
        h("td", { class: "num " + cls, title: verdict }, fmtRtfNum(r.rtf)),
        h("td", { class: "num" }, r.first == null ? "—" : T("{n} с", { n: fmtNum(r.first) })));
    }))))
    : h("p", { class: "muted small" }, T("Замеров ещё нет.")),
    ...(rows.length ? [h("p", { class: "muted small" }, T("RTF — время синтеза / длина фразы. Меньше 1 — быстрее звучания. " +
      "Для стрима нужно меньше 0,9, лучше до 0,5 (зелёный)."))] : []));   // replaceChildren(null) пишет «null»
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
$("#backend-form").onchange = async e => {
  if (!(await api("/api/backend", { backend: e.target.value })).ok) { backendKey = ""; renderServer(); }
};
