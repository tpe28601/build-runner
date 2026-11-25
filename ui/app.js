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
const pendingJobActions = new Set();
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
    reading: false, scanning: false, searching: 0, booting: true, promptSeen: false, tail: "", triageKey: "" };
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
// relative：結果寫回相對路徑，瀏覽範圍由後端錨定。anchor＝"repo" 時錨到 Git
// 工作樹頂層（產出物規則「資料來源＝Repo 根」），否則錨 Workspace 根。
function openFolderPicker(input, relative = false, anchor = null) {
  let wsId = null, wsRoot = null;
  const wsRoots = {};   // 正規化 Workspace 根路径 -> {id, root}
  async function fetchDir(path, opts) {
    const body = { path: path || "" };
    if (relative) {
      body.relative = true;
      if (anchor === "repo") body.anchor = "repo";
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
    // 後端回報實際瀏覽根（anchor=repo 時為 repo 根）：選取後轉相對路徑要以它為準
    if (relative && wsRoot && result.rootPath) wsRoot.root = result.rootPath;
    return result;
  }
  const raw = input.value.trim().split("{", 1)[0];
  const absolute = /^(?:[a-z]:[\\/]|\/)/i.test(raw);
  return TbDirPicker.open({
    title: relative ? (anchor === "repo" ? "選擇 Repo 根資料夾（Build 產出所在）" : "選擇 Workspace 資料夾") : "選擇資料夾",
    start: !relative && absolute ? raw : "",
    fetchDir,
  }).then((picked) => {
    if (!picked) return;
    // 前綴比對要含路徑分隔邊界：錨點 E:\repo 不可讓 E:\repo2\... 也算境內（repo 錨點下
    // 同字首兄弟資料夾常見）；選到錨點本身則寫回 "."。
    const rootNorm = wsRoot ? wsRoot.root.replace(/[\\/]+$/, "") : "";
    const pickedLc = picked.toLowerCase();
    const rootLc = rootNorm.toLowerCase();
    if (relative && wsRoot && (pickedLc === rootLc || pickedLc.startsWith(rootLc + "\\") || pickedLc.startsWith(rootLc + "/"))) {
      const rel = picked.slice(rootNorm.length).replace(/^[\\/]+/, "");
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

function artifactLocation(row) {
  if (row?.state !== "success") return null;
  const folders = new Map();
  for (const [index, file] of (row.artifacts || []).entries()) {
    const end = Math.max(file.lastIndexOf("/"), file.lastIndexOf("\\"));
    if (end < 0) continue;
    const folder = file.slice(0, end + 1);
    const key = folder.replaceAll("\\", "/").toLowerCase();
    if (!folders.has(key)) folders.set(key, { folder, index });
  }
  if (!folders.size) return null;
  const select = element("select", "tb-select");
  select.setAttribute("aria-label", "產出物資料夾");
  for (const { folder, index } of folders.values()) select.add(new Option(folder, index));
  const control = element("div", "row artifact-location");
  if (folders.size > 1) control.append(select);
  control.append(button("開啟產出物位置", () =>
    api("/jobs/" + row.id + "/open", "POST", { index: Number(select.value) })));
  return control;
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

function canRestartJob(row) {
  if (!row) return false;
  return activeStates.has(row.state) ||
    ((row.state === "stopped" || row.state === "failed") && !jobs.some((job) => job.id === row.restartedAs));
}

function updateLogJobControls() {
  const job = jobs.find((row) => row.id === log.id);
  const active = job && activeStates.has(job.state);
  const pending = job && pendingJobActions.has(job.id);
  const stop = $("log-stop-job");
  const stopLabel = job?.state === "queued" ? "取消排隊" : "停止建置";
  stop.title = stopLabel;
  stop.setAttribute("aria-label", stopLabel);
  stop.hidden = !active;
  $("log-restart-job").hidden = !canRestartJob(job);
  stop.disabled = $("log-restart-job").disabled = !!pending;
  const location = $("log-artifact-location");
  const jobId = job?.state === "success" ? job.id : "";
  if (location.dataset.jobId !== jobId) {
    const control = artifactLocation(job);
    location.replaceChildren();
    if (control) location.append(control);
    location.hidden = !control;
    location.dataset.jobId = jobId;
  }
}

async function controlJob(row, action) {
  const restarting = action === "restart";
  if (!row || pendingJobActions.has(row.id)) return;
  const active = activeStates.has(row.state);
  if (restarting ? !canRestartJob(row) : !active) return;
  const label = row.state === "queued" ? "取消排隊" : "停止建置";
  const prompt = restarting
    ? (active ? "結束目前的 Job，並從第一步重新建置「" : "從第一步重新建置「") + row.workspace + "」？"
    : label + "「" + row.workspace + "」？";
  if (!window.confirm(prompt)) return;
  pendingJobActions.add(row.id);
  updateLogJobControls();
  try {
    const { result, warnings } = await api("/jobs/" + row.id + "/" + action, "POST", { confirm: true });
    await syncJobs();
    if (restarting) {
      openLog(result.id);
      const codes = new Set((warnings || []).map((warning) => warning.code));
      const note = codes.has("SHARED_GIT_DIR") ? "（警告：有 Workspaces 共用同一 Git repo。）"
        : codes.has("QUEUE_SAME_WORKSPACE") ? "（同一資料夾已有 Job，新的會排在後面依序執行。）" : "";
      announce("已建立重新建置的 Job。" + note);
    } else {
      announce(result.state === "stopped" ? "已停止 Job。" : "Job 已結束。");
    }
  } finally {
    pendingJobActions.delete(row.id);
    updateLogJobControls();
  }
}

function jobActionButton(row, action, label, iconName, danger = false) {
  const classes = danger ? "tb-btn tb-btn--icon tb-btn--danger" : "tb-btn tb-btn--icon";
  const control = button("", () => controlJob(row, action), classes);
  control.title = label;
  control.setAttribute("aria-label", label);
  control.innerHTML = TbIcons.svg(iconName);
  return control;
}

function jobCard(row) {
  const card = element("article", "tb-card tb-card--hover stack");
  const head = element("div", "row");
  head.append(element("h2", "job-title", row.workspace), badge(row.state));
  const details = element("p", "tb-muted",
    [row.recipe, row.checkoutTarget && ("目標 " + row.checkoutTarget), row.branch, row.commit].filter(Boolean).join(" · "));
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
    const label = row.state === "queued" ? "取消排隊" : "停止建置";
    foot.append(jobActionButton(row, "stop", label, "square", true));
  }
  if (canRestartJob(row)) foot.append(jobActionButton(row, "restart", "重新建置", "refresh-cw"));
  foot.append(elapsed);
  card.append(head, details);
  if (row.step || activeStates.has(row.state)) card.append(step);
  if (row.message) card.append(element("p", "tb-small tb-muted", row.message));
  if (row.historyError) card.append(element("p", "preview-warning", "歷史紀錄寫入失敗。"));
  const outputs = artifactSummary(row);
  if (outputs) card.append(outputs);
  const location = artifactLocation(row);
  if (location) card.append(location);
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
  updateLogJobControls();
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

function workspaceChoice(row, prefix, classes = "check") {
  const label = element("label", classes);
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
  if (page === "workspaces") {
    renderWorkspaces();
    void loadWorkspaceDirty();   // badges fill in afterwards; the list stays instant
  }
  if (page === "build") renderBuildChoices();
}

let dirtyBusy = false;

async function loadWorkspaceDirty() {
  if (dirtyBusy) return;   // a running scan merges into the refreshed rows anyway
  dirtyBusy = true;
  try {
    const { result } = await api("/workspaces/dirty");
    let changed = false;
    for (const row of workspaces) {
      if (row.id in result && row.dirty !== result[row.id]) {
        row.dirty = result[row.id];
        changed = true;
      }
    }
    if (changed && page === "workspaces") renderWorkspaces();
  } catch {
    // The badges simply stay blank until the next refresh.
  } finally {
    dirtyBusy = false;
  }
}

function renderWorkspaces() {
  $("workspace-build").disabled = !selected.size;
  $("workspace-list").replaceChildren(...(workspaces.length ? workspaces.map(workspaceCard) : [
    empty("尚未登錄 Workspace。請新增資料夾或從 Git repo 匯入。"),
  ]));
}

const gitOperationLabels = {revert: "Revert 進行中", "cherry-pick": "Cherry-pick 進行中", merge: "Merge 進行中",
                            rebase: "Rebase 進行中", bisect: "Bisect 進行中"};

function workspaceCard(row) {
  const card = element("article", "tb-card tb-card--hover stack workspace-card");
  const head = element("div", "row");
  const choice = workspaceChoice(row, "workspace", "check ws-choice");
  const checkbox = choice.querySelector("input");
  head.append(choice, badge(row.lastResult));
  if (row.gitOperation) {
    head.append(element("span", "tb-badge tb-badge--danger", gitOperationLabels[row.gitOperation] || row.gitOperation));
  }
  card.addEventListener("click", (event) => {
    if (event.target.closest("label, input, button, select, a")) return;
    checkbox.click();
  });
  card.append(head, element("p", "tb-mono workspace-path", row.path));
  const dirtyLabel = row.dirty === null || row.dirty === undefined ? "" : row.dirty ? "有未提交變更" : "工作樹乾淨";
  const metadata = row.gitCommonDir ?
    [row.branch || "detached HEAD", row.commit, row.subject, dirtyLabel].filter(Boolean).join(" · ") :
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
    // 分支動輒上百條且名稱很長：輸入關鍵字過濾。原生 datalist 在 Chromium 會畫成深色
    // 泡泡、只列符合目前值的一筆，改用自製下拉清單（外觀同 select 選單）。
    // 候選顯示去 ref 前綴的短名；送回前再解析回完整 ref。
    const label = element("div", "tb-field branch-field");
    const input = element("input", "tb-input tb-input--mono branch-input");
    input.placeholder = "搜尋 local／remote branch，或貼上 commit SHA";
    input.autocomplete = "off";
    input.spellcheck = false;
    const list = element("ul", "branch-menu");
    list.id = "branch-list-" + row.id;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    const toggle = element("button", "branch-toggle");
    toggle.type = "button";
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-label", "顯示全部分支");
    toggle.innerHTML = TbIcons.svg("chevron-down");
    const byDisplay = new Map();   // 短名 -> 完整 ref 清單（local／remote 可能同名）
    const displays = [];
    const groupOf = new Map();     // 短名 -> 「本地」或「遠端」（同名時以本地為準）
    for (const branch of branches) {
      const display = branch.replace(/^refs\/(?:heads|remotes)\//, "");
      if (!byDisplay.has(display)) { byDisplay.set(display, []); displays.push(display); }
      byDisplay.get(display).push(branch);
      if (branch.startsWith("refs/heads/") || !groupOf.has(display)) {
        groupOf.set(display, branch.startsWith("refs/heads/") ? "本地" : "遠端");
      }
    }
    displays.sort((a, b) => (groupOf.get(a) === "本地" ? 0 : 1) - (groupOf.get(b) === "本地" ? 0 : 1));
    if (row.branch && byDisplay.has(row.branch)) input.value = row.branch;   // 預設顯示目前分支
    let active = -1;
    const items = () => [...list.children].filter((node) => node.dataset.value !== undefined);
    const highlight = (index) => {
      const rows = items();
      active = rows.length ? (index + rows.length) % rows.length : -1;
      rows.forEach((node, i) => node.setAttribute("aria-selected", String(i === active)));
      if (active >= 0) { rows[active].scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", rows[active].id); }
      else input.removeAttribute("aria-activedescendant");
    };
    const closeMenu = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };
    const openMenu = (showAll = false) => {
      const query = showAll ? "" : input.value.trim().toLowerCase();
      const matches = query ? displays.filter((name) => name.toLowerCase().includes(query)) : displays;
      list.replaceChildren();
      let group = "";
      matches.slice(0, 300).forEach((name, i) => {
        if (groupOf.get(name) !== group) {
          group = groupOf.get(name);
          const heading = element("li", "branch-group", group);
          heading.setAttribute("role", "presentation");
          list.append(heading);
        }
        const option = element("li", "branch-option" + (name === row.branch ? " is-current" : ""), name);
        option.id = list.id + "-" + i;
        option.dataset.value = name;
        option.setAttribute("role", "option");
        option.addEventListener("mousedown", (event) => {   // mousedown：搶在 input blur 之前
          event.preventDefault();
          input.value = name;
          closeMenu();
        });
        option.addEventListener("mousemove", () => { if (active !== i) highlight(i); });
        list.append(option);
      });
      if (!matches.length) list.append(element("li", "branch-option branch-option--empty", "沒有符合的分支"));
      else if (matches.length > 300) list.append(element("li", "branch-option branch-option--empty", "還有 " + (matches.length - 300) + " 筆，請輸入更多關鍵字"));
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      highlight(matches.indexOf(input.value.trim()));
    };
    input.addEventListener("input", () => openMenu());
    input.addEventListener("focus", () => openMenu(true));
    input.addEventListener("click", () => { if (list.hidden) openMenu(true); });
    input.addEventListener("blur", closeMenu);
    toggle.addEventListener("mousedown", (event) => {
      event.preventDefault();
      if (list.hidden) { input.focus(); openMenu(true); } else closeMenu();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) openMenu(true);
        highlight(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        closeMenu();
      }
    });
    const resolveBranch = () => {
      const v = input.value.trim();
      if (!v) throw new Error("請輸入或選擇分支。");
      if (branches.includes(v)) return v;                       // 完整 ref（如自訂輸入）
      const hits = byDisplay.get(v);
      if (hits) return hits.find((b) => b.startsWith("refs/heads/")) || hits[0];   // 同名優先 local
      if (/^[0-9a-fA-F]{4,40}$/.test(v)) return v;               // commit SHA：由後端驗證能否檢出（detach）
      throw new Error("分支「" + v + "」不在清單中，也不是 commit SHA；請先「重新整理 Git」。");
    };
    const control = element("div", "branch-combo");
    control.append(input, toggle, list);
    label.append(element("span", "tb-field__label branch-label", "切換分支"), control);
    // 強制切換＝git checkout -f：丟棄未提交的追蹤檔案變更、取消進行中的 revert/
    // cherry-pick/merge（untracked 檔保留）。Git 的 sequencer 擋鎖只有這條路過得去。
    const forceBox = element("input");
    forceBox.type = "checkbox";
    const forceLabel = element("label", "check");
    forceLabel.append(forceBox, element("span", "", "強制（丟棄未提交變更，取消進行中的操作）"));
    const switchBtn = button("切換", async () => {
      let ref;
      try { ref = resolveBranch(); } catch (error) { announce(error.message, true); return; }
      const status = await runGit(row, "checkout", ref, false, forceBox.checked);
      if (!status) return;
      // 切分支後 Submodule 常停留在舊 commit：主動詢問是否更新（使用者選擇，不預設跑）。
      // hasSubmodules===false 表示查過沒有子模組，不必打擾；查不到（undefined）仍詢問。
      if (status.hasSubmodules !== false &&
          window.confirm("分支已切換。是否更新 Submodule（git submodule update --init --recursive）？")) {
        await runGit(row, "submodule", null, true);   // 上面的詢問就是確認，不再重複 confirm
      }
    });
    // Enter：清單開著且有高亮項時先選取它，再觸發切換；isComposing 過濾中文輸入法組字 Enter。
    listen(input, "keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      const picked = !list.hidden && active >= 0 ? items()[active]?.dataset.value : null;
      if (picked) input.value = picked;
      closeMenu();
      switchBtn.click();
    });
    card.append(label, forceLabel, switchBtn);
  } else if (row.gitCommonDir) {
    card.append(element("p", "tb-field__hint", "重新整理 Git 可列出可切換的分支。"));
  }
  return card;
}

async function runGit(row, action, branch, preConfirmed = false, force = false) {
  const label = action === "pull" ? "Pull ff-only" : action === "submodule" ? "Submodule update" : "切換分支";
  if (!preConfirmed && action !== "refresh") {
    let question = "對「" + row.name + "」執行 " + label + "？";
    if (action === "checkout" && force) {
      question = "強制切換「" + row.name + "」：未提交的追蹤檔案變更將永久丟棄，進行中的 revert／cherry-pick／rebase 會被取消（untracked 檔保留）。確定？";
    } else if (action === "checkout" && row.gitOperation) {
      // sequencer 狀態（revert/cherry-pick/rebase）Git 必然拒絕切換；bisect 或
      // 衝突已解完的 merge 則可能直接成功，語氣分開避免誤導。
      const blocking = ["revert", "cherry-pick", "rebase"].includes(row.gitOperation);
      question += "（偵測到" + (gitOperationLabels[row.gitOperation] || row.gitOperation) +
                  (blocking ? "，不勾強制會被 Git 擋下" : "，Git 可能拒絕切換") +
                  "；建議先在終端機結束該操作以保留變更，或勾選「強制」。）";
    }
    if (!window.confirm(question)) return false;
  }
  announce("Git 工作處理中…", false, true);
  try {
    const { result } = await api("/workspaces/" + row.id + "/git", "POST",
                                 { action, branch, confirm: action !== "refresh", force });
    let status = result;
    while (status.state === "running") {
      await new Promise((resolve) => setTimeout(resolve, 350));
      status = (await api("/git-tasks/" + status.id)).result;
    }
    if (status.state !== "success") throw new Error(status.error?.message || "Git 工作失敗。");
    gitDetails.set(row.id, status.branches);
    await refreshWorkspaces();
    announce("Git 狀態已更新。");
    return status;
  } catch (error) {
    announce(error.message, true);
    return false;
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
  $("build-workspaces").replaceChildren(...(workspaces.length ? workspaces.map((row) =>
    workspaceChoice(row, "build", "check build-workspace-card")) : [
    empty("請先在 Workspace 分頁登錄資料夾。"),
  ]));
  // 目標候選：merge 各 Workspace 已刷新的分支短名（去重）；SHA 直接打字即可
  const list = $("build-target-list");
  const seen = new Set();
  list.replaceChildren();
  for (const refs of gitDetails.values()) {
    for (const ref of refs || []) {
      const display = ref.replace(/^refs\/(?:heads|remotes)\//, "");
      if (!seen.has(display)) {
        seen.add(display);
        list.append(new Option(display, display));
      }
    }
  }
}

async function loadRecipes() {
  await loadConfiguration();
  selectRecords("recipe", configuration.recipes, "請選擇 Recipe");
  renderBuildParameters();
}

function buildInputs() {
  const body = { workspaceIds: [...selected], recipe: $("recipe").value,
    ...buildParameterPayload() };
  const opts = { submodule: $("build-submodule").checked, fetch: $("build-fetch").checked,
                 force: $("build-force").checked };
  if (buildQueue.length) {
    // Ordered multi-target queue: array order is the build order per workspace.
    body.checkouts = buildQueue.map((item) => ({ target: item.target, ...opts }));
  } else {
    const target = $("build-target").value.trim();
    if (target) body.checkout = { target, ...opts };
  }
  if (!body.workspaceIds.length || !body.recipe) throw new Error("請選擇 Workspace 與 Recipe。");
  return body;
}

async function checkBuild(body) {
  $("preview").replaceChildren();
  try {
    const { result, warnings } = await api("/jobs/check", "POST", body);
    $("preview-hint").textContent = warnings.length ? "請確認下列警告再開始 Build。" : "檢查完成，未發現衝突。";
    const checkout = (body.checkouts && body.checkouts[0]) || body.checkout || null;
    if (body.checkouts && body.checkouts.length > 1) {
      $("preview").append(element("p", "tb-muted", "Build 佇列：" + body.checkouts.map((item) => item.target).join(" → ") +
        "（每個 Workspace 依序建置）"));
    }
    for (const plan of result) {
      $("preview").append(element("strong", "", plan.workspace));
      if (plan.checkoutTarget) {
        $("preview").append(element("p", "tb-muted", "Build 前會先 Checkout 到：" + plan.checkoutTarget +
          (checkout && checkout.submodule ? "（並更新 Submodule）" : "") +
          (checkout && checkout.force ? "（強制：未提交變更將被丟棄）" : "")));
      }
      for (const argv of plan.argvPreview) $("preview").append(element("pre", "preview-command", JSON.stringify(argv)));
    }
    for (const warning of warnings) {
      const text = warning.code === "SHARED_GIT_DIR" ? "有 Workspaces 共用同一 Git repo。"
        : warning.code === "QUEUE_SAME_WORKSPACE" ? "同一 Workspace 已有 Job 在執行或排隊，新 Job 會自動排在後面依序執行。"
        : "有 Job 使用相同的資料夾。";
      $("preview").append(element("p", "preview-warning", text));
    }
    buildChecked = JSON.stringify(body);
    return warnings;
  } catch (error) {
    buildChecked = "";
    $("preview-hint").textContent = "檢查未通過。";
    const text = error.message;
    const warning = element("p", "preview-warning", text);
    warning.dataset.error = "";
    $("preview").append(warning);
    throw new Error(text);
  }
}

listen($("check-build"), "click", () => checkBuild(buildInputs()));

// ----- Build 佇列：勾選多個 branch／commit，可調順序後依序建置 -----
let buildQueue = [];          // [{ target, label }] 使用者排列順序＝建置順序
let pickWorkspaceId = null;   // 對話框查詢用的 Workspace（多選 Workspace 時取第一個）

const queueEntry = (target) => buildQueue.find((item) => item.target === target);

function toggleQueue(target, label) {
  const index = buildQueue.findIndex((item) => item.target === target);
  if (index >= 0) buildQueue.splice(index, 1);
  else if (buildQueue.length >= 30) { announce("Build 佇列最多 30 個目標。", true); return; }
  else buildQueue.push({ target, label });
  renderQueues();
}

function moveInQueue(from, to) {
  if (to < 0 || to >= buildQueue.length) return;
  const [item] = buildQueue.splice(from, 1);
  buildQueue.splice(to, 0, item);
  renderQueues();
}

function renderQueueList(list) {
  list.replaceChildren();
  list.hidden = !buildQueue.length;
  buildQueue.forEach((item, index) => {
    const line = element("li", "commit-queue-row");
    line.append(element("span", "commit-queue-label", (index + 1) + ". " + item.label),
                button("↑", () => moveInQueue(index, index - 1), "tb-btn commit-queue-move"),
                button("↓", () => moveInQueue(index, index + 1), "tb-btn commit-queue-move"),
                button("移除", () => { buildQueue.splice(index, 1); renderQueues(); }, "tb-btn commit-queue-move"));
    list.append(line);
  });
}

function renderQueues() {
  renderQueueList($("commit-pick-queue"));
  renderQueueList($("build-queue"));
  $("commit-pick-clear").hidden = !buildQueue.length;
  $("build-picked").textContent = buildQueue.length ? "已排入 " + buildQueue.length + " 個目標，將依序建置。" : "";
  for (const box of document.querySelectorAll("#branch-table input, #commit-pick-list input")) {
    box.checked = Boolean(queueEntry(box.dataset.target));
  }
}

// ----- Branch 表格（仿 TortoiseGit 選 log 對話框）：左樹右表，可勾選入佇列 -----
let pickBranches = [];
let pickGroup = "all";
let pickRef = "";
let pickSeq = 0;
let openNodes = new Set();     // 展開中的樹節點 path
let targetOld = "";            // 編輯 Build 目標前的值（取消時回退）

const pickFail = (error) => announce(error.message, true);

// 每個 branch 的完整樹路徑：heads/<name> 或 remotes/<remote>/<name>。
const branchPath = (branch) => (branch.group === "heads" ? "heads/" : "remotes/") + branch.name;

// 樹含群組節點與葉分支（同 TortoiseGit）：依下一段名稱遞迴分組，最後段為葉。
function makeNode(prefix, list, label) {
  const node = { label, path: prefix, count: list.length, children: new Map(), leaves: [] };
  const buckets = new Map();
  for (const branch of list) {
    const rest = branchPath(branch).slice(prefix.length + 1);
    const cut = rest.indexOf("/");
    if (cut < 0) { node.leaves.push(branch); continue; }
    const seg = rest.slice(0, cut);
    if (!buckets.has(seg)) buckets.set(seg, []);
    buckets.get(seg).push(branch);
  }
  for (const [seg, sub] of [...buckets.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    node.children.set(seg, makeNode(prefix + "/" + seg, sub, seg));
  }
  node.leaves.sort((a, b) => a.name.localeCompare(b.name));
  return node;
}

function inGroup(branch) {
  if (pickGroup === "all") return true;
  const path = branchPath(branch);
  return path === pickGroup || path.startsWith(pickGroup + "/");
}

// Filter 字串同時縮減樹與表格（TortoiseGit 行為：不符合的分支不出現在樹上）。
function filteredBranches() {
  const keyword = $("commit-filter").value.trim().toLowerCase();
  if (!keyword) return pickBranches;
  return pickBranches.filter((branch) => (branch.name + " " + branch.subject + " " + branch.author + " " + branch.sha)
      .toLowerCase().includes(keyword));
}

async function loadPick(ref) {
  const seq = ++pickSeq;
  const params = new URLSearchParams({ limit: "100" });
  if (ref) params.set("ref", ref);
  const { result } = await api("/workspaces/" + pickWorkspaceId + "/commits?" + params);
  if (seq !== pickSeq) return;    // 使用者已點別的分支，丟棄遲到的舊回應
  pickRef = ref;
  $("commit-ref-hint").textContent = result.resolved + " 的 commits（最新 " + result.commits.length +
    " 筆）：勾選 Commit 加入 Build 佇列";
  const list = $("commit-pick-list");
  list.replaceChildren();
  for (const commit of result.commits) {
    const line = element("label", "commit-row");
    const box = element("input");
    box.type = "checkbox";
    box.dataset.target = commit.sha;
    box.checked = Boolean(queueEntry(commit.sha));
    listen(box, "change", () => toggleQueue(commit.sha, commit.short + " · " + commit.subject));
    line.append(box, element("code", "commit-sha", commit.short), element("span", "commit-date", commit.date),
                element("span", "commit-author", commit.author), element("span", "commit-subject", commit.subject));
    list.append(line);
  }
  renderBranchTable();
}

async function loadBranches() {
  const { result } = await api("/workspaces/" + pickWorkspaceId + "/branches");
  if (result.error) announce("無法讀取分支清單：" + result.error, true);
  pickBranches = result.branches;
  // 預設展開 All／heads／remotes／各 remote 一層，更深的群組手動展開。
  openNodes = new Set(["all", "heads", "remotes"]);
  for (const branch of pickBranches) if (branch.remote) openNodes.add("remotes/" + branch.remote);
  renderTree();
  renderBranchTable();
}

function renderTree() {
  const tree = $("ref-tree");
  tree.replaceChildren();
  const list = filteredBranches();
  const makeRow = (depth, active) => {
    const row = element("div", "ref-node-row" + (active ? " is-active" : ""));
    row.style.paddingInlineStart = depth * 16 + "px";
    tree.append(row);
    return row;
  };
  const leafRow = (branch, depth) => {
    const path = branchPath(branch);
    const row = makeRow(depth, pickGroup === path);
    const btn = button(branch.name.slice(branch.name.lastIndexOf("/") + 1), () => {
      pickGroup = path;
      renderTree();
      renderBranchTable();
      loadPick(branch.name).catch(pickFail);
    }, "ref-node ref-leaf");
    btn.title = branch.ref;
    row.append(element("span", "ref-arrow"), btn);
  };
  const nodeRow = (node, depth) => {
    const row = makeRow(depth, pickGroup === node.path);
    const arrow = element("button", "ref-arrow");
    arrow.type = "button";
    const open = openNodes.has(node.path);
    const childCount = node.children.size + node.leaves.length;
    arrow.textContent = childCount ? (open ? "▾" : "▸") : "";
    if (childCount) {
      listen(arrow, "click", () => {
        if (openNodes.has(node.path)) openNodes.delete(node.path); else openNodes.add(node.path);
        renderTree();
      });
    }
    row.append(arrow, button(node.label + " (" + node.count + ")", () => {
      pickGroup = node.path;
      if (childCount) openNodes.add(node.path);
      renderTree();
      renderBranchTable();
    }, "ref-node"));
    if (!open) return;
    const items = [
      ...[...node.children.values()].map((child) => ({ key: child.label, child })),
      ...node.leaves.map((branch) => ({ key: branch.name.slice(branch.name.lastIndexOf("/") + 1), branch })),
    ].sort((a, b) => a.key.localeCompare(b.key));
    // 單一群組可能有數千分支，節點只渲染前 200 項，其餘交給右側表格。
    for (const item of items.slice(0, 200)) {
      if (item.child) nodeRow(item.child, depth + 1); else leafRow(item.branch, depth + 1);
    }
    if (items.length > 200) {
      makeRow(depth + 1, false).append(element("span", "ref-arrow"), button(
        "⋯ 另有 " + (items.length - 200) + " 項（點此在表格檢視）",
        () => { pickGroup = node.path; renderTree(); renderBranchTable(); }, "ref-node ref-more"));
    }
  };
  nodeRow({ label: "All", path: "all", count: list.length, children: new Map(), leaves: [] }, 0);
  const heads = list.filter((branch) => branch.group === "heads");
  const remotes = list.filter((branch) => branch.group.startsWith("remotes/"));
  if (heads.length) nodeRow(makeNode("heads", heads, "heads"), 0);
  if (remotes.length) nodeRow(makeNode("remotes", remotes, "remotes"), 0);
}

function renderBranchTable() {
  const table = $("branch-table");
  table.replaceChildren();
  const head = element("div", "branch-row branch-head");
  for (const title of ["", "Branch Name", "Date Last Commit", "Last Author Date", "Last Commit"]) {
    head.append(element("span", "", title));
  }
  table.append(head);
  const matched = filteredBranches().filter(inGroup);
  for (const branch of matched.slice(0, 300)) {
    const row = element("div", "branch-row" + (branch.name === pickRef ? " is-current" : ""));
    row.title = branch.ref;
    const box = element("input");
    box.type = "checkbox";
    box.dataset.target = branch.ref;      // 完整 ref：local 字面名 origin/x 時才不會與遠端撞 key
    box.checked = Boolean(queueEntry(branch.ref));
    listen(box, "change", () => toggleQueue(branch.ref, "Branch " + branch.name));
    row.append(box, element("span", "branch-name", branch.name), element("span", "", branch.date),
               element("span", "", branch.authorDate),
               element("span", "", branch.subject + " (" + branch.sha + ")"));
    // 點列＝下方 commit 清單切到該分支；勾 checkbox 才是加入 Build 佇列。
    listen(row, "click", (event) => {
      if (event.target === box) return;
      loadPick(branch.name).catch(pickFail);
    });
    table.append(row);
  }
  if (matched.length > 300) {
    table.append(element("p", "tb-small tb-muted", "…另有 " + (matched.length - 300) +
                         " 個分支未顯示，請用 Filter 或選左側群組縮小範圍。"));
  }
}

listen($("build-pick"), "click", async () => {
  const row = workspaces.find((item) => item.id === [...selected][0]);
  if (!row) throw new Error("請先選擇 Workspace。");
  pickWorkspaceId = row.id;
  pickGroup = "all";
  $("commit-filter").value = "";
  $("commit-pick-hint").textContent = "來源：" + row.name + "；勾選 Branch 或 Commit 加入 Build 佇列，可調整順序" +
    (selected.size > 1 ? "；多選 Workspace 時僅依第一個 Workspace 查詢" : "");
  $("commit-pick").showModal();
  try {
    await loadBranches();
  } catch (error) {
    pickFail(error);
  }
  try {
    await loadPick($("build-target").value.trim() || row.branch || "");
  } catch {
    try {
      await loadPick("");
    } catch (error) {
      pickFail(error);
    }
  }
  renderQueues();
});
listen($("commit-filter"), "input", () => { renderTree(); renderBranchTable(); });
listen($("commit-pick-clear"), "click", () => { buildQueue = []; renderQueues(); });
listen($("commit-pick-close"), "click", () => $("commit-pick").close());
// 勾「強制切樹」前必須確認：git switch -f 會丟棄未提交的追蹤檔案變更。
listen($("build-force"), "change", () => {
  if ($("build-force").checked && !window.confirm(
      "強制切樹會以 git switch -f 切換，工作區中「未提交的追蹤檔案變更」將被丟棄且無法復原"
    + "（untracked 檔案保留）。\n"
    + "若選的是遠端分支且本機已有同名分支，本機分支會被重置到遠端最新（多出的本機 commit 可經 reflog 找回）。\n\n"
    + "確定允許 Runner 在建置前強制切換？")) {
    $("build-force").checked = false;
  }
});
// 手動修改 Build 目標代表改走單筆模式；確認後才清佇列，避免誤觸輸入就把排好的目標全丟。
listen($("build-target"), "beforeinput", () => { targetOld = $("build-target").value; });
listen($("build-target"), "input", () => {
  if (!buildQueue.length) return;
  if (!window.confirm("手動編輯 Build 目標會清空已排的 " + buildQueue.length + " 個佇列目標，繼續？")) {
    $("build-target").value = targetOld;
    return;
  }
  buildQueue = [];
  renderQueues();
});
listen($("build-form"), "submit", async () => {
  const body = buildInputs();
  if (JSON.stringify(body) !== buildChecked) {
    const warnings = await checkBuild(body);
    // QUEUE_SAME_WORKSPACE just means the new job lines up behind the active
    // ones on the same folder; that is the point of the queue, so never block.
    if (warnings.some((warning) => warning.code !== "QUEUE_SAME_WORKSPACE")) return;
  }
  const { result } = await api("/jobs", "POST", body);
  $("build-target").value = "";
  $("build-fetch").checked = false;
  $("build-force").checked = false;
  buildQueue = [];
  renderQueues();
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
    const rootText = row.state === "success" ? "—" : row.rootError?.text || "—";
    const root = element("span", "history-root", rootText);
    if (rootText !== "—") root.title = rootText;
    record.insertCell().append(root);
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
  updateLogJobControls();
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

listen($("log-stop-job"), "click", () => controlJob(jobs.find((row) => row.id === log.id), "stop"));
listen($("log-restart-job"), "click", () => controlJob(jobs.find((row) => row.id === log.id), "restart"));
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
  const root = report.rootError;
  const key = JSON.stringify([!!report.lineCount, report.result, report.cascadeCount, root?.line, root?.text]);
  if (key === view.triageKey) return;
  view.triageKey = key;
  const box = $("triage");
  box.hidden = !report.lineCount;
  const heading = element("span", "triage-summary");
  heading.append(element("strong", "", "Log 判讀"), badge(report.result));
  if (report.cascadeCount) heading.append(element("span", "tb-small tb-muted", "連鎖錯誤 " + report.cascadeCount + " 筆"));
  if (!root) {
    heading.append(element("span", "tb-small tb-muted", "目前未辨識到根本錯誤。"));
    box.replaceChildren(heading);
    return;
  }
  heading.append(element("span", "tb-small tb-muted", "根本錯誤 · 第 " + root.line + " 行"));
  const summary = element("summary");
  summary.append(heading);
  const details = element("details");
  details.open = box.querySelector("details")?.open === true;
  details.append(summary, button("跳到第 " + root.line + " 行 · " + root.text, () => jumpTo(root.line)));
  box.replaceChildren(details);
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
    if (page === "schedules") await loadSchedulesPage();
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

const themeButton = $("theme-toggle");
const themeLabels = { light: "淺色", dark: "深色", system: "跟隨系統" };
const themeIcons = { light: "sun", dark: "moon", system: "monitor" };
function renderThemeButton() {
  const { mode } = TbTheme.get();
  themeButton.innerHTML = TbIcons.svg(themeIcons[mode], { anim: "wiggle" });
  themeButton.title = "主題：" + themeLabels[mode] + "（點一下切換）";
  themeButton.setAttribute("aria-label", themeButton.title);
}
document.addEventListener("tb-theme-change", renderThemeButton);
themeButton.addEventListener("click", () => {
  const { modes } = TbTheme;
  TbTheme.set(modes[(modes.indexOf(TbTheme.get().mode) + 1) % modes.length]);
});
renderThemeButton();

async function pollServerState() {
  const badge = $("server-state");
  let label = "伺服器未回應";
  let tone = "danger";
  try {
    const health = await api("/health");
    if (health.ok) {
      $("server-port").textContent = "本機服務 port " + health.port;
      label = "連線正常";
      tone = "ok";
    }
  } catch (_) { /* 下次輪詢重新連線 */ }
  if (badge.textContent !== label) badge.textContent = label;
  badge.className = "tb-badge tb-badge--" + tone;
  setTimeout(pollServerState, 3000);
}

window.addEventListener("hashchange", navigate);
document.addEventListener("DOMContentLoaded", () => {
  initializeOperations();
  navigate();
  if (!document.documentElement.dataset.embedded) pollServerState();
  setTimeout(tick, 1000);
});
