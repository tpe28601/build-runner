"""Job folder cleanup semantics: whole-folder deletion, retention, orphan sweep."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from build_runner import history, jobs


def make_job(data, job_id, state="failed", *, mtime=None):
    folder = data / "jobs" / job_id
    folder.mkdir(parents=True)
    row = {"id": job_id, "state": state, "workspace": "A", "workspaceId": "a",
           "queuedAt": "2026-11-01T00:00:00+00:00"}
    (folder / "status.json").write_text(json.dumps(row), encoding="utf-8")
    (folder / "build.log").write_text("output\n", encoding="utf-8")
    if mtime is not None:
        for item in folder.rglob("*"):
            if item.is_file():
                os.utime(item, (mtime, mtime))
    return row


class CleanupTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.data = Path(storage.name)

    def test_remove_job_dir_deletes_folder_and_reports_missing(self):
        row = make_job(self.data, "b" * 32)
        self.assertTrue(jobs.remove_job_dir(self.data, row["id"]))
        self.assertFalse((self.data / "jobs" / row["id"]).exists())
        self.assertFalse(jobs.remove_job_dir(self.data, row["id"]))

    def test_remove_job_dir_rejects_ids_outside_jobs_root(self):
        with self.assertRaises(jobs.ToolError):
            jobs.remove_job_dir(self.data, ".." + "x" * 30)

    def test_prune_returns_pruned_ids(self):
        rows = [make_job(self.data, f"{index:032x}") for index in (1, 2, 3)]
        for row in rows:
            history.append(self.data, {**row, "endedAt": "2026-11-01T00:00:00+00:00"})
        self.assertEqual(history.prune(self.data, {"count": 1}), [rows[0]["id"], rows[1]["id"]])
        self.assertEqual(len(history.list_history(self.data)), 1)

    def test_prune_without_rules_keeps_everything(self):
        make_job(self.data, "a" * 32)
        self.assertEqual(history.prune(self.data, {}), [])

    def test_sweep_orphans_keeps_active_known_and_recent(self):
        old = time.time() - 48 * 3600
        gone = make_job(self.data, "a" * 32, mtime=old)
        running = make_job(self.data, "b" * 32, "running", mtime=old)
        recent = make_job(self.data, "c" * 32, mtime=time.time())
        listed = make_job(self.data, "d" * 32, mtime=old)
        history.append(self.data, {**listed, "endedAt": "2026-10-01T00:00:00+00:00"})
        self.assertEqual(jobs.sweep_orphans(self.data), [gone["id"]])
        for keep in (running, recent, listed):
            self.assertTrue((self.data / "jobs" / keep["id"]).exists())


if __name__ == "__main__":
    unittest.main()
