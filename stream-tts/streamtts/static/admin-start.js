"use strict";
/** Язык (выбор в шапке) и старт админки: последним из всех скриптов страницы — состояние приходит асинхронно и
 *  отрисовывается функциями всех вкладок, редактора и обучения. Если /api/state ответит раньше, чем выполнится
 *  скрипт после этого, renderAll падает на его функции (trAvailable, trSync): нет вкладок «Сервер» и «Загрузки» и
 *  WebSocket (так было в 1.0.1 при первом открытии; tests/e2e/test_admin_scripts.py). */
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
