"""Regression tests for the original script's failure cases."""

import unittest
from datetime import date

from lma_tools.views.report import (
    duration,
    parse_date,
    recordings,
    render,
    runtime_seconds,
    sorted_recordings,
    summarize,
)


def doc(identifier="band2025-01-01", **kwargs):
    return {
        "identifier": identifier,
        "publicdate": "2025-01-01T12:00:00Z",
        "collection": ["etree", "band"],
        "venue": "Hall",
        **kwargs,
    }


class ReportTests(unittest.TestCase):
    def rows(self, docs, counts):
        return recordings(docs, counts, date(2025, 1, 11))

    def test_same_day_has_one_day_denominator(self):
        rows = recordings([doc()], {"band2025-01-01": 10}, date(2025, 1, 1))
        self.assertEqual(rows[0].daily, 10)

    def test_future_or_missing_upload_date_unknown(self):
        for publicdate in (None, "invalid", "2026-01-01"):
            row = self.rows(
                [doc(publicdate=publicdate)], {"band2025-01-01": 10}
            )[0]
            self.assertIsNone(row.daily)

    def test_unknown_not_zero(self):
        rows = self.rows([doc(), doc("other")], {"other": 0})
        stats = summarize(rows)
        self.assertEqual(stats["unknown_views"], 1)
        self.assertEqual(stats["zero_views"], 1)
        self.assertIsNone(stats["average_views"])

    def test_average_policy_and_daily_denominator(self):
        docs = [doc("one"), doc("two", publicdate=None), doc("zero")]
        rows = self.rows(docs, {"one": 100, "two": 200, "zero": 0})
        self.assertEqual(summarize(rows)["average_views"], 150)
        self.assertEqual(summarize(rows)["average_daily"], 10)
        self.assertEqual(summarize(rows, False)["average_views"], 100)
        self.assertEqual(summarize(rows, False)["average_daily"], 5)

    def test_favorites_and_collections_excluded(self):
        rows = self.rows(
            [
                doc(),
                doc("fav-person"),
                doc("collection", mediatype="collection"),
            ],
            {},
        )
        self.assertEqual(len(rows), 1)

    def test_date_fallback(self):
        self.assertEqual(
            self.rows([doc(date="invalid")], {})[0].date, date(2025, 1, 1)
        )
        self.assertIsNone(parse_date("2025-02-31"))

    def test_runtime_formats(self):
        for text, expected in (
            ("1:02:03", 3723),
            ("72:05", 4325),
            ("00:00:00", 0),
            (["01:00:00"], 3600),
        ):
            with self.subTest(text=text):
                self.assertEqual(runtime_seconds(text), expected)

    def test_bad_runtime_ignored(self):
        for value in (None, "", "words", "1:99:00", "1:02:60", "-1:00"):
            self.assertIsNone(runtime_seconds(value))

    def test_runtime_summary_uses_known_count(self):
        rows = self.rows(
            [
                doc("a", runtime="01:00:00"),
                doc("b", runtime="bad"),
                doc("c", runtime="00:00:00"),
            ],
            {},
        )
        stats = summarize(rows)
        self.assertEqual(stats["runtime_count"], 2)
        self.assertEqual(stats["average_runtime_seconds"], 1800)
        self.assertEqual(duration(90061), "25:01:01")

    def test_numeric_sort_and_unknown_last(self):
        rows = self.rows([doc("a"), doc("b"), doc("c")], {"a": 9, "b": 100})
        for reverse, expected in (
            (False, ["a", "b", "c"]),
            (True, ["b", "a", "c"]),
        ):
            self.assertEqual(
                [
                    r.identifier
                    for r in sorted_recordings(rows, "views", reverse)
                ],
                expected,
            )

    def test_runtime_sort_numeric(self):
        rows = self.rows(
            [doc("a", runtime="2:00:00"), doc("b", runtime="12:00:00")], {}
        )
        self.assertEqual(sorted_recordings(rows, "runtime")[0].identifier, "a")

    def test_group_counts_with_missing_venue(self):
        docs = [
            doc(),
            doc("second", date="2025-01-01"),
            doc("third", date="2025-01-02", venue=None),
        ]
        stats = summarize(self.rows(docs, {}))
        self.assertEqual(
            [stats[k] for k in ("tapes", "dates", "shows", "performances")],
            [3, 2, 1, 1],
        )

    def test_collection_order_and_favorites_do_not_split_performance(self):
        docs = [
            doc(),
            doc(
                "second",
                date="2025-01-01",
                collection=["band", "fav-person", "etree"],
            ),
        ]
        self.assertEqual(summarize(self.rows(docs, {}))["performances"], 1)

    def test_empty_report(self):
        stats = summarize([])
        self.assertEqual(stats["tapes"], 0)
        self.assertEqual(stats["runtime_seconds"], 0)
        self.assertIsNone(stats["average_views"])

    def test_unknown_counts_preserve_original_summary_labels(self):
        rows = self.rows([doc("known"), doc("unknown")], {"known": 20})
        text = render(rows, summarize(rows), "views")
        self.assertIn("         Views: 20", text)
        self.assertNotIn("Unknown Views", text)
        self.assertNotIn("Runtime Seconds", text)
        self.assertIn("?", text)
