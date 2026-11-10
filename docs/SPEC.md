# Build-Runner 規格（草稿 v0.1，2026-11-10）

Dev-Toolbox 第四個工具：管理多個程式碼資料夾、用可自訂的流程跑 build、即時看 log、整理產出物、保留歷史，並提供 MCP 介面給 code agent 使用。

> 名稱與 port 是暫定值，可再改。標「待定」的部分等明天的參數規格表確認。

## 1. 基本資料

| 項目 | 值 |
| --- | --- |
| id | `build-runner` |
| 名稱 | Build Runner |
| 版本 | 0.1.0 |
| runtime | Python 3.10+，**只用標準函式庫** |
| 預設 port | 8740（照 TOOL-SPEC：往上找 +20，找不到綁 port 0） |
| 資料夾 | `%LOCALAPPDATA%\DevToolbox\build-runner\` |
| 位置 | `Dev-Toolbox\tools\Build-Runner\` |

選 Python 的原因：子行程即時讀輸出、zip、檔案比對、MCP（stdio JSON-RPC）用標準函式庫都很好寫；Web 用 `ThreadingHTTPServer`，這台電腦已驗證可用。

**repo 不得含公司內容。** build 指令、參數表、路徑、平台名稱全部放在使用者資料夾；repo 只放通用框架和假名範例（例如 `examples/recipe-sample.json` 用 `PlatformA`）。

## 2. 名詞

| 名詞 | 意思 |
| --- | --- |
| Workspace | 一個程式碼資料夾（一包 code）。可自由新增／移除。 |
| 參數表 | 匯入的 build 參數清單，每個參數有說明。 |
| Recipe（流程） | 一組步驟：前置步驟 → 主 build → 後置步驟 → 產出物規則。 |
| Job | 某個 Workspace 套用某個 Recipe 跑一次。 |
| Artifact 規則 | build 完要挑哪些檔、怎麼改名、複製到哪、要不要壓縮。 |

## 3. 功能

### 3.1 Workspace 管理

- 新增：選資料夾（輸入路徑，或後端用 `tkinter.filedialog` 開選擇視窗；tkinter 不能用時退回手動輸入）。
- 從 git repo 匯入：輸入 repo 路徑，用 `git worktree list --porcelain` 列出所有 worktree，勾選後加入。
- 每個 Workspace 顯示：名稱（可改）、路徑、branch、HEAD commit（短 hash＋標題）、是否有未提交變更、上次 build 結果。
- 可做的 git 動作（都用參數陣列呼叫 git）：重新整理狀態、`git pull`（ff-only）、切 branch（列出 local／remote branch 選）。
- 不依賴 Git Ref Manager 的內部程式；兩者都直接讀 git，所以資料一致。
- 存在 `workspaces.json`。

### 3.2 參數表

- 匯入 Markdown、CSV、XLSX 或 JSON 參數表，轉成內部格式存成 `paramsets\<名稱>.json`。
- Markdown 以段落標題分組，讀取「參數／用途」表格。反引號內的旗標、帶值提示、以直線分隔的選項各自轉換。環境變數與棄用項目不加入可選旗標，使用限制備註保留供檢視。自然語言的限制可以在流程設定另存成明確的互斥規則。
- CSV 與 XLSX 的第一列使用 `flag` 欄。可選欄位為 `key`、`type`、`group`、`desc`、`default`、`choices`、`placeholder`、`conflicts`、`command`。XLSX 取第一張工作表。`choices` 與 `conflicts` 欄接受以直線分隔的值或 JSON 陣列。
- `presets` 是以名稱為鍵的物件，每個值是參數 key 到值的對應。CLI 或 Build 頁的 preset 會覆蓋步驟的 preset，個別選項再覆蓋 preset。未指定的值採用參數預設值。

```json
{
  "name": "sample",
  "command": "builda.bat",
  "params": [
    { "key": "r",    "flag": "-r",    "type": "switch", "group": "一般", "desc": "Release build", "default": true },
    { "key": "nou",  "flag": "-nou",  "type": "switch", "group": "一般", "desc": "不更新 xxx" },
    { "key": "plat", "flag": "-p",    "type": "choice", "choices": ["A", "B"], "desc": "平台" },
    { "key": "ver",  "flag": "-v",    "type": "value",  "desc": "版本字串", "placeholder": "1.0.0" }
  ],
  "conflicts": [["r", "d"]]
}
```

- type：`switch`（勾選）、`choice`（下拉）、`value`（填值）。
- 介面：參數依 group 分組，每個參數旁邊顯示說明；上方有搜尋框（搜 flag 和說明）；下方即時預覽完整指令；互斥的參數同時勾選時顯示警告。
- 可存成「常用組合」（preset），一鍵套用。

### 3.3 Recipe（自訂流程）

```json
{
  "name": "Clean + Release",
  "paramset": "sample",
  "steps": [
    { "name": "cleanall", "argv": ["builda.bat", "cleanall"], "continueOnFail": false },
    { "name": "build",    "argv": ["builda.bat"], "useParams": true, "preset": "release" }
  ],
  "env": { "KEY": "VALUE" },
  "timeoutMin": 180,
  "artifacts": "release-rules"
}
```

- 預設範本：先 `builda.bat cleanall`，再跑 `builda.bat -r`。步驟可新增、刪除、排序、停用。
- 每個步驟在 Workspace 根目錄執行（可另設子資料夾 `cwd`）。
- `.bat`／`.cmd` 用 `["cmd.exe", "/d", "/c", ...argv]` 執行；其他直接用 argv。不經 shell 字串拼接。
- 步驟失敗就停（除非 `continueOnFail`）。
- 存在 `recipes\<名稱>.json`。

### 3.4 Job（多個同時跑）

- 從「Workspace × Recipe」建立 Job，可一次勾多個 Workspace。
- 可同時跑多個 Job；「同時最多幾個」由使用者在設定或總覽頁隨時調整（預設 2，填 0 代表不限），超過就排隊。調高後立刻從排隊中啟動，調低不影響已在跑的 Job。可隨時新增、取消排隊、停止。
- **衝突警告**：
  - 同一個資料夾已有 Job 在跑或排隊 → 紅色警告，預設不允許（要勾「我知道」才放行）。
  - 不同 worktree 但同一個 repo（`git rev-parse --git-common-dir` 相同）→ 黃色提醒。
- 停止：用 `taskkill /PID <pid> /T /F` 結束整個行程樹。
- **架構**：每個 Job 由獨立的 runner 子行程執行（`python -m build_runner.runner <jobId>`），狀態寫在 `jobs\<jobId>\status.json`、輸出寫在 `jobs\<jobId>\build.log`。Web、CLI、MCP 都讀同一份檔案，所以關掉網頁不會中斷 build，三種介面看到的也一致。
- status.json：`{ id, workspace, recipe, argvPreview, state: queued|running|success|failed|stopped, step, startedAt, endedAt, exitCode, pid, branch, commit }`。

### 3.5 Log 與錯誤

- 即時 log：前端每秒輪詢 `GET /api/jobs/<id>/log?from=<byte offset>`，只拿新增的部分。
- 編碼：依序試 UTF-8 → UTF-16（有 BOM 時）→ cp950，無法解碼的字元用替代字元。
- 快速搜尋按鈕：`error`、`fail`、`warning`，加上自訂關鍵字；顯示筆數，可上一筆／下一筆跳轉，命中處高亮。
- 自動判讀（沿用 build-log-triage 的規則）：判斷 `- Done -`／`- Failed -`、找第一個根本錯誤（compile／link 優先）、把 `NMAKE U1077`、`error 7000` 歸為連鎖錯誤。結果顯示在 log 上方，點一下跳到該行。
- 「AI 分析」：把判讀結果＋錯誤前後 40 行（加上 Workspace、branch、指令）組成提示詞，提示詞範本可在設定裡修改。三種送法，設定裡選預設、按鈕旁的下拉可臨時切換：
  1. **OpenCode**：在該 Workspace 資料夾執行 `opencode run <提示詞>`（opencode 執行檔路徑、要帶的 `--model`／`--agent` 等參數可在設定填），輸出顯示在面板。長提示詞先寫到暫存檔，避免超過命令列長度。
  2. **自訂端點**：OpenAI 相容 API（例如 LiteLLM），設定 base URL、model、API key（key 存在使用者資料夾，畫面上遮罩顯示），用 `/v1/chat/completions` 送出並顯示回答。
  3. **複製提示詞**：只複製到剪貼簿，讓使用者自己貼到任何 AI。
  - 送出都是背景工作，可取消；回答存到 `jobs\<jobId>\ai-<時間>.md`，歷史頁可以再打開。
- 大 log：前端只保留最後 N 行（預設 20000），搜尋在後端做。

### 3.6 產出物（Artifact 規則）

```json
{
  "name": "release-rules",
  "rules": [
    {
      "select": ["Build/**/*.bin", "Build/**/*.cap"],
      "exclude": ["**/temp/**"],
      "rename": "{name}_{branch}_{date}_{commit7}{ext}",
      "dest": "C:/sample/output/{date}",
      "zip": { "enabled": true, "file": "{workspace}_{date}_{time}.zip" },
      "when": "success"
    }
  ]
}
```

- 每條規則：挑檔（glob）→ 排除 → 改名 → 複製到目的地 → 可選壓縮。可多條規則，不同檔案放不同地方。
- 變數：`{workspace} {name} {ext} {branch} {commit7} {date} {time} {jobId} {recipe}`，以及參數值 `{param.plat}`。
- 介面上可「預覽」：列出會被挑到的檔案和改名後的結果，確認後才執行。
- 目的地已有同名檔時：預設加序號，不覆蓋。
- 存在 `artifacts\<名稱>.json`。

### 3.7 歷史紀錄

- 預設全部保留，寫在 `history.jsonl`（一行一筆）：時間、Workspace、branch、commit、Recipe、實際指令、結果、耗時、根本錯誤摘要、log 路徑、產出物清單。
- 介面：表格，可篩選（Workspace、結果、日期）和排序。
- 匯出：CSV 或 JSON，可選匯出範圍。
- 清除：勾選要刪的紀錄（可一併刪 log），後端要求 `confirm: true` 才執行；也可設「保留最近 N 天／N 筆」自動清理（預設關閉）。
- 保留規則會在儲存設定或建置結案時套用到歷史紀錄。刪除 log 由使用者在手動刪除時另外勾選。

### 3.8 通知

- Job 結束時：瀏覽器通知（Notification API，需使用者允許）＋頁籤標題閃爍＋可選提示音。
- 選填：Windows 系統通知（用 PowerShell 呼叫 `Windows.UI.Notifications`，失敗就略過）。

### 3.9 排程 build

- 用 Windows「工作排程器」：介面設定時間與要跑的 Job（Workspace × Recipe），後端用 `schtasks /Create` 建立呼叫 CLI 的排程（`python -m build_runner.cli run ...`）。
- 這樣工具沒開著也會執行；結果一樣寫進歷史。
- 介面可列出、停用、刪除本工具建立的排程（排程名稱固定前綴 `DevToolbox-BuildRunner-`）。
- 頻率支援每日與每週，時間以 Windows 工作排程器的本機時間為準。

### 3.10 MCP 介面（給 code agent）

- `mcp_server.py`：stdio 的 MCP server（JSON-RPC 2.0，只用標準函式庫），直接呼叫 core。
- 在 opencode.json 註冊為 local MCP：`["python", "<路徑>/mcp_server.py"]`。
- 提供的 tools：

| tool | 作用 |
| --- | --- |
| `list_workspaces` | 列出 Workspace 和狀態 |
| `list_recipes` | 列出 Recipe、參數表、preset |
| `describe_params` | 列出某參數表的參數與說明 |
| `start_build` | 用既有 Workspace＋Recipe（可帶 preset 或參數）開始 Job，回傳 jobId |
| `get_job` | Job 狀態 |
| `read_log` | 讀 log 的某一段（tail N 行或行號範圍），單次上限 400 行 |
| `search_log` | 在 log 搜尋關鍵字或規則，回傳行號與片段 |
| `triage_job` | 回傳判讀結果（結果、根本錯誤、連鎖錯誤） |
| `stop_job` | 停止 Job |
| `list_history` | 最近 N 筆歷史 |

- 安全限制：agent **不能**傳任意指令，只能用已存在的 Recipe 與參數表裡有的參數；不提供刪除歷史、改設定的 tool。

## 4. CLI

`python -m build_runner.cli <command>`（`Run-CLI.bat` 包一層），共通參數照 TOOL-SPEC（`--help --version --json --data-dir --yes --dry-run`）。

| command | 作用 |
| --- | --- |
| `workspaces` | 列出 Workspace |
| `run --workspace <名稱> --recipe <名稱> [--preset <名稱>] [--wait]` | 開始 build；`--wait` 等到結束並以結果決定 exit code |
| `jobs` / `status <jobId>` | 列出 Job／查狀態 |
| `stop <jobId>` | 停止 |
| `triage <jobId 或 log 檔>` | 判讀 log |
| `import-params <檔案> --name <名稱>` | 匯入參數表 |
| `history [--export csv|json --out <檔>]` | 歷史 |

exit code 照 TOOL-SPEC（0 成功、1 失敗、3 參數錯、4 取消、5 環境不符）。

## 5. Web API（除共通 API 外）

| 方法與路徑 | 用途 |
| --- | --- |
| `GET/POST/DELETE /api/workspaces` | Workspace 管理；`POST /api/workspaces/import-worktrees` |
| `POST /api/workspaces/<id>/git` | `{ action: refresh|pull|checkout, branch }` |
| `GET/POST/DELETE /api/paramsets`、`POST /api/paramsets/import` | 參數表 |
| `GET/POST/DELETE /api/recipes` | Recipe |
| `GET/POST/DELETE /api/artifact-rules`、`POST /api/artifact-rules/preview` | 產出物規則 |
| `POST /api/jobs/check` | 建立前檢查衝突，回傳警告清單 |
| `POST /api/jobs` | 建立 Job（可多個 Workspace） |
| `GET /api/jobs`、`GET /api/jobs/<id>` | 列表／狀態 |
| `GET /api/jobs/<id>/log?from=<offset>` | 增量 log |
| `GET /api/jobs/<id>/search?q=&kind=error|fail|warning` | 搜尋 |
| `GET /api/jobs/<id>/triage`、`POST /api/jobs/<id>/ai` | 判讀／AI 分析 |
| `POST /api/jobs/<id>/stop` | 停止 |
| `GET /api/history`、`POST /api/history/export`、`POST /api/history/delete` | 歷史（delete 要 `confirm: true`） |
| `GET/POST/DELETE /api/schedules` | 排程 |
| `GET/POST /api/settings` | 設定（同時數量、AI 送法與端點、提示詞範本、通知、保留天數） |

安全規則全部照 TOOL-SPEC 第 5 節（Host 檢查、`X-Toolbox-Request`、Origin、CSP、白名單靜態檔、body 上限）。
TOOL-SPEC 寫「同一時間只允許一個會寫入的工作」：本工具的 build Job 例外，改由上面的衝突檢查管理；設定檔寫入仍要加鎖。

## 6. 介面（ui-kit 3.0，上方分頁）

| 分頁 | 內容 |
| --- | --- |
| 總覽 | 正在跑／排隊的 Job 卡片（進度、步驟、耗時、停止鈕），最近結果 |
| Workspace | 資料夾清單、git 狀態、新增／匯入 worktree、勾選後「開始 build」 |
| Build | 選 Workspace（多選）＋ Recipe ＋ 參數（分組勾選＋說明＋搜尋＋指令預覽）→ 衝突檢查 → 開始 |
| Log | 選 Job，即時 log、error／fail／warning 快速搜尋、判讀摘要、AI 分析 |
| 流程設定 | Recipe、參數表匯入、preset、產出物規則（含預覽） |
| 歷史 | 表格、篩選、匯出、勾選刪除 |
| 設定 | 同時數量、AI 送法（OpenCode／自訂端點／複製）、通知、排程、保留規則 |

圖標用 Lucide（`tb-icons.js`），顏色只用 `--tb-*` token，支援 `?embedded=1`，400px 寬不破版。

每個分頁的「功能說明」會開啟該頁操作步驟。

## 7. 檔案結構

```text
Build-Runner/
  tool.json  README.md  CHANGELOG.md  LICENSE  .gitignore
  Run-Web.bat  Run-CLI.bat
  build_runner/
    __init__.py
    paths.py        資料夾位置、讀寫 JSON（暫存檔＋改名）
    workspaces.py   Workspace 與 git
    paramsets.py    參數表與匯入
    recipes.py      Recipe、組指令
    jobs.py         建立／排隊／衝突檢查／停止
    runner.py       單一 Job 的執行子行程
    logtools.py     解碼、增量讀取、搜尋、判讀
    artifacts.py    產出物規則與預覽
    history.py      歷史、匯出、清除
    schedule.py     schtasks
    web.py          HTTP 伺服器
    cli.py
  mcp_server.py
  ui/  index.html  app.js  style.css  ui-kit.css  tb-theme.js  tb-icons.js
  examples/  recipe-sample.json  paramset-sample.json  artifacts-sample.json
  tests/
```

## 8. 已確認事項

已確認（2026-11-10）：名稱 `Build-Runner`、port 8740；同時數量由使用者自由調整；AI 分析支援 OpenCode、自訂端點、複製提示詞三種。
