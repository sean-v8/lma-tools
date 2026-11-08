"""Task polling must not block rendering or confuse separate upload runs."""

import unittest
from unittest.mock import patch

from lma_tools.upload import tasks


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
