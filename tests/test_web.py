import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.data = cls.root / "data"
        cls.data.mkdir()
        cls.server = subprocess.Popen(
            [sys.executable, str(ROOT / "build_runner" / "web.py"), "--port", "0", "--data-dir", str(cls.data)],
            cwd=cls.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        cls.addClassCleanup(cls.close_server)
        line = cls.server.stdout.readline().strip()
        if not line.startswith("READY http://127.0.0.1:"):
            raise AssertionError(f"Expected READY, got {line!r}: {cls.server.stderr.read()}")
        cls.port = urlsplit(line.removeprefix("READY ")).port

    @classmethod
    def close_server(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
        _, errors = cls.server.communicate(timeout=5)
        if errors:
            print(errors, file=sys.stderr)

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, payload, headers or {})
            response = conn.getresponse()
            raw = response.read()
            content = json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, content, dict(response.getheaders())
        finally:
            conn.close()

    def api(self, method, path, payload=None, code=200):
        headers = {"Content-Type": "application/json", "X-Toolbox-Request": "1"}
        status, result, _ = self.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        self.assertEqual(status, code, result)
        return result

    def test_health_static_assets_and_host(self):
        health = self.api("GET", "/api/health")
        self.assertEqual(health["port"], self.port)
        self.assertEqual(health["id"], "build-runner")
        self.assertEqual(self.api("GET", "/api/info")["dataDir"], str(self.data))
        status, body, headers = self.request("GET", "/?embedded=1")
        self.assertEqual(status, 200)
        self.assertIn(b"Build Runner", body)
        self.assertIn("frame-ancestors 'self' http://127.0.0.1:8700 http://localhost:8700",
                      headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.request("HEAD", "/app.js")[:2], (200, b""))
        self.api("GET", "/ui/../tool.json", code=404)
        status, result, _ = self.request("GET", "/api/health", headers={"Host": "invalid.example"})
        self.assertEqual((status, result["error"]["code"]), (403, "FORBIDDEN_HOST"))

    def test_listener_file_advertises_the_live_url(self):
        listener = json.loads((self.data / "listener.json").read_text(encoding="utf-8"))
        health = self.api("GET", "/api/health")
        self.assertEqual(listener["tool"], "build-runner")
        self.assertEqual(listener["url"], f"http://127.0.0.1:{self.port}")
        self.assertEqual(listener["port"], self.port)
        self.assertEqual(listener["port"], health["port"])
        self.assertEqual(listener["pid"], health["pid"])
        self.assertTrue(listener["startedAt"])

    def test_occupied_explicit_port_exits_without_ready(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "build_runner" / "web.py"), "--port", str(self.port),
             "--data-dir", str(self.data)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_folder_browser_lists_only_folders_and_keeps_relative_selection_inside_workspace(self):
        root = self.root / "Browse sample"
        child = root / "Source sample"
        child.mkdir(parents=True)
        (child / ".git").write_text("gitdir: sample", encoding="utf-8")
        (root / "sample.txt").touch()
        row = self.api("POST", "/api/workspaces", {"name": "Browse sample", "path": str(root)}, code=201)["result"]
        listing = self.api("POST", "/api/browse", {"path": str(root)})["result"]
        self.assertEqual(listing["parent"], str(root.parent))
        self.assertEqual(listing["items"], [{"name": child.name, "path": str(child), "hasGit": True}])
        scope = {"relative": True, "workspaceId": row["id"]}
        roots = self.api("POST", "/api/browse", {"relative": True})["result"]
        self.assertIn(row["id"], [item.get("workspaceId") for item in roots["items"]])
        listing = self.api("POST", "/api/browse", scope)["result"]
        self.assertIsNone(listing["parent"])
        self.assertEqual(listing["selection"], ".")
        listing = self.api("POST", "/api/browse", {**scope, "path": str(child)})["result"]
        self.assertEqual(listing["selection"], child.name)
        self.api("POST", "/api/browse", {**scope, "path": str(root.parent)}, code=400)

    def test_browse_repo_anchor_scopes_to_git_toplevel(self):
        repo = self.root / "Anchor repo"
        platform = repo / "PlatformPkg"
        build = repo / "Build" / "DT"
        build.mkdir(parents=True)
        platform.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        row = self.api("POST", "/api/workspaces", {"name": "Anchor repo", "path": str(platform)}, code=201)["result"]
        scope = {"relative": True, "workspaceId": row["id"], "anchor": "repo"}
        listing = self.api("POST", "/api/browse", scope)["result"]
        self.assertEqual(listing["rootPath"].lower(), str(repo.resolve()).lower())
        self.assertEqual(listing["selection"], ".")
        listing = self.api("POST", "/api/browse", {**scope, "path": str(build)})["result"]
        self.assertEqual(listing["selection"], "Build/DT")
        self.api("POST", "/api/browse", {**scope, "path": str(repo.parent)}, code=400)
        # Workspace 不是 Git checkout：anchor=repo 退回 Workspace 根
        plain = self.root / "Plain folder"
        plain.mkdir()
        row = self.api("POST", "/api/workspaces", {"name": "Plain folder", "path": str(plain)}, code=201)["result"]
        listing = self.api("POST", "/api/browse", {"relative": True, "workspaceId": row["id"], "anchor": "repo"})["result"]
        self.assertEqual(listing["rootPath"].lower(), str(plain.resolve()).lower())

    def test_history_delete_requires_confirmation_and_removes_job_folder(self):
        job_id = "a" * 32
        folder = self.data / "jobs" / job_id
        folder.mkdir(parents=True)
        log = folder / "build.log"
        log.write_text("sample log", encoding="utf-8")
        history = self.data / "history.jsonl"
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": job_id, "workspaceId": "sample", "workspace": "PlatformA",
                                     "state": "failed", "endedAt": "2026-11-01T00:00:00+00:00"}) + "\n")
        filtered = self.api("GET", "/api/history?workspace=sample&state=failed")["result"]
        self.assertEqual([item["id"] for item in filtered], [job_id])
        result = self.api("POST", "/api/history/export", {"format": "json", "ids": [job_id]})["result"]
        self.assertEqual(json.loads(result["content"]), filtered)
        self.api("POST", "/api/history/delete", {"ids": [job_id]}, code=400)
        self.assertTrue(log.exists())
        result = self.api("POST", "/api/history/delete", {"ids": [job_id], "confirm": True})["result"]
        self.assertEqual(result, {"deleted": 1, "deletedJobs": 1})
        self.assertFalse(folder.exists())

    def test_ai_settings_key_is_omitted_from_all_settings_responses(self):
        key = "sample-private-key"
        for result in (self.api("POST", "/api/settings", {"ai": {"apiKey": key}}),
                       self.api("GET", "/api/settings")):
            self.assertNotIn(key, json.dumps(result))
            self.assertTrue(result["result"]["ai"]["hasApiKey"])
        self.api("POST", "/api/settings", {"ai": {"clearApiKey": True}})

    def test_builtin_recipe_is_visible_and_read_only(self):
        builtins = self.api("GET", "/api/recipes")["result"][:3]
        self.assertEqual([record["name"] for record in builtins],
                         ["Clean + Build", "Clean + Build (-nou)", "Clean + Build + Package"])
        self.assertTrue(all(record["builtIn"] for record in builtins))
        self.assertEqual(builtins[0]["steps"][0]["argv"], ["builda.bat", "-cleanall"])
        self.assertEqual(builtins[1]["steps"][0]["argv"], ["builda.bat", "-cleanall", "-nou"])
        self.assertEqual(builtins[1]["steps"][1]["argv"], ["builda.bat", "-r", "-nou"])
        self.assertEqual([step["argv"] for step in builtins[2]["steps"]],
                         [["builda.bat", "-cleanall", "-nou"], ["builda.bat", "-r", "-nou"]])
        artifact = self.api("GET", "/api/artifact-rules")["result"][0]
        self.assertEqual(builtins[2]["artifacts"], artifact["id"])
        self.assertTrue(artifact["builtIn"])
        self.assertEqual(artifact["rules"][0]["root"], "repo")
        self.assertTrue(artifact["rules"][0]["zip"]["enabled"])
        self.api("POST", "/api/artifact-rules", {**artifact, "confirm": True}, code=400)
        self.api("DELETE", "/api/artifact-rules", {"name": artifact["id"], "confirm": True}, code=400)
        for record in builtins:
            self.api("POST", "/api/recipes", {**record, "confirm": True}, code=400)
            self.api("DELETE", "/api/recipes", {"name": record["id"], "confirm": True}, code=400)

    def test_log_download_and_ai_prompt_scope(self):
        job_id = "b" * 32
        folder = self.data / "jobs" / job_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(
            {"id": job_id, "workspace": "PlatformA", "branch": "feature/x", "state": "failed",
             "argv": ["builda.bat", "-r"], "queuedAt": "2026-11-01T00:00:00+00:00",
             "startedAt": "2026-11-01T00:00:01+00:00", "endedAt": "2026-11-01T00:10:00+00:00",
             "exitCode": 1}), encoding="utf-8")
        body = "".join(f"line {number}\n" for number in range(1, 11))
        body += "line 11 sample.c(9): error C2065: unknown\n"
        body += "line 12 before second error\nsecond.c(10): error C2143: missing token\nline 14 after second error\n"
        (folder / "build.log").write_bytes(body.encode("utf-8"))
        status, content, headers = self.request("GET", f"/api/jobs/{job_id}/log?download=1")
        self.assertEqual(status, 200)
        self.assertEqual(content, body.encode("utf-8"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Content-Disposition"],
                         f'attachment; filename="PlatformA-feature-x-{job_id[:8]}.log"')
        self.api("POST", "/api/settings", {"ai": {"enabled": True}})
        self.addCleanup(self.api, "POST", "/api/settings", {"ai": {"enabled": False}})
        prompt = self.api("POST", f"/api/jobs/{job_id}/ai",
                          {"method": "copy", "preview": True, "logScope": "range",
                           "logStart": 2, "logEnd": 4})["result"]["prompt"]
        self.assertIn("2: line 2", prompt)
        self.assertIn("4: line 4", prompt)
        self.assertNotIn("5: line 5", prompt)
        self.assertEqual(self.api("POST", f"/api/jobs/{job_id}/ai",
                                  {"method": "copy", "preview": True})["result"]["scope"], "around")
        selected = {"method": "copy", "preview": True, "logScope": "around",
                    "logErrorLine": 13, "logBefore": 1, "logAfter": 0}
        prompt = self.api("POST", f"/api/jobs/{job_id}/ai", selected)["result"]["prompt"]
        self.assertIn("12: line 12", prompt)
        self.assertIn("13: second.c", prompt)
        self.assertIn('"line": 13', prompt)
        self.assertNotIn("C2065", prompt)
        self.assertNotIn("14: line 14", prompt)
        self.api("POST", f"/api/jobs/{job_id}/ai", {**selected, "logErrorLine": 12}, code=400)
        self.api("POST", f"/api/jobs/{job_id}/ai", {**selected, "logBefore": -1}, code=400)
        self.api("POST", f"/api/jobs/{job_id}/ai", {"method": "copy", "preview": True,
                                                    "logScope": "everywhere"}, code=400)
        self.api("POST", f"/api/jobs/{job_id}/ai", {"method": "copy", "preview": True,
                                                    "logScope": "range", "logStart": "2"}, code=400)

    def test_ai_analysis_requires_the_settings_switch(self):
        job_id = "c" * 32
        folder = self.data / "jobs" / job_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(
            {"id": job_id, "workspace": "PlatformA", "branch": "main", "state": "failed",
             "argv": ["builda.bat", "-r"], "queuedAt": "2026-11-01T00:00:00+00:00",
             "startedAt": "2026-11-01T00:00:01+00:00", "endedAt": "2026-11-01T00:10:00+00:00",
             "exitCode": 1}), encoding="utf-8")
        (folder / "build.log").write_text("boom\n", encoding="utf-8")
        # Ships disabled: analysis requests, previews included, are refused.
        self.assertIs(self.api("GET", "/api/settings")["result"]["ai"]["enabled"], False)
        for payload in ({"method": "copy"}, {"method": "copy", "preview": True}):
            self.assertEqual(self.api("POST", f"/api/jobs/{job_id}/ai", payload, code=409)["error"]["code"],
                             "AI_DISABLED")
        # The switch turns the feature on and back off.
        self.assertIs(self.api("POST", "/api/settings", {"ai": {"enabled": True}})["result"]["ai"]["enabled"], True)
        self.assertIn("boom", self.api("POST", f"/api/jobs/{job_id}/ai",
                                       {"method": "copy", "preview": True})["result"]["prompt"])
        # Unrelated AI fields (API key edits) must leave the enable flag alone.
        self.assertIs(self.api("POST", "/api/settings", {"ai": {"apiKey": "sample"}})["result"]["ai"]["enabled"], True)
        self.assertIs(self.api("POST", "/api/settings", {"ai": {"clearApiKey": True}})["result"]["ai"]["enabled"], True)
        self.assertIs(self.api("POST", "/api/settings", {"ai": {"enabled": False}})["result"]["ai"]["enabled"], False)
        self.api("POST", f"/api/jobs/{job_id}/ai", {"method": "copy", "preview": True}, code=409)
        # Reading stored analyses keeps working while disabled; the flag must be a boolean.
        self.assertEqual(self.api("GET", f"/api/jobs/{job_id}/ai")["result"], [])
        self.assertIsNone(self.api("GET", f"/api/jobs/{job_id}/ai?active=1")["result"])
        self.assertEqual(self.api("POST", "/api/settings", {"ai": {"enabled": "yes"}}, code=400)["error"]["code"],
                         "INVALID_AI_SETTINGS")

    def test_enter_button_requires_running_job(self):
        running = "e" * 32
        stopped = "f" * 32
        dead = "0" * 32
        # The runner liveness check opens the recorded pid, so use a real live process.
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))  # LIFO-safe: kill before wait
        import build_runner.jobs as jobs_module  # noqa: PLC0415 - keep the module import local to this test
        with jobs_module.process_token(holder.pid) as token:
            pass
        for job_id, state, pid, pid_token in ((running, "running", holder.pid, token),
                                              (stopped, "stopped", 999999, None),
                                              (dead, "running", 999999, token)):
            folder = self.data / "jobs" / job_id
            folder.mkdir(parents=True)
            (folder / "status.json").write_text(json.dumps(
                {"id": job_id, "workspace": "PlatformA", "state": state, "pid": pid, "pidToken": pid_token,
                 "queuedAt": "2026-11-01T00:00:00+00:00"}), encoding="utf-8")
        self.assertEqual(self.api("POST", f"/api/jobs/{running}/enter", {})["result"], {"sent": True})
        self.assertTrue((self.data / "jobs" / running / "enter.request").is_file())
        self.assertEqual(self.api("POST", f"/api/jobs/{stopped}/enter", {}, code=409)["error"]["code"],
                         "JOB_NOT_RUNNING")
        self.assertFalse((self.data / "jobs" / stopped / "enter.request").exists())
        # A row still marked running whose runner died must not accept the request either.
        self.assertEqual(self.api("POST", f"/api/jobs/{dead}/enter", {}, code=409)["error"]["code"],
                         "JOB_NOT_RUNNING")
        self.assertFalse((self.data / "jobs" / dead / "enter.request").exists())
        # Retire the fake runner rows so later reconciles never see them as active.
        for job_id in (running, dead):
            (self.data / "jobs" / job_id / "status.json").write_text(json.dumps(
                {"id": job_id, "workspace": "PlatformA", "state": "stopped",
                 "queuedAt": "2026-11-01T00:00:00+00:00"}), encoding="utf-8")

    def test_auto_skip_prompts_setting_roundtrip(self):
        self.assertIs(self.api("GET", "/api/settings")["result"]["autoSkipPrompts"], True)
        self.assertIs(self.api("POST", "/api/settings", {"autoSkipPrompts": False})["result"]["autoSkipPrompts"], False)
        self.assertIs(self.api("GET", "/api/settings")["result"]["autoSkipPrompts"], False)
        self.assertEqual(self.api("POST", "/api/settings", {"autoSkipPrompts": "yes"}, code=400)["error"]["code"],
                         "INVALID_PROMPT_SETTING")
        self.assertIs(self.api("POST", "/api/settings", {"autoSkipPrompts": True})["result"]["autoSkipPrompts"], True)

    def test_write_guards_and_json_errors(self):
        headers = {"Content-Type": "application/json"}
        status, result, _ = self.request("POST", "/api/settings", "{}", headers)
        self.assertEqual((status, result["error"]["code"]), (403, "MISSING_TOOLBOX_HEADER"))
        headers["X-Toolbox-Request"] = "1"
        status, result, response = self.request("POST", "/api/settings", "{}", {**headers, "Origin": "http://invalid.example"})
        self.assertEqual((status, result["error"]["code"]), (403, "FORBIDDEN_ORIGIN"))
        self.assertFalse(any(key.lower().startswith("access-control-allow-") for key in response))
        status, _, response = self.request("OPTIONS", "/api/settings", headers={"Origin": "http://invalid.example"})
        self.assertEqual(status, 405)
        self.assertFalse(any(key.lower().startswith("access-control-allow-") for key in response))
        status, result, _ = self.request("POST", "/api/settings", None, {
            **headers, "Content-Length": str(1024**2 + 1), "Expect": "100-continue",
        })
        self.assertEqual((status, result["error"]["code"]), (413, "PAYLOAD_TOO_LARGE"))
        status, result, _ = self.request("POST", "/api/settings", b"x" * (1024**2 + 1), headers)
        self.assertEqual((status, result["error"]["code"]), (413, "PAYLOAD_TOO_LARGE"))
        status, result, _ = self.request("POST", "/api/settings", b"x" * 50000, {**headers, "Host": "invalid.example"})
        self.assertEqual((status, result["error"]["code"]), (403, "FORBIDDEN_HOST"))
        status, _, _ = self.request("POST", "/api/settings", "{}", {**headers, "Content-Type": "application/jsonp"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/settings", "[", headers)
        self.assertEqual(status, 400)

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Uses Git for Windows")
    def test_worktree_import_and_branch_switch(self):
        main = self.root / "Repo sample"
        linked = self.root / "Worktree sample"
        main.mkdir()
        for args in (("init", "-b", "sample-main"),
                     # -c commit.gpgsign=false: a machine-wide commit.gpgsign
                     # would open a passphrase dialog a test cannot answer.
                     ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid",
                      "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Sample"),
                     ("branch", "sample-other"),
                     ("worktree", "add", "--detach", str(linked))):
            result = subprocess.run(["git", "-C", str(main), *args], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        route = "/api/workspaces/import-worktrees"
        choices = self.api("POST", route, {"path": str(main)})["result"]
        self.assertEqual({Path(value) for value in choices}, {main, linked})
        imported = self.api("POST", route, {"path": str(main), "paths": [str(linked)]}, code=201)["result"]
        git = "/api/workspaces/" + imported[0]["id"] + "/git"
        self.api("POST", git, {"action": "checkout", "branch": "refs/heads/sample-other"}, code=400)
        task = self.api("POST", git, {
            "action": "checkout", "branch": "refs/heads/sample-other", "confirm": True,
        }, code=202)["result"]
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.15)
            task = self.api("GET", "/api/git-tasks/" + task["id"])["result"]
        self.assertEqual(task["state"], "success", task)
        self.assertEqual(task["workspace"]["branch"], "sample-other")
        self.assertIs(task["hasSubmodules"], False)   # no .gitmodules: UI must skip the prompt
        # submodule update must succeed even in a repo without submodules;
        # a request without confirm must be rejected first.
        self.api("POST", git, {"action": "submodule"}, code=400)
        task = self.api("POST", git, {"action": "submodule", "confirm": True}, code=202)["result"]
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.15)
            task = self.api("GET", "/api/git-tasks/" + task["id"])["result"]
        self.assertEqual(task["state"], "success", task)

        # An in-progress revert shows as a badge, blocks the normal switch, and is
        # cancelled by the force variant (checkout -f), which also discards edits.
        self.assertEqual(subprocess.run(["git", "-C", str(main), "branch", "sample-third"],
                                        capture_output=True, timeout=10).returncode, 0)
        (linked / "note.txt").write_text("v2\n", encoding="utf-8")
        for args in (("add", "note.txt"),
                     # sample-third stays on the pre-note commit: note.txt must not
                     # exist there, so a discarded checkout removes the file.
                     ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid",
                      "-c", "commit.gpgsign=false", "commit", "-m", "note v2")):
            result = subprocess.run(["git", "-C", str(linked), *args], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.run(["git", "-C", str(linked), "revert", "-n", "HEAD"],
                                        capture_output=True, timeout=10).returncode, 0)

        def workspace_row():
            return next(r for r in self.api("GET", "/api/workspaces")["result"] if r["id"] == imported[0]["id"])

        def git_task(body, state):
            task = self.api("POST", git, body, code=202)["result"]
            deadline = time.monotonic() + 10
            while task["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.15)
                task = self.api("GET", "/api/git-tasks/" + task["id"])["result"]
            self.assertEqual(task["state"], state, task)
            return task

        self.assertEqual(workspace_row()["gitOperation"], "revert")
        git_task({"action": "checkout", "branch": "refs/heads/sample-third", "confirm": True}, "failed")
        # locale-independent proof the switch was refused: nothing moved.
        self.assertEqual(workspace_row()["branch"], "sample-other")
        self.assertEqual(workspace_row()["gitOperation"], "revert")
        forced = git_task({"action": "checkout", "branch": "refs/heads/sample-third",
                           "force": True, "confirm": True}, "success")
        self.assertEqual(forced["workspace"]["branch"], "sample-third")
        self.assertIsNone(workspace_row()["gitOperation"])
        self.assertFalse((linked / "note.txt").exists())

        # A local commit SHA is accepted too and lands in a detached HEAD.
        def head_of():
            return subprocess.run(["git", "-C", str(linked), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()

        full = subprocess.run(["git", "-C", str(main), "rev-parse", "sample-third"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        task = git_task({"action": "checkout", "branch": full[:8], "confirm": True}, "success")
        self.assertIsNone(task["workspace"]["branch"])
        self.assertEqual(head_of(), full)
        # An unknown SHA fails without moving the worktree.
        git_task({"action": "checkout", "branch": "f" * 40, "confirm": True}, "failed")
        self.assertEqual(head_of(), full)

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Uses Git for Windows and batch files")
    def test_build_checkout_target_and_same_workspace_queue(self):
        ws = self.root / "Checkout build ws"
        sample = ws / "build-sample"
        sample.mkdir(parents=True)
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", sample / "fake build.bat")

        def repo_git(*args):
            result = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.decode("utf-8", "replace").strip()

        repo_git("init", "-b", "main")
        identity = ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid", "-c", "commit.gpgsign=false")
        repo_git(*identity, "add", "-A")
        repo_git(*identity, "commit", "-qm", "base")
        repo_git("branch", "second")
        row = self.api("POST", "/api/workspaces", {"name": ws.name, "path": str(ws)}, code=201)["result"]
        self.api("POST", "/api/recipes", {"name": "Checkout gate",
                                          "env": {"BUILD_RUNNER_TEST_PYTHON": sys.executable},
                                          "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "gate"]}]},
                 code=201)
        self.api("POST", "/api/recipes", {"name": "Checkout quick",
                                          "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "success"]}]},
                 code=201)

        def final(job_id):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                done = self.api("GET", "/api/jobs/" + job_id)["result"]
                if done["state"] not in ("queued", "running"):
                    return done
                time.sleep(0.15)
            self.fail("job did not finish: " + job_id)

        limit_before = self.api("GET", "/api/settings")["result"]["maxConcurrentJobs"]
        self.api("POST", "/api/settings", {"maxConcurrentJobs": 2})
        try:
            # Job 1 checks out "second" before the gate step runs.
            gate = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Checkout gate",
                                                  "checkout": {"target": "second"}}, code=201)["result"][0]
            deadline = time.monotonic() + 60
            while not (sample / "ready.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not (sample / "ready.txt").exists():
                job = self.api("GET", "/api/jobs/" + gate["id"])["result"]
                log = self.api("GET", "/api/jobs/" + gate["id"] + "/log?from=0")["result"]["text"]
                self.fail("gate step did not start: state=%s step=%s message=%s log=%r"
                          % (job["state"], job["step"], job.get("message"), log[-300:]))
            self.assertEqual(repo_git("branch", "--show-current"), "second")

            # A second checkout build queues on the same workspace instead of
            # conflicting, and dispatch must not overlap it (limit is 2).
            queued = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Checkout quick",
                                                    "checkout": {"target": "main"}}, code=201)
            quick = queued["result"][0]
            self.assertIn("QUEUE_SAME_WORKSPACE", [w.get("code") for w in queued.get("warnings") or []])
            time.sleep(0.8)
            self.assertEqual(self.api("GET", "/api/jobs/" + quick["id"])["result"]["state"], "queued")
            (sample / "release.txt").touch()
            self.assertEqual(final(gate["id"])["state"], "success")
            quick_done = final(quick["id"])
            self.assertEqual(quick_done["state"], "success", quick_done)
            self.assertEqual(quick_done["branch"], "main")
            gate_log = self.api("GET", "/api/jobs/" + gate["id"] + "/log?from=0")["result"]["text"]
            self.assertIn("Checkout second", gate_log)
            self.assertIn("- Done -", gate_log)

            # A missing target fails the job at the Checkout step, with no build run.
            ghost = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Checkout quick",
                                                   "checkout": {"target": "ghost-target"}}, code=201)["result"][0]
            ghost_done = final(ghost["id"])
            self.assertEqual(ghost_done["state"], "failed", ghost_done)
            self.assertIn("ghost-target", ghost_done["message"] or "")
            self.assertNotIn("- Done -", self.api("GET", "/api/jobs/" + ghost["id"] + "/log?from=0")["result"]["text"])
            self.assertEqual(repo_git("branch", "--show-current"), "main")

            # The commit picker lists a branch's history and rejects unknown refs.
            listed = self.api("GET", "/api/workspaces/" + row["id"] + "/commits?ref=second")["result"]
            self.assertEqual(listed["resolved"], "refs/heads/second")
            self.assertEqual([commit["subject"] for commit in listed["commits"]], ["base"])
            self.api("GET", "/api/workspaces/" + row["id"] + "/commits?ref=ghost", code=404)
            self.api("GET", "/api/workspaces/" + row["id"] + "/commits?ref=ghost%20target", code=400)
        finally:
            self.api("POST", "/api/settings", {"maxConcurrentJobs": limit_before})

    @unittest.skipUnless(os.name == "nt", "Uses Windows batch files")
    def test_same_workspace_plain_build_queues_behind(self):
        ws = self.root / "Plain queue ws"
        sample = ws / "build-sample"
        sample.mkdir(parents=True)
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", sample / "fake build.bat")
        row = self.api("POST", "/api/workspaces", {"name": ws.name, "path": str(ws)}, code=201)["result"]
        self.api("POST", "/api/recipes", {"name": "Plain gate",
                                          "env": {"BUILD_RUNNER_TEST_PYTHON": sys.executable},
                                          "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "gate"]}]},
                 code=201)
        self.api("POST", "/api/recipes", {"name": "Plain quick",
                                          "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "success"]}]},
                 code=201)

        def final(job_id):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                done = self.api("GET", "/api/jobs/" + job_id)["result"]
                if done["state"] not in ("queued", "running"):
                    return done
                time.sleep(0.15)
            self.fail("job did not finish: " + job_id)

        # A generous limit proves the folder itself, not the limit, serializes.
        limit_before = self.api("GET", "/api/settings")["result"]["maxConcurrentJobs"]
        self.api("POST", "/api/settings", {"maxConcurrentJobs": 3})
        try:
            gate = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Plain gate"},
                            code=201)["result"][0]
            deadline = time.monotonic() + 60
            while not (sample / "ready.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue((sample / "ready.txt").exists(), "gate step did not start")

            # A plain build (no checkout) on the busy folder is now accepted as
            # a queue entry: check and create both only warn about the queue.
            check = self.api("POST", "/api/jobs/check", {"workspaceIds": [row["id"]], "recipe": "Plain quick"})
            self.assertIn("QUEUE_SAME_WORKSPACE", [w.get("code") for w in check.get("warnings") or []])
            created = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Plain quick"},
                               code=201)
            quick = created["result"][0]
            self.assertIn("QUEUE_SAME_WORKSPACE", [w.get("code") for w in created.get("warnings") or []])
            time.sleep(0.8)
            self.assertEqual(self.api("GET", "/api/jobs/" + quick["id"])["result"]["state"], "queued")
            (sample / "release.txt").touch()
            gate_done = final(gate["id"])
            quick_done = final(quick["id"])
            self.assertEqual(gate_done["state"], "success", gate_done)
            self.assertEqual(quick_done["state"], "success", quick_done)
            self.assertGreaterEqual(quick_done["startedAt"], gate_done["endedAt"])
        finally:
            self.api("POST", "/api/settings", {"maxConcurrentJobs": limit_before})

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Uses Git for Windows and batch files")
    def test_build_checkout_multi_target_queue(self):
        ws = self.root / "Queue build ws"
        sample = ws / "build-sample"
        sample.mkdir(parents=True)
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", sample / "fake build.bat")

        def repo_git(*args):
            result = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.decode("utf-8", "replace").strip()

        repo_git("init", "-b", "main")
        identity = ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid", "-c", "commit.gpgsign=false")
        repo_git(*identity, "add", "-A")
        repo_git(*identity, "commit", "-qm", "base")
        repo_git("branch", "second")
        (ws / "extra.txt").write_text("extra")
        repo_git(*identity, "add", "-A")
        repo_git(*identity, "commit", "-qm", "extra")
        row = self.api("POST", "/api/workspaces", {"name": ws.name, "path": str(ws)}, code=201)["result"]
        self.api("POST", "/api/recipes", {"name": "Queue quick",
                                          "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "success"]}]},
                 code=201)

        def final(job_id):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                done = self.api("GET", "/api/jobs/" + job_id)["result"]
                if done["state"] not in ("queued", "running"):
                    return done
                time.sleep(0.15)
            self.fail("job did not finish: " + job_id)

        # The order the user picked is the build order: two checkout jobs on
        # one workspace must run serially, "second" first and "main" after.
        batch = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                               "checkouts": [{"target": "second"}, {"target": "main"}]}, code=201)
        first, second = batch["result"]
        self.assertIn("QUEUE_SAME_WORKSPACE", [w.get("code") for w in batch.get("warnings") or []])
        self.assertLess(first["queuedAt"], second["queuedAt"])
        first_done, second_done = final(first["id"]), final(second["id"])
        self.assertEqual((first_done["state"], second_done["state"]), ("success", "success"))
        self.assertEqual(first_done["branch"], "second")
        self.assertEqual(second_done["branch"], "main")
        self.assertGreaterEqual(second_done["startedAt"], first_done["endedAt"])
        self.assertEqual(repo_git("branch", "--show-current"), "main")

        # Branch table for the picker: every local branch with its tip, newest first.
        tips = self.api("GET", "/api/workspaces/" + row["id"] + "/branches")["result"]["branches"]
        self.assertEqual([item["name"] for item in tips], ["main", "second"])
        self.assertEqual(tips[0]["subject"], "extra")
        self.assertEqual(tips[0]["group"], "heads")
        self.assertIsNone(tips[0]["remote"])
        self.assertRegex(tips[0]["date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(tips[0]["authorDate"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertTrue(tips[0]["sha"])
        # A plain folder yields an empty list plus a reason so the dialog still opens.
        plain = self.root / "Not a repo"
        plain.mkdir()
        plain_row = self.api("POST", "/api/workspaces", {"name": plain.name, "path": str(plain)}, code=201)["result"]
        fallback = self.api("GET", "/api/workspaces/" + plain_row["id"] + "/branches")["result"]
        self.assertEqual(fallback["branches"], [])
        self.assertTrue(fallback["error"])

        # Malformed batch bodies are rejected before any job is created.
        jobs_before = len(self.api("GET", "/api/jobs")["result"])
        self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                       "checkouts": {"target": "main"}}, code=400)
        self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                       "checkouts": [{"target": " "}]}, code=400)
        self.assertEqual(len(self.api("GET", "/api/jobs")["result"]), jobs_before)

        # A dirty worktree blocks the checkout step; only force (-f) gets through.
        (ws / "extra.txt").write_text("dirty")
        blocked = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                                 "checkout": {"target": "second"}}, code=201)["result"][0]
        blocked_done = final(blocked["id"])
        self.assertEqual(blocked_done["state"], "failed", blocked_done)
        # The failure must come from Checkout, not from the build step: the log
        # carries the checkout note but never reaches the runner's done marker.
        blocked_log = self.api("GET", "/api/jobs/" + blocked["id"] + "/log?from=0")["result"]["text"]
        self.assertIn("Checkout second", blocked_log)
        self.assertNotIn("- Done -", blocked_log)
        self.assertEqual(repo_git("branch", "--show-current"), "main")
        forced = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                                "checkout": {"target": "second", "force": True}}, code=201)["result"][0]
        forced_done = final(forced["id"])
        self.assertEqual(forced_done["state"], "success", forced_done)
        self.assertEqual(forced_done["branch"], "second")
        self.assertEqual(repo_git("status", "--porcelain"), "")

        # A remote branch whose local counterpart exists is not silently reused
        # or overwritten: without force the job fails with guidance; with force
        # the local branch is reset onto the remote tip (git switch -C -f).
        bare = self.root / "Queue origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True, timeout=15, check=True)
        repo_git("remote", "add", "origin", str(bare))
        repo_git("push", "-q", "origin", "second")
        (ws / "extra.txt").write_text("local ahead")
        repo_git(*identity, "add", "-A")
        repo_git(*identity, "commit", "-qm", "local only")
        repo_git("switch", "-q", "main")
        self.assertNotEqual(repo_git("rev-parse", "second"), repo_git("rev-parse", "origin/second"))
        conflict = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                                  "checkout": {"target": "refs/remotes/origin/second"}},
                            code=201)["result"][0]
        conflict_done = final(conflict["id"])
        self.assertEqual(conflict_done["state"], "failed", conflict_done)
        # The guidance message is ours (raised before git runs), not git's fatal.
        self.assertIn("強制切樹", conflict_done["message"] or "")
        self.assertEqual(repo_git("branch", "--show-current"), "main")
        self.assertNotEqual(repo_git("rev-parse", "second"), repo_git("rev-parse", "origin/second"))
        reset = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                               "checkout": {"target": "refs/remotes/origin/second",
                                                            "force": True}}, code=201)["result"][0]
        reset_done = final(reset["id"])
        self.assertEqual(reset_done["state"], "success", reset_done)
        self.assertEqual(reset_done["branch"], "second")
        self.assertEqual(repo_git("rev-parse", "second"), repo_git("rev-parse", "origin/second"))

        # The same force job must also succeed while HEAD already sits on the
        # local branch: git switch -C resets the checked-out branch in place.
        again = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                               "checkout": {"target": "refs/remotes/origin/second",
                                                            "force": True}}, code=201)["result"][0]
        again_done = final(again["id"])
        self.assertEqual(again_done["state"], "success", again_done)
        self.assertEqual(again_done["branch"], "second")

        # Restarting a failed checkout job keeps its checkout target so the
        # retry checks out the same revision instead of building in place; the
        # plan is copied whole, so the force flag survives a restart too.
        ghost = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Queue quick",
                                               "checkout": {"target": "ghost-target", "force": True}},
                         code=201)["result"][0]
        self.assertEqual(final(ghost["id"])["state"], "failed")
        replacement = self.api("POST", "/api/jobs/" + ghost["id"] + "/restart", {"confirm": True}, code=201)["result"]
        self.assertEqual(replacement["checkoutTarget"], "ghost-target")
        self.assertEqual(replacement["recipe"], "Queue quick")
        plan = json.loads((self.data / "jobs" / replacement["id"] / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["checkout"]["force"])
        self.assertEqual(final(replacement["id"])["state"], "failed")

    @unittest.skipUnless(os.name == "nt", "Uses Windows batch files")
    def test_multi_workspace_validation_precedes_queueing(self):
        folders = [self.root / "Sample B", self.root / "Sample C"]
        ids = []
        for folder in folders:
            folder.mkdir()
            row = self.api("POST", "/api/workspaces", {"name": folder.name, "path": str(folder)}, code=201)
            ids.append(row["result"]["id"])
        recipe = {"name": "Multi", "steps": [{"cwd": "build-sample", "argv": ["fake build.bat", "success"]}]}
        self.api("POST", "/api/recipes", recipe, code=201)
        spec = {"workspaceIds": ids, "recipe": "Multi"}
        before = self.api("GET", "/api/jobs")["result"]
        for index, folder in enumerate(folders):
            subdir = folder / "build-sample"
            subdir.mkdir()
            shutil.copyfile(ROOT / "tests" / "fake_build.bat", subdir / "fake build.bat")
            if index == 0:
                self.api("POST", "/api/jobs", spec, code=400)
                self.assertEqual(self.api("GET", "/api/jobs")["result"], before)
        rows = self.api("POST", "/api/jobs", spec, code=201)["result"]
        self.assertEqual([row["workspaceId"] for row in rows], ids)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(row["state"] in ("queued", "running") for row in rows):
            time.sleep(0.1)
            rows = [self.api("GET", "/api/jobs/" + row["id"])["result"] for row in rows]
        self.assertEqual([row["state"] for row in rows], ["success", "success"], rows)

    @unittest.skipUnless(os.name == "nt", "Uses Windows batch files")
    def test_job_lifecycle_and_log(self):
        workspace = self.root / "Workspace A"
        workspace.mkdir()
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", workspace / "fake build.bat")
        row = self.api("POST", "/api/workspaces", {"name": "PlatformA", "path": str(workspace)}, code=201)["result"]
        recipe = {"name": "Sample", "steps": [{"argv": ["fake build.bat", "fail", "sample"]}]}
        self.api("POST", "/api/recipes", recipe, code=201)
        spec = {"workspaceIds": [row["id"]], "recipe": "Sample"}
        preview = self.api("POST", "/api/jobs/check", spec)["result"]
        self.assertEqual(preview[0]["argvPreview"], [["fake build.bat", "fail", "sample"]])
        job = self.api("POST", "/api/jobs", spec, code=201)["result"][0]
        path = "/api/jobs/" + job["id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and job["state"] in ("queued", "running"):
            time.sleep(0.1)
            job = self.api("GET", path)["result"]
        self.assertEqual(job["state"], "failed")
        log = self.api("GET", path + "/log?from=0")["result"]
        self.assertIn("error C2065", log["text"])
        self.assertEqual(self.api("GET", path + f"/log?from={log['nextOffset']}")["result"]["text"], "")
        search = self.api("GET", path + "/search?kind=error&q=C2065")["result"]
        self.assertEqual(search["total"], 1)
        self.assertIn("sample.c", search["matches"][0]["text"])
        triage = self.api("GET", path + "/triage")["result"]
        self.assertEqual(triage["rootError"]["location"], "sample.c:12")
        self.assertIn(job["id"], [item["id"] for item in self.api("GET", "/api/history")["result"]])
        self.api("DELETE", "/api/workspaces", {"id": row["id"]}, code=400)
        self.api("DELETE", "/api/workspaces", {"id": row["id"], "confirm": True})

    @unittest.skipUnless(os.name == "nt", "Uses the Windows Job runner")
    def test_restart_reuses_the_saved_plan_and_preserves_the_original_log(self):
        workspace = self.root / "Restart sample"
        workspace.mkdir()
        (workspace / "active.lock").write_bytes(b"x")

        def git(*args):
            return subprocess.check_output(["git", "-C", str(workspace), *args],
                                           stderr=subprocess.PIPE, text=True, timeout=10).strip()

        def commit_sample():
            git("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Sample")
            return git("rev-parse", "--short", "HEAD")

        git("init", "-b", "restart-original")
        original_commit = commit_sample()
        release = workspace / "release.txt"
        row = self.api("POST", "/api/workspaces", {"name": workspace.name, "path": str(workspace)}, code=201)["result"]

        def release_jobs():
            release.touch()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not any(job.get("workspaceId") == row["id"] and job["state"] in ("queued", "running")
                           for job in self.api("GET", "/api/jobs")["result"]):
                    return
                time.sleep(.05)

        self.addCleanup(release_jobs)
        script = (
            "import msvcrt, os, pathlib, time; active = open('active.lock', 'r+b'); "
            "msvcrt.locking(active.fileno(), msvcrt.LK_NBLCK, 1); "
            "print(os.environ['RESTART_SAMPLE'], flush=True); "
            "next((None for _ in range(300) if pathlib.Path('release.txt').exists() or time.sleep(.05)), None)"
        )
        self.api("POST", "/api/recipes", {
            "name": "Restart sample",
            "steps": [{"argv": [sys.executable, "-c", script]}],
            "env": {"RESTART_SAMPLE": "Original plan"},
        }, code=201)
        original = self.api("POST", "/api/jobs", {"workspaceIds": [row["id"]], "recipe": "Restart sample"},
                            code=201)["result"][0]
        original_path = "/api/jobs/" + original["id"]
        original_folder = self.data / "jobs" / original["id"]
        deadline = time.monotonic() + 8
        while not (original_folder / "build.log").read_bytes() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIn(b"Original plan", (original_folder / "build.log").read_bytes())
        self.assertEqual(original["branch"], "restart-original")
        self.assertEqual(original["commit"], original_commit)
        self.api("POST", original_path + "/restart", {}, code=400)
        self.api("POST", "/api/recipes", {
            "name": "Restart sample",
            "steps": [{"argv": [sys.executable, "-c", "raise SystemExit(1)"]}],
            "confirm": True,
        })
        git("switch", "-c", "restart-updated")
        updated_commit = commit_sample()
        replacement = self.api("POST", original_path + "/restart", {"confirm": True}, code=201)["result"]
        self.assertNotEqual(replacement["id"], original["id"])
        self.assertEqual(replacement["branch"], "restart-updated")
        self.assertEqual(replacement["commit"], updated_commit)
        self.assertEqual(self.api("GET", original_path)["result"]["state"], "stopped")
        self.assertIn(b"Original plan", (original_folder / "build.log").read_bytes())
        self.assertEqual((self.data / "jobs" / replacement["id"] / "plan.json").read_bytes(),
                         (original_folder / "plan.json").read_bytes())
        again = self.api("POST", original_path + "/restart", {"confirm": True}, code=201)["result"]
        self.assertEqual(again["id"], replacement["id"])
        release.touch()
        deadline = time.monotonic() + 8
        while replacement["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(.05)
            replacement = self.api("GET", "/api/jobs/" + replacement["id"])["result"]
        self.assertEqual(replacement["state"], "success", replacement)
        self.api("POST", "/api/jobs/" + replacement["id"] + "/restart", {"confirm": True}, code=409)

    @unittest.skipUnless(os.name == "nt", "Runs a Windows build job")
    def test_build_can_override_or_disable_recipe_artifacts(self):
        folder = self.root / "Artifact overrides"
        folder.mkdir()
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", folder / "build.bat")
        (folder / "image.bin").write_bytes(b"build output")
        workspace = self.api("POST", "/api/workspaces", {"name": folder.name, "path": str(folder)}, code=201)["result"]
        for name, dest in (("Default outputs", "default"), ("Selected outputs", "selected")):
            self.api("POST", "/api/artifact-rules", {"name": name, "rules": [
                {"select": ["image.bin"], "dest": dest},
            ]}, code=201)
        recipe = {"name": "Artifact override sample", "artifacts": "Default outputs",
                  "steps": [{"argv": ["build.bat", "success"]}]}
        self.api("POST", "/api/recipes", recipe, code=201)
        base = {"workspaceIds": [workspace["id"]], "recipe": recipe["name"]}
        for selection, expected in (({}, "Default outputs"), ({"artifactRule": ""}, None),
                                    ({"artifactRule": "Selected outputs"}, "Selected outputs")):
            with self.subTest(selection=selection):
                result = self.api("POST", "/api/jobs/check", {**base, **selection})["result"]
                self.assertEqual(result[0].get("artifactRule"), expected)
        self.api("POST", "/api/jobs/check", {**base, "artifactRule": False}, code=400)
        job = self.api("POST", "/api/jobs", {**base, "artifactRule": "Selected outputs"}, code=201)["result"][0]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and job["state"] in ("queued", "running"):
            time.sleep(0.1)
            job = self.api("GET", "/api/jobs/" + job["id"])["result"]
        self.assertEqual(job["state"], "success", job)
        self.assertEqual(job["artifacts"], [str(folder / "selected" / "image.bin")])
        self.assertFalse((folder / "default").exists())
        saved = self.api("GET", "/api/recipes")["result"]
        self.assertEqual(next(row for row in saved if row["id"] == recipe["name"])["artifacts"], "Default outputs")

    def test_artifact_preview_for_a_job_keeps_the_repo_root(self):
        repo = self.root / "Repo package"
        (repo / "OemPkgs" / "DT" / "Platform").mkdir(parents=True)
        roms = repo / "Build" / "DT" / "Pkg"
        roms.mkdir(parents=True)
        (roms / "bios.rom").write_bytes(b"rom")
        subprocess.check_call(["git", "-C", str(repo), "init"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        workspace = self.api("POST", "/api/workspaces", {"name": repo.name, "path": str(repo)}, code=201)["result"]
        self.api("POST", "/api/artifact-rules", {"name": "Repo rule", "rules": [
            {"root": "repo", "select": ["Build/DT/Pkg/*.rom"], "dest": "out"}]}, code=201)
        job_id = "d" * 32
        folder = self.data / "jobs" / job_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(
            {"id": job_id, "workspace": repo.name, "workspaceId": workspace["id"], "recipe": "Sample",
             "argv": ["builda.bat", "-r"], "state": "success", "branch": "main",
             "queuedAt": "2026-11-01T00:00:00+00:00", "startedAt": "2026-11-01T00:00:01+00:00",
             "endedAt": "2026-11-01T00:10:00+00:00", "exitCode": 0}), encoding="utf-8")
        plan = self.api("POST", "/api/artifact-rules/preview",
                        {"name": "Repo rule", "workspaceId": workspace["id"], "jobId": job_id})["result"]
        self.assertEqual([row["source"] for row in plan["files"]], ["Build/DT/Pkg/bios.rom"])

    @unittest.skipUnless(os.name == "nt", "Uses Windows directory junctions")
    def test_workspace_can_build_from_a_junction_to_the_repo_root(self):
        repo = self.root / "A longer example repository folder"
        workspace = repo / "packages" / "example"
        step = workspace / "build"
        step.mkdir(parents=True)
        subprocess.check_call(["git", "-C", str(repo), "init"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        alias = self.root / "short [example]"
        row = self.api("POST", "/api/workspaces", {"name": "Short path example", "path": str(workspace)}, code=201)["result"]
        listed = next(item for item in self.api("GET", "/api/workspaces")["result"] if item["id"] == row["id"])
        suggested = Path(listed["suggestedShortPath"])
        self.assertEqual(suggested.parent, Path(workspace.anchor))
        self.assertEqual(suggested.name, "WS-Short_path_example")
        self.assertNotIn("shortPath", listed)
        self.api("POST", "/api/workspaces", {"id": row["id"], "name": "Renamed short path example"})
        renamed = next(item for item in self.api("GET", "/api/workspaces")["result"] if item["id"] == row["id"])
        self.assertEqual(Path(renamed["suggestedShortPath"]).name, "WS-Renamed_short_path_example")
        edited = self.api("POST", "/api/workspaces", {"id": row["id"], "shortPath": str(alias)})["result"]
        self.assertEqual(Path(edited["shortPath"]), alias)
        self.assertEqual(Path(edited["path"]), workspace)
        self.assertFalse(alias.exists())
        script = step / "record-path.cmd"
        script.write_text(
            '@echo off\n"%~1" -c "import sys; from pathlib import Path; '
            'Path(\'script-path.txt\').write_text(sys.argv[1], encoding=\'utf-8\')" "%~dp0."\n',
            encoding="ascii", newline="\r\n",
        )
        recipe = {"name": "Run from short path", "steps": [{
            "cwd": "build", "argv": [sys.executable, "-c",
                "import os; from pathlib import Path; Path('cwd.txt').write_text(os.getcwd(), encoding='utf-8')"],
        }, {"cwd": "build", "argv": [str(script), sys.executable]}]}
        self.api("POST", "/api/recipes", recipe, code=201)
        spec = {"workspaceIds": [row["id"]], "recipe": recipe["name"]}
        plan = self.api("POST", "/api/jobs/check", spec)["result"][0]
        self.assertEqual(Path(plan["shortPath"]), alias)
        self.assertFalse(alias.exists())

        def build():
            job = self.api("POST", "/api/jobs", spec, code=201)["result"][0]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and job["state"] in ("queued", "running"):
                time.sleep(0.05)
                job = self.api("GET", "/api/jobs/" + job["id"])["result"]
            return job

        for _ in range(2):
            job = build()
            self.assertEqual(job["state"], "success", job)
            expected = alias / "packages" / "example" / "build"
            self.assertEqual(Path((step / "cwd.txt").read_text(encoding="utf-8")), expected)
            self.assertEqual(Path((step / "script-path.txt").read_text(encoding="utf-8")), expected)
            self.assertTrue(alias.samefile(repo))
            self.assertEqual(Path(job["workspacePath"]), workspace)

        occupied = self.root / "existing directory"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_bytes(b"existing data")
        self.api("POST", "/api/workspaces", {"id": row["id"], "shortPath": str(occupied)})
        job = build()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("短路徑", job["message"])
        self.assertEqual(marker.read_bytes(), b"existing data")
        self.api("POST", "/api/workspaces", {"id": row["id"], "shortPath": "relative"}, code=400)
        reset = self.api("POST", "/api/workspaces", {"id": row["id"], "shortPath": ""})["result"]
        self.assertIsNone(reset.get("shortPath"))
        self.assertTrue(alias.samefile(repo))

    @unittest.skipUnless(os.name == "nt", "Uses Windows drive-letter paths")
    def test_suggested_short_path_follows_the_workspace_name(self):
        names = ["Sample_2026 PlatformA", "Foo/Bar", "Foo Bar", "測試", "x" * 80, "a" * 59 + ". b",
                 "CON", "Beta"]
        created = {}
        for index, name in enumerate(names):
            folder = self.root / f"suggestion folder {index}"
            folder.mkdir()
            created[name] = self.api("POST", "/api/workspaces",
                                     {"name": name, "path": str(folder)}, code=201)["result"]
        listed = {item["id"]: item for item in self.api("GET", "/api/workspaces")["result"]}

        def alias(name):
            return Path(listed[created[name]["id"]]["suggestedShortPath"]).name

        self.assertEqual(alias("Sample_2026 PlatformA"), "WS-Sample_2026_PlatformA")
        self.assertRegex(alias("測試"), r"^WS-[0-9a-f]{8}$")
        self.assertEqual(alias("x" * 80), "WS-" + "x" * 60)
        self.assertEqual(alias("a" * 59 + ". b"), "WS-" + "a" * 59)  # truncation never ends in "._-"
        self.assertEqual(alias("CON"), "WS-CON")  # the prefix keeps reserved device names usable
        self.assertTrue(alias("Foo/Bar").startswith("WS-Foo_Bar-"))
        self.assertTrue(alias("Foo Bar").startswith("WS-Foo_Bar-"))
        self.assertNotEqual(alias("Foo/Bar"), alias("Foo Bar"))
        # a suggestion must be accepted as the configured short path unchanged
        sample = created["Sample_2026 PlatformA"]
        suggestion = listed[sample["id"]]["suggestedShortPath"]
        edited = self.api("POST", "/api/workspaces", {"id": sample["id"], "shortPath": suggestion})["result"]
        self.assertEqual(edited["shortPath"], suggestion)
        # a sibling's configured short path also blocks the plain suggestion
        beta = created["Beta"]
        anchor = Path(Path(beta["path"]).anchor)
        occupied = str(anchor / "WS-Beta")
        self.api("POST", "/api/workspaces", {"id": created["CON"]["id"], "shortPath": occupied})
        relisted = {item["id"]: item for item in self.api("GET", "/api/workspaces")["result"]}
        self.assertEqual(relisted[beta["id"]]["suggestedShortPath"],
                         str(anchor / f"WS-Beta-{beta['id'][:6]}"))

    def test_workspace_list_defers_dirty_checks(self):
        workspace = self.root / "Dirty sample"
        workspace.mkdir()
        row = self.api("POST", "/api/workspaces", {"name": workspace.name, "path": str(workspace)}, code=201)["result"]
        listed = {item["id"]: item for item in self.api("GET", "/api/workspaces")["result"]}
        self.assertIsNone(listed[row["id"]]["dirty"])   # the fast list path skips git status
        self.assertIsNone(self.api("GET", "/api/workspaces/dirty")["result"][row["id"]])  # not a Git repo
        subprocess.check_call(["git", "-C", str(workspace), "init"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        self.assertIs(self.api("GET", "/api/workspaces/dirty")["result"][row["id"]], False)
        (workspace / "draft.txt").write_text("change\n", encoding="utf-8")
        self.assertIs(self.api("GET", "/api/workspaces/dirty")["result"][row["id"]], True)
