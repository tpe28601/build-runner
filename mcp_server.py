"""Build Runner MCP server over newline-delimited stdio JSON-RPC."""

import argparse
from collections import deque
from itertools import islice
import json
import re
import sys

from build_runner import history, jobs, logtools, paramsets, recipes, workspaces
from build_runner.paths import ROOT, TOOL_ID, ToolError, config_records, data_dir, read_json

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_REQUEST = 1024 * 1024
MAX_LOG_LINES = 400
TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
JOB = {"jobId": {**TEXT, "pattern": r"^[a-f0-9]{32}$"}}


def integer(minimum, maximum):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def tool(description, properties=None, required=()):
    return {"description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False}}


TOOLS = {
    "list_workspaces": tool("List registered workspaces, Git state and last build result."),
    "list_recipes": tool("List available recipes, parameter tables and preset names."),
    "describe_params": tool("Describe the allowed parameters and presets of a saved table.",
                            {"name": TEXT}, ["name"]),
    "start_build": tool(
        "Start a saved recipe in a registered workspace. Inspect the recipe before starting it.",
        {"workspace": TEXT, "recipe": TEXT, "preset": TEXT, "params": {"type": "object"}},
        ["workspace", "recipe"],
    ),
    "get_job": tool("Get a job's state and current step.", JOB, ["jobId"]),
    "read_log": tool(
        "Read at most 400 log lines. Use tail, or an inclusive one-based start/end range. Defaults to tail 100.",
        {**JOB, "tail": integer(1, MAX_LOG_LINES), "start": integer(1, 2**31 - 1),
         "end": integer(1, 2**31 - 1)},
        ["jobId"],
    ),
    "search_log": tool(
        "Find log lines by keyword or error, fail, or warning category. Index is a zero-based match offset.",
        {**JOB, "q": {"type": "string", "maxLength": 4096},
         "kind": {"type": "string", "enum": ["error", "fail", "warning"]},
         "index": integer(0, 2**31 - 1), "limit": integer(1, MAX_LOG_LINES)},
        ["jobId"],
    ),
    "triage_job": tool("Get build result markers, root error and cascading errors.", JOB, ["jobId"]),
    "stop_job": tool("Stop a running build or cancel a queued build.", JOB, ["jobId"]),
    "send_enter": tool("Press Enter once for a build stopped at an input prompt (username, password, or passphrase).",
                       JOB, ["jobId"]),
    "list_history": tool("List recent build history, newest first.", {"limit": integer(1, 400)}),
}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_arguments(name, arguments):
    schema = TOOLS[name]["inputSchema"]
    fields = schema["properties"]
    if not isinstance(arguments, dict) or arguments.keys() - fields.keys() or set(schema["required"]) - arguments.keys():
        raise ToolError("INVALID_ARGUMENTS", "Arguments must match the tool's input schema.")
    for key, value in arguments.items():
        rules = fields[key]
        kind = rules["type"]
        if kind == "string":
            valid = (isinstance(value, str) and rules.get("minLength", 0) <= len(value) <= rules.get("maxLength", 4096)
                     and "\0" not in value and ("enum" not in rules or value in rules["enum"])
                     and ("pattern" not in rules or re.fullmatch(rules["pattern"], value)))
        elif kind == "integer":
            valid = type(value) is int and rules["minimum"] <= value <= rules["maximum"]
        else:
            valid = isinstance(value, dict)
        if not valid:
            raise ToolError("INVALID_ARGUMENTS", f"Invalid argument: {key}.")


def result_payload(value, is_error=False):
    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
    if is_error:
        result["isError"] = True
    return result


class Server:
    def __init__(self, data):
        self.data = data
        self.initialized = False
        self.manifest = read_json(ROOT / "tool.json")

    def dispatch(self, method, params):
        if method == "ping":
            return {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            if not isinstance(requested, str) or not requested:
                raise RpcError(-32602, "initialize requires protocolVersion")
            self.initialized = True
            return {"protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[0],
                    "serverInfo": {"name": TOOL_ID, "version": self.manifest["version"]},
                    "capabilities": {"tools": {"listChanged": False}}}
        if not self.initialized:
            raise RpcError(-32002, "Server not initialized")
        if method == "tools/list":
            return {"tools": [{"name": name, **spec} for name, spec in TOOLS.items()]}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                raise RpcError(-32602, "Unknown tool")
            args = params.get("arguments", {})
            try:
                validate_arguments(name, args)
                return result_payload(getattr(self, name)(**args))
            except ToolError as exc:
                return result_payload({"code": exc.code, "message": str(exc), "detail": exc.detail}, True)
            except OSError as exc:
                return result_payload({"code": "IO_ERROR", "message": str(exc)}, True)
        raise RpcError(-32601, "Method not found")

    def list_workspaces(self):
        latest = {row["workspaceId"]: row["state"] for row in history.list_history(self.data)}
        return [{**row, **workspaces.git_state(row["path"]), "lastResult": latest.get(row["id"])}
                for row in workspaces.list_workspaces(self.data)]

    def list_recipes(self):
        tables = {table["id"]: table for table in config_records(self.data, "paramsets")}
        return [{
            "id": recipe["id"], "name": recipe["name"], "builtIn": recipe["builtIn"],
            "paramset": recipe.get("paramset"), "presets": sorted(tables.get(recipe.get("paramset"), {}).get("presets", {})),
            "steps": [{key: step[key] for key in ("name", "argv", "enabled", "useParams", "preset") if key in step}
                      for step in recipe.get("steps", []) if isinstance(step, dict)],
        } for recipe in recipes.list_recipes(self.data)]

    def describe_params(self, name):
        return paramsets.load(self.data, name)

    def start_build(self, workspace, recipe, preset=None, params=None):
        row = workspaces.get_workspace(self.data, workspace)
        results, warnings = jobs.start_many(self.data, [row], recipe, preset, overrides=params, restrict_values=True)
        return {"jobId": results[0]["id"], "state": results[0]["state"], "warnings": warnings}

    def get_job(self, jobId):
        row = jobs.get_job(self.data, jobId)
        if row["state"] in jobs.ACTIVE:
            jobs.reconcile(self.data)
            return jobs.get_job(self.data, jobId)
        return row

    def log(self, job_id):
        jobs.get_job(self.data, job_id)
        return jobs.job_dir(self.data, job_id) / "build.log"

    def read_log(self, jobId, tail=None, start=None, end=None):
        if tail is not None and (start is not None or end is not None):
            raise ToolError("INVALID_LOG_RANGE", "Use tail or a line range.")
        if start is not None or end is not None:
            start = start if start is not None else max(1, end - MAX_LOG_LINES + 1)
            end = end if end is not None else start + MAX_LOG_LINES - 1
            if not 1 <= end - start + 1 <= MAX_LOG_LINES:
                raise ToolError("INVALID_LOG_RANGE", "Read between 1 and 400 lines at a time.")
        lines = enumerate(logtools.iter_lines(self.log(jobId)), 1)
        selected = islice(lines, start - 1, end) if start is not None else deque(lines, maxlen=tail or 100)
        return {"jobId": jobId, "lines": [{"line": number, "text": text} for number, text in selected]}

    def search_log(self, jobId, q="", kind=None, index=0, limit=100):
        if not q and not kind:
            raise ToolError("INVALID_ARGUMENTS", "Provide a search keyword or category.")
        return logtools.search_page(self.log(jobId), q, kind, index, limit)

    def triage_job(self, jobId):
        report = logtools.triage(self.log(jobId))
        for field, count in (("errors", "errorCount"), ("cascadeErrors", "cascadeCount")):
            report[count] = len(report[field])
            report[field] = report[field][:100]
        return report

    def stop_job(self, jobId):
        return jobs.stop(self.data, jobId)

    def send_enter(self, jobId):
        row = jobs.get_job(self.data, jobId)
        if row["state"] != "running":
            raise ToolError("JOB_NOT_RUNNING", "Job is not running.")
        with jobs.process_token(row.get("pid") or 0) as token:
            if not jobs.runner_matches(row, token):
                raise ToolError("JOB_NOT_RUNNING", "Runner has already exited.")
        jobs.request_enter(self.data, jobId)
        return {"sent": True}

    def list_history(self, limit=20):
        return list(reversed(history.list_history(self.data)[-limit:]))


def error_payload(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_line(server, line):
    try:
        def reject_constant(value):
            raise ValueError(value)
        message = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return error_payload(None, -32700, "Parse error")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return error_payload(None, -32600, "Invalid Request")
    if "id" not in message:
        # This server has no subscriptions. Initialized and other client
        # notifications have no response.
        return None
    request_id = message["id"]
    if type(request_id) not in (int, str):
        return error_payload(None, -32600, "Invalid Request ID")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error_payload(request_id, -32602, "params must be an object")
    try:
        result = server.dispatch(message["method"], params)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        return error_payload(request_id, exc.code, str(exc))
    except Exception as exc:
        print(f"MCP request failed: {exc}", file=sys.stderr)
        return error_payload(request_id, -32603, "Internal error")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    args = parser.parse_args(argv)
    try:
        server = Server(data_dir(args.data_dir))
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    source, target = sys.stdin.buffer, sys.stdout.buffer
    while line := source.readline(MAX_REQUEST + 1):
        if len(line) > MAX_REQUEST:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_REQUEST + 1)
            response = error_payload(None, -32600, "Request exceeds 1 MB")
        else:
            response = handle_line(server, line)
        if response is not None:
            try:
                target.write(json.dumps(response, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n")
                target.flush()
            except BrokenPipeError:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
