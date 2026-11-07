"""HTTP protocol tests; no live Archive requests."""

import unittest
from unittest.mock import Mock

import requests

from lma_tools.report.client import ArchiveClient, ArchiveError


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.client = ArchiveClient(12, self.session)

    def response(self, data):
        response = Mock()
        response.json.return_value = data
        return response

    def test_pagination_deduplicates(self):
        first = [{"identifier": str(i)} for i in range(500)]
        second = [{"identifier": "499"}, {"identifier": "500"}]
        self.session.get.side_effect = [
            self.response({"response": {"docs": first, "numFound": 502}}),
            self.response({"response": {"docs": second, "numFound": 502}}),
        ]
        docs = list(self.client.search("collection:etree"))
        self.assertEqual(len(docs), 501)
        args = self.session.get.call_args.kwargs
        self.assertEqual(args["params"]["page"], 2)
        self.assertEqual(args["timeout"], 12)

    def test_empty_search(self):
        self.session.get.return_value = self.response(
            {"response": {"docs": [], "numFound": 0}}
        )
        self.assertEqual(list(self.client.search("x")), [])

    def test_empty_query_rejected_before_network(self):
        with self.assertRaises(ValueError):
            list(self.client.search(""))
        self.session.get.assert_not_called()

    def test_bad_search_schema(self):
        for data in (
            {},
            {"response": {"docs": "bad", "numFound": 1}},
            {"response": {"docs": [{}], "numFound": 1}},
        ):
            self.session.get.return_value = self.response(data)
            with self.assertRaises(ArchiveError):
                list(self.client.search("x"))

    def test_premature_empty_page_errors(self):
        self.session.get.return_value = self.response(
            {"response": {"docs": [], "numFound": 501}}
        )
        with self.assertRaises(ArchiveError):
            list(self.client.search("x"))

    def test_http_failure(self):
        self.session.get.side_effect = requests.Timeout("timed out")
        result = dict(self.client.view_counts(["item"]))
        self.assertIsInstance(result["item"], ArchiveError)

    def test_status_checked(self):
        response = self.response({})
        response.raise_for_status.side_effect = requests.HTTPError("503")
        self.session.get.return_value = response
        result = dict(self.client.view_counts(["item"]))
        self.assertIsInstance(result["item"], ArchiveError)
        response.json.assert_not_called()

    def test_bad_json(self):
        response = Mock()
        response.json.side_effect = ValueError("bad JSON")
        self.session.get.return_value = response
        result = dict(self.client.view_counts(["item"]))
        self.assertIsInstance(result["item"], ArchiveError)

    def test_view_validation(self):
        for value in (None, -1, "10", True):
            self.session.get.return_value = self.response(
                {"item": {"all_time": value}}
            )
            result = dict(self.client.view_counts(["item"]))
            self.assertIsInstance(result["item"], ArchiveError)
        self.session.get.return_value = self.response(
            {"item": {"all_time": 0}}
        )
        self.assertEqual(dict(self.client.view_counts(["item"]))["item"], 0)

    def test_metadata_and_identifier_encoding(self):
        self.session.get.return_value = self.response({"metadata": {"a": 1}})
        self.assertEqual(self.client.metadata("a/b"), {"a": 1})
        self.assertTrue(self.session.get.call_args.args[0].endswith("a%2Fb"))

    def test_retry_policy_and_close(self):
        adapter = self.session.mount.call_args_list[0].args[1]
        self.assertEqual(adapter.max_retries.total, 3)
        self.assertIn(429, adapter.max_retries.status_forcelist)
        self.client.close()
        self.session.close.assert_called_once()

    def test_thousand_counts_use_small_batches(self):
        identifiers = [f"item{index}" for index in range(1000)]
        self.session.get.return_value = self.response(
            {
                identifier: {"all_time": index}
                for index, identifier in enumerate(identifiers)
            }
        )
        counts = dict(self.client.view_counts(identifiers))
        self.assertEqual(len(counts), 1000)
        self.assertEqual(counts["item999"], 999)
        self.assertEqual(self.session.get.call_count, 10)
        for call in self.session.get.call_args_list:
            self.assertIn("/views/v1/short/", call.args[0])
            self.assertLessEqual(len(call.args[0].split(",")), 100)

    def test_batch_missing_item_preserves_good_counts(self):
        self.session.get.return_value = self.response(
            {"good": {"all_time": 42}}
        )
        counts = dict(self.client.view_counts(["good", "missing"]))
        self.assertEqual(counts["good"], 42)
        self.assertIsInstance(counts["missing"], ArchiveError)
        self.session.get.assert_called_once()

    def test_network_outage_stops_remaining_requests(self):
        self.session.get.side_effect = requests.Timeout("slow")
        counts = dict(
            self.client.view_counts([f"item{i}" for i in range(366)])
        )
        self.assertEqual(len(counts), 366)
        self.assertTrue(
            all(isinstance(v, ArchiveError) for v in counts.values())
        )
        self.session.get.assert_called_once()

    def test_empty_batch_no_request(self):
        self.assertEqual(list(self.client.view_counts([])), [])
        self.session.get.assert_not_called()

    def test_identifiers_encoded_separately_and_deduplicated(self):
        self.session.get.return_value = self.response(
            {"a/b": {"all_time": 1}, "c,d": {"all_time": 2}}
        )
        counts = dict(self.client.view_counts(["a/b", "a/b", "c,d"]))
        self.assertEqual(counts, {"a/b": 1, "c,d": 2})
        url = self.session.get.call_args.args[0]
        self.assertTrue(url.endswith("a%2Fb,c%2Cd"))

    def test_long_identifiers_bound_batch_url(self):
        identifiers = [f"{i}" + "x" * 199 for i in range(50)]
        self.session.get.return_value = self.response(
            {identifier: {"all_time": 1} for identifier in identifiers}
        )
        self.assertEqual(len(dict(self.client.view_counts(identifiers))), 50)
        for call in self.session.get.call_args_list:
            self.assertLess(len(call.args[0]), 1600)

    def failing_response(self, status):
        response = self.response({})
        response.status_code = status
        error = requests.HTTPError(
            "long URL should not appear", response=response
        )
        response.raise_for_status.side_effect = error
        return response

    def test_failed_large_batch_recovers_and_remembers_size(self):
        identifiers = [f"item{i}" for i in range(30)]

        def get(url, **kwargs):
            names = url.rsplit("/", 1)[1].split(",")
            if len(names) > 3:
                return self.failing_response(502)
            return self.response({name: {"all_time": 7} for name in names})

        self.session.get.side_effect = get
        counts = dict(self.client.view_counts(identifiers))
        self.assertEqual(counts, dict.fromkeys(identifiers, 7))
        self.assertLessEqual(self.client.view_batch_size, 3)
        self.session.get.reset_mock()
        self.assertEqual(
            dict(self.client.view_counts(["next", "other"])),
            {"next": 7, "other": 7},
        )
        self.session.get.assert_called_once()

    def test_gateway_outage_stops_after_three_failed_singles(self):
        self.session.get.return_value = self.failing_response(502)
        counts = dict(
            self.client.view_counts([f"item{i}" for i in range(366)])
        )
        self.assertEqual(len(counts), 366)
        self.assertTrue(
            all(isinstance(v, ArchiveError) for v in counts.values())
        )
        self.assertLessEqual(self.session.get.call_count, 16)
        self.assertIn("stopped", str(counts["item365"]))

    def test_recovery_does_not_repeat_successful_subset(self):
        self.session.get.side_effect = [
            self.failing_response(502),
            self.response({"a": {"all_time": 10}}),
            self.failing_response(502),
        ]
        counts = dict(self.client.view_counts(["a", "b"]))
        self.assertEqual(counts["a"], 10)
        self.assertIsInstance(counts["b"], ArchiveError)
        self.assertEqual(self.session.get.call_count, 3)

    def test_permanent_error_is_not_split(self):
        self.session.get.return_value = self.failing_response(403)
        counts = dict(self.client.view_counts([f"item{i}" for i in range(40)]))
        self.assertEqual(len(counts), 40)
        self.session.get.assert_called_once()

    def test_view_status_retries_defer_to_splitting(self):
        retry = self.session.mount.call_args_list[1].args[1].max_retries
        self.assertEqual(retry.status, 0)
        self.assertFalse(retry.raise_on_status)

    def test_http_errors_are_concise_and_keep_status(self):
        self.session.get.return_value = self.failing_response(502)
        counts = dict(self.client.view_counts(["item"]))
        self.assertEqual(counts["item"].status, 502)
        self.assertEqual(
            str(counts["item"]), "HTTP 502 from be-api.us.archive.org"
        )

    def test_configured_batch_size(self):
        client = ArchiveClient(12, self.session, view_batch_size=20)
        names = [f"item{i}" for i in range(45)]
        self.session.get.return_value = self.response(
            {name: {"all_time": 1} for name in names}
        )
        self.assertEqual(len(dict(client.view_counts(names))), 45)
        self.assertEqual(
            [
                len(call.args[0].rsplit("/", 1)[1].split(","))
                for call in self.session.get.call_args_list
            ],
            [20, 20, 5],
        )
