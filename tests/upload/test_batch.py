"""Approved batches preserve review boundaries and share transfer slots."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import Mock, patch

from lma_tools.upload import batch, budget, cli, launcher
from lma_tools.upload.audit import inventory

from .helpers import ID, make_show, metadata_fixture


class BatchTests(unittest.TestCase):
    def test_queued_approval_rejects_local_remote_and_metadata_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.flac"
            path.write_bytes(b"audio")
            entry = {"name": path.name, **inventory([path])[path.name]}
            item = Mock(
                exists=True, files=[entry], metadata={"creator": "Artist"}
            )
            plan = dict(
                inventory=inventory([path]),
                existed=True,
                deletions=[],
                remote_files=[dict(entry)],
                remote_metadata=dict(item.metadata),
            )
            batch.validate_approval(plan, item, [path], {"creator": "Artist"})
            for change, error in (
                (lambda: path.write_bytes(b"changed"), "Local files"),
                (lambda: entry.update(md5="0" * 32), "Remote files"),
                (
                    lambda: item.metadata.update(creator="Other"),
                    "Remote metadata",
                ),
            ):
                with self.subTest(error=error):
                    path.write_bytes(b"audio")
                    entry.update(**plan["remote_files"][0])
                    item.metadata = dict(plan["remote_metadata"])
                    change()
                    with self.assertRaisesRegex(ValueError, error):
                        batch.validate_approval(
                            plan, item, [path], {"creator": "Artist"}
                        )

    def test_child_plan_is_private_single_use_and_bypasses_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = {"argv": ["show"], "approval": {"metadata": {}}}
            path = batch.write_plan(directory, payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with (
                patch.dict(os.environ),
                patch.object(cli, "main", return_value=0) as run,
            ):
                self.assertEqual(batch.consume_plan(path), 0)
            run.assert_called_once_with(["show"], approved=payload["approval"])
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".running").exists())
            with self.assertRaises(FileNotFoundError):
                batch.consume_plan(path)

    def test_insecure_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = batch.write_plan(Path(temporary), {})
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                batch.consume_plan(path)

    def test_review_enqueues_without_writing_to_archive(self):
        with metadata_fixture(self) as (_, item, session, args):
            plans = []
            self.assertEqual(cli.main(args, enqueue=plans.append), 0)
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0]["identifier"], ID)
            self.assertTrue(plans[0]["inventory"])
            item.modify_metadata.assert_not_called()
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_approved_child_executes_without_another_editor_or_prompt(self):
        with metadata_fixture(self) as (folder, item, session, args):
            plans = []
            self.assertEqual(cli.main(args, enqueue=plans.append), 0)
            approval = json.loads(json.dumps(plans[0]))
            session.get_item.side_effect = [
                item,
                Mock(exists=True, metadata={"mediatype": "collection"}),
            ]
            with (
                patch("builtins.input", side_effect=AssertionError("prompt")),
                patch.object(cli, "edit_review", side_effect=AssertionError),
                patch(
                    "lma_tools.upload.prepare.prepare_show",
                    side_effect=AssertionError("prepare"),
                ),
            ):
                self.assertEqual(cli.main(args, approved=approval), 0)
            self.assertEqual(item.upload_file.call_count, 3)
            session.submit_task.assert_called_once()
            journal = cli.Journal(folder.parent / f"{ID}_lma.log", ID)
            self.assertTrue(
                any(e["event"] == "audit_ok" for e in journal.events())
            )

    def test_no_argument_prepares_all_outputs_using_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_show(root)
            second = make_show(root, "other", "other2026-08-18")
            config = root / "config.toml"
            config.write_text(f'[upload]\nshows_root = "{root}"\n')
            with (
                patch(
                    "lma_tools.upload.prepare.prepare_show", return_value={}
                ) as prepare,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["-p", "--config", str(config)]), 0)
            self.assertEqual(
                {call.args[0] for call in prepare.call_args_list},
                {first, second},
            )

    def test_starts_first_approved_upload_before_reviewing_second(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = [
                make_show(root),
                make_show(root, "other", "other2026-08-18"),
            ]
            events = []
            args = cli.parser().parse_args([])
            args.config = root / "config.toml"
            args.config.write_text(f'[upload]\nshows_root = "{root}"\n')

            def review(argv, **kwargs):
                events.append("review")
                kwargs["enqueue"]({"identifier": Path(argv[-1]).name})
                return 0

            with (
                patch.object(cli, "main", side_effect=review),
                patch.object(
                    cli,
                    "scan_outputs",
                    return_value=[(p.parent, p) for p in outputs],
                ),
                patch.object(launcher, "ensure_dashboard"),
                patch.object(
                    launcher,
                    "start_window",
                    side_effect=lambda *a, **kw: events.append("start"),
                ),
                patch.object(
                    batch, "state_directory", return_value=root / "state"
                ),
                patch.object(batch.shutil, "which", return_value="/tmux"),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    batch.run_batch([], args, {"shows_root": root}), 0
                )
            self.assertEqual(events, ["review", "start", "review", "start"])
            plans = list((root / "state").glob("batch-*/approved-*.json"))
            self.assertEqual(len(plans), 2)
            self.assertTrue(
                all(json.loads(p.read_text())["argv"] for p in plans)
            )

    def test_slots_bound_concurrent_transfers_and_cancel_waiters(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for index in range(2):
                (directory / f"slot-{index}").touch()
            lock = Lock()
            counts = {"active": 0, "peak": 0}

            def work():
                with budget.transfer_slot():
                    with lock:
                        counts["active"] += 1
                        counts["peak"] = max(counts["peak"], counts["active"])
                    time.sleep(0.02)
                    with lock:
                        counts["active"] -= 1

            with patch.dict(os.environ, LMA_UPLOAD_BATCH_SLOTS=str(directory)):
                threads = [Thread(target=work) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(counts["peak"], 2)
                cancel = Event()
                cancel.set()
                with self.assertRaises(Exception):
                    with budget.transfer_slot(cancel):
                        self.fail("Cancelled transfer acquired a slot")
