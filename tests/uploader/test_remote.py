"""Replacement approval and deletion scope stay separate from transport."""

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.uploader import cli
from lma_tools.uploader.remote import delete_originals, review_remote
from tests.uploader.test_lma import ID, response


class RemoteReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.journal = cli.Journal(self.folder / "show.log", ID)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def entry(self, name, content=b"old", **kwargs):
        return dict(
            name=name,
            md5=hashlib.md5(content).hexdigest(),
            size=str(len(content)),
            source="original",
            **kwargs,
        )

    def test_replace_all_conflicts_retains_matching_skips(self):
        paths = []
        for name in ("one.flac", "two.md5", "same.ffp"):
            path = self.folder / name
            path.write_bytes(b"new")
            paths.append(path)
        item = Mock(
            files=[
                self.entry("one.flac"),
                self.entry("two.md5"),
                self.entry("same.ffp", b"new"),
            ]
        )
        with patch("builtins.input", return_value="a") as prompt:
            replacements, deletions = review_remote(
                item,
                paths,
                self.journal,
            )
        self.assertEqual(set(replacements), {"one.flac", "two.md5"})
        self.assertEqual(deletions, [])
        prompt.assert_called_once()

    def test_deletions_unselected_and_derivatives_excluded(self):
        derivative = self.entry("generated.flac")
        derivative["source"] = "derivative"
        item = Mock(
            files=[
                self.entry("old.flac"),
                derivative,
                self.entry("item_meta.xml"),
                self.entry("../outside.flac"),
                self.entry("cover_thumb.jpg"),
            ]
        )
        # Archive thumbnails normally have source=derivative.
        item.files[-1]["source"] = "derivative"
        with patch("builtins.input", return_value=""):
            _, deletions = review_remote(
                item,
                [],
                self.journal,
                clean=True,
            )
        self.assertEqual(deletions, [])
        with patch("builtins.input", side_effect=["all", "DELETE"]):
            _, deletions = review_remote(
                item,
                [],
                self.journal,
                clean=True,
            )
        self.assertEqual([e["name"] for e in deletions], ["old.flac"])

    def test_pending_checkpoint_blocks_cleanup(self):
        self.journal.save_resume(
            "old.flac",
            {
                "upload_id": "pending",
                "signature": {},
                "part_bytes": 16,
                "parts": {},
                "phase": "assembling",
                "version": 1,
            },
        )
        with patch("builtins.input") as prompt:
            _, deletions = review_remote(
                Mock(files=[self.entry("old.flac")]),
                [],
                self.journal,
                clean=True,
            )
        self.assertEqual(deletions, [])
        prompt.assert_not_called()

    def test_changed_remote_prevents_delete(self):
        approved = self.entry("old.flac")
        session = Mock()
        session.get_metadata.return_value = {
            "files": [self.entry("old.flac", b"changed")]
        }
        with self.assertRaisesRegex(ValueError, "changed after"):
            delete_originals(session, ID, [approved], self.journal)
        session.send.assert_not_called()

    def test_deletes_are_conditional_and_never_cascade(self):
        approved = self.entry("old.flac")
        session = Mock(access_key="test-key", secret_key="test-secret")
        session.get_metadata.return_value = {"files": [approved]}
        session.send.return_value = response(204)
        with patch(
            "lma_tools.uploader.remote.remote_state",
            return_value=("missing", "HEAD"),
        ):
            delete_originals(session, ID, [approved], self.journal)
        request = session.prepare_request.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.headers["x-archive-cascade-delete"], "0")
        self.assertEqual(request.headers["x-archive-queue-derive"], "0")
        self.assertEqual(
            request.headers["If-Match"], '"' + approved["md5"] + '"'
        )
        self.assertEqual(
            [e["event"] for e in self.journal.events()],
            ["delete_started", "delete_accepted", "delete_verified"],
        )

    def test_cleanup_only_does_not_prompt_to_replace(self):
        path = self.folder / "one.flac"
        path.write_bytes(b"new")
        with patch("builtins.input") as prompt:
            replacements, deletions = review_remote(
                Mock(files=[self.entry("one.flac")]),
                [path],
                self.journal,
                clean=True,
                replacements_only=True,
            )
        self.assertFalse(replacements)
        self.assertFalse(deletions)
        prompt.assert_not_called()

    def test_bad_selection_does_not_delete_anything(self):
        with patch("builtins.input", return_value="99"):
            with self.assertRaisesRegex(ValueError, "Invalid cleanup"):
                review_remote(
                    Mock(files=[self.entry("old.flac")]),
                    [],
                    self.journal,
                    clean=True,
                )
