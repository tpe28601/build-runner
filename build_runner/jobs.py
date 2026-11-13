"""Shared job registry, concurrency control, and Windows process lifecycle."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from . import artifacts, history, paramsets, recipes, workspaces
from .paths import ROOT, ToolError, file_lock, read_json, write_json

ACTIVE = {"queued", "running"}
RESTARTABLE = ACTIVE | {"stopped", "failed"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def lock(data):
    return file_lock(data / "jobs" / ".lock")


def git_lock_path(data):
    return data / "jobs" / ".git-write.lock"


def job_dir(data, job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ToolError("INVALID_JOB_ID", f"Invalid job ID: {job_id}")
    return data / "jobs" / job_id


def get_job(data, job_id):
    row = read_json(job_dir(data, job_id) / "status.json")
    if row is None:
        raise ToolError("JOB_NOT_FOUND", f"Job not found: {job_id}")
    return row


def remove_job_dir(data, job_id):
    """Delete a finished job's whole folder. Caller holds the job lock."""
    folder = job_dir(data, job_id)
    if not folder.exists():
        return False
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != (data / "jobs").resolve():
        raise ToolError("INVALID_JOB_PATH", f"Refusing to remove unexpected folder for job {job_id}.")
    shutil.rmtree(folder)
    return True


