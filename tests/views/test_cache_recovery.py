"""Keep complete cached results and terminal state after search failures."""

import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.views.client import ArchiveError
from lma_tools.views.config import Config
from lma_tools.views.service import SearchService
from lma_tools.views.ui import interactive
from tests.views.test_terminal import TtyBuffer


class CacheRecoveryTests(unittest.TestCase):
    def test_failed_search_retains_cache_and_restores_terminal(self):
        def partial():
            yield {"identifier": "partial"}
            raise ArchiveError("Invalid Archive search response")

        for failure in (
            ArchiveError("Invalid Archive search response"),
            partial(),
        ):
            with self.subTest(failure=type(failure).__name__):
                client = Mock()
                client.search.side_effect = [[{"identifier": "good"}], failure]
                client.view_counts.return_value = [("good", 10)]
                stream = TtyBuffer()
                commands = ["", "", "p", "cache", "p", "", "q"]
                with (
                    patch.dict("os.environ", TERM="xterm"),
                    patch("builtins.input", side_effect=commands),
                    redirect_stdout(stream),
                ):
                    interactive(
                        SearchService(client), Config(), ["x"], "views", False
                    )
                text = stream.getvalue()
                error = text.index("Error: Invalid Archive search response")
                self.assertIn("cache:False", text[error:])
                self.assertIn("cache:True", text[error:])
                self.assertEqual(client.search.call_count, 2)
                self.assertEqual(text.count("\033[?1049h"), 3)
                self.assertEqual(text.count("\033[?1049l"), 3)
                self.assertNotIn("\033[1A", text)
