"use strict";

const pageHelp = {
  overview: ["總覽", [
    "查看執行中、排隊中和最近完成的 Job。按「查看 Log」可追蹤輸出。",
    "在「同時執行上限」調整可同時執行的 Job 數量。填 0 代表不限。",
    "要中斷工作時，使用 Job 卡片上的停止或取消排隊。",
  ]],
  workspaces: ["Workspace", [
    "輸入名稱與資料夾路徑，或按資料夾圖示瀏覽本機磁碟。",
    "要匯入 Git worktree，先選 Repo，再勾選要登錄的資料夾。",
    "勾選 Workspace 後可前往 Build。重新整理 Git 可查看分支和變更。",
  ]],
  build: ["Build", [
    "選取一個或多個 Workspace，再選 Recipe。",
    "有參數表時可搜尋旗標、套用 preset，或直接調整參數。右側會即時顯示指令。",
    "開始前可先檢查衝突。同資料夾已有工作時，需明確勾選允許後才能執行。",
  ]],
  log: ["Log", [
    "選取 Job 後會自動讀取新增輸出。關閉「追蹤最新輸出」可停在目前位置。要存檔時按「下載 .log」。",
    "搜尋關鍵字或選擇 error、fail、warning。使用上一筆／下一筆，或點判讀摘要跳到該行。",
    "Job 結束後可複製 AI 提示詞，或送到已設定的 OpenCode／自訂端點。可选择提示詞帶入的 log 範圍（錯誤附近、尾端、完整或自訂起訖行），「預覽提示詞」可先檢查內容。",
  ]],
  history: ["歷史", [
    "可依 Workspace、結果及完成日期篩選紀錄。",
    "匯出 CSV 或 JSON 時，有勾選就匯出勾選項目，否則匯出目前篩選結果。",
    "刪除前會要求確認，會連同該 Job 的 log 一併移除（執行中的 Job 不能刪）。已儲存的分析回答也會一併刪除。",
  ]],
  recipes: ["流程設定", [
    "可查看內建的 Clean + Build 與 Clean + Build (-nou)，或複製成自己的 Recipe，再編排步驟及連結參數表與產出物規則。",
    "在參數表匯入檔案並編輯互斥規則。Preset 可從 Build 頁另存。",
    "在產出物設定選取、排除、命名和 ZIP。手動執行前先預覽並確認。",
  ]],
  settings: ["設定", [
    "歷史保留可依天數或筆數清理，填 0 停用該條件。",
    "可開啟瀏覽器通知、頁籤標題閃爍或提示音。桌面通知需要瀏覽器授權。",
    "在 AI 分析設定選擇預設方式和提示詞範本。工作排程支援每日或每週，可啟用、停用或刪除。",
  ]],
};

let operationSettings = { notifications: { title: true }, ai: { method: "copy" } };
const checkedHistory = new Set();
let currentHistory = [];
let appliedHistoryFilters = {};
const analysesByJob = new Map();
const analysisPolls = new Set();
let analysisFocus = false;
let notificationBaseline;
const notifiedStates = new Map();
let titleTimer;
let audio;

function initializeOperations() {
  for (const [id, [title, steps]] of Object.entries(pageHelp)) {
    const head = $(id).querySelector(".page-head");
    const actions = element("div", "row page-head-actions");
    actions.append(...[...head.children].slice(1));
    const trigger = button("功能說明", () => {
      $("page-help-title").textContent = title + "｜功能說明";
      const items = element("ol");
      items.setAttribute("role", "list");
      for (const [index, step] of steps.entries()) {
        const item = element("li");
        const number = element("span", "page-help-number", (index + 1) + ".");
        number.setAttribute("aria-hidden", "true");
        item.append(number, element("span", "page-help-step", step));
        items.append(item);
      }
      $("page-help-content").replaceChildren(items);
      $("page-help").showModal();
    });
    trigger.prepend(icon("info"));
    actions.append(trigger);
    head.append(actions);
    TbIcons.render(actions);
  }
  refreshOperationSettings().then(() => {
    if (!$("log-job").value) $("ai-method").value = operationSettings.ai.method;
    updateAnalysisActions();
  }).catch((error) => announce(error.message, true));
  document.addEventListener("pointerdown", prepareAudio, { once: true });
  window.addEventListener("focus", stopTitleFlash);
}

function currentFilters() {
  return Object.fromEntries([...new FormData($("history-filter"))].filter(([, value]) => value));
}

