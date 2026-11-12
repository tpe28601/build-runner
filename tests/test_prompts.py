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
        # The prompt was answered and the step moved on, so it is not waiting.
        self.assertFalse(ends_with_prompt("Enter Your Username: service\ncontinuing...\n"))

    def test_own_notes_do_not_hide_a_prompt(self):
        # The note can append on top of the newest prompt; the step's real last
        # line still decides. A prompt under the notes stays "waiting", which is
        # correct: another Enter is what a re-ask needs.
        self.assertTrue(ends_with_prompt("Enter Your Username: \n\n[Build Runner] Enter sent\n"))
        self.assertTrue(ends_with_prompt("Enter password for x:\n[Build Runner] note\n"))
        self.assertFalse(ends_with_prompt("Enter Your Username: \n[Build Runner] Enter sent\nnext step runs\n"))
        self.assertFalse(ends_with_prompt("[Build Runner] Enter sent\n\n[Build Runner] other note\n"))


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
        # CREATE_NEW_CONSOLE (hidden) mirrors production, where every step owns
        # its console; sharing the test runner's console would let one test's
        # injected keys answer a later test's getpass. stdio stays redirected.
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        return subprocess.Popen([sys.executable, "-c", code], cwd=str(self.log_path.parent),
                                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info)

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
        ready = self.log_path.parent / "child.ready"
        process = self.child(
            "import sys; from pathlib import Path; Path('child.ready').touch(); "
            "sys.stdout.write('GOT' + repr(sys.stdin.readline())); sys.stdout.flush()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        # wait() clears a stale request at step start, so the request must land
        # while wait() is already polling and the child is certainly still reading.
        outcome = {}

        def run():
            try:
                outcome["code"] = forwarder.wait(process, time.monotonic() + 15)
            except subprocess.TimeoutExpired:
                outcome["code"] = "timeout"

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.request.write_text("manual", encoding="utf-8")
        worker.join(20)
        self.assertEqual(outcome.get("code"), 0)
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

    def test_three_prompts_back_to_back_clear_in_seconds(self):
        # Real pacing (default constants): username, password, passphrase in a row
        # must all be skipped quickly, not one per COOLDOWN_SEC of 10.
        forwarder = self.forwarder()
        del forwarder.POLL_SEC, forwarder.STALL_SEC, forwarder.COOLDOWN_SEC   # class defaults
        process = self.child(
            "import sys\n"
            "for prompt in ('Enter Your Username: ', 'Enter password for https://git.corp/x.git:',\n"
            "                'Enter passphrase for key (enter to skip):'):\n"
            "    sys.stdout.write(prompt); sys.stdout.flush(); sys.stdin.readline()\n"
            "sys.stdout.write('ALL CLEAR\\n'); sys.stdout.flush()",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        started = time.monotonic()
        self.assertEqual(forwarder.wait(process, time.monotonic() + 30), 0)
        elapsed = time.monotonic() - started
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("ALL CLEAR", text)
        self.assertEqual(text.count("Enter sent automatically"), 3)
        self.assertLess(elapsed, 12)   # ~3 x (startup + STALL 1.5s + COOLDOWN 2s), not ~35s

    def test_credsupport_chain_clears_username_and_both_getpass_prompts(self):
        # The real builda.bat credential flow (credSupport.GetCredFileForUser):
        # input() reads the stdin pipe, but getpass reads raw console keys whose
        # prompt never reaches the log. The dual-channel send must clear all
        # three; the log tail sits on the Username line the whole time.
        forwarder = self.forwarder()
        del forwarder.POLL_SEC, forwarder.STALL_SEC, forwarder.COOLDOWN_SEC   # class defaults
        process = self.child(
            "import getpass\n"
            "u = input('Enter Your Username: ')\n"
            "pw = getpass.getpass('Enter password for https://git.corp/x.git:')\n"
            "ph = getpass.getpass('Enter passphrase for key (enter to skip):')\n"
            "print('CHAIN DONE user=%r pw_len=%d ph_len=%d' % (u, len(pw), len(ph)))",
            forwarder.log,
        )
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        started = time.monotonic()
        self.assertEqual(forwarder.wait(process, time.monotonic() + 40), 0)
        elapsed = time.monotonic() - started
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("CHAIN DONE user='' pw_len=0 ph_len=0", text)   # all three skipped empty
        sent = text.count("Enter sent automatically")
        self.assertIn(sent, (2, 3))   # one per channel race at worst; never the MAX_AUTO give-up
        self.assertLess(elapsed, 20)


if __name__ == "__main__":
    unittest.main()
