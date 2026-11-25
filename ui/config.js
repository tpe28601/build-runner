"use strict";

let configuration = { recipes: [], tables: [], artifacts: [] };
let buildRecipeId = null;
let buildOverrides = Object.create(null);
let recipeDraft;
let recipeTemplateCommand;
let artifactDraft;
let artifactPreview = null;
let artifactDirty = false;
const builtinBiosArtifactId = "@builtin:bios-rom-package";

async function loadConfiguration() {
  const [recipes, tables, artifacts] = await Promise.all(
    ["/recipes", "/paramsets", "/artifact-rules"].map((path) => api(path)),
  );
  configuration = { recipes: recipes.result, tables: tables.result, artifacts: artifacts.result };
}

function recordById(records, id) {
  return records.find((record) => record.id === id);
}

function copyName(source, records) {
  const label = source.name.replace(/[^\p{L}\p{N}_ .-]/gu, "").replace(/ {2,}/g, " ").replace(/^[ .-]+/, "").trimEnd();
  const stem = Array.from(label || "新項目");
  const used = new Set(records.flatMap((record) => [record.id.toLowerCase(), record.name.toLowerCase()]));
  for (let number = 1; ; number++) {
    const suffix = number === 1 ? " 複本" : " 複本 " + number;
    const name = stem.slice(0, 128 - suffix.length).join("").trimEnd() + suffix;
    if (!used.has(name.toLowerCase())) return name;
  }
}

function selectRecords(id, records, placeholder, value = $(id).value, keepMissing = false) {
  const select = $(id);
  select.replaceChildren(new Option(placeholder, ""), ...records.map((record) =>
    new Option(record.name + (record.builtIn ? "（內建）" : ""), record.id)));
  if (value && keepMissing && !recordById(records, value)) select.append(new Option(value + "（找不到）", value));
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
}

function fillPresets(select, table, placeholder, value) {
  select.replaceChildren(new Option(placeholder, ""), ...Object.keys(table?.presets || {}).map((name) => new Option(name, name)));
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
}

function parameterNotes(notes) {
  const node = element("details", "editor-notes");
  node.append(element("summary", "", "使用限制備註"));
  const list = element("ul");
  for (const note of notes) list.append(element("li", "", note));
  node.append(list);
  return node;
}

function activeBuildRecipe() {
  return recordById(configuration.recipes, $("recipe").value);
}

function activeBuildTable(recipe = activeBuildRecipe()) {
  return recipe?.steps?.some((step) => step.enabled !== false && step.useParams)
    ? recordById(configuration.tables, recipe.paramset) : null;
}

function effectiveValues(table, preset, overrides) {
  const values = Object.create(null);
  for (const param of table.params) values[param.key] = param.default ?? (param.type === "switch" ? false : "");
  return Object.assign(values, preset ? table.presets?.[preset] : {}, overrides);
}

function selectedBuildPreset() {
  return $("build-preset").value;
}

function fieldPreset(recipe) {
  return selectedBuildPreset() || [...recipe.steps].reverse().find((step) => step.enabled !== false && step.useParams)?.preset || "";
}

function buildParameterPayload() {
  if (!activeBuildTable()) return {};
  const preset = selectedBuildPreset();
  return { ...(preset ? { preset } : {}), params: { ...buildOverrides } };
}

function renderBuildParameters() {
  const recipe = activeBuildRecipe();
  const table = activeBuildTable(recipe);
  if (buildRecipeId !== $("recipe").value) {
    buildRecipeId = $("recipe").value;
    buildOverrides = Object.create(null);
    $("build-preset").value = "";
    $("parameter-search").value = "";
    buildChecked = "";
  }
  $("build-parameters").hidden = !table;
  if (table) {
    fillPresets($("build-preset"), table, "依 Recipe 設定", selectedBuildPreset());
    $("build-parameter-notes").replaceChildren(...(table.notes?.length ? [parameterNotes(table.notes)] : []));
    renderParameterGroups();
  }
  renderBuildPreview();
}

