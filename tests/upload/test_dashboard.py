import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests import Request, Session

from lma_tools.upload import dashboard, launcher
from lma_tools.upload.progress import (
    OrderedProgress,
    SharedColumns,
    watch_transfer,
)


class DashboardTests(unittest.TestCase):
    def test_derive_failure_keeps_completed_upload_in_total(self):
        job = self.job(1, "derive failed")
        job.update(sent=100, confirmed=100, completed=5, rate=0)
        total = dashboard.total_progress([job])
        self.assertEqual(total["sent"], 100)
        self.assertEqual(total["completed"], 5)
        self.assertEqual(total["status"], "derive failed")

    def job(self, started, status="uploading", heartbeat=100):
        return {
            "started": started,
            "heartbeat": heartbeat,
            "identifier": f"artist-{started}",
            "status": status,
            "total": 100,
            "sent": 40,
            "confirmed": 20,
            "files": 5,
            "completed": 1,
            "rate": 10,
        }

    def test_total_sums_active_work_and_successes_but_not_stopped_runs(self):
        jobs = [self.job(1), self.job(2, "checking"), self.job(3, "done")]
        jobs[2].update(sent=100, confirmed=100, completed=5, rate=999)
        jobs += [
            self.job(4, state) for state in ("failed", "cancelled", "stale")
        ]
        total = dashboard.total_progress(jobs)
        self.assertEqual(total["total"], 300)
        self.assertEqual(total["sent"], 180)
        self.assertEqual(total["confirmed"], 140)
        self.assertEqual(total["files"], 15)
        self.assertEqual(total["completed"], 7)
        self.assertEqual(total["rate"], 20)
        self.assertEqual(total["status"], "2 running")

    def test_rate_smoothing_uses_new_heartbeats_and_includes_idle_time(self):
        history = dashboard.DashboardJobs(opened=100)
        job = self.job(1)
        self.assertEqual(history.update([job], now=100)[0]["rate"], 10)
        job.update(heartbeat=130, rate=0)
        rate = history.update([job], now=130)[0]["rate"]
        self.assertAlmostEqual(rate, 10 / 2.718281828, places=6)
        self.assertEqual(history.update([job], now=131)[0]["rate"], rate)
        job.update(heartbeat=160, rate=20)
        self.assertGreater(history.update([job], now=160)[0]["rate"], rate)
        job.update(heartbeat=161, status="done")
        self.assertEqual(history.update([job], now=161)[0]["rate"], 0)

    def test_elapsed_freezes_for_finished_or_stale_jobs(self):
        job = self.job(10, heartbeat=40)
        self.assertEqual(dashboard.elapsed_time([job], now=100), 90)
        job["status"] = "done"
        self.assertEqual(dashboard.elapsed_time([job], now=100), 30)
        job["status"] = "stale"
        self.assertEqual(dashboard.elapsed_time([job], now=200), 30)
        self.assertEqual(dashboard.elapsed_time([], now=200), 0)

    def test_compact_status_keeps_failures_visible(self):
        job = self.job(1)
        self.assertEqual(dashboard.status_text(job), "1/5 completed")
        job["status"] = "deriving"
        self.assertEqual(dashboard.status_text(job), "1/5 completed, derive")
        job["status"] = "failed"
        self.assertEqual(dashboard.status_text(job), "1/5 completed, failed")

    def test_dashboard_timing_uses_run_start_and_rate_not_skipped_bytes(self):
        with OrderedProgress(total=100, file=io.StringIO()) as bar:
            bar.n = 60
            bar._display_elapsed = 120
            bar._ema_dn = lambda: 10
            bar._ema_dt = lambda: 1
            columns = SharedColumns()
            columns.prepare([bar])
            tail = bar.format_dict["column_tail"]
            self.assertIn("02:00<00:04", tail)
            bar._ema_dn = lambda: 0
            self.assertIn("02:00<", bar.format_dict["column_tail"])
            self.assertIn("?", bar.format_dict["column_tail"])
            bar.n = 100
            self.assertIn("02:00<00:00", bar.format_dict["column_tail"])

    def test_dashboard_renders_total_and_show_with_elapsed_remaining(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        job = self.job(time.time() - 120, heartbeat=time.time())

        def wide_bar(**kwargs):
            kwargs.update(dynamic_ncols=False, ncols=180)
            return OrderedProgress(**kwargs)

        with (
            patch.object(dashboard, "OrderedProgress", side_effect=wide_bar),
            patch.object(dashboard.sys, "stderr", stream),
            patch.object(dashboard, "read_jobs", return_value=[job]),
            patch.object(
                dashboard.time, "sleep", side_effect=KeyboardInterrupt
            ),
        ):
            dashboard.dashboard(Path("unused"))
        output = stream.getvalue()
        self.assertIn("TOTAL", output)
        self.assertIn("-" * 179, output)
        self.assertIn("\x1b[?25h", output)
        self.assertIn("<", output)
        self.assertNotIn("uploading", output)

    def test_finishing_a_run_does_not_remove_it_from_the_total(self):
        jobs = [self.job(1), self.job(2)]
        before = dashboard.total_progress(jobs)
        jobs[0].update(status="done", sent=100, confirmed=100, completed=5)
        after = dashboard.total_progress(jobs)
        self.assertEqual(before["total"], after["total"])
        self.assertGreater(after["sent"], before["sent"])
        jobs[1].update(status="done", sent=100, confirmed=100, completed=5)
        total = dashboard.total_progress(jobs)
        self.assertEqual(total["sent"], total["total"])
        self.assertEqual(total["completed"], total["files"])
        self.assertEqual(total["status"], "done")
        self.assertEqual(total["rate"], 0)
        self.assertEqual(dashboard.total_progress([])["status"], "idle")

    def test_all_observed_completions_survive_snapshot_expiry_until_close(
        self,
    ):
        history = dashboard.DashboardJobs(opened=100)
        jobs = [self.job(n, "done", heartbeat=101) for n in range(12)]
        self.assertEqual(len(history.update(jobs, now=102)), 12)
        self.assertEqual(len(history.update([], now=100000)), 12)
        reopened = dashboard.DashboardJobs(opened=103)
        self.assertEqual(reopened.update(jobs, now=104), [])

    def test_running_job_before_open_is_kept_when_it_finishes(self):
        history = dashboard.DashboardJobs(opened=100)
        job = self.job(1, heartbeat=99)
        self.assertEqual(len(history.update([job], now=100)), 1)
        job.update(status="done", heartbeat=101)
        jobs = history.update([job], now=102)
        self.assertEqual(jobs[0]["status"], "done")
        self.assertEqual(len(history.update([], now=100000)), 1)

    def test_disappearing_running_snapshot_becomes_stale(self):
        history = dashboard.DashboardJobs(opened=100)
        history.update([self.job(1)], now=100)
        jobs = history.update([], now=111)
        self.assertEqual(jobs[0]["status"], "stale")
        self.assertEqual(jobs[0]["rate"], 0)
        self.assertEqual(dashboard.total_progress(jobs)["files"], 0)

    def test_retries_reset_inflight_and_only_acknowledgements_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "one.flac", root / "two.flac"
            first.write_bytes(b"a" * 10)
            second.write_bytes(b"b" * 20)
            tracker = dashboard.ShowProgress(
                "artist2026-08-17", [first, second], "show", root / "state"
            )
            try:
                tracker.begin(first.name)
                tracker.advance(first.name, 10)
                tracker.publish()
                data = json.loads(tracker.path.read_text())
                self.assertEqual((data["sent"], data["confirmed"]), (10, 0))
                tracker.begin(first.name)
                tracker.advance(first.name, 4)
                tracker.complete(second.name)  # A checksum-verified skip.
                tracker.publish()
                data = json.loads(tracker.path.read_text())
                self.assertEqual(data["sent"], 24)
                self.assertEqual(data["confirmed"], 20)
                self.assertEqual(tracker.wire, 14)
                self.assertEqual(tracker.path.stat().st_mode & 0o777, 0o600)
            finally:
                tracker.close("cancelled")
            data = json.loads(tracker.path.read_text())
            self.assertEqual(data["sent"], 20)
            self.assertEqual(data["status"], "cancelled")
            self.assertFalse(tracker.thread.is_alive())

    def test_resumed_parts_credit_bytes_without_counting_wire_or_whole_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "one.flac"
            file.write_bytes(b"x" * 100)
            tracker = dashboard.ShowProgress(
                "artist2026-08-17", [file], "show", root / "state"
            )
            try:
                tracker.resume(file.name, 40)
                tracker.advance(file.name, 10)
                tracker.publish()
                self.assertEqual(tracker.data["sent"], 50)
                self.assertEqual(tracker.data["confirmed"], 0)
                self.assertEqual(tracker.data["completed"], 0)
                self.assertEqual(tracker.wire, 10)
            finally:
                tracker.close("cancelled")
            self.assertEqual(tracker.data["sent"], 40)

    def test_file_counter_counts_all_flacs_but_never_small_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = [
                root / name
                for name in (
                    "notes.txt",
                    "cover.jpg",
                    "checks.ffp",
                    "one.flac",
                    "two.FLAC",
                )
            ]
            for path in files:
                path.write_bytes(b"x" * 10)
            tracker = dashboard.ShowProgress(
                "artist2026-08-17", files, "show", root / "state"
            )
            try:
                for path in files[:3]:
                    tracker.complete(path.name)
                tracker.complete(
                    "one.flac"
                )  # Previously uploaded and skipped.
                tracker.publish()
                self.assertEqual(tracker.data["files"], 2)
                self.assertEqual(tracker.data["completed"], 1)
                self.assertEqual(tracker.data["total"], 50)
                self.assertEqual(tracker.data["sent"], 40)
            finally:
                tracker.close("done")

    def test_stale_jobs_are_labelled_and_old_snapshots_expire(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {
                "heartbeat": time.time() - 20,
                "started": time.time() - 30,
                "status": "uploading",
                "rate": 42,
                "identifier": "artist2026-08-17",
                "total": 100,
                "sent": 20,
                "confirmed": 0,
                "completed": 0,
                "files": 1,
            }
            (root / "job.json").write_text(json.dumps(data))
            (root / "broken.json").write_text("{")
            jobs = dashboard.read_jobs(root)
            self.assertEqual(jobs[0]["status"], "stale")
            self.assertEqual(jobs[0]["rate"], 0)
            self.assertEqual(
                dashboard.read_jobs(root, now=time.time() + 86401), []
            )
            self.assertFalse((root / "job.json").exists())

    def test_sequential_observer_counts_transport_and_restores_session(self):
        tracker = Mock()
        session = Session()
        body = io.BytesIO(b"payload")
        request = Request(
            "PUT", "https://s3.us.archive.org/item/file", data=body
        ).prepare()
        body.read()  # A prior local checksum pass must not be counted.
        body.seek(0)

        def transport(prepared, **kwargs):
            self.assertEqual(prepared.body.read(), b"payload")
            return Mock()

        session.send = transport
        with watch_transfer(session, Path("one.flac"), tracker):
            session.send(request)
        self.assertIs(session.send, transport)
        self.assertIs(request.body, body)
        tracker.begin.assert_called_once_with("one.flac")
        tracker.advance.assert_called_once_with("one.flac", 7)

    def test_dashboard_creation_is_detached_and_existing_session_untouched(
        self,
    ):
        with patch.object(launcher.subprocess, "run") as run:
            run.return_value = Mock(returncode=0)
            launcher.ensure_dashboard()
            self.assertEqual(run.call_count, 1)
            run.reset_mock()
            run.side_effect = [Mock(returncode=1), Mock(returncode=0)]
            launcher.ensure_dashboard()
            command = run.call_args.args[0]
            self.assertEqual(
                command[:5], ["tmux", "new-session", "-d", "-s", "lmau"]
            )
            self.assertIn("lma_tools.upload.dashboard", command[-1])

    def test_session_name_starts_with_show(self):
        name = launcher.cli.tmux_session_name("nephrok2026-08-17.t-flac2448")
        self.assertTrue(name.startswith("nephrok2026-08-17_"))
        self.assertNotIn("lma_", name)
