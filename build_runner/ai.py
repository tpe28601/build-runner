"""Build-log prompts and cancellable local analysis tasks."""

from datetime import datetime
from itertools import islice
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from . import jobs, logtools
from .paths import ROOT, ToolError
from .runner import contain_process_tree

METHODS = ("copy", "opencode", "endpoint")
ANSWER = re.compile(r"ai-\d{8}-\d{6}-[a-f0-9]{8}\.md")
MAX_RESPONSE = 2 * 1024 * 1024
DEFAULT_TEMPLATE = (
    "Analyze this build failure. Identify the likely root cause, distinguish cascading errors, "
    "and suggest concrete verification steps.\n\n"
    "Workspace: {workspace}\nBranch: {branch}\nCommand: {command}\n\n"
    "Triage:\n{triage}\n\nBuild log context:\n{context}\n"
)


def configure(current, changes):
    if not isinstance(changes, dict):
        raise ToolError("INVALID_AI_SETTINGS", "AI settings must be an object.")
    config = {
        "method": "copy", "template": DEFAULT_TEMPLATE, "timeoutSec": 120,
        "opencodePath": "", "opencodeArgs": [], "baseUrl": "", "model": "", "apiKey": "",
        **current,
    }
    for field in ("method", "template", "timeoutSec", "opencodePath", "opencodeArgs", "baseUrl", "model"):
        if field in changes:
            config[field] = changes[field]
    if changes.get("clearApiKey") is True:
        config["apiKey"] = ""
    elif changes.get("apiKey"):
        config["apiKey"] = changes["apiKey"]
    if config["method"] not in METHODS or type(config["timeoutSec"]) is not int or not 1 <= config["timeoutSec"] <= 900:
        raise ToolError("INVALID_AI_SETTINGS", "Choose a method and a timeout between 1 and 900 seconds.")
    for field in ("opencodePath", "baseUrl", "model", "apiKey", "template"):
        value = config[field]
        if not isinstance(value, str) or "\0" in value or len(value) > 20000:
            raise ToolError("INVALID_AI_SETTINGS", f"Invalid {field}.")
    if not config["template"].strip():
        raise ToolError("INVALID_AI_SETTINGS", "The prompt template cannot be empty.")
    if "\r" in config["apiKey"] or "\n" in config["apiKey"]:
        raise ToolError("INVALID_AI_SETTINGS", "API key cannot contain newlines.")
    args = config["opencodeArgs"]
    if not isinstance(args, list) or len(args) > 50 or any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ToolError("INVALID_AI_SETTINGS", "OpenCode arguments must be a JSON string array.")
    if config["baseUrl"]:
        endpoint_url(config["baseUrl"])
    return config


def public(config):
    return {key: value for key, value in config.items() if key != "apiKey"} | {"hasApiKey": bool(config.get("apiKey"))}


def endpoint_url(base):
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError as exc:
        raise ToolError("INVALID_AI_URL", "Enter a valid HTTP or HTTPS base URL.") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or port == 0
            or any(char.isspace() for char in base)):
        raise ToolError("INVALID_AI_URL", "Enter an HTTP or HTTPS base URL without credentials or query parameters.")
    path = parsed.path.rstrip("/")
    return parsed._replace(path=path + ("" if path.endswith("/v1") else "/v1") + "/chat/completions").geturl()


