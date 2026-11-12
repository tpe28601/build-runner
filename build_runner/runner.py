"""Execute one persisted job in an independent process."""

import argparse
import ctypes
from ctypes import wintypes
import os
import subprocess
import time

from . import artifacts, jobs, logtools, prompts, recipes, workspaces
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
        try:
            process.stdin.write(b"\r\n")
            process.stdin.flush()
        except OSError:
            return   # the step already exited; the leftover request is cleared at the next step start
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
    auto_skip = jobs.auto_skip_prompts(data)
    try:
        contain_process_tree()
        contained = True
        git = workspaces.git_state(plan["workspacePath"], include_dirty=False)
        row.update(branch=git["branch"], commit=git["commit"])
        with log_path.open("ab", buffering=0) as log:
            forwarder = EnterForwarder(log, log_path, folder / "enter.request", auto_skip=auto_skip)
            for step in plan["steps"]:
                command, executable, step_env = recipes.launch_args(step, env)
                with jobs.lock(data):
                    row["step"] = step["name"]
                    row["argv"].append(step["argv"])
                    jobs.save(data, row)
                    process = subprocess.Popen(
                        command, executable=executable, cwd=step["cwd"], env=step_env, shell=False,
                        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
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
                                              {**row, "jobId": row["id"]})
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
