"""Retry warnings stay in live rows instead of leaving stale scrollback."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload import cli
from lma_tools.upload.progress import ProgressBoard
from tests.report.test_terminal import TtyBuffer


class TransientWarningTests(unittest.TestCase):
    def test_interactive_warning_is_replaced_by_transfer_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.flac"
            path.write_bytes(b"x" * 100)
            stream = TtyBuffer()
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(stream=stream, files=[path])
            try:
                row = board.row(path, 1)
                row.status("waiting for item")
                row.message("Multipart initiate: NoSuchBucket", transient=True)
                self.assertNotIn("NoSuchBucket", stream.getvalue())
                row.status("part 1/2")
                self.assertNotIn("waiting for item", str(row.bar))
                self.assertEqual(len(board.columns.bars), 2)
                self.assertIs(board.columns.bars[0], board.total)
            finally:
                board.close()

    def test_plain_output_retains_warning_for_redirected_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.flac"
            path.write_bytes(b"x")
            stream = io.StringIO()
            board = ProgressBoard(stream=stream, files=[path])
            try:
                row = board.row(path, 1)
                row.message("Retry details", transient=True)
            finally:
                board.close()
            self.assertIn("track.flac: Retry details", stream.getvalue())

    def test_first_transfer_status_uses_the_live_board(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "notes.txt"
            path.write_text("show")
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(stream=TtyBuffer(), files=[path])
            journal = Mock()
            journal.resume_state.return_value = None
            item = Mock(identifier="artist2026-11-07", exists=True, files=[])
            output = io.StringIO()
            with (
                patch.object(cli, "ProgressBoard", return_value=board),
                patch.object(cli, "corroborate_log", return_value=False),
                patch.object(cli, "upload_with_recovery", return_value={}),
                patch.object(board, "message", wraps=board.message) as notify,
                redirect_stdout(output),
            ):
                cli.upload_files(Mock(), item, [path], {}, journal, 0, 0)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("Checking notes.txt", notify.call_args.args[0])
            self.assertTrue(board.closed)
            self.assertTrue(board.separator.disable)
