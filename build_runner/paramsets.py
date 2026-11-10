"""Import local parameter tables and resolve validated build arguments."""

import csv
import io
import json
from pathlib import Path
import posixpath
import re
from xml.etree import ElementTree
import zipfile

from .paths import ToolError, named_file, read_json

DEFAULT_COMMAND = "builda.bat"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_ROWS = 5000
FLAG = re.compile(r"(?:--?|/)[A-Za-z0-9_?][A-Za-z0-9_?-]*")
KEY = re.compile(r"[\w-]{1,128}")
HEADERS = {
    "參數": "flag", "旗標": "flag", "用途": "desc", "說明": "desc", "分類": "group",
    "類型": "type", "選項": "choices", "預設": "default", "互斥": "conflicts",
}
PARAM_TYPES = {"switch", "choice", "value"}


def invalid(message):
    return ToolError("INVALID_PARAMSET", message)


def text(value, field, default=""):
    if value is None:
        return default
    if not isinstance(value, str) or "\0" in value:
        raise invalid(f"{field} must be text.")
    return value


def key_from_flag(flag):
    if not FLAG.fullmatch(flag):
        raise invalid(f"Invalid flag: {flag}")
    return flag[1:].lstrip("-").replace("?", "question").casefold()


def validate_value(param, value):
    key, kind = param["key"], param["type"]
    if kind == "switch":
        if type(value) is bool:
            return value
    elif value is None or value == "":
        return ""
    elif isinstance(value, str) and "\0" not in value:
        if kind == "value" or value in param["choices"]:
            return value
    raise ToolError("INVALID_PARAM_VALUE", f"Invalid value for {param['flag']}.", key=key)


def values_for(params, values):
    if not isinstance(values, dict) or any(key not in params for key in values):
        raise ToolError("INVALID_PARAMS", "Parameter values must use keys from the selected table.")
    return {key: validate_value(params[key], value) for key, value in values.items()}


def normalize(source):
    if not isinstance(source, dict):
        raise invalid("A parameter table must be a JSON object.")
    name = text(source.get("name"), "name").strip()
    command = text(source.get("command"), "command", DEFAULT_COMMAND).strip()
    rows = source.get("params")
    if not name or not command or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise invalid("Provide a name, command and nonempty parameter list.")
    params = {}
    flags = set()
    for row in rows:
        if not isinstance(row, dict):
            raise invalid("Parameter entries must be objects.")
        flag = text(row.get("flag"), "flag").strip()
        fallback = key_from_flag(flag)
        key = text(row.get("key"), "key", fallback).strip() or fallback
        kind = text(row.get("type"), "type", "switch").strip().lower() or "switch"
        if not KEY.fullmatch(key) or key in params or flag.casefold() in flags or kind not in PARAM_TYPES:
            raise invalid(f"Duplicate or invalid parameter: {flag}")
        param = {"key": key, "flag": flag, "type": kind,
                 "group": text(row.get("group"), "group").strip() or "一般",
                 "desc": text(row.get("desc"), "desc")}
        if kind == "choice":
            choices = row.get("choices")
            if (not isinstance(choices, list) or not choices
                    or any(not isinstance(item, str) or not item or "\0" in item for item in choices)
                    or len(set(choices)) != len(choices)):
                raise invalid(f"{flag} needs a list of unique choices.")
            param["choices"] = choices
        if "placeholder" in row:
            param["placeholder"] = text(row["placeholder"], "placeholder")
        if "default" in row:
            param["default"] = validate_value(param, row["default"])
        params[key] = param
        flags.add(flag.casefold())
    conflicts = source.get("conflicts", [])
    if not isinstance(conflicts, list):
        raise invalid("conflicts must contain groups of parameter keys.")
    unique = []
    for group in conflicts:
        if (not isinstance(group, list) or len(group) < 2
                or any(not isinstance(key, str) or key not in params for key in group)
                or len(set(group)) != len(group)):
            raise invalid("Each conflict must list two or more different parameter keys.")
        if group not in unique:
            unique.append(group)
    presets = source.get("presets", {})
    if not isinstance(presets, dict):
        raise invalid("presets must be an object keyed by preset name.")
    for key in presets:
        if not isinstance(key, str) or not key.strip() or key != key.strip() or "\0" in key or len(key) > 128:
            raise invalid("Invalid preset name.")
    notes = source.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        raise invalid("notes must be a list of strings.")
    return {"name": name, "command": command, "params": list(params.values()), "conflicts": unique,
            "presets": {key: values_for(params, value) for key, value in presets.items()}, "notes": notes}


