"""Local show totals share the file grid without dashboard coupling."""

import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from requests import Request, Session

from lma_tools.uploader.progress import ProgressBoard, watch_transfer
from tests.views.test_terminal import TtyBuffer


class ShowTotalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.files = [Path(temporary.name) / name for name in ("a", "b")]
        for path in self.files:
            path.write_bytes(b"x" * 100)
        environment = patch.dict("os.environ", TERM="xterm")
        environment.start()
        self.addCleanup(environment.stop)

    def board(self, stream=None, **kwargs):
        board = ProgressBoard(
            stream if stream is not None else TtyBuffer(),
            files=self.files,
            **kwargs,
        )
        self.addCleanup(board.close)
        return board

    def test_retries_reset_only_unconfirmed_progress(self):
        board = self.board()
        first = board.row(self.files[0], 0)
        second = board.row(self.files[1], 1)
        first.begin()
        first.advance(80)
        board.complete("b")
        second.close()
        self.assertEqual(board.total.n, 180)
        first.begin()
        self.assertEqual(board.total.n, 100)
        first.advance(150)
        self.assertEqual(board.total.n, 200)
        self.assertEqual(board.wire, 230)
        board.complete("a")
        board.complete("a")
        first.begin()
        first.advance(1)
        first.close()
        self.assertEqual(board.total.n, 200)
        self.assertEqual(board.rows, [])
        self.assertFalse(board.total.disable)
        board.close()
        board.close()
        self.assertTrue(board.total.disable)

    def test_resume_and_skips_never_inflate_wire_rate(self):
        board = self.board()
        row = board.row(self.files[0], 0)
        board.started = 10
        with patch("lma_tools.uploader.progress.monotonic", return_value=20):
            row.resume(80)
            board.complete("b")
            self.assertEqual(board.total.n, 180)
            self.assertEqual(board.total.format_dict["rate"], 0)
            row.advance(10)
            self.assertEqual(board.total.n, 190)
            self.assertEqual(board.total.format_dict["rate"], 1)
            row.resume(90)
            self.assertEqual(board.wire, 10)
            row.begin()
            self.assertEqual(row.bar._resume_offset, 0)
            self.assertEqual(board.total.n, 100)

    def test_total_and_rows_share_columns_and_survive_resize_messages(self):
        board = self.board()
        rows = [board.row(path, i) for i, path in enumerate(self.files)]
        bars = [board.total, *(row.bar for row in rows)]
        for width in (160, 80, 120):
            for bar in bars:
                bar.dynamic_ncols = None
                bar.ncols = width
            board._redraw()
            lines = [str(bar) for bar in bars]
            self.assertEqual([abs(bar.pos) for bar in bars], [0, 1, 2])
            self.assertEqual(len({line.index("|") for line in lines}), 1)
            self.assertEqual(len({line.rindex("|") for line in lines}), 1)
            self.assertTrue(all(len(line) < width for line in lines))
            board.message("Keep this status")
        rows[0].close()
        self.assertEqual(abs(rows[1].bar.pos), 1)
        self.assertIn("Keep this status", board.stream.getvalue())
        self.assertNotIn("\x1b[J", board.stream.getvalue())
        self.assertNotIn("\x1b[2J", board.stream.getvalue())

    def test_disabled_output_stays_plain_and_accounts_locally(self):
        for stream in (io.StringIO(), TtyBuffer()):
            with patch.dict("os.environ", TERM="dumb"):
                board = self.board(stream)
            row = board.row(self.files[0], 0)
            row.begin()
            row.resume(40)
            row.advance(10)
            row.finish("complete")
            board.complete("a")
            board.close()
            self.assertIsNone(board.total)
            self.assertEqual(board.confirmed, {"a"})
            self.assertEqual(board.wire, 10)
            self.assertEqual(stream.getvalue(), "a: Complete\n")

    def test_transport_observer_counts_once_and_restores_body(self):
        tracker = Mock()
        board = self.board(tracker=tracker)
        row = board.row(self.files[0], 0)
        session = Session()
        self.addCleanup(session.close)
        body = io.BytesIO(b"x" * 100)
        request = Request("PUT", "https://example.org/a", data=body).prepare()
        session.send = Mock(side_effect=lambda req: req.body.read())
        original = session.send
        with watch_transfer(session, self.files[0], tracker, row):
            self.assertEqual(session.send(request), b"x" * 100)
        self.assertIs(session.send, original)
        self.assertIs(request.body, body)
        self.assertEqual(board.total.n, 100)
        self.assertEqual(board.wire, 100)
        tracker.begin.assert_called_once_with("a")
        tracker.advance.assert_called_once_with("a", 100)
        board.complete("a")
        tracker.complete.assert_not_called()

    def test_concurrent_updates_and_refreshes_do_not_deadlock(self):
        board = self.board()
        rows = [board.row(path, i) for i, path in enumerate(self.files)]

        def upload(row):
            row.begin()
            for _ in range(100):
                row.advance(1)
            board.complete(row.path.name)
            row.close()

        def refresh():
            for _ in range(100):
                with board.lock:
                    board.total.refresh()

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(upload, row) for row in rows]
            futures.append(pool.submit(refresh))
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(board.total.n, 200)
        self.assertEqual(board.wire, 200)

    def test_legacy_api_has_no_total_and_empty_selection_has_one(self):
        legacy = ProgressBoard(TtyBuffer())
        self.addCleanup(legacy.close)
        self.assertIsNone(legacy.total)
        board = ProgressBoard(TtyBuffer(), files=[])
        self.addCleanup(board.close)
        self.assertEqual(board.total.total, 0)