function renderParameterGroups() {
  const recipe = activeBuildRecipe();
  const table = activeBuildTable(recipe);
  if (!table) return;
  const values = effectiveValues(table, fieldPreset(recipe), buildOverrides);
  const query = $("parameter-search").value.trim().toLowerCase();
  const groups = new Map();
  let visible = 0;
  for (const param of table.params) {
    if (query && !(param.flag + " " + param.desc).toLowerCase().includes(query)) continue;
    visible++;
    if (!groups.has(param.group)) {
      const group = element("div", "parameter-group stack");
      group.append(element("h3", "", param.group));
      groups.set(param.group, group);
    }
    const label = element("label", param.type === "switch" ? "check parameter-switch" : "tb-field");
    const name = element("code", "parameter-flag", param.flag);
    let control;
    if (param.type === "choice") {
      control = element("select", "tb-select");
      control.append(new Option("不使用", ""), ...param.choices.map((value) => new Option(value, value)));
      control.value = values[param.key];
    } else {
      control = element("input", param.type === "value" ? "tb-input tb-input--mono" : "");
      if (param.type === "switch") {
        control.type = "checkbox";
        control.checked = !!values[param.key];
      } else {
        control.value = values[param.key];
        control.placeholder = param.placeholder || "";
      }
    }
    const description = element("span", "tb-small tb-muted parameter-description", param.desc);
    description.id = "parameter-description-" + visible;
    control.setAttribute("aria-label", param.flag);
    if (param.desc) control.setAttribute("aria-describedby", description.id);
    if (param.type === "switch") {
      const content = element("span", "parameter-copy");
      content.append(name, description);
      label.append(control, content);
    } else {
      label.append(name, control, description);
    }
    control.addEventListener("input", () => {
      buildOverrides[param.key] = param.type === "switch" ? control.checked : control.value;
      buildChecked = "";
      renderBuildPreview();
    });
    groups.get(param.group).append(label);
  }
  $("parameter-count").textContent = "顯示 " + visible + " / " + table.params.length + " 項";
  $("parameter-groups").replaceChildren(...(visible ? [...groups.values()] : [empty("沒有符合搜尋條件的參數。")]));
}

function renderBuildPreview() {
  const recipe = activeBuildRecipe();
  const preview = $("preview");
  preview.replaceChildren();
  $("preview-hint").textContent = recipe ? "修改參數會即時更新。開始前可檢查 Workspace 衝突。" : "先選擇 Recipe，這裡會顯示實際參數陣列。";
  if (!recipe) return;
  const table = activeBuildTable(recipe);
  const conflicts = new Set();
  if (!table && recipe.paramset && recipe.steps?.some((step) => step.enabled !== false && step.useParams)) {
    conflicts.add("找不到這個 Recipe 的參數表。");
  }
  for (const [index, step] of (recipe.steps || []).entries()) {
    if (step.enabled === false) continue;
    const argv = [...(step.argv || [])];
    if (table && step.useParams) {
      const preset = selectedBuildPreset() || step.preset;
      if (preset && !Object.hasOwn(table.presets || {}, preset)) conflicts.add("找不到 preset " + preset + "。");
      const values = effectiveValues(table, preset, buildOverrides);
      for (const group of table.conflicts || []) {
        const selected = group.filter((key) => values[key]);
        if (selected.length > 1) {
          const flags = selected.map((key) => table.params.find((param) => param.key === key)?.flag || key);
          conflicts.add("互斥參數 · " + flags.join(" / "));
        }
      }
      for (const param of table.params) {
        if (!values[param.key]) continue;
        argv.push(param.flag);
        if (param.type !== "switch") argv.push(values[param.key]);
      }
    }
    preview.append(element("strong", "", step.name || "步驟 " + (index + 1)),
      element("pre", "preview-command", JSON.stringify(argv)));
  }
  for (const conflict of conflicts) {
    const warning = element("p", "preview-warning", conflict);
    warning.dataset.error = "";
    warning.setAttribute("role", "alert");
    preview.prepend(warning);
  }
}

$("recipe").addEventListener("change", renderBuildParameters);
$("parameter-search").addEventListener("input", renderParameterGroups);
$("build-preset").addEventListener("change", () => {
  buildOverrides = Object.create(null);
  buildChecked = "";
  renderParameterGroups();
  renderBuildPreview();
});

listen($("save-preset"), "click", async () => {
  const recipe = activeBuildRecipe();
  const table = activeBuildTable(recipe);
  if (!table) return;
  const preset = window.prompt("Preset 名稱", selectedBuildPreset())?.trim();
  if (!preset) return;
  const confirm = Object.hasOwn(table.presets || {}, preset);
  if (confirm && !window.confirm("覆寫 preset「" + preset + "」？")) return;
  await api("/paramsets/presets", "POST", {
    name: table.id, preset, values: effectiveValues(table, fieldPreset(recipe), buildOverrides), confirm,
  });
  await loadConfiguration();
  fillPresets($("build-preset"), activeBuildTable(), "依 Recipe 設定", preset);
  buildOverrides = Object.create(null);
  buildChecked = "";
  renderBuildParameters();
  announce("已儲存 preset。");
});

