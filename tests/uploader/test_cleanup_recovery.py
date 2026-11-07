"""Resume reviewed replacements and finish cleanup across process restarts."""

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from requests import Response

from lma_tools.uploader import cli, multipart, remote
from lma_tools.uploader.recovery import RecoveryPolicy

IDENTIFIER = "artist2026-08-17.audience.t-flac2448"


def response(status=200, content=b"{}", **headers):
    result = Response()
    result.status_code = status
    result._content = content
    result._content_consumed = True
    result.headers.update(headers)
    return result


class CleanupRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "project" / IDENTIFIER
        self.folder.mkdir(parents=True)
        self.config = self.root / "config.toml"
        self.config.write_text(
            f'[uploader]\nshows_root = "{self.root}"\n'
            "[uploader.recovery]\nretries = 0\nchecks = 1\n"
            "delay = 0.001\nmax_delay = 0.001\n",
            encoding="utf-8",
        )
        self.log = self.folder.parent / f"{IDENTIFIER}_lma.log"
        self.journal = cli.Journal(self.log, IDENTIFIER)
        self.item = Mock(exists=True, identifier=IDENTIFIER, files=[])
        self.session = Mock(access_key="test", secret_key="test")
        self.session.get_item.return_value = self.item
        self.session.get_metadata.return_value = {"files": []}
        self.session.submit_task.side_effect = lambda *a, **kw: response()
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(
            patch.object(cli, "ArchiveSession", return_value=self.session)
        )

    def entry(self, name="old.flac", content=b"old"):
        return {
            "name": name,
            "source": "original",
            "md5": hashlib.md5(content).hexdigest(),
            "size": str(len(content)),
        }

    def clean(self, target=None):
        return cli.main(
            ["--config", str(self.config), "--clean", target or IDENTIFIER]
        )

    def checkpoint(self, path, phase, signature):
        self.journal.save_resume(
            path.name,
            {
                "version": 1,
                "upload_id": "existing-upload",
                "signature": signature,
                "part_bytes": 5 * multipart.MIB,
                "parts": {},
                "phase": phase,
            },
        )

    def test_same_payload_checkpoint_permits_review_and_resume(self):
        path = self.folder / "one.flac"
        path.write_bytes(b"corrected audio")
        digest, size, stamp = cli.file_md5(path)
        self.item.files = [self.entry(path.name)]
        signature = {
            "identifier": IDENTIFIER,
            "name": path.name,
            "md5": digest,
            "size": size,
        }
        for phase in ("uploading", "completing", "assembling"):
            with self.subTest(phase=phase):
                self.checkpoint(path, phase, signature)
                with patch("builtins.input", return_value="r") as prompt:
                    replacements, deletions = remote.review_remote(
                        self.item, [path], self.journal
                    )
                self.assertEqual(replacements, {path.name: (digest, size)})
                self.assertEqual(deletions, [])
                prompt.assert_called_once()
                upload = multipart.MultipartUpload(
                    self.session,
                    IDENTIFIER,
                    path,
                    digest,
                    size,
                    stamp,
                    self.journal,
                    multipart.MultipartOptions(),
                    RecoveryPolicy(),
                    Mock(),
                )
                with patch.object(upload, "initiate") as initiate:
                    self.assertFalse(upload.load())
                initiate.assert_not_called()
                self.assertEqual(upload.state["upload_id"], "existing-upload")
                self.assertEqual(upload.state["phase"], phase)

    def test_unknown_payload_checkpoint_blocks_even_force(self):
        path = self.folder / "one.flac"
        path.write_bytes(b"corrected audio")
        digest, size, _ = cli.file_md5(path)
        self.item.files = [self.entry(path.name)]
        signature = {
            "identifier": IDENTIFIER,
            "name": path.name,
            "md5": digest,
            "size": size,
        }
        variants = [None, {}, {**signature, "md5": "0" * 32}]
        variants += [{**signature, "identifier": "another-item"}]
        variants += [{**signature, "size": size + 1}]
        variants += [{**signature, "name": "another.flac"}]
        for phase in ("uploading", "completing", "assembling"):
            for value in variants:
                with self.subTest(phase=phase, signature=value):
                    self.checkpoint(path, phase, value)
                    with patch("builtins.input") as prompt:
                        with self.assertRaisesRegex(
                            ValueError, "checkpoint is still active"
                        ):
                            remote.review_remote(
                                self.item, [path], self.journal, force=True
                            )
                    prompt.assert_not_called()
        self.session.send.assert_not_called()

    def test_pending_deletion_finishes_after_restart_without_candidates(self):
        approved = self.entry()
        self.journal.append("delete_started", **approved)
        self.journal.append("delete_accepted", name=approved["name"])
        self.session.head.side_effect = [
            response(
                ETag='"' + approved["md5"] + '"',
                **{"Content-Length": approved["size"]},
            ),
            response(404),
        ]
        with patch("builtins.input", return_value="UPLOAD"):
            with self.assertRaisesRegex(ValueError, "Deletion still pending"):
                self.clean()
            self.session.submit_task.assert_not_called()
            self.assertEqual(self.clean(), 0)
        self.assertEqual(self.session.head.call_count, 2)
        self.session.send.assert_not_called()
        self.session.submit_task.assert_called_once_with(
            IDENTIFIER,
            "derive.php",
            comment="lma-tools: selected uploads complete",
            request_kwargs={"timeout": (30, 60)},
        )
        restarted = cli.Journal(self.log, IDENTIFIER)
        events = restarted.events()
        kinds = [event["event"] for event in events]
        self.assertLess(
            kinds.index("delete_verified"), kinds.index("derive_queued")
        )
        reviews = [e for e in events if e["event"] == "remote_review"]
        self.assertEqual(reviews[-1]["deletions"], [])
        self.assertEqual(remote.unfinished_cleanup(restarted), [])
        with patch("builtins.input", return_value="UPLOAD"):
            self.assertEqual(self.clean(), 0)
        self.assertEqual(self.session.submit_task.call_count, 1)

    def test_cleanup_empty_folder_works_by_name_and_explicit_path(self):
        self.assertEqual(list(self.folder.iterdir()), [])
        for target in (IDENTIFIER, str(self.folder)):
            with self.subTest(target=target):
                with (
                    patch("builtins.input", return_value="UPLOAD") as prompt,
                    patch.object(
                        cli,
                        "discover",
                        side_effect=AssertionError("discovery"),
                    ),
                    patch.object(
                        cli,
                        "audio_runtime",
                        side_effect=AssertionError("audio"),
                    ),
                    patch.object(
                        cli,
                        "upload_files",
                        side_effect=AssertionError("upload"),
                    ),
                ):
                    self.assertEqual(self.clean(target), 0)
                prompt.assert_called_once()
        self.assertEqual(list(self.folder.iterdir()), [])
        self.session.get_item.assert_called_with(
            IDENTIFIER, request_kwargs={"timeout": (30, 60)}
        )
        self.item.modify_metadata.assert_not_called()
        self.item.upload_file.assert_not_called()
        self.session.send.assert_not_called()
        self.session.submit_task.assert_not_called()
