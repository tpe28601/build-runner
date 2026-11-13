"""Export and import portable settings packs (recipes, parameter tables, presets, artifact rules)."""

from datetime import datetime, timezone
import json
import os
import re
import shutil
from pathlib import Path

from . import artifacts, paramsets, recipes
from .paths import ToolError, config_records, file_lock, named_file, read_json, write_json

FORMAT = "build-runner-pack"
VERSION = 1
SUFFIX = ".brpack.json"
MAX_PACK_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 500
CONFLICT_ACTIONS = ("overwrite", "rename", "skip")
# Pack section -> data folder. Presets live inside parameter tables and are handled separately.
FOLDERS = {"recipes": "recipes", "paramsets": "paramsets", "artifactRules": "artifacts"}
SECTIONS = ("artifactRules", "paramsets", "presets", "recipes")   # import order: dependencies first
ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def invalid(message):
    return ToolError("INVALID_PACK", message)


def clean(record):
    """Drop runtime-only fields that config_records adds."""
    return {key: value for key, value in record.items() if key not in ("id", "builtIn")}


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def machine_warnings(section, name, record):
    """Flag absolute paths and the current user name; the pack must stay machine-neutral."""
    warnings = []
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    for text in strings_in(record):
        if ABSOLUTE.match(text.strip()):
            warnings.append({"code": "ABSOLUTE_PATH", "section": section, "name": name,
                             "message": f"{name} 含絕對路徑：{text}（換電腦後可能不存在）"})
        elif user and len(user) > 2 and user.casefold() in text.casefold():
            warnings.append({"code": "USER_NAME", "section": section, "name": name,
                             "message": f"{name} 含目前使用者名稱：{text}"})
    return warnings


def preset_id(table, preset):
    return f"{table}/{preset}"


def split_preset(value):
    if not isinstance(value, str) or "/" not in value:
        raise ToolError("INVALID_ARGUMENTS", f"Preset must be written as TABLE/PRESET: {value}")
    table, preset = value.split("/", 1)
    if not table or not preset:
        raise ToolError("INVALID_ARGUMENTS", f"Preset must be written as TABLE/PRESET: {value}")
    return table, preset


def inventory(data):
    """Everything a pack can contain, for the export dialog."""
    tables = list(config_records(data, "paramsets"))
    return {
        "recipes": [row["id"] for row in config_records(data, "recipes")],
        "paramsets": [row["id"] for row in tables],
        "presets": [preset_id(row["id"], key) for row in tables for key in (row.get("presets") or {})],
        "artifactRules": [row["id"] for row in config_records(data, "artifacts")],
    }


def pick(available, wanted, section):
    if wanted is None:
        return []
    missing = [name for name in wanted if name not in available]
    if missing:
        raise ToolError("RECORD_NOT_FOUND", f"{section} not found: {', '.join(missing)}", names=missing)
    return list(dict.fromkeys(wanted))


def export_pack(data, *, recipe_names=None, paramset_names=None, preset_ids=None, artifact_names=None,
                include_all=False, name="", description="", tool_version=""):
    """Build a pack from saved records. Returns (pack, warnings)."""
    available = inventory(data)
    if include_all:
        recipe_names = available["recipes"]
        paramset_names = available["paramsets"]
        preset_ids = available["presets"]
        artifact_names = available["artifactRules"]
    chosen = {
        "recipes": pick(available["recipes"], recipe_names, "Recipe"),
        "paramsets": pick(available["paramsets"], paramset_names, "Parameter table"),
        "artifactRules": pick(available["artifactRules"], artifact_names, "Artifact rule"),
    }
    if preset_ids is None:
        # Without an explicit preset list, a chosen table brings its own presets.
        preset_ids = [item for item in available["presets"] if split_preset(item)[0] in chosen["paramsets"]]
    chosen["presets"] = pick(available["presets"], preset_ids, "Preset")
    if not any(chosen.values()):
        raise ToolError("EMPTY_PACK", "Select at least one item to export.")

    warnings = []
    pack = {"format": FORMAT, "version": VERSION, "name": name.strip() or "build-runner-settings",
            "description": description, "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": {"id": "build-runner", "version": tool_version},
            "recipes": [], "paramsets": [], "presets": [], "artifactRules": []}
    for section in ("recipes", "paramsets", "artifactRules"):
        for item in chosen[section]:
            record = clean(read_json(named_file(data, FOLDERS[section], item)) or {})
            record["name"] = item
            if section == "paramsets":
                record.pop("presets", None)   # exported separately so they can be picked one by one
            pack[section].append(record)
            warnings += machine_warnings(section, item, record)
    tables = {}
    for item in chosen["presets"]:
        table, preset = split_preset(item)
        if table not in tables:
            tables[table] = paramsets.load(data, table)
        pack["presets"].append({"paramset": table, "name": preset, "values": tables[table]["presets"][preset]})
    warnings += reference_warnings(pack, data)
    return pack, warnings


