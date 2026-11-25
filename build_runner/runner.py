"""Execute one persisted job in an independent process."""

import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time

from . import artifacts, console, jobs, junctions, logtools, prompts, recipes, workspaces
from .paths import data_dir, read_json


def contain_process_tree():
    """Have Windows terminate remaining build descendants when this runner exits."""
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("perProcessTime", ctypes.c_int64), ("perJobTime", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("minWorkingSet", ctypes.c_size_t),
            ("maxWorkingSet", ctypes.c_size_t), ("activeProcesses", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("schedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("ioCounters", ctypes.c_uint64 * 6),
            ("processMemory", ctypes.c_size_t), ("jobMemory", ctypes.c_size_t),
            ("peakProcessMemory", ctypes.c_size_t), ("peakJobMemory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    # A replacement runner must leave this job. Build commands inherit it.
    limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not (
        kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    # The non-inheritable handle stays open until the runner process exits.


class EnterForwarder:
    """Unblock build steps that stop on an interactive input prompt.

    ``web_api`` drops an ``enter.request`` file in the job folder when the user
    presses the Enter button, and this forwarder consumes it while waiting. The
    runner also watches the log tail for known credential prompts (unless the
    autoSkipPrompts setting is turned off) and presses Enter itself once the
    output has clearly stalled on one.
    """

    POLL_SEC = 0.5
    STALL_SEC = 1.5
    COOLDOWN_SEC = 2.0   # username/password/passphrase prompts often arrive back to back
    MAX_AUTO = 5
    TAIL_BYTES = 4096

    def __init__(self, log, log_path, request_path, *, auto_skip=True):
        self.log = log
        self.log_path = log_path
        self.request_path = request_path
        self.auto_skip = auto_skip
        self.auto_sent = 0
        self.last_auto = 0.0
        self.auto_gave_up = False
        self.prompt = False
        self.stalled_at = 0.0
        self.offset = None
        self.reader = None

    def wait(self, process, deadline):
        """process.wait() that also answers input prompts until *deadline*."""
        self.prompt = False
        self.stalled_at = time.monotonic()
        self.offset = None
        try:
            self.request_path.unlink()   # a request from a previous step must not leak
        except OSError:
            pass
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    code = process.poll()   # a step finishing exactly on the deadline still counts
                    if code is not None:
                        return code
                    raise subprocess.TimeoutExpired(process.args, remaining)
                try:
                    return process.wait(timeout=min(self.POLL_SEC, remaining))
                except subprocess.TimeoutExpired:
                    pass
                if self.request_path.exists():
                    self.send(process, "manual")
                else:
                    self.watch(process)
        finally:
            if self.reader is not None:
                try:
                    self.reader.close()
                except OSError:
                    pass
                self.reader = None
            try:
                process.stdin.close()
            except OSError:
                pass

    def watch(self, process):
        """Press Enter when the log tail sits on a known prompt with no growth."""
        if not self.auto_skip or self.auto_gave_up:
            return
        try:
            if self.reader is None:
                # One shared read handle: seek(0, 2) reports the true EOF even
                # while the step is writing, unlike stat() on the directory entry.
                self.reader = self.log_path.open("rb")
            size = self.reader.seek(0, 2)
        except OSError:
            return
        if size != self.offset:
            # The first sample (offset is None) lands here too: the step may
            # already sit on a prompt written before the first poll tick.
            start = max(0, size - self.TAIL_BYTES)
            try:
                self.reader.seek(start)
                chunk = self.reader.read(size - start)
            except OSError:
                return
            self.offset = size
            self.stalled_at = time.monotonic()
            self.prompt = prompts.ends_with_prompt(chunk.decode("utf-8", "replace"))
            return
        now = time.monotonic()
        if not self.prompt or now - self.stalled_at < self.STALL_SEC or now - self.last_auto < self.COOLDOWN_SEC:
            return
        if self.auto_sent >= self.MAX_AUTO:
            self.auto_gave_up = True
            self.note("too many input prompts; automatic Enter disabled for this job")
            return
        self.auto_sent += 1
        self.last_auto = now
        self.send(process, "auto")

    def send(self, process, source):
        # Two channels, because prompts read input two different ways: the pipe
        # reaches stdin (input(), fgets), WriteConsoleInput reaches raw key reads
        # (getpass, msvcrt.getwch, ReadConsole) through the console the step's
        # process tree owns. Trade-off accepted for skip-every-prompt mode: a
        # key the current prompt ignores stays queued and may answer the next
        # console read (e.g. a trailing "pause"); the next injection flushes
        # leftovers first, and MAX_AUTO bounds the total.
        delivered = False
        try:
            process.stdin.write(b"\r\n")
            process.stdin.flush()
            delivered = True
        except OSError:
            pass   # the step exited or never reads its pipe; the console may still take the key
        if console.inject_enter(process.pid):
            delivered = True
        if not delivered:
            return   # nothing accepted the Enter; the leftover request is cleared at the next step start
        try:
            self.request_path.unlink()   # only after success: a failed send must not swallow the request
        except OSError:
            pass
        if source == "manual":
            self.note("Enter sent")
        else:
            self.note("input prompt detected, Enter sent automatically")

    def note(self, text):
        # Automatic sends happen while the step is blocked on input, so the note
        # lands between its lines; a manual send can interleave with live output,
        # but the single O_APPEND write never corrupts what the step wrote.
        try:
            self.log.write(("\n[Build Runner] " + text + "\n").encode("utf-8"))
        except OSError:
            pass


def checkout_note(log, text):
    log.write(f"[Build Runner] {text}\n".encode("utf-8", "replace"))


def run_checkout(path, spec, log):
    """Prepare the build revision: fetch, checkout the target, refresh submodules.

    Raises jobs.ToolError on any failure; run() then ends the job with the
    message instead of building the wrong revision.
    """
    target = spec["target"]
    if spec.get("fetch"):
        checkout_note(log, "Git fetch origin --prune …")
        workspaces.git(path, "fetch", "origin", "--prune", required=True, timeout=3600)
    resolved = workspaces.resolve_checkout(path, target)
    if not resolved:
        raise jobs.ToolError("CHECKOUT_TARGET_NOT_FOUND",
                             f"本機找不到「{target}」；請確認 branch 或 commit SHA（或改用「先 fetch origin」）。")
    kind, value = resolved
    checkout_note(log, f"Checkout {target} → {'commit ' + value[:12] if kind == 'sha' else value}")
    # git switch -f: drop conflicting uncommitted tracked changes (untracked
    # files survive). Only used when the job's checkout spec carries force,
    # which the UI gates behind an explicit confirm dialog.
    force = bool(spec.get("force"))
    if force:
        checkout_note(log, "強制切樹（-f）：未提交的追蹤檔案變更將被丟棄（untracked 檔保留）。")
    if kind == "sha":
        workspaces.git(path, "switch", *(("-f",) if force else ()), "--detach", value, required=True, timeout=600)
    elif value.startswith("refs/heads/"):
        workspaces.git(path, "switch", *(("-f",) if force else ()), "--", value.removeprefix("refs/heads/"),
                       required=True, timeout=600)
    else:
        # Remote branch: make a local branch of the same name track it. Plain
        # --track dies with "a branch named ... already exists" when that local
        # branch is there, so decide up front: without force stop with clear
        # guidance (the local branch may differ from the remote tip); with
        # force reset it onto the remote tip (-C), discarding conflicts (-f).
        remote_ref = value.removeprefix("refs/remotes/")
        if "/" not in remote_ref:  # malformed refs/remotes/<no-remote> ref
            raise jobs.ToolError("CHECKOUT_TARGET_NOT_FOUND",
                                 f"遠端分支「{remote_ref}」格式異常，無法解析 remote 名稱。")
        local_name = remote_ref.split("/", 1)[1]        # remote names contain no "/"
        if force:
            checkout_note(log, f"強制（-C -f）：本機 branch「{local_name}」若已存在，將重置到遠端最新。")
            workspaces.git(path, "switch", "-C", local_name, "-f", "--track", "--",
                           remote_ref, required=True, timeout=600)
        elif workspaces.git(path, "show-ref", "--verify", "--quiet", "refs/heads/" + local_name) is not None:
            raise jobs.ToolError(
                "CHECKOUT_BRANCH_EXISTS",
                f"本機已存在 branch「{local_name}」，內容可能與遠端不同，Job 不覆寫它。"
                "請改選本機分支，或勾「強制切樹」以遠端最新版本重置本機 branch（本機多出的 commit 可經 reflog 找回）。")
        else:
            workspaces.git(path, "switch", "--track", "--", remote_ref, required=True, timeout=600)
    if spec.get("submodule"):
        checkout_note(log, "git submodule update --init --recursive …")
        workspaces.git(path, "submodule", "update", "--init", "--recursive", required=True, timeout=1800)
    checkout_note(log, "Checkout 完成，開始執行建置步驟。")


def run(data, job_id):
    folder = jobs.job_dir(data, job_id)
    log_path = folder / "build.log"
    with jobs.lock(data):
        row = jobs.get_job(data, job_id)
        if row["state"] != "running" or row["pid"] != os.getpid():
            return
        plan = read_json(folder / "plan.json")

    deadline = time.monotonic() + plan["timeoutSec"]
    env = {**os.environ, **{key.upper(): value for key, value in plan["env"].items()}}
    failure = None
    message = None
    contained = False
    alias = None
    auto_skip = jobs.auto_skip_prompts(data)
    try:
        contain_process_tree()
        contained = True
        if plan.get("shortPath"):
            row["step"] = "短路徑"
            with jobs.lock(data):
                jobs.save(data, row)
            workspace_path = Path(plan["workspacePath"])
            target = Path(workspaces.repo_root(workspace_path) or workspace_path).resolve()
            if not workspace_path.is_relative_to(target):
                raise jobs.ToolError("INVALID_SHORT_PATH", "Workspace 不在 junction 的目標資料夾內。")
            alias = junctions.ensure_short_path(plan["shortPath"], target)
            with log_path.open("ab", buffering=0) as log:
                log.write(f"[Build Runner] 建置短路徑：{alias / workspace_path.relative_to(target)}\n".encode("utf-8"))
            deadline = time.monotonic() + plan["timeoutSec"]
        checkout = plan.get("checkout") or {}
        if checkout.get("target"):
            # Build the requested revision, not wherever the tree happens to be.
            # A dirty worktree or missing target fails the job here, before any
            # build step runs, unless the spec opted into force (-f).
            row["step"] = "Checkout"
            with jobs.lock(data):
                jobs.save(data, row)
            with log_path.open("ab", buffering=0) as log:
                run_checkout(plan["workspacePath"], checkout, log)
            # Fetch/switch/submodule have their own per-command timeouts; that
            # prep time must not eat into the build steps' shared deadline.
            deadline = time.monotonic() + plan["timeoutSec"]
        git = workspaces.git_state(plan["workspacePath"], include_dirty=False)
        row.update(branch=git["branch"], commit=git["commit"])
        # Give the step an attachable console for raw-key prompts without showing a window.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        with log_path.open("ab", buffering=0) as log:
            forwarder = EnterForwarder(log, log_path, folder / "enter.request", auto_skip=auto_skip)
            for step in plan["steps"]:
                if alias is not None:
                    step = junctions.shorten_step(step, target, alias)
                command, executable, step_env = recipes.launch_args(step, env)
                with jobs.lock(data):
                    row["step"] = step["name"]
                    row["argv"].append(step["argv"])
                    jobs.save(data, row)
                    process = subprocess.Popen(
                        command, executable=executable, cwd=step["cwd"], env=step_env, shell=False,
                        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
                    )
                try:
                    exit_code = forwarder.wait(process, deadline)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        try:
                            jobs.kill_tree(process.pid)
                        except jobs.ToolError:
                            if process.poll() is None:
                                raise
                    process.wait()
                    failure = 1
                    message = "Recipe timed out."
                    break
                if exit_code:
                    failure = failure if failure is not None else exit_code
                    if not step["continueOnFail"]:
                        break
    except (OSError, subprocess.SubprocessError, jobs.ToolError) as exc:
        failure = 1
        message = str(exc)

    if failure is None and plan.get("artifactRules"):
        with jobs.lock(data):
            row["step"] = "Artifacts"
            jobs.save(data, row)
        try:
            artifact_plan = artifacts.preview(plan["workspacePath"], plan["artifactRules"],
                                              {**row, "jobId": row["id"],
                                               "repoRoot": workspaces.repo_root(plan["workspacePath"])})
            outcome = artifacts.execute(artifact_plan)
            row["artifacts"] = [item["destination"] for item in (*outcome["files"], *outcome["archives"])]
            row["artifactErrors"] = outcome["errors"]
        except (OSError, jobs.ToolError) as exc:
            row["artifactErrors"] = [{"message": str(exc)}]

    try:
        report = logtools.triage(log_path)
    except OSError as exc:
        report = None
        message = message or f"Unable to read the build log: {exc}"
    with jobs.lock(data):
        jobs.finish(data, row, "failed" if failure is not None else "success",
                    failure if failure is not None else 0, message=message, report=report)
        jobs.dispatch(data, breakaway=contained)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    run(data_dir(args.data_dir), args.job_id)


if __name__ == "__main__":
    main()
