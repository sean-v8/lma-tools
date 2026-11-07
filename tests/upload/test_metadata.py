"""Metadata transport, fresh defaults and reviewed updates."""

import io
from contextlib import redirect_stdout
from html import unescape
from unittest import TestCase
from unittest.mock import Mock, patch

from requests import ConnectionError

from lma_tools.upload import cli, metadata_review
from tests.upload.helpers import ID, NOTES, metadata_fixture


class MetadataFirstTests(TestCase):
    fixture = metadata_fixture

    def test_description_encoding_preserves_literal_text(self):
        text = "Name <person@example.com> & <b>literal</b> &lt; >\n  indent"
        metadata = {"description": text, "creator": "Artist & Friends"}
        payload = cli.transport_metadata(metadata)
        self.assertEqual(unescape(payload["description"]), text)
        self.assertNotIn("<person@", payload["description"])
        self.assertEqual(payload["creator"], metadata["creator"])
        self.assertEqual(metadata["description"], text)
        self.assertEqual(cli.transport_metadata(metadata), payload)
        self.assertEqual(cli.transport_metadata({}), {})

    def test_metadata_update_encodes_description_but_log_stays_plain(self):
        with self.fixture() as (folder, item, session, args):
            self.assertEqual(cli.main(args + ["-m"]), 0)
            payload = item.modify_metadata.call_args.args[0]
            self.assertIn("&lt;taper@example.com&gt;", payload["description"])
            events = cli.Journal(folder.parent / f"{ID}_lma.log", ID).events()
            saved = next(e for e in events if e["event"] == "metadata_ok")
            self.assertIn(
                "<taper@example.com>", saved["metadata"]["description"]
            )

    def test_creation_encodes_description_before_first_txt(self):
        with self.fixture() as (_, item, session, args):
            item.exists = False
            self.assertEqual(cli.main(args), 0)
            payload = item.upload_file.call_args_list[0].kwargs["metadata"]
            self.assertIn("&lt;taper@example.com&gt;", payload["description"])
            self.assertEqual(unescape(payload["description"]), NOTES.rstrip())

    def test_existing_item_metadata_precedes_every_upload(self):
        with self.fixture() as (_, item, session, args):
            order = Mock()
            order.attach_mock(item.modify_metadata, "metadata")
            order.attach_mock(item.upload_file, "upload")
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(order.mock_calls[0][0], "metadata")
            payload = item.modify_metadata.call_args.args[0]
            self.assertEqual(payload["runtime"], "2:05:50")
            self.assertEqual(payload["transferer"], "Example Taper")
            self.assertNotIn("uploader", payload)
            self.assertNotIn("collection", payload)
            self.assertEqual(item.metadata["uploader"], "taper@example.com")

    def test_metadata_only_uploads_nothing_and_does_not_derive(self):
        with self.fixture() as (_, item, session, args):
            self.assertEqual(cli.main(args + ["-m"]), 0)
            item.modify_metadata.assert_called_once()
            self.assertNotIn(
                "collection", item.modify_metadata.call_args.args[0]
            )
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_metadata_only_can_correct_creator_and_title_with_expectations(
        self,
    ):
        with self.fixture() as (_, item, session, args):
            item.metadata["creator"] = "Old artist"
            item.metadata["title"] = "Old title"
            self.assertEqual(cli.main(args + ["-m"]), 0)
            call = item.modify_metadata.call_args
            self.assertEqual(call.args[0]["creator"], "Artist")
            self.assertEqual(
                call.kwargs["expect"],
                {"creator": "Old artist", "title": "Old title"},
            )
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_full_resume_updates_creator_with_expectation(self):
        with self.fixture() as (_, item, session, args):
            item.metadata["creator"] = "Old artist"
            self.assertEqual(cli.main(args), 0)
            call = item.modify_metadata.call_args
            self.assertEqual(call.args[0]["creator"], "Artist")
            self.assertEqual(call.kwargs["expect"], {"creator": "Old artist"})
            item.upload_file.assert_called()

    def test_metadata_failure_prevents_uploads(self):
        with self.fixture() as (_, item, session, args):
            item.modify_metadata.side_effect = ValueError("metadata rejected")
            with self.assertRaisesRegex(ValueError, "metadata rejected"):
                cli.main(args)
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_file_failure_does_not_prevent_prior_runtime_update(self):
        with self.fixture() as (folder, item, session, args):
            item.upload_file.side_effect = ValueError("network interrupted")
            with self.assertRaisesRegex(ValueError, "network interrupted"):
                cli.main(args)
            self.assertEqual(
                item.modify_metadata.call_args.args[0]["runtime"], "2:05:50"
            )
            events = cli.Journal(folder.parent / f"{ID}_lma.log", ID).events()
            names = [event["event"] for event in events]
            self.assertLess(
                names.index("metadata_ok"), names.index("upload_started")
            )
            session.submit_task.assert_not_called()

    def test_metadata_only_rejects_missing_item(self):
        with self.fixture() as (_, item, session, args):
            item.exists = False
            with self.assertRaisesRegex(ValueError, "does not exist"):
                cli.main(args + ["-m"])
            item.modify_metadata.assert_not_called()
            item.upload_file.assert_not_called()

    def test_metadata_only_preview_writes_nothing(self):
        with self.fixture() as (folder, item, session, args):
            before = {
                p: p.read_bytes()
                for p in folder.parent.rglob("*")
                if p.is_file()
            }
            self.assertEqual(cli.main(args + ["-m", "--preview"]), 0)
            self.assertEqual(
                before,
                {
                    p: p.read_bytes()
                    for p in folder.parent.rglob("*")
                    if p.is_file()
                },
            )
            session.get_item.assert_not_called()

    def test_saved_display_name_cannot_override_notes_transferer(self):
        with self.fixture() as (folder, item, session, args):
            old = cli.parse_notes(NOTES, ID)
            old["uploader"] = old.pop("transferer")
            old["source"] = "Previous manual edit"
            cli.Journal(folder.parent / f"{ID}_lma.log", ID).append(
                "run_start", mode="full", metadata=old
            )
            self.assertEqual(cli.main(args + ["-m"]), 0)
            payload = item.modify_metadata.call_args.args[0]
            self.assertEqual(payload["transferer"], "Example Taper")
            self.assertEqual(
                payload["source"], cli.parse_notes(NOTES, ID)["source"]
            )
            self.assertNotIn("uploader", payload)


