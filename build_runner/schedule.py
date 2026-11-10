"""Manage only this tool's Windows scheduled tasks."""

import csv
import os
from pathlib import Path
import re
import subprocess
import sys
from xml.etree import ElementTree

from .paths import ROOT, ToolError, file_lock, read_json, write_json

PREFIX = "DevToolbox-BuildRunner-"
DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
NAME = re.compile(r"[\w][\w-]{0,39}")


def task_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ToolError("INVALID_SCHEDULE", "Use 1–40 letters, digits, hyphens or underscores for the task name.")
    return PREFIX + name


def normalize(row):
    name = row.get("name")
    task_name(name)
    frequency, start, day = row.get("frequency"), row.get("time"), row.get("day")
    if frequency not in ("DAILY", "WEEKLY") or not isinstance(start, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
        raise ToolError("INVALID_SCHEDULE", "Choose daily or weekly and a valid HH:MM time.")
    if frequency == "WEEKLY" and day not in DAY_CODES:
        raise ToolError("INVALID_SCHEDULE", "Choose a weekday.")
    for field in ("workspace", "recipe"):
        if not isinstance(row.get(field), str) or not row[field].strip() or "\0" in row[field]:
            raise ToolError("INVALID_SCHEDULE", f"Choose a valid {field}.")
    preset = row.get("preset")
    if preset is not None and (not isinstance(preset, str) or not preset.strip() or "\0" in preset):
        raise ToolError("INVALID_SCHEDULE", "Invalid preset.")
    return {**{field: row[field] for field in ("workspace", "recipe")}, "name": name,
            "frequency": frequency, "time": start, "day": day if frequency == "WEEKLY" else None, "preset": preset}


def command(*args):
    if os.name != "nt":
        raise ToolError("WINDOWS_REQUIRED", "Scheduling requires Windows.", 5)
    binary = Path(os.environ["SystemRoot"]) / "System32" / "schtasks.exe"
    try:
        result = subprocess.run([str(binary), *args], shell=False, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("SCHEDULE_FAILED", str(exc), 1) from exc
    # schtasks can emit the system OEM code page or UTF-16 XML.
    def decode(raw):
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", errors="replace")
        if len(raw) >= 4 and raw[1] == raw[3] == 0:
            return raw.decode("utf-16-le", errors="replace")
        return raw.decode("oem", errors="replace")
    if result.returncode:
        raise ToolError("SCHEDULE_FAILED", decode(result.stderr).strip() or decode(result.stdout).strip()
                        or "Task Scheduler command failed.", 1)
    return decode(result.stdout)


def list_tasks(data):
    tasks = []
    for row in csv.reader(command("/Query", "/FO", "CSV", "/NH").splitlines()):
        full = row[0].removeprefix("\\") if row else ""
        if not full.casefold().startswith(PREFIX.casefold()):
            continue
        name = full[len(PREFIX):]
        if not NAME.fullmatch(name):
            continue
        entry = read_json(data / "schedules" / f"{name}.json", {})
        enabled = None
        try:
            xml = command("/Query", "/TN", full, "/XML").lstrip("\ufeff\r\n ")
            enabled = ElementTree.fromstring(xml).findtext(".//{*}Settings/{*}Enabled", "true").lower() == "true"
        except (ToolError, ElementTree.ParseError, UnicodeError):
            pass
        tasks.append({**entry, "name": name, "enabled": enabled, "schedulerStatus": row[2] if len(row) > 2 else ""})
    return tasks


def create(data, options):
    row = normalize(options)
    name = task_name(row["name"])
    folder = data / "schedules"
    with file_lock(data / ".schedules.lock"):
        if any(item["name"].casefold() == row["name"].casefold() for item in list_tasks(data)):
            raise ToolError("SCHEDULE_EXISTS", "A task with this name already exists.")
        config = folder / f"{row['name']}.json"
        launcher = folder / "launch.py"
        folder.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from build_runner.cli import main\n"
            "folder = Path(__file__).resolve().parent\n"
            "row = json.loads((folder / (sys.argv[1] + '.json')).read_text(encoding='utf-8'))\n"
            "args = ['--data-dir', str(folder.parent), 'run', '--workspace', row['workspace'], '--recipe', row['recipe'], '--wait']\n"
            "if row.get('preset'): args.extend(['--preset', row['preset']])\n"
            "raise SystemExit(main(args))\n",
            encoding="utf-8",
        )
        action = subprocess.list2cmdline([sys.executable, str(launcher), row["name"]])
        if len(action) > 262:
            raise ToolError("INVALID_SCHEDULE", "Task action exceeds 262 characters. Use a shorter data directory.")
        write_json(config, row)
        try:
            args = ["/Create", "/TN", name, "/TR", action, "/SC", row["frequency"], "/ST", row["time"]]
            if row["day"]:
                args += ["/D", row["day"]]
            command(*args)
        except ToolError:
            config.unlink(missing_ok=True)
            raise
    return {**row, "enabled": True}


def change(data, name, enabled):
    if type(enabled) is not bool:
        raise ToolError("INVALID_SCHEDULE", "enabled must be true or false.")
    with file_lock(data / ".schedules.lock"):
        command("/Change", "/TN", task_name(name), "/ENABLE" if enabled else "/DISABLE")


def delete(data, name):
    with file_lock(data / ".schedules.lock"):
        command("/Delete", "/TN", task_name(name), "/F")
        (data / "schedules" / f"{name}.json").unlink(missing_ok=True)
