import unittest
from unittest.mock import Mock, patch

from requests import Timeout

from lma_tools.upload import history


def record(creator="Collection Artist", title_artist=None, identifier="one"):
    return {
        "identifier": identifier,
        "creator": creator,
        "title": f"{title_artist or creator} Live at Old Venue on 2025-01-02",
    }


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "creator": "Local Artist",
            "title": "Local Title",
            "collection": "ExampleCollection",
            "transferer": "Example Taper",
            "venue": "New Venue",
            "date": "2026-08-17",
            "description": "Unchanged <taper@example.com>",
        }
        self.session = Mock(user_email="taper@example.com")
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.session.get_item.return_value = Mock(exists=False)

    def suggest(self, *samples):
        self.session.get.side_effect = [
            Mock(json=Mock(return_value={"response": {"docs": sample}}))
            for sample in samples
        ]
        with patch.object(
            history, "ArchiveSession", return_value=self.session
        ):
            return history.suggest("artist2026-08-17", self.metadata)

    def test_personal_majority_preferred_and_only_naming_fields_change(self):
        result, note = self.suggest(
            [
                record(),
                record(identifier="two"),
                record("Other", identifier="3"),
            ]
        )
        self.assertEqual(result["creator"], "Collection Artist")
        self.assertEqual(
            result["title"],
            "Collection Artist Live at New Venue on 2026-08-17",
        )
        self.assertEqual(result["description"], self.metadata["description"])
        self.assertEqual(self.metadata["creator"], "Local Artist")
        self.assertIn("2/3 your uploads", note)
        self.session.get.assert_called_once()
        params = self.session.get.call_args.kwargs["params"]
        self.assertIn('uploader:"taper@example.com"', params["q"])
        self.assertIn('NOT identifier:"artist2026-08-17"', params["q"])
        self.assertEqual(params["sort[]"][0], "publicdate desc")
        self.assertEqual(params["rows"], 3)

    def test_creator_and_title_artist_can_differ(self):
        result, _ = self.suggest(
            [
                record("Artist", "The Artist"),
                record("Artist", "The Artist", "two"),
            ]
        )
        self.assertEqual(result["creator"], "Artist")
        self.assertTrue(result["title"].startswith("The Artist Live at"))

    def test_others_fill_short_personal_sample(self):
        result, _ = self.suggest(
            [record()], [record(identifier="two"), record(identifier="three")]
        )
        self.assertEqual(result["creator"], "Collection Artist")
        self.assertIn(
            "AND NOT (uploader:",
            self.session.get.call_args.kwargs["params"]["q"],
        )

    def test_conflicting_personal_history_can_fall_back_to_others(self):
        result, _ = self.suggest(
            [record("A"), record("B"), record("C")],
            [record(), record(identifier="two")],
        )
        self.assertEqual(result["creator"], "Collection Artist")

    def test_no_consensus_keeps_local_values(self):
        result, note = self.suggest(
            [record("A")], [record("B", identifier="two")]
        )
        self.assertEqual(result, self.metadata)
        self.assertIn("No naming consensus", note)

    def test_missing_auth_email_uses_transferer(self):
        self.session.user_email = None
        self.suggest([record(), record(identifier="two")])
        query = self.session.get.call_args.kwargs["params"]["q"]
        self.assertIn('transferer:"Example Taper"', query)

    def test_existing_item_uses_its_current_naming_without_search(self):
        self.session.get_item.return_value = Mock(
            exists=True, metadata={"creator": "Fixed", "title": "Fixed title"}
        )
        result, note = self.suggest()
        self.assertEqual(result["creator"], "Fixed")
        self.assertEqual(result["title"], "Fixed title")
        self.session.get.assert_not_called()
        self.assertIn("existing item", note)

    def test_timeout_keeps_review_available(self):
        self.session.get_item.side_effect = Timeout()
        result, note = self.suggest()
        self.assertEqual(result, self.metadata)
        self.assertIn("unavailable", note)

    def test_invalid_search_response_keeps_local_values(self):
        result, _ = self.suggest(["not a metadata record"])
        self.assertEqual(result, self.metadata)

    def test_ambiguous_creators_and_unrecognized_titles_are_not_inferred(self):
        ambiguous = record()
        ambiguous["creator"] = ["A", "B"]
        unusual = record()
        unusual["title"] = "A completely different title"
        self.assertIsNone(history.consensus([ambiguous, unusual]))

    def test_query_literal_escapes_quotes_and_backslashes(self):
        self.assertEqual(history.quoted('a"b\\c'), '"a\\"b\\\\c"')