function editorInput(title, value, update, options = {}) {
  const label = element("label", "tb-field");
  const caption = element("span", "tb-field__label", title);
  const input = element(options.rows ? "textarea" : "input", options.rows ? "tb-textarea tb-input--mono" : "tb-input");
  if (options.rows) input.rows = options.rows;
  input.value = value ?? "";
  input.addEventListener("input", () => update(input.value));
  label.append(caption);
  if (options.folder) {
    const row = element("span", "tb-input-row");
    const browse = button("", () => openFolderPicker(input, options.folder === "relative", options.anchor ? options.anchor() : null), "tb-btn tb-btn--icon");
    browse.title = "瀏覽" + title;
    browse.setAttribute("aria-label", browse.title);
    input.setAttribute("aria-label", title);
    browse.append(icon("folder-open"));
    row.append(input, browse);
    TbIcons.render(row);
    label.append(row);
  } else {
    label.append(input);
  }
  return label;
}

function editorCheck(title, value, update) {
  const label = element("label", "check");
  const input = element("input");
  input.type = "checkbox";
  input.checked = !!value;
  input.addEventListener("change", () => update(input.checked));
  label.append(input, element("span", "", title));
  return label;
}

function parseJson(value, label) {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error(label + " 必須是有效的 JSON。");
  }
}

function lineValues(value) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function visibleEditor(id) {
  for (const toggle of document.querySelectorAll("[data-config]")) {
    const on = toggle.dataset.config === id;
    $(toggle.dataset.config).hidden = !on;
    toggle.setAttribute("aria-pressed", String(on));
    toggle.classList.toggle("is-active", on);
  }
}

for (const toggle of document.querySelectorAll("[data-config]")) {
  toggle.addEventListener("click", () => visibleEditor(toggle.dataset.config));
}

function editStep(step) {
  return { ...step, argvText: JSON.stringify(step.argv || []), enabled: step.enabled !== false };
}

function defaultRecipe(name = "", source = null) {
  const basis = source || configuration.recipes.find((recipe) => recipe.builtIn);
  return {
    name, timeoutMin: basis?.timeoutMin ?? 180, env: source?.env || {}, steps: basis?.steps || [],
    paramset: source?.paramset, artifacts: source?.artifacts,
  };
}

function setRecipeDraft(record, { useStockCommand = !record.id } = {}) {
  recipeDraft = { ...record, steps: (record.steps || []).map(editStep) };
  $("recipe-form").hidden = !!record.builtIn;
  $("copy-recipe").disabled = !record.id;
  $("remove-recipe").disabled = !record.id || !!record.builtIn;
  const preview = $("builtin-recipe-preview");
  preview.hidden = !record.builtIn;
  if (record.builtIn) {
    const packaging = recordById(configuration.artifacts, record.artifacts);
    preview.replaceChildren(
      element("p", "tb-muted", "內建範例，先清理再建置。執行前請確認 Workspace 根目錄有 builda.bat。"),
      ...record.steps.flatMap((step) => [element("strong", "", step.name),
        element("pre", "preview-command", JSON.stringify(step.argv))]),
    );
    if (packaging) preview.append(
      element("strong", "", "Package"),
      element("p", "tb-muted", "建置成功後套用「" + packaging.name + "」。請先複製 Recipe，再依 Workspace 調整工作子資料夾及產出物規則。"),
    );
    return;
  }
  recipeTemplateCommand = useStockCommand ? "builda.bat" : null;
  $("recipe-name").value = record.name;
  $("recipe-timeout").value = record.timeoutMin ?? 180;
  $("recipe-env").value = JSON.stringify(record.env || {}, null, 2);
  selectRecords("recipe-table", configuration.tables, "不使用參數表", record.paramset || "", true);
  selectRecords("recipe-artifacts", configuration.artifacts, "不套用產出物規則", record.artifacts || "", true);
  renderRecipeSteps();
}

function renderRecipeSteps() {
  const table = recordById(configuration.tables, $("recipe-table").value);
  const rows = recipeDraft.steps.map((step, index) => {
    const item = element("fieldset", "editor-item stack");
    item.append(element("legend", "", "步驟 " + (index + 1)));
    const fields = element("div", "editor-fields");
    fields.append(
      editorInput("步驟名稱", step.name, (value) => { step.name = value; }),
      editorInput("工作子資料夾", step.cwd, (value) => { step.cwd = value; }, { folder: "relative" }),
    );
    const controls = element("div", "row");
    controls.append(
      editorCheck("啟用步驟", step.enabled, (value) => { step.enabled = value; }),
      editorCheck("使用 Build 參數", step.useParams, (value) => { step.useParams = value; renderRecipeSteps(); }),
      editorCheck("失敗後繼續", step.continueOnFail, (value) => { step.continueOnFail = value; }),
    );
    item.append(fields, editorInput("參數陣列（JSON）", step.argvText, (value) => { step.argvText = value; }, { rows: 2 }), controls);
    if (step.useParams && table) {
      const label = element("label", "tb-field");
      const select = element("select", "tb-select");
      fillPresets(select, table, "使用參數預設值", step.preset);
      if (step.preset && !select.value) {
        select.append(new Option(step.preset + "（找不到）", step.preset));
        select.value = step.preset;
      }
      select.addEventListener("change", () => { step.preset = select.value; });
      label.append(element("span", "tb-field__label", "步驟 preset"), select);
      item.append(label);
    }
    const actions = element("div", "row");
    for (const [title, offset] of [["上移", -1], ["下移", 1]]) {
      const move = button(title, () => {
        const next = index + offset;
        [recipeDraft.steps[index], recipeDraft.steps[next]] = [recipeDraft.steps[next], recipeDraft.steps[index]];
        renderRecipeSteps();
      });
      move.disabled = index + offset < 0 || index + offset >= recipeDraft.steps.length;
      actions.append(move);
    }
    actions.append(button("刪除步驟", () => {
      recipeDraft.steps.splice(index, 1);
      renderRecipeSteps();
    }));
    item.append(actions);
    return item;
  });
  $("recipe-steps").replaceChildren(...(rows.length ? rows : [empty("請新增至少一個步驟。")]));
}