def reference_warnings(pack, data):
    """Recipes and presets that point at tables or rules neither in the pack nor on this machine."""
    in_pack = {section: {row.get("name") for row in pack[section]} for section in ("paramsets", "artifactRules")}
    existing = inventory(data)
    known_tables = in_pack["paramsets"] | set(existing["paramsets"])
    known_rules = in_pack["artifactRules"] | set(existing["artifactRules"]) | {artifacts.BUILTIN_ROM_ID}
    presets = {(row["paramset"], row["name"]) for row in pack["presets"]}
    for row in config_records(data, "paramsets"):
        presets |= {(row["id"], key) for key in (row.get("presets") or {})}
    warnings = []
    for row in pack["recipes"]:
        table = row.get("paramset")
        if table and table not in known_tables:
            warnings.append({"code": "MISSING_PARAMSET", "section": "recipes", "name": row["name"],
                             "message": f"Recipe {row['name']} 使用的參數表 {table} 不在設定包，也不在本機。"})
        rule = row.get("artifacts")
        if rule and rule not in known_rules:
            warnings.append({"code": "MISSING_ARTIFACT_RULE", "section": "recipes", "name": row["name"],
                             "message": f"Recipe {row['name']} 使用的產出物規則 {rule} 不在設定包，也不在本機。"})
        for step in row.get("steps") or []:
            preset = step.get("preset") if isinstance(step, dict) else None
            if preset and table and (table, preset) not in presets:
                warnings.append({"code": "MISSING_PRESET", "section": "recipes", "name": row["name"],
                                 "message": f"Recipe {row['name']} 的步驟使用 preset {preset}，參數表 {table} 沒有這個 preset。"})
    for row in pack["presets"]:
        if row["paramset"] not in known_tables:
            warnings.append({"code": "MISSING_PARAMSET", "section": "presets",
                             "name": preset_id(row["paramset"], row["name"]),
                             "message": f"Preset {row['name']} 所屬的參數表 {row['paramset']} 不在設定包，也不在本機。"})
    return warnings


def dumps(pack):
    return json.dumps(pack, ensure_ascii=False, indent=2) + "\n"


def write_pack(path, pack, *, overwrite=False):
    path = Path(path)
    if path.exists() and not overwrite:
        raise ToolError("CONFIRM_REQUIRED", f"File already exists: {path}", 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(pack), encoding="utf-8", newline="\n")
    return path


# ---------------- import ----------------

