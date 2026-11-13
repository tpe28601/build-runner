import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from build_runner import packs  # noqa: E402
from build_runner.paths import ToolError, write_json  # noqa: E402

TABLE = {
    "name": "tableA", "command": "build.bat",
    "params": [
        {"key": "release", "flag": "-r", "type": "switch", "group": "Build", "desc": "Release"},
        {"key": "debug", "flag": "-d", "type": "switch", "group": "Build", "desc": "Debug"},
        {"key": "mode", "flag": "-mode", "type": "choice", "choices": ["A", "B"]},
    ],
    "conflicts": [["release", "debug"]],
    "presets": {"rel": {"release": True, "mode": "A"}, "dbg": {"debug": True}},
}
RECIPE = {"name": "Build A", "paramset": "tableA", "artifacts": "out",
          "steps": [{"name": "Build", "argv": ["build.bat"], "useParams": True, "preset": "rel"}],
          "env": {}, "timeoutMin": 30}
RULE = {"name": "out", "rules": [{"select": ["Build/**/*.bin"], "dest": "Artifacts", "rename": "{name}{ext}"}]}


class PackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        write_json(self.source / "paramsets" / "tableA.json", TABLE)
        write_json(self.source / "recipes" / "Build A.json", RECIPE)
        write_json(self.source / "artifacts" / "out.json", RULE)

    def exported(self):
        pack, warnings = packs.export_pack(self.source, include_all=True, name="sample", tool_version="0.0.0")
        return packs.parse(packs.dumps(pack)), warnings

    def read(self, folder, name):
        return json.loads((self.target / folder / f"{name}.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {str(path.relative_to(self.target)): path.read_bytes()
                for path in self.target.rglob("*") if path.is_file() and not path.name.endswith(".lock")}

    def test_round_trip_reproduces_records(self):
        pack, warnings = self.exported()
        self.assertEqual(warnings, [])
        self.assertEqual({key: len(pack[key]) for key in packs.SECTIONS},
                         {"artifactRules": 1, "paramsets": 1, "presets": 2, "recipes": 1})
        self.assertEqual(pack["paramsets"][0]["presets"], {})   # presets travel in their own section
        result = packs.apply(self.target, pack)
        self.assertEqual(result["unresolved"], [])
        self.assertIsNone(result["backups"])
        self.assertEqual(self.read("recipes", "Build A"), RECIPE)
        self.assertEqual(self.read("artifacts", "out")["rules"][0]["select"], ["Build/**/*.bin"])
        table = self.read("paramsets", "tableA")
        self.assertEqual(table["presets"], TABLE["presets"])
        self.assertEqual(table["conflicts"], TABLE["conflicts"])

    def test_export_selection_and_explicit_presets(self):
        pack, warnings = packs.export_pack(self.source, recipe_names=["Build A"], paramset_names=["tableA"],
                                           preset_ids=["tableA/dbg"])
        self.assertEqual([row["name"] for row in pack["presets"]], ["dbg"])
        self.assertEqual(pack["artifactRules"], [])
        # The rule is still on this machine, so the reference is not reported as missing.
        self.assertEqual(warnings, [])
        with self.assertRaises(ToolError) as caught:
            packs.export_pack(self.source, recipe_names=["nope"])
        self.assertEqual(caught.exception.code, "RECORD_NOT_FOUND")
        with self.assertRaises(ToolError) as caught:
            packs.export_pack(self.source)
        self.assertEqual(caught.exception.code, "EMPTY_PACK")

    def test_conflict_requires_a_decision(self):
        pack, _ = self.exported()
        packs.apply(self.target, pack)
        before = self.snapshot()
        with self.assertRaises(ToolError) as caught:
            packs.apply(self.target, pack)
        self.assertEqual(caught.exception.code, "CONFLICT_DECISION_REQUIRED")
        self.assertIn("recipes:Build A", caught.exception.detail["conflicts"])
        self.assertEqual(self.snapshot(), before)

    def test_conflict_overwrite_backs_up_originals(self):
        pack, _ = self.exported()
        packs.apply(self.target, pack)
        write_json(self.target / "recipes" / "Build A.json", {**RECIPE, "timeoutMin": 99})
        result = packs.apply(self.target, pack, default="overwrite")
        self.assertEqual(self.read("recipes", "Build A")["timeoutMin"], 30)
        backup = Path(result["backups"])
        self.assertEqual(json.loads((backup / "recipes" / "Build A.json").read_text(encoding="utf-8"))["timeoutMin"], 99)
        self.assertTrue((backup / "paramsets" / "tableA.json").is_file())

    def test_conflict_rename_rewrites_references(self):
        pack, _ = self.exported()
        packs.apply(self.target, pack)
        result = packs.apply(self.target, pack, default="rename")
        targets = {item["name"]: item["target"] for item in result["items"]}
        self.assertEqual(targets["Build A"], "Build A-2")
        self.assertEqual(targets["tableA"], "tableA-2")
        renamed = self.read("recipes", "Build A-2")
        self.assertEqual(renamed["paramset"], "tableA-2")
        self.assertEqual(renamed["artifacts"], "out-2")
        self.assertEqual(sorted(self.read("paramsets", "tableA-2")["presets"]), ["dbg", "rel"])
        self.assertEqual(self.read("recipes", "Build A"), RECIPE)   # original untouched

    def test_conflict_skip_leaves_existing_files(self):
        pack, _ = self.exported()
        packs.apply(self.target, pack)
        write_json(self.target / "recipes" / "Build A.json", {**RECIPE, "timeoutMin": 77})
        before = self.snapshot()
        result = packs.apply(self.target, pack, default="skip")
        self.assertEqual(result["written"], [])
        self.assertEqual(self.snapshot(), before)

    def test_per_item_decisions(self):
        pack, _ = self.exported()
        packs.apply(self.target, pack)
        decisions = {"recipes:Build A": "rename", "paramsets:tableA": "skip", "artifactRules:out": "skip",
                     "presets:tableA/rel": "overwrite", "presets:tableA/dbg": "skip"}
        result = packs.apply(self.target, pack, decisions)
        self.assertEqual([row["name"] for row in result["written"]], ["tableA", "Build A-2"])
        self.assertEqual(self.read("recipes", "Build A-2")["paramset"], "tableA")

    def test_preset_into_missing_table_is_skipped_with_warning(self):
        pack, _ = self.exported()
        pack["paramsets"] = []
        result = packs.apply(self.target, pack, dry_run=True)
        self.assertTrue(any(item["action"] == "missing-table" for item in result["items"]))
        codes = {row["code"] for row in result["warnings"]}
        self.assertIn("MISSING_PARAMSET", codes)

    def test_dry_run_writes_nothing(self):
        pack, _ = self.exported()
        result = packs.apply(self.target, pack, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["written"], [])
        self.assertFalse(self.target.exists())

    def test_bad_files(self):
        with self.assertRaises(ToolError) as caught:
            packs.parse(b"{not json")
        self.assertEqual(caught.exception.code, "INVALID_PACK")
        with self.assertRaises(ToolError) as caught:
            packs.parse(json.dumps({"format": "something-else", "version": 1}))
        self.assertEqual(caught.exception.code, "INVALID_PACK")
        with self.assertRaises(ToolError) as caught:
            packs.parse(json.dumps({"format": packs.FORMAT, "version": packs.VERSION + 1}))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_PACK_VERSION")
        with self.assertRaises(ToolError):
            packs.parse(json.dumps({"format": packs.FORMAT, "version": 1, "recipes": [{"name": "x", "steps": []}]}))
        with self.assertRaises(ToolError):
            packs.parse(json.dumps({"format": packs.FORMAT, "version": 1, "recipes": [{"name": "../evil", "steps": [{"argv": ["a"]}]}]}))

    def test_absolute_paths_are_reported(self):
        write_json(self.source / "recipes" / "Abs.json",
                   {"name": "Abs", "steps": [{"argv": ["C:\\tools\\build.bat"]}], "env": {}, "timeoutMin": 5})
        _, warnings = packs.export_pack(self.source, recipe_names=["Abs"])
        self.assertEqual([row["code"] for row in warnings], ["ABSOLUTE_PATH"])

    def test_example_pack_is_valid(self):
        pack = packs.parse((ROOT / "examples" / "sample.brpack.json").read_bytes())
        result = packs.apply(self.target, pack, dry_run=True)
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["warnings"], [])

    def test_cli_export_and_import_dry_run(self):
        out = self.root / "out" / "sample.brpack.json"
        run = lambda *args: subprocess.run([sys.executable, str(ROOT / "build_runner" / "cli.py"), *args, "--json"],
                                           capture_output=True, encoding="utf-8", timeout=30)
        exported = run("export-pack", "--out", str(out), "--all", "--data-dir", str(self.source))
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        self.assertTrue(out.is_file())
        dry = run("import-pack", str(out), "--dry-run", "--data-dir", str(self.target))
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertFalse(self.target.exists())
        imported = run("import-pack", str(out), "--data-dir", str(self.target))
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        again = run("import-pack", str(out), "--data-dir", str(self.target))
        self.assertEqual(again.returncode, 4)
        self.assertEqual(json.loads(again.stdout)["error"]["code"], "CONFLICT_DECISION_REQUIRED")
        renamed = run("import-pack", str(out), "--on-conflict", "rename", "--data-dir", str(self.target))
        self.assertEqual(renamed.returncode, 0, renamed.stdout + renamed.stderr)
        self.assertTrue((self.target / "recipes" / "Build A-2.json").is_file())


if __name__ == "__main__":
    unittest.main()
