"""Task polling must not block rendering or confuse separate upload runs."""

import unittest
from unittest.mock import Mock, patch

from lma_tools.upload import dashboard, tasks
from lma_tools.upload.progress import UploadProgress


class TaskMonitorTests(unittest.TestCase):
    def test_poll_results_are_scoped_to_a_run_and_cached(self):
        with patch.object(tasks.Thread, "start"):
            monitor = tasks.TaskMonitor()
        job = dict(identifier="artist2026-08-17", started=1, status="deriving")
        key = (job["identifier"], 1)
        with patch.object(tasks.time, "monotonic", return_value=100):
            monitor.update([job])
            self.assertEqual(monitor.pending, {key})
            monitor.pending.clear()
            monitor.checked[key] = 100
            monitor.states[key] = "verified"
            self.assertEqual(monitor.update([job])[0]["status"], "verified")
            self.assertFalse(monitor.pending)
            newer = dict(job, started=2, status="deriving")
            self.assertEqual(monitor.update([newer])[0]["status"], "deriving")
            self.assertIn((job["identifier"], 2), monitor.pending)

    def test_active_transfer_does_not_trigger_task_requests(self):
        with patch.object(tasks.Thread, "start"):
            monitor = tasks.TaskMonitor()
        job = dict(
            identifier="artist2026-08-17", started=1, status="uploading"
        )
        self.assertEqual(monitor.update([job]), [job])
        self.assertFalse(monitor.pending)

    def test_completion_after_a_day_freezes_at_detection_time(self):
        with patch.object(tasks.Thread, "start"):
            monitor = tasks.TaskMonitor()
        job = dict(
            identifier="artist2026-08-17",
            started=100,
            heartbeat=4305,
            status="deriving",
            rate=0,
        )
        key = (job["identifier"], job["started"])
        finished = 100 + 25 * 3600 + 10 * 60 + 5

        def poll(state, now):
            monitor.pending.clear()
            monitor.pending.add(key)
            monitor.stop = Mock()
            monitor.stop.wait.side_effect = [False, True]
            with (
                patch.object(tasks, "ArchiveSession") as session,
                patch.object(tasks, "task_status", return_value=state),
                patch.object(tasks.time, "time", return_value=now),
            ):
                session.return_value.__enter__.return_value.access_key = "a"
                session.return_value.__enter__.return_value.secret_key = "s"
                monitor._run()

        poll("deriving", finished - 30)
        running = monitor.update([dict(job)])[0]
        self.assertEqual(
            dashboard.elapsed_time([running], now=finished - 30),
            finished - 130,
        )
        poll("verified", finished)
        for now in (finished, finished + 3600):
            poll("verified", now)
            result = monitor.update([dict(job)])[0]
            self.assertEqual(result["heartbeat"], finished)
            elapsed = dashboard.elapsed_time([result], now=now)
            self.assertEqual(
                UploadProgress.format_interval(elapsed), "25:10:05"
            )
        newer = dict(job, started=finished + 1)
        self.assertEqual(
            monitor.update([newer])[0]["heartbeat"], job["heartbeat"]
        )
        poll("deriving", finished + 7200)
        self.assertNotIn(key, monitor.finished)
