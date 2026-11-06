"use strict";
/** Вкладка «Загрузки» (install.py): модели синтеза ESpeech, CosyVoice3 и Supertonic 3 с примерами звучания, CUDA,
 *  пакеты для создания голосов; очередь загрузок, «Удалить» у скачанного, место на диске. Строки пакетов — и в
 *  редакторе голоса (#ed-packages). На ярлыке вкладки — процент идущей загрузки или точка: модель не выбрана или не
 *  скачана (warn), загрузка оборвалась (bad). Вариант CosyVoice3 выбирается на «Сервере» (admin-server.js). */
const DL_ENGINES = ["espeech", "cosyvoice", "supertonic"];   // порядок карточек
const ESPEECH_VRAM_GB = 0.9;   // пик видеопамяти ESpeech при синтезе: 0,89–0,94 ГБ (docs/espeech.md)
const fmt1 = x => Number(x).toLocaleString(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
/** Место на диске: до 1000 МБ — в МБ, дальше — в ГБ с одним знаком. */
const fmtSize = b => b / 1048576 >= 1000 ? T("{n} ГБ", { n: fmt1(b / 1073741824) })
  : T("{n} МБ", { n: fmtNum(b / 1048576, 0) });
const fmtDur = s => s < 60 ? T("{n} с", { n: Math.ceil(s) }) : T("{n} мин", { n: Math.round(s / 60) });

const dlRunning = () => S.setup && S.setup.download.state === "running" ? S.setup.download : null;
const dlPct = d => d.total ? Math.min(100, Math.floor(100 * d.done / d.total)) : 0;
const engineInfo = id => S.cosy.engines.find(e => e.id === id) || {};
const engineReady = id => !!id && !!engineInfo(id).ready;
const quantOf = file => (file.match(/[-_](Q\w+|F16|BF16)\.gguf$/i) || [, file])[1];
const tag = (cls, text) => h("span", { class: "tag " + cls }, text);

/** Всё, что можно скачать здесь: id → имя, место на диске, скачано ли. */
function dlItems() {
  const st = S.setup, out = {};
  if (st.espeech.offer) out.espeech = { name: "ESpeech-TTS-1_SFT-256K", disk: st.espeech.disk,
    have: st.espeech.installed || st.espeech.model && st.espeech.runtime === "outdated" };
  if (engineInfo("cosyvoice").available) for (const q of st.quants)
    out[q.quant] = { name: "CosyVoice3 " + q.quant, disk: q.size, have: st.models.includes(q.file) };
  if (st.supertonic.offer) out.supertonic = { name: "Supertonic 3", disk: st.supertonic.disk, have: st.supertonic.model };
  if (st.cuda.dir) out.cuda = { name: "CUDA " + st.cuda.pinned.split(".")[0], disk: st.cuda.unpacked, have: st.cuda.installed };
  for (const p of S.packages) out[p.id] = { name: p.title, disk: p.size, have: p.installed };
  return out;
}
const dlName = id => (dlItems()[id] || {}).name || id;

// Примеры звучания вариантов (static/samples/<id>.mp3, фраза SAMPLE_PHRASE, seed 42): один плеер на всю админку
const sample = new Audio();
let sampleKey = "";
sample.onended = sample.onerror = () => { sampleKey = ""; renderDownloads(); };
function playSample(key) {
  if (sampleKey === key) { sample.pause(); sampleKey = ""; }
  else { sample.src = `/static/samples/${key}.mp3`; sample.play().catch(() => {}); sampleKey = key; }
  renderDownloads();
}
const sampleBtn = key => !S.setup.samples.includes(key) ? null : h("div", { class: "samples" }, h("button", { type: "button", class: sampleKey === key ? "on" : "",
  title: `«${S.setup.sample_phrase}»`, onclick: () => playSample(key) }, (sampleKey === key ? "■ " : "▶ ") + T("Пример")));

async function dlStart(id) {
  // веса Supertonic 3 — под OpenRAIL-M: условия подтверждаются перед загрузкой (ссылка — в карточке)
  if (id === "supertonic" && !S.setup.supertonic.model && !await ask(T("Веса Supertonic 3 распространяются по " +
      "лицензии OpenRAIL-M с ограничениями использования (ссылка — в карточке модели). Скачивая модель, вы " +
      "принимаете эти условия."), T("Принимаю, скачать")))
    return;
  api("/api/download", { id });
}

// --- строка варианта: имя и метки, размер и память, пример, прогресс; кнопки справа ---
function dlTags(id, have) {
  const q = S.setup ? S.setup.queue.indexOf(id) : -1;
  return [have ? tag("good", T("скачано")) : null, q >= 0 ? tag("", T("в очереди: {n}", { n: q + 1 })) : null];
}
function dlStatus(id) {
  const st = S.setup, d = st && st.download;
  if (!d) return null;
  if (d.state === "running" && d.id === id) return h("div", {},
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${dlPct(d)}%` })),
    h("div", { class: "small muted" }, T("{done} из {total}", { done: fmtSize(d.done), total: fmtSize(d.total) }) +
      (d.speed > 0 ? " · " + T("{n} МБ/с", { n: fmtNum(d.speed / 1048576) }) : d.file ? " · " + d.file : "")));
  const f = st.failed[id];
  return f && f.state === "error" ? h("div", { class: "small bad" }, T("Ошибка: {e}", { e: f.error })) : null;
}
/** Кнопки строки: «Отменить» у идущей, «Убрать» у стоящей в очереди, ✕ у скачанного (delText — подтверждение,
 *  пусто — удалять нельзя), иначе «Скачать» («Продолжить» после ошибки или отмены). */
function dlActs(id, have, delText, extra) {
  const st = S.setup, d = dlRunning();
  if (!st) return [];   // готовый сервер в тестах: качать некуда
  if (d && d.id === id) return h("button", { type: "button", onclick: () => api("/api/download/cancel") }, T("Отменить"));
  if (st.queue.includes(id)) return h("button", { type: "button", title: T("Убрать из очереди"),
    onclick: () => api("/api/download/unqueue", { id }) }, T("Убрать"));
  if (have) {
    const title = T("Удалить {name}", { name: dlName(id) });
    return [extra, delText ? h("button", { type: "button", class: "icon danger", title, "aria-label": title,
      onclick: async () => { if (await ask(delText, T("Удалить"), true)) api("/api/download/delete", { id }); } }, "✕") : null];
  }
  return h("button", { type: "button", class: "primary", onclick: () => dlStart(id) },
    st.failed[id] ? T("Продолжить") : T("Скачать"));
}
function dlRow({ id, name, meta, tags = [], extra = [], acts, cls = "" }) {
  return h("li", { class: cls }, h("div", { class: "grow" }, h("div", {}, h("b", {}, name), tags),
    meta ? h("div", { class: "meta" }, meta) : null, extra, dlStatus(id)), h("div", { class: "acts" }, acts));
}

// --- карточки моделей: заголовок, описание модели, варианты ---
const DL_DESC = {
  espeech: T("Естественное звучание, расстановка ударений, возможность добавлять новые голоса."),
  cosyvoice: T("Легко позволяет создавать новые голоса. Более лёгкие варианты могут давать немного больше артефактов."),
  supertonic: T("Лёгкая модель для синтеза на процессоре в реальном времени, имеет 10 готовых голосов."),
};
function dlVariants(key) {
  const st = S.setup;
  if (key === "espeech") {
    const e = st.espeech, outdated = e.runtime === "outdated" && e.model, have = e.installed || outdated;
    return [dlRow({ id: "espeech", name: "ESpeech-TTS-1_SFT-256K",
      meta: `${fmtSize(e.disk)} · ${T("видеопамять ≈ {n} ГБ", { n: fmt1(ESPEECH_VRAM_GB) })}`,
      tags: [...dlTags("espeech", have), outdated ? tag("warn", T("есть обновление")) : null],
      extra: [sampleBtn("espeech"),
        e.gpu ? null : h("div", { class: "small warn" }, T("Без видеокарты NVIDIA слишком медленный для стрима."))],
      acts: dlActs("espeech", have, T("Удалить ESpeech ({size})? Голоса останутся.", { size: fmtSize(e.disk) }),
        outdated ? h("button", { type: "button", class: "primary", onclick: () => dlStart("espeech") }, T("Обновить"))
          : null) })];
  }
  if (key === "cosyvoice") return st.quants.map(q => {
    const have = st.models.includes(q.file), used = have && q.file === st.model;
    // выбранный вариант можно удалить: CosyVoice3 перейдёт на оставшийся (первый по имени, как install.py) или
    // будет ждать модель
    const rest = st.models.filter(m => m !== q.file);
    const delText = T("Удалить вариант {name} ({size})?", { name: q.quant, size: fmtSize(q.size) }) + (!used ? ""
      : " " + (rest.length ? T("Он сейчас выбран: CosyVoice3 перейдёт на {other}.", { other: quantOf(rest[0]) })
        : T("Он сейчас выбран: CosyVoice3 будет ждать, пока не скачаете другой вариант.")));
    return dlRow({ id: q.quant, name: q.quant, cls: used ? "on" : "",
      // память — среднее между обычным пиком и пиком на самом длинном образце (замеры — engines/cosyvoice)
      meta: `${fmtSize(q.size)} · ${T("видеопамять ≈ {n} ГБ", { n: fmt1((q.vram + q.vram_max) / 2 / 1024) })}`,
      tags: [q.quant === st.default_quant ? tag("", T("рекомендуется")) : null, ...dlTags(q.quant, have),
        used ? tag("accent", T("выбран")) : null],
      extra: [sampleBtn("cosyvoice-" + q.quant)], acts: dlActs(q.quant, have, delText) });
  });
  const s = st.supertonic, partial = s.model && !s.installed;
  return [dlRow({ id: "supertonic", name: "Supertonic 3", meta: `${fmtSize(s.disk)} · ${T("только процессор")}`,
    tags: [...dlTags("supertonic", s.installed),
      partial ? tag("warn", T("голосов: {n} из {total}", { n: s.voices, total: s.voices_total })) : null],
    extra: [sampleBtn("supertonic")],
    acts: dlActs("supertonic", s.installed, T("Удалить Supertonic 3 ({size}) вместе с его готовыми голосами?",
      { size: fmtSize(s.disk) })) })];
}
function dlModelCard(key) {
  const st = S.setup, picked = S.cosy.picked === key, ready = engineReady(key);
  const head = h("div", { class: "card-head" }, h("h2", {}, engineInfo(key).title),
    picked ? tag("accent", T("выбрана")) : null,
    picked && !ready ? tag("warn", T("сервер ждёт модель")) : null,
    ready && !picked ? h("span", { class: "side" }, h("a", { href: "#server" }, T("выбрать на «Сервере»"))) : null);
  const license = key === "supertonic" ? [" " + T("Лицензия:") + " ",
    h("a", { href: st.supertonic.license, target: "_blank", rel: "noopener" }, "BigScience OpenRAIL-M"), "."] : [];
  return h("div", { class: "card" + (picked ? " picked" : "") }, head,
    h("p", { class: "desc" }, DL_DESC[key], license), h("ul", { class: "setlist dlist" }, dlVariants(key)));
}
/** Модели, которые здесь качаются: найден их сервер синтеза (у ESpeech и Supertonic — и загрузка по кнопке). */
const dlEngines = () => DL_ENGINES.filter(k => engineInfo(k).available &&
  (k === "cosyvoice" || S.setup[k].offer));

// --- правая колонка: место на диске, CUDA, пакеты ---
function renderDisk(items) {
  const st = S.setup, f = st.disk, have = Object.values(items).filter(i => i.have);
  const total = have.reduce((a, i) => a + i.disk, 0);
  $("#dl-sum").replaceChildren(have.length ? h("dl", { class: "sum" },
    have.flatMap(i => [h("dt", {}, i.name), h("dd", {}, fmtSize(i.disk))]),
    h("dt", { class: "total" }, T("Всего")), h("dd", { class: "total" }, fmtSize(total)))
    : h("p", { class: "muted small" }, T("Пока ничего не скачано.")));
  $("#dl-free").textContent = f.free == null ? ""
    : f.docker ? T("Свободно в томе {dir}: {size}", { dir: f.where, size: fmtSize(f.free) })
    : f.where ? T("Свободно на диске {drive} {size}", { drive: f.where, size: fmtSize(f.free) })
    : T("Свободно: {size}", { size: fmtSize(f.free) });
  // в Docker — тома контейнера и папки рядом с docker-compose.yml, к которым они привязаны
  const dir = p => h("code", {}, f.docker ? `${p}  (./${p.split("/")[1]})` : p);
  $("#dl-dirs-title").textContent = f.docker ? T("Папки (тома контейнера)") : T("Папки");
  $("#dl-dirs").replaceChildren(...[h("div", {}, T("Модели и пакеты:") + " ", dir(st.models_dir)),
    st.cuda.dir ? h("div", {}, "CUDA: ", dir(st.cuda.dir)) : null,
    st.espeech.offer ? h("div", {}, T("Рантайм ESpeech:") + " ", dir(st.espeech.runtime_dir)) : null].filter(Boolean));
}
function renderCudaRow(items) {
  const c = S.setup.cuda;
  $("#dl-cuda-card").hidden = !c.dir;   // нет cosyvoice-server: CUDA некуда класть
  if (!c.dir) return;
  const name = items.cuda.name, size = fmtSize(c.unpacked), d = dlRunning();
  let li;
  if (!c.gpu && !c.installed) li = h("li", { class: "na" }, h("div", { class: "grow" },
    h("div", {}, h("b", {}, name), tag("", T("недоступна"))), h("div", { class: "meta" }, size),
    h("div", { class: "small muted" }, c.docker ? T("Контейнер не видит видеокарту NVIDIA: запустите его с " +
      "docker-compose.gpu.yml.") : T("Видеокарты NVIDIA нет."))));
  else li = dlRow({ id: "cuda", name, cls: c.installed || d && d.id === "cuda" ? "" : "on",
    // версия — у скачанной кнопкой (cuda-version.txt), у CUDA из сборки сервера её нет
    meta: c.version ? `${size} · ${c.version}` : size, tags: dlTags("cuda", c.installed),
    extra: c.installed ? [] : [h("div", { class: "small muted" },
      T("Найдена видеокарта NVIDIA. Скачайте CUDA для работы моделей с её использованием."))],
    // CUDA из сборки сервера (образ с GPU=cuda, сборка из исходников) не удаляется: удаляется только скачанная
    acts: dlActs("cuda", c.installed, !c.deletable ? ""
      : c.docker ? T("Удалить CUDA ({size})? CosyVoice3 и ESpeech перейдут на процессор.", { size })
      : T("Удалить CUDA ({size})? CosyVoice3 перейдёт на Vulkan, ESpeech — на процессор.", { size })) });
  $("#dl-cuda").replaceChildren(li);
}
/** Пакет (downloads.PACKAGES): блок «Создание голосов» и нужные пакеты в редакторе голоса. */
function pkgRow(p) {
  const tags = S.setup ? dlTags(p.id, p.installed)
    : [p.installed ? tag("good", T("установлен")) : tag("", T("не установлен"))];
  return dlRow({ id: p.id, name: p.title, meta: fmtSize(p.size), tags,
    extra: [h("div", { class: "small muted" }, p.about), p.usable ? null : h("div", { class: "small warn" },
      T("В этой версии программы нет onnxruntime: пакет работает в программе для Windows, в Docker и при запуске " +
        "из исходников с пакетами из requirements.txt."))],
    acts: !p.usable ? tag("", T("недоступен")) : dlActs(p.id, p.installed, p.restart
      ? T("Удалить «{name}»? Сервер синтеза перезапустится.", { name: p.title }) : T("Удалить «{name}»?", { name: p.title })) });
}

// --- полоса сверху: идущая загрузка и очередь или «скачайте модель» до первой загрузки ---
function renderStrip(items) {
  const st = S.setup, d = dlRunning(), el = $("#dl-strip");
  el.hidden = !d && !!S.cosy.picked;
  if (d) {
    const left = d.speed > 0 && d.total > d.done
      ? " · " + T("осталось {time}", { time: fmtDur((d.total - d.done) / d.speed) }) : "";
    el.className = "strip";
    el.replaceChildren(h("div", { class: "grow" },
      h("div", { class: "small" }, h("b", {}, `${dlName(d.id)}: ${dlPct(d)}%`), left),
      h("div", { class: "track" }, h("div", { class: "fill", style: `width:${dlPct(d)}%` })),
      st.queue.length ? h("ol", { class: "queue", "aria-label": T("Очередь загрузок") }, st.queue.map((id, i) => {
        const name = dlName(id);
        return h("li", {}, h("span", { class: "n" }, `${i + 1}.`), h("span", { class: "grow" }, name),
          h("span", { class: "muted" }, items[id] ? fmtSize(items[id].disk) : ""),
          h("button", { type: "button", title: T("Убрать из очереди"), "aria-label": T("Убрать {name} из очереди", { name }),
            onclick: () => api("/api/download/unqueue", { id }) }, "✕"));
      })) : null),
      h("button", { type: "button", onclick: () => api("/api/download/cancel") }, T("Отменить")));
  } else if (!S.cosy.picked) {
    el.className = "strip first";
    el.replaceChildren(h("div", { class: "grow small" }, h("b", {}, T("Скачайте одну из моделей синтеза голоса."))));
  }
}
function renderBadge() {
  const d = dlRunning(), st = S.setup;
  const err = Object.values(st.failed).some(f => f.state === "error"), missing = !engineReady(S.cosy.picked);
  $("#dl-badge").replaceChildren(...(d ? [h("span", { class: "pct" }, `${dlPct(d)}%`)]
    : err || missing ? [h("span", { class: "dot " + (err ? "bad" : "warn"),
      title: err ? T("Загрузка прервалась") : T("Модель не скачана") })] : []));
}

let downloadsKey = "";
function renderDownloads() {
  const st = S.setup;
  $('#tabs [data-tab="downloads"]').hidden = !st;
  const key = JSON.stringify([st, S.packages, S.cosy.picked, S.cosy.engines, sampleKey, edPackages()]);
  if (key === downloadsKey) return;
  downloadsKey = key;
  const ed = S.packages.filter(p => edPackages().includes(p.id));
  $("#ed-packages").replaceChildren(...ed.map(pkgRow));
  if (D && !$("#editor").hidden) renderEdVariants();   // галочка «убрать музыку» зависит от пакета
  if (!st) return;
  const items = dlItems();
  renderBadge();
  renderStrip(items);
  $("#dl-models").replaceChildren(...dlEngines().map(dlModelCard));
  renderDisk(items);
  renderCudaRow(items);
  $("#packages").replaceChildren(...S.packages.map(pkgRow));
}
