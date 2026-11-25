# Build Runner

在 Windows 本機執行建置 Recipe，管理 Workspace、參數、排程、產出物與建置紀錄。Web、CLI 和 MCP 共用同一份資料與 Job 佇列。

## 啟動

- 需要 Windows 和 Python 3.10 以上，使用 Python 標準函式庫。Git 狀態與 worktree 功能需要 Git for Windows。
- **從 Hub 開啟**：在 Dev-Toolbox 根目錄執行 `Open-Web.bat`，從側欄選 **Build Runner** 並按 **啟動**。Hub 會從 `tool.json` 自動找到工具。
- **單獨開啟**：執行 `Run-Web.bat`。服務只綁定 `127.0.0.1`，預設 port 為 `8740`。如果預設 port 無法使用，啟動時顯示的 `READY` 行會列出實際網址。
- **CLI**：執行 `Run-CLI.bat --help` 查看指令。也可從 Dev-Toolbox 根目錄執行 `Run-Hub.bat run build-runner workspaces --json`。

## 第一次建置

1. 在 **Workspace** 新增資料夾，或選取 Git repo 後匯入 worktree。卡片會顯示本機分支、最近的 commit 和工作樹狀態。勾選 Workspace 後，前往 **Build** 選擇建置目標。
2. 在 **流程設定 → Recipe** 查看內建 **Clean + Build**、**Clean + Build (-nou)** 或 **Clean + Build + Package**。打包範例會在清理與建置成功後套用 BIOS ROM 產出物規則。按 **複製為新 Recipe** 可調整 `builda.bat` 的工作子資料夾，並套用符合專案的產出物規則。
3. 在 **Build** 選取 Workspace 和 Recipe。「Build 目標」可填 branch、remote branch 或 commit SHA，Runner 會在建置前切到該修訂並預設更新 Submodule（可選先 `git fetch origin`）；留空則建置目前位置。工作區有未提交變更時 checkout 會失敗，需要時可勾「強制切樹」（會先跳確認框，改走 `git switch -f`：丟棄未提交的追蹤檔案變更，untracked 檔保留；選遠端分支而本機已有同名分支時，未勾會明確報錯，勾了則把本機分支重置到遠端最新）。按「選擇 Branch／Commit…」可像 TortoiseGit 一樣用左側 heads／remotes 樹與 Filter 找分支，勾選多個 branch 與 commit（點分支列可看該分支的 commits）排成 Build 佇列，逐筆用 ↑↓ 調整順序或移除，送出後每個目標各建一個 Job、同一 Workspace 依序 checkout＋建置。同一資料夾的 Job（不論有沒有 Build 目標）一律排隊依序執行，檢查指令與資料夾狀態後開始建置。
4. 建置在背景執行。在 **Log** 查看即時輸出、搜尋警告或錯誤，遇到輸入提示時可送出 Enter。執行中可從 **總覽** 或 **Log** 停止或重新建置。重新建置會用原流程從第一步開始，並保留原 Job 的紀錄。

遇到建置工具的路徑長度限制時，可以在 **Workspace → 短路徑設定** 按「使用建議路徑」填入同磁碟的短路徑，或自行輸入工作樹外的本機絕對路徑。建置時會建立或重用指向 Git 工作樹根目錄的 junction，非 Git 專案則指向 Workspace。建置步驟沿用原本的子目錄，Log 會顯示實際使用的短路徑。留空可停用，既有 junction 會保留供重複使用。

## 參數、產出物與排程

- 在 **流程設定 → 參數表與 preset** 匯入 Markdown、CSV、XLSX 或 JSON。設定 Recipe 使用該表後，可在 Build 頁分組搜尋、選取參數、套用 preset 並即時預覽指令。Markdown 的限制備註可另設為互斥規則。
- 在 **流程設定 → 產出物** 設定 glob、排除項、改名範本、目的地與 ZIP。手動整理會先顯示預覽。要在 build 成功後自動整理，將規則組連結到 Recipe。同名檔案會加序號。規則選「資料來源＝Repo 根」時，資料夾挑選器會從 Git 工作樹頂層開始瀏覽，選好的資料夾依 repo 根寫成相對路徑。
- 在 **工作排程** 管理每日或每週的 Windows 工作排程。排程使用 Windows 本機時間，透過 CLI 執行，即使 Web 或 Hub 已關閉也可建置。通知和歷史保留在 **設定** 管理。
- AI 分析預設關閉，先在 **設定 → AI 分析設定** 開啟「啟用 AI 分析功能」。開啟後在 **Log** 可複製 AI 提示詞，或將選定 Job 的相關 log 送往已設定的 OpenCode 或相容端點。在 Windows 使用 OpenCode 時，請設定 `.exe` 的絕對路徑。

## 設定包

設定包（`.brpack.json`）把 Recipe、參數表、preset 與產出物規則打包成一個 UTF-8 JSON 檔，方便在團隊間或換電腦時共用。設定包不含 Workspace 路徑；匯出時若 Recipe 或產出物規則含絕對路徑或目前的使用者名稱，會顯示提醒。

- **匯出**：在 **流程設定** 右上角按「匯出設定包」，逐項勾選要包含的內容後下載。
- **匯入**：按「匯入設定包」並選檔，會先檢查格式與版本，再列出要新增的項目和名稱重複的項目。名稱重複的項目逐一選擇「覆蓋」、「改名」（自動加 `-2`）或「略過」，確認後才寫入。被覆蓋的原檔會先備份到資料夾的 `backups\pack-import-<時間>\`。改名的參數表或產出物規則，設定包內引用它的 Recipe 會一併改用新名稱。
- Recipe 或 preset 引用的參數表、產出物規則、preset 不在設定包也不在本機時，匯出與匯入都會提示。
- 範例：[`examples/sample.brpack.json`](examples/sample.brpack.json)。

```powershell
.\Run-CLI.bat export-pack --out .\team.brpack.json --all
.\Run-CLI.bat export-pack --out .\platforma.brpack.json --recipe 'PlatformA Release' --paramset PlatformA
.\Run-CLI.bat import-pack .\examples\sample.brpack.json --dry-run
.\Run-CLI.bat import-pack .\team.brpack.json --on-conflict rename
```

`export-pack` 可重複指定 `--recipe`、`--paramset`、`--preset 參數表/preset`、`--artifact-rule`；只指定參數表時會一併帶上它的 preset。`import-pack` 遇到已存在的名稱時，必須用 `--on-conflict overwrite|rename|skip` 指定處理方式，否則不會寫入（exit code 4）。

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