async function loadHistoryRows() {
  const filters = currentFilters();
  const query = new URLSearchParams(filters);
  const { result } = await api("/history?" + query);
  appliedHistoryFilters = filters;
  if (!Object.keys(filters).length) {
    const options = [...new Map(result.map((row) => [row.workspaceId || row.workspace, row.workspace]))]
      .map(([id, name]) => ({ id, name }));
    selectRecords("history-workspace", options, "全部");
  }
  currentHistory = result;
  const visible = new Set(result.map((row) => row.id));
  for (const id of checkedHistory) if (!visible.has(id)) checkedHistory.delete(id);
  updateHistorySelection();
  return result;
}

function historyCheckbox(row) {
  const box = element("input");
  box.type = "checkbox";
  box.className = "history-check";
  box.value = row.id;
  box.checked = checkedHistory.has(row.id);
  box.setAttribute("aria-label", "選取 " + row.workspace + " " + timeLabel(row.endedAt));
  box.addEventListener("change", () => {
    if (box.checked) checkedHistory.add(row.id);
    else checkedHistory.delete(row.id);
    updateHistorySelection();
  });
  return box;
}

function updateHistorySelection() {
  $("history-selected").textContent = "已選 " + checkedHistory.size + " 筆";
  $("history-delete").disabled = !checkedHistory.size;
  $("history-all").disabled = !currentHistory.length;
  $("history-all").checked = !!currentHistory.length && checkedHistory.size === currentHistory.length;
  $("history-all").indeterminate = checkedHistory.size > 0 && checkedHistory.size < currentHistory.length;
}

listen($("history-filter"), "submit", loadHistory);
$("history-filter").addEventListener("reset", () => {
  setTimeout(() => loadHistory().catch((error) => announce(error.message, true)), 0);
});
$("history-all").addEventListener("change", () => {
  checkedHistory.clear();
  if ($("history-all").checked) for (const row of currentHistory) checkedHistory.add(row.id);
  document.querySelectorAll(".history-check").forEach((box) => { box.checked = checkedHistory.has(box.value); });
  updateHistorySelection();
});

