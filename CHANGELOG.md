# Changelog

## [0.4.5] - 2026-11-13

### 新增

- **產出物規則可錨定到 repo 根**：每條規則新增 `root`（`workspace` 預設，或 `repo`＝`git rev-parse --show-toplevel` 的工作樹頂層）。Dell 平台的 Workspace 是平台資料夾（`builda.bat` 所在）但 `Build\` 產出在 repo 根，原本被鎖在 Workspace 內的 glob 因此挑不到檔；改用 `root: "repo"` 後可直接挑 `Build/DT/<平台>/RELEASE_CLANGPDB/ReleaseBIOS/*.rom`。 glob 仍不得含 `..`、盤符或絕對路徑（不得逃出錨點），相對 `dest` 落在錨點下、絕對路徑照舊。規則編輯器加「資料來源」選單，內建 BIOS ROM 打包規則改採 `root: "repo"`。

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