def load(data, name):
    source = read_json(named_file(data, "paramsets", name))
    if source is None:
        raise ToolError("PARAMSET_NOT_FOUND", f"Parameter table not found: {name}")
    return normalize(source)


def resolve(table, preset=None, overrides=None):
    params = {param["key"]: param for param in table["params"]}
    defaults = {key: param.get("default", False if param["type"] == "switch" else "")
                for key, param in params.items()}
    if preset is not None:
        if not isinstance(preset, str) or preset not in table["presets"]:
            raise ToolError("PRESET_NOT_FOUND", "Preset not found in the selected parameter table.")
        defaults.update(table["presets"][preset])
    if overrides is not None:
        defaults.update(values_for(params, overrides))
    for group in table["conflicts"]:
        selected = [key for key in group if defaults[key]]
        if len(selected) > 1:
            flags = " / ".join(params[key]["flag"] for key in selected)
            raise ToolError("PARAM_CONFLICT", f"Conflicting parameters: {flags}", keys=selected)
    argv = []
    for key, param in params.items():
        value = defaults[key]
        if value:
            argv.append(param["flag"])
            if param["type"] != "switch":
                argv.append(value)
    return argv, defaults


def parse(raw, filename, name, command=None):
    if len(raw) > MAX_SOURCE_BYTES:
        raise invalid("Parameter source is larger than 8 MB.")
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".xlsx":
            source = from_rows(xlsx_rows(raw))
        elif suffix == ".json":
            source = json.loads(raw.decode("utf-8-sig"))
        elif suffix == ".csv":
            source = from_rows(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        elif suffix in (".md", ".markdown"):
            source = from_markdown(raw.decode("utf-8-sig"))
        else:
            raise invalid("Supported source formats: Markdown, CSV, XLSX, JSON.")
    except (ValueError, KeyError, IndexError, OverflowError, RecursionError,
            ElementTree.ParseError, zipfile.BadZipFile, csv.Error, RuntimeError) as exc:
        raise invalid("Unable to read the parameter table. Check the file format and columns.") from exc
    if not isinstance(source, dict):
        raise invalid("A parameter table must be a JSON object.")
    return normalize({**source, "name": name, **({"command": command} if command else {})})


def from_markdown(source):
    group = "一般"
    notes, rows = [], []
    in_table = False
    in_notes = False
    ignored = False
    fenced = False
    tick = chr(96)
    for line in source.splitlines():
        if line.lstrip().startswith((tick * 3, "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)", line)
        if heading:
            group = re.sub(r"^\d+[.)、]\s*", "", heading[1]).strip()
            in_notes = bool(re.search(r"限制|備註|^notes\b", group, re.I))
            ignored = bool(re.search(r"棄用|忽略|deprecated|ignored", group, re.I))
            in_table = False
            continue
        if in_notes and (bullet := re.match(r"^\s*[-*]\s+(.+)", line)):
            notes.append(bullet[1])
        if ignored or not line.strip().startswith("|"):
            in_table = False
            continue
        cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 2:
            continue
        if cells[0].casefold() in ("參數", "旗標", "flag", "parameter"):
            in_table = True
            continue
        if not in_table:
            continue
        # Parse the leading inline-code flags only. Descriptive prose and
        # source line numbers are not part of the invocation.
        head = re.match(r"^" + tick + r"[^" + tick + r"]+" + tick +
                        r"(?:\s*/\s*" + tick + r"[^" + tick + r"]+" + tick + r")*", cells[0])
        if not head:
            continue
        for literal in head[0].split(tick)[1::2]:
            token = literal.strip().split(maxsplit=1)
            if not token or not FLAG.fullmatch(token[0]):
                continue
            row = {"flag": token[0], "group": group, "desc": cells[1]}
            if len(token) == 2:
                hint = token[1]
                if "|" in hint:
                    row.update(type="choice", choices=[item.strip() for item in hint.split("|")])
                else:
                    row.update(type="value", placeholder=hint)
            rows.append(row)
    return {"params": rows, "notes": notes}


def cell_list(value):
    if value.strip().startswith("["):
        return json.loads(value)
    return [item.strip() for item in value.split("|") if item.strip()]


def from_rows(rows):
    rows = iter(rows)
    headers = next((row for row in rows if any(row)), [])
    headers = [HEADERS.get(cell.strip().casefold(), cell.strip().casefold()) for cell in headers]
    if "flag" not in headers:
        raise invalid("The first table row must include a flag column.")
    names = [header for header in headers if header]
    if len(set(names)) != len(names):
        raise invalid("Column names must be unique.")
    params = []
    exclusions = []
    command = None
    for row in rows:
        if not any(row):
            continue
        cells = {key: value.strip() for key, value in zip(headers, row) if key and value.strip()}
        if not cells.get("flag"):
            raise invalid("Each parameter row needs a flag.")
        param = {key: cells[key] for key in ("key", "flag", "type", "group", "desc", "placeholder") if key in cells}
        if "choices" in cells:
            param["choices"] = cell_list(cells["choices"])
            param.setdefault("type", "choice")
        if "default" in cells:
            value = cells["default"]
            if param.get("type", "switch").lower() == "switch":
                lookup = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
                if value.lower() not in lookup:
                    raise invalid(f"Invalid switch default for {cells['flag']}.")
                value = lookup[value.lower()]
            param["default"] = value
        params.append(param)
        key = param.get("key") or key_from_flag(param["flag"])
        if "conflicts" in cells:
            exclusions.extend([key, other] for other in cell_list(cells["conflicts"]))
        command = command or cells.get("command")
        if len(params) > MAX_ROWS:
            raise invalid(f"Parameter tables support up to {MAX_ROWS} rows.")
    return {"params": params, "conflicts": exclusions, "command": command}


def xml_part(archive, name):
    if archive.getinfo(name).file_size > MAX_XML_BYTES:
        raise invalid("XLSX XML part exceeds the size limit.")
    raw = archive.read(name)
    if b"<!DOCTYPE" in raw.replace(b"\0", b"").upper():
        raise invalid("XLSX XML must not contain a document type declaration.")
    root = ElementTree.fromstring(raw)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return root


def xlsx_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        sheet = xml_part(archive, "xl/workbook.xml").find("sheets/sheet")
        if sheet is None:
            raise invalid("XLSX has no worksheet.")
        relation_id = next((value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"), None)
        relations = xml_part(archive, "xl/_rels/workbook.xml.rels")
        relation = next((row for row in relations if row.get("Id") == relation_id), None)
        if relation is None or relation.get("TargetMode") == "External":
            raise invalid("XLSX first worksheet has no internal relationship.")
        target = relation.attrib["Target"]
        location = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
        if not location.startswith("xl/"):
            raise invalid("Invalid XLSX worksheet location.")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [join_text(item) for item in xml_part(archive, "xl/sharedStrings.xml").findall("si")]
        for row in xml_part(archive, location).findall("sheetData/row"):
            cells = []
            for cell in row.findall("c"):
                match = re.match(r"^([A-Z]{1,2})[1-9][0-9]*$", cell.get("r", ""))
                if match is None:
                    raise invalid("Invalid XLSX cell reference.")
                index = 0
                for char in match[1]:
                    index = index * 26 + ord(char) - ord("A") + 1
                if not 1 <= index <= 256:
                    raise invalid("Parameter worksheets support up to 256 columns.")
                cells.extend([""] * (index - len(cells)))
                value = cell.findtext("v", "")
                kind = cell.get("t")
                if kind == "s":
                    index_string = int(value)
                    if index_string < 0:
                        raise invalid("Invalid XLSX shared string index.")
                    value = shared[index_string]
                elif kind == "inlineStr":
                    value = join_text(cell)
                cells[index - 1] = value
            yield cells


def join_text(element):
    return "".join(child.text or "" for child in element.iter("t"))
