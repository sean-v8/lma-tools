"""Fresh notes/config win over journals; remote values are comparison only."""

import io
from contextlib import redirect_stdout
from unittest import TestCase
from unittest.mock import Mock, patch

from requests import ConnectionError

from lma_tools.upload import cli, metadata_review
from tests.upload import test_metadata_first
from tests.upload.test_lma import ID, NOTES


class MetadataRefreshTests(TestCase):
    fixture = test_metadata_first.MetadataFirstTests.fixture

    def test_old_review_cannot_override_notes_or_configured_naming(self):
        with self.fixture() as (folder, item, session, args):
            config = folder.parent.parent / "config.toml"
            config.write_text(
                config.read_text().replace(
                    'artist = "ExampleArtistCollection"',
                    "[upload.collections.artist]\n"
                    'collection = "ExampleArtistCollection"\n'
                    'creator = "Current Artist"',
                )
            )
            old = cli.parse_notes(NOTES, ID)
            old.update(
                creator="Old Artist",
                title="Old Title",
                description="Old notes",
                source="Old source",
                subject="Live music",
            )
            cli.Journal(folder.parent / f"{ID}_lma.log", ID).append(
                "run_start", mode="full", metadata=old
            )
            self.assertEqual(cli.main(args + ["-m"]), 0)
            payload = item.modify_metadata.call_args.args[0]
            self.assertEqual(payload["creator"], "Current Artist")
            self.assertTrue(payload["title"].startswith("Current Artist Live"))
            fresh = cli.parse_notes(NOTES, ID)
            self.assertEqual(payload["source"], fresh["source"])
            self.assertEqual(payload["subject"], "Live music")
            self.assertEqual(
                cli.html.unescape(payload["description"]),
                fresh["description"],
            )

    def test_existing_remote_values_do_not_replace_review_defaults(self):
        proposed = cli.parse_notes(NOTES, ID)
        proposed.update(
            collection="ExampleArtistCollection", runtime="0:01:00"
        )
        remote = dict(proposed, creator="Old Artist", title="Old Title")
        with (
            patch.object(metadata_review, "snapshot", return_value=remote),
            patch("lma_tools.upload.history.suggest") as suggest,
            patch(
                "lma_tools.upload.ui.edit_document",
                side_effect=lambda text, validate: validate(text),
            ) as editor,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.edit_review(ID, proposed), (ID, proposed))
        suggest.assert_not_called()
        document = editor.call_args.args[0]
        self.assertIn('# - "Old Artist"', document)
        self.assertIn('# + "Artist"', document)

    def test_unavailable_snapshot_does_not_reapply_old_history(self):
        with (
            patch.object(metadata_review, "snapshot", return_value=None),
            patch("lma_tools.upload.history.suggest") as suggest,
            patch(
                "lma_tools.upload.ui.edit_document",
                return_value=(ID, {}),
            ),
            redirect_stdout(io.StringIO()),
        ):
            cli.edit_review(ID, {})
        suggest.assert_not_called()

    def test_description_comparison_decodes_transport_once(self):
        self.assertIn(
            "No proposed metadata differences",
            metadata_review.comparison(
                {"description": "A &amp; B\n", "creator": ["Artist"]},
                {"description": "A & B", "creator": "Artist"},
            ),
        )
        diff = metadata_review.comparison(
            {"description": "First\nOld\nLast"},
            {"description": "First\nNew\nLast"},
        )
        self.assertIn("# -Old", diff)
        self.assertIn("# +New", diff)
        self.assertIn("unavailable", metadata_review.comparison(None, {}))
        self.assertIn("New item", metadata_review.comparison({}, {}))

    def test_snapshot_is_read_only_and_handles_network_failure(self):
        session = Mock()
        session.get_item.return_value = Mock(
            exists=True, metadata={"creator": "Artist"}
        )
        manager = Mock()
        manager.__enter__ = Mock(return_value=session)
        manager.__exit__ = Mock(return_value=False)
        with patch.object(
            metadata_review, "ArchiveSession", return_value=manager
        ):
            self.assertEqual(
                metadata_review.snapshot(ID), {"creator": "Artist"}
            )
            session.get_item.assert_called_once_with(
                ID, request_kwargs={"timeout": (5, 10)}
            )
            session.get_item.side_effect = ConnectionError("offline")
            self.assertIsNone(metadata_review.snapshot(ID))
        session.modify_metadata.assert_not_called()