def prompt_for(row, log, template=DEFAULT_TEMPLATE):
    report = logtools.triage(log)
    root = report["rootError"]
    center = root["line"] if root else max(1, report["lineCount"] - 40)
    first = max(1, center - 40)
    context = "\n".join(f"{number}: {line[:1500]}" for number, line in
                        enumerate(islice(logtools.iter_lines(log), first - 1, center + 40), first))
    triage = {"result": report["result"], "rootError": root, "cascadeErrors": report["cascadeErrors"][:8]}
    values = {"workspace": str(row.get("workspace") or ""), "branch": str(row.get("branch") or ""),
              "command": json.dumps(row.get("argv") or row.get("argvPreview") or [], ensure_ascii=False),
              "triage": json.dumps(triage, ensure_ascii=False), "context": context}
    return re.sub(r"\{(workspace|branch|command|triage|context)\}", lambda match: values[match[1]], template)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(request):
    config = request["config"]
    body = json.dumps({"model": config["model"], "messages": [
        {"role": "user", "content": request["prompt"]},
    ]}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config["apiKey"]:
        headers["Authorization"] = "Bearer " + config["apiKey"]
    req = Request(endpoint_url(config["baseUrl"]), data=body, headers=headers, method="POST")
    with build_opener(NoRedirect()).open(req, timeout=config["timeoutSec"]) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Endpoint response exceeds 2 MB.")
    answer = json.loads(raw)["choices"][0]["message"]["content"]
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Endpoint returned no text.")
    return answer


def opencode(request):
    config = request["config"]
    prompt = request["prompt"]
    if len(prompt) > 6000:
        path = Path(request["promptPath"])
        path.write_text(prompt, encoding="utf-8")
        prompt = f"Read the build-analysis prompt from this local file and follow it: {path}"
    result = subprocess.run([config["opencodePath"], "run", *config["opencodeArgs"], prompt],
                            shell=False, cwd=request["workspacePath"], stdin=subprocess.DEVNULL,
                            capture_output=True)
    if result.returncode:
        raise ValueError(f"OpenCode exited with code {result.returncode}: "
                         + result.stderr[:1000].decode("utf-8", errors="replace"))
    answer = result.stdout[:MAX_RESPONSE + 1]
    if not answer.strip():
        raise ValueError("OpenCode returned no text.")
    if len(answer) > MAX_RESPONSE:
        raise ValueError("OpenCode response exceeds 2 MB.")
    return answer.decode("utf-8", errors="replace")


def worker():
    try:
        if os.name == "nt":
            contain_process_tree()
        request = json.load(sys.stdin.buffer)
        answer = endpoint(request) if request["method"] == "endpoint" else opencode(request)
        sys.stdout.buffer.write(answer.encode("utf-8"))
    except Exception as exc:
        sys.stderr.buffer.write(str(exc).encode("utf-8", errors="replace"))
        return 1
    return 0


class Analyses:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}
        self.processes = {}

    def get(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise ToolError("AI_TASK_NOT_FOUND", "找不到 AI 分析工作。")
            return self.tasks[task_id].copy()

    def active_for(self, job_id):
        with self.lock:
            return next((task.copy() for task in self.tasks.values()
                         if task["jobId"] == job_id and task["state"] == "running"), None)

    def start(self, method, config, row, folder, prompt):
        if method == "endpoint" and (not config["baseUrl"] or not config["model"]):
            raise ToolError("AI_NOT_CONFIGURED", "請先設定端點 URL 與模型。")
        if method == "opencode":
            path = Path(config["opencodePath"])
            if not path.is_absolute() or not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
                raise ToolError("AI_NOT_CONFIGURED", "請設定 OpenCode 執行檔的絕對路徑（Windows 請使用 .exe）。")
            if not Path(row["workspacePath"]).is_dir():
                raise ToolError("WORKSPACE_NOT_FOUND", "Workspace 資料夾不存在。")
        task_id = uuid4().hex
        task = {"id": task_id, "jobId": row["id"], "state": "running", "method": method}
        with self.lock:
            if self.processes:
                raise ToolError("JOB_BUSY", "已有 AI 分析正在執行。", 1)
            while len(self.tasks) >= 30:
                self.tasks.pop(next(iter(self.tasks)))
            child = subprocess.Popen([sys.executable, "-m", "build_runner.ai"], cwd=ROOT, shell=False,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            self.tasks[task_id] = task
            self.processes[task_id] = child
        prompt_path = folder / f".ai-prompt-{task_id}.txt"
        payload = {"method": method, "config": config, "prompt": prompt, "promptPath": str(prompt_path),
                   "workspacePath": row["workspacePath"]}
        threading.Thread(target=self.collect, args=(task_id, child, folder, prompt_path, payload)).start()
        return task.copy()

    @staticmethod
    def terminate(child):
        if child.poll() is None:
            if os.name == "nt":
                try:
                    jobs.kill_tree(child.pid)
                    return
                except (ToolError, OSError, subprocess.SubprocessError):
                    pass
            try:
                child.kill()
            except ProcessLookupError:
                pass

    def cancel(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise ToolError("AI_TASK_NOT_FOUND", "找不到 AI 分析工作。")
            task = self.tasks[task_id]
            child = self.processes.get(task_id)
            if child and task["state"] == "running":
                task["state"] = "cancelled"
        if child:
            self.terminate(child)
        return self.get(task_id)

    def close(self):
        with self.lock:
            children = list(self.processes.values())
        for child in children:
            self.terminate(child)

    def collect(self, task_id, child, folder, prompt_path, payload):
        config = payload["config"]
        try:
            stdout, stderr = child.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                                timeout=config["timeoutSec"])
            error = stderr.decode("utf-8", errors="replace")
            answer = stdout.decode("utf-8", errors="replace")
            if config["apiKey"]:
                error = error.replace(config["apiKey"], "••••")
                answer = answer.replace(config["apiKey"], "••••")
            if child.returncode:
                outcome = {"state": "failed", "message": error[:1500] or "AI analysis failed."}
            else:
                outcome = {"state": "success", "answer": answer}
        except subprocess.TimeoutExpired:
            self.terminate(child)
            child.communicate()
            outcome = {"state": "failed", "message": "AI 分析逾時。"}
        except OSError as exc:
            outcome = {"state": "failed", "message": str(exc)}
        finally:
            try:
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass
        with self.lock:
            self.processes.pop(task_id, None)
            task = self.tasks[task_id]
            if task["state"] == "cancelled":
                return
            if outcome["state"] == "success":
                name = datetime.now().strftime("ai-%Y%m%d-%H%M%S-") + task_id[:8] + ".md"
                try:
                    with (folder / name).open("x", encoding="utf-8") as stream:
                        stream.write(outcome["answer"])
                    outcome["file"] = name
                except OSError as exc:
                    outcome = {"state": "failed", "message": str(exc)}
            task.update(outcome)


if __name__ == "__main__":
    raise SystemExit(worker())
