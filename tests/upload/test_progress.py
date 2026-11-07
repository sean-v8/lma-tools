"""Aligned progress, total accounting and transient warnings."""

import io
import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests
from requests import Request, Session

from lma_tools.upload import cli
from lma_tools.upload.progress import (
    OrderedProgress,
    ProgressBoard,
    SharedColumns,
    TransferSession,
    UploadBody,
    UploadCancelled,
    watch_transfer,
)
from lma_tools.upload.recovery import RecoveryPolicy, upload_with_recovery
from tests.helpers import TtyBuffer
from tests.upload.helpers import ID, response


class ColumnTests(unittest.TestCase):
    def assert_aligned(self, bars, width):
        lines = [str(bar) for bar in bars]
        self.assertEqual(len({line.index("|") for line in lines}), 1)
        self.assertEqual(len({line.rindex("|") for line in lines}), 1)
        self.assertEqual(len({len(line) for line in lines}), 1)
        for line in lines:
            self.assertLessEqual(len(line), width)
            self.assertIn("B/s", line)
            self.assertTrue(line.endswith("]"), line)
        return lines

    def test_file_rows_share_columns_across_metrics_and_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(TtyBuffer())
                try:
                    for index, name in enumerate(
                        [
                            "short.flac",
                            "artist2026-01-01.long.s1t01.flac",
                            "another.s1t02.flac",
                        ]
                    ):
                        path = Path(tmp) / name
                        with path.open("wb") as handle:
                            handle.truncate(1024 ** (index + 1))
                        row = board.row(path, index)
                        rows.append(row)
                        row.bar.dynamic_ncols = None
                        row.bar.ncols = 160
                        row.bar.n = 123 * (index + 1)
                        row.bar._ema_dn = lambda n=index: 10 ** (n + 1)
                        row.bar._ema_dt = lambda: 1
                    board._redraw()
                    bars = [row.bar for row in rows]
                    lines = self.assert_aligned(bars, 160)
                    self.assertIn("short.flac ", lines[0])
                    rows[1].status("awaiting ack")
                    self.assert_aligned(bars, 160)
                    for bar in bars:
                        bar.ncols = 80
                    board._redraw()
                    self.assert_aligned(bars, 80)
                    rows[1].close()
                    self.assert_aligned([rows[0].bar, rows[2].bar], 80)
                finally:
                    for row in rows:
                        row.close()

    def test_resume_counts_checkpointed_bytes_without_an_inflated_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"x" * 100)
            with patch.dict("os.environ", TERM="xterm"):
                row = ProgressBoard(TtyBuffer()).row(path, 1)
                try:
                    row.resume(80)
                    values = row.bar.format_dict
                    self.assertEqual(values["n"], 80)
                    self.assertEqual(values["upload_rate"], "0.00 B/s")
                    row.advance(10)
                    self.assertEqual(row.bar.n, 90)
                finally:
                    row.close()

    def test_dashboard_rows_use_longest_label_and_same_bar_width(self):
        bars = []
        columns = SharedColumns(timing=False)
        try:
            for index, name in enumerate(
                [
                    "short2026-01-01",
                    "longer-artist2026-01-01.recording",
                    "third2026-01-02",
                ]
            ):
                bar = OrderedProgress(
                    total=1024 ** (index + 1),
                    desc=name,
                    file=TtyBuffer(),
                    ncols=140,
                    delay=1,
                )
                bar.n = index + 1
                bar.set_postfix_str(
                    f"{index}/12 confirmed, uploading", refresh=False
                )
                bars.append(bar)
            columns.prepare(bars)
            lines = self.assert_aligned(bars, 140)
            self.assertEqual(lines[0].index("|"), len(bars[1].desc) + 5)
            bars[-1].set_description_str("a-much-longer-show2026-01-02", False)
            columns.prepare(bars)
            self.assert_aligned(bars, 140)
        finally:
            for bar in bars:
                bar.close()


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
        with patch("lma_tools.upload.progress.monotonic", return_value=20):
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
            with patch.object(board.separator, "display") as separator:
                board._redraw()
            separator.assert_called_once_with(msg="-" * (width - 1), pos=1)
            lines = [str(bar) for bar in bars]
            self.assertEqual([abs(bar.pos) for bar in bars], [0, 2, 3])
            self.assertEqual(len({line.index("|") for line in lines}), 1)
            self.assertEqual(len({line.rindex("|") for line in lines}), 1)
            self.assertTrue(all(len(line) < width for line in lines))
            board.message("Keep this status")
        rows[0].close()
        self.assertEqual(abs(rows[1].bar.pos), 2)
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

    def test_optional_file_inventory_controls_total_creation(self):
        legacy = ProgressBoard(TtyBuffer())
        self.addCleanup(legacy.close)
        self.assertIsNone(legacy.total)
        board = ProgressBoard(TtyBuffer(), files=[])
        self.addCleanup(board.close)
        self.assertEqual(board.total.total, 0)


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


