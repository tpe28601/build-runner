# Changelog

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
