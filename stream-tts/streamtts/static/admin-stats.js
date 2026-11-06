"use strict";
/** Вкладка «Статистика»: сейчас, загрузка, модели в памяти, история по часам. */
// --- статистика ---
function tile(k, v, s) { return h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
  s ? h("div", { class: "s" }, s) : null); }
function renderLiveStats() {
  if (!$("#tab-stats").classList.contains("on")) return;
  const q = S.engine.queue, playing = q.find(i => i.state === "playing"), g = S.load.gpu, c = S.load.cpu;
  const gb = mb => fmtNum(mb / 1024);
  $("#live").replaceChildren(
    tile(T("В очереди"), q.length, q.length ? T("синтезируется: {n}", { n: q.filter(i => i.state === "synth").length }) : ""),
    tile(T("Играет"), playing ? playing.display : "—", playing ? playing.voice : ""),
    tile("cosyvoice", S.cosy.ok ? T("доступен") : T("нет"), S.cosy.ok ? (S.cosy.busy || T("свободен")) : S.cosy.error),
    c ? tile(T("Процессор"), c.util == null ? "—" : `${c.util}%`, T("ОЗУ {used} / {total} ГБ",
      { used: gb(c.mem_used), total: gb(c.mem_total) }))
      : tile(T("Процессор"), T("нет данных"), ""),
    g ? tile(T("Видеокарта"), `${g.util}%`, T("{used} / {total} ГБ · {temp} °C",
      { used: gb(g.mem_used), total: gb(g.mem_total), temp: g.temp }))
      : tile(T("Видеокарта"), T("нет данных"), T("nvidia-smi недоступен: нет видеокарты NVIDIA или она не проброшена в контейнер")),
    tile(T("Скорость синтеза"), fmtRtf(S.engine.rtf), T("последние фразы")));
  drawLoad();
  renderModels();
}

// История загрузки: целиком приходит с полным состоянием, дальше — по точке в каждом сообщении
const LOAD_WINDOW = 600;   // с, как stats.HISTORY
let loadHist = [];
function trackLoad(l) {
  if (!l) return;
  if (l.history) loadHist = l.history.slice();
  else if (l.sample && (!loadHist.length || l.sample[0] > loadHist.at(-1)[0])) loadHist.push(l.sample);
  const from = Date.now() / 1000 - LOAD_WINDOW;
  while (loadHist.length && loadHist[0][0] < from) loadHist.shift();
}

function drawLoad() {
  const box = $("#load-chart"), W = Math.max(320, box.clientWidth || 800), H = 180;
  const pad = { l: 40, r: 8, t: 8, b: 22 };
  const now = Date.now() / 1000, start = now - LOAD_WINDOW;
  const x = t => pad.l + (W - pad.l - pad.r) * (t - start) / LOAD_WINDOW;
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / 100);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": T("Загрузка процессора и видеокарты") });
  for (let v = 0; v <= 100; v += 25) {
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "grid" }));
    const tx = svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" }); tx.textContent = v + "%"; s.append(tx);
  }
  for (let m = 10; m >= 0; m -= 2) {
    const tx = svg("text", { x: x(now - m * 60), y: H - 6, "text-anchor": m === 10 ? "start" : m ? "middle" : "end" });
    tx.textContent = m ? T("−{n} мин", { n: m }) : T("сейчас"); s.append(tx);
  }
  // линия рвётся там, где замеров не было (админка была закрыта) или значения нет
  const line = (k, color) => {
    let d = "", prev = null;
    for (const p of loadHist) {
      if (p[k] == null) { prev = null; continue; }
      d += (prev && p[0] - prev[0] < 10 ? "L" : "M") + `${x(p[0]).toFixed(1)},${y(p[k]).toFixed(1)} `;
      prev = p;
    }
    if (d) s.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round",
      "stroke-linecap": "round" }));
  };
  line(1, "var(--series-1)");
  line(2, "var(--series-2)");
  const cross = svg("line", { y1: pad.t, y2: H - pad.b, class: "cross", visibility: "hidden" });
  s.append(cross);
  const hit = svg("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b, fill: "transparent" });
  const tip = $("#tip");
  hit.addEventListener("mousemove", e => {
    if (!loadHist.length) return;
    const r = s.getBoundingClientRect(), t = start + ((e.clientX - r.left) * W / r.width - pad.l) / (W - pad.l - pad.r) * LOAD_WINDOW;
    const p = loadHist.reduce((a, b) => Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a);
    cross.setAttribute("x1", x(p[0])); cross.setAttribute("x2", x(p[0])); cross.setAttribute("visibility", "visible");
    const pct = v => v == null ? "—" : v + "%";
    tip.hidden = false;
    tip.replaceChildren(h("div", {}, fmtClock(p[0])), h("div", {}, T("процессор: {v}", { v: pct(p[1]) })),
      h("div", {}, T("видеокарта: {v}", { v: pct(p[2]) })), h("div", {}, T("видеопамять: {v}", { v: pct(p[3]) })));
    tip.style.left = Math.min(innerWidth - 170, e.clientX + 12) + "px"; tip.style.top = (e.clientY + 12) + "px";
  });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  s.append(hit);
  box.replaceChildren(s);
}

