"""Incremental log reads and compiler error classification."""

import codecs
from collections import deque, OrderedDict
import io
import re
import threading


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


TAIL_CHUNK = 256 * 1024
TAIL_MAX_READ = 16 * 1024 * 1024


def _newline_count(path, stop):
    """Count line breaks in the first ``stop`` bytes without decoding."""
    total = 0
    with path.open("rb") as stream:
        remaining = stop
        while remaining > 0:
            raw = stream.read(min(TAIL_CHUNK, remaining))
            if not raw:
                break
            total += raw.count(b"\n")
            remaining -= len(raw)
    return total


def tail(path, count, *, final=True):
    """Return the last ``count`` lines with their absolute numbering."""
    try:
        with path.open("rb") as stream:
            stream.seek(0)
            encoding, bom = encoding_for(stream.read(4096))
            stream.seek(0, 2)
            size = stream.tell()
            if not size:
                return {"text": "", "nextOffset": 0, "startLine": 1}
            if encoding.startswith("utf-16"):
                # UTF-16 line breaks cannot be located byte-wise: decode fully.
                rows = deque(enumerate(iter_lines(path), 1), maxlen=count)
                start_line = rows[0][0] if rows else 1
                text = "".join(line + "\n" for _, line in rows)
                pending = 0 if final else size % 2
                if pending:
                    # A half code unit sits at EOF: TextIOWrapper appended a replacement
                    # character for it. Drop that and the newline we added so the
                    # incremental reader can finish the code unit on the next poll.
                    if text.endswith("\n"):
                        text = text[:-1]
                    if text.endswith("\ufffd"):
                        text = text[:-1]
                elif not final and rows:
                    # Live job, whole code units: withhold the trailing newline unless the
                    # file actually ends on one, so a half-written line stays partial.
                    stream.seek(size - 2)
                    if stream.read(2) != (b"\n\x00" if encoding.endswith("le") else b"\x00\n"):
                        text = text[:-1]
                return {"text": text, "nextOffset": size - pending, "startLine": start_line}
            position, collected = size, bytearray()
            while position > bom and collected.count(b"\n") <= count and len(collected) < TAIL_MAX_READ:
                step = min(TAIL_CHUNK, position - bom)
                position -= step
                stream.seek(position)
                collected[:0] = stream.read(step)
            cut = 0
            if position > bom:
                head = collected.find(b"\n")
                cut = len(collected) if head == -1 else head + 1
            while collected.count(b"\n", cut) > count:
                nxt = collected.find(b"\n", cut)
                if nxt == -1:
                    break
                cut = nxt + 1
            window = bytes(collected[cut:])
            start = position + cut
            if not bom and encoding == "utf-8":
                encoding = encoding_for(window, final=final)[0]
            start_line = _newline_count(path, start) + 1
    except FileNotFoundError:
        return {"text": "", "nextOffset": 0, "startLine": 1}
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    text = decoder.decode(window, final=final)
    # While the job runs, hold back an incomplete trailing character for the next read.
    pending = 0 if final else len(decoder.getstate()[0])
    return {"text": text, "nextOffset": size - pending, "startLine": start_line}


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


_SCAN_LOCK = threading.Lock()
_SCAN_CACHE = OrderedDict()   # (path, size, mtime_ns, args) -> whole-file scan result
_SCAN_CACHE_MAX = 8


def _cached_scan(path, args, produce):
    """Cache whole-file scans; file size and mtime invalidate appended output.

    Callers must treat the returned value (including nested dicts) as read-only.
    """
    try:
        info = path.stat()
    except OSError:
        return produce()
    key = (str(path), info.st_size, info.st_mtime_ns) + args
    with _SCAN_LOCK:
        hit = _SCAN_CACHE.get(key)
        if hit is not None:
            _SCAN_CACHE.move_to_end(key)
            return hit
    value = produce()
    with _SCAN_LOCK:
        _SCAN_CACHE[key] = value
        while len(_SCAN_CACHE) > _SCAN_CACHE_MAX:
            _SCAN_CACHE.popitem(last=False)
    return value


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
    result = _cached_scan(path, ("search", q, kind, index, limit),
                          lambda: _search_page(path, q, kind, index, limit))
    return {"counts": dict(result["counts"]), "total": result["total"], "matches": list(result["matches"])}


def _search_page(path, q, kind, index, limit):
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
    report = _cached_scan(path, ("triage",), lambda: _triage(path))
    return dict(report, errors=list(report["errors"]), cascadeErrors=list(report["cascadeErrors"]))


def _triage(path):
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
