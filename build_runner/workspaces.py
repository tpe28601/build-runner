"""Workspace registry and local Git metadata."""

import os
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

from .paths import ToolError, file_lock, read_json, write_json


def local_roots():
    if os.name != "nt":
        return [Path("/")]
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    mask = kernel.GetLogicalDrives()
    roots = [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]
    return [Path(root) for root in roots if kernel.GetDriveTypeW(root) in (2, 3, 6)]


def browse_folder(path, scope=None):
    if not path and scope is None:
        return {"current": None, "parent": None, "selection": None, "truncated": False,
                "items": [{"name": str(root), "path": str(root)} for root in local_roots()]}
    roots = local_roots() if os.name == "nt" else None
    try:
        root = Path(scope).expanduser() if scope is not None else None
        if root is not None:
            if not root.is_absolute() or (roots is not None and Path(root.anchor) not in roots):
                raise ToolError("INVALID_FOLDER", "Workspace 必須在本機磁碟。")
            root = root.resolve()
        folder = Path(path).expanduser() if path else root
        if root is not None and not folder.is_absolute():
            folder = root / folder
        if not folder.is_absolute() or (roots is not None and Path(folder.anchor) not in roots):
            raise ToolError("INVALID_FOLDER", "請輸入有效的本機資料夾路徑。")
        folder = folder.resolve()
        if (root is not None and not folder.is_relative_to(root)
                or roots is not None and Path(folder.anchor) not in roots):
            raise ToolError("INVALID_FOLDER", "此資料夾超出可瀏覽範圍。")
        items = []
        truncated = False
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if len(items) == 500:
                    truncated = True
                    break
                child = Path(entry.path)
                try:
                    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                    has_git = not (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and (child / ".git").exists()
                except OSError:
                    has_git = False
                items.append({"name": entry.name, "path": str(child), "hasGit": has_git})
    except (OSError, RuntimeError) as exc:
        raise ToolError("FOLDER_UNAVAILABLE", "無法讀取此資料夾。") from exc
    return {"current": str(folder), "parent": None if folder in (root, folder.parent) else str(folder.parent),
            "selection": folder.relative_to(root).as_posix() if root is not None else str(folder),
            "items": sorted(items, key=lambda item: item["name"].casefold()), "truncated": truncated}


def list_workspaces(data):
    rows = read_json(data / "workspaces.json", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or any(not isinstance(row.get(key), str) or not row[key] for key in ("id", "name", "path"))
        for row in rows
    ):
        raise ToolError("INVALID_WORKSPACES", "workspaces.json must contain a list of workspace records.")
    return rows


def get_workspace(data, name):
    rows = list_workspaces(data)
    for key in ("id", "name"):
        match = next((row for row in rows if row[key].casefold() == name.casefold()), None)
        if match is not None:
            return match
    raise ToolError("WORKSPACE_NOT_FOUND", f"Workspace not found: {name}")


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def add_workspace(data, name, path):
    name = name.strip()
    path = Path(path).expanduser().resolve()
    if not name or not path.is_dir():
        raise ToolError("INVALID_WORKSPACE", "Provide a name and an existing workspace folder.")
    with file_lock(data / ".workspaces.lock"):
        rows = list_workspaces(data)
        if any(row["name"].casefold() == name.casefold() or path_key(row["path"]) == path_key(path)
               for row in rows):
            raise ToolError("WORKSPACE_EXISTS", "Workspace name or folder already exists.")
        row = {"id": uuid4().hex, "name": name, "path": str(path)}
        write_json(data / "workspaces.json", [*rows, row])
    return row


def git(path, *args, required=False, timeout=10):
    try:
        run = subprocess.run(
            ["git", "-C", str(path), *args], shell=False, capture_output=True,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if required:
            raise ToolError("GIT_UNAVAILABLE", str(exc), 5) from exc
        return None
    if run.returncode:
        if required:
            raise ToolError("GIT_FAILED", run.stderr.strip() or "Git command failed.", 1)
        return None
    return run.stdout.rstrip("\r\n")


def git_state(path, include_dirty=True):
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return {"gitCommonDir": None, "branch": None, "commit": None, "subject": None, "dirty": None}
    branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = git(path, "log", "-1", "--format=%h%n%s")
    commit, _, subject = (head or "").partition("\n")
    dirty = git(path, "status", "--porcelain") if include_dirty else None
    return {
        "gitCommonDir": path_key(common),
        "branch": branch,
        "commit": commit or None,
        "subject": subject or None,
        "dirty": bool(dirty) if dirty is not None else None,
    }


def list_worktrees(path):
    output = git(path, "worktree", "list", "--porcelain", "-z", required=True)
    return [field[len("worktree "):] for field in output.split("\0") if field.startswith("worktree ")]
