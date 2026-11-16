"""Terminal links preserve visible report layout and plain-text exports."""

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.report.cli import main
from lma_tools.report.config import Config
from lma_tools.report.report import recordings, render, summarize
from tests.helpers import TtyBuffer

OSC_LINK = re.compile(r"\x1b\]8;;[^\x1b]*\x1b\\")


class ReportHyperlinkTests(unittest.TestCase):
    def setUp(self):
        self.rows = recordings(
            [
                {"identifier": "short", "runtime": "01:00:00"},
                {"identifier": "band2026-01-08.cm4.t-flac2448"},
            ],
            {"short": 12},
        )
        self.summary = summarize(self.rows)

    def test_links_only_identifiers_with_padding_outside_link(self):
        with patch.dict("os.environ", {"TERM": "xterm-256color"}):
            text = render(self.rows, self.summary, "views", stream=TtyBuffer())
        self.assertIn(
            "\033]8;;https://archive.org/details/short\033\\"
            "short\033]8;;\033\\",
            text,
        )
        self.assertEqual(text.count("\033]8;;https://archive.org/details/"), 2)
        self.assertIn("short\033]8;;\033\\ ", text)
        self.assertNotIn("\033", text.split("\n\n", 1)[1])

    def test_visible_output_identical_for_all_report_columns(self):
        with patch.dict("os.environ", {"TERM": "xterm"}):
            for sort in ("views", "daily", "runtime"):
                with self.subTest(sort=sort):
                    plain = render(
                        self.rows,
                        self.summary,
                        sort,
                        "         Total: 0.100s",
                        stream=io.StringIO(),
                    )
                    linked = render(
                        self.rows,
                        self.summary,
                        sort,
                        "         Total: 0.100s",
                        stream=TtyBuffer(),
                    )
                    self.assertEqual(OSC_LINK.sub("", linked), plain)

    def test_redirected_and_dumb_outputs_have_no_escape_codes(self):
        for stream, term in ((io.StringIO(), "xterm"), (TtyBuffer(), "dumb")):
            with self.subTest(term=term):
                with patch.dict("os.environ", {"TERM": term}):
                    text = render(
                        self.rows, self.summary, "views", stream=stream
                    )
                self.assertNotIn("\033", text)

    def test_default_stream_follows_redirected_stdout(self):
        with patch.dict("os.environ", {"TERM": "xterm"}):
            with redirect_stdout(io.StringIO()):
                plain = render(self.rows, self.summary, "views")
            with redirect_stdout(TtyBuffer()):
                linked = render(self.rows, self.summary, "views")
        self.assertNotIn("\033", plain)
        self.assertIn("https://archive.org/details/short", linked)

    def test_url_path_encoded_and_control_characters_not_linked(self):
        rows = recordings([{"identifier": "show/name?#"}], {})
        with patch.dict("os.environ", {"TERM": "xterm"}):
            text = render(rows, summarize(rows), "views", stream=TtyBuffer())
            unsafe = recordings([{"identifier": "bad\x07label"}], {})
            unlinked = render(
                unsafe, summarize(unsafe), "views", stream=TtyBuffer()
            )
        self.assertIn("https://archive.org/details/show%2Fname%3F%23", text)
        self.assertNotIn("\033]8", unlinked)

    def test_disabled_preference_keeps_tty_output_plain(self):
        with patch.dict("os.environ", {"TERM": "xterm"}):
            text = render(
                self.rows,
                self.summary,
                "views",
                hyperlinks=False,
                stream=TtyBuffer(),
            )
        self.assertNotIn("\033", text)
        self.assertIn("band2026-01-08.cm4.t-flac2448", text)

    def test_config_applies_to_once_and_interactive_reports(self):
        for enabled in (False, True):
            for once in (False, True):
                with self.subTest(enabled=enabled, once=once):
                    client = Mock()
                    client.search.return_value = [{"identifier": "demo"}]
                    client.view_counts.return_value = [("demo", 10)]
                    output = TtyBuffer()
                    with (
                        patch.dict("os.environ", {"TERM": "xterm"}),
                        patch(
                            "lma_tools.report.cli.ArchiveClient",
                            return_value=client,
                        ),
                        patch(
                            "lma_tools.report.cli.load_config",
                            return_value=Config(hyperlinks=enabled),
                        ),
                        patch("builtins.input", side_effect=["", "q"]),
                        redirect_stdout(output),
                    ):
                        self.assertEqual(main(["--once"] if once else []), 0)
                    self.assertEqual("\033]8;;" in output.getvalue(), enabled)
