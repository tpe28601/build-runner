import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from build_runner.console import inject_enter

# A tool that waits on raw console keys: launched with its own hidden console
# (CREATE_NEW_CONSOLE), stdin/stdout DEVNULL so a pipe could never be the path
# the Enter travels on.
GETWCH_CONSOLE = (
    "import msvcrt, os; from pathlib import Path\n"
    "Path('child.ready').write_text(str(os.getpid()))\n"
    "chars = [msvcrt.getwch() for _ in range(3)]\n"
    "Path('child.got').write_text(''.join(chars))\n"
)

# Production shape, replayed inside the test: a DETACHED helper stands in for
# the runner and starts the step the way runner.py does (plain Popen, no
# flags). The bat's python grandchild then owns a real console - the same one
# builda.bat's getpass waits on - and inject_enter must reach it.
PROD_SHAPE_HELPER = r"""
import os, subprocess, sys, time
from pathlib import Path
d = Path(os.environ["SHAPE_DIR"])
sys.path.insert(0, os.environ["RUNNER_ROOT"])
from build_runner.console import inject_enter
code = ("import msvcrt, os\n"
        "from pathlib import Path\n"
        "d = os.environ['SHAPE_DIR']\n"
        "Path(d + '/child.ready').write_text(str(os.getpid()))\n"
        "got = [msvcrt.getwch() for _ in range(3)]\n"
        "Path(d + '/child.got').write_text(''.join(got))\n")
(d / "chain.py").write_text(code, encoding="utf-8")
bat = d / "step.bat"
bat.write_text('@echo off\r\n"%PY%" "%SCRIPT%"\r\n', encoding="ascii")
env = {**os.environ, "PY": sys.executable, "SCRIPT": str(d / "chain.py")}
log = (d / "build.log").open("ab", buffering=0)
step = subprocess.Popen([str(bat)], stdin=subprocess.PIPE, stdout=log,
                        stderr=subprocess.STDOUT, env=env, cwd=str(d))
deadline = time.monotonic() + 15
while not (d / "child.ready").exists() and time.monotonic() < deadline:
    time.sleep(0.1)
injected = 0
while injected < 3 and time.monotonic() < deadline:
    if inject_enter(step.pid):
        injected += 1
    time.sleep(0.3)
(d / "helper.done").write_text(str(injected))
if step.poll() is None:
    step.kill()
step.wait()
"""


# Raw INPUT_RECORD parsing (no msvcrt/getpass), reusing console.py's own ctypes
# bindings so the reader and the writer agree on the layout. Regression guard
# for the INPUT-record stride: with a wrong union pad the OS silently drops
# the key-up record and only this kind of reader notices.
READER_CONSOLE = (
    "import ctypes, json, os, sys, time\n"
    "from ctypes import wintypes\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, os.environ['RUNNER_ROOT'])\n"
    "from build_runner.console import (kernel32, Input, KEY_EVENT, GENERIC_READ, GENERIC_WRITE,\n"
    "                                  FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING, INVALID)\n"
    "kernel32.ReadConsoleInputW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Input),\n"
    "                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))\n"
    "kernel32.ReadConsoleInputW.restype = wintypes.BOOL\n"
    "handle = kernel32.CreateFileW('CONIN$', GENERIC_READ | GENERIC_WRITE,\n"
    "                              FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)\n"
    "Path('child.ready').write_text(str(os.getpid()))\n"
    "events = (Input * 2)()\n"
    "read = wintypes.DWORD(0)\n"
    "keys = []\n"
    "deadline = time.monotonic() + 20\n"
    "while len(keys) < 2 and time.monotonic() < deadline and handle not in INVALID:\n"
    "    if not kernel32.ReadConsoleInputW(handle, events, 2, ctypes.byref(read)):\n"
    "        break\n"
    "    for i in range(read.value):\n"
    "        if events[i].type & 0xFFFF == KEY_EVENT:\n"
    "            key = events[i].Union.KeyEvent\n"
    "            keys.append([1 if key.bKeyDown else 0, key.wVirtualKeyCode, ord(key.UnicodeChar)])\n"
    "Path('child.got').write_text(json.dumps(keys))\n"
)


