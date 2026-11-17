# Changelog

## [0.6.0] - 2026-11-17

### 新功能

- **建置前 Checkout**：Build 頁可填「Build 目標」（branch、remote branch 或 commit SHA），Runner 會先切到該修訂再建置，預設一併執行 `git submodule update --init --recursive`（可取消勾選），並可選擇先 `git fetch origin --prune`。留空則直接在目前位置建置。目標找不到或工作區為髒樹時，Job 在 Checkout 步驟失敗，不會建置到錯誤的修訂。
- **同 Workspace 排隊建置**：兩個 Job 都自帶 Build 目標時，同一資料夾（或同一 Git repo）不再視為衝突，可排隊；調度器會依序一次執行一個 checkout Job，避免互搶工作區。
- Workspace 卡的切換框與 Build 目標採同一套解析規則：完整 ref、顯示名稱（如 `main`、`origin/main`）與 commit SHA（4–40 位十六進位）皆可，local 與 remote 同名時以 local 優先。
- Build 預覽、Job 卡片與建置 log 會顯示 checkout 目標與執行進度。
- 「選擇 Branch／Commit…」（仿 TortoiseGit 選 log 對話框）：左側多層樹依分支名的 `/` 逐層分組（heads／remotes／各 remote 預設展開，群組與葉分支混排、可逐層收合），右側分支表格（Branch Name、Date Last Commit、Last Author Date、Last Commit），上方 Filter 可依名稱、作者、SHA、commit 訊息即時過濾（樹同步縮減）；點分支列可看該分支最新 100 筆 commits。可勾選多個 branch／commit 排成 Build 佇列，逐筆用 ↑↓ 調整順序或移除（手動編輯 Build 目標欄會先確認才清佇列）；送出後依順序對每個目標各建一個 Job，同一 Workspace 依序 checkout＋建置。樹與表格字級加大，branch 清單日期改用不受中文語系影響的格式。

### 修正

- 建置、衝突檢查、Recipe、參數表與 Workspace 的錯誤訊息改為正體中文。
- 由 Hub 啟動時，自動檢查更新不再回傳 403，瀏覽器主控台不再出現錯誤訊息。

## [0.5.4] - 2026-11-15

- 獨立版更新儲存在啟動資料夾下的 `updates/`。搬移整個資料夾時會保留目前版本。
- 從 Hub 的工具資料夾直接啟動時，會使用同一份 Hub 更新目錄。

## [0.5.3] - 2026-11-15

- 修正：玻璃主題下，下拉選單展開後的選項看不清楚；選項改用不透明的頁面底色與一般文字顏色（同步 ui-kit）。

## [0.5.2] - 2026-11-15

- 嵌入時跟隨 Hub 共用主題，玻璃分頁列、主要面板、設定包對話框與通知使用一致外觀。
- 獨立開啟使用石墨深淺色，通知在沒有內容時會隱藏。

## [0.5.0] - 2026-11-13

### 新增

