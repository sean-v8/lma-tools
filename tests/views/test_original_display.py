"""Literal output expectations for the original interactive presentation."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.views.config import Config
from lma_tools.views.report import Recording, render
from lma_tools.views.service import SearchService
from lma_tools.views.ui import HELP, interactive, print_query


class OriginalDisplayTests(unittest.TestCase):
    def test_original_summary_spacing_precision_and_separator(self):
        rows = [Recording({"identifier": "show-one"}, 562, 0.9, 89919, None)]
        stats = {
            "tapes": 6,
            "performances": 6,
            "shows": 6,
            "dates": 6,
            "views": 2308,
            "average_views": 2308 / 6,
            "average_daily": 0.9,
            "runtime_seconds": 89919,
            "average_runtime_seconds": 89919 / 6,
            "runtime_days": 89919 / 86400,
        }
        self.assertEqual(
            render(rows, stats, "views"),
            "show-one   562\n"
            "\n"
            "         Tapes: 6\n"
            "  Performances: 6\n"
            "         Shows: 6\n"
            "         Dates: 6\n"
            "\n"
            "         Views: 2308\n"
            "       Average: 384.7\n"
            "         Daily: 0.90\n"
            "\n"
            "       Runtime: 24:58:39\n"
            "       Average: 04:09:46\n"
            "          Days:     1.04\n"
            "\n"
            "**************",
        )

    def test_original_query_lines(self):
        with redirect_stdout(io.StringIO()) as output:
            print_query(["collection:demo"], "views", True, True)
        self.assertEqual(
            output.getvalue(), "\nsort:-views\ncache:True\ncollection:demo\n"
        )

    def test_original_help_text(self):
        self.assertEqual(
            HELP,
            "    enter = search\n"
            "        q = quit\n"
            "\n"
            "    cache = toggle cache\n"
            "   reload = reload cache\n"
            "\n"
            "    trace = set trace\n"
            "    debug = toggle debugging\n"
            "\n"
            "        p = print current tags\n"
            "       pp = print all possible tags\n"
            "\n"
            "        f = print favorites\n"
            "\n"
            "        x = clear last tag\n"
            "       xx = clear all tags + results\n"
            "\n"
            "        u = uploader\n"
            "        i = identifier\n"
            "        a = collection\n"
            "        v = venue\n"
            "        c = city\n"
            "        d = date\n"
            "        y = year\n"
            "        n = description\n"
            "\n"
            "       l_ = list matched tags\n"
            "       s_ = sort by tag",
        )

    def test_menu_repeats_after_report_and_cache_toggles(self):
        client = Mock()
        client.search.return_value = [{"identifier": "demo"}]
        client.view_counts.return_value = [("demo", 10)]
        with patch(
            "builtins.input", side_effect=["cache", "p", "", "q"]
        ) as ask:
            with redirect_stdout(io.StringIO()) as output:
                interactive(
                    SearchService(client),
                    Config(),
                    ["collection:demo"],
                    "views",
                    False,
                )
        text = output.getvalue()
        self.assertTrue(text.startswith("\nsort:views\n"))
        self.assertEqual(text.count("    enter = search"), 2)
        self.assertIn("cache:True", text)
        self.assertNotIn("AGPL", text)
        self.assertNotIn("lma-views>", text)
        self.assertTrue(
            all(call.args == ("\n",) for call in ask.call_args_list)
        )
