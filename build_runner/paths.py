"""Data directory selection and serialized, atomic writes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TOOL_ID = "build-runner"


class ToolError(Exception):
    def __init__(self, code, message, exit_code=3, **detail):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.detail = detail


def named_file(data, folder, name):
    if (not isinstance(name, str) or not re.fullmatch(r"[\w][\w .-]{0,127}", name)
            or name.endswith((".", " "))):
        raise ToolError("INVALID_NAME", f"Invalid {folder} name.")
    return data / folder / (name + ".json")


def config_records(data, folder):
    for path in sorted((data / folder).glob("*.json")):
        record = read_json(path)
        if isinstance(record, dict):
            yield {**record, "id": path.stem, "name": record.get("name") or path.stem}


def save_config(data, folder, record, *, overwrite=False, identity=None):
    path = named_file(data, folder, identity if identity is not None else record.get("name"))
    with file_lock(data / f".{folder}.lock"):
        exists = path.exists()
        if exists and not overwrite:
            raise ToolError("CONFIRM_REQUIRED", "An existing record requires overwrite confirmation.", 4)
        write_json(path, record)
    return exists


def data_dir(override=None):
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get("DEVTOOLBOX_DATA"):
        return (Path(os.environ["DEVTOOLBOX_DATA"]) / TOOL_ID).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ToolError("MISSING_LOCALAPPDATA", "Set LOCALAPPDATA or use --data-dir.", 5)
    return (Path(local) / "DevToolbox" / TOOL_ID).resolve()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_JSON", f"Invalid JSON in {path.name}.") from exc


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        # On Windows a short-lived reader may momentarily deny replacement.
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 5
    # Windows removes the lock when its last handle closes, even after a crash.
    temporary = getattr(os, "O_TEMPORARY", 0)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | temporary, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ToolError("JOB_BUSY", f"Timed out acquiring {path.parent.name} lock.", 1)
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        if not temporary:
            path.unlink()
