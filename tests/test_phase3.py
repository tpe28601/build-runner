import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_runner import artifacts, paramsets
from build_runner.paths import ToolError


class ParameterTests(unittest.TestCase):
    def test_csv_import_parses_defaults_and_conflict_keys(self):
        table = paramsets.parse(b"flag,desc,default,conflicts\n-r,Release,true,d\n-d,Debug,false,\n",
                                "sample.csv", "sample")
        self.assertEqual(table["conflicts"], [["r", "d"]])
        self.assertEqual(paramsets.resolve(table)[0], ["-r"])
    def test_markdown_import_handles_types_groups_and_ignores_non_flags(self):
        source = """# Sample parameters
## 1. Build
| 參數 | 用途 | 依據（行號） |
|---|---|---|
| （無參數） | Default build | 10 |
| §-r§ / §--release§ | Release | 11 |
| §-mode A\\|B§ | Mode | 12 |
| §-n #§ | Count | 13 |
## 2. 已棄用的參數
| 參數 | 用途 |
|---|---|
| §-old§ | Unused |
## 3. 環境變數
| 變數 | 用途 |
|---|---|
| §SAMPLE§ | Environment setting |
## 使用限制備註
- Consult the Workspace documentation.
""".replace("§", chr(96))
        table = paramsets.parse(source.encode(), "sample.md", "sample")
        self.assertEqual([row["flag"] for row in table["params"]], ["-r", "--release", "-mode", "-n"])
        self.assertEqual(table["params"][2]["choices"], ["A", "B"])
        self.assertEqual(table["params"][3]["placeholder"], "#")
        self.assertEqual(table["params"][0]["group"], "Build")
        self.assertEqual(table["notes"], ["Consult the Workspace documentation."])

    def test_xlsx_reads_shared_strings_and_sparse_cells(self):
        workbook = io.BytesIO()
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("xl/workbook.xml",
                             '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                             '<sheets><sheet name="Sample" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels",
                             '<Relationships><Relationship Id="rId1" Target="worksheets/sample.xml"/></Relationships>')
            archive.writestr("xl/sharedStrings.xml", "<sst><si><t>flag</t></si><si><t>-r</t></si></sst>")
            archive.writestr("xl/worksheets/sample.xml", """
                <worksheet><sheetData>
                <row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>desc</t></is></c></row>
                <row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2" t="inlineStr"><is><t>Release</t></is></c></row>
                </sheetData></worksheet>""")
        table = paramsets.parse(workbook.getvalue(), "sample.xlsx", "sample")
        self.assertEqual([(row["flag"], row["desc"]) for row in table["params"]], [("-r", "Release")])

    def test_parameter_arguments_use_preset_and_override_in_table_order(self):
        table = paramsets.normalize({"name": "Sample", "params": [
            {"key": "r", "flag": "-r", "type": "switch", "default": True},
            {"key": "d", "flag": "-d", "type": "switch"},
            {"key": "mode", "flag": "-mode", "type": "choice", "choices": ["A", "B"]},
            {"key": "label", "flag": "-label", "type": "value"},
        ], "presets": {"sample": {"r": False, "mode": "A"}}, "conflicts": [["r", "d", "label"]]})
        args, values = paramsets.resolve(table, "sample", {"mode": "B", "label": "sample label"})
        self.assertEqual(args, ["-mode", "B", "-label", "sample label"])
        self.assertEqual(values["mode"], "B")
        with self.assertRaisesRegex(ToolError, "-r.*-d"):
            paramsets.resolve(table, None, {"d": True})
        with self.assertRaises(ToolError):
            paramsets.resolve(table, None, {"other": "A"})