// график перерисовывается раз в 2 с: mouseleave старого svg уже не придёт, поэтому подсказка прячется по контейнеру
$("#load-chart").addEventListener("mouseleave", () => { $("#tip").hidden = true; });

let modelsKey = "";
function renderModels() {
  const key = JSON.stringify(S.models);   // без изменений не перерисовывать: клик по кнопке терялся бы
  if (key === modelsKey) return;
  modelsKey = key;
  const rows = S.models.map(m => h("tr", {},
    h("td", {}, h("div", {}, m.what), h("div", { class: "muted small" }, m.name)),
    h("td", { class: "small" }, m.where),
    h("td", {}, pill(m.loaded ? T("загружена") : T("не загружена"), m.loaded ? "good" : "")),
    h("td", { class: "num" }, m.unload ? h("button", { class: "small", onclick: () => unloadModel(m) }, T("Выгрузить"))
      : m.load ? h("button", { class: "small primary", onclick: () => api("/api/models/load", { id: m.id }) }, T("Загрузить"))
      : null)));
  $("#mem-models").replaceChildren(h("table", {}, h("tbody", {}, ...rows)));
}
async function unloadModel(m) {
  if (m.id === "tts" && !await ask(T("Выгрузить модель синтеза? Озвучка остановится, награды будут возвращаться, " +
    "пока модель снова не загружена."), T("Выгрузить"), true)) return;
  if ((await api("/api/models/unload", { id: m.id })).ok) toast(T("Модель выгружена"));
}
async function loadStats() {
  renderLiveStats();
  const hours = +$("#period").value;
  const r = await fetch(`/api/stats?hours=${hours}&test=${$("#with-test").checked ? 1 : 0}`);
  if (!r.ok) return;
  const st = await r.json();
  const t = st.total;
  $("#kpi").replaceChildren(
    tile(T("Активаций"), t.n || 0),
    tile(T("Сыграно"), t.played || 0),
    tile(T("Возвратов"), t.refunded || 0, t.n ? `${fmtNum(100 * (t.refunded || 0) / t.n, 0)}%` : ""),
    tile(T("Ожидание до звука"), st.wait_ms == null ? "—" : T("{n} с", { n: fmtNum(st.wait_ms / 1000) }), T("в среднем")),
    tile(T("Скорость синтеза"), fmtRtf(st.synth_ratio),
      st.avg_audio_ms ? T("средняя фраза {n} с", { n: fmtNum(st.avg_audio_ms / 1000) }) : ""));
  drawHours(st, hours);
  bars("#top-voices", st.voices.map(x => [x.voice, x.n]));
  bars("#top-viewers", st.viewers.map(x => [x.login, x.n]));
  const SOURCE_NAMES = { reward: T("награда"), command: T("команда"), chat: T("чат"), test: T("тест") };
  bars("#top-sources", st.sources.map(x => [SOURCE_NAMES[x.source] || x.source, x.n]));
  bars("#reasons", st.reasons.map(x => [T(x.reason), x.n]));   // причины в базе по-русски (rules.R_*)
}
$("#period").onchange = loadStats;
$("#with-test").onchange = loadStats;
setInterval(() => { if ($("#tab-stats").classList.contains("on")) loadStats(); }, 60000);

