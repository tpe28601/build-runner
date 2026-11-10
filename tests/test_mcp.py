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


class MCPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.workspace = self.root / "Workspace sample"
        self.workspace.mkdir()
        self.write("workspaces.json", [{"id": "sample", "name": "PlatformA", "path": str(self.workspace)}])
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_server.py"), "--data-dir", str(self.data)],
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        self.addCleanup(self.close)
        self.sequence = 0

    def write(self, name, content):
        file = self.data / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(content), encoding="utf-8")

    def close(self):
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.stdin = None
        stdout, stderr = self.proc.communicate(timeout=10)
        self.assertEqual(stdout, "", stdout)
        self.assertEqual(self.proc.returncode, 0, stderr)

    def send(self, message):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params or {}})
        response = self.proc.stdout.readline()
        self.assertTrue(response, "MCP server exited before responding")
        value = json.loads(response)
        self.assertEqual(value["jsonrpc"], "2.0")
        self.assertEqual(value["id"], self.sequence)
        return value

    def initialize(self):
        response = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "Test", "version": "1"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.send({"method": "notifications/initialized"})

    def tool(self, tool_name, is_error=False, **arguments):
        response = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        self.assertEqual(response["result"].get("isError", False), is_error, response)
        return json.loads(response["result"]["content"][0]["text"])

    def test_initialization_tools_and_builtin_recipe(self):
        self.initialize()
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {
            "list_workspaces", "list_recipes", "describe_params", "start_build", "get_job",
            "read_log", "search_log", "triage_job", "stop_job", "list_history",
        })
        self.assertEqual(self.tool("list_workspaces")[0]["name"], "PlatformA")
        recipe = self.tool("list_recipes")[0]
        self.assertTrue(recipe["builtIn"])
        self.assertEqual(recipe["name"], "Clean + Build")
        self.assertEqual(recipe["steps"][0]["argv"], ["builda.bat", "cleanall"])
        self.assertEqual(recipe["steps"][1]["argv"], ["builda.bat", "-r"])
        self.tool("start_build", is_error=True, workspace="sample", recipe=recipe["id"], argv=["other.bat"])
        self.tool("start_build", is_error=True, workspace="sample", recipe=recipe["id"], params={"unknown": True})
        result = self.tool("start_build", is_error=True, workspace="sample", recipe=recipe["id"])
        self.assertEqual(result["code"], "BUILTIN_SCRIPT_NOT_FOUND")
        self.assertFalse((self.data / "jobs").exists())

    def test_read_log_enforces_maximum_and_returns_numbered_tail(self):
        self.initialize()
        job_id = "a" * 32
        self.write(f"jobs/{job_id}/status.json", {"id": job_id, "state": "success"})
        log = self.data / "jobs" / job_id / "build.log"
        log.write_text("\n".join(f"Sample {i}" for i in range(1, 451)), encoding="utf-8")
        result = self.tool("read_log", jobId=job_id, tail=400)
        self.assertEqual(len(result["lines"]), 400)
        self.assertEqual(result["lines"][0], {"line": 51, "text": "Sample 51"})
        self.tool("read_log", is_error=True, jobId=job_id, start=1, end=401)
        result = self.tool("read_log", jobId=job_id, start=449, end=450)
        self.assertEqual([line["line"] for line in result["lines"]], [449, 450])

    def test_build_requires_declared_keys_and_rejects_agent_response_file_arguments(self):
        self.initialize()
        self.write("paramsets/sample.json", {"name": "sample", "params": [
            {"key": "label", "flag": "-label", "type": "value"},
        ]})
        self.write("recipes/sample.json", {"name": "sample", "paramset": "sample",
                                           "steps": [{"argv": ["builda.bat"], "useParams": True}]})
        self.assertEqual(self.tool("describe_params", name="sample")["params"][0]["flag"], "-label")
        for params in ({"other": True}, {"label": "@sample.rsp"}, {"label": "A&B"}):
            self.tool("start_build", is_error=True, workspace="sample", recipe="sample", params=params)
        self.assertFalse((self.data / "jobs").exists())

    @unittest.skipUnless(os.name == "nt", "Uses a Windows batch file")
    def test_builtin_recipe_runs_clean_then_build_without_saving_a_copy(self):
        self.initialize()
        (self.workspace / "builda.bat").write_text(
            '@echo off\nif "%~1"=="cleanall" (\n  echo Clean step\n  exit /b 0\n)\n'
            "echo Build step %*\nexit /b 0\n", encoding="utf-8",
        )
        recipe = self.tool("list_recipes")[0]
        job_id = self.tool("start_build", workspace="sample", recipe=recipe["id"])["jobId"]
        status = self.wait_for_job(job_id)
        self.assertEqual(status["state"], "success")
        self.assertEqual([line["text"] for line in self.tool("read_log", jobId=job_id)["lines"]],
                         ["Clean step", "Build step -r"])
        self.assertFalse((self.data / "recipes").exists())

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.tool("get_job", jobId=job_id)
            if status["state"] not in ("running", "queued"):
                return status
            time.sleep(0.1)
        self.fail("Build did not finish")

    @unittest.skipUnless(os.name == "nt", "Uses a Windows batch file")
    def test_start_build_get_job_and_triage_through_stdio(self):
        self.initialize()
        self.write("recipes/sample.json", {"name": "Sample", "steps": [{"argv": ["fake_build.bat", "fail"]}]})
        shutil.copyfile(ROOT / "tests" / "fake_build.bat", self.workspace / "fake_build.bat")
        job_id = self.tool("start_build", workspace="sample", recipe="sample")["jobId"]
        status = self.wait_for_job(job_id)
        self.assertEqual(status["state"], "failed")
        result = self.tool("triage_job", jobId=job_id)
        self.assertEqual(result["rootError"]["location"], "sample.c:12")
        self.assertEqual(self.tool("search_log", jobId=job_id, q="C2065")["total"], 1)
        self.assertEqual(self.tool("list_history", limit=1)[0]["id"], job_id)