def sweep_orphans(data, *, max_age_sec=24 * 3600):
    """Delete job folders with no history row that sat untouched for max_age_sec.

    Covers rows whose history append failed and leftovers from older deletion
    behaviour. Caller holds the job lock (or runs before the server starts).
    """
    known = {row.get("id") for row in history.list_history(data)}
    cutoff = time.time() - max_age_sec
    removed = []
    jobs_root = data / "jobs"
    for folder in sorted(jobs_root.iterdir()) if jobs_root.is_dir() else []:
        if not re.fullmatch(r"[a-f0-9]{32}", folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            row = read_json(folder / "status.json")
        except (ToolError, OSError, ValueError):
            row = None   # corrupt status file: still sweep the folder once it is old enough
        if (isinstance(row, dict) and row.get("state") in ACTIVE) or folder.name in known:
            continue
        try:
            newest = max((item.stat().st_mtime for item in folder.rglob("*") if item.is_file()), default=0)
            if newest > cutoff:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(folder)
            removed.append(folder.name)
        except OSError:
            pass
    return removed


def save(data, row):
    write_json(job_dir(data, row["id"]) / "status.json", row)


def list_jobs(data):
    rows = [read_json(path) for path in (data / "jobs").glob("*/status.json")]
    return sorted((row for row in rows if row is not None), key=lambda row: (row["queuedAt"], row["id"]))


@contextmanager
def process_token(pid, wait_ms=0):
    """Keep the Windows process handle open while using its creation timestamp."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    filetime = ctypes.POINTER(wintypes.FILETIME)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            raise ctypes.WinError(ctypes.get_last_error())
        yield None
        return
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, created, exited, kernel_time, user_time):
            raise ctypes.WinError(ctypes.get_last_error())
        waited = kernel.WaitForSingleObject(handle, wait_ms)
        if waited not in (0, 258):  # WAIT_OBJECT_0 or WAIT_TIMEOUT
            raise ctypes.WinError(ctypes.get_last_error())
        yield (created.dwHighDateTime << 32) | created.dwLowDateTime if waited == 258 else None
    finally:
        kernel.CloseHandle(handle)


def runner_matches(row, token):
    return token is not None and token == row.get("pidToken")


def recover_dead(data, rows):
    for row in rows:
        if row["state"] == "running":
            with process_token(row["pid"]) as token:
                if not runner_matches(row, token):
                    finish(data, row, "failed", 1, message="Runner exited before recording a result.")


def reconcile(data):
    rows = list_jobs(data)
    for row in rows:
        if row["state"] == "running":
            with process_token(row["pid"]) as token:
                alive = runner_matches(row, token)
            if not alive:
                with lock(data):
                    dispatch(data)
                return list_jobs(data)
    return rows


def settings(data):
    result = read_json(data / "settings.json", {})
    if not isinstance(result, dict):
        raise ToolError("INVALID_SETTINGS", "settings.json must contain an object.")
    return result


def limit(data):
    value = settings(data).get("maxConcurrentJobs", 2)
    if type(value) is not int or value < 0:
        raise ToolError("INVALID_CONCURRENCY", "maxConcurrentJobs must be a nonnegative integer.")
    return value


def set_limit(data, value):
    if type(value) is not int or value < 0:
        raise ToolError("INVALID_CONCURRENCY", "Concurrency must be a nonnegative integer.")
    with lock(data):
        values = settings(data)
        values["maxConcurrentJobs"] = value
        write_json(data / "settings.json", values)
        dispatch(data)
    return value


def auto_skip_prompts(data):
    """Whether runners should press Enter on known input prompts by themselves."""
    try:
        return bool(settings(data).get("autoSkipPrompts", True))
    except ToolError:
        return True


def request_enter(data, job_id):
    """Ask a running job's runner to send one Enter key to the build step."""
    (job_dir(data, job_id) / "enter.request").write_text(now(), encoding="utf-8")


def check_conflicts(data, path, common, force=False, *, rows=None):
    warnings = []
    key = workspaces.path_key(path)
    for row in list_jobs(data) if rows is None else rows:
        if row["state"] not in ACTIVE:
            continue
        if workspaces.path_key(row["workspacePath"]) == key:
            if not force:
                raise ToolError("WORKSPACE_CONFLICT", "This folder already has an active job.", jobId=row["id"])
            warnings.append({"code": "WORKSPACE_CONFLICT", "message": "Same workspace folder.", "jobId": row["id"]})
        elif common and row.get("gitCommonDir") == common:
            warnings.append({"code": "SHARED_GIT_DIR", "message": "Workspaces share a Git repository.", "jobId": row["id"]})
    return warnings


def job_plan(workspace, recipe, recipe_name, preset, table, overrides, artifact_rules):
    plan = recipes.prepare(workspace, recipe, preset, table=table, overrides=overrides)
    plan["artifactRules"] = artifact_rules
    path = str(plan["workspacePath"])
    git = workspaces.git_state(path, include_dirty=False)
    preview = {
        "workspace": workspace["name"],
        "workspaceId": workspace["id"],
        "workspacePath": path,
        "recipe": recipe.get("name") or recipe_name,
        "argvPreview": [step["argv"] for step in plan["steps"]],
        "branch": git["branch"],
        "commit": git["commit"],
        "gitCommonDir": git["gitCommonDir"],
        "params": plan["params"],
    }
    return preview, plan


def start(data, workspace, recipe_name, preset=None, *, force=False, dry_run=False):
    rows, warnings = start_many(data, [workspace], recipe_name, preset, force=force, dry_run=dry_run)
    return rows[0], warnings


def create_queued_job(data, preview, plan, *, pending=False):
    row = {
        **preview,
        "id": uuid4().hex, "state": "queued", "step": None,
        "queuedAt": now(), "startedAt": None, "endedAt": None, "exitCode": None,
        "pid": None, "argv": [],
    }
    folder = job_dir(data, row["id"])
    write_json(folder / "plan.json", plan)
    (folder / "build.log").touch()
    write_json(folder / ("status.pending.json" if pending else "status.json"), row)
    return row


def start_many(data, workspace_rows, recipe_name, preset=None, *, force=False, dry_run=False, overrides=None,
               restrict_values=False):
    recipe = recipes.get_recipe(data, recipe_name)
    uses_params = any(isinstance(step, dict) and step.get("enabled") is not False and step.get("useParams")
                      for step in recipe["steps"])
    table = paramsets.load(data, recipe["paramset"]) if uses_params and recipe.get("paramset") else None
    artifact_rules = artifacts.load(data, recipe["artifacts"]) if recipe.get("artifacts") else None
    if overrides is not None and not isinstance(overrides, dict):
        raise ToolError("INVALID_PARAMS", "params must be an object.")
    if restrict_values and table and overrides:
        for param in table["params"]:
            value = overrides.get(param["key"])
            if (param["type"] == "value" and isinstance(value, str)
                    and (re.search(r'[&|<>^%!()"\r\n\x00]', value) or value.lstrip().startswith(("-", "/", "@")))):
                raise ToolError("INVALID_PARAM_VALUE", f"Unsafe free-form value for {param['flag']}.")
    plans = [job_plan(row, recipe, recipe_name, preset, table, overrides, artifact_rules) for row in workspace_rows]
    if not dry_run and os.name != "nt":
        raise ToolError("WINDOWS_REQUIRED", "Running jobs requires Windows.", 5)

    def validate(rows):
        if git_lock_path(data).exists():
            raise ToolError("JOB_BUSY", "A Git operation is in progress. Retry after it finishes.", 1)
        warnings = []
        for preview, _ in plans:
            warnings.extend(check_conflicts(data, preview["workspacePath"], preview["gitCommonDir"], force, rows=rows))
            rows.append({**preview, "state": "queued", "id": None})
        return warnings

    if dry_run:
        return [preview for preview, _ in plans], validate(list_jobs(data))

    with lock(data):
        limit(data)
        active = list_jobs(data)
        recover_dead(data, active)
        warnings = validate(active)
        ids = [create_queued_job(data, preview, plan)["id"] for preview, plan in plans]
        dispatch(data)
        return [get_job(data, job_id) for job_id in ids], warnings


def dispatch(data, *, breakaway=False):
    """Start queued jobs in order. Caller holds the job lock."""
    maximum = limit(data)
    rows = list_jobs(data)
    recover_dead(data, rows)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    if breakaway:
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    running = sum(row["state"] == "running" for row in rows)
    for row in rows:
        if maximum and running >= maximum:
            break
        if row["state"] != "queued":
            continue
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "build_runner.runner", row["id"], "--data-dir", str(data)],
                cwd=ROOT, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            finish(data, row, "failed", 1, message=f"Runner launch failed: {exc}")
            continue
        try:
            with process_token(child.pid) as token:
                if token is None:
                    finish(data, row, "failed", 1, message="Runner exited during startup.")
                    continue
                row.update(state="running", pid=child.pid, pidToken=token, startedAt=now())
            save(data, row)
        except BaseException:
            child.kill()
            child.wait()
            raise
        running += 1


