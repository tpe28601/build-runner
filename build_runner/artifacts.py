"""Plan and copy Workspace artifacts without overwriting existing files."""

from datetime import datetime
from fnmatch import fnmatchcase
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
import zipfile

from .paths import ToolError, named_file, read_json

MAX_FILES = 10000
VARIABLE = re.compile(r"\{([^{}]+)\}")
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEVICES = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)


def invalid(message):
    return ToolError("INVALID_ARTIFACT_RULE", message)


def required_text(value, field):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise invalid(f"Provide a valid {field}.")
    return value.strip()


def patterns(value, field):
    if not isinstance(value, list) or len(value) > 100:
        raise invalid(f"{field} must be an array of relative glob patterns.")
    result = []
    for item in value:
        pattern = required_text(item, field).replace("\\", "/")
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts or ":" in pattern or not path.parts:
            raise invalid(f"{field} cannot leave the Workspace.")
        components = []
        for part in path.parts:
            if part != "**" or not components or components[-1] != "**":
                components.append(part)
        result.append("/".join(components))
    return result


def normalize(config):
    if not isinstance(config, dict):
        raise invalid("Artifact rules must be a JSON object.")
    name = required_text(config.get("name"), "name")
    rules = config.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
        raise invalid("Provide 1 to 100 artifact rules.")
    rows = []
    for row in rules:
        if not isinstance(row, dict) or row.get("when", "success") != "success":
            raise invalid("Artifact rules must run after success.")
        select = patterns(row.get("select"), "select")
        if not select:
            raise invalid("An artifact rule needs at least one selection pattern.")
        zipped = row.get("zip", {})
        if not isinstance(zipped, dict) or type(zipped.get("enabled", False)) is not bool:
            raise invalid("zip.enabled must be a boolean.")
        archive = {"enabled": zipped.get("enabled", False)}
        if archive["enabled"]:
            archive["file"] = required_text(zipped.get("file"), "zip.file")
        rows.append({
            "select": select,
            "exclude": patterns(row.get("exclude", []), "exclude"),
            "rename": required_text(row.get("rename", "{name}{ext}"), "rename"),
            "dest": required_text(row.get("dest"), "dest"),
            "zip": archive, "when": "success",
        })
    return {"name": name, "rules": rows}


def load(data, name):
    source = read_json(named_file(data, "artifacts", name))
    if source is None:
        raise ToolError("ARTIFACT_RULE_NOT_FOUND", f"Artifact rule not found: {name}")
    return normalize(source)


def segment_match(value, pattern):
    return fnmatchcase(os.path.normcase(value), os.path.normcase(pattern))


def matches(parts, pattern):
    @lru_cache(maxsize=None)
    def match(index, token):
        if token == len(pattern):
            return index == len(parts)
        if pattern[token] == "**":
            return match(index, token + 1) or (index < len(parts) and match(index + 1, token))
        return index < len(parts) and segment_match(parts[index], pattern[token]) and match(index + 1, token + 1)
    return match(0, 0)


def safe_entry(entry):
    attributes = getattr(entry.lstat(), "st_file_attributes", 0)
    return not entry.is_symlink() and not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def candidates(folder, parts, excluded, prefix=()):
    if not parts:
        if folder.is_file():
            yield folder
        return
    head, *tail = parts
    if head == "**":
        yield from candidates(folder, tail, excluded, prefix)
    if not folder.is_dir():
        return
    # Resolve literal components directly instead of walking unrelated trees.
    if head != "**" and not any(char in head for char in "*?["):
        direct = folder / head
        entries = [direct] if direct.exists() else []
    else:
        entries = sorted(folder.iterdir(), key=lambda path: os.path.normcase(path.name))
    for entry in entries:
        relative = (*prefix, entry.name)
        if (head != "**" and not segment_match(entry.name, head)
                or any(matches(relative, pattern) for pattern in excluded)):
            continue
        if not safe_entry(entry):
            continue
        if head == "**":
            if entry.is_dir():
                yield from candidates(entry, parts, excluded, relative)
            elif not tail and entry.is_file():
                yield entry
        else:
            yield from candidates(entry, tail, excluded, relative)


def clean_variable(value):
    if value is None or value is False:
        return ""
    text = UNSAFE.sub("_", str(value)).strip()
    # Preserve the leading dot in an extension, while avoiding special segments.
    return "_" if text in (".", "..") else text


def render(template, values):
    def substitute(match):
        name = match[1]
        if name not in values:
            raise ToolError("INVALID_ARTIFACT_VARIABLE", f"Unknown artifact variable: {name}")
        return values[name]

    if any(char in VARIABLE.sub("", template) for char in "{}"):
        raise invalid("Unbalanced artifact variable braces.")
    return VARIABLE.sub(substitute, template)


def filename(value):
    if (not value or value in (".", "..") or value.endswith((" ", "."))
            or UNSAFE.search(value) or DEVICES.fullmatch(value.partition(".")[0])):
        raise invalid(f"Invalid output filename: {value}")
    return value


