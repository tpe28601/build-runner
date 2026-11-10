import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliEntryTests(unittest.TestCase):
    def test_manifest_entry_is_directly_runnable_by_python(self):
        manifest = json.loads((ROOT / "tool.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.run(
                [sys.executable, str(ROOT / manifest["cli"]["entry"]), "--version"],
                cwd=temp, capture_output=True, encoding="utf-8", timeout=10,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), manifest["version"])


@unittest.skipUnless(os.name == "nt", "Tests exercise Windows batch files and taskkill")
class BuildRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temp, temp)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.folders = {}
        recipes = self.data / "recipes"
        recipes.mkdir(parents=True)
        workspaces = []
        for name in ("PlatformA", "PlatformB", "PlatformC"):
            workspace = self.root / f"{name} sample"
            workspace.mkdir()
            self.folders[name] = workspace
            shutil.copyfile(ROOT / "tests" / "fake_build.bat", workspace / "fake build.bat")
            workspaces.append({"id": name.lower(), "name": name, "path": str(workspace)})
        self.write(self.data / "workspaces.json", workspaces)
        self.write(self.data / "settings.json", {"maxConcurrentJobs": 1})
        for name in ("fail", "hold", "success", "gate"):
            self.write(recipes / f"{name}.json", {
                "name": name, "steps": [{"name": "Build", "argv": ["fake build.bat", name, "sample argument"]}],
                "env": {"BUILD_RUNNER_TEST_PYTHON": sys.executable},
            })

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def cleanup_temp(temp):
        # Windows may release descendant process file handles shortly after taskkill returns.
        deadline = time.monotonic() + 3
        while True:
            try:
                temp.cleanup()
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def cli(self, *args, code=0):
        run = subprocess.run(
            [sys.executable, "-m", "build_runner.cli", "--data-dir", str(self.data), "--json", *args],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def start(self, workspace, recipe):
        job_id = self.cli("run", "--workspace", workspace, "--recipe", recipe)["result"]["id"]
        self.addCleanup(self.stop_if_active, job_id)
        return job_id

    def stop_if_active(self, job_id):
        if self.cli("status", job_id)["result"]["state"] in ("queued", "running"):
            self.cli("stop", job_id)

    def test_wait_records_failed_build_and_triages_stderr(self):
        result = self.cli("run", "--workspace", "PlatformA", "--recipe", "fail", "--wait", code=1)
        status = result["result"]
        job = self.data / "jobs" / status["id"]
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exitCode"], 1)
        self.assertEqual(json.loads((job / "status.json").read_text(encoding="utf-8")), status)
        self.assertIn("error C2065", (job / "build.log").read_text(encoding="utf-8"))
        self.assertIn("Argument sample argument", (job / "build.log").read_text(encoding="utf-8"))
        report = self.cli("triage", status["id"])["result"]
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["rootError"]["location"], "sample.c:12")
        self.assertEqual(len(report["cascadeErrors"]), 2)
        record = self.cli("history")["result"][0]
        self.assertEqual(record["id"], status["id"])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["rootError"], report["rootError"])

    def test_stop_advances_queue_and_conflict_blocks_same_folder(self):
        first = self.start("PlatformA", "hold")
        queued = self.start("PlatformB", "success")
        conflict = self.cli("run", "--workspace", "PlatformA", "--recipe", "success", code=3)
        self.assertEqual(conflict["error"]["code"], "WORKSPACE_CONFLICT")
        self.assertEqual(self.cli("status", queued)["result"]["state"], "queued")
        self.assertEqual(self.cli("stop", first)["result"]["state"], "stopped")
        self.wait_for_success(queued)

    def wait_for_success(self, job_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.cli("status", job_id)["result"]["state"] == "success":
                return
            time.sleep(0.1)
        self.fail("Job did not finish")

    def test_runner_advances_queue_when_build_finishes(self):
        first = self.start("PlatformA", "gate")
        queued = self.start("PlatformB", "success")
        self.assertEqual(self.cli("status", queued)["result"]["state"], "queued")
        (self.folders["PlatformA"] / "release.txt").touch()
        self.wait_for_success(first)
        self.wait_for_success(queued)

    def test_increasing_limit_dispatches_queue_immediately(self):
        first = self.start("PlatformA", "hold")
        queued = self.start("PlatformB", "success")
        self.cli("concurrency", "0")
        self.wait_for_success(queued)
        self.assertEqual(self.cli("status", first)["result"]["state"], "running")

    def test_dead_runner_does_not_block_following_jobs(self):
        first = self.start("PlatformA", "gate")
        pid = self.cli("status", first)["result"]["pid"]
        ready = self.folders["PlatformA"] / "ready.txt"
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "Build child did not finish starting")
        killed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
        )
        self.assertEqual(killed.returncode, 0, killed.stderr)
        second = self.start("PlatformB", "success")
        self.wait_for_success(second)
        self.assertEqual(self.cli("status", first)["result"]["state"], "failed")

    @unittest.skipUnless(shutil.which("git"), "Requires Git")
    def test_different_worktrees_of_same_repo_warn_but_can_queue(self):
        main = self.folders["PlatformA"]
        linked = self.root / "Linked sample"
        for args in (
            ("init",),
            ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid",
             "commit", "--allow-empty", "-m", "Sample"),
            ("worktree", "add", "--detach", str(linked)),
        ):
            result = subprocess.run(["git", "-C", str(main), *args], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", linked / "fake build.bat")
        self.cli("workspace-add", "--name", "Linked", "--path", str(linked))
        self.start("PlatformA", "hold")
        result = self.cli("run", "--workspace", "Linked", "--recipe", "success")
        job_id = result["result"]["id"]
        self.addCleanup(self.stop_if_active, job_id)
        self.assertEqual(result["result"]["state"], "queued")
        self.assertEqual([warning["code"] for warning in result["warnings"]], ["SHARED_GIT_DIR"])

    def test_native_executable_can_be_relative_to_workspace(self):
        workspace = self.folders["PlatformA"]
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", workspace / "sample.exe")
        self.write(self.data / "recipes" / "native.json", {
            "name": "native", "steps": [{"argv": [r".\sample.exe", "fake build.bat"]}],
        })
        result = self.cli("run", "--workspace", "PlatformA", "--recipe", "native", "--wait")["result"]
        log = self.data / "jobs" / result["id"] / "build.log"
        self.assertIn(str(workspace / "fake build.bat"), log.read_text(encoding="utf-8"))

    def test_recipe_path_can_locate_native_tool(self):
        directory = self.root / "sample tools"
        directory.mkdir()
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", directory / "sample-tool.exe")
        self.write(self.data / "recipes" / "native.json", {
            "name": "native",
            "steps": [{"argv": ["sample-tool.exe", "fake build.bat"]}],
            "env": {"Path": str(directory)},
        })
        self.cli("run", "--workspace", "PlatformA", "--recipe", "native", "--wait")

    def test_history_write_failure_does_not_leave_job_running(self):
        (self.data / "history.jsonl").mkdir()
        job_id = self.start("PlatformA", "success")
        self.wait_for_success(job_id)
        self.assertTrue(self.cli("status", job_id)["result"]["historyError"])

    def test_dry_run_leaves_job_store_untouched(self):
        result = self.cli("run", "--workspace", "PlatformA", "--recipe", "success", "--dry-run")
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["result"]["argvPreview"], [["fake build.bat", "success", "sample argument"]])
        self.assertFalse((self.data / "jobs").exists())


if __name__ == "__main__":
    unittest.main()