def parse(raw):
    """Validate a pack's envelope and every record; returns the normalized pack."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_PACK_BYTES:
        raise invalid("Settings pack is larger than 4 MB.")
    try:
        pack = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise invalid("Settings pack is not valid UTF-8 JSON.") from exc
    if not isinstance(pack, dict) or pack.get("format") != FORMAT:
        raise invalid(f'Not a Build Runner settings pack (format must be "{FORMAT}").')
    version = pack.get("version")
    if type(version) is not int:
        raise invalid("Settings pack version must be an integer.")
    if version > VERSION or version < 1:
        raise ToolError("UNSUPPORTED_PACK_VERSION",
                        f"Settings pack version {version} is not supported (this Build Runner reads version {VERSION}).")
    result = {"format": FORMAT, "version": version,
              "name": text(pack.get("name"), "name"), "description": text(pack.get("description"), "description"),
              "createdAt": text(pack.get("createdAt"), "createdAt"),
              "tool": pack.get("tool") if isinstance(pack.get("tool"), dict) else {}}
    total = 0
    for section in ("recipes", "paramsets", "presets", "artifactRules"):
        rows = pack.get(section, [])
        if not isinstance(rows, list):
            raise invalid(f"{section} must be an array.")
        total += len(rows)
        if total > MAX_ITEMS:
            raise invalid(f"A settings pack holds up to {MAX_ITEMS} items.")
        result[section] = [validate(section, row) for row in rows]
        keys = [row_key(section, row) for row in result[section]]
        if len({key.casefold() for key in keys}) != len(keys):
            raise invalid(f"{section} contains duplicate names.")
    return result


def text(value, field):
    if value is None:
        return ""
    if not isinstance(value, str) or "\0" in value:
        raise invalid(f"{field} must be text.")
    return value


def row_key(section, row):
    return preset_id(row["paramset"], row["name"]) if section == "presets" else row["name"]


def validate(section, row):
    if not isinstance(row, dict):
        raise invalid(f"{section} entries must be objects.")
    name = row.get("name")
    if section in FOLDERS:
        named_file(Path("."), FOLDERS[section], name)   # same naming rule as saved records
    if section == "recipes":
        return recipes.validate(clean(row))
    if section == "paramsets":
        return paramsets.normalize({**clean(row), "presets": {}})
    if section == "artifactRules":
        return artifacts.normalize(clean(row))
    table = row.get("paramset")
    named_file(Path("."), "paramsets", table)
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 128:
        raise invalid("Invalid preset name.")
    if not isinstance(row.get("values"), dict):
        raise invalid(f"Preset {name} needs a values object.")
    return {"paramset": table, "name": name, "values": row["values"]}


def free_name(taken, base):
    """base-2, base-3 ... that is not taken (case-insensitive) and still a valid record name."""
    folded = {item.casefold() for item in taken}
    for index in range(2, 1000):
        suffix = f"-{index}"
        candidate = base[:128 - len(suffix)].rstrip(". ") + suffix
        if candidate.casefold() not in folded:
            return candidate
    raise ToolError("NAME_EXHAUSTED", f"No free name for {base}.")


def plan(data, pack, decisions=None, default=None):
    """Work out what an import would do. decisions maps "section:key" to overwrite|rename|skip."""
    decisions = decisions or {}
    for action in [default, *decisions.values()]:
        if action is not None and action not in CONFLICT_ACTIONS:
            raise ToolError("INVALID_ARGUMENTS", f"Conflict action must be one of {', '.join(CONFLICT_ACTIONS)}.")
    existing = inventory(data)
    taken = {section: set(existing[section]) for section in ("recipes", "paramsets", "artifactRules")}
    renamed = {"paramsets": {}, "artifactRules": {}}
    items = []
    unresolved = []

    def decide(section, key, conflict):
        if not conflict:
            return "create"
        action = decisions.get(f"{section}:{key}", default)
        if action is None:
            unresolved.append(f"{section}:{key}")
            return "conflict"
        return action

    for section in ("artifactRules", "paramsets", "recipes"):
        for row in pack[section]:
            name = row["name"]
            conflict = name.casefold() in {item.casefold() for item in taken[section]}
            action = decide(section, name, conflict)
            target = name
            if action == "rename":
                target = free_name(taken[section] | {r["name"] for r in pack[section]}, name)
                if section in renamed:
                    renamed[section][name] = target
            if action in ("create", "rename"):
                taken[section].add(target)
            items.append({"section": section, "name": name, "target": target, "action": action, "conflict": conflict})

    # Presets merge into a table: either one this import writes (possibly renamed) or one already saved.
    imported_tables = {item["name"]: item for item in items if item["section"] == "paramsets"}
    for row in pack["presets"]:
        table = row["paramset"]
        entry = imported_tables.get(table)
        if entry and entry["action"] in ("create", "rename", "overwrite"):
            # The pack's table replaces any saved one, and pack tables carry no presets of their own.
            target_table, base = entry["target"], {}
        elif table in existing["paramsets"]:
            target_table = table
            base = paramsets.load(data, table)["presets"]
        else:
            items.append({"section": "presets", "name": preset_id(table, row["name"]), "target": None,
                          "action": "missing-table", "conflict": False})
            continue
        key = preset_id(table, row["name"])
        conflict = row["name"] in base
        action = decide("presets", key, conflict)
        target = row["name"]
        if action == "rename":
            target = free_name(set(base) | {r["name"] for r in pack["presets"] if r["paramset"] == table}, row["name"])
        items.append({"section": "presets", "name": key, "target": preset_id(target_table, target),
                      "action": action, "conflict": conflict})

    warnings = reference_warnings(remap(pack, renamed), data)
    warnings += [{"code": "MISSING_PARAMSET", "section": "presets", "name": item["name"],
                  "message": f"Preset {item['name']} 所屬的參數表不在設定包也不在本機，會略過。"}
                 for item in items if item["action"] == "missing-table"]
    for section in ("recipes", "artifactRules"):
        for row in pack[section]:
            warnings += machine_warnings(section, row["name"], row)
    return {"items": items, "unresolved": unresolved, "warnings": dedupe(warnings), "renamed": renamed}


def dedupe(rows):
    seen, result = set(), []
    for row in rows:
        key = (row["code"], row.get("name"), row["message"])
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def remap(pack, renamed):
    """Pack copy with renamed tables/rules applied to recipe and preset references."""
    copy = json.loads(json.dumps(pack))
    for row in copy["recipes"]:
        if row.get("paramset") in renamed["paramsets"]:
            row["paramset"] = renamed["paramsets"][row["paramset"]]
        if row.get("artifacts") in renamed["artifactRules"]:
            row["artifacts"] = renamed["artifactRules"][row["artifacts"]]
    for row in copy["presets"]:
        if row["paramset"] in renamed["paramsets"]:
            row["paramset"] = renamed["paramsets"][row["paramset"]]
    return copy


def apply(data, pack, decisions=None, default=None, *, dry_run=False):
    """Import a parsed pack. Nothing is written when dry_run is set or any conflict is unresolved."""
    result = plan(data, pack, decisions, default)
    if result["unresolved"] and not dry_run:
        raise ToolError("CONFLICT_DECISION_REQUIRED",
                        "Choose overwrite, rename or skip for every name that already exists.", 4,
                        conflicts=result["unresolved"])
    result.update(written=[], backups=None, dryRun=dry_run)
    if dry_run:
        return result

    renamed = result["renamed"]
    rows = {section: {row["name"]: row for row in pack[section]} for section in ("recipes", "paramsets", "artifactRules")}
    writes = {}   # (folder, target) -> record
    for item in result["items"]:
        section, action = item["section"], item["action"]
        if section == "presets" or action not in ("create", "rename", "overwrite"):
            continue
        record = json.loads(json.dumps(rows[section][item["name"]]))
        record["name"] = item["target"]
        if section == "recipes":
            if record.get("paramset") in renamed["paramsets"]:
                record["paramset"] = renamed["paramsets"][record["paramset"]]
            if record.get("artifacts") in renamed["artifactRules"]:
                record["artifacts"] = renamed["artifactRules"][record["artifacts"]]
        writes[(FOLDERS[section], item["target"])] = record
    for item, row in zip([i for i in result["items"] if i["section"] == "presets"], pack["presets"]):
        if item["action"] not in ("create", "rename", "overwrite"):
            continue
        table, preset = split_preset(item["target"])
        key = ("paramsets", table)
        if key not in writes:
            writes[key] = paramsets.load(data, table)
        writes[key].setdefault("presets", {})[preset] = row["values"]
    # Validate every final record before touching disk.
    for (folder, target), record in writes.items():
        if folder == "paramsets":
            writes[(folder, target)] = paramsets.normalize(record)
            for preset in writes[(folder, target)]["presets"]:
                paramsets.resolve(writes[(folder, target)], preset)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = data / "backups" / f"pack-import-{stamp}"
    for folder in ("artifacts", "paramsets", "recipes"):
        targets = [(target, record) for (kind, target), record in writes.items() if kind == folder]
        if not targets:
            continue
        with file_lock(data / f".{folder}.lock"):
            for target, record in targets:
                path = named_file(data, folder, target)
                if path.exists():
                    (backup / folder).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup / folder / path.name)
                    result["backups"] = str(backup)
                write_json(path, record)
                result["written"].append({"folder": folder, "name": target})
    return result