function bars(sel, rows) {
  const max = Math.max(1, ...rows.map(r => r[1]));
  $(sel).replaceChildren(...(rows.length ? rows.map(([name, n]) => h("div", { class: "b" },
    h("span", { class: "t", title: name }, name), h("span", { class: "n" }, n),
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${100 * n / max}%` }))))
    : [h("span", { class: "muted" }, T("нет данных"))]));
}

function drawHours(st, hours) {
  const box = $("#hours"), W = Math.max(320, box.clientWidth || 800), H = 220;
  const pad = { l: 34, r: 8, t: 8, b: 22 };
  const now = Math.floor(Date.now() / 3600000) * 3600;
  const start = now - (hours - 1) * 3600;
  const map = new Map(st.by_hour.map(r => [r.hour, r]));
  const cols = [];
  for (let t = start; t <= now; t += 3600) { const r = map.get(t) || {}; cols.push({ t, p: r.played || 0, f: r.refunded || 0 }); }
  const max = Math.max(1, ...cols.map(c => c.p + c.f));
  const step = niceStep(max), top = Math.ceil(max / step) * step;
  const cw = (W - pad.l - pad.r) / cols.length, gap = cw >= 6 ? 2 : 0, bw = Math.max(1, cw - gap);
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": T("Активации по часам") });
  for (let v = 0; v <= top; v += step) {
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "grid" }));
    const tx = svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" }); tx.textContent = v; s.append(tx);
  }
  const tip = $("#tip");
  cols.forEach((c, i) => {
    const x = pad.l + i * cw + gap / 2;
    const d = new Date(c.t * 1000);
    const label = hours <= 24 ? d.getHours() % 3 === 0 : d.getHours() === 0 && (hours <= 168 || d.getDate() % 5 === 1);
    if (label) {
      const tx = svg("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle" });
      tx.textContent = hours <= 24 ? `${String(d.getHours()).padStart(2, "0")}:00`
        : d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" });
      s.append(tx);
    }
    // сыграно снизу, возврат сверху; между сегментами зазор 2px, верх последнего сегмента скруглён
    const segs = [[c.p, "var(--series-1)"], [c.f, "var(--series-2)"]].filter(z => z[0] > 0);
    let base = 0;
    segs.forEach(([v, color], k) => {
      const y0 = y(base), y1 = y(base + v) + (k > 0 && gap ? 2 : 0);
      const hgt = Math.max(1, y0 - y1), last = k === segs.length - 1, rr = last ? Math.min(4, bw / 2, hgt) : 0;
      s.append(svg("path", { fill: color, d: `M${x},${y0} V${y1 + rr} Q${x},${y1} ${x + rr},${y1} H${x + bw - rr} ` +
        `Q${x + bw},${y1} ${x + bw},${y1 + rr} V${y0} Z` }));
      base += v;
    });
    const hit = svg("rect", { x: pad.l + i * cw, y: pad.t, width: cw, height: H - pad.t - pad.b, fill: "transparent" });
    hit.addEventListener("mousemove", e => {
      tip.hidden = false;
      tip.replaceChildren(h("div", {}, d.toLocaleString(LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
        h("div", {}, T("сыграно: {n}", { n: c.p })), h("div", {}, T("возврат: {n}", { n: c.f })));
      tip.style.left = Math.min(innerWidth - 160, e.clientX + 12) + "px"; tip.style.top = (e.clientY + 12) + "px";
    });
    hit.addEventListener("mouseleave", () => tip.hidden = true);
    s.append(hit);
  });
  box.replaceChildren(s);
  const active = cols.filter(c => c.p + c.f);
  $("#hours-table").replaceChildren(active.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, T("Час")),
    h("th", { class: "num" }, T("Сыграно")), h("th", { class: "num" }, T("Возврат")))),
    h("tbody", {}, ...active.map(c => h("tr", {}, h("td", {}, new Date(c.t * 1000).toLocaleString(LOCALE,
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })),
      h("td", { class: "num" }, c.p), h("td", { class: "num" }, c.f))))) : h("p", { class: "muted" }, T("Активаций не было.")));
}
function niceStep(max) {
  const raw = max / 4, p = 10 ** Math.floor(Math.log10(raw)), n = raw / p;
  return Math.max(1, (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p);
}
