"""Terminal screen restoration and final-output placement."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.views.config import Config
from lma_tools.views.service import SearchService
from lma_tools.views.terminal import FetchDisplay
from lma_tools.views.ui import HELP, interactive


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class TerminalTests(unittest.TestCase):
    def test_restores_screen_after_exception_and_interrupt(self):
        for error in (ValueError("failed"), KeyboardInterrupt()):
            stream = TtyBuffer()
            with patch.dict("os.environ", {"TERM": "xterm"}):
                with self.assertRaises(type(error)):
                    with FetchDisplay(stream) as display:
                        display.progress("demo", 3, 1, 1)
                        raise error
            self.assertTrue(stream.getvalue().startswith("\033[?1049h"))
            self.assertTrue(stream.getvalue().endswith("\033[?1049l"))
            self.assertIn("demo", stream.getvalue())

    def test_redirected_and_dumb_output_have_no_escapes(self):
        for stream, term in ((io.StringIO(), "xterm"), (TtyBuffer(), "dumb")):
            with patch.dict("os.environ", {"TERM": term}):
                with FetchDisplay(stream) as display:
                    display.progress("demo", 3, 1, 1)
            self.assertEqual(stream.getvalue(), "")

    def test_final_sort_prints_after_screen_restore(self):
        client = Mock()
        client.search.return_value = [{"identifier": "a"}, {"identifier": "b"}]
        client.view_counts.return_value = [("a", 1), ("b", 20)]
        stream = TtyBuffer()
        with patch.dict("os.environ", {"TERM": "xterm"}):
            with patch("builtins.input", side_effect=["", "q"]):
                with redirect_stdout(stream):
                    interactive(
                        SearchService(client),
                        Config(),
                        ["collection:demo"],
                        "views",
                        True,
                    )
        output = stream.getvalue()
        temporary = output.split("\033[?1049h", 1)[1].split("\033[?1049l")[0]
        final = output.split("\033[?1049l", 1)[1]
        self.assertLess(temporary.index("a "), temporary.index("b "))
        self.assertLess(final.index("b "), final.index("a "))
        self.assertIn("Tapes", final)
        self.assertNotIn("\033[1A", final)

    def test_help_uses_one_command_per_line(self):
        self.assertIn("        q = quit\n", HELP)
        self.assertIn("\n\n        f = print favorites", HELP)
        self.assertNotIn("q = quit   ", HELP)

    def test_default_includes_both_collections(self):
        self.assertEqual(
            Config().query, ("collection:(etree OR taperssection)",)
        )