class MetadataRefreshTests(TestCase):
    fixture = metadata_fixture

    def test_old_review_cannot_override_notes_or_configured_naming(self):
        with self.fixture() as (folder, item, session, args):
            config = folder.parent.parent / "config.toml"
            config.write_text(
                config.read_text().replace(
                    'artist = "ExampleArtistCollection"',
                    "[upload.prefix.artist]\n"
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

    def test_collection_membership_is_not_reported_as_replacement(self):
        text = metadata_review.comparison(
            {"collection": ["ArtistCollection", "etree"]},
            {"collection": "ArtistCollection"},
        )
        self.assertIn("No proposed metadata differences", text)
        text = metadata_review.comparison(
            {"collection": ["OtherCollection", "etree"]},
            {"collection": "ArtistCollection"},
        )
        self.assertIn("protected", text)

    def test_creation_requests_both_collections_without_mutating_review(self):
        reviewed = {"mediatype": "etree", "collection": "ArtistCollection"}
        payload = cli.transport_metadata(reviewed)
        self.assertEqual(payload["collection"], ["ArtistCollection", "etree"])
        self.assertEqual(reviewed["collection"], "ArtistCollection")
        self.assertEqual(cli.transport_metadata(payload), payload)
        self.assertNotIn("collection_added", payload)