for (const format of ["csv", "json"]) listen($("history-" + format), "click", async () => {
  const filter = { ...appliedHistoryFilters, format };
  if (checkedHistory.size) filter.ids = [...checkedHistory];
  const { result } = await api("/history/export", "POST", filter);
  const object = new Blob([result.content], { type: format === "csv" ? "text/csv;charset=utf-8" : "application/json" });
  const url = URL.createObjectURL(object);
  const link = element("a");
  link.href = url;
  link.download = "build-runner-history." + format;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

listen($("history-delete"), "click", async () => {
  if (!checkedHistory.size) return;
  if (!window.confirm("刪除 " + checkedHistory.size + " 筆歷史紀錄（連同 Job 目錄與 log）？")) return;
  try {
    const { result, warnings } = await api("/history/delete", "POST", { ids: [...checkedHistory], confirm: true });
    checkedHistory.clear();
    await loadHistory();
    announce("已刪除 " + result.deleted + " 筆紀錄（" + result.deletedJobs + " 個 Job 資料夾）。" +
      (warnings.length ? " 有 " + warnings.length + " 個資料夾無法刪除。" : ""), warnings.length > 0);
  } catch (error) {
    announce(error.message, true);
  }
});

function notificationsEnabled() {
  return Object.values(operationSettings.notifications).some(Boolean);
}

function prepareAudio() {
  if (!$("notify-sound").checked && !operationSettings.notifications.sound) return;
  try {
    audio ||= new AudioContext();
    audio.resume().catch(() => {});
  } catch {
    // Audio may be unavailable in a browser without audio output.
  }
}

function playNotice() {
  if (!audio || audio.state !== "running") return;
  const oscillator = audio.createOscillator();
  const volume = audio.createGain();
  oscillator.frequency.value = 660;
  volume.gain.setValueAtTime(0.09, audio.currentTime);
  volume.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.22);
  oscillator.connect(volume).connect(audio.destination);
  oscillator.start();
  oscillator.stop(audio.currentTime + 0.23);
}

function stopTitleFlash() {
  clearInterval(titleTimer);
  titleTimer = undefined;
  document.title = "Build Runner";
}

function flashTitle(text) {
  stopTitleFlash();
  document.title = text;
  let count = 0;
  titleTimer = setInterval(() => {
    if (++count >= 10) stopTitleFlash();
    else document.title = count % 2 ? "Build Runner" : text;
  }, 750);
}

function trackNotifications(rows) {
  const first = notificationBaseline === undefined;
  if (first) notificationBaseline = Date.now();
  const ready = [];
  for (const row of rows) {
    const previous = notifiedStates.get(row.id);
    notifiedStates.set(row.id, row.state);
    if (!first && !activeStates.has(row.state) &&
        (activeStates.has(previous) || (!previous && Date.parse(row.endedAt) >= notificationBaseline))) ready.push(row);
  }
  if (!ready.length) return;
  const flags = operationSettings.notifications;
  const title = ready.length === 1 ? ready[0].workspace + " · " + states[ready[0].state] : ready.length + " 個 Build 已結束";
  if (flags.title) flashTitle(title);
  if (flags.sound) playNotice();
  if (flags.desktop && "Notification" in window && Notification.permission === "granted") {
    try {
      const notice = new Notification("Build Runner", { body: title });
      notice.onclick = () => { window.focus(); openLog(ready[0].id); notice.close(); };
    } catch {
      // Some embedded browsers disallow desktop notifications.
    }
  }
}

async function refreshOperationSettings() {
  const { result } = await api("/settings");
  operationSettings = result;
  return result;
}

function renderSettings(settings) {
  const { historyRetention: retention, notifications, ai, autoSkipPrompts } = settings;
  $("retention-days").value = retention.days;
  $("retention-count").value = retention.count;
  for (const key of ["desktop", "title", "sound"]) $("notify-" + key).checked = notifications[key];
  $("auto-skip-prompts").checked = autoSkipPrompts !== false;
  $("ai-default").value = ai.method;
  $("ai-timeout").value = ai.timeoutSec;
  $("ai-executable").value = ai.opencodePath;
  $("ai-args").value = JSON.stringify(ai.opencodeArgs);
  $("ai-base").value = ai.baseUrl;
  $("ai-model").value = ai.model;
  $("ai-key").value = "";
  $("ai-key-state").textContent = ai.hasApiKey ? "已儲存 ••••••••，留空可保留。" : "尚未設定，可依端點需求填寫。";
  $("ai-clear-key").checked = false;
  $("ai-template").value = ai.template;
}

async function saveOperationSettings(body) {
  const { result } = await api("/settings", "POST", body);
  operationSettings = result;
  return result;
}

listen($("retention-form"), "submit", async () => {
  const rules = { days: Number($("retention-days").value), count: Number($("retention-count").value) };
  if ((rules.days || rules.count) && !window.confirm("套用保留規則並清理超出範圍的歷史紀錄？")) return;
  await saveOperationSettings({ historyRetention: rules, confirm: true });
  announce("已儲存歷史保留規則。");
});

listen($("notifications-form"), "submit", async () => {
  const flags = Object.fromEntries(["desktop", "title", "sound"].map((key) => [key, $("notify-" + key).checked]));
  if (flags.sound) prepareAudio();
  let denied = false;
  if (flags.desktop) {
    let permission;
    try {
      permission = "Notification" in window && await Notification.requestPermission();
    } catch {
      permission = "denied";
    }
    denied = permission !== "granted";
    if (denied) flags.desktop = false;
  }
  await saveOperationSettings({ notifications: flags });
  $("notify-desktop").checked = flags.desktop;
  if (!flags.title) stopTitleFlash();
  announce(denied ? "已儲存其他通知選項。瀏覽器未授權桌面通知。" : "已儲存通知設定。", denied);
});

listen($("prompts-form"), "submit", async () => {
  await saveOperationSettings({ autoSkipPrompts: $("auto-skip-prompts").checked });
  announce("已儲存互動提示設定。");
});

listen($("ai-settings-form"), "submit", async () => {
  const ai = {
    method: $("ai-default").value, timeoutSec: Number($("ai-timeout").value),
    opencodePath: $("ai-executable").value.trim(), opencodeArgs: parseJson($("ai-args").value || "[]", "OpenCode 參數"),
    baseUrl: $("ai-base").value.trim(), model: $("ai-model").value.trim(), apiKey: $("ai-key").value,
    clearApiKey: $("ai-clear-key").checked, template: $("ai-template").value,
  };
  const { ai: saved } = await saveOperationSettings({ ai });
  $("ai-key").value = "";
  $("ai-clear-key").checked = false;
  $("ai-key-state").textContent = saved.hasApiKey ? "已儲存 ••••••••，留空可保留。" : "尚未設定，可依端點需求填寫。";
  announce("已儲存 AI 設定。");
});

function updateAnalysisActions() {
  const job = jobs.find((item) => item.id === log.id);
  const task = analysesByJob.get(log.id);
  const running = task?.state === "running";
  $("ai-run").disabled = !job || activeStates.has(job.state) || running;
  $("ai-preview").disabled = !job || activeStates.has(job.state) || running;
  $("ai-cancel").hidden = !running;
  $("ai-method").disabled = !!running;
  $("ai-scope").disabled = !!running;
  $("ai-run").textContent = $("ai-method").value === "copy" ? "複製提示詞" : "送出分析";
}

function logScopePayload() {
  const scope = $("ai-scope").value;
  const payload = { logScope: scope };
  if (scope === "range") {
    const start = Number.parseInt($("ai-log-start").value, 10);
    const end = Number.parseInt($("ai-log-end").value, 10);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
      throw Object.assign(new Error("請輸入有效的起訖行號（起始行 ≥ 1 且不大於結束行）。"), { code: "INVALID_LOG_RANGE" });
    }
    payload.logStart = start;
    payload.logEnd = end;
  }
  return payload;
}