def finish(data, row, state, exit_code, *, message=None, report=None):
    """Finalize a job while holding the job lock."""
    log_path = job_dir(data, row["id"]) / "build.log"
    ended = now()
    start = row["startedAt"]
    elapsed = (datetime.fromisoformat(ended) - datetime.fromisoformat(start)).total_seconds() if start else 0
    row.update(state=state, endedAt=ended, exitCode=exit_code)
    if message:
        row["message"] = message
    try:
        history.append(data, {
            **row, "durationSec": elapsed, "rootError": report["rootError"] if report else None,
            "logPath": str(log_path), "artifacts": row.get("artifacts", []),
        })
        for pruned_id in history.prune(data, settings(data).get("historyRetention", {})):
            if pruned_id != row["id"]:
                try:
                    remove_job_dir(data, pruned_id)
                except (OSError, ToolError):
                    pass
    except (OSError, ToolError) as exc:
        row["historyError"] = str(exc)
    save(data, row)
    return row


def kill_tree(pid):
    run = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"], shell=False,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    with process_token(pid, wait_ms=3000) as token:
        if token is not None:
            raise ToolError("STOP_FAILED", f"Process {pid} did not exit after taskkill.", 1,
                            taskkillExitCode=run.returncode)


def stop_active_job(data, row):
    """Stop and finalize an active job. Caller holds the job lock."""
    if row["state"] == "running":
        with process_token(row["pid"]) as token:
            if not runner_matches(row, token):
                return finish(data, row, "failed", 1, message="Runner exited before recording a result.")
            kill_tree(row["pid"])
    return finish(data, row, "stopped", 4)


