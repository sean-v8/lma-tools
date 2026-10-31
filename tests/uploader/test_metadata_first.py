import io
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from lma_tools.uploader import cli as lma
from tests.uploader.test_lma import ID, NOTES, make_show, response


class MetadataFirstTests(TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = make_show(root)
            config = root / "config.toml"
            config.write_text(
                f'[uploader]\nshows_root = "{root}"\n'
                '[uploader.collections]\nartist = "ExampleArtistCollection"\n'
            )
            metadata = lma.parse_notes(NOTES, ID)
            metadata.update(
                collection=["ExampleArtistCollection", "etree"],
                uploader="taper@example.com",
            )
            item = Mock(
                exists=True, identifier=ID, files=[], metadata=metadata
            )
            item.modify_metadata.return_value = response()
            item.upload_file.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.get_item.side_effect = [
                item,
                Mock(exists=True, metadata={"mediatype": "collection"}),
            ]
            session.submit_task.return_value = response()
            args = ["--config", str(config), ID]
            with (
                patch.object(lma, "audio_runtime", return_value="2:05:50"),
                patch.object(
                    lma, "edit_review", side_effect=lambda i, m: (i, m)
                ),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                redirect_stdout(io.StringIO()),
            ):
                yield folder, item, session, args

    def test_existing_item_metadata_precedes_every_upload(self):
        with self.fixture() as (_, item, session, args):
            order = Mock()
            order.attach_mock(item.modify_metadata, "metadata")
            order.attach_mock(item.upload_file, "upload")
            self.assertEqual(lma.main(args), 0)
            self.assertEqual(order.mock_calls[0][0], "metadata")
            payload = item.modify_metadata.call_args.args[0]
            self.assertEqual(payload["runtime"], "2:05:50")
            self.assertEqual(payload["transferer"], "Example Taper")
            self.assertNotIn("uploader", payload)
            self.assertNotIn("collection", payload)
            self.assertEqual(item.metadata["uploader"], "taper@example.com")

    def test_metadata_only_uploads_nothing_and_does_not_derive(self):
        with self.fixture() as (_, item, session, args):
            self.assertEqual(lma.main(args + ["-m"]), 0)
            item.modify_metadata.assert_called_once()
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_metadata_failure_prevents_uploads(self):
        with self.fixture() as (_, item, session, args):
            item.modify_metadata.side_effect = ValueError("metadata rejected")
            with self.assertRaisesRegex(ValueError, "metadata rejected"):
                lma.main(args)
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_file_failure_does_not_prevent_prior_runtime_update(self):
        with self.fixture() as (folder, item, session, args):
            item.upload_file.side_effect = ValueError("network interrupted")
            with self.assertRaisesRegex(ValueError, "network interrupted"):
                lma.main(args)
            self.assertEqual(
                item.modify_metadata.call_args.args[0]["runtime"], "2:05:50"
            )
            events = lma.Journal(folder.parent / f"{ID}_lma.log", ID).events()
            names = [event["event"] for event in events]
            self.assertLess(
                names.index("metadata_ok"), names.index("upload_started")
            )
            session.submit_task.assert_not_called()

    def test_metadata_only_rejects_missing_item(self):
        with self.fixture() as (_, item, session, args):
            item.exists = False
            with self.assertRaisesRegex(ValueError, "does not exist"):
                lma.main(args + ["-m"])
            item.modify_metadata.assert_not_called()
            item.upload_file.assert_not_called()

    def test_metadata_only_preview_writes_nothing(self):
        with self.fixture() as (folder, item, session, args):
            before = {
                p: p.read_bytes()
                for p in folder.parent.rglob("*")
                if p.is_file()
            }
            self.assertEqual(lma.main(args + ["-m", "--preview"]), 0)
            self.assertEqual(
                before,
                {
                    p: p.read_bytes()
                    for p in folder.parent.rglob("*")
                    if p.is_file()
                },
            )
            session.get_item.assert_not_called()

    def test_legacy_log_display_name_migrates_to_transferer(self):
        with self.fixture() as (folder, item, session, args):
            old = lma.parse_notes(NOTES, ID)
            old["uploader"] = old.pop("transferer")
            old["source"] = "Previous manual edit"
            lma.Journal(folder.parent / f"{ID}_lma.log", ID).append(
                "run_start", mode="full", metadata=old
            )
            self.assertEqual(lma.main(args + ["-m"]), 0)
            payload = item.modify_metadata.call_args.args[0]
            self.assertEqual(payload["transferer"], "Example Taper")
            self.assertEqual(payload["source"], "Previous manual edit")
            self.assertNotIn("uploader", payload)