function showAnalysis(text, label = "分析結果") {
  $("ai-output-label").textContent = label;
  $("ai-output").value = text;
  $("ai-output-field").hidden = false;
}

function renderAnalysisTask(task) {
  if (log.id !== task.jobId) return;
  $("ai-status").textContent = {
    running: "AI 分析處理中…", success: "已儲存分析結果。", cancelled: "已取消 AI 分析。",
    failed: task.message || "AI 分析失敗。",
  }[task.state];
  if (task.state === "success") showAnalysis(task.answer);
  updateAnalysisActions();
}

async function loadSavedAnalyses(id) {
  if (!id) return;
  const { result } = await api("/jobs/" + id + "/ai");
  if (log.id !== id) return;
  const prior = $("ai-saved").value;
  $("ai-saved").replaceChildren(new Option("選擇已儲存結果", ""), ...result.map((file) => new Option(file, file)));
  $("ai-saved").value = result.includes(prior) ? prior : "";
  $("ai-saved-field").hidden = !result.length;
}

function chooseAnalysisJob(id) {
  $("ai-method").value = operationSettings.ai.method;
  $("ai-status").textContent = id ? "選擇分析方式。Job 結束後即可使用。" : "選擇已結束的 Job。";
  $("ai-output-field").hidden = $("ai-saved-field").hidden = true;
  $("ai-output").value = "";
  $("ai-preview-field").hidden = true;
  $("ai-preview-text").value = "";
  $("ai-saved").replaceChildren();
  const task = analysesByJob.get(id);
  if (task) renderAnalysisTask(task);
  updateAnalysisActions();
  loadSavedAnalyses(id).catch((error) => { if (log.id === id) announce(error.message, true); });
  if (id && (!task || task.state === "running")) {
    api("/jobs/" + id + "/ai?active=1").then(({ result }) => {
      if (result) return monitorAnalysis(result);
      if (task?.state === "running" && !analysisPolls.has(task.id)) return monitorAnalysis(task);
    }).catch((error) => { if (log.id === id) announce(error.message, true); });
  }
}

function openSavedAnalysis(id) {
  analysisFocus = true;
  openLog(id);
  if (page === "log") focusAnalysis();
}

function focusAnalysis() {
  if (!analysisFocus) return;
  analysisFocus = false;
  $("ai-panel").scrollIntoView({ block: "start" });
  $("ai-method").focus({ preventScroll: true });
}

$("ai-method").addEventListener("change", updateAnalysisActions);
listen($("ai-saved"), "change", async () => {
  const id = log.id;
  const file = $("ai-saved").value;
  if (!file) return;
  const { result } = await api("/jobs/" + id + "/ai?" + new URLSearchParams({ file }));
  if (log.id === id && $("ai-saved").value === file) showAnalysis(result.answer);
});