def stop(data, job_id):
    with lock(data):
        row = get_job(data, job_id)
        if row["state"] not in ACTIVE:
            return row
        result = stop_active_job(data, row)
        dispatch(data)
        return result


def restart(data, job_id):
    """Re-run the saved plan as a new job; returns (replacement row, warnings)."""
    snapshot = get_job(data, job_id)
    # Read the checkout without blocking other jobs while Git answers.
    git = (workspaces.git_state(snapshot["workspacePath"], include_dirty=False)
           if snapshot["state"] in RESTARTABLE else None)
    with lock(data):
        original = get_job(data, job_id)
        replacement_id = original.get("restartedAs")
        if replacement_id:
            folder = job_dir(data, replacement_id)
            if (folder / "status.json").is_file():
                return get_job(data, replacement_id), []
            if not (folder / "status.pending.json").is_file():
                replacement_id = None
        if not replacement_id and original["state"] not in RESTARTABLE:
            raise ToolError("JOB_NOT_ACTIVE", "只能重新建置執行中、排隊中、已停止或失敗的 Job。")

        if git_lock_path(data).exists():
            raise ToolError("JOB_BUSY", "A Git operation is in progress. Retry after it finishes.", 1)
        limit(data)
        peers = [row for row in list_jobs(data) if row["id"] != job_id]
        recover_dead(data, peers)
        if not replacement_id and git is None:
            # Rare race: the state turned restartable while the lock was held.
            # Re-reading git inside the lock is the trade for keeping the
            # common path lock-free.
            git = workspaces.git_state(original["workspacePath"], include_dirty=False)
        git_common = git["gitCommonDir"] if git else original.get("gitCommonDir")
        warnings = check_conflicts(data, original["workspacePath"], git_common, rows=peers)

        if not replacement_id:
            plan = read_json(job_dir(data, job_id) / "plan.json")
            if not isinstance(plan, dict) or not plan.get("steps"):
                raise ToolError("JOB_PLAN_NOT_FOUND", "找不到原 Job 的執行計畫。")
            if any(not Path(step["cwd"]).is_dir() for step in plan["steps"]):
                raise ToolError("INVALID_WORKSPACE", "原 Job 的工作資料夾已不存在。")
            preview = {key: original.get(key) for key in (
                "workspace", "workspaceId", "workspacePath", "recipe", "argvPreview",
                "params",
            )}
            preview.update(branch=git["branch"], commit=git["commit"], gitCommonDir=git_common,
                           restartedFrom=job_id)
            replacement = create_queued_job(data, preview, plan, pending=True)
            replacement_id = replacement["id"]
            original["restartedAs"] = replacement_id
            save(data, original)

        # Keep the replacement out of the dispatcher until the original is stopped.
        # Retain the staged job ID so a failed publication can be retried.
        pending = job_dir(data, replacement_id) / "status.pending.json"
        if not pending.is_file():
            raise ToolError("JOB_NOT_FOUND", "找不到重來後的 Job。")
        if original["state"] in ACTIVE:
            stop_active_job(data, original)
        os.replace(pending, pending.with_name("status.json"))
        dispatch(data)
        return get_job(data, replacement_id), warnings


def wait(data, job_id):
    next_dispatch = time.monotonic()
    while True:
        row = get_job(data, job_id)
        if row["state"] not in ACTIVE:
            return row
        if row["state"] == "queued":
            alive = False
        else:
            with process_token(row["pid"]) as token:
                alive = runner_matches(row, token)
        if not alive and time.monotonic() >= next_dispatch:
            with lock(data):
                dispatch(data)
            next_dispatch = time.monotonic() + 1
        time.sleep(0.2)
