from pathlib import Path
import codecs
import tempfile
import unittest

from build_runner import logtools


class LogTests(unittest.TestCase):
    def test_log_search_and_byte_offsets_with_windows_encodings(self):
        for encoding in ("utf-8", "utf-16", "cp950"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "build.log"
                first = "測試\r\n".encode(encoding)
                path.write_bytes(first)
                part = logtools.read_from(path, 0)
                self.assertEqual(part["text"], "測試\r\n")
                self.assertEqual(part["nextOffset"], len(first))
                second = "warning: 測試\r\n".encode(encoding)
                if encoding == "utf-16":
                    second = second[2:]
                with path.open("ab") as stream:
                    stream.write(second)
                self.assertEqual(logtools.read_from(path, part["nextOffset"])["text"], "warning: 測試\r\n")
                self.assertEqual(logtools.search(path, "測試", "warning")[0]["line"], 2)

    def test_incremental_read_retries_incomplete_character(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            path.write_bytes(b"start\n\xe6")
            partial = logtools.read_from(path, 0, final=False)
            self.assertEqual(partial, {"text": "start\n", "nextOffset": 6})
            path.write_bytes("start\n測試\n".encode("utf-8"))
            self.assertEqual(logtools.read_from(path, 6)["text"], "測試\n")


class TailTests(unittest.TestCase):
    def test_tail_matches_iter_lines_numbering_for_every_encoding(self):
        for encoding in ("utf-8", "utf-16", "cp950"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "build.log"
                lines = [f"line {number} 結尾" for number in range(1, 101)]
                path.write_text("\n".join(lines) + "\n", encoding=encoding, newline="")
                result = logtools.tail(path, 20)
                self.assertEqual(result["text"].splitlines()[-20:], lines[-20:])
                self.assertEqual(result["startLine"], 81)
                self.assertEqual(result["nextOffset"], path.stat().st_size)

    def test_tail_returns_whole_file_when_shorter_than_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            path.write_text("a\nb\nc\n", encoding="utf-8", newline="")
            result = logtools.tail(path, 10)
            self.assertEqual(result, {"text": "a\nb\nc\n", "nextOffset": 6, "startLine": 1})

    def test_tail_handles_missing_file_and_no_trailing_newline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            self.assertEqual(logtools.tail(path, 5), {"text": "", "nextOffset": 0, "startLine": 1})
            path.write_text("a\nb\nc", encoding="utf-8", newline="")
            result = logtools.tail(path, 10)
            self.assertEqual((result["text"], result["startLine"]), ("a\nb\nc", 1))

    def test_tail_final_false_holds_back_incomplete_character(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            path.write_bytes(b"start\n\xe6")
            partial = logtools.tail(path, 5, final=False)
            self.assertEqual((partial["text"], partial["nextOffset"]), ("start\n", 6))
            with path.open("ab") as stream:
                stream.write(b"\xb8\xac" + "試\n".encode("utf-8"))
            self.assertEqual(logtools.read_from(path, partial["nextOffset"])["text"], "測試\n")

    def test_tail_utf16_live_keeps_partial_line(self):
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "build.log"
                bom = codecs.BOM_UTF16_LE if encoding.endswith("le") else codecs.BOM_UTF16_BE
                data = bom + "first\nsecond\npartial".encode(encoding)   # no trailing newline
                path.write_bytes(data)
                live = logtools.tail(path, 5, final=False)
                # The half-written line must stay partial so line numbers keep matching.
                self.assertEqual((live["text"], live["nextOffset"]), ("first\nsecond\npartial", len(data)))
                with path.open("ab") as stream:
                    stream.write("-done\n".encode(encoding))
                self.assertEqual(logtools.read_from(path, live["nextOffset"], final=True)["text"], "-done\n")
                self.assertEqual(logtools.tail(path, 5)["text"], "first\nsecond\npartial-done\n")

    def test_tail_utf16_live_holds_back_half_code_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            data = codecs.BOM_UTF16_LE + "first\nsecond\n".encode("utf-16-le")
            path.write_bytes(data + b"\x00")   # writer stopped mid code unit
            live = logtools.tail(path, 5, final=False)
            self.assertEqual((live["text"], live["nextOffset"]), ("first\nsecond\n", len(data)))


class ScanCacheTests(unittest.TestCase):
    def test_search_page_and_triage_refresh_after_append(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            path.write_text("normal\nerror C2065: x\n", encoding="utf-8", newline="")
            first = logtools.triage(path)
            self.assertEqual(first["rootError"]["kind"], "msvc")
            self.assertEqual(logtools.search_page(path, "", "error", 0, 100)["total"], 1)
            # Second call at identical size/mtime must hit the cache; appending invalidates it.
            self.assertEqual(logtools.triage(path)["rootError"]["line"], first["rootError"]["line"])
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write("error C2065: y\n")
            self.assertEqual(logtools.search_page(path, "", "error", 0, 100)["total"], 2)
            self.assertEqual(len(logtools.triage(path)["errors"]), 2)
