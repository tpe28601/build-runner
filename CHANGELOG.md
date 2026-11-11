# Changelog

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
