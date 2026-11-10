"""Incremental log reads and compiler error classification."""

import codecs
from collections import deque
import io
import re


def encoding_for(data, final=False):
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", 2
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", 2
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8", 3
    try:
        codecs.getincrementaldecoder("utf-8")().decode(data, final=final)
    except UnicodeDecodeError:
        return "cp950", 0
    return "utf-8", 0


def read_from(path, offset=0, *, final=True, limit=256 * 1024):
    """Return text and the next byte offset, preserving incomplete characters."""
    if offset < 0 or limit < 4:
        raise ValueError("Invalid offset or read size")
    try:
        with path.open("rb") as stream:
            prefix = stream.read(4096)
            encoding, bom = encoding_for(prefix)
            stream.seek(0, 2)
            size = stream.tell()
            start = max(min(offset, size), bom)
            stream.seek(start)
            raw = stream.read(min(limit, size - start))
    except FileNotFoundError:
        return {"text": "", "nextOffset": 0}
    at_end = final and start + len(raw) == size
    if not bom and encoding == "utf-8":
        encoding, _ = encoding_for(raw, final=at_end)
    if not final and start == 0 and raw in (b"\xff", b"\xfe", b"\xef", b"\xef\xbb"):
        return {"text": "", "nextOffset": 0}
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    text = decoder.decode(raw, final=at_end)
    return {"text": text, "nextOffset": start + len(raw) - len(decoder.getstate()[0])}


def iter_lines(path):
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return
    with stream:
        prefix = stream.read(65536)
        encoding, bom = encoding_for(prefix)
        if encoding.startswith("utf-16"):
            stream.seek(bom)
            with io.TextIOWrapper(stream, encoding=encoding, errors="replace") as text:
                for line in text:
                    yield line.rstrip("\r\n")
            return
        stream.seek(bom)
        for raw in stream:
            line_encoding = encoding if bom or encoding == "cp950" else encoding_for(raw, final=True)[0]
            yield raw.decode(line_encoding, errors="replace").rstrip("\r\n")


# Order matters: first matching pattern names the category.
PATTERNS = [
    ("compile", re.compile(r"(?<![\w./\\-])(?P<file>[A-Za-z]:[\\/][^:(]+?|[\w./\\-]+\.(?:c|h|cpp|asm|nasm|s|vfr|hfr|uni|asl|aslc|inf|dec|dsc|fdf))[(:](?P<line>\d+)(?:[,:]\d+)?\)?\s*:\s*(?:fatal )?error\b", re.I)),
    ("link", re.compile(r"(lld-link|ld\.lld|link)\s*:\s*error|undefined symbol|unresolved external|undefined reference", re.I)),
    ("msvc", re.compile(r"\berror (?:C|LNK|RC|U|A)\d{4}\b", re.I)),
    ("build", re.compile(r"^.*?:\s*error \d{4,5}\b", re.I)),
    ("tool", re.compile(r"\b(GenFw|GenFv|GenFfs|GenSec|VfrCompile|Trim|BrotliCompress|LzmaCompress|iasl|nasm)\b.*\berror\b", re.I)),
    ("make", re.compile(r"(NMAKE|make(\[\d+\])?)\s*:\s*(fatal error|\*\*\*)", re.I)),
    ("python", re.compile(r"^Traceback \(most recent call last\)")),
    ("generic", re.compile(r"(^|\s)(ERROR|FATAL)\b[:\s-]|\berror:\s", re.I)),
]
IGNORE = re.compile(
    r"-Werror|-Wno-error|/WX\b|ErrorLib|ErrorCode|error\.h\b|ReportStatusCode|\b0 error|\berrors?\s*=\s*0|"
    r"MaxErrors|error_count|ASSERT_EFI_ERROR|EFI_ERROR\(|DEBUG_ERROR|\bErrorStatus|Error(Handler|Log|Info)",
    re.I,
)
RESULT_FAIL = re.compile(r"^\s*-\s*Failed\s*-", re.I)
RESULT_DONE = re.compile(r"^\s*-\s*Done\s*-", re.I)
CASCADE = re.compile(r"\b(?:NMAKE\b.*\bU1077|error\s+7000)\b", re.I)
WARNING = re.compile(r"\bwarning\b(?:\s+[A-Z]+\d+\b|\s*:)", re.I)
FAIL = re.compile(r"\b(?:failed|failure|fail)\b", re.I)
PRIMARY = {"compile", "msvc", "link"}
ERROR_HINT = re.compile(r"error|fatal|undefined|unresolved|traceback|\*\*\*", re.I)


def classify(line):
    if not ERROR_HINT.search(line):
        return None, None
    if IGNORE.search(line) and not re.search(r":\s*(fatal )?error\b|error \d{4}", line, re.I):
        return None, None
    for kind, pattern in PATTERNS:
        match = pattern.search(line)
        if match:
            location = None
            if kind == "compile":
                location = f'{match["file"]}:{match["line"]}'
            return kind, location
    return None, None


def search(path, q="", kind=None):
    predicates = {
        None: lambda line: True,
        "error": lambda line: classify(line)[0] is not None,
        "fail": FAIL.search,
        "warning": WARNING.search,
    }
    if kind not in predicates:
        raise ValueError("Invalid log search kind")
    query = q.casefold()
    matches = predicates[kind]
    return [{"line": number, "text": line} for number, line in enumerate(iter_lines(path), 1)
            if query in line.casefold() and matches(line)]


def search_page(path, q, kind, index, limit):
    counts = {"error": 0, "fail": 0, "warning": 0}
    total = 0
    matches = []
    query = q.casefold()
    for number, line in enumerate(iter_lines(path), 1):
        if query not in line.casefold():
            continue
        categories = {
            "error": classify(line)[0] is not None,
            "fail": bool(FAIL.search(line)),
            "warning": bool(WARNING.search(line)),
        }
        for category, matched in categories.items():
            counts[category] += matched
        if kind is None or categories[kind]:
            if index <= total < index + limit:
                matches.append({"line": number, "text": line[:2000]})
            total += 1
    return {"counts": counts, "total": total, "matches": matches}


def triage(path):
    errors = {}
    cascades = []
    tail = deque(maxlen=200)
    line_count = 0
    for line_count, line in enumerate(iter_lines(path), 1):
        tail.append(line)
        kind, location = classify(line)
        if not kind:
            continue
        error = {"line": line_count, "kind": kind, "location": location, "text": line.strip(), "count": 1}
        if CASCADE.search(line):
            cascades.append(error)
            continue
        key = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "#", line.strip())
        key = re.sub(r"\s+", " ", key)[:200]
        if key in errors:
            errors[key]["count"] += 1
        else:
            errors[key] = error
    result = "unknown"
    for line in reversed(tail):
        if RESULT_FAIL.match(line):
            result = "failed"
            break
        if RESULT_DONE.match(line):
            result = "success"
            break
    ordered = list(errors.values())
    root = next((error for error in ordered if error["kind"] in PRIMARY), ordered[0] if ordered else None)
    return {"result": result, "lineCount": line_count, "rootError": root,
            "errors": ordered, "cascadeErrors": cascades}