def destination(root, value):
    value = required_text(value, "destination")
    windows = PureWindowsPath(value)
    path = Path(value)
    if windows.drive and not path.is_absolute():
        raise invalid("A destination with a drive must be absolute.")
    if not path.is_absolute():
        path = (root / path).resolve()
        if not path.is_relative_to(root):
            raise invalid("Relative destinations must stay inside the Workspace.")
    return path.resolve()


def available(path, reserved):
    candidate = path
    for number in range(2, MAX_FILES + 2):
        key = os.path.normcase(str(candidate))
        if key not in reserved and not os.path.lexists(candidate):
            reserved.add(key)
            return candidate
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
    raise ToolError("ARTIFACT_COLLISION_LIMIT", "Too many output filename collisions.", 1)


def preview(workspace, config, context):
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise invalid("Workspace directory does not exist.")
    moment = datetime.now().astimezone()
    values = {key: clean_variable(context.get(key)) for key in ("workspace", "branch", "recipe", "jobId")}
    values["workspace"] = values["workspace"] or clean_variable(root.name)
    values.update(commit7=clean_variable((context.get("commit") or "")[:7]),
                  date=moment.strftime("%Y-%m-%d"), time=moment.strftime("%H%M%S"), name="", ext="")
    values.update({f"param.{key}": clean_variable(value) for key, value in context.get("params", {}).items()})
    plan = {"workspacePath": str(root), "files": [], "archives": []}
    reserved = set()
    for index, rule in enumerate(config["rules"], 1):
        excludes = [tuple(pattern.split("/")) for pattern in rule["exclude"]]
        sources = {}
        for pattern in rule["select"]:
            for path in candidates(root, tuple(pattern.split("/")), excludes):
                relative = path.relative_to(root).as_posix()
                if any(matches(tuple(relative.split("/")), exclude) for exclude in excludes):
                    continue
                sources[relative] = path
                if len(plan["files"]) + len(sources) > MAX_FILES:
                    raise invalid(f"An artifact operation supports up to {MAX_FILES} files.")
        groups = {}
        for relative, path in sorted(sources.items()):
            file_values = {**values, "name": clean_variable(path.stem), "ext": clean_variable(path.suffix)}
            dest = destination(root, render(rule["dest"], file_values))
            target = dest / filename(render(rule["rename"], file_values))
            output = available(target, reserved)
            plan["files"].append({"id": len(plan["files"]), "rule": index, "source": relative,
                                  "destination": str(output), "requested": str(target)})
            groups.setdefault(dest, []).append(plan["files"][-1]["id"])
        if rule["zip"]["enabled"]:
            for dest, entries in groups.items():
                archive_name = filename(render(rule["zip"]["file"], values))
                if not archive_name.lower().endswith(".zip"):
                    raise invalid("Archive filenames must end in .zip.")
                target = dest / archive_name
                plan["archives"].append({"rule": index, "destination": str(available(target, reserved)),
                                         "requested": str(target), "entries": entries})
    return plan


def write_unique(path, fallback, writer):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".build-runner-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            writer(stream)
        for _ in range(MAX_FILES):
            try:
                # Windows rename fails if the destination exists. On POSIX
                # link provides the same atomic no-overwrite publication.
                if os.name == "nt":
                    os.rename(tmp, path)
                else:
                    os.link(tmp, path)
            except FileExistsError:
                path = available(fallback, set())
                continue
            return path
    finally:
        tmp.unlink(missing_ok=True)
    raise ToolError("ARTIFACT_COLLISION_LIMIT", "Too many output filename collisions.", 1)


def copy_file(source, stream):
    with source.open("rb") as original:
        shutil.copyfileobj(original, stream)


def zip_files(paths, stream):
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)


def execute(plan):
    root = Path(plan["workspacePath"]).resolve()
    result = {"files": [], "archives": [], "errors": []}
    copied = {}
    for entry in plan["files"]:
        source = root / entry["source"]
        try:
            if not source.resolve().is_relative_to(root) or not source.is_file() or not safe_entry(source):
                raise invalid("Source no longer exists in the Workspace.")
            actual = write_unique(Path(entry["destination"]), Path(entry["requested"]),
                                  lambda stream: copy_file(source, stream))
            copied[entry["id"]] = actual
            result["files"].append({"source": entry["source"], "destination": str(actual)})
        except (OSError, ToolError) as exc:
            result["errors"].append({"source": entry["source"], "message": str(exc)})
    for entry in plan["archives"]:
        paths = [copied[index] for index in entry["entries"] if index in copied]
        if not paths:
            continue
        try:
            actual = write_unique(Path(entry["destination"]), Path(entry["requested"]),
                                  lambda stream: zip_files(paths, stream))
            result["archives"].append({"destination": str(actual), "count": len(paths)})
        except (OSError, ToolError, zipfile.BadZipFile) as exc:
            result["errors"].append({"destination": entry["destination"], "message": str(exc)})
    return result
