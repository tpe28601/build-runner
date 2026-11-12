"use strict";

const $ = (id) => document.getElementById(id);
const nav = [...document.querySelectorAll(".nav a")];
const filters = [...document.querySelectorAll("[data-kind]")];
const pages = nav.map((link) => link.hash.slice(1));
const activeStates = new Set(["queued", "running"]);
const LIVE_WINDOW = 5000;   // max recent lines kept in the log DOM
const TAIL_LINES = 2000;    // first-screen lines for a finished job
const states = { queued: "排隊中", running: "執行中", success: "成功", failed: "失敗", stopped: "已停止", unknown: "未判定" };
const tones = { queued: "warn", running: "accent", success: "ok", failed: "danger", stopped: "dry" };
// 與後端 build_runner/prompts.py 相同的提示偵測（账号/密码/passphrase 等待輸入）
const PROMPT_PATTERN = /\benter (?:your username|password for|passphrase for)\b/i;
const selected = new Set();
const gitDetails = new Map();
let page;
let navigation = 0;
let workspaces = [];
let jobs = [];
let jobsKey = "";
let buildChecked = "";
let worktreeRepo = "";
let log = newLog("");

function newLog(id) {
  return { id, offset: 0, line: 1, partial: "", index: -1, total: 0, kind: "", highlighted: [],
    target: null, searchVersion: 0, jumpVersion: 0, lastScan: 0, dirty: true, final: false,
    reading: false, scanning: false, searching: 0, booting: true, promptSeen: false, tail: "" };
}

function element(tag, classes, text) {
  const node = document.createElement(tag);
  if (classes) node.className = classes;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const node = element("i");
  node.dataset.tbIcon = name;
  return node;
}

function button(text, action, classes = "tb-btn") {
  const node = element("button", classes, text);
  node.type = "button";
  listen(node, "click", action);
  return node;
}

function listen(node, event, action) {
  let pending = false;
  node.addEventListener(event, async (e) => {
    if (event === "submit") e.preventDefault();
    if (pending) return;
    pending = true;
    try {
      await action(e);
    } catch (error) {
      announce(error.message, true);
    } finally {
      pending = false;
    }
  });
}

