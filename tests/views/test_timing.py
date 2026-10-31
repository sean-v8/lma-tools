"""In-memory diagnostics and opt-in timing placement in terminal reports."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import MaxRetryError
from urllib3.response import HTTPResponse

from lma_tools.views.client import ArchiveClient
from lma_tools.views.config import Config
from lma_tools.views.report import render
from lma_tools.views.service import SearchService
from lma_tools.views.timing import TimingRecorder, TimingRetry
from lma_tools.views.ui import HELP, interactive


class TimingTests(unittest.TestCase):
    def response(self, data, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = data
        return response

    def test_disabled_recorder_keeps_no_events_or_display(self):
        timing = TimingRecorder()
        with timing.span("test"):
            timing.write("event")
        self.assertEqual(timing.events, [])
        self.assertEqual(timing.render(), "")

    def test_span_success_failure_and_interrupt(self):
        timing = TimingRecorder(True)
        with timing.span("success"):
            pass
        for error in (ValueError("secret query"), KeyboardInterrupt()):
            with self.assertRaises(type(error)):
                with timing.span("failure"):
                    raise error
        self.assertEqual(
            [e["outcome"] for e in timing.events],
            ["ok", "ValueError", "KeyboardInterrupt"],
        )
        self.assertTrue(all(e["seconds"] >= 0 for e in timing.events))
        self.assertNotIn("secret query", str(timing.events))

    def test_views_record_count_not_identifiers(self):
        timing = TimingRecorder(True)
        session = Mock()
        session.headers = {}
        session.get.return_value = self.response(
            {"private-name": {"all_time": 42}, "other": {"all_time": 1}}
        )
        client = ArchiveClient(session=session, timing=timing)
        self.assertEqual(
            dict(client.view_counts(["private-name", "other"])),
            {"private-name": 42, "other": 1},
        )
        event = next(e for e in timing.events if e["event"] == "request")
        self.assertEqual(
            (event["kind"], event["items"], event["status"]), ("views", 2, 200)
        )
        self.assertNotIn("private-name", str(timing.events))

    def test_retry_clones_keep_recorder_and_record_exhaustion(self):
        timing = TimingRecorder(True)
        retry = TimingRetry(total=1, backoff_factor=0, timing=timing)
        response = HTTPResponse(status=502)
        next_retry = retry.increment("GET", "/secret", response=response)
        self.assertIs(next_retry.timing, timing)
        next_retry.sleep()
        with self.assertRaises(MaxRetryError):
            next_retry.increment("GET", "/secret", response=response)
        names = [e["event"] for e in timing.events]
        self.assertIn("retry_scheduled", names)
        self.assertIn("retry_wait", names)
        self.assertIn("retry_exhausted", names)
        self.assertNotIn("secret", str(timing.events))

    def service(self):
        client = Mock()
        client.search.return_value = [{"identifier": "demo"}]
        client.view_counts.return_value = [("demo", 10)]
        timing = TimingRecorder(True)
        return SearchService(client, timing=timing)

    def test_report_resets_measurements_and_shows_cache(self):
        service = self.service()
        for _ in range(2):
            service.report(["uploader:private"], Config(), "views", False)
        events = service.timing.events
        self.assertEqual(len([e for e in events if e["event"] == "report"]), 1)
        self.assertEqual(len([e for e in events if e["event"] == "search"]), 1)
        self.assertIn("cached", service.timing.render())
        self.assertNotIn("private", str(events))

    def test_recovery_records_failure_reduction_and_success(self):
        timing = TimingRecorder(True)
        session = Mock()
        session.headers = {}
        failed = self.response({}, 502)
        failed.raise_for_status.side_effect = requests.HTTPError(
            "private URL", response=failed
        )
        session.get.side_effect = [
            failed,
            self.response({"a": {"all_time": 1}}),
            self.response({"b": {"all_time": 2}}),
        ]
        client = ArchiveClient(session=session, timing=timing)
        self.assertEqual(
            dict(client.view_counts(["a", "b"])), {"a": 1, "b": 2}
        )
        events = timing.events
        network = [e for e in events if e["event"] == "request"]
        self.assertEqual([e["status"] for e in network], [502, 200, 200])
        self.assertEqual([e["items"] for e in network], [2, 1, 1])
        self.assertIn("HTTP 502; batch size 1", timing.render())
        self.assertNotIn("private URL", str(events))

    def test_debug_toggle_and_block_position(self):
        service = self.service()
        service.timing.enabled = False
        self.assertIn(
            "    trace = set trace\n    debug = toggle debugging\n", HELP
        )
        with patch("builtins.input", side_effect=["debug", "", "q"]):
            with redirect_stdout(io.StringIO()) as output:
                interactive(
                    service, Config(), ["collection:demo"], "views", False
                )
        text = output.getvalue()
        self.assertLess(
            text.index("         Daily:"), text.index("        Search:")
        )
        self.assertLess(text.index("         Total:"), text.index("********"))
        self.assertTrue(service.timing.enabled)
        with patch("builtins.input", side_effect=["debug", "", "q"]):
            with redirect_stdout(io.StringIO()) as output:
                interactive(
                    service, Config(), ["collection:demo"], "views", False
                )
        self.assertNotIn("         Total:", output.getvalue())
        self.assertFalse(service.timing.enabled)

    def test_normal_report_unchanged(self):
        service = self.service()
        rows, summary = service.report(["x"], Config(), "views", False)
        plain = render(rows, summary, "views")
        self.assertEqual(render(rows, summary, "views", ""), plain)
        self.assertNotIn("Search:", plain)

    def test_shared_recorder_inherited_from_real_client(self):
        client = ArchiveClient()
        try:
            service = SearchService(client)
            self.assertIs(service.timing, client.timing)
        finally:
            client.close()
