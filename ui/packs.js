"use strict";

// 設定包（.brpack.json）匯出／匯入：流程設定頁右上角兩顆按鈕。
const packSections = [
  ["recipes", "Recipe"], ["paramsets", "參數表"], ["presets", "Preset"], ["artifactRules", "產出物規則"],
];
const packActions = { overwrite: "覆蓋", rename: "改名", skip: "略過" };
let packContent = "";
let packPlan = null;

function packWarnings(box, warnings) {
  box.replaceChildren(...(warnings || []).map((row) => element("p", "tb-small pack-warning", "⚠ " + row.message)));
}

function closePackDialog(id) {
  $(id).close();
}

for (const trigger of document.querySelectorAll("[data-close]")) {
  trigger.addEventListener("click", () => closePackDialog(trigger.dataset.close));
}

// ---------- 匯出 ----------
listen($("pack-export-open"), "click", async () => {
  const { result } = await api("/packs/inventory");
  const box = $("pack-export-items");
  box.replaceChildren();
  let total = 0;
  for (const [key, label] of packSections) {
    const names = result[key] || [];
    total += names.length;
    const group = element("fieldset", "pack-group");
    group.append(element("legend", "", label + "（" + names.length + "）"));
    if (!names.length) group.append(element("p", "tb-small tb-muted", "沒有已儲存的項目。"));
    for (const name of names) {
      const row = element("label", "check");
      const box = element("input");
      box.type = "checkbox";
      box.value = name;
      box.dataset.section = key;
      box.addEventListener("change", syncPackAll);
      row.append(box, element("span", "", name));
      group.append(row);
    }
    box.append(group);
  }
  if (!total) box.append(element("p", "tb-muted", "目前沒有自訂的 Recipe、參數表或產出物規則（內建項目不需要匯出）。"));
  $("pack-all").checked = false;
  $("pack-export-warnings").replaceChildren();
  $("pack-export").showModal();
});

function packChecks() {
  return [...$("pack-export-items").querySelectorAll("input[type=checkbox]")];
}

function syncPackAll() {
  const checks = packChecks();
  $("pack-all").checked = checks.length > 0 && checks.every((box) => box.checked);
}

$("pack-all").addEventListener("change", () => {
  for (const box of packChecks()) box.checked = $("pack-all").checked;
});

listen($("pack-export-form"), "submit", async () => {
  const body = { name: $("pack-name").value.trim(), description: $("pack-description").value.trim() };
  for (const [key] of packSections) body[key] = [];
  for (const box of packChecks()) if (box.checked) body[box.dataset.section].push(box.value);
  if (!packSections.some(([key]) => body[key].length)) throw new Error("請至少勾選一個項目。");
  const { result, warnings } = await api("/packs/export", "POST", body);
  const url = URL.createObjectURL(new Blob([result.content], { type: "application/json" }));
  const link = element("a");
  link.href = url;
  link.download = result.filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  packWarnings($("pack-export-warnings"), warnings);
  if (!warnings?.length) closePackDialog("pack-export");
  announce("已匯出設定包 " + result.filename + (warnings?.length ? "（有 " + warnings.length + " 則提醒）" : "。"), false);
});

// ---------- 匯入 ----------
function resetPackImport() {
  packContent = "";
  packPlan = null;
  $("pack-file").value = "";
  for (const id of ["pack-import-summary", "pack-import-items", "pack-import-warnings"]) $(id).replaceChildren();
  $("pack-import-confirm").disabled = true;
}

listen($("pack-import-open"), "click", () => {
  resetPackImport();
  $("pack-import").showModal();
});

function packDecisions() {
  const decisions = {};
  for (const select of $("pack-import-items").querySelectorAll("select[data-key]")) {
    if (select.value) decisions[select.dataset.key] = select.value;
  }
  return decisions;
}

const actionLabels = { create: "新增", overwrite: "覆蓋", rename: "改名", skip: "略過", "missing-table": "略過（缺參數表）" };

function renderPackPlan(plan) {
  const box = $("pack-import-items");
  box.replaceChildren();
  for (const [key, label] of packSections) {
    const rows = plan.items.filter((item) => item.section === key);
    if (!rows.length) continue;
    const group = element("fieldset", "pack-group");
    group.append(element("legend", "", label + "（" + rows.length + "）"));
    for (const item of rows) {
      const row = element("div", "pack-row");
      row.append(element("code", "", item.name));
      if (item.conflict) {
        const select = element("select", "tb-select");
        select.dataset.key = item.section + ":" + item.name;
        select.setAttribute("aria-label", item.name + " 名稱重複時的處理方式");
        select.append(new Option("請選擇…", ""), ...Object.entries(packActions).map(([value, text]) => new Option(text, value)));
        select.value = item.action in packActions ? item.action : "";
        select.addEventListener("change", () => refreshPackPlan().catch((error) => announce(error.message, true)));
        row.append(element("span", "tb-badge tb-badge--warn", "名稱重複"), select);
      } else {
        row.append(element("span", "tb-badge" + (item.action === "missing-table" ? " tb-badge--danger" : " tb-badge--ok"),
          actionLabels[item.action] || item.action));
      }
      if (item.target && item.target !== item.name && item.action === "rename") row.append(element("span", "tb-small tb-muted", "→ " + item.target));
      group.append(row);
    }
    box.append(group);
  }
  $("pack-import-confirm").disabled = plan.unresolved.length > 0 || !plan.items.some((item) => ["create", "overwrite", "rename"].includes(item.action));
}

async function refreshPackPlan() {
  const { result, warnings } = await api("/packs/preview", "POST", { content: packContent, decisions: packDecisions() });
  packPlan = result;
  const meta = result.pack;
  const summary = [meta.name || "（未命名）", meta.description, meta.tool?.version ? "Build Runner " + meta.tool.version : "",
    meta.createdAt ? "建立於 " + meta.createdAt : ""].filter(Boolean).join(" · ");
  $("pack-import-summary").replaceChildren(element("p", "tb-small", summary));
  renderPackPlan(result);
  packWarnings($("pack-import-warnings"), warnings);
}

listen($("pack-file"), "change", async () => {
  const file = $("pack-file").files[0];
  packPlan = null;
  $("pack-import-confirm").disabled = true;
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("設定包超過 1 MB，請改用 CLI 的 import-pack。");
  packContent = await file.text();
  for (const id of ["pack-import-items", "pack-import-warnings"]) $(id).replaceChildren();
  await refreshPackPlan();
});

listen($("pack-import-form"), "submit", async () => {
  if (!packPlan) return;
  const { result } = await api("/packs/import", "POST", { content: packContent, decisions: packDecisions(), confirm: true });
  closePackDialog("pack-import");
  await loadConfigurationEditor({});
  announce("已匯入 " + result.written.length + " 個檔案" + (result.backups ? "，覆蓋前的原檔已備份。" : "。"));
});
