import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from build_runner.prompts import ends_with_prompt
from build_runner.runner import EnterForwarder


class PromptDetectTests(unittest.TestCase):
    def test_known_prompts_at_the_end(self):
        self.assertTrue(ends_with_prompt("compiling lib\r\nEnter Your Username: "))
        self.assertTrue(ends_with_prompt("Enter password for https://git.corp/fw.git:"))
        self.assertTrue(ends_with_prompt("Enter passphrase for key 'id_rsa' (enter to skip):"))
        self.assertTrue(ends_with_prompt("ENTER YOUR USERNAME:"))

    def test_prompt_not_waiting_at_the_end(self):
        self.assertFalse(ends_with_prompt(""))
        self.assertFalse(ends_with_prompt("   \r\n"))
        self.assertFalse(ends_with_prompt("link x.obj\nBuild completed.\n"))
        # The prompt was answered already, so the step is not waiting.
        self.assertFalse(ends_with_prompt("Enter Your Username: service\ncontinuing...\n"))


class EnterForwarderTests(unittest.TestCase):
    """Drive a real child process that blocks on stdin, like a stalled step."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.log_path = root / "build.log"
        self.log_path.touch()
        self.request = root / "enter.request"

    def forwarder(self, *, auto_skip=True):
        forwarder = EnterForwarder(self.log_path.open("ab", buffering=0), self.log_path,
                                   self.request, auto_skip=auto_skip)
        self.addCleanup(forwarder.log.close)
        forwarder.POLL_SEC = 0.05
        forwarder.STALL_SEC = 0.2
        forwarder.COOLDOWN_SEC = 0.2
        return forwarder

    def child(self, code, log):
        return subprocess.Popen([sys.executable, "-c", code], cwd=str(self.log_path.parent),
                                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)

    def test_auto_sends_enter_when_output_stalls_on_prompt(self):
        forwarder = self.forwarder()
        process = self.child(
            "import sys; sys.stdout.write('Enter Your Username: '); sys.stdout.flush(); "
            "sys.stdout.write('RESUMED' + repr(sys.stdin.readline())); sys.stdout.flush()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        self.assertEqual(forwarder.wait(process, time.monotonic() + 15), 0)
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("RESUMED'\\n'", text)
        self.assertIn("Enter sent automatically", text)
        self.assertFalse(self.request.exists())

    def test_manual_request_is_consumed_and_forwarded(self):
        forwarder = self.forwarder(auto_skip=False)
        process = self.child(
            "import sys; sys.stdout.write('GOT' + repr(sys.stdin.readline())); sys.stdout.flush()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        # The request lands after wait() starts (stale files are cleared at step start).
        timer = threading.Timer(0.4, lambda: self.request.write_text("manual", encoding="utf-8"))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(forwarder.wait(process, time.monotonic() + 15), 0)
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("GOT'\\n'", text)
        self.assertIn("Enter sent", text)
        self.assertNotIn("automatically", text)
        self.assertFalse(self.request.exists())

    def test_auto_skip_off_leaves_the_step_waiting(self):
        forwarder = self.forwarder(auto_skip=False)
        process = self.child(
            "import sys; sys.stdout.write('Enter password for vault: '); sys.stdout.flush(); "
            "sys.stdin.readline()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        with self.assertRaises(subprocess.TimeoutExpired):
            forwarder.wait(process, time.monotonic() + 1.5)
        self.assertNotIn("Enter sent", self.log_path.read_text(encoding="utf-8", errors="replace"))

    def test_auto_gives_up_after_five_sends(self):
        forwarder = self.forwarder()
        # The child re-prompts after every Enter: five presses, then the forwarder stops.
        process = self.child(
            "import sys\n"
            "for i in range(12):\n"
            "    sys.stdout.write('Enter passphrase for key (enter to skip):\\n')\n"
            "    sys.stdout.flush()\n"
            "    sys.stdin.readline()\n"
            "sys.stdout.write('ALL SKIPPED\\n'); sys.stdout.flush()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        outcome = {}

        def run():
            try:
                outcome["code"] = forwarder.wait(process, time.monotonic() + 30)
            except subprocess.TimeoutExpired:
                outcome["code"] = "timeout"

        threading.Thread(target=run, daemon=True).start()
        text = ""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            if "too many input prompts" in text:
                break
            time.sleep(0.1)
        self.assertEqual(text.count("Enter sent automatically"), forwarder.MAX_AUTO)
        self.assertIn("too many input prompts", text)
        self.assertNotIn("ALL SKIPPED", text)   # the 6th prompt stays waiting; the user takes over


if __name__ == "__main__":
    unittest.main()
