"""Execute one persisted job in an independent process."""

import argparse
import ctypes
from ctypes import wintypes
import os
import subprocess
import time

from . import artifacts, jobs, logtools, recipes, workspaces
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
    try:
        contain_process_tree()
        contained = True
        git = workspaces.git_state(plan["workspacePath"], include_dirty=False)
        row.update(branch=git["branch"], commit=git["commit"])
        with log_path.open("ab", buffering=0) as log:
            for step in plan["steps"]:
                command, executable, step_env = recipes.launch_args(step, env)
                with jobs.lock(data):
                    row["step"] = step["name"]
                    row["argv"].append(step["argv"])
                    jobs.save(data, row)
                    process = subprocess.Popen(
                        command, executable=executable, cwd=step["cwd"], env=step_env, shell=False,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    )
                try:
                    exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
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