$("edit-recipe").addEventListener("change", () => setRecipeDraft(recordById(configuration.recipes, $("edit-recipe").value) || defaultRecipe()));
listen($("new-recipe"), "click", () => {
  $("edit-recipe").value = "";
  setRecipeDraft(defaultRecipe());
  $("recipe-name").focus();
});
listen($("copy-recipe"), "click", () => {
  const source = recordById(configuration.recipes, $("edit-recipe").value);
  if (!source) return;
  $("edit-recipe").value = "";
  setRecipeDraft(defaultRecipe(copyName(source, configuration.recipes), structuredClone(source)),
    { useStockCommand: !!source.builtIn });
  $("recipe-name").select();
});
$("recipe-table").addEventListener("change", () => {
  const table = recordById(configuration.tables, $("recipe-table").value);
  if (recipeTemplateCommand) {
    const command = table?.command || "builda.bat";
    const stock = [[], ["-cleanall"], ["-cleanall", "-nou"], ["-r"], ["-r", "-nou"]];
    for (const step of recipeDraft.steps) {
      // Keep custom argv intact. Only replace the executable in the stock template.
      for (const suffix of stock) {
        if (step.argvText === JSON.stringify([recipeTemplateCommand, ...suffix])) {
          step.argvText = JSON.stringify([command, ...suffix]);
          break;
        }
      }
    }
    recipeTemplateCommand = command;
  }
  for (const step of recipeDraft.steps) if (step.preset && !Object.hasOwn(table?.presets || {}, step.preset)) step.preset = "";
  renderRecipeSteps();
});
listen($("add-step"), "click", () => {
  const table = recordById(configuration.tables, $("recipe-table").value);
  recipeDraft.steps.push(editStep({ name: "", argv: [table?.command || "builda.bat"] }));
  renderRecipeSteps();
});
listen($("remove-recipe"), "click", async () => {
  if (!recipeDraft.id || !window.confirm("刪除 Recipe「" + recipeDraft.name + "」？")) return;
  await api("/recipes", "DELETE", { name: recipeDraft.id, confirm: true });
  $("edit-recipe").value = "";
  await loadConfigurationEditor();
  announce("已刪除 Recipe。");
});
listen($("recipe-form"), "submit", async () => {
  const name = $("recipe-name").value.trim();
  const id = recipeDraft.id || name;
  const confirm = !!recordById(configuration.recipes, id);
  if (confirm && !window.confirm("儲存變更並覆寫這份 Recipe？")) return;
  const steps = recipeDraft.steps.map(({ argvText, ...step }, index) => ({
    ...step, argv: parseJson(argvText, "步驟 " + (index + 1) + " 的參數陣列"),
    name: step.name?.trim() || "步驟 " + (index + 1), cwd: step.cwd?.trim() || ".",
    ...(step.useParams && step.preset ? { preset: step.preset } : { preset: undefined }),
  }));
  await api("/recipes", "POST", { id, name, steps, confirm,
    paramset: $("recipe-table").value, artifacts: $("recipe-artifacts").value,
    env: parseJson($("recipe-env").value, "環境變數"), timeoutMin: Number($("recipe-timeout").value),
  });
  await loadConfigurationEditor({ recipe: id });
  announce("已儲存 Recipe。");
});

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result).split(",")[1]));
    reader.addEventListener("error", () => reject(new Error("無法讀取參數檔。")));
    reader.readAsDataURL(file);
  });
}

