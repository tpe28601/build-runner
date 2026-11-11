"""HTTP routes over the Build Runner core."""

import base64
import binascii
from dataclasses import dataclass
from itertools import islice
import os
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

from . import ai, artifacts, history, jobs, logtools, paramsets, recipes, schedule, workspaces
from .paths import ToolError, config_records, file_lock, named_file, save_config, write_json


class HttpError(Exception):
    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status = status
        self.response = {"ok": False, "error": {"code": code, "message": message, "detail": detail or {}}}


@dataclass
class Download:
    data: bytes
    filename: str


def ok(result, status=200, **extra):
    return status, {"ok": True, "result": result, **extra}


def string(body, field):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise HttpError(400, "INVALID_ARGUMENTS", f"請提供有效的 {field}。")
    return value.strip()


def strings(body, field):
    values = body.get(field)
    if (not isinstance(values, list) or not 1 <= len(values) <= 100
            or any(not isinstance(value, str) or not value.strip() or "\0" in value for value in values)
            or len(set(values)) != len(values)):
        raise HttpError(400, "INVALID_ARGUMENTS", f"{field} 必須包含 1 到 100 個不同的字串。")
    return values


def confirm(body):
    if body.get("confirm") is not True:
        raise HttpError(400, "CONFIRM_REQUIRED", "此操作需要 confirm: true。")


def integer(query, field, default, minimum=0, maximum=2**53 - 1):
    value = query.get(field, [str(default)])[0]
    if not re.fullmatch(r"[0-9]{1,16}", value) or not minimum <= int(value) <= maximum:
        raise HttpError(400, "INVALID_ARGUMENTS", f"{field} 超出允許範圍。")
    return int(value)


def tool_error(exc):
    status = 400
    if exc.code.endswith("_NOT_FOUND"):
        status = 404
    elif exc.code in ("JOB_BUSY", "WORKSPACE_CONFLICT", "WORKSPACE_EXISTS"):
        status = 409
    elif exc.exit_code in (1, 5):
        status = 500
    return HttpError(status, exc.code, str(exc), exc.detail)


