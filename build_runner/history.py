"""Build history, filtering, retention and export."""

import csv
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .paths import ToolError


def list_history(data):
    try:
        with (data / "history.jsonl").open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except FileNotFoundError:
        return []
    except (ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_HISTORY", "history.jsonl contains an invalid record.", 1) from exc


def append(data, record):
    # Job finalization holds the job lock, so only one writer appends at a time.
    with (data / "history.jsonl").open("ab") as stream:
        stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))


def replace(data, rows):
    """Replace history atomically. Caller holds the job lock."""
    fd, name = tempfile.mkstemp(dir=data, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temporary, data / "history.jsonl")
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def timestamp(row):
    try:
        value = datetime.fromisoformat(row.get("endedAt") or "")
        return value if value.tzinfo else value.astimezone()
    except (TypeError, ValueError):
        return None


def filtered(rows, workspace=None, state=None, start=None, end=None):
    try:
        start = date.fromisoformat(start) if start else None
        end = date.fromisoformat(end) if end else None
    except (TypeError, ValueError) as exc:
        raise ToolError("INVALID_HISTORY_FILTER", "Dates must use YYYY-MM-DD.") from exc
    if start and end and start > end:
        raise ToolError("INVALID_HISTORY_FILTER", "Start date must precede the end date.")
    if state and state not in ("success", "failed", "stopped"):
        raise ToolError("INVALID_HISTORY_FILTER", "Unknown build result.")
    selected = []
    for row in rows:
        if workspace and workspace not in (row.get("workspaceId"), row.get("workspace")):
            continue
        if state and state != row.get("state"):
            continue
        if start or end:
            stamp = timestamp(row)
            if stamp is None:
                continue
            day = stamp.astimezone().date()
            if (start and day < start) or (end and day > end):
                continue
        selected.append(row)
    return selected


def retained(rows, days, count, *, now=None):
    if days:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        rows = [row for row in rows if (timestamp(row) or cutoff) >= cutoff]
    return rows[-count:] if count else rows


def prune(data, rules):
    """Drop rows past retention. Returns pruned job ids so callers can delete job folders."""
    days, count = rules.get("days", 0), rules.get("count", 0)
    if not (days or count):
        return []
    current = list_history(data)
    remaining = retained(current, days, count)
    if len(current) == len(remaining):
        return []
    replace(data, remaining)
    kept = {row.get("id") for row in remaining}
    return [row["id"] for row in current
            if isinstance(row.get("id"), str) and row["id"] not in kept and re.fullmatch(r"[a-f0-9]{32}", row["id"])]


def export(rows, format):
    if format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    fields = ("id", "endedAt", "workspace", "branch", "commit", "recipe", "argv", "state",
              "durationSec", "exitCode", "rootError", "logPath", "artifacts")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        values = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            values[key] = value
        writer.writerow(values)
    return output.getvalue()


def write_export(path, content, overwrite=False):
    try:
        with Path(path).open("w" if overwrite else "x", encoding="utf-8", newline="") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise ToolError("CONFIRM_REQUIRED", "Export exists. Pass --yes to overwrite it.", 4) from exc