class ArtifactTests(unittest.TestCase):
    def test_preview_and_execute_copy_and_zip_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            source = root / "Build"
            (source / "temp").mkdir(parents=True)
            (source / "sample.bin").write_bytes(b"sample")
            (source / "temp" / "hidden.bin").write_bytes(b"hidden")
            dest = Path(temp) / "out"
            dest.mkdir()
            (dest / "sample_A.bin").write_bytes(b"existing")
            config = artifacts.normalize({"name": "Sample", "rules": [{
                "select": ["Build/**/**/*.bin"], "exclude": ["**/temp/**"],
                "rename": "{name}_{param.mode}{ext}", "dest": str(dest),
                "zip": {"enabled": True, "file": "{workspace}.zip"},
            }]})
            plan = artifacts.preview(root, config, {"workspace": "PlatformA", "params": {"mode": "A"}})
            self.assertEqual([Path(row["destination"]).name for row in plan["files"]], ["sample_A_2.bin"])
            self.assertFalse((dest / "sample_A_2.bin").exists())
            (dest / "sample_A_2.bin").write_bytes(b"race")
            result = artifacts.execute(plan)
            self.assertEqual(result["errors"], [])
            self.assertEqual((dest / "sample_A_3.bin").read_bytes(), b"sample")
            self.assertEqual((dest / "sample_A_2.bin").read_bytes(), b"race")
            with zipfile.ZipFile(dest / "PlatformA.zip") as archive:
                self.assertEqual(archive.namelist(), ["sample_A_3.bin"])

    def test_invalid_glob_and_rename_are_rejected(self):
        with self.assertRaises(ToolError):
            artifacts.normalize({"name": "Sample", "rules": [{"select": ["../*.bin"], "dest": "out"}]})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sample.bin").touch()
            config = artifacts.normalize({"name": "Sample", "rules": [{
                "select": ["*.bin"], "dest": "out", "rename": "../{name}{ext}",
            }]})
            with self.assertRaises(ToolError):
                artifacts.preview(root, config, {})

    def test_repo_root_rule_collects_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            workspace = repo / "DellPkgs" / "Platform"
            roms = repo / "Build" / "DT" / "Pkg"
            roms.mkdir(parents=True)
            workspace.mkdir(parents=True)
            (roms / "bios.rom").write_bytes(b"rom")
            config = artifacts.normalize({"name": "ROM", "rules": [{
                "root": "repo", "select": ["Build/DT/Pkg/*.rom"], "dest": "Artifacts",
            }]})
            self.assertEqual(config["rules"][0]["root"], "repo")
            with self.assertRaises(ToolError):
                artifacts.preview(workspace, config, {})   # the context carries no repo root
            plan = artifacts.preview(workspace, config, {"repoRoot": str(repo)})
            self.assertEqual([row["source"] for row in plan["files"]], ["Build/DT/Pkg/bios.rom"])
            result = artifacts.execute(plan)
            self.assertEqual(result["errors"], [])
            self.assertEqual((repo / "Artifacts" / "bios.rom").read_bytes(), b"rom")
        with self.assertRaises(ToolError):
            artifacts.normalize({"name": "Bad", "rules": [{"root": "elsewhere", "select": ["a"], "dest": "out"}]})

    @unittest.skipUnless(os.name == "nt", "Uses a Windows build script")
    def test_cli_preset_reaches_runner_and_artifacts_are_recorded_after_success(self):
        tool = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "Workspace sample"
            workspace.mkdir()
            data = root / "data"
            (workspace / "sample-build.bat").write_text(
                '@echo off\nif not "%~1"=="-mode" exit /b 1\n'
                'if not "%~2"=="B" exit /b 1\n'
                'if not exist Build mkdir Build\n>Build\\sample.bin echo Sample\nexit /b 0\n',
                encoding="utf-8",
            )
            for path, record in {
                "workspaces.json": [{"id": "platform-a", "name": "PlatformA", "path": str(workspace)}],
                "recipes/sample.json": {"name": "sample", "paramset": "sample", "artifacts": "sample",
                                        "steps": [{"argv": ["sample-build.bat"], "useParams": True}]},
                "artifacts/sample.json": {"name": "sample", "rules": [{"select": ["Build/*.bin"], "dest": str(root / "out"),
                    "rename": "{name}_{param.mode}{ext}", "zip": {"enabled": True, "file": "{workspace}.zip"}}]},
                "source.json": {"name": "Source", "params": [{"flag": "-mode", "type": "choice", "choices": ["A", "B"]}],
                                "presets": {"sample": {"mode": "B"}}},
            }.items():
                target = data / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(record), encoding="utf-8")

            def cli(*args, code=0):
                run = subprocess.run([sys.executable, "-m", "build_runner.cli", "--data-dir", str(data), "--json", *args],
                                     cwd=tool, capture_output=True, encoding="utf-8", timeout=15)
                self.assertEqual(run.returncode, code, run.stdout + run.stderr)
                return json.loads(run.stdout)["result"]

            cli("import-params", str(data / "source.json"), "--name", "sample", "--dry-run")
            self.assertFalse((data / "paramsets" / "sample.json").exists())
            cli("import-params", str(data / "source.json"), "--name", "sample")
            status = cli("run", "--workspace", "PlatformA", "--recipe", "sample", "--wait", code=1)
            self.assertEqual(status["state"], "failed")
            self.assertFalse((root / "out").exists())
            status = cli("run", "--workspace", "PlatformA", "--recipe", "sample", "--preset", "sample", "--wait")
            self.assertEqual(status["argv"], [["sample-build.bat", "-mode", "B"]])
            self.assertEqual(status["artifactErrors"], [])
            self.assertEqual({Path(value).name for value in status["artifacts"]}, {"sample_B.bin", "PlatformA.zip"})
            self.assertEqual(cli("history")[-1]["artifacts"], status["artifacts"])
