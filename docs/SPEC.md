# Build-Runner 規格（草稿 v0.1，2026-11-10）

Dev-Toolbox 第四個工具：管理多個程式碼資料夾、用可自訂的流程跑 build、即時看 log、整理產出物、保留歷史，並提供 MCP 介面給 code agent 使用。

> 名稱與 port 是暫定值，可再改。標「待定」的部分等明天的參數規格表確認。

## 1. 基本資料

| 項目 | 值 |
| --- | --- |
| id | `build-runner` |
| 名稱 | Build Runner |
| 版本 | 0.2.0 |
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
- 可做的 git 動作（都用參數陣列呼叫 git）：重新整理狀態、`git pull`（ff-only）、切 branch（local／remote 可搜尋，也接受本機存在的 commit SHA：切到 detached HEAD；勾「強制」改走 `git checkout -f`：丟棄未提交變更並取消進行中的 revert/cherry-pick/merge）、`git submodule update --init --recursive`。revert/rebase 等進行中會在卡片顯示 `gitOperation` 標章，此時 Git 拒絕一般切換。branch 名稱與 SHA 由 `workspaces.resolve_checkout()` 統一解析（完整 ref→顯示名→SHA；local 與 remote 同名優先 local），Build 目標共用同一函式。
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
    { "name": "cleanall", "argv": ["builda.bat", "-cleanall"], "continueOnFail": false },
    { "name": "build",    "argv": ["builda.bat"], "useParams": true, "preset": "release" }
  ],
  "env": { "KEY": "VALUE" },
  "timeoutMin": 180,
  "artifacts": "release-rules"
}
```

- 內建 Recipe 可複製成可編輯的自訂流程。「Clean + Build」執行 `builda.bat -cleanall` 和 `builda.bat -r`。「Clean + Build (-nou)」的兩步都加 `-nou`。「Clean + Build + Package」在兩個 `-nou` 步驟成功後套用內建 BIOS ROM 打包規則。內建 Recipe 與內建產出物規則唯讀（`@builtin:` ID 的 POST/DELETE 回 400），範例檔缺損時清單自動省略該項。依名稱查詢時使用者檔案優先，只有找不到同名使用者檔才比對內建顯示名，自訂 Recipe 不會被內建遮蔽。
- 每個步驟在 Workspace 根目錄執行（可另設子資料夾 `cwd`）。
- `.bat`／`.cmd` 用 `["cmd.exe", "/d", "/c", ...argv]` 執行；其他直接用 argv。不經 shell 字串拼接。
- 步驟失敗就停（除非 `continueOnFail`）。
- 存在 `recipes\<名稱>.json`。

### 3.4 Job（多個同時跑）

- 從「Workspace × Recipe」建立 Job，可一次勾多個 Workspace。
- 可同時跑多個 Job；「同時最多幾個」由使用者在設定或總覽頁隨時調整（預設 2，填 0 代表不限），超過就排隊。調高後立刻從排隊中啟動，調低不影響已在跑的 Job。可隨時新增、取消排隊、停止。另有硬性序列化（不受上限影響）：任何 Job 與執行中 Job 相同 Workspace 資料夾 → 不啟動，依 queuedAt 順序輪流跑；帶 Build 目標的 Job 亦與相同 repo（gitCommonDir，含其他 worktree）的執行中 Job 序列化，避免切樹互搶。
- **Build 目標（checkout）**：`POST /api/jobs` 可帶 `checkout: { target, fetch, submodule, force }`（target ≤200 字元；submodule 預設 true），或 `checkouts: [同上] × 1..30` 的多目標佇列（陣列順序＝建置順序，每目標×每 Workspace 各建一個 Job，queuedAt 以 1ms 遞增保證 dispatch 依序）。Runner 在第一個建置步驟前依序執行：選配 `git fetch origin --prune` → `workspaces.resolve_checkout()` 解析目標（branch／remote／SHA）→ `git switch`（SHA 走 `--detach`）→ 選配 `git submodule update --init --recursive`，log 以 `[Build Runner]` 標注進度，status 的 `step` 顯示 `Checkout`。`force: true` 時 switch 加 `-f`（三路徑 --detach／--／--track 皆適用）：丟棄未提交的追蹤檔案變更（untracked 保留）；UI 的「強制切樹」勾選需經確認框，取消自動反勾。目標為遠端分支時：本機已有同名分支且未勾 force → Job 以 `CHECKOUT_BRANCH_EXISTS` 失敗（避免建置到可能落後的本機分支）；勾 force → `git switch -C -f <name> --track -- <remote>/<name>` 重置本機分支到遠端最新。未勾 force 時髒樹或找不到目標会让 Job 在 Checkout 步驟直接失敗，不會建置到錯誤修訂。preview 的每個 plan 帶 `checkoutTarget`。Build 頁「選擇 Branch／Commit…」仿 TortoiseGit 選 log 對話框：左側多層樹（依分支名 `/` 逐層分組，含葉分支；heads／remotes／各 remote 預設展開，單節點最多渲染 200 項）、右側 Branch 表格（名稱、Last Commit／Author Date、Last Commit，資料來自 `GET /api/workspaces/<id>/branches`），Filter 欄同步縮減樹與表格；點分支列切換下方 `GET /api/workspaces/<id>/commits` 的 commit 清單；勾選 branch（完整 ref）／commit 加入 Build 佇列、逐筆 ↑↓ 排序或移除後一次送出 `checkouts`；手動編輯「Build 目標」欄會先確認再清掉佇列，改走單筆 `checkout`。
- **衝突警告**：
  - 同一個資料夾已有 Job 在跑或排隊 → 黃色 `QUEUE_SAME_WORKSPACE` 警告（201 放行）：Job 直接排在後面，dispatch 保證同一資料夾一次只跑一個、依 queuedAt 順序執行，與併發上限和是否帶 Build 目標無關。
  - 不同 worktree 但同一個 repo（`git rev-parse --git-common-dir` 相同）→ 黃色提醒。
- 停止：用 `taskkill /PID <pid> /T /F` 結束整個行程樹。
- **架構**：每個 Job 由獨立的 runner 子行程執行（`python -m build_runner.runner <jobId>`），狀態寫在 `jobs\<jobId>\status.json`、輸出寫在 `jobs\<jobId>\build.log`。Web、CLI、MCP 都讀同一份檔案，所以關掉網頁不會中斷 build，三種介面看到的也一致。
- status.json：`{ id, workspace, recipe, argvPreview, checkoutTarget, state: queued|running|success|failed|stopped, step, startedAt, endedAt, exitCode, pid, branch, commit }`。
- **互動提示跳過**：Build 停在 `Enter Your Username:`、`Enter password for ...:`、`Enter passphrase for ... (enter to skip):` 時送 Enter（空回答略過）。自動模式（`autoSkipPrompts`，預設開）偵測 log 尾端提示＋輸出停止 1.5 秒後自動送，每 Job 上限 5 次、冷卻 2 秒；手動為 log 頁「按 Enter 繼續」橫幅與 MCP `send_enter`。
  - **雙通道投遞**：① 寫 `\r\n` 到步驟行程的 stdin pipe；② `AttachConsole` 附加到步驟行程樹的主控台，以 `WriteConsoleInput` 注入 VK_RETURN（`build_runner/console.py`）。第 ② 條是必要的：`getpass` 類提示直接從主控台讀字元、題目也不會進 log，只有 pipe 通道會漏掉。任一通道送達即算成功，才清掉 `enter.request` 旗標。未被當前提示吃掉的 Enter 會滯留佇列、可能答到後續的主控台讀取（例如 bat 結尾的 `pause`）——自動模式以每次注入前 Flush＋每 Job 上限 5 次封頂，此取換在「全部提示都要跳過」的場景下可接受。
  - `inject_enter()` 開頭先 `FreeConsole()`（呼叫端自帶主控台時 `AttachConsole` 回 ACCESS_DENIED），結束後若原本有主控台以 `AttachConsole(ATTACH_PARENT_PROCESS)` 還原（僅對繼承自父行程的主控台保證是同一個；生產 runner 為 DETACHED，不受影響）。注入前主控台輸入佇列先 Flush 確保只剩這次 Enter——若操作人員正在可見的步驟視窗裡親手輸入，其輸入會被一併清掉。

### 3.5 Log 與錯誤

- 即時 log：前端每秒輪詢 `GET /api/jobs/<id>/log?from=<byte offset>`，只拿新增的部分。
- 編碼：依序試 UTF-8 → UTF-16（有 BOM 時）→ cp950，無法解碼的字元用替代字元。
- 快速搜尋按鈕：`error`、`fail`、`warning`，加上自訂關鍵字；顯示筆數，可上一筆／下一筆跳轉，命中處高亮。
- 自動判讀（沿用 build-log-triage 的規則）：判斷 `- Done -`／`- Failed -`、找第一個根本錯誤（compile／link 優先）、把 `NMAKE U1077`、`error 7000` 歸為連鎖錯誤。結果顯示在 log 上方，點一下跳到該行。
- 「AI 分析」：**功能預設關閉**，設定頁「AI 分析設定」的「啟用 AI 分析功能」開關打開後，Log 頁與歷史頁的分析入口才顯示；關閉時 `POST /api/jobs/<id>/ai` 回 409 `AI_DISABLED`（包含預覽與複製）；讀取已儲存結果的 GET API 不受影響（畫面上要等開關重開才看得到入口），進行中的分析不會因關閉而中止。把判讀結果＋選定的 log 範圍（加上 Workspace、branch、指令）組成提示詞，提示詞範本可在設定裡修改。log 範圍下拉：錯誤附近 ±40 行（預設）／尾端 200 行／完整 log（取檔尾，超過時開頭加省略提示）／自訂起訖行號（上限 20000 行）。「預覽提示詞」以 `POST /api/jobs/<id>/ai` 帶 `preview: true` 取回提示詞內容但不送出分析。三種送法，設定裡選預設、按鈕旁的下拉可臨時切換：
  1. **OpenCode**：在該 Workspace 資料夾執行 `opencode run <提示詞>`（opencode 執行檔路徑、要帶的 `--model`／`--agent` 等參數可在設定填），輸出顯示在面板。長提示詞先寫到暫存檔，避免超過命令列長度。
  2. **自訂端點**：OpenAI 相容 API（例如 LiteLLM），設定 base URL、model、API key（key 存在使用者資料夾，畫面上遮罩顯示），用 `/v1/chat/completions` 送出並顯示回答。
  3. **複製提示詞**：只複製到剪貼簿，讓使用者自己貼到任何 AI。
  - 送出都是背景工作，可取消；回答存到 `jobs\<jobId>\ai-<時間>.md`，歷史頁可以再打開。
- 大 log：前端只保留最後 N 行（預設 5000）；開啟 Job 首屏改讀檔尾（`?tail=`），不再整份重播；搜尋在後端做，結果依檔案大小與修改時間快取。
- 下載：log 頁「下載 .log」走 `GET /api/jobs/<id>/log?download=1`，回應 `Content-Disposition: attachment`，檔名 `<workspace>-<branch>-<job前8碼>.log`，非 `[A-Za-z0-9._-]` 字元取代為 `-`。

### 3.6 產出物（Artifact 規則）

```json
{
  "name": "release-rules",
  "rules": [
    {
      "root": "workspace",
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
- `root`（預設 `workspace`）決定 glob 與相對 `dest` 的錨點：`workspace`＝Workspace 資料夾；`repo`＝Git 工作樹頂層（`git rev-parse --show-toplevel`），用於「Workspace 是平台資料夾、Build 產出在 repo 根 `Build\`」的 OEM 平台佈局；Workspace 不在 checkout 內時該規則報錯。glob 仍不得含 `..`、盤符或絕對路徑（不得再逃出錨點）；`dest` 可絕對路徑，相對時落在錨點下。plan 檔逐筆記 `base`，舊 plan 無 `base` 時視為 workspace。
- 內建 BIOS ROM 打包規則（`@builtin:bios-rom-package`）即用 `root: "repo"`。
- 變數：`{workspace} {name} {ext} {branch} {commit7} {date} {time} {jobId} {recipe}`，以及參數值 `{param.plat}`。
- 介面上可「預覽」：列出會被挑到的檔案和改名後的結果，確認後才執行。
- 目的地已有同名檔時：預設加序號，不覆蓋。
- 存在 `artifacts\<名稱>.json`。

### 3.7 歷史紀錄

- 預設全部保留，寫在 `history.jsonl`（一行一筆）：時間、Workspace、branch、commit、Recipe、實際指令、結果、耗時、根本錯誤摘要、log 路徑、產出物清單。
- 介面：表格，可篩選（Workspace、結果、日期）和排序。
- 匯出：CSV 或 JSON，可選匯出範圍。
- 清除：勾選要刪的紀錄，連同該 Job 的 `jobs\<jobId>\` 資料夾（log、AI 回答）一併移除，後端要求 `confirm: true` 才執行；排队、執行中或進行 AI 分析的 Job 會被拒絕（409）；也可設「保留最近 N 天／N 筆」自動清理（預設關閉）。
- 保留規則會在儲存設定或建置結案時套用到歷史紀錄，並連帶清理脫群的 Job 目錄；Web 啟動時也會清掉超過 24 小時且沒有歷史參照的孤兒目錄。

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
| `send_enter` | 對卡在輸入提示（帳號／密碼／passphrase）的執行中 Job 送一次 Enter |
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
| `export-pack --out <檔> [--recipe …] [--paramset …] [--preset 表/preset …] [--artifact-rule …] [--all]` | 匯出設定包（`.brpack.json`） |
| `import-pack <檔> [--on-conflict overwrite|rename|skip] [--dry-run]` | 匯入設定包；有名稱重複又未指定處理方式時不寫入，exit 4 |

exit code 照 TOOL-SPEC（0 成功、1 失敗、3 參數錯、4 取消、5 環境不符）。

## 5. Web API（除共通 API 外）

| 方法與路徑 | 用途 |
| --- | --- |
| `GET/POST/DELETE /api/workspaces` | Workspace 管理；GET 不吃 `git status`（大 repo 慢），未提交變更要由 `GET /api/workspaces/dirty`（並行檢查各 Workspace）取；`POST /api/workspaces/import-worktrees` |
| `POST /api/workspaces/<id>/git` | `{ action: refresh|pull|checkout|submodule, branch }`；checkout 的 `branch` 也接受本機可解析的 commit SHA（切 detached HEAD），另可帶 `force: true`（`git checkout -f`，丟棄未提交變更、取消進行中的 sequencer 操作）。任務完成後的 `workspace` 含 `gitOperation`（revert/rebase/… 進行中標記） |
| `GET /api/workspaces/<id>/commits?ref=&limit=` | Build 頁「選擇 Branch／Commit…」：列 `ref`（預設 HEAD，可填 branch／remote／SHA，經 `resolve_checkout` 解析）上最新最多 200 筆 commit，回傳 `{ ref, resolved, commits: [{ sha, short, date, author, subject }] }` |
| `GET /api/workspaces/<id>/branches` | Branch 挑選表格：`for-each-ref --sort=-committerdate --format=…iso8601-strict…`（排除 symref；iso8601-strict 不受 zh-CN locale 影響），回傳 `{ branches: [{ ref, name, group, remote, date, authorDate, author, sha, subject }] }`，group 為 `heads`／`remotes/<remote>`；非 Git 資料夾或 Git 失敗時回 `{ branches: [], error }`（HTTP 200，讓對話框仍能開） |
| `GET/POST/DELETE /api/paramsets`、`POST /api/paramsets/import` | 參數表 |
| `GET/POST/DELETE /api/recipes` | Recipe |
| `GET/POST/DELETE /api/artifact-rules`、`POST /api/artifact-rules/preview` | 產出物規則（GET 第一筆為唯讀內建 `@builtin:bios-rom-package`） |
| `GET /api/packs/inventory`、`POST /api/packs/export`、`POST /api/packs/preview`、`POST /api/packs/import` | 設定包：列出可匯出項目、匯出（回傳 JSON 內容與檔名）、預覽匯入計畫（`decisions` 為 `{ "<section>:<名稱>": "overwrite|rename|skip" }`）、寫入（需 `confirm: true`，覆蓋前備份到 `backups\`） |
| `POST /api/jobs/check` | 建立前檢查，回傳警告清單（同資料夾已有 ACTIVE Job → `QUEUE_SAME_WORKSPACE`：會排隊依序執行，不拒絕） |
| `POST /api/jobs` | 建立 Job（可多個 Workspace）；選填 `checkout: { target, fetch, submodule, force }` 或 `checkouts: [...] × 1..30`（多目標佇列，陣列順序＝建置順序），Runner 建置前先切到該 branch／SHA 並更新 submodule（force 時 `git switch -f` 丟棄未提交變更），preview 每筆 plan 含 `checkoutTarget` |
| `GET /api/jobs`、`GET /api/jobs/<id>` | 列表／狀態 |
| `GET /api/jobs/<id>/log?from=<offset>` | 增量 log；`?download=1` 下載 `.log` 檔 |
| `GET /api/jobs/<id>/search?q=&kind=error|fail|warning` | 搜尋 |
| `GET /api/jobs/<id>/triage`、`POST /api/jobs/<id>/ai` | 判讀／AI 分析（ai POST 可帶 `logScope`/`logStart`/`logEnd`，`around` 另可帶 `logErrorLine`/`logBefore`/`logAfter`，`preview: true` 只回提示詞；AI 功能開關關閉時 POST 回 409 `AI_DISABLED`） |
| `POST /api/jobs/<id>/stop` | 停止 |
| `POST /api/jobs/<id>/restart` | 讓執行中、排隊中、已停止或失敗的 Job 從原執行計畫第一步重新建置，需 `confirm: true`；新 Job 以 `restartedFrom`／`restartedAs` 與原 Job 互連，重複呼叫冪等回傳同一新 Job。若新 Job 發布前中断，暫存檔由 orphan 清理（≤24 小時），重新按「重新建置」會重建 |
| `POST /api/jobs/<id>/enter` | 對執行中 Job 送一次 Enter（非執行中回 409，不需 confirm） |
| `GET /api/history`、`POST /api/history/export`、`POST /api/history/delete` | 歷史（delete 要 `confirm: true`） |
| `GET/POST/DELETE /api/schedules` | 排程 |
| `GET/POST /api/settings` | 設定（同時數量、互動提示自動送 Enter、AI 功能開關與送法端點、提示詞範本、通知、保留天數） |

安全規則全部照 TOOL-SPEC 第 5 節（Host 檢查、`X-Toolbox-Request`、Origin、CSP、白名單靜態檔、body 上限）。
TOOL-SPEC 寫「同一時間只允許一個會寫入的工作」：本工具的 build Job 例外，改由上面的衝突檢查管理；設定檔寫入仍要加鎖。

## 6. 介面（ui-kit 3.0，上方分頁）

| 分頁 | 內容 |
| --- | --- |
| 總覽 | 正在跑／排隊的 Job 卡片（進度、步驟、耗時、停止和重新建置），最近結果 |
| Workspace | 資料夾清單、git 狀態、新增／匯入 worktree、勾選後「開始 build」 |
| Build | 選 Workspace（多選）＋ Recipe ＋ 參數（分組勾選＋說明＋搜尋＋指令預覽）→ 衝突檢查 → 開始 |
| Log | 選 Job，即時 log、error／fail／warning 快速搜尋、判讀摘要、AI 分析（功能開關關閉時隱藏）、偵測到輸入提示時顯示「按 Enter 繼續」 |
| 流程設定 | Recipe、參數表匯入、preset、產出物規則（含預覽） |
| 歷史 | 表格、篩選、匯出、勾選刪除 |
| 工作排程 | 新增、啟用、停用或刪除每日或每週的 Windows 排程 |
| 設定 | 互動提示（autoSkipPrompts）、AI（功能開關預設關、送法 OpenCode／自訂端點／複製）、通知、保留規則 |

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