- **設定包匯出／匯入**（`.brpack.json`，`format: build-runner-pack`，`version: 1`）：Recipe、參數表、preset、產出物規則可逐項勾選匯出成單一 UTF-8 JSON，不含 Workspace 路徑；匯出時對絕對路徑與目前使用者名稱提出警告。
  - Web：**流程設定** 右上角新增「匯出設定包」與「匯入設定包」對話框。匯入會先驗證格式與版本，列出新增與名稱重複的項目，逐項選擇覆蓋／改名（自動加 `-2`）／略過後才寫入。
  - 覆蓋前把原檔備份到 `backups\pack-import-<時間>\`；改名的參數表或產出物規則會同步改寫設定包內 Recipe 與 preset 的引用。
  - preset 隨設定包獨立傳遞，可併入既有參數表；所屬參數表不存在時略過並提示。Recipe 引用的參數表、產出物規則或 preset 不存在時也會提示。
  - CLI：`export-pack --out <檔> [--recipe …] [--paramset …] [--preset 表/preset …] [--artifact-rule …] [--all]`、`import-pack <檔> [--on-conflict overwrite|rename|skip] [--dry-run]`；有名稱重複又未指定處理方式時不寫入並回傳 exit code 4（`CONFLICT_DECISION_REQUIRED`）。
  - API：`GET /api/packs/inventory`、`POST /api/packs/export`、`POST /api/packs/preview`、`POST /api/packs/import`（需 `confirm: true`）。
- 範例 `examples/sample.brpack.json`；測試 `tests/test_packs.py`（round-trip、三種衝突處理、逐項決定、壞檔、dry-run、CLI）。

### 變更

- **切換分支改用自製下拉清單**：原本的 input＋`<datalist>` 在 Chromium 會畫成深色泡泡、只列出符合目前值的一筆；改成與 select 選單同外觀的清單（深淺色皆可），分「本地」「遠端」兩組，預設帶入並標示目前分支，仍可輸入關鍵字過濾；方向鍵移動、Enter 選取並切換、Esc 關閉，右側箭頭可展開全部分支。
- **流程設定 → 參數表與 preset 可瀏覽完整參數清單**：選參數表後在摘要下方列出全部參數，依分組可展開／收合（預設展開第一組，另有「全部展開／收合」），每組標題附筆數；每列顯示旗標、類型（開關／帶值／選項及可選值）、說明與使用它的 preset 標籤。可搜尋旗標與說明（顯示「顯示 x / y 項」並自動展開命中的組），也可只看某個 preset 設定的參數與值。唯讀，不在此編輯。
- 文件與測試裡的平台專屬字樣改為中性寫法（「OEM 平台」、測試資料夾 `OemPkgs`）。
- `tool.json` 說明改為中文。
- 新增 `recipes.validate()`，供設定包匯入檢查 Recipe 結構。

## [0.4.8] - 2026-11-13

### 新增

- **Workspace 卡顯示進行中的 Git 操作**：`git_state()` 偵測各 worktree 的 gitdir 內 `rebase-merge|rebase-apply`／`REVERT_HEAD`／`CHERRY_PICK_HEAD`／`MERGE_HEAD`／`BISECT_LOG` 標記檔，新增 `gitOperation` 欄位，卡片顯示紅色標章（如「Revert 進行中」）。Git 在 revert／cherry-pick／rebase 未完成時**拒絕任何分支切換**，以前只能按了切換失敗才知道；偵測沿用既有 `rev-parse` 行程順帶取 `--absolute-git-dir`，零額外 Git 行程。
- **強制切換分支**：切換分支區新增「強制」核取方塊，送出 `force: true` 後端改走 `git checkout -f`（local）／`git checkout -f --track`（remote）。選 `checkout -f` 而非 `switch --discard-changes` 是因為後者對 sequencer 擋鎖一樣被拒（實測）；代價是**未提交的追蹤檔案變更永久丟棄**、進行中的 revert／cherry-pick／rebase 被取消（untracked 檔保留），confirm 文案對此明確警告；偵測到進行中操作而未勾強制時，確認框會先提示 Git 會拒絕並建議在終端機以 `--quit` 結束該操作以保留變更。

### 變更

- 分支切換的 Git 行程逾時從 10 秒放寬為 600 秒：大型 monorepo（整樹 checkout）切分支常超過 10 秒。

## [0.4.7] - 2026-11-13

### 新增

- **Workspace 卡「切換分支」改為可直接搜尋的輸入框**：分支動輒上百條且名稱很長，原本的原生 select 難找。現在是 input＋datalist：直接輸入關鍵字即由瀏覽器即時過濾（含子字串比對），Enter 或按「切換」送出。候選顯示去 `refs/heads|remotes` 前綴的短名，送出前解析回完整 ref（貼上完整 ref 亦可；local／remote 同名優先 local；不在清單時提示先「重新整理 Git」）。
- **切換分支後詢問是否更新 Submodule**：切換成功後跳出詢問（`git submodule update --init --recursive`，新 Git action `submodule`，30 分鐘上限、需 confirm）。只有 repo 查得「有 submodule」或查不到時才詢問，無子模組的 repo 不打擾；選擇即執行，不預設跑。409「已有 Git 工作正在執行」現在會附帶當前動作名（如 checkout／submodule）。

### 修復

- **Git 逾時的錯誤訊息不再誤報「找不到 Git」**：`workspaces.git()` 原本把 `TimeoutExpired` 與 `OSError` 混在同一個 `GIT_UNAVAILABLE`；現在逾時獨立為 `GIT_TIMEOUT`，並提示背景可能仍有 Git 行程（與殘留的 `index.lock`）。

## [0.4.6] - 2026-11-13

### 修復

- **產出物規則「Build 產出資料夾」的資料夾挑選器跟隨資料來源**：規則選「資料來源＝Repo 根」時，📁 挑選器原本仍鎖在 Workspace 根瀏覽（`/api/browse` relative 模式只認 Workspace），而 OEM 平台的 `Build\` 產出在 repo 根、Workspace 外面，根本走不到，只能手動打字。現在挑選器會以 `anchor: "repo"` 請求，後端把瀏覽範圍錨到 `git rev-parse --show-toplevel` 的工作樹頂層（Workspace 不是 checkout 時退回 Workspace 根），選取的資料夾依 repo 根轉成相對路徑寫回；選單與非 relative 瀏覽行為不變，超出範圍仍被擋下。

## [0.4.5] - 2026-11-13

### 新增

- **產出物規則可錨定到 repo 根**：每條規則新增 `root`（`workspace` 預設，或 `repo`＝`git rev-parse --show-toplevel` 的工作樹頂層）。OEM 平台的 Workspace 是平台資料夾（`builda.bat` 所在）但 `Build\` 產出在 repo 根，原本被鎖在 Workspace 內的 glob 因此挑不到檔；改用 `root: "repo"` 後可直接挑 `Build/DT/<平台>/RELEASE_CLANGPDB/ReleaseBIOS/*.rom`。 glob 仍不得含 `..`、盤符或絕對路徑（不得逃出錨點），相對 `dest` 落在錨點下、絕對路徑照舊。規則編輯器加「資料來源」選單，內建 BIOS ROM 打包規則改採 `root: "repo"`。

### 變更

- **Workspace 列表秒開**：`GET /api/workspaces` 不再跑 `git status`（幾十 GB 的 repo 會卡好幾秒），「有未提交變更／工作樹乾淨」改由新端點 `GET /api/workspaces/dirty`（最多 4 併發檢查各 Workspace）在背景補齊；同一時間只會有一輪掃描。
- **總覽 Job 卡移除「送 Enter」按鈕**：手動送 Enter 改由 Log 頁偵測到提示時的「按 Enter 繼續」橫幅與 MCP `send_enter` 承擔；API 與自動跳過不受影響。

## [0.4.4] - 2026-11-13

### 新增

- **Job 重新建置**：`POST /api/jobs/<id>/restart`（需 `confirm: true`）用原 Job 的執行計畫（含參數與產出物規則）建立新 Job，從第一步重新執行；執行中或排隊中的 Job 會先停止，原 Job 與 log 保留，並以 `restartedFrom`／`restartedAs` 互連。重複送出採冪等，回傳已建立的新 Job。總覽卡片與 Log 頁新增停止／重新建置按鈕。
- **AI 分析「錯誤附近」可選錯誤行**：可指定錯誤行與前後行數（各 0–1000），錯誤行選單從搜尋結果每頁 100 筆分頁載入；未選時仍以判讀出的根本錯誤為中心。
- **內建 Clean + Build + Package Recipe 與 BIOS ROM 產出物規則**：建置成功後依內建規則整理 ROM 並壓縮；兩者唯讀，可在流程設定頁複製或載入為可編輯的複本。產出物編輯器新增「Build 產出資料夾（相對 Workspace）」欄位，儲存時自動合成完整 glob。
- **工作排程獨立成頁**；設定頁保留通知、AI 與歷史保留。

### 變更

- 每個 build 步驟改在隱藏的新主控台執行，讓不經 cmd.exe 的步驟也有可注入 Enter 的真實主控台；停止工作與 git 查詢不再閃出視窗。
- Log 判讀摘要改為可摺疊；Log 查看區可拖曳右下角調整高度；Workspace 與 Build 的選取卡片整卡可點；功能說明改為單一對話框按鈕。
- 與內建同名的自訂 Recipe：依名稱查詢一律先解析到使用者檔案，內建只透過 `@builtin:` ID 或無同名檔案時命中，自訂流程不會被內建遮蔽。

## [0.4.3] - 2026-11-12

### 變更

- 頂部改用與 Git Ref Manager 一致的兩列配置，顯示實際 port 與連線狀態，並使用圖示按鈕切換主題。

## [0.4.2] - 2026-11-12

### 變更

- **AI 分析預設關閉**：新增設定頁「AI 分析設定」顶部的「啟用 AI 分析功能」開關（預設關）。關閉時 Log 頁的 AI 面板與歷史頁的「AI 分析」按鈕都隱藏，`POST /api/jobs/<id>/ai`（含預覽與複製提示詞）回傳 409 `AI_DISABLED`；讀取已儲存結果的 GET API 不受影響，重新開啟開關後仍可檢視與繼續分析。關閉開關不會中止已經在跑的分析，進行中的任務會照常完成並儲存。打開開關後所有功能與原本相同。

## [0.4.1] - 2026-11-12

### 新增

- 送 Enter 改為雙通道：除了寫入 stdin pipe（`\r\n`），runner 也會 `AttachConsole` 附加到 build 步驟行程樹的主控台，以 `WriteConsoleInput` 注入 VK_RETURN。`getpass` 類提示（如 `Enter Your Username:`）直接從主控台讀字元、題目也不進 log，過去只有 pipe 通道會漏掉這種提示；現在任一通道送達即算成功。自動模式、手動「送 Enter」按鈕與 MCP `send_enter` 都走同一雙通道。

### 修正

- 跑測試時偶爾彈出「啟動 `git -C ... log -1 ...` 時發生錯誤 0x800700E8」：機器 global git config 啟用 GPG 簽章（`commit.gpgsign=true`）時，測試裡的裸 `git commit` 會觸發 gpg→pinentry 密碼對話框，無人應答導致簽章失敗並在 OS 層彈訊。測試建倉庫的 commit 一律加 `-c commit.gpgsign=false`，不再依賴執行環境的 git 設定。
- `workspaces.git()` 容錯放寬：git 行程啟動階段的任何 OS 錯誤（不再只有找不到 git）都降級為「無 git 資訊」， Workspace 徽章缺資料但不會拖垮整個 Job。

## [0.4.0] - 2026-11-12

### 新增

- 互動提示送 Enter：Build 停在 `Enter Your Username:`、`Enter password for ...:`、`Enter passphrase for ... (enter to skip):` 時不再卡死。
  - 自動模式（預設開）：runner 偵測到 log 尾端停在上述提示且輸出停止 1.5 秒後自動送 Enter（寫入 `\r\n` 讓工具以空回答略過）；每 Job 上限 5 次、冷卻 2 秒（連續提示可在數秒內全數放行），超過放棄並在 log 記錄 `too many input prompts`。設定頁「互動提示」可關閉。
  - 手動模式：執行中 Job 的卡片新增「送 Enter」按鈕，log 頁偵測到提示時顯示橫幅與「按 Enter 繼續」按鈕；不受自動開關影響。
  - MCP 新增 `send_enter` 工具；API 為 `POST /api/jobs/<id>/enter`（非執行中回傳 409）。
- 設定 API 新增 `autoSkipPrompts`（布林，預設 true）；非布林值回傳 400 `INVALID_PROMPT_SETTING`。

### 變更

- Build 步驟的 stdin 由 `DEVNULL` 改為 `PIPE`：過去靠 EOF 自行略過的工具，現在改由自動偵測與手動按鈕處理提示。

## [0.3.0] - 2026-11-11

### 新增

- AI 分析可選 log 範圍：提示詞的 log context 支援「錯誤附近 ±40 行（預設）／尾端 200 行／完整 log（自動截尾）／自訂起訖行號（上限 20000 行）」；新增「預覽提示詞」按鈕，送出前可先檢查實際代入的內容與字元數。
- log 下載：log 頁新增「下載 .log」連結，`GET /api/jobs/<id>/log?download=1` 以 `Content-Disposition: attachment` 送出，檔名為 `<workspace>-<branch>-<job前8碼>.log`（非 ASCII 字元自動取代）。
- 第二個內建 Recipe「Clean + Build (-nou)」：Clean 為 `builda.bat -cleanall -nou`、Build 為 `builda.bat -r -nou`，與原內建一樣唯讀，可用「複製成自己的 Recipe」編輯。

### 變更

- 內建 Recipe「Clean + Build」的 Clean 步驟改為 `builda.bat -cleanall`（原 `builda.bat cleanall`，與 builda.bat 旗標一致）。

### 修正

- 歷史頁「根本錯誤」欄：建置成功的紀錄不再顯示 triage 誤判的雜訊文字（改顯示「—」）；長錯誤文字以 2 行截斷，完整內容移到 title 提示，表格行高不再被撐爆。

## [0.2.0] - 2026-11-11

### 變更

- 歷史刪除語意：刪除紀錄時連同該 Job 的 `jobs/<id>/` 資料夾（含 `build.log`、AI 回答）一併移除，log 頁下拉選單立即同步消失；移除「一併刪除 log」勾選框。執行中或進行 AI 分析的 Job 會被擋下（409）。
- 保留規則（retention）與孤兒掃描會連同脫群的 Job 目錄一起清理，避免 `jobs/` 目錄無限累積。

### 修正

- 開啟已完成或長篇 log 不再整份重播：首屏改用檔尾 `?tail=` 讀取，卡頓問題解除。
- `search`／`triage` 增加依檔案大小與修改時間失效的快取，重複掃描同一大 log 不再每次全文重讀。
- DOM 保留窗由 20000 行降為 5000 行，長 log 捲動更順。

### 新增

- 寬螢幕配置：移除 `.shell` 的 1200px 上限，內容隨視窗撐滿，左右 padding 改 clamp 響應式。

## [0.1.0] - 2026-11-11

### 新增

- Windows Web、CLI 與 MCP 入口，共用 Workspace、Recipe、建置佇列、log 和歷史。
- 內建 Clean + Build Recipe，依序執行 `builda.bat cleanall` 和 `builda.bat -r`。支援參數表匯入、preset、衝突檢查和指令預覽。
- 增量 log、搜尋及錯誤判讀；產出物預覽、命名、複製與 ZIP。
- 歷史篩選、匯出、保留規則、通知與 Windows 工作排程。
- AI 提示詞複製、OpenCode 和相容端點分析；可取消並保存回答。
- Hub 自動掃描與嵌入模式。各分頁提供功能說明，操作回饋以浮動通知顯示。
