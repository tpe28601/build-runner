"""Optional Windows directory junctions for shorter build paths."""

from hashlib import sha256
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess

from .paths import ToolError


def suggest_short_path(workspace):
    path = PureWindowsPath(workspace["path"])
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        return None
    suffix = sha256(workspace["id"].encode("utf-8")).hexdigest()[:8]
    return str(PureWindowsPath(path.anchor) / f"br-{suffix}")


def normalize_short_path(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str):
        raise ToolError("INVALID_SHORT_PATH", "短路徑必須是本機磁碟的絕對資料夾路徑。")
    path = PureWindowsPath(value.strip())
    if (not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive) or len(path.parts) < 2
            or any(part in (".", "..") or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                   or re.search(r'[<>:"|?*\x00-\x1f]', part) for part in path.parts[1:])):
        raise ToolError("INVALID_SHORT_PATH", "請輸入本機磁碟的絕對短路徑，例如 C:\\A。")
    return str(path)


def existing_junction(alias, target):
    try:
        info = alias.lstat()
    except FileNotFoundError:
        return False
    if (getattr(info, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT
            and alias.is_dir() and alias.samefile(target)):
        return True
    raise ToolError("SHORT_PATH_OCCUPIED", f"短路徑已被占用，請更換路徑：{alias}")


def ensure_short_path(value, target):
    if os.name != "nt":
        raise ToolError("WINDOWS_REQUIRED", "建置短路徑需要 Windows 的目錄 junction。", 5)
    normalized = normalize_short_path(value)
    if normalized is None:
        raise ToolError("INVALID_SHORT_PATH", "請輸入有效的建置短路徑。")
    if not target.is_dir():
        raise ToolError("INVALID_SHORT_PATH_TARGET", "建置短路徑的目標資料夾已不存在。")
    alias = Path(normalized)
    if not alias.parent.is_dir():
        raise ToolError("SHORT_PATH_PARENT_MISSING", f"短路徑的上層資料夾不存在：{alias.parent}")
    if (alias.parent.resolve() / alias.name).is_relative_to(target):
        raise ToolError("INVALID_SHORT_PATH", "短路徑必須在要連結的資料夾之外。")
    if existing_junction(alias, target):
        return alias

    cmd = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
    # mklink is a cmd built-in. Expand each quoted path once, with delayed expansion off.
    proc = subprocess.run(
        [cmd, "/d", "/v:off", "/c", "mklink", "/J", "%DEVTOOLBOX_JUNCTION_LINK%", "%DEVTOOLBOX_JUNCTION_TARGET%"],
        env={**os.environ, "DEVTOOLBOX_JUNCTION_LINK": f'"{alias}"', "DEVTOOLBOX_JUNCTION_TARGET": f'"{target}"'},
        shell=False, stdin=subprocess.DEVNULL, capture_output=True, encoding="oem", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
    )
    # Another job may have created the same junction in the meantime.
    if existing_junction(alias, target):
        return alias
    detail = (proc.stderr or proc.stdout).strip()
    raise ToolError("SHORT_PATH_CREATE_FAILED", f"無法建立建置短路徑 {alias}。" + (f" {detail}" if detail else ""), 1)


def shorten_step(step, target, alias):
    cwd = alias / Path(step["cwd"]).relative_to(target)
    argv = step["argv"]
    program = Path(argv[0])
    if program.is_absolute() and program.is_relative_to(target):
        argv = [str(alias / program.relative_to(target)), *argv[1:]]
    return {**step, "cwd": str(cwd), "argv": argv}