class Api:
    def __init__(self, data, manifest, server):
        self.data = data
        self.manifest = manifest
        self.server = server
        self.started = time.monotonic()
        self.mutations = threading.Lock()
        self.git_tasks = {}
        self.git_busy = False
        self.artifact_previews = {}
        self.artifact_tasks = {}
        self.analyses = ai.Analyses()

    def route(self, method, path, query, body):
        try:
            return self.dispatch(method, path, query, body)
        except ToolError as exc:
            raise tool_error(exc) from exc

    def dispatch(self, method, path, query, body):
        if method == "GET" and path == "/api/health":
            return 200, {"ok": True, "id": self.manifest["id"], "version": self.manifest["version"],
                         "pid": os.getpid(), "port": self.server.server_port,
                         "uptimeSec": int(time.monotonic() - self.started)}
        if method == "GET" and path == "/api/info":
            return 200, {"ok": True, **self.manifest, "dataDir": str(self.data)}
        if method == "POST" and path == "/api/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return ok({"closing": True})
        if path == "/api/workspaces":
            return self.workspace_route(method, body)
        if method == "POST" and path == "/api/browse":
            path = body.get("path", "")
            if not isinstance(path, str) or "\0" in path:
                raise HttpError(400, "INVALID_FOLDER", "請提供有效的資料夾路徑。")
            if body.get("relative") is True:
                if not body.get("workspaceId"):
                    return ok({"current": None, "parent": None, "selection": None, "truncated": False,
                               "items": [{"name": row["name"], "path": "", "workspaceId": row["id"]}
                                         for row in workspaces.list_workspaces(self.data)]})
                workspace = self.workspace_by_id(string(body, "workspaceId"))
                return ok(workspaces.browse_folder(path.strip(), workspace["path"]))
            return ok(workspaces.browse_folder(path.strip()))
        if method == "POST" and path == "/api/workspaces/import-worktrees":
            return self.import_worktrees(body)
        git_route = re.fullmatch(r"/api/workspaces/([\w-]+)/git", path)
        if method == "POST" and git_route:
            return self.git_action(self.workspace_by_id(git_route[1]), body)
        task = re.fullmatch(r"/api/git-tasks/([a-f0-9]{32})", path)
        if method == "GET" and task:
            with self.mutations:
                if task[1] not in self.git_tasks:
                    raise HttpError(404, "TASK_NOT_FOUND", "找不到 Git 工作。")
                return ok(self.git_tasks[task[1]].copy())
        if path == "/api/recipes":
            return self.recipe_route(method, body)
        if path == "/api/paramsets":
            return self.config_route(method, body, "paramsets", paramsets.normalize)
        if method == "POST" and path == "/api/paramsets/import":
            return self.import_params(body)
        if method in ("POST", "DELETE") and path == "/api/paramsets/presets":
            return self.change_preset(method, body)
        if method == "POST" and path == "/api/paramsets/conflicts":
            return self.update_conflicts(body)
        if path == "/api/artifact-rules":
            return self.config_route(method, body, "artifacts", artifacts.normalize)
        if method == "POST" and path == "/api/artifact-rules/preview":
            return self.preview_artifacts(body)
        if method == "POST" and path == "/api/artifact-rules/execute":
            return self.execute_artifacts(body)
        artifact_task = re.fullmatch(r"/api/artifact-tasks/([a-f0-9]{32})", path)
        if method == "GET" and artifact_task:
            with self.mutations:
                row = self.artifact_tasks.get(artifact_task[1])
                if row is None:
                    raise HttpError(404, "TASK_NOT_FOUND", "找不到產出物工作。")
                return ok(row.copy())
        if path == "/api/settings":
            if method == "GET":
                return ok(self.public_settings(jobs.settings(self.data)))
            if method == "POST":
                return self.update_settings(body)
        task = re.fullmatch(r"/api/ai-tasks/([a-f0-9]{32})(/cancel)?", path)
        if task:
            if method == "GET" and not task[2]:
                return ok(self.analyses.get(task[1]))
            if method == "POST" and task[2]:
                return ok(self.analyses.cancel(task[1]))
        if path == "/api/jobs" and method == "GET":
            return ok(list(reversed(jobs.reconcile(self.data))))
        if method == "POST" and path in ("/api/jobs", "/api/jobs/check"):
            return self.start_jobs(body, path.endswith("/check"))
        job = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/([a-z]+))?", path)
        if job:
            return self.job_route(method, job[1], job[2], query, body)
        if method == "GET" and path == "/api/history":
            return ok(self.history_rows({key: values[0] for key, values in query.items()}))
        if method == "POST" and path == "/api/history/export":
            fmt = string(body, "format")
            if fmt not in ("csv", "json"):
                raise HttpError(400, "INVALID_FORMAT", "匯出格式須為 csv 或 json。")
            return ok({"format": fmt, "content": history.export(self.history_rows(body), fmt)})
        if method == "POST" and path == "/api/history/delete":
            return self.delete_history(body)
        if path == "/api/schedules":
            return self.schedule_route(method, body)
        raise HttpError(404, "NOT_FOUND", "找不到指定的 API 或檔案。")

    @staticmethod
    def public_settings(values):
        return {"maxConcurrentJobs": values.get("maxConcurrentJobs", 2),
                "historyRetention": {"days": 0, "count": 0} | values.get("historyRetention", {}),
                "notifications": {"desktop": False, "title": True, "sound": False} | values.get("notifications", {}),
                "ai": ai.public(ai.configure(values.get("ai", {}), {}))}

    def update_settings(self, body):
        with jobs.lock(self.data):
            values = jobs.settings(self.data)
            if "maxConcurrentJobs" in body:
                limit = body["maxConcurrentJobs"]
                if type(limit) is not int or limit < 0:
                    raise ToolError("INVALID_CONCURRENCY", "Concurrency must be a nonnegative integer.")
                values["maxConcurrentJobs"] = limit
            if "historyRetention" in body:
                rules = body["historyRetention"]
                if (not isinstance(rules, dict) or any(type(rules.get(key, 0)) is not int
                        or not 0 <= rules.get(key, 0) <= 100000 for key in ("days", "count"))):
                    raise HttpError(400, "INVALID_RETENTION", "保留天數與筆數須為非負整數。")
                if any(rules.get(key, 0) for key in ("days", "count")):
                    confirm(body)
                values["historyRetention"] = {key: rules.get(key, 0) for key in ("days", "count")}
            if "notifications" in body:
                flags = body["notifications"]
                if (not isinstance(flags, dict) or any(key not in ("desktop", "title", "sound")
                        or type(value) is not bool for key, value in flags.items())):
                    raise HttpError(400, "INVALID_NOTIFICATIONS", "通知設定必須是布林值。")
                values["notifications"] = values.get("notifications", {}) | flags
            if "ai" in body:
                values["ai"] = ai.configure(values.get("ai", {}), body["ai"])
            response = self.public_settings(values)
            write_json(self.data / "settings.json", values)
            if "historyRetention" in body:
                for pruned_id in history.prune(self.data, values["historyRetention"]):
                    try:
                        jobs.remove_job_dir(self.data, pruned_id)
                    except (OSError, ToolError):
                        pass
            if "maxConcurrentJobs" in body:
                jobs.dispatch(self.data)
        return ok(response)

    @staticmethod
    def history_ids(values):
        if (not isinstance(values, list) or not 1 <= len(values) <= 10000
                or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{32}", item) for item in values)):
            raise HttpError(400, "INVALID_HISTORY_IDS", "請選擇有效的歷史紀錄。")
        return set(values)

    def history_rows(self, filters):
        rows = history.filtered(history.list_history(self.data), filters.get("workspace"),
                                filters.get("state"), filters.get("start"), filters.get("end"))
        if "ids" in filters:
            ids = self.history_ids(filters["ids"])
            rows = [row for row in rows if row["id"] in ids]
        return list(reversed(rows))

    def delete_history(self, body):
        confirm(body)
        ids = self.history_ids(body.get("ids"))
        warnings = []
        with jobs.lock(self.data):
            rows = history.list_history(self.data)
            removed = [row for row in rows if row["id"] in ids]
            active = {row["id"] for row in jobs.list_jobs(self.data) if row["state"] in jobs.ACTIVE}
            blocked = next((row["id"] for row in removed if row["id"] in active), None)
            if blocked:
                raise HttpError(409, "JOB_ACTIVE", "有紀錄對應的 Job 仍在排隊或執行中，請先停止再刪除。")
            analysing = next((row["id"] for row in removed if self.analyses.active_for(row["id"])), None)
            if analysing:
                raise HttpError(409, "AI_RUNNING", "有 Job 正在進行 AI 分析，請先結束分析再刪除。")
            deleted = 0
            if removed:
                history.replace(self.data, [row for row in rows if row["id"] not in ids])
                for row in removed:
                    try:
                        if jobs.remove_job_dir(self.data, row["id"]):
                            deleted += 1
                    except (OSError, ToolError) as exc:
                        warnings.append({"jobId": row["id"], "message": str(exc)})
        return ok({"deleted": len(removed), "deletedJobs": deleted}, warnings=warnings)

    def schedule_route(self, method, body):
        if method == "GET":
            return ok(schedule.list_tasks(self.data))
        if method == "DELETE":
            confirm(body)
            name = string(body, "name")
            schedule.delete(self.data, name)
            return ok({"name": name})
        if "enabled" in body:
            name = string(body, "name")
            schedule.change(self.data, name, body["enabled"])
            return ok({"name": name, "enabled": body["enabled"]})
        row = schedule.normalize(body)
        workspaces.get_workspace(self.data, row["workspace"])
        recipe = recipes.get_recipe(self.data, row["recipe"])
        if row["preset"]:
            if not recipe.get("paramset"):
                raise HttpError(400, "INVALID_SCHEDULE", "此 Recipe 沒有參數表。")
            paramsets.resolve(paramsets.load(self.data, recipe["paramset"]), row["preset"])
        return ok(schedule.create(self.data, row), 201)

    def workspace_by_id(self, workspace_id):
        for row in workspaces.list_workspaces(self.data):
            if row["id"] == workspace_id:
                return row
        raise HttpError(404, "WORKSPACE_NOT_FOUND", "找不到指定的 Workspace。")

    def workspace_route(self, method, body):
        if method == "GET":
            last = {row["workspaceId"]: row["state"] for row in history.list_history(self.data)}
            return ok([{**row, **workspaces.git_state(row["path"]), "lastResult": last.get(row["id"])}
                       for row in workspaces.list_workspaces(self.data)])
        if method == "POST" and "id" not in body:
            return ok(workspaces.add_workspace(self.data, string(body, "name"), string(body, "path")), 201)
        workspace_id = string(body, "id")
        if method == "DELETE":
            confirm(body)
        with file_lock(self.data / ".workspaces.lock"):
            current = workspaces.list_workspaces(self.data)
            row = next((row for row in current if row["id"] == workspace_id), None)
            if not row:
                raise HttpError(404, "WORKSPACE_NOT_FOUND", "找不到指定的 Workspace。")
            if method == "POST":
                name = string(body, "name")
                if any(other is not row and other["name"].casefold() == name.casefold() for other in current):
                    raise HttpError(409, "WORKSPACE_EXISTS", "此 Workspace 名稱已存在。")
                row["name"] = name
            else:
                current.remove(row)
            write_json(self.data / "workspaces.json", current)
        return ok(row)

    def import_worktrees(self, body):
        path = string(body, "path")
        choices = {workspaces.path_key(value): value for value in workspaces.list_worktrees(path)}
        if "paths" not in body:
            return ok(list(choices.values()))
        selected = [workspaces.path_key(value) for value in strings(body, "paths")]
        if any(key not in choices for key in selected):
            raise HttpError(400, "INVALID_WORKTREE", "選取的資料夾必須屬於這個 Git repo。")
        added, warnings = [], []
        with file_lock(self.data / ".workspaces.lock"):
            current = workspaces.list_workspaces(self.data)
            keys = {workspaces.path_key(row["path"]) for row in current}
            names = {row["name"].casefold() for row in current}
            for key in selected:
                folder = Path(choices[key])
                if key in keys or not folder.is_dir():
                    warnings.append({"code": "WORKTREE_SKIPPED", "message": str(folder)})
                    continue
                base = folder.name or "Workspace"
                name, suffix = base, 2
                while name.casefold() in names:
                    name = f"{base} ({suffix})"
                    suffix += 1
                row = {"id": uuid4().hex, "name": name, "path": str(folder.resolve())}
                names.add(name.casefold())
                keys.add(key)
                added.append(row)
            if added:
                write_json(self.data / "workspaces.json", [*current, *added])
        return ok(added, 201, warnings=warnings)

    def git_action(self, workspace, body):
        action = string(body, "action")
        if action not in ("refresh", "pull", "checkout"):
            raise HttpError(400, "INVALID_GIT_ACTION", "無效的 Git 操作。")
        branch = string(body, "branch") if action == "checkout" else None
        if action != "refresh":
            confirm(body)
        with self.mutations:
            if self.git_busy:
                raise HttpError(409, "JOB_BUSY", "已有 Git 工作正在執行。")
            while len(self.git_tasks) >= 30:
                self.git_tasks.pop(next(iter(self.git_tasks)))
            task_id = uuid4().hex
            self.git_tasks[task_id] = {"id": task_id, "state": "running"}
            self.git_busy = True
        threading.Thread(target=self.run_git, args=(task_id, workspace, action, branch), daemon=action == "refresh").start()
        return ok(self.git_tasks[task_id].copy(), 202)

    def run_git(self, task_id, workspace, action, branch):
        path = workspace["path"]
        outcome = {"id": task_id}
        try:
            git = workspaces.git_state(path, include_dirty=False)
            refs = git_refs(path) if git["gitCommonDir"] else []
            if action != "refresh":
                self.mutate_git(path, git["gitCommonDir"], action, branch, refs)
            outcome.update(state="success", workspace={**workspace, **workspaces.git_state(path)}, branches=refs)
        except ToolError as exc:
            outcome.update(state="failed", error=tool_error(exc).response["error"])
        except OSError as exc:
            outcome.update(state="failed", error={"code": "GIT_FAILED", "message": str(exc)})
        finally:
            with self.mutations:
                self.git_tasks[task_id] = outcome
                self.git_busy = False

    def mutate_git(self, path, common, action, branch, refs):
        with file_lock(jobs.git_lock_path(self.data)):
            # Synchronize with a build submission that passed validation just
            # before this Git operation acquired its reservation.
            with jobs.lock(self.data):
                jobs.dispatch(self.data)
                rows = jobs.list_jobs(self.data)
                active = [row for row in rows if row["state"] in jobs.ACTIVE]
                key = workspaces.path_key(path)
                if any(workspaces.path_key(row["workspacePath"]) == key or
                       (common and row["gitCommonDir"] == common) for row in active):
                    raise ToolError("JOB_BUSY", "此 Git repo 有執行中或排隊的 build。")
            if action == "pull":
                workspaces.git(path, "pull", "--ff-only", required=True, timeout=120)
            elif branch not in refs:
                raise ToolError("INVALID_BRANCH", "請選擇列出的 local 或 remote branch。")
            elif branch.startswith("refs/heads/"):
                workspaces.git(path, "switch", "--", branch.removeprefix("refs/heads/"), required=True)
            else:
                workspaces.git(path, "switch", "--track", "--", branch.removeprefix("refs/remotes/"), required=True)

    def recipe_route(self, method, body):
        if method == "GET":
            return ok(recipes.list_recipes(self.data))
        if body.get("id") in recipes.BUILTIN_IDS or body.get("name") in recipes.BUILTIN_IDS:
            raise HttpError(400, "BUILTIN_RECIPE_READ_ONLY", "內建 Recipe 請先複製再編輯。")

        def normalize(record):
            steps = record.get("steps")
            if not isinstance(steps, list) or not steps or any(
                    not isinstance(step, dict) or not isinstance(step.get("argv"), list) or not step["argv"]
                    or any(not isinstance(arg, str) or "\0" in arg for arg in step["argv"]) or not step["argv"][0]
                    for step in steps):
                raise HttpError(400, "INVALID_RECIPE", "Recipe 必須包含有 argv 的步驟。")
            env = record.get("env", {})
            timeout = record.get("timeoutMin", 180)
            if (not isinstance(env, dict)
                    or any(not key or "=" in key or "\0" in key or not isinstance(value, str) or "\0" in value
                           for key, value in env.items())
                    or type(timeout) is not int or timeout < 1):
                raise HttpError(400, "INVALID_RECIPE", "環境變數須為字串，逾時分鐘數須為正整數。")
            return record
        return self.config_route(method, body, "recipes", normalize)

    def config_route(self, method, body, folder, normalize):
        if method == "GET":
            return ok(list(config_records(self.data, folder)))
        name = string(body, "name")
        if method == "DELETE":
            confirm(body)
            path = named_file(self.data, folder, name)
            with file_lock(self.data / f".{folder}.lock"):
                try:
                    path.unlink()
                except FileNotFoundError as exc:
                    raise HttpError(404, "RECORD_NOT_FOUND", "找不到指定的設定。") from exc
            return ok({"name": name})
        record = {key: value for key, value in body.items() if key not in ("confirm", "id")}
        result = normalize({**record, "name": name})
        existed = save_config(self.data, folder, result, overwrite=body.get("confirm") is True, identity=body.get("id"))
        return ok(result, 200 if existed else 201)

    def import_params(self, body):
        name = string(body, "name")
        filename = string(body, "filename")
        try:
            raw = base64.b64decode(string(body, "content"), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HttpError(400, "INVALID_UPLOAD", "檔案上傳內容無效。") from exc
        command = string(body, "command") if body.get("command") else None
        table = paramsets.parse(raw, filename, name, command)
        if body.get("preview") is True:
            return ok(table)
        existed = save_config(self.data, "paramsets", table, overwrite=body.get("confirm") is True)
        return ok(table, 200 if existed else 201)

    def change_preset(self, method, body):
        name = string(body, "name")
        key = string(body, "preset")
        with file_lock(self.data / ".paramsets.lock"):
            table = paramsets.load(self.data, name)
            if method == "DELETE":
                confirm(body)
                if key not in table["presets"]:
                    raise HttpError(404, "PRESET_NOT_FOUND", "找不到指定的 preset。")
                del table["presets"][key]
            else:
                if key in table["presets"]:
                    confirm(body)
                table["presets"][key] = body.get("values")
                table = paramsets.normalize(table)
                paramsets.resolve(table, key)
            write_json(named_file(self.data, "paramsets", name), table)
        return ok(table)

    def update_conflicts(self, body):
        confirm(body)
        name = string(body, "name")
        with file_lock(self.data / ".paramsets.lock"):
            table = paramsets.load(self.data, name)
            table = paramsets.normalize({**table, "conflicts": body.get("conflicts")})
            write_json(named_file(self.data, "paramsets", name), table)
        return ok(table)

    def preview_artifacts(self, body):
        config = artifacts.load(self.data, string(body, "name"))
        workspace = self.workspace_by_id(string(body, "workspaceId"))
        preview_id = uuid4().hex
        context = {"workspace": workspace["name"], "jobId": preview_id, **workspaces.git_state(workspace["path"], False)}
        if body.get("jobId"):
            job = jobs.get_job(self.data, string(body, "jobId"))
            if job["workspaceId"] != workspace["id"] or job["state"] != "success":
                raise HttpError(400, "INVALID_ARTIFACT_JOB", "請選擇此 Workspace 已成功的 Job。")
            context = {**job, "jobId": job["id"]}
        plan = artifacts.preview(workspace["path"], config, context)
        with self.mutations:
            while len(self.artifact_previews) >= 20:
                self.artifact_previews.pop(next(iter(self.artifact_previews)))
            self.artifact_previews[preview_id] = (time.monotonic() + 600, plan)
        return ok({**plan, "previewId": preview_id})

    def execute_artifacts(self, body):
        confirm(body)
        preview_id = string(body, "previewId")
        with self.mutations:
            if any(row["state"] == "running" for row in self.artifact_tasks.values()):
                raise HttpError(409, "JOB_BUSY", "已有產出物工作正在執行。")
            preview = self.artifact_previews.pop(preview_id, None)
            if not preview or preview[0] < time.monotonic():
                raise HttpError(404, "ARTIFACT_PREVIEW_EXPIRED", "預覽已失效，請重新預覽。")
            while len(self.artifact_tasks) >= 20:
                self.artifact_tasks.pop(next(iter(self.artifact_tasks)))
            task_id = uuid4().hex
            task = {"id": task_id, "state": "running"}
            self.artifact_tasks[task_id] = task
        threading.Thread(target=self.run_artifacts, args=(task_id, preview[1])).start()
        return ok(task, 202)

    def run_artifacts(self, task_id, plan):
        try:
            outcome = artifacts.execute(plan)
            row = {"id": task_id, "state": "failed" if outcome["errors"] else "success", "result": outcome}
        except (OSError, ToolError) as exc:
            row = {"id": task_id, "state": "failed", "result": {"files": [], "archives": [],
                                                               "errors": [{"message": str(exc)}]}}
        with self.mutations:
            self.artifact_tasks[task_id] = row

    def start_jobs(self, body, preview):
        ids = strings(body, "workspaceIds")
        name = string(body, "recipe")
        rows = [self.workspace_by_id(workspace_id) for workspace_id in ids]
        if preview:
            jobs.reconcile(self.data)
        preset = string(body, "preset") if body.get("preset") is not None else None
        result, warnings = jobs.start_many(self.data, rows, name, preset,
                                           force=body.get("confirm") is True, dry_run=preview,
                                           overrides=body.get("params"))
        return ok(result, 200 if preview else 201, warnings=warnings)

    def job_route(self, method, job_id, action, query, body):
        row = jobs.get_job(self.data, job_id)
        path = jobs.job_dir(self.data, job_id) / "build.log"
        if method == "GET":
            if action is None:
                return ok(row)
            if action == "log":
                if "download" in query:
                    if not path.is_file():
                        raise HttpError(404, "LOG_NOT_FOUND", "Log file not found.")
                    label = re.sub(r"[^A-Za-z0-9._-]+", "-",
                                   f"{row.get('workspace') or ''}-{row.get('branch') or ''}-{job_id[:8]}")
                    label = label.strip("-.") or job_id
                    return 200, Download(path.read_bytes(), f"{label}.log")
                if "line" in query:
                    line = integer(query, "line", 1, minimum=1)
                    start = max(1, line - 10)
                    context = list(islice(logtools.iter_lines(path), start - 1, line + 10))
                    return ok({"firstLine": start, "lines": context})
                if "tail" in query:
                    return ok(logtools.tail(path, integer(query, "tail", 2000, minimum=1, maximum=50000),
                                            final=row["state"] not in jobs.ACTIVE))
                offset = integer(query, "from", 0)
                return ok(logtools.read_from(path, offset, final=row["state"] not in jobs.ACTIVE))
            if action == "search":
                q = query.get("q", [""])[0]
                kind = query.get("kind", [""])[0] or None
                if kind not in (None, "error", "fail", "warning"):
                    raise HttpError(400, "INVALID_SEARCH_KIND", "搜尋類型必須是 error、fail 或 warning。")
                return ok(logtools.search_page(path, q, kind, integer(query, "index", 0),
                                               integer(query, "limit", 100, maximum=100)))
            if action == "triage":
                report = logtools.triage(path)
                report["cascadeCount"] = len(report["cascadeErrors"])
                report["errorCount"] = len(report["errors"])
                report["cascadeErrors"] = report["cascadeErrors"][:100]
                report["errors"] = report["errors"][:100]
                return ok(report)
        if method == "POST" and action == "stop":
            confirm(body)
            return ok(jobs.stop(self.data, job_id))
        if action == "ai":
            folder = path.parent
            if method == "GET":
                if query.get("active") == ["1"]:
                    return ok(self.analyses.active_for(job_id))
                if "file" in query:
                    name = query["file"][0]
                    file = folder / name
                    if not ai.ANSWER.fullmatch(name) or not file.is_file() or file.is_symlink():
                        raise HttpError(404, "AI_ANSWER_NOT_FOUND", "找不到分析結果。")
                    return ok({"file": name, "answer": file.read_text(encoding="utf-8")})
                return ok(sorted((file.name for file in folder.glob("ai-*.md") if ai.ANSWER.fullmatch(file.name)), reverse=True))
            if method == "POST":
                if row["state"] in jobs.ACTIVE:
                    raise HttpError(409, "JOB_BUSY", "請等候建置結束後再分析。")
                if not path.is_file():
                    raise HttpError(404, "LOG_NOT_FOUND", "找不到建置 log。")
                config = ai.configure(jobs.settings(self.data).get("ai", {}), {})
                selected = body.get("method", config["method"])
                if selected not in ai.METHODS:
                    raise HttpError(400, "INVALID_AI_METHOD", "請選擇有效的 AI 分析方式。")
                scope = body.get("logScope", "around")
                if scope not in ("around", "tail", "full", "range"):
                    raise HttpError(400, "INVALID_LOG_SCOPE", "log 範圍必須是 around、tail、full 或 range。")
                bounds = []
                for field in ("logStart", "logEnd"):
                    value = body.get(field, 1)
                    if type(value) is not int or value < 1:
                        raise HttpError(400, "INVALID_LOG_RANGE", f"{field} 必須是正整數。")
                    bounds.append(value)
                try:
                    prompt = ai.prompt_for(row, path, config["template"], scope, *bounds)
                except ToolError as exc:
                    raise HttpError(400, exc.code, str(exc)) from exc
                if body.get("preview") is True:
                    return ok({"prompt": prompt, "scope": scope})
                if selected == "copy":
                    return ok({"prompt": prompt})
                return ok(self.analyses.start(selected, config, row, folder, prompt), 202)
        raise HttpError(404, "NOT_FOUND", "找不到指定的 Job API。")


def git_refs(path):
    refs = workspaces.git(path, "for-each-ref", "--format=%(refname)%09%(symref)",
                          "refs/heads", "refs/remotes", required=True)
    return [ref.partition("\t")[0] for ref in refs.splitlines() if not ref.partition("\t")[2]]
