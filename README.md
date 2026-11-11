# Build Runner

在 Windows 本機執行建置 Recipe，管理 Workspace、參數、排程、產出物與建置紀錄。Web、CLI 和 MCP 共用同一份資料與 Job 佇列。

## 啟動

- 需要 Windows 和 Python 3.10 以上，使用 Python 標準函式庫。Git 狀態與 worktree 功能需要 Git for Windows。
- **從 Hub 開啟**：在 Dev-Toolbox 根目錄執行 `Open-Web.bat`，從側欄選 **Build Runner** 並按 **啟動**。Hub 會從 `tool.json` 自動找到工具。
- **單獨開啟**：執行 `Run-Web.bat`。服務只綁定 `127.0.0.1`，預設 port 為 `8740`。如果預設 port 無法使用，啟動時顯示的 `READY` 行會列出實際網址。
- **CLI**：執行 `Run-CLI.bat --help` 查看指令。也可從 Dev-Toolbox 根目錄執行 `Run-Hub.bat run build-runner workspaces --json`。

## 第一次建置

1. 在 **Workspace** 新增資料夾，或選取 Git repo 後匯入 worktree。
2. 在 **流程設定 → Recipe** 查看內建 **Clean + Build**（`builda.bat -cleanall` → `builda.bat -r`）與 **Clean + Build (-nou)**（兩步驟各加 `-nou`）。請確認 Workspace 根目錄有該批次檔，且你要先清理再建置。要使用不同的指令，按 **複製為新 Recipe** 後修改。
3. 在 **Build** 選取 Workspace 和 Recipe。檢查指令與資料夾衝突後開始建置。
4. 在 **Log** 查看即時輸出、搜尋警告或錯誤，或從 **歷史** 回看結果。每個分頁都有 **功能說明**。

## 參數、產出物與排程

- 在 **流程設定 → 參數表與 preset** 匯入 Markdown、CSV、XLSX 或 JSON。設定 Recipe 使用該表後，可在 Build 頁分組搜尋、選取參數、套用 preset 並即時預覽指令。Markdown 的限制備註可另設為互斥規則。
- 在 **流程設定 → 產出物** 設定 glob、排除項、改名範本、目的地與 ZIP。手動整理會先顯示預覽。要在 build 成功後自動整理，將規則組連結到 Recipe。同名檔案會加序號。
- 在 **設定** 管理每日或每週的 Windows 工作排程、通知與歷史保留。排程使用 Windows 的本機時間，透過 CLI 執行，即使 Web 或 Hub 已關閉也可建置。
- 在 **Log** 可複製 AI 提示詞，或將選定 Job 的相關 log 送往已設定的 OpenCode 或相容端點。在 Windows 使用 OpenCode 時，請設定 `.exe` 的絕對路徑。

## CLI 範例

在 Build Runner 資料夾執行。下列 `PlatformA` 與 `C:\src\sample` 為範例。

```powershell
.\Run-CLI.bat workspace-add --name PlatformA --path 'C:\src\sample'
.\Run-CLI.bat run --workspace PlatformA --recipe 'Clean + Build' --dry-run
.\Run-CLI.bat run --workspace PlatformA --recipe 'Clean + Build' --wait
.\Run-CLI.bat import-params .\examples\paramset-sample.json --name sample
.\Run-CLI.bat history --export csv --out .\build-history.csv
```

`--dry-run` 顯示建置指令。`--wait` 會等候建置完成並回傳結果。可加 `--json` 取得機器可讀的回應。

## OpenCode MCP

將以下設定合併到 `opencode.json` 的 `mcp`，把範例路徑換成實際的絕對路徑。

```json
{
  "mcp": {
    "build-runner": {
      "type": "local",
      "command": ["python", "C:/tools/Dev-Toolbox/tools/Build-Runner/mcp_server.py"],
      "enabled": true
    }
  }
}
```

MCP 可列出 Workspace、Recipe、參數和歷史，啟動或停止 Job，以及搜尋、讀取和判讀 log。建置使用既有的 Recipe 和參數表，單次讀 log 最多 400 行。

## 資料位置

預設為 `%LOCALAPPDATA%\DevToolbox\build-runner\`。如有設定 `DEVTOOLBOX_DATA`，工具會使用其下的 `build-runner` 資料夾。CLI、Web 與 MCP 也接受 `--data-dir`。若要讓不同入口共用自訂資料，請使用同一位置。

其他格式與行為見 [工具規格](docs/SPEC.md)。授權見 [LICENSE](LICENSE)。