function renderTableDetails() {
  const table = recordById(configuration.tables, $("edit-table").value);
  const box = $("table-details");
  box.replaceChildren();
  $("conflict-form").hidden = !table;
  if (!table) return;
  box.append(element("p", "tb-small tb-muted", table.command + " · " + table.params.length + " 項參數"));
  const group = element("div", "row");
  for (const [type, label] of [["switch", "開關"], ["choice", "選項"], ["value", "帶值"]]) {
    group.append(element("span", "tb-badge", label + " " + table.params.filter((param) => param.type === type).length));
  }
  box.append(group);
  if (table.notes?.length) box.append(parameterNotes(table.notes));
  for (const preset of Object.keys(table.presets || {})) {
    const row = element("div", "row");
    row.append(element("span", "tb-mono", preset), button("刪除 preset", async () => {
      if (!window.confirm("刪除 preset「" + preset + "」？")) return;
      await api("/paramsets/presets", "DELETE", { name: table.id, preset, confirm: true });
      await loadConfigurationEditor({ table: table.id });
      announce("已刪除 preset。");
    }));
    box.append(row);
  }
  box.append(parameterBrowser(table));
  const flags = new Map(table.params.map((param) => [param.key, param.flag]));
  $("table-conflicts").value = (table.conflicts || []).map((group) => group.map((key) => flags.get(key) || key).join(", ")).join("\n");
}

// 唯讀的完整參數清單：依分組可展開，可搜尋旗標／說明，可只看某個 preset 會設定的參數。
function parameterBrowser(table) {
  const wrap = element("div", "stack param-browser");
  wrap.append(element("h3", "", "參數清單"));
  const tools = element("div", "editor-fields");
  const searchField = element("label", "tb-field");
  const search = element("input", "tb-input");
  search.type = "search";
  search.placeholder = "旗標或說明";
  searchField.append(element("span", "tb-field__label", "搜尋參數"), search);
  const presetField = element("label", "tb-field");
  const preset = element("select", "tb-select");
  fillPresets(preset, table, "全部參數", "");
  presetField.append(element("span", "tb-field__label", "只看 preset 設定的參數"), preset);
  tools.append(searchField, presetField);
  const actions = element("div", "row");
  const count = element("span", "tb-small tb-muted");
  const groupsBox = element("div", "stack");
  const setOpen = (open) => groupsBox.querySelectorAll("details").forEach((node) => { node.open = open; });
  actions.append(count, element("span", "tb-spacer"), button("全部展開", () => setOpen(true)), button("全部收合", () => setOpen(false)));
  wrap.append(tools, actions, groupsBox);
  const typeLabels = { switch: "開關", choice: "選項", value: "帶值" };
  const usedBy = new Map();   // 參數 key -> 有實際設定它（勾選或填值）的 preset 名稱
  for (const [name, presetValues] of Object.entries(table.presets || {})) {
    for (const [key, value] of Object.entries(presetValues)) {
      if (value === false || value === "") continue;
      if (!usedBy.has(key)) usedBy.set(key, []);
      usedBy.get(key).push(name);
    }
  }

  function valueText(param, value) {
    if (param.type === "switch") return value ? "勾選" : "不勾選";
    return value === "" ? "不使用" : String(value);
  }

  function render() {
    const query = search.value.trim().toLowerCase();
    const values = preset.value ? table.presets?.[preset.value] || {} : null;
    const groups = new Map();
    let shown = 0;
    for (const param of table.params) {
      if (values && !(param.key in values)) continue;
      if (query && !(param.flag + " " + (param.desc || "") + " " + param.group).toLowerCase().includes(query)) continue;
      if (!groups.has(param.group)) groups.set(param.group, []);
      groups.get(param.group).push(param);
      shown++;
    }
    count.textContent = "顯示 " + shown + " / " + table.params.length + " 項";
    groupsBox.replaceChildren();
    if (!shown) groupsBox.append(element("p", "tb-muted", "沒有符合的參數。"));
    let first = true;
    for (const [name, params] of groups) {
      const details = element("details", "param-group");
      details.open = Boolean(query || values) || first;   // 搜尋或篩 preset 時展開有命中的組，平常只展開第一組
      first = false;
      details.append(element("summary", "", name + "（" + params.length + "）"));
      const list = element("dl", "param-list");
      for (const param of params) {
        const term = element("dt");
        term.append(element("code", "parameter-flag", param.flag));
        if (param.type === "value" && param.placeholder) term.append(element("code", "tb-muted", " <" + param.placeholder + ">"));
        term.append(element("span", "tb-badge", typeLabels[param.type] || param.type));
        if (values) term.append(element("span", "tb-badge tb-badge--ok", valueText(param, values[param.key])));
        else {
          if (param.default !== undefined && param.default !== false && param.default !== "") {
            term.append(element("span", "tb-badge", "預設 " + valueText(param, param.default)));
          }
          for (const presetName of usedBy.get(param.key) || []) term.append(element("span", "tb-badge tb-badge--ok", presetName));
        }
        const desc = element("dd", "tb-small");
        desc.textContent = param.desc || "（沒有說明）";
        if (param.type === "choice") desc.append(element("div", "tb-muted", "可選：" + param.choices.join(" | ")));
        list.append(term, desc);
      }
      details.append(list);
      groupsBox.append(details);
    }
  }
  search.addEventListener("input", render);
  preset.addEventListener("change", render);
  render();
  return wrap;
}

