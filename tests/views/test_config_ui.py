"""Configuration and user-facing behavior regressions."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.views.client import ArchiveError
from lma_tools.views.config import Config, load_config
from lma_tools.views.fields import field_name, query_text, quoted_clause
from lma_tools.views.service import SearchService
from lma_tools.views.ui import interactive


class ConfigTests(unittest.TestCase):
    def config(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(text)
            return load_config(path)

    def test_valid(self):
        config = self.config('query = ["collection:demo"]\nsort = "v"')
        self.assertEqual(config.query, ("collection:demo",))
        self.assertEqual(config.sort, "venue")

    def test_bad_types_and_keys(self):
        for text in (
            'query = "x"',
            "query = [1]",
            "timeout = 0",
            "timeout = nan",
            "timeout = true",
            "reverse = 1",
            'sort = "unknown"',
            "mystery = 1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config(text)

    def test_view_batch_setting_and_validation(self):
        self.assertEqual(self.config("").view_batch_size, 100)
        self.assertEqual(
            self.config("view_batch_size = 20").view_batch_size, 20
        )
        for value in ("0", "-1", "true", "1.5", "'\"20\"'"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config("view_batch_size = " + value)

    def test_missing_explicit_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_config(Path(tmp) / "missing.toml")

    def test_queries_and_escaping(self):
        self.assertEqual(query_text(["a OR b", "c"]), "(a OR b) AND (c)")
        self.assertEqual(quoted_clause("venue", 'a"b'), 'venue:"a\\"b"')
        self.assertEqual(field_name("u"), "uploader")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.search.return_value = [
            {"identifier": "item", "collection": ["etree", "fav-demo"]}
        ]
        self.client.view_counts.return_value = [("item", 5)]
        self.service = SearchService(self.client)

    def report(self, refresh=False, clauses=None):
        return self.service.report(
            clauses or ["x"], Config(), "views", False, refresh
        )

    def test_sort_reuses_cache(self):
        self.report()
        self.report()
        self.client.search.assert_called_once()
        self.client.view_counts.assert_called_once()

    def test_changed_query_reloads(self):
        self.report()
        self.report(clauses=["y"])
        self.assertEqual(self.client.search.call_count, 2)
        self.client.view_counts.assert_called_once()

    def test_refresh_reloads_all(self):
        self.report()
        self.report(refresh=True)
        self.assertEqual(self.client.search.call_count, 2)
        self.assertEqual(self.client.view_counts.call_count, 2)

    def test_failed_count_unknown_and_retried(self):
        self.client.view_counts.side_effect = [
            [("item", ArchiveError("oops"))],
            [("item", 7)],
        ]
        rows, summary = self.report()
        self.assertIsNone(rows[0].views)
        self.assertEqual(summary["unknown_views"], 1)
        self.assertEqual(
            self.service.warnings, ["1 recording(s): oops (item)"]
        )
        self.assertEqual(self.report()[0][0].views, 7)
        self.assertEqual(self.service.warnings, [])

    def test_favorites_do_not_mutate_query(self):
        clauses = ["x"]
        self.assertEqual(self.service.favorites(clauses), [("item", ["demo"])])
        self.assertEqual(clauses, ["x"])
        self.report()
        self.assertEqual(self.client.search.call_count, 2)

    def test_list_values_uses_every_value(self):
        self.assertEqual(
            self.service.list_values(["x"], "collection"),
            ["etree", "fav-demo"],
        )

    def test_private_metadata_fallback_cached(self):
        self.client.metadata.return_value = {"uploader": "public-value"}
        self.assertEqual(
            self.service.list_values(["x"], "uploader"), ["public-value"]
        )
        self.service.list_values(["x"], "uploader")
        self.client.metadata.assert_called_once()

    def test_clear_last_clause_and_bad_command(self):
        clauses = ["x"]
        with patch("builtins.input", side_effect=["x", "sbad", "q"]):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    interactive(
                        self.service, Config(), clauses, "views", False
                    ),
                    0,
                )
        self.assertEqual(clauses, [])
        self.assertIn("Not found in tag dictionary.", output.getvalue())

    def test_eof_quits(self):
        with patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    interactive(self.service, Config(), [], "views", False), 0
                )

    def test_returning_from_favorites_reuses_search(self):
        self.report()
        self.service.favorites(["x"])
        self.report()
        self.client.search.assert_called_once()
        self.client.view_counts.assert_called_once()

    def test_backtracking_reuses_query(self):
        self.report(clauses=["x"])
        self.report(clauses=["x", "y"])
        self.report(clauses=["x"])
        self.assertEqual(self.client.search.call_count, 2)

    def test_query_cache_eviction(self):
        for index in range(9):
            self.service.search([str(index)])
        self.assertEqual(len(self.service.search_cache), 8)
        self.service.search(["0"])
        self.assertEqual(self.client.search.call_count, 10)

    def test_all_missing_counts_submitted_together(self):
        self.client.search.return_value = [
            {"identifier": "one"},
            {"identifier": "two"},
        ]
        self.client.view_counts.return_value = [("one", 10), ("two", 20)]
        _, stats = self.report()
        self.client.view_counts.assert_called_once_with(["one", "two"])
        self.assertEqual(stats["views"], 30)

    def test_cached_favorites_excludes_items_without_membership(self):
        self.client.search.return_value = [
            {"identifier": "a", "collection": ["etree"]},
            {"identifier": "b", "collection": ["etree", "fav-demo"]},
        ]
        self.service.search(["x"])
        self.assertEqual(self.service.favorites(["x"]), [("b", ["demo"])])
        self.client.search.assert_called_once()

    def test_shared_batch_failure_has_one_warning(self):
        names = [f"demo{i}" for i in range(50)]
        self.client.search.return_value = [
            {"identifier": name} for name in names
        ]
        error = ArchiveError("HTTP 502 from Archive", 502)
        self.client.view_counts.return_value = [
            (name, error) for name in names
        ]
        rows, stats = self.report()
        self.assertEqual(len(self.service.warnings), 1)
        self.assertIn("50 recording(s)", self.service.warnings[0])
        self.assertLess(len(self.service.warnings[0]), 120)
        self.assertEqual(stats["unknown_views"], 50)
        self.assertTrue(all(row.views is None for row in rows))
