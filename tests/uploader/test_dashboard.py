import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests import Request, Session

from lma_tools.uploader import dashboard, launcher
from lma_tools.uploader.progress import watch_transfer


class DashboardTests(unittest.TestCase):
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
            self.assertIn("lma_tools.uploader.dashboard", command[-1])

    def test_session_name_starts_with_show(self):
        name = launcher.cli.tmux_session_name("nephrok2026-08-17.t-flac2448")
        self.assertTrue(name.startswith("nephrok2026-08-17_"))
        self.assertNotIn("lma_", name)
