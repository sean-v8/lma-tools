"""Remote verification must prove content rather than infer task success."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload import audit, cli

from .helpers import ID, metadata_fixture


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "track.flac"
        self.path.write_bytes(b"audio")
        self.session = Mock()
        self.session.get_metadata.return_value = {
            "metadata": {"creator": "Artist", "collection": ["Band", "etree"]}
        }

    def test_audit_confirms_files_and_required_collections(self):
        with patch.object(
            audit, "remote_state", return_value=("match", "HEAD")
        ):
            result = audit.audit_upload(
                self.session,
                ID,
                [self.path],
                {"creator": "Artist", "collection": ["Band", "etree"]},
            )
        self.assertEqual(result, {"files": 1, "fields": 2})
        self.session.submit_task.assert_not_called()

    def test_unknown_and_missing_checksums_never_verify(self):
        for state in ("unknown", "missing"):
            with (
                self.subTest(state=state),
                patch.object(
                    audit, "remote_state", return_value=(state, "HEAD")
                ),
                self.assertRaisesRegex(ValueError, "track.flac"),
            ):
                audit.audit_upload(
                    self.session, ID, [self.path], {}, attempts=1
                )

    def test_metadata_mismatch_retries_then_fails(self):
        with patch.object(audit.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "metadata: creator"):
                audit.audit_upload(
                    self.session, ID, [], {"creator": "Different"}
                )
        self.assertEqual(sleep.call_count, 2)

    def test_metadata_normalization_does_not_hide_real_changes(self):
        self.assertTrue(audit.equivalent("description", "A & B", "A &amp; B"))
        self.assertTrue(
            audit.equivalent("date", "2026-08-17", "2026-08-17T00:00:00Z")
        )
        self.assertFalse(audit.equivalent("creator", "A", "B"))
        self.assertFalse(
            audit.equivalent("collection", ["Band", "etree"], ["Band"])
        )

    def test_summary_distinguishes_missing_matching_changed_and_unknown(self):
        paths = []
        for name in ("upload", "skip", "replace", "check"):
            path = self.path.with_name(name + ".flac")
            path.write_bytes(name.encode())
            paths.append(path)
        evidence = audit.inventory(paths)
        item = Mock(
            files=[
                {"name": "skip.flac", **evidence["skip.flac"]},
                {"name": "replace.flac", "md5": "0" * 32, "size": "1"},
                {"name": "check.flac"},
            ]
        )
        with redirect_stdout(io.StringIO()):
            groups = audit.transfer_summary(item, paths)
        for name in ("upload", "skip", "replace", "check"):
            self.assertEqual(groups[name], [name + ".flac"])

    def test_task_states_are_read_only(self):
        for summary, expected in (
            ({"queued": 1}, "deriving"),
            ({"running": 1}, "deriving"),
            ({"error": 1}, "derive failed"),
            ({"paused": 1}, "derive failed"),
            ({}, "verified"),
        ):
            with self.subTest(summary=summary):
                self.session.get_tasks_summary.return_value = summary
                self.assertEqual(audit.task_status(self.session, ID), expected)
        self.session.submit_task.assert_not_called()

    def test_audit_failure_prevents_final_derive_and_run_success(self):
        with metadata_fixture(self) as (folder, _, session, args):
            with patch.object(
                cli, "audit_upload", side_effect=ValueError("audit")
            ):
                with self.assertRaisesRegex(ValueError, "audit"):
                    cli.main(args)
            session.submit_task.assert_not_called()
            journal = cli.Journal(folder.parent / f"{ID}_lma.log", ID)
            self.assertNotIn("run_ok", [e["event"] for e in journal.events()])

    def test_dry_run_queries_without_editor_prompts_or_writes(self):
        with metadata_fixture(self) as (folder, item, session, args):
            before = {
                p: p.read_bytes()
                for p in folder.parent.rglob("*")
                if p.is_file()
            }
            with (
                patch.object(cli, "edit_review", side_effect=AssertionError),
                patch("builtins.input", side_effect=AssertionError),
            ):
                self.assertEqual(cli.main([*args, "--dry-run"]), 0)
            after = {
                p: p.read_bytes()
                for p in folder.parent.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            session.get_item.assert_called_once()
            item.modify_metadata.assert_not_called()
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()
