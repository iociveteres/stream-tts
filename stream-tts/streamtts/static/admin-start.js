"use strict";
/** Язык (выбор в шапке) и старт админки: последним из admin-*.js — состояние приходит асинхронно и
 *  отрисовывается функциями всех вкладок. */
// --- язык: страница перезагружается, тексты для зрителей по умолчанию сервер меняет сам (i18n.py) ---
$("#lang").value = LANG;
$("#lang").onchange = async e => {
  if ((await api("/api/settings", { lang: e.target.value })).ok) location.reload();
  else e.target.value = LANG;
};
$("#prof-sample").placeholder = T("проверить фразу");   // у поля translate="no": образец мата остаётся русским

// --- старт ---
// Первый запуск: модель не выбрана или выбранная не скачана — сразу на вкладку «Загрузки»
const firstTab = () => S.setup && !engineReady(S.cosy.picked) ? "downloads" : (S.twitch.mode === "chat" ? S.twitch.chat.state : S.twitch.state) === "ok" ? "queue" : "twitch";
reload().then(() => { showTab(location.hash.slice(1) || firstTab()); connect(); });
