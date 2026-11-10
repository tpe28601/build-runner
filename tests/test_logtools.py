from pathlib import Path
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
