"""Derive completion survives dashboard restarts without rewriting uploads."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lma_tools.upload import dashboard, tasks
from lma_tools.upload.progress import UploadProgress


class DashboardCompletionTests(unittest.TestCase):
    def job(self):
        return dict(
            identifier="artist2026-08-17",
            started=100,
            heartbeat=500,
            status="deriving",
            rate=123,
            transfer_finished=True,
            total=100,
            sent=100,
            confirmed=100,
            files=1,
            completed=1,
        )

    def test_completion_freezes_and_is_hidden_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "run.json"
            original = json.dumps(self.job())
            snapshot.write_text(original)
            finished = 100 + 25 * 3600 + 605
            with patch.object(tasks.Thread, "start"):
                monitor = tasks.TaskMonitor()
            history = dashboard.DashboardJobs(opened=finished - 100)
            rows = dashboard.read_jobs(root, now=finished - 100)
            self.assertEqual(len(history.update(rows)), 1)
            key = ("artist2026-08-17", 100)
            monitor.states[key] = "verified"
            monitor.finished[key] = finished
            rows = monitor.update(rows)
            retained = history.update(rows, now=finished + 50)
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0]["rate"], 123)
            self.assertEqual(
                UploadProgress.format_interval(
                    dashboard.elapsed_time(retained, now=finished + 50)
                ),
                "25:10:05",
            )
            self.assertEqual(snapshot.read_text(), original)
            restarted = dashboard.DashboardJobs(opened=finished + 100)
            rows = dashboard.read_jobs(root, now=finished + 100)
            self.assertEqual(rows[0]["status"], "verified")
            self.assertEqual(rows[0]["heartbeat"], finished)
            self.assertEqual(restarted.update(rows), [])
            with patch.object(tasks.Thread, "start"):
                fresh_monitor = tasks.TaskMonitor()
            fresh_monitor.update(rows)
            self.assertEqual(fresh_monitor.pending, set())
            self.assertEqual(
                dashboard.read_jobs(root, now=finished + 86401), []
            )
            self.assertFalse(snapshot.exists())
            self.assertFalse(snapshot.with_suffix(".done").exists())

    def test_first_completion_wins_and_other_runs_are_not_matched(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = self.job()
            job["_state_path"] = str(Path(temporary) / "run.json")
            first = tasks.save_completion(job, "verified", 1000)
            later = tasks.save_completion(job, "verified", 2000)
            self.assertEqual(first, later)
            newer = dict(job, started=200)
            self.assertIsNone(tasks.saved_completion(newer))

    def test_corrupt_completion_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = self.job()
            path = Path(temporary) / "run.json"
            job["_state_path"] = str(path)
            for contents in (
                "{",
                "[]",
                json.dumps(
                    dict(
                        identifier=job["identifier"],
                        started=100,
                        status="verified",
                        finished=-1,
                    )
                ),
            ):
                path.with_suffix(".done").write_text(contents)
                self.assertIsNone(tasks.saved_completion(job))
