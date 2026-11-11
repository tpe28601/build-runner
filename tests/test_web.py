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
        builtins = self.api("GET", "/api/recipes")["result"][:2]
        self.assertEqual([record["name"] for record in builtins], ["Clean + Build", "Clean + Build (-nou)"])
        self.assertTrue(all(record["builtIn"] for record in builtins))
        self.assertEqual(builtins[0]["steps"][0]["argv"], ["builda.bat", "-cleanall"])
        self.assertEqual(builtins[1]["steps"][0]["argv"], ["builda.bat", "-cleanall", "-nou"])
        self.assertEqual(builtins[1]["steps"][1]["argv"], ["builda.bat", "-r", "-nou"])
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
        (folder / "build.log").write_bytes(body.encode("utf-8"))
        status, content, headers = self.request("GET", f"/api/jobs/{job_id}/log?download=1")
        self.assertEqual(status, 200)
        self.assertEqual(content, body.encode("utf-8"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Content-Disposition"],
                         f'attachment; filename="PlatformA-feature-x-{job_id[:8]}.log"')
        prompt = self.api("POST", f"/api/jobs/{job_id}/ai",
                          {"method": "copy", "preview": True, "logScope": "range",
                           "logStart": 2, "logEnd": 4})["result"]["prompt"]
        self.assertIn("2: line 2", prompt)
        self.assertIn("4: line 4", prompt)
        self.assertNotIn("5: line 5", prompt)
        self.assertEqual(self.api("POST", f"/api/jobs/{job_id}/ai",
                                  {"method": "copy", "preview": True})["result"]["scope"], "around")
        self.api("POST", f"/api/jobs/{job_id}/ai", {"method": "copy", "preview": True,
                                                    "logScope": "everywhere"}, code=400)
        self.api("POST", f"/api/jobs/{job_id}/ai", {"method": "copy", "preview": True,
                                                    "logScope": "range", "logStart": "2"}, code=400)

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
                     ("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid",
                      "commit", "--allow-empty", "-m", "Sample"),
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