async function monitorAnalysis(task) {
  if (analysisPolls.has(task.id)) return;
  const taskId = task.id;
  const jobId = task.jobId;
  analysisPolls.add(taskId);
  let errors = 0;
  try {
    while (true) {
      analysesByJob.set(jobId, task);
      renderAnalysisTask(task);
      if (task.state !== "running") break;
      await new Promise((resolve) => setTimeout(resolve, 500));
      try {
        task = (await api("/ai-tasks/" + taskId)).result;
        errors = 0;
      } catch (error) {
        if (error.code === "AI_TASK_NOT_FOUND" || ++errors >= 3) {
          task = { ...task, state: "failed", message: error.message };
        }
      }
      const latest = analysesByJob.get(jobId);
      if (latest?.id === taskId && latest.state === "cancelled" && task.state === "running") task = latest;
    }
    if (task.state === "success") await loadSavedAnalyses(jobId);
  } finally {
    analysisPolls.delete(taskId);
  }
}

listen($("ai-run"), "click", async () => {
  const id = log.id;
  const method = $("ai-method").value;
  if (method !== "copy" && !window.confirm("將此 Job 的提示詞和相關 log 送往" + (method === "opencode" ? " OpenCode" : "自訂端點") + "？")) return;
  const { result } = await api("/jobs/" + id + "/ai", "POST", { method, ...logScopePayload() });
  if (method === "copy") {
    if (log.id !== id) return;
    try {
      await navigator.clipboard.writeText(result.prompt);
      $("ai-status").textContent = "已複製提示詞。";
    } catch {
      $("ai-status").textContent = "無法寫入剪貼簿，請手動複製下方文字。";
      showAnalysis(result.prompt, "AI 提示詞");
      $("ai-output").focus();
      $("ai-output").select();
    }
    return;
  }
  await monitorAnalysis(result);
});

listen($("ai-scope"), "change", () => {
  const range = $("ai-scope").value === "range";
  $("ai-start-field").hidden = !range;
  $("ai-end-field").hidden = !range;
});

listen($("ai-preview"), "click", async () => {
  const id = log.id;
  const { result } = await api("/jobs/" + id + "/ai", "POST", { method: "copy", preview: true, ...logScopePayload() });
  if (log.id !== id) return;
  $("ai-preview-text").value = result.prompt;
  $("ai-preview-meta").textContent = "（" + result.prompt.length.toLocaleString() + " 字元）";
  $("ai-preview-field").hidden = false;
});

listen($("ai-cancel"), "click", async () => {
  const task = analysesByJob.get(log.id);
  if (!task || task.state !== "running") return;
  const { result } = await api("/ai-tasks/" + task.id + "/cancel", "POST");
  analysesByJob.set(result.jobId, result);
  renderAnalysisTask(result);
});

async function loadSchedules() {
  const list = $("schedule-list");
  try {
    const { result } = await api("/schedules");
    const rows = result.map((schedule) => {
      const card = element("article", "editor-item stack");
      card.append(element("strong", "", schedule.name),
        element("p", "tb-muted", [schedule.workspace, schedule.recipe, schedule.frequency === "DAILY" ? "每日" :
          schedule.frequency === "WEEKLY" ? "每週 " + schedule.day : "", schedule.time,
          schedule.enabled === false ? "已停用" : schedule.schedulerStatus].filter(Boolean).join(" · ")));
      const actions = element("div", "row");
      if (schedule.enabled !== null) actions.append(button(schedule.enabled === false ? "啟用排程" : "停用排程", async () => {
        await api("/schedules", "POST", { name: schedule.name, enabled: schedule.enabled === false });
        await loadSchedules();
      }));
      actions.append(button("刪除排程", async () => {
        if (!window.confirm("刪除排程「" + schedule.name + "」？")) return;
        await api("/schedules", "DELETE", { name: schedule.name, confirm: true });
        await loadSchedules();
      }));
      card.append(actions);
      return card;
    });
    list.replaceChildren(...(rows.length ? rows : [empty("尚未設定排程。")]));
  } catch (error) {
    list.replaceChildren(empty(error.message));
  }
}

$("schedule-frequency").addEventListener("change", () => {
  $("schedule-day-field").hidden = $("schedule-frequency").value !== "WEEKLY";
});
listen($("schedule-form"), "submit", async (event) => {
  const body = Object.fromEntries(new FormData(event.currentTarget));
  await api("/schedules", "POST", body);
  await loadSchedules();
  announce("已建立排程。");
});

async function loadSettingsPage() {
  const settings = await refreshOperationSettings();
  renderSettings(settings);
  await Promise.all([loadSchedules(), Promise.all([refreshWorkspaces(), loadConfiguration()]).then(() => {
    selectRecords("schedule-workspace", workspaces, "請選擇 Workspace");
    selectRecords("schedule-recipe", configuration.recipes, "請選擇 Recipe");
  })]);
}