// ---------- 資料夾挑選（ui-kit tb-dirpicker 共用元件） ----------
function openFolderPicker(input, relative = false) {
  let wsId = null, wsRoot = null;
  const wsRoots = {};   // 正規化 Workspace 根路径 -> {id, root}
  async function fetchDir(path, opts) {
    const body = { path: path || "" };
    if (relative) {
      body.relative = true;
      const key = (path || "").replace(/[\\/]+$/, "").toLowerCase();
      if (!wsId && wsRoots[key]) { wsId = wsRoots[key].id; wsRoot = wsRoots[key]; }
      if (wsId) body.workspaceId = wsId;
    }
    if (opts && opts.scan) { body.scan = true; body.depth = opts.depth || 2; body.limit = opts.limit || 50; }
    const { result } = await api("/browse", "POST", body);
    if (relative && !wsId && result.items) {
      // Workspace 清單：轉成可瀏覽的絕對路徑項目，並記住各自所屬 Workspace
      result.items = result.items.map((item) => {
        if (!item.workspaceId || !item.rootPath) return item;
        const key = item.rootPath.replace(/[\\/]+$/, "").toLowerCase();
        wsRoots[key] = { id: item.workspaceId, root: item.rootPath };
        return { name: item.name, path: item.rootPath, hasGit: false };
      });
    }
    return result;
  }
  const raw = input.value.trim().split("{", 1)[0];
  const absolute = /^(?:[a-z]:[\\/]|\/)/i.test(raw);
  return TbDirPicker.open({
    title: relative ? "選擇 Workspace 資料夾" : "選擇資料夾",
    start: !relative && absolute ? raw : "",
    fetchDir,
  }).then((picked) => {
    if (!picked) return;
    if (relative && wsRoot && picked.toLowerCase().startsWith(wsRoot.root.toLowerCase())) {
      const rel = picked.slice(wsRoot.root.length).replace(/^[\\/]+/, "");
      input.value = rel ? rel.split(/[\\/]+/).join("/") : ".";
    } else {
      input.value = picked;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  });
}

for (const trigger of document.querySelectorAll("[data-folder-target]")) {
  listen(trigger, "click", () => openFolderPicker($(trigger.dataset.folderTarget)));
}

async function api(path, method = "GET", body = {}) {
  const options = method === "GET" ? {} : {
    method, headers: { "Content-Type": "application/json", "X-Toolbox-Request": "1" }, body: JSON.stringify(body),
  };
  const response = await fetch("/api" + path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "請求失敗");
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

let toastTimer;

function announce(text, error = false, pending = false) {
  const toast = $("toast");
  clearTimeout(toastTimer);
  toast.classList.toggle("tb-toast--ok", !error && !pending);
  toast.classList.toggle("tb-toast--danger", error);
  toast.textContent = text;
  toast.hidden = !text;
  if (text && !pending) {
    toastTimer = setTimeout(() => {
      toast.hidden = true;
      toast.textContent = "";
    }, error ? 6000 : 3200);
  }
}

function promptAtTail(text) {
  // 與後端 prompts.py 相同的規則：向後跳過空行與本工具自加的 [Build Runner]
  // 記號行（記號可能蓋在最新提示之上），比對步骤真正的最後一行。
  const lines = (text || "").slice(-4096).split("\n");
  for (let i = lines.length - 1; i >= 0; --i) {
    const line = lines[i].trim();
    if (!line || line.startsWith("[Build Runner]")) continue;
    return PROMPT_PATTERN.test(line);
  }
  return false;
}

function badge(state) {
  return element("span", "tb-badge" + (tones[state] ? " tb-badge--" + tones[state] : ""), states[state] || "尚無紀錄");
}

function empty(text) {
  return element("div", "tb-empty", text);
}

function timeLabel(value) {
  return value ? new Date(value).toLocaleString("zh-TW", { hour12: false }) : "—";
}

function artifactSummary(row) {
  const files = row.artifacts || [];
  const errors = row.artifactErrors || [];
  if (!files.length && !errors.length) return null;
  const detail = element("details", "artifact-summary");
  detail.append(element("summary", "", "產出物 " + files.length + " 個" + (errors.length ? " · 失敗 " + errors.length + " 筆" : "")));
  const list = element("ul", "artifact-results");
  for (const file of files) list.append(element("li", "tb-mono", file));
  for (const error of errors) list.append(element("li", "preview-warning", error.message));
  detail.append(list);
  return detail;
}

function duration(seconds) {
  const elapsed = Math.max(0, Math.floor(seconds));
  return elapsed >= 60 ? Math.floor(elapsed / 60) + " 分 " + elapsed % 60 + " 秒" : elapsed + " 秒";
}

function updateElapsed() {
  document.querySelectorAll("[data-start]").forEach((node) => {
    node.textContent = duration((Date.now() - Date.parse(node.dataset.start)) / 1000);
  });
}

function openLog(id) {
  chooseLog(id);
  if (page !== "log") location.hash = "log";
  else pollLog().catch((error) => announce(error.message, true));
}

function jobCard(row) {
  const card = element("article", "tb-card tb-card--hover stack");
  const head = element("div", "row");
  head.append(element("h2", "job-title", row.workspace), badge(row.state));
  const details = element("p", "tb-muted", [row.recipe, row.branch, row.commit].filter(Boolean).join(" · "));
  const step = element("p", "tb-small", row.step ? "步驟 · " + row.step : "等待排程");
  const foot = element("div", "row");
  const elapsed = element("span", "tb-muted tb-small");
  if (row.state === "running" && row.startedAt) elapsed.dataset.start = row.startedAt;
  else if (row.endedAt) {
    const seconds = row.durationSec ?? (row.startedAt ? (Date.parse(row.endedAt) - Date.parse(row.startedAt)) / 1000 : 0);
    elapsed.textContent = timeLabel(row.endedAt) + " · " + duration(seconds);
  }
  foot.append(button("查看 Log", () => openLog(row.id)));
  if (activeStates.has(row.state)) {
    const label = row.state === "queued" ? "取消排隊" : "停止";
    foot.append(button(label, async () => {
      if (!window.confirm(label + "「" + row.workspace + "」的 Build？")) return;
      await api("/jobs/" + row.id + "/stop", "POST", { confirm: true });
      await syncJobs();
    }));
  }
  if (row.state === "running") {
    // 給卡住的互動提示（账号/密码/passphrase）送一個 Enter；後端 runner 0.5 秒內送入。
    const enter = button("送 Enter", async () => {
      await api("/jobs/" + row.id + "/enter", "POST");
      announce("已送出 Enter，等待輸入的步驟會繼續。");
      enter.disabled = true;
      setTimeout(() => { enter.disabled = false; }, 3000);
    });
    foot.append(enter);
  }
  foot.append(elapsed);
  card.append(head, details);
  if (row.step || activeStates.has(row.state)) card.append(step);
  if (row.message) card.append(element("p", "tb-small tb-muted", row.message));
  if (row.historyError) card.append(element("p", "preview-warning", "歷史紀錄寫入失敗。"));
  const outputs = artifactSummary(row);
  if (outputs) card.append(outputs);
  card.append(foot);
  return card;
}

function renderOverview() {
  const active = jobs.filter((row) => activeStates.has(row.state));
  $("running-count").textContent = active.filter((row) => row.state === "running").length;
  $("queued-count").textContent = active.filter((row) => row.state === "queued").length;
  $("active-jobs").replaceChildren(...(active.length ? active.map((row) => jobCard(row)) : [empty("目前沒有執行中或排隊的 Job。")]));
  const recent = jobs.filter((row) => !activeStates.has(row.state)).slice(0, 6);
  $("recent-jobs").replaceChildren(...(recent.length ? recent.map((row) => jobCard(row)) : [empty("尚未有建置結果。")]));
  updateElapsed();
}

function syncLogPicker() {
  if (log.id && !jobs.some((row) => row.id === log.id)) chooseLog("");   // deleted job
  const options = [new Option("選擇 Job", "")];
  for (const row of jobs) options.push(new Option(row.workspace + " · " + row.recipe + " · " + states[row.state], row.id));
  $("log-job").replaceChildren(...options);
  $("log-job").value = log.id;
}

async function syncJobs(force = false) {
  const { result } = await api("/jobs");
  trackNotifications(result);
  const key = JSON.stringify(result);
  if (key !== jobsKey || force) {
    jobs = result;
    jobsKey = key;
    if (page === "overview") renderOverview();
    if (page === "log") syncLogPicker();
  }
}

function workspaceChoice(row, prefix) {
  const label = element("label", "check");
  const box = element("input");
  box.type = "checkbox";
  box.value = row.id;
  box.checked = selected.has(row.id);
  box.id = prefix + "-" + row.id;
  box.addEventListener("change", () => {
    if (box.checked) selected.add(row.id);
    else selected.delete(row.id);
    buildChecked = "";
    if (page === "build") renderBuildPreview();
    if (page === "workspaces") $("workspace-build").disabled = !selected.size;
  });
  label.append(box, element("span", "", row.name));
  return label;
}

async function refreshWorkspaces() {
  const { result } = await api("/workspaces");
  workspaces = result;
  const ids = new Set(result.map((row) => row.id));
  for (const id of selected) if (!ids.has(id)) selected.delete(id);
  if (page === "workspaces") renderWorkspaces();
  if (page === "build") renderBuildChoices();
}

function renderWorkspaces() {
  $("workspace-build").disabled = !selected.size;
  $("workspace-list").replaceChildren(...(workspaces.length ? workspaces.map(workspaceCard) : [
    empty("尚未登錄 Workspace。請新增資料夾或從 Git repo 匯入。"),
  ]));
}

function workspaceCard(row) {
  const card = element("article", "tb-card tb-card--hover stack");
  const head = element("div", "row");
  head.append(workspaceChoice(row, "workspace"), badge(row.lastResult));
  card.append(head, element("p", "tb-mono workspace-path", row.path));
  const metadata = row.gitCommonDir ?
    [row.branch || "detached HEAD", row.commit, row.subject, row.dirty ? "有未提交變更" : "工作樹乾淨"].filter(Boolean).join(" · ") :
    "非 Git 資料夾";
  card.append(element("p", "git-meta tb-muted", metadata));
  const actions = element("div", "row");
  actions.append(button("重新整理 Git", () => runGit(row, "refresh")));
  if (row.gitCommonDir) actions.append(button("Pull ff-only", () => runGit(row, "pull")));
  actions.append(
    button("改名", async () => {
      const name = window.prompt("Workspace 名稱", row.name)?.trim();
      if (!name || name === row.name) return;
      await api("/workspaces", "POST", { id: row.id, name });
      await refreshWorkspaces();
    }),
    button("移除", async () => {
      if (!window.confirm("從 Build Runner 移除「" + row.name + "」的登錄？")) return;
      await api("/workspaces", "DELETE", { id: row.id, confirm: true });
      await refreshWorkspaces();
    }),
  );
  card.append(actions);
  const branches = gitDetails.get(row.id);
  if (branches?.length) {
    const label = element("label", "tb-field");
    const select = element("select", "tb-select");
    select.append(new Option("選擇 local 或 remote branch", ""));
    for (const branch of branches) select.append(new Option(branch.replace(/^refs\/(?:heads|remotes)\//, ""), branch));
    label.append(element("span", "tb-field__label branch-label", "切換分支"), select);
    card.append(label, button("切換", () => {
      if (!select.value) throw new Error("請選擇分支。");
      return runGit(row, "checkout", select.value);
    }));
  } else if (row.gitCommonDir) {
    card.append(element("p", "tb-field__hint", "重新整理 Git 可列出可切換的分支。"));
  }
  return card;
}

async function runGit(row, action, branch) {
  const label = action === "pull" ? "Pull ff-only" : "切換分支";
  if (action !== "refresh" && !window.confirm("對「" + row.name + "」執行 " + label + "？")) return;
  announce("Git 工作處理中…", false, true);
  try {
    const { result } = await api("/workspaces/" + row.id + "/git", "POST", { action, branch, confirm: action !== "refresh" });
    let status = result;
    while (status.state === "running") {
      await new Promise((resolve) => setTimeout(resolve, 350));
      status = (await api("/git-tasks/" + status.id)).result;
    }
    if (status.state !== "success") throw new Error(status.error?.message || "Git 工作失敗。");
    gitDetails.set(row.id, status.branches);
    await refreshWorkspaces();
    announce("Git 狀態已更新。");
  } catch (error) {
    announce(error.message, true);
  }
}

listen($("workspace-add"), "submit", async (e) => {
  const form = e.currentTarget;
  await api("/workspaces", "POST", Object.fromEntries(new FormData(form)));
  form.reset();
  await refreshWorkspaces();
  announce("已新增 Workspace。");
});

listen($("worktree-import"), "submit", async (e) => {
  const input = new FormData(e.currentTarget).get("path");
  const { result } = await api("/workspaces/import-worktrees", "POST", { path: input });
  worktreeRepo = input;
  const options = result.map((path) => {
    const label = element("label", "check");
    const box = element("input");
    box.type = "checkbox";
    box.value = path;
    label.append(box, element("span", "tb-mono", path));
    return label;
  });
  $("worktree-choices").replaceChildren(...(options.length ? options : [empty("找不到 worktree。")]));
  $("import-selected").hidden = !options.length;
});

listen($("import-selected"), "click", async () => {
  const paths = [...$("worktree-choices").querySelectorAll("input:checked")].map((box) => box.value);
  if (!paths.length) throw new Error("請先選取要匯入的 worktree。");
  const { result, warnings } = await api("/workspaces/import-worktrees", "POST", { path: worktreeRepo, paths });
  $("worktree-choices").replaceChildren();
  $("import-selected").hidden = true;
  await refreshWorkspaces();
  announce("已匯入 " + result.length + " 個 worktree" + (warnings.length ? "，略過 " + warnings.length + " 個。" : "。"));
});

listen($("workspace-build"), "click", () => { location.hash = "build"; });

function renderBuildChoices() {
  $("build-workspaces").replaceChildren(...(workspaces.length ? workspaces.map((row) => workspaceChoice(row, "build")) : [
    empty("請先在 Workspace 分頁登錄資料夾。"),
  ]));
}

async function loadRecipes() {
  await loadConfiguration();
  selectRecords("recipe", configuration.recipes, "請選擇 Recipe");
  renderBuildParameters();
}

function buildInputs() {
  const body = { workspaceIds: [...selected], recipe: $("recipe").value, confirm: $("allow-conflict").checked,
    ...buildParameterPayload() };
  if (!body.workspaceIds.length || !body.recipe) throw new Error("請選擇 Workspace 與 Recipe。");
  return body;
}

async function checkBuild(body) {
  $("preview").replaceChildren();
  try {
    const { result, warnings } = await api("/jobs/check", "POST", body);
    $("preview-hint").textContent = warnings.length ? "請確認下列警告再開始 Build。" : "檢查完成，未發現衝突。";
    for (const plan of result) {
      $("preview").append(element("strong", "", plan.workspace));
      for (const argv of plan.argvPreview) $("preview").append(element("pre", "preview-command", JSON.stringify(argv)));
    }
    for (const warning of warnings) {
      const text = warning.code === "SHARED_GIT_DIR" ? "有 Workspaces 共用同一 Git repo。" : "有 Job 使用相同的資料夾。";
      $("preview").append(element("p", "preview-warning", text));
    }
    buildChecked = JSON.stringify(body);
    return warnings;
  } catch (error) {
    buildChecked = "";
    $("preview-hint").textContent = "檢查未通過。";
    const text = error.code === "WORKSPACE_CONFLICT" ? "同一資料夾已有執行中或排隊的 Job。" : error.message;
    const warning = element("p", "preview-warning", text);
    warning.dataset.error = "";
    $("preview").append(warning);
    throw new Error(text);
  }
}

listen($("check-build"), "click", () => checkBuild(buildInputs()));
listen($("build-form"), "submit", async () => {
  const body = buildInputs();
  if (JSON.stringify(body) !== buildChecked) {
    const warnings = await checkBuild(body);
    if (warnings.length) return;
  }
  const { result } = await api("/jobs", "POST", body);
  $("allow-conflict").checked = false;
  buildChecked = "";
  await syncJobs();
  openLog(result[0].id);
  announce("已建立 " + result.length + " 個 Job。");
});

listen($("limit-form"), "submit", async () => {
  const { result } = await api("/settings", "POST", { maxConcurrentJobs: Number($("limit").value) });
  $("limit").value = result.maxConcurrentJobs;
  await syncJobs(true);
  announce("已更新同時執行上限。");
});

async function loadOverview() {
  const { result } = await api("/settings");
  $("limit").value = result.maxConcurrentJobs;
  await syncJobs(true);
}

async function loadHistory() {
  const result = await loadHistoryRows();
  const container = $("history-list");
  if (!result.length) {
    container.replaceChildren(empty("尚未有歷史紀錄。"));
    return;
  }
  const wrap = element("div", "tb-table-wrap history-table");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "建置歷史表格");
  const table = element("table", "tb-table");
  const header = table.createTHead().insertRow();
  for (const title of ["選取", "Workspace / Recipe", "分支 / Commit", "結果", "結束時間 / 耗時", "根本錯誤", "Log"]) {
    const cell = element("th", "", title);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const row of result) {
    const record = body.insertRow();
    record.insertCell().append(historyCheckbox(row));
    record.insertCell().append(element("strong", "", row.workspace), element("div", "tb-small tb-muted", row.recipe));
    record.insertCell().textContent = [row.branch, row.commit].filter(Boolean).join(" · ") || "—";
    const result = record.insertCell();
    result.append(badge(row.state));
    const outputs = artifactSummary(row);
    if (outputs) result.append(outputs);
    record.insertCell().append(element("div", "", timeLabel(row.endedAt)), element("div", "tb-small tb-muted", duration(row.durationSec)));
    const root = record.insertCell();
    root.className = "history-root";
    const rootText = row.state === "success" ? "—" : row.rootError?.text || "—";
    root.textContent = rootText;
    if (rootText !== "—") root.title = rootText;
    const actions = element("div", "row");
    const aiButton = button("AI 分析", () => openSavedAnalysis(row.id));
    aiButton.hidden = operationSettings.ai?.enabled !== true;   // hidden while the feature switch is off
    actions.append(button("查看 Log", () => openLog(row.id)), aiButton);
    record.insertCell().append(actions);
  }
  wrap.append(table);
  container.replaceChildren(wrap);
}


function chooseLog(id) {
  if (id === log.id) return;
  clearTimeout(searchTimer);
  log = newLog(id);
  $("log-query").value = "";
  $("follow").checked = true;
  $("live-lines").replaceChildren();
  $("archive-lines").replaceChildren();
  $("triage").replaceChildren();
  $("triage").hidden = true;
  $("log-state").replaceWith(Object.assign(badge(), { id: "log-state", textContent: "尚未選擇 Job" }));
  $("log-job").value = id;
  const download = $("log-download");
  download.href = id ? "/api/jobs/" + encodeURIComponent(id) + "/log?download=1" : "#";
  download.hidden = !id;
  $("log-footer").textContent = id ? "等待輸出…" : "選擇 Job 即可查看輸出。";
  renderSearch(log, { total: 0, counts: {}, matches: [] });
  showLive();
  chooseAnalysisJob(id);
}

function showLive() {
  $("archive-lines").replaceChildren();
  $("archive-lines").hidden = true;
  $("live-lines").hidden = false;
  $("back-live").hidden = true;
  if ($("follow").checked) $("log-output").scrollTop = $("log-output").scrollHeight;
}

function lineNode(number, text, prefix) {
  const row = element("div", "log-line");
  row.id = prefix + "-" + number;
  row.append(element("span", "log-number", number), element("span", "log-text", text));
  return row;
}

function trimLive(limit) {
  const container = $("live-lines");
  while (container.childElementCount > limit) container.firstElementChild.remove();
}

function appendLog(view, text) {
  const output = $("log-output");
  const live = $("live-lines");
  // Keep the log's last lines across chunks: an input prompt can end exactly on
  // a read boundary, so only a cumulative tail can be matched (updateEnterBanner).
  view.tail = (view.tail + text).slice(-4096);
  const lines = (view.partial + text).split("\n");
  view.partial = lines.pop();
  if (live.lastElementChild?.dataset.partial) live.lastElementChild.remove();
  // A large chunk can contain more lines than the retained window.
  const skip = Math.max(0, lines.length - LIVE_WINDOW);
  view.line += skip;
  const fragment = document.createDocumentFragment();
  for (const line of lines.slice(skip)) fragment.append(lineNode(view.line++, line.replace(/\r$/, ""), "live"));
  if (view.partial) {
    const last = lineNode(view.line, view.partial, "live");
    last.dataset.partial = "true";
    fragment.append(last);
  }
  live.append(fragment);
  trimLive(LIVE_WINDOW - $("archive-lines").childElementCount);
  if ($("follow").checked && !live.hidden) output.scrollTop = output.scrollHeight;
  $("log-footer").textContent = "保留 " + live.childElementCount.toLocaleString("zh-TW") + " 行近期輸出";
}

async function jumpTo(number) {
  const view = log;
  const version = ++view.jumpVersion;
  $("follow").checked = false;
  view.target?.classList.remove("is-target");
  let target = $("live-" + number);
  if (target) {
    showLive();
  } else {
    const { result } = await api("/jobs/" + view.id + "/log?line=" + number);
    if (view !== log || version !== view.jumpVersion) return;
    const archive = $("archive-lines");
    archive.replaceChildren(...result.lines.map((line, offset) => lineNode(result.firstLine + offset, line, "archive")));
    trimLive(LIVE_WINDOW - archive.childElementCount);
    $("live-lines").hidden = true;
    archive.hidden = false;
    $("back-live").hidden = false;
    target = $("archive-" + number);
  }
  if (!target) return;
  view.target = target;
  target.classList.add("is-target");
  const output = $("log-output");
  output.scrollTop += target.getBoundingClientRect().top - output.getBoundingClientRect().top - 24;
}

// ---------- 輸入提示（等待按 Enter 的帳密／passphrase 提示）----------
function updateEnterBanner(job) {
  const banner = $("enter-banner");
  if (banner) banner.hidden = !(job && job.state === "running" && log.promptSeen);
}

listen($("enter-send"), "click", async () => {
  if (!log.id) return;
  const send = $("enter-send");
  await api("/jobs/" + log.id + "/enter", "POST");
  announce("已送出 Enter，等待輸入的步驟會繼續。");
  send.disabled = true;
  setTimeout(() => { send.disabled = false; }, 3000);
});

function renderTriage(view, report) {
  if (view !== log) return;
  const box = $("triage");
  box.hidden = !report.lineCount;
  const header = element("div", "row");
  header.append(element("strong", "", "Log 判讀"), badge(report.result));
  if (report.cascadeCount) header.append(element("span", "tb-small", "連鎖錯誤 " + report.cascadeCount + " 筆"));
  const root = report.rootError;
  const detail = root
    ? button("第 " + root.line + " 行 · " + root.text, () => jumpTo(root.line))
    : element("p", "tb-small tb-muted", "目前未辨識到根本錯誤。");
  box.replaceChildren(header, detail);
}

function renderSearch(view, result) {
  view.total = result.total;
  const query = $("log-query").value.trim();
  const enabled = !!(query || view.kind);
  for (const filter of filters) {
    filter.setAttribute("aria-pressed", String(view.kind === filter.dataset.kind));
    filter.lastElementChild.textContent = result.counts[filter.dataset.kind] || 0;
  }
  $("previous").disabled = $("next").disabled = !enabled || !result.total;
  const position = view.index >= 0 && view.index < result.total ? view.index + 1 + " / " : "";
  $("match-count").textContent = enabled ? position + result.total + " 筆" : "輸入關鍵字或選擇分類";
  for (const node of view.highlighted) {
    node.classList.remove("is-match");
    const content = node.lastElementChild;
    if (content.firstElementChild) content.textContent = content.textContent;
  }
  view.highlighted = [];
  if (!enabled) return;
  for (const match of result.matches) {
    const node = $("live-" + match.line) || $("archive-" + match.line);
    if (!node) continue;
    node.classList.add("is-match");
    view.highlighted.push(node);
    const content = node.lastElementChild;
    const text = content.textContent;
    const index = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
    if (index !== -1) content.replaceChildren(
      text.slice(0, index), element("mark", "", text.slice(index, index + query.length)), text.slice(index + query.length),
    );
  }
}

async function scan(view, jump = false) {
  if (!view.id) return;
  view.searching++;
  const version = ++view.searchVersion;
  const selected = Math.max(0, view.index);
  const base = Math.floor(selected / 100) * 100;
  const query = new URLSearchParams({ q: $("log-query").value.trim(), kind: view.kind, index: base });
  if (!query.get("q") && !view.kind) query.set("limit", 0);
  try {
    const { result } = await api("/jobs/" + view.id + "/search?" + query);
    if (view !== log || version !== view.searchVersion) return;
    const match = result.matches[selected - base];
    if (jump && match) await jumpTo(match.line);
    if (view === log && version === view.searchVersion) renderSearch(view, result);
  } finally {
    view.searching--;
  }
}

async function scanTriage(view) {
  const { result } = await api("/jobs/" + view.id + "/triage");
  renderTriage(view, result);
}

async function pollLog() {
  const view = log;
  if (!view.id || view.reading) return;
  view.reading = true;
  try {
    const job = jobs.find((item) => item.id === view.id);
    if (job) $("log-state").replaceWith(Object.assign(badge(job.state), { id: "log-state" }));
    updateAnalysisActions();
    if (!view.final) {
      if (view.booting) {
        // First screen reads the file tail; replaying the whole log would stall
        // the tab while the DOM window keeps only the most recent lines anyway.
        const { result } = await api("/jobs/" + view.id + "/log?tail=" + TAIL_LINES);
        if (view !== log) return;
        view.booting = false;   // only after success: a failed tail must not degrade to ?from=0
        view.offset = result.nextOffset;
        view.line = result.startLine;
        if (result.text) appendLog(view, result.text);
        view.promptSeen = promptAtTail(view.tail);
        if (job && !activeStates.has(job.state)) view.final = true;
        view.dirty = true;
      } else {
        const { result } = await api("/jobs/" + view.id + "/log?from=" + view.offset);
        if (view !== log) return;
        view.offset = result.nextOffset;
        if (result.text) {
          appendLog(view, result.text);
          view.promptSeen = promptAtTail(view.tail);
          view.dirty = true;
        } else if (job && !activeStates.has(job.state)) {
          // A status poll can observe completion before the last offset read.
          view.final = true;
          view.dirty = true;
        }
      }
    }
    updateEnterBanner(job);
    if (view.dirty && !view.scanning && !view.searching && (view.final || Date.now() - view.lastScan >= 2500)) {
      view.dirty = false;
      view.scanning = true;
      view.lastScan = Date.now();
      Promise.all([scan(view), scanTriage(view)]).catch((error) => {
        if (view === log) announce(error.message, true);
      }).finally(() => { view.scanning = false; });
    }
  } finally {
    view.reading = false;
  }
}

listen($("log-job"), "change", () => {
  chooseLog($("log-job").value);
  return pollLog();
});
listen($("back-live"), "click", () => { $("follow").checked = true; showLive(); });
listen($("follow"), "change", () => { if ($("follow").checked) showLive(); });

let searchTimer;
$("log-query").addEventListener("input", () => {
  log.index = -1;
  log.searchVersion++;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => scan(log).catch((error) => announce(error.message, true)), 180);
});

for (const filter of filters) listen(filter, "click", () => {
  log.kind = log.kind === filter.dataset.kind ? "" : filter.dataset.kind;
  log.index = -1;
  return scan(log);
});

for (const [id, direction] of [["previous", -1], ["next", 1]]) listen($(id), "click", () => {
  if (!log.total) return;
  const start = log.index === -1 && direction === -1 ? 0 : log.index;
  log.index = (start + direction + log.total) % log.total;
  return scan(log, true);
});

async function navigate() {
  window.applyAiVisibility?.();   // keep the analysis panel in step with the feature switch
  const hash = location.hash.slice(1);
  page = pages.includes(hash) ? hash : "overview";
  const current = ++navigation;
  for (const name of pages) $(name).hidden = name !== page;
  for (const link of nav) {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  try {
    if (page === "overview") await loadOverview();
    if (page === "workspaces") await refreshWorkspaces();
    if (page === "build") await Promise.all([refreshWorkspaces(), loadRecipes()]);
    if (page === "recipes") await loadConfigurationEditor();
    if (page === "history") await loadHistory();
    if (page === "settings") await loadSettingsPage();
    if (page === "log") {
      await syncJobs(true);
      if (current === navigation) {
        await pollLog();
        focusAnalysis();
      }
    }
  } catch (error) {
    if (current === navigation) announce(error.message, true);
  }
}

async function tick() {
  try {
    if (page === "overview" || page === "log" || notificationsEnabled()) await syncJobs();
    if (page === "overview") updateElapsed();
    if (page === "log") await pollLog();
  } catch (error) {
    announce(error.message, true);
  } finally {
    setTimeout(tick, 1000);
  }
}

document.addEventListener("tb-theme-change", () => { $("theme").value = TbTheme.get().mode; });
$("theme").value = TbTheme.get().mode;
$("theme").addEventListener("change", () => TbTheme.set($("theme").value));
window.addEventListener("hashchange", navigate);
document.addEventListener("DOMContentLoaded", () => {
  initializeOperations();
  navigate();
  setTimeout(tick, 1000);
});
