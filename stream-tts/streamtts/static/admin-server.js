"use strict";
/** Вкладка «Сервер»: модель и её загрузка, устройство, модель синтеза и её настройки, словарь ударений,
 *  CUDA, ESpeech, Supertonic 3, скорость, наборы голосов; необязательные пакеты (блок — в настройках). */
// --- сервер: модель, устройство, наборы голосов ---
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
/** RTF — время синтеза / длина звука, везде два знака: 0,31 (меньше 1 — быстрее звучания). */
const fmtRtfNum = rtf => rtf == null || !(rtf > 0) ? "—"
  : Number(rtf).toLocaleString(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtRtf = rtf => rtf == null || !(rtf > 0) ? "—" : "RTF " + fmtRtfNum(rtf);

function dlProgress(d) {
  const left = d.speed > 0 ? " · " + T("осталось {time}", { time: fmtDur((d.total - d.done) / d.speed) }) : "";
  return T("{file}: {done} из {total}", { file: d.file || T("подготовка"), done: fmtMB(d.done), total: fmtMB(d.total) }) +
    (d.speed > 0 ? " · " + T("{n} МБ/с", { n: fmtNum(d.speed / 1048576) }) : "") + left;
}

// Дополнительные пакеты (downloads.PACKAGES): блок в настройках и нужные пакеты в редакторе голоса
let packagesKey = "";
function renderPackages() {
  const d = S.setup && S.setup.download, key = JSON.stringify([S.packages, d, S.setup ? 1 : 0, edPackages()]);
  if (key === packagesKey) return;
  packagesKey = key;
  const item = p => {
    const running = d && d.state === "running", mine = d && d.package === p.id;
    let action;
    if (!S.setup) action = p.installed ? h("span", { class: "tag good" }, T("установлен"))
      : h("span", { class: "tag" }, T("не установлен"));
    else if (mine && running) action = h("button", { onclick: () => api("/api/model/cancel") }, T("Отменить"));
    else if (p.installed) action = h("button", { class: "icon danger", title: T("Удалить пакет"), onclick: async () => {
        if (await ask(p.restart ? T("Удалить пакет «{name}»? Сервер синтеза перезапустится.", { name: p.title })
            : T("Удалить пакет «{name}»?", { name: p.title }), T("Удалить"), true))
          api("/api/package/delete", { id: p.id }); } }, "✕");
    else action = h("button", { class: "primary", disabled: running, onclick: () => api("/api/package/download", { id: p.id }) },
      T("Скачать"));
    const status = mine && ["running", "error", "cancelled"].includes(d.state) ? h("div", { class: "small " + ({ error: "bad", done: "good" }[d.state] || "") },
        { running: dlProgress(d), error: T("Ошибка: {e}", { e: d.error }),
          cancelled: T("Загрузка отменена: «Скачать» продолжит с того же места.") }[d.state] || "")
      : null;
    if (!p.usable) action = h("span", { class: "tag" }, T("недоступен"));
    const hint = !p.usable ? T("В этой версии программы нет onnxruntime: пакет работает в программе для Windows, в Docker " +
        "и при запуске из исходников с пакетами из requirements.txt.") : "";
    return h("li", { class: p.installed ? "" : "on" }, h("div", { class: "grow" },
      h("b", {}, p.title), " ", h("span", { class: "muted small" }, fmtMB(p.size)),
      S.setup && p.installed ? h("span", { class: "tag" }, T("установлен")) : null,
      h("div", { class: "small muted" }, p.about),
      hint ? h("div", { class: "small warn" }, hint) : null,
      status), action);
  };
  $("#packages").replaceChildren(...S.packages.map(item));
  $("#ed-packages").replaceChildren(...S.packages.filter(p => edPackages().includes(p.id)).map(item));
  if (D && !$("#editor").hidden) renderEdVariants();   // галочка «убрать музыку» зависит от пакета
}

let backendKey = "";
function renderServer() {
  const st = S.setup, rt = S.cosy.runtime, c = S.cosy;
  const choice = c.engines.filter(e => e.available).length > 1;
  $('#tabs [data-tab="server"]').hidden = !st && !choice && !c.params.length;
  renderEngine();
  $$("#tab-server .managed").forEach(el => el.hidden = !st);
  if (!st) return;
  $("#quants-card").hidden = c.engine !== "cosyvoice";
  // ESpeech: карточка у выбранной модели, а у программы для Windows ещё и пока он не скачан — предложение скачать
  $("#espeech-card").hidden = c.engine !== "espeech" && !(st.espeech.offer && !st.espeech.installed);
  $("#espeech-dir").textContent = st.espeech.model_dir || rt.models_dir || "";
  // Supertonic 3: так же — у выбранной модели и пока не скачан
  $("#supertonic-card").hidden = !st.supertonic.offer || c.engine !== "supertonic" && st.supertonic.installed;
  $("#supertonic-dir").textContent = st.supertonic.model_dir || "";
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
          api("/api/model/download", { quant: q.quant }) }, T("Скачать"))));
  const known = st.quants.map(q => q.file);
  const other = st.models.filter(m => !known.includes(m)).map(m => h("li", { class: m === st.model ? "on" : "" },
    h("span", { class: "grow" }, m), use(m)));
  // Установщик ничего не качает: при первом запуске здесь и модель, и подсказка про CUDA (renderCuda)
  const empty = st.models.length ? [] : [h("li", { class: "muted" },
    T("Модель не установлена: послушайте примеры и скачайте вариант.") + (st.cuda.offer
      ? T(" Для видеокарты NVIDIA скачайте ещё CUDA — в блоке «Устройство синтеза».") : ""))];
  $("#models").replaceChildren(...empty, ...catalog, ...other);
  // Загрузка
  $("#model-cancel").hidden = !running;
  $("#dl").hidden = d.state === "idle";
  $("#dl-fill").style.width = (d.total ? Math.min(100, 100 * d.done / d.total) : 0) + "%";
  $("#dl-text").className = "small " + ({ error: "bad", done: "good" }[d.state] || "");
  $("#dl-text").textContent = {
    running: dlProgress(d),
    done: d.package === "cuda" ? T("Готово: CUDA установлена, сервер перезапускается.")
      : d.package === "espeech" ? T("ESpeech скачан")
      : d.package === "supertonic" ? T("Supertonic 3 скачан, готовые голоса поставлены.")
      : d.package ? ((pkg(d.package) || {}).restart ? T("Готово: пакет скачан, сервер перезапускается.")
        : T("Готово: пакет скачан."))
      : T("Готово: {quant} скачана, сервер перезапускается.", { quant: d.quant }),
    cancelled: T("Загрузка отменена на {done} из {total}: «Скачать» продолжит с этого места.",
      { done: fmtMB(d.done), total: fmtMB(d.total) }),
    error: T("Ошибка: {e}. «Скачать» продолжит с того же места.", { e: d.error }),
  }[d.state] || "";
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
  renderCuda(st, d);
  renderEspeech(st, d);
  renderSupertonic(st, d);
  // Наборы голосов
  // Набор — по записи на модель: коллекция «модель + источник» ставится и удаляется отдельно (voicesets.py)
  $("#sets").replaceChildren(...(st.sets.length ? st.sets.map(s => h("li", {},
    h("span", { class: "grow" }, h("b", {}, s.name), h("span", { class: "muted" }, " · " + s.title),
      h("span", { class: "muted small" }, T(" — голосов: {n}: {voices}", { n: s.voices.length,
        voices: s.voices.slice(0, 8).join(", ") + (s.voices.length > 8 ? "…" : "") }))),
    h("button", { class: "icon danger", title: T("Удалить набор"), onclick: () =>
      ask(T("Удалить набор «{name}» (голосов: {n})?", { name: `${s.name} · ${s.title}`, n: s.voices.length }),
        T("Удалить"), true)
        .then(yes => yes && api("/api/voicesets/delete", { name: s.name, model: s.model }))
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

// Модель синтеза (engines/): выбор и её настройки по схеме params из /status сервера
let engineKey = "";
function renderEngine() {
  const c = S.cosy, key = JSON.stringify([c.engines, c.engine, c.switching, c.params]);
  if (key === engineKey) return;
  engineKey = key;
  $("#engine-form").replaceChildren(...c.engines.map(e => h("label", { class: e.available ? "" : "na" },
    h("input", { type: "radio", name: "engine", value: e.id, checked: e.id === c.engine,
      disabled: !e.available || !!c.switching }),
    h("span", {}, e.title, e.available ? "" : h("span", { class: "muted small" }, " — " + e.why)))));
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

// CUDA (install.py, cuda_state): есть драйвер NVIDIA, а ggml-cuda.dll рядом с сервером нет — предложить скачать
let cudaKey = "";
function renderCuda(st, d) {
  const c = st.cuda, mine = d.package === "cuda", running = d.state === "running";
  const key = JSON.stringify([c, mine && d]);
  if (key === cudaKey) return;
  cudaKey = key;
  $("#cuda").hidden = !c.offer && !(mine && running);
  if ($("#cuda").hidden) return;
  const action = mine && running ? h("button", { onclick: () => api("/api/model/cancel") }, T("Отменить"))
    : h("button", { class: "primary", disabled: running, onclick: () => api("/api/cuda/download") }, T("Скачать"));
  const status = mine && ["running", "error", "cancelled"].includes(d.state) ? h("div", { class: "small " + ({ error: "bad" }[d.state] || "") },
      { running: dlProgress(d), error: T("Ошибка: {e}", { e: d.error }),
        cancelled: T("Загрузка отменена: «Скачать» продолжит с того же места.") }[d.state])
    : null;
  $("#cuda").replaceChildren(h("li", { class: "on" }, h("div", { class: "grow" },
    h("b", {}, T("Ускорение на видеокартах NVIDIA (CUDA)")), " ",
    h("span", { class: "muted small" }, T("{size}, на диске {disk}", { size: fmtMB(c.size), disk: fmtMB(c.unpacked) })),
    h("div", { class: "small muted" }, T("Видеокарта NVIDIA есть, а CUDA не установлена. С CUDA синтез на ней обычно " +
      "быстрее и требует меньше видеопамяти, чем через Vulkan. Файлы лягут в {dir}, сервер синтеза перезапустится.",
      { dir: c.dir })),
    status), action));
}

// ESpeech у программы для Windows (engines/espeech/download.py): рантайм с PyTorch и модель качаются по кнопке
let espeechKey = "";
function renderEspeech(st, d) {
  const e = st.espeech, mine = d.package === "espeech", running = d.state === "running";
  const key = JSON.stringify([e, mine && d, running]);
  if (key === espeechKey) return;
  espeechKey = key;
  $("#espeech-prepare").hidden = e.offer;
  if (!e.offer) { $("#espeech").replaceChildren(); return; }
  let action;
  if (mine && running) action = h("button", { onclick: () => api("/api/model/cancel") }, T("Отменить"));
  else if (e.installed) action = h("button", { class: "icon danger", title: T("Удалить ESpeech"), onclick: async () => {
      if (await ask(T("Удалить ESpeech ({size})? Голоса останутся, скачать его можно будет снова.", { size: fmtMB(e.disk) }), T("Удалить"), true)) api("/api/espeech/delete"); } }, "✕");
  else action = h("button", { class: "primary", disabled: running, onclick: () => api("/api/espeech/download") },
    e.runtime === "outdated" && e.model ? T("Обновить") : T("Скачать"));
  const status = mine && ["running", "error", "cancelled"].includes(d.state) ? h("div", { class: "small " + ({ error: "bad" }[d.state] || "") },
      { running: dlProgress(d), error: T("Ошибка: {e}. «Скачать» продолжит с того же места.", { e: d.error }),
        cancelled: T("Загрузка отменена: «Скачать» продолжит с того же места.") }[d.state])
    : mine && d.state === "done" && e.installed ? h("div", { class: "small good" }, T("ESpeech скачан"))
    : null;
  const size = e.installed ? T("на диске {disk}", { disk: fmtMB(e.disk) })
    : T("скачать {size}, на диске {disk}", { size: fmtMB(e.size), disk: fmtMB(e.disk) });
  const what = !e.installed && e.runtime === "outdated" && e.model ? h("div", { class: "small muted" },
    T("Вышло обновление ESpeech. Модель скачивать заново не придётся.")) : null;
  $("#espeech").replaceChildren(h("li", { class: e.installed ? "" : "on" }, h("div", { class: "grow" },
    h("b", {}, "ESpeech-TTS-1_SFT-256K"), " ", h("span", { class: "muted small" }, size),
    e.installed ? h("span", { class: "tag" }, T("установлен")) : null,
    h("div", { class: "small muted" }, T("Звучит естественнее CosyVoice, но озвучка начинается с задержкой.")),
    what,
    !e.gpu ? h("div", { class: "small warn" }, T("Нет видеокарты NVIDIA. На процессоре ESpeech слишком медленный для стрима.")) : null,
    status), action));
}

// Supertonic 3 (engines/supertonic/download.py): модель с Hugging Face и 10 готовых голосов. Веса — под OpenRAIL-M:
// условия видны в карточке и подтверждаются перед загрузкой
let supertonicKey = "";
function renderSupertonic(st, d) {
  const e = st.supertonic, mine = d.package === "supertonic", running = d.state === "running";
  const key = JSON.stringify([e, mine && d, running]);
  if (key === supertonicKey) return;
  supertonicKey = key;
  if (!e.offer) { $("#supertonic").replaceChildren(); return; }
  let action;
  if (mine && running) action = h("button", { onclick: () => api("/api/model/cancel") }, T("Отменить"));
  else if (e.installed) action = h("button", { class: "icon danger", title: T("Удалить Supertonic 3"), onclick: async () => {
      if (await ask(T("Удалить Supertonic 3 ({size}) вместе с его готовыми голосами? Скачать можно будет снова.",
          { size: fmtMB(e.disk) }), T("Удалить"), true)) api("/api/supertonic/delete"); } }, "✕");
  else if (e.model) action = h("button", { class: "primary", disabled: running, onclick: () => api("/api/supertonic/download") },
    T("Поставить голоса"));
  else action = h("button", { class: "primary", disabled: running, onclick: async () => {
      if (await ask(T("Веса Supertonic 3 распространяются по лицензии OpenRAIL-M с ограничениями использования " +
          "(ссылка — в карточке модели). Скачивая модель, вы принимаете эти условия."), T("Принимаю, скачать")))
        api("/api/supertonic/download"); } }, T("Скачать"));
  const status = mine && ["running", "error", "cancelled"].includes(d.state) ? h("div", { class: "small " + ({ error: "bad" }[d.state] || "") },
      { running: dlProgress(d), error: T("Ошибка: {e}. «Скачать» продолжит с того же места.", { e: d.error }),
        cancelled: T("Загрузка отменена: «Скачать» продолжит с того же места.") }[d.state])
    : mine && d.state === "done" && e.installed ? h("div", { class: "small good" }, T("Supertonic 3 скачан, готовые голоса поставлены."))
    : null;
  const size = e.installed || e.model ? T("на диске {disk}", { disk: fmtMB(e.disk) })
    : T("скачать {size}", { size: fmtMB(e.size) });
  const partial = e.model && !e.installed ? h("div", { class: "small warn" },
    T("Готовых голосов: {n} из {total}. «Поставить голоса» вернёт недостающие.", { n: e.voices, total: e.voices_total }))
    : null;
  $("#supertonic").replaceChildren(h("li", { class: e.installed ? "" : "on" }, h("div", { class: "grow" },
    h("b", {}, "Supertonic 3"), " ", h("span", { class: "muted small" }, size),
    e.installed ? h("span", { class: "tag" }, T("установлен")) : null,
    h("div", { class: "small muted" }, T("Лёгкая модель: работает только на процессоре, видеокарта остаётся игре. " +
      "Голоса — только 10 готовых ({names}), своих голосов из записи у неё нет.", { names: e.names.join(", ") })),
    h("div", { class: "small muted" }, h("a", { href: e.license, target: "_blank", rel: "noopener" },
      T("Лицензия весов: BigScience OpenRAIL-M")), " — ",
      T("нельзя выдавать себя за другого человека без его согласия, травить и порочить людей; озвучку нужно " +
        "помечать как созданную машиной, например в описании канала.")),
    partial, status), action));
}

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
