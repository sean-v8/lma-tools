"""Preparation, metadata and cleanup workflow boundaries."""

import contextlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload import cli
from lma_tools.upload import cli as lma
from lma_tools.upload import core as lma_core
from tests.upload.helpers import (
    ID,
    make_show,
    metadata_fixture,
    response,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        audit = patch.object(lma, "audit_upload", return_value={})
        audit.start()
        self.addCleanup(audit.stop)
        preparation = patch(
            "lma_tools.upload.prepare.prepare_show", return_value={}
        )
        preparation.start()
        self.addCleanup(preparation.stop)
        manifests = patch(
            "lma_tools.upload.prepare.validate_manifests",
            return_value={"valid": True},
        )
        manifests.start()
        self.addCleanup(manifests.stop)

    def setup_show(self, root):
        folder = make_show(root)
        config = root / "config.toml"
        config.write_text(
            f'[upload]\nshows_root = "{root}"\nupload_workers = 1\n'
            '[upload.prefix]\nartist = "ExampleArtistCollection"\n'
        )
        return folder, ["--config", str(config), ID]

    def test_preview_and_completion_write_nothing_and_need_no_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            before = {
                p: p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            with (
                patch.object(
                    lma,
                    "ArchiveSession",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch.object(lma, "audio_runtime", return_value="1:00:00"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["--preview"]), 0)
                self.assertEqual(
                    lma.main(
                        [
                            "--config",
                            str(root / "config.toml"),
                            "--complete",
                            "art",
                        ]
                    ),
                    0,
                )
            after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((folder / ".lma-upload.toml").exists())

    def test_cancel_creates_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            with (
                patch.object(lma, "audio_runtime", return_value="1:00:00"),
                patch.object(
                    lma, "edit_review", side_effect=lambda i, m, **kw: (i, m)
                ),
                patch("builtins.input", return_value="no"),
                patch.object(
                    lma,
                    "ArchiveSession",
                    return_value=Mock(
                        access_key="test",
                        secret_key="test",
                        get_item=Mock(
                            return_value=Mock(
                                exists=False, files=[], metadata={}
                            )
                        ),
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args), 0)
            self.assertEqual(list(root.rglob("*.log")), [])

    def test_upload_only_never_computes_or_updates_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            digest = lma.file_md5(folder / "one.flac")[0]
            (folder / "checks.ffp").write_text(f"one.flac:{digest}\n")
            (folder / "checks.md5").write_text(f"{digest}  one.flac\n")
            item = Mock(exists=True, identifier=ID, files=[])
            item.upload_file.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.send = Mock()
            session.get_item.return_value = item
            session.submit_task.return_value = response()
            with (
                patch.object(
                    lma,
                    "audio_runtime",
                    side_effect=AssertionError("runtime forbidden"),
                ),
                patch.object(
                    lma_core,
                    "FLAC",
                    return_value=Mock(
                        info=Mock(md5_signature=int(digest, 16))
                    ),
                ),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["-u"]), 0)
            item.modify_metadata.assert_not_called()
            self.assertCountEqual(
                [c.kwargs["key"] for c in item.upload_file.call_args_list],
                [
                    ID + ".txt",
                    "cover.jpg",
                    "checks.ffp",
                    "checks.md5",
                    "one.flac",
                ],
            )
            for call in item.upload_file.call_args_list:
                self.assertEqual(call.kwargs["metadata"], {})
            self.assertEqual(list(folder.glob("*.log")), [])
            self.assertEqual(len(list(folder.parent.glob("*_lma.log"))), 1)

    def test_runtime_only_never_uploads_or_derives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            item = Mock(exists=True, identifier=ID, files=[])
            item.modify_metadata.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.get_item.return_value = item
            with (
                patch.object(lma, "audio_runtime", return_value="2:05:50"),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["-r"]), 0)
            item.modify_metadata.assert_called_once_with(
                {"runtime": "2:05:50"}, timeout=60
            )
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_creation_upload_order_and_single_derive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            item = Mock(exists=False, identifier=ID, files=[])
            item.upload_file.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.send = Mock()
            session.get_item.side_effect = [
                item,
                Mock(exists=True, metadata={"mediatype": "collection"}),
            ]
            session.submit_task.return_value = response()
            with (
                patch.object(lma, "audio_runtime", return_value="2:05:50"),
                patch.object(
                    lma, "edit_review", side_effect=lambda i, m, **kw: (i, m)
                ),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args), 0)
            calls = item.upload_file.call_args_list
            self.assertEqual(
                [c.kwargs["key"] for c in calls],
                [ID + ".txt", "cover.jpg", "one.flac"],
            )
            self.assertEqual(calls[0].kwargs["metadata"]["runtime"], "2:05:50")
            self.assertNotIn("setlist", calls[0].kwargs["metadata"])
            self.assertEqual(
                [c.kwargs["queue_derive"] for c in calls], [False, False, True]
            )
            session.submit_task.assert_called_once()


class UpdatedWorkflowTests(unittest.TestCase):
    fixture = metadata_fixture

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
                "[upload.prefix.artist]\n"
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
                "lma_tools.upload.prepare.prepare_show",
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
                    "lma_tools.upload.recovery.remote_state",
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
                    "lma_tools.upload.recovery.remote_state",
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
                patch("lma_tools.upload.prepare.prepare_show") as prepare,
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
