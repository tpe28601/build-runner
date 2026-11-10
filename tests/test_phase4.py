from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from build_runner import ai, history, schedule
from build_runner.paths import ToolError


class Phase4Tests(unittest.TestCase):
    def wait_for_analysis(self, manager, task_id):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            task = manager.get(task_id)
            if task["state"] != "running":
                return task
            time.sleep(0.05)
        self.fail("AI analysis did not finish.")

    def test_custom_endpoint_persists_answer_and_masks_api_key(self):
        seen = []

        class Endpoint(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers["Authorization"], request))
                body = b'{"choices":[{"message":{"content":"# Sample answer"}}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server, tempfile.TemporaryDirectory() as temp:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                folder = Path(temp)
                config = ai.configure({}, {"baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                           "model": "sample", "apiKey": "sample-secret", "timeoutSec": 10})
                self.assertNotIn("apiKey", ai.public(config))
                self.assertTrue(ai.public(config)["hasApiKey"])
                manager = ai.Analyses()
                task = manager.start("endpoint", config, {"id": "sample", "workspacePath": temp}, folder, "sample prompt")
                try:
                    task = self.wait_for_analysis(manager, task["id"])
                    self.assertEqual(task["state"], "success", task)
                    self.assertEqual((folder / task["file"]).read_text(encoding="utf-8"), "# Sample answer")
                    self.assertEqual(seen[0][:2], ("/v1/chat/completions", "Bearer sample-secret"))
                    self.assertEqual(seen[0][2]["messages"][0]["content"], "sample prompt")
                finally:
                    manager.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)

    @unittest.skipUnless(os.name == "nt", "Uses Windows process tree termination")
    def test_opencode_long_prompt_file_is_removed_on_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "run").write_text(
                "from pathlib import Path\nimport sys, time\n"
                "path = Path(sys.argv[-1].split(': ', 1)[1])\n"
                "assert len(path.read_text(encoding='utf-8')) == 6001\n"
                "Path('ready').touch()\ntime.sleep(30)\n", encoding="utf-8",
            )
            config = ai.configure({}, {"opencodePath": sys.executable, "timeoutSec": 15})
            manager = ai.Analyses()
            task = manager.start("opencode", config, {"id": "sample", "workspacePath": temp}, folder, "a" * 6001)
            try:
                deadline = time.monotonic() + 5
                while not (folder / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((folder / "ready").exists(), manager.get(task["id"]))
                self.assertEqual(manager.cancel(task["id"])["state"], "cancelled")
                deadline = time.monotonic() + 5
                while list(folder.glob(".ai-prompt-*.txt")) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(list(folder.glob(".ai-prompt-*.txt")), [])
                self.assertEqual(list(folder.glob("ai-*.md")), [])
            finally:
                manager.close()
    def test_history_filters_and_retention(self):
        rows = [
            {"id": "a", "workspaceId": "one", "state": "failed", "endedAt": "2025-01-01T00:00:00+00:00"},
            {"id": "b", "workspaceId": "one", "state": "success", "endedAt": "2025-01-05T00:00:00+00:00"},
            {"id": "c", "workspaceId": "two", "state": "failed", "endedAt": "2025-01-06T00:00:00+00:00"},
        ]
        self.assertEqual(history.filtered(rows, "one", "failed"), rows[:1])
        now = datetime(2025, 1, 7, tzinfo=timezone.utc)
        self.assertEqual(history.retained(rows, 3, 1, now=now), rows[-1:])
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            history.replace(data, rows)
            self.assertEqual(history.list_history(data), rows)

    def test_prompt_contains_context_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "build.log"
            log.write_text("Before\nsample.c(12): error C2065: unknown\n- Failed -\n", encoding="utf-8")
            prompt = ai.prompt_for({"workspace": "PlatformA", "branch": "sample", "argv": [["builda.bat"]]}, log)
            for expected in ("PlatformA", "sample", "builda.bat", "Before", "C2065"):
                self.assertIn(expected, prompt)

    def test_schedule_options_are_validated_and_scoped(self):
        options = schedule.normalize({"name": "sample", "workspace": "PlatformA", "recipe": "Test",
                                      "frequency": "WEEKLY", "time": "06:30", "day": "MON"})
        self.assertEqual(options["name"], "sample")
        self.assertEqual(schedule.task_name("sample"), "DevToolbox-BuildRunner-sample")
        with self.assertRaises(ToolError):
            schedule.task_name("../other")
        with self.assertRaises(ToolError):
            schedule.normalize({**options, "time": "26:30"})