class ConsoleInjectTests(unittest.TestCase):
    """Inject Enter into real child processes through their console input."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ready = self.root / "child.ready"
        self.got = self.root / "child.got"

    def launch(self, code, flags, startupinfo=None, use_pipe=False):
        kwargs = dict(cwd=str(self.root), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=flags)
        if use_pipe:
            kwargs["stdin"] = subprocess.PIPE
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        if startupinfo is not None:
            kwargs["startupinfo"] = startupinfo
        process = subprocess.Popen([sys.executable, "-c", code], **kwargs)
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        return process

    def wait_file(self, path, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    return text
            time.sleep(0.05)
        self.fail(f"{path.name} never appeared")

    def hidden_console_startupinfo(self):
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0   # SW_HIDE: the injected tests must not flash windows
        return info

    def test_hidden_console_child_receives_injected_enter(self):
        # CREATE_NEW_CONSOLE child with stdin/stdout DEVNULL: the only way its
        # getwch() can return is the console key events written by inject_enter.
        process = self.launch(GETWCH_CONSOLE, subprocess.CREATE_NEW_CONSOLE,
                              startupinfo=self.hidden_console_startupinfo())
        self.wait_file(self.ready)
        self.addCleanup(process.wait, 30)
        for _ in range(3):
            self.assertTrue(inject_enter(process.pid))
            time.sleep(0.3)
        got = self.wait_file(self.got)
        # The console reports Enter as CR or LF depending on the reader; either
        # one proves the injected key event is what unblocked getwch().
        self.assertEqual(len(got), 3)
        self.assertTrue(all(c in ("\r", "\n") for c in got), repr(got))

    def test_key_down_and_key_up_both_arrive_as_valid_records(self):
        # One inject_enter() must deliver two well-formed KEY_EVENT records:
        # key-down carrying the character and key-up without it. A broken
        # INPUT_RECORD stride loses the key-up inside the console driver.
        env = {**os.environ, "RUNNER_ROOT": str(Path(__file__).resolve().parent.parent)}
        process = subprocess.Popen([sys.executable, "-c", READER_CONSOLE], cwd=str(self.root),
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE,
                                   startupinfo=self.hidden_console_startupinfo())
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        self.wait_file(self.ready)
        self.addCleanup(process.wait, 30)
        self.assertTrue(inject_enter(process.pid))
        keys = json.loads(self.wait_file(self.got, timeout=30))
        self.assertEqual(len(keys), 2, repr(keys))
        down, up = keys
        self.assertEqual([down[0], up[0]], [1, 0], repr(keys))       # down first, then up
        self.assertEqual([down[1], up[1]], [0x0D, 0x0D], repr(keys))  # both VK_RETURN
        self.assertEqual([down[2], up[2]], [0x0D, 0], repr(keys))     # char only on key-down

    def test_production_shape_bat_step_is_injectable(self):
        # Detached helper (stands in for the runner) starts a .bat step the way
        # runner.py does; the bat's python grandchild blocks in getwch() and
        # three inject_enter() calls from the helper must free it.
        helper = self.root / "helper.py"
        helper.write_text(PROD_SHAPE_HELPER, encoding="utf-8")
        env = {**os.environ,
               "SHAPE_DIR": str(self.root),
               "RUNNER_ROOT": str(Path(__file__).resolve().parent.parent),
               "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, str(helper)], env=env, cwd=str(self.root),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS)
        self.addCleanup(lambda: (proc.kill(), proc.wait()) if proc.poll() is None else None)
        got = self.wait_file(self.got, timeout=30)
        self.assertTrue(all(c in ("\r", "\n") for c in got), repr(got))
        self.assertEqual(self.wait_file(self.root / "helper.done"), "3")

    def test_process_without_console_reports_false(self):
        # A pure-pipe step must keep the old behaviour: False, no side effects.
        process = self.launch("import time; time.sleep(8)", subprocess.DETACHED_PROCESS)
        self.assertFalse(inject_enter(process.pid))
        time.sleep(0.5)
        self.assertIsNone(process.poll())   # the sleeper is untouched

    def test_unknown_pid_reports_false(self):
        self.assertFalse(inject_enter(0x7FFFFFF0))


if __name__ == "__main__":
    unittest.main()
