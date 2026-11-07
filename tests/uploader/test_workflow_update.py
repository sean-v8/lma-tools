"""Preparation and reviewed remote changes integrate without eager deletes."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.uploader import cli
from tests.uploader import test_metadata_first


class UpdatedWorkflowTests(unittest.TestCase):
    def fixture(self):
        return test_metadata_first.MetadataFirstTests.fixture(self)

    def test_missing_collection_never_creates_or_uploads(self):
        with self.fixture() as (_, item, session, args):
            item.exists = False
            session.get_item.side_effect = [
                item,
                Mock(exists=False, metadata={}),
            ]
            with self.assertRaisesRegex(ValueError, "existing Archive"):
                cli.main(args)
            item.upload_file.assert_not_called()
            item.modify_metadata.assert_not_called()
            session.submit_task.assert_not_called()

    def test_saved_creator_bypasses_history_lookup(self):
        with self.fixture() as (folder, item, session, args):
            path = folder.parent.parent / "config.toml"
            text = path.read_text().replace(
                'artist = "ExampleArtistCollection"',
                "[uploader.collections.artist]\n"
                'collection = "ExampleArtistCollection"\n'
                'creator = "Saved Artist"',
            )
            path.write_text(text)
            with patch.object(
                cli, "edit_review", side_effect=lambda i, m, **kw: (i, m)
            ) as review:
                cli.main(args + ["-m"])
            self.assertFalse(review.call_args.kwargs["history"])
            self.assertEqual(
                item.modify_metadata.call_args.args[0]["creator"],
                "Saved Artist",
            )

    def test_preparation_only_never_contacts_archive(self):
        with self.fixture() as (_, item, session, args):
            with patch(
                "lma_tools.uploader.prepare.prepare_show",
                return_value={"genre": "Jazz"},
            ):
                self.assertEqual(cli.main(args + ["-p"]), 0)
            session.get_item.assert_not_called()
            item.upload_file.assert_not_called()

    def test_corrected_files_verified_before_cleanup_and_final_derive(self):
        with self.fixture() as (_, item, session, args):
            order = []
            obsolete = {"name": "old.flac"}
            with (
                patch.object(
                    cli, "review_remote", return_value=({}, [obsolete])
                ),
                patch.object(
                    cli,
                    "upload_files",
                    side_effect=lambda *a, **kw: order.append(
                        ("upload", kw["defer_derive"])
                    ),
                ),
                patch(
                    "lma_tools.uploader.recovery.remote_state",
                    side_effect=lambda *a: (
                        order.append(("verify", a[2])) or ("match", "HEAD")
                    ),
                ),
                patch.object(
                    cli,
                    "delete_originals",
                    side_effect=lambda *a, **kw: order.append(("delete",)),
                ),
                patch.object(
                    cli,
                    "queue_final_derive",
                    side_effect=lambda *a: order.append(("derive",)),
                ),
            ):
                cli.main(args)
            self.assertEqual(order[0], ("upload", True))
            self.assertEqual(order[-2:], [("delete",), ("derive",)])
            self.assertTrue(all(e[0] == "verify" for e in order[1:-2]))

    def test_uncertain_corrected_files_prevent_cleanup(self):
        with self.fixture() as (_, item, session, args):
            with (
                patch.object(
                    cli,
                    "review_remote",
                    return_value=({}, [{"name": "old.flac"}]),
                ),
                patch.object(cli, "upload_files"),
                patch(
                    "lma_tools.uploader.recovery.remote_state",
                    return_value=("unknown", "HEAD"),
                ),
                patch.object(cli, "delete_originals") as delete,
            ):
                with self.assertRaisesRegex(ValueError, "Cleanup postponed"):
                    cli.main(args)
            delete.assert_not_called()
            session.submit_task.assert_not_called()

    def test_cleanup_only_does_not_prepare_upload_or_modify_metadata(self):
        with self.fixture() as (_, item, session, args):
            with (
                patch.object(cli, "review_remote", return_value=({}, [])),
                patch("lma_tools.uploader.prepare.prepare_show") as prepare,
                patch.object(cli, "audio_runtime") as runtime,
                patch.object(cli, "upload_files") as upload,
            ):
                cli.main(args + ["--clean"])
            prepare.assert_not_called()
            runtime.assert_not_called()
            upload.assert_not_called()
            item.modify_metadata.assert_not_called()
            session.submit_task.assert_not_called()

    def test_help_discovery_and_options_are_available(self):
        with redirect_stdout(io.StringIO()):
            args = cli.parser().parse_args(["-p", "example"])
        self.assertTrue(args.prepare)
        self.assertFalse(args.clean)