class ProgressTests(unittest.TestCase):
    def test_resize_rebuilds_grid_before_tqdm_can_pad_to_old_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("long-band-name-" * 6 + ".flac")
            path.write_bytes(b"audio-data")
            stream = TtyBuffer()
            with patch.dict("os.environ", TERM="xterm"):
                row = ProgressBoard(stream).row(path, 0)
                try:
                    row.bar.dynamic_ncols = None
                    for width in (160, 80, 160, 80):
                        row.bar.ncols = width
                        stream.seek(0)
                        stream.truncate()
                        row.bar.refresh()
                        text = stream.getvalue()
                        self.assertIn("\x1b[J", text)
                        self.assertNotIn("\x1b[2J", text)
                        self.assertNotIn("\x1b[H", text)
                        plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
                        for line in plain.split("\r"):
                            self.assertLess(len(line), width)
                finally:
                    row.close()

    def test_transport_reads_count_bytes_and_cooperate_with_cancel(self):
        row = Mock()
        stop = Event()
        body = UploadBody(io.BytesIO(b"abcdef"), row, stop)
        self.assertEqual(body.read(4), b"abcd")
        self.assertEqual(body.read(), b"ef")
        self.assertEqual(sum(c.args[0] for c in row.advance.call_args_list), 6)
        stop.set()
        with self.assertRaises(UploadCancelled):
            body.read()

    def test_redirected_put_resets_progress_without_wrapping_twice(self):
        row = Mock()
        session = TransferSession(
            row, Event(), config={"s3": {"access": "test", "secret": "test"}}
        )
        self.addCleanup(session.close)
        request = requests.Request(
            "PUT",
            "https://s3.us.archive.org/item/file",
            data=io.BytesIO(b"data"),
        ).prepare()
        original = request.body
        calls = []

        def transport(active, prepared, **kwargs):
            calls.append(prepared.url)
            if len(calls) == 1:
                redirected = prepared.copy()
                redirected.url = "https://alternate.archive.org/item/file"
                return active.send(redirected)
            self.assertEqual(prepared.body.read(), b"data")
            return response()

        with patch("requests.sessions.Session.send", new=transport):
            session.send(request)
        self.assertIs(request.body, original)
        self.assertEqual(row.begin.call_count, 2)
        self.assertEqual(sum(c.args[0] for c in row.advance.call_args_list), 4)

    def test_bars_use_local_byte_speed_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio-data")
            stream = TtyBuffer()
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(stream)
                rows = [board.row(path, slot) for slot in (0, 1)]
                try:
                    self.assertEqual([abs(r.bar.pos) for r in rows], [0, 1])
                    rows[0].begin()
                    rows[0].advance(10)
                    self.assertEqual(rows[0].bar.n, 10)
                    self.assertEqual(
                        rows[0].bar.format_dict["uploaded"], "10.00 B"
                    )
                    self.assertIn(
                        "B/s", rows[0].bar.format_dict["upload_rate"]
                    )
                    self.assertIn("awaiting ack", rows[0].bar.postfix)
                    rows[0].begin()
                    self.assertEqual(rows[0].bar.n, 0)
                finally:
                    for row in rows:
                        row.close()
            self.assertIn("one.flac", stream.getvalue())

    def test_active_rows_sort_by_file_order_and_close_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio")
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(TtyBuffer())
                third = board.row(path, 3)
                first = board.row(path, 1)
                second = board.row(path, 2)
                try:
                    self.assertEqual(board.rows, [first, second, third])
                    self.assertEqual(
                        [abs(row.bar.pos) for row in board.rows], [0, 1, 2]
                    )
                    second.close()
                    fourth = board.row(path, 4)
                    self.assertEqual(board.rows, [first, third, fourth])
                    self.assertEqual(
                        [abs(row.bar.pos) for row in board.rows], [0, 1, 2]
                    )
                    fourth.close()
                finally:
                    first.close()
                    second.close()
                    third.close()
                self.assertEqual(board.rows, [])

    def test_long_names_keep_rates_visible_on_narrow_terminals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("band2026-01-01." * 9 + "s1t01.flac")
            path.write_bytes(b"audio-data")
            with patch.dict("os.environ", TERM="xterm"):
                row = ProgressBoard(TtyBuffer()).row(path, 1)
                try:
                    row.bar.dynamic_ncols = None
                    row.bar.ncols = 80
                    for state in ("checking", "", "verifying", "awaiting ack"):
                        row.status(state)
                        text = str(row.bar)
                        self.assertLessEqual(len(text), 80)
                        self.assertIn("B/s", text)
                        self.assertIn("s1t01.flac", text)
                        self.assertIn("…", text)
                finally:
                    row.close()

    def test_tty_finishing_does_not_append_duplicate_status_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio")
            stream = TtyBuffer()
            with patch.dict("os.environ", TERM="xterm"):
                row = ProgressBoard(stream).row(path, 1)
                try:
                    row.finish("complete")
                    self.assertEqual(row.bar.postfix, "complete")
                    self.assertNotIn("one.flac: Complete", stream.getvalue())
                    row.message("Warning: remote confirmation delayed")
                    self.assertIn(
                        "Warning: remote confirmation", stream.getvalue()
                    )
                finally:
                    row.close()

    def test_non_tty_finishing_retains_plain_status_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio")
            stream = io.StringIO()
            row = ProgressBoard(stream).row(path, 1)
            row.finish("skipped")
            row.close()
            self.assertEqual(stream.getvalue(), "one.flac: Skipped\n")

    def test_redirected_progress_contains_no_cursor_escape_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio")
            stream = io.StringIO()
            board = ProgressBoard(stream)
            row = board.row(path, 0)
            row.begin()
            row.advance(5)
            row.message("Complete")
            row.close()
            self.assertNotIn("\033", stream.getvalue())

    def test_cancelling_recovery_interrupts_a_long_wait_without_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.flac"
            path.write_bytes(b"audio")
            digest, size, stamp = cli.file_md5(path)
            cancel, waiting = Event(), Event()
            send = Mock(side_effect=requests.ReadTimeout("lost"))
            session = Mock()

            def notify(text, **kwargs):
                if "Waiting" in text:
                    waiting.set()

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    upload_with_recovery,
                    send,
                    session,
                    ID,
                    path,
                    digest,
                    size,
                    stamp,
                    Mock(),
                    RecoveryPolicy(delay=60),
                    notify=notify,
                    cancel=cancel,
                )
                self.assertTrue(waiting.wait(3))
                cancel.set()
                with self.assertRaises(UploadCancelled):
                    future.result(timeout=3)
            send.assert_called_once()
            session.get_metadata.assert_not_called()

    def test_concurrent_journal_records_are_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = cli.Journal(Path(tmp) / "log", ID)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(journal.append, "test", index=i)
                    for i in range(80)
                ]
                for future in futures:
                    future.result(timeout=5)
            lines = [
                line for line in journal.path.read_text().splitlines() if line
            ]
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                sorted(r["index"] for r in records), list(range(80))
            )