$("edit-table").addEventListener("change", renderTableDetails);
listen($("import-parameters"), "submit", async (e) => {
  const file = $("import-file").files[0];
  if (!file || file.size > 740 * 1024) throw new Error("Web 匯入支援 740 KiB 以內的檔案。較大的檔案請使用 CLI。");
  const name = $("import-name").value.trim();
  const confirm = configuration.tables.some((table) => table.id.toLowerCase() === name.toLowerCase());
  if (confirm && !window.confirm("覆寫參數表「" + name + "」？")) return;
  const form = e.currentTarget;
  const { result } = await api("/paramsets/import", "POST", {
    name, confirm, filename: file.name, command: $("import-command").value.trim(), content: await fileBase64(file),
  });
  form.reset();
  await loadConfigurationEditor({ table: name });
  announce("已匯入 " + result.params.length + " 項參數。");
});
listen($("conflict-form"), "submit", async () => {
  const table = recordById(configuration.tables, $("edit-table").value);
  if (!table) return;
  const keys = new Map(table.params.map((param) => [param.flag.toLowerCase(), param.key]));
  const conflicts = lineValues($("table-conflicts").value).map((line) =>
    line.split(",").map((key) => keys.get(key.trim().toLowerCase()) || key.trim()));
  await api("/paramsets/conflicts", "POST", { name: table.id, conflicts, confirm: true });
  await loadConfigurationEditor({ table: table.id });
  announce("已儲存互斥規則。");
});


function defaultArtifactRule() {
  return {
    root: "workspace",
    select: ["Build/**/*.bin"], exclude: ["**/temp/**"], rename: "{name}{ext}",
    dest: "Artifacts/{date}", zip: { enabled: false, file: "{workspace}_{date}_{time}.zip" }, when: "success",
  };
}

function defaultArtifact() {
  return { name: "", rules: [defaultArtifactRule()] };
}

function editArtifactRule(rule) {
  const patterns = (rule.select || []).map((pattern) => pattern.split("/"));
  const folder = [];
  for (const [index, segment] of (patterns[0] || []).slice(0, -1).entries()) {
    if (/[*?\[]/.test(segment) || !patterns.every((parts) => index < parts.length - 1 && parts[index] === segment)) break;
    folder.push(segment);
  }
  return {
    ...rule,
    sourceDir: folder.join("/"),
    selectText: patterns.map((parts) => parts.slice(folder.length).join("/")).join("\n"),
    excludeText: (rule.exclude || []).join("\n"),
    zip: { ...defaultArtifactRule().zip, ...rule.zip },
  };
}

function artifactSelectPatterns(sourceDir, selectText) {
  const folder = sourceDir.trim() ? sourceDir.replaceAll("\\", "/") : "";
  if (folder.startsWith("/") || /[:*?\[]/.test(folder) || folder.split("/").includes("..")) {
    throw new Error("Build 產出資料夾必須是資料來源內的相對路徑。");
  }
  const patterns = folder
    ? selectText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    : lineValues(selectText);
  return patterns.map((pattern) => {
    const relative = pattern.replaceAll("\\", "/");
    if (relative.startsWith("/") || relative.includes(":") || relative.split("/").includes("..")) {
      throw new Error("檔名或 Glob 必須相對於 Build 產出資料夾。");
    }
    return [folder, relative].filter(Boolean).join("/");
  });
}

function setArtifactDraft(record) {
  artifactDraft = { ...record, rules: (record.rules || []).map(editArtifactRule) };
  $("artifact-name").value = record.name;
  $("artifact-name").disabled = !!record.builtIn;
  $("copy-artifact").disabled = !record.id;
  $("remove-artifact").disabled = !record.id || !!record.builtIn;
  $("add-artifact-rule").disabled = !!record.builtIn;
  $("artifact-form").querySelector('button[type="submit"]').disabled = !!record.builtIn;
  $("load-bios-artifact-example").hidden = record.id === builtinBiosArtifactId;
  artifactDirty = false;
  clearArtifactPreview();
  renderArtifactRules();
}

function markArtifactDirty() {
  artifactDirty = true;
  clearArtifactPreview();
}

function renderArtifactRules() {
  const rows = artifactDraft.rules.map((rule, index) => {
    const item = element("fieldset", "editor-item stack");
    item.disabled = !!artifactDraft.builtIn;
    item.append(element("legend", "", "規則 " + (index + 1)));
    const field = (label, key, options) => editorInput(label, rule[key], (value) => {
      rule[key] = value;
      markArtifactDirty();
    }, options);
    const source = element("select", "tb-select");
    source.append(new Option("Workspace（builda.bat 所在資料夾）", "workspace"), new Option("Repo 根（Git 工作樹頂層）", "repo"));
    source.value = rule.root || "workspace";
    source.addEventListener("change", () => {
      rule.root = source.value;
      markArtifactDirty();
    });
    const sourceField = element("label", "tb-field");
    sourceField.append(element("span", "tb-field__label", "資料來源"), source);
    item.append(
      sourceField,
      field("Build 產出資料夾（相對資料來源）", "sourceDir", { folder: "relative", anchor: () => rule.root === "repo" ? "repo" : null }),
      field("檔名或 Glob（每行一條）", "selectText", { rows: 2 }),
      field("排除 Glob（相對資料來源，每行一條）", "excludeText", { rows: 2 }),
      ...(rule.zip.enabled && rule.zip.only ? [] : [field("改名範本", "rename")]),
      field("產出目的地（可絕對路徑；相對時置於資料來源下）", "dest", { folder: "absolute" }),
      editorCheck("建立 ZIP", rule.zip.enabled, (value) => {
        rule.zip.enabled = value;
        markArtifactDirty();
        renderArtifactRules();
      }),
    );
    if (rule.zip.enabled) item.append(
      editorCheck("只建立 ZIP（保留子目錄）", !!rule.zip.only, (value) => {
        rule.zip.only = value;
        markArtifactDirty();
        renderArtifactRules();
      }),
      editorInput("ZIP 檔名範本", rule.zip.file, (value) => {
        rule.zip.file = value;
        markArtifactDirty();
      }),
    );
    item.append(button("刪除規則", () => {
      artifactDraft.rules.splice(index, 1);
      markArtifactDirty();
      renderArtifactRules();
    }));
    return item;
  });
  $("artifact-rules").replaceChildren(...(rows.length ? rows : [empty("請新增至少一條規則。")]));
}

function clearArtifactPreview() {
  artifactPreview = null;
  $("artifact-preview").replaceChildren();
  $("execute-artifact").disabled = true;
}

$("edit-artifact").addEventListener("change", () => setArtifactDraft(recordById(configuration.artifacts, $("edit-artifact").value) || defaultArtifact()));
$("artifact-name").addEventListener("input", markArtifactDirty);
listen($("new-artifact"), "click", () => {
  $("edit-artifact").value = "";
  setArtifactDraft(defaultArtifact());
  $("artifact-name").focus();
});
function copyArtifactDraft(source) {
  $("edit-artifact").value = "";
  setArtifactDraft({ name: copyName(source, configuration.artifacts), rules: structuredClone(source.rules) });
  $("artifact-name").select();
}

listen($("copy-artifact"), "click", () => {
  const source = recordById(configuration.artifacts, $("edit-artifact").value);
  if (source) copyArtifactDraft(source);
});
listen($("load-bios-artifact-example"), "click", () => {
  const example = recordById(configuration.artifacts, builtinBiosArtifactId);
  if (!example) throw new Error("找不到內建的 BIOS ROM 打包範例。");
  copyArtifactDraft(example);
});
listen($("add-artifact-rule"), "click", () => {
  artifactDraft.rules.push(editArtifactRule(defaultArtifactRule()));
  markArtifactDirty();
  renderArtifactRules();
});
listen($("remove-artifact"), "click", async () => {
  if (!artifactDraft.id || !window.confirm("刪除產出物規則組「" + artifactDraft.name + "」？")) return;
  await api("/artifact-rules", "DELETE", { name: artifactDraft.id, confirm: true });
  await loadConfigurationEditor({ artifact: "" });
  announce("已刪除規則組。");
});
listen($("artifact-form"), "submit", async () => {
  const name = $("artifact-name").value.trim();
  const id = artifactDraft.id || name;
  const confirm = !!recordById(configuration.artifacts, id);
  if (confirm && !window.confirm("儲存變更並覆寫這份產出物規則？")) return;
  const rules = artifactDraft.rules.map(({ sourceDir, selectText, excludeText, ...rule }) => ({
    ...rule, select: artifactSelectPatterns(sourceDir, selectText), exclude: lineValues(excludeText),
  }));
  await api("/artifact-rules", "POST", { id, name, rules, confirm });
  await loadConfigurationEditor({ artifact: id });
  announce("已儲存規則組。");
});

function artifactJobs() {
  const workspaceId = $("artifact-workspace").value;
  const rows = jobs.filter((job) => job.workspaceId === workspaceId && job.state === "success");
  selectRecords("artifact-job", rows.map((job) => ({ id: job.id, name: job.recipe + " · " + timeLabel(job.endedAt) })), "不指定 Job");
}

async function refreshArtifactOptions() {
  await Promise.all([refreshWorkspaces(), syncJobs()]);
  selectRecords("artifact-workspace", workspaces, "請選擇 Workspace");
  artifactJobs();
}

$("artifact-workspace").addEventListener("change", () => { clearArtifactPreview(); artifactJobs(); });
$("artifact-job").addEventListener("change", clearArtifactPreview);
listen(document.querySelector('[data-config="artifact-editor"]'), "click", refreshArtifactOptions);

function showArtifactResults(plan, completed = false) {
  const output = $("artifact-preview");
  const files = plan.files || [];
  const archives = plan.archives || [];
  output.replaceChildren(element("strong", "", (completed ? "已複製 " : "預計複製 ") + files.length + " 個檔案，" + archives.length + " 個 ZIP"));
  const list = element("ul", "artifact-results");
  for (const row of files) {
    const item = element("li", "stack");
    item.append(element("span", "", row.source), element("code", "", "→ " + row.destination));
    list.append(item);
  }
  for (const row of archives) {
    list.append(element("li", "tb-mono", "ZIP · " + row.destination));
  }
  if (list.childElementCount) output.append(list);
  for (const error of plan.errors || []) {
    const message = element("p", "preview-warning", [error.source || error.destination, error.message].filter(Boolean).join(" · "));
    message.dataset.error = "";
    output.append(message);
  }
}

listen($("preview-artifact"), "click", async () => {
  const name = $("edit-artifact").value;
  const workspaceId = $("artifact-workspace").value;
  if (!name || artifactDirty) throw new Error("請先儲存產出物規則組。");
  if (!workspaceId) throw new Error("請先選擇預覽 Workspace。");
  clearArtifactPreview();
  const jobId = $("artifact-job").value;
  const { result } = await api("/artifact-rules/preview", "POST", { name, workspaceId, jobId });
  if (artifactDirty || name !== $("edit-artifact").value || workspaceId !== $("artifact-workspace").value ||
      jobId !== $("artifact-job").value) return;
  artifactPreview = result.previewId;
  showArtifactResults(result);
  $("execute-artifact").disabled = !result.files.length;
});

listen($("execute-artifact"), "click", async () => {
  if (!artifactPreview || !window.confirm("依這份預覽複製並整理產出物？")) return;
  $("execute-artifact").disabled = true;
  const previewId = artifactPreview;
  try {
    let task = (await api("/artifact-rules/execute", "POST", { previewId, confirm: true })).result;
    while (task.state === "running") {
      await new Promise((resolve) => setTimeout(resolve, 350));
      task = (await api("/artifact-tasks/" + task.id)).result;
    }
    if (artifactPreview === previewId) {
      artifactPreview = null;
      showArtifactResults(task.result, true);
    }
    announce(task.state === "success" ? "產出物已整理完成。" : "部分產出物處理失敗，請查看結果。", task.state !== "success");
  } catch (error) {
    if (artifactPreview === previewId && error.code !== "ARTIFACT_PREVIEW_EXPIRED") $("execute-artifact").disabled = false;
    throw error;
  }
});

async function loadConfigurationEditor({ recipe, table, artifact } = {}) {
  await loadConfiguration();
  selectRecords("edit-recipe", configuration.recipes, "新建 Recipe",
    recipe ?? (recipeDraft ? undefined : configuration.recipes.find((row) => row.builtIn)?.id));
  selectRecords("edit-table", configuration.tables, "請選擇參數表", table);
  selectRecords("edit-artifact", configuration.artifacts, "新建規則組", artifact);
  if (!recipeDraft || recipe !== undefined || (recipeDraft.id || "") !== $("edit-recipe").value) {
    setRecipeDraft(recordById(configuration.recipes, $("edit-recipe").value) || defaultRecipe());
  } else if (!recipeDraft.builtIn) {
    selectRecords("recipe-table", configuration.tables, "不使用參數表", $("recipe-table").value, true);
    selectRecords("recipe-artifacts", configuration.artifacts, "不套用產出物規則", $("recipe-artifacts").value, true);
    renderRecipeSteps();
  }
  renderTableDetails();
  if (!artifactDraft || artifact !== undefined || (artifactDraft.id || "") !== $("edit-artifact").value) {
    setArtifactDraft(recordById(configuration.artifacts, $("edit-artifact").value) || defaultArtifact());
  }
  if (!$("artifact-editor").hidden) await refreshArtifactOptions();
}
