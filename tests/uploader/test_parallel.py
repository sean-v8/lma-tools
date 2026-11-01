"""Exercise concurrent transfers through real request-building code."""

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import requests
from internetarchive import ArchiveSession
from internetarchive.item import Item

from lma_tools.uploader import cli
from lma_tools.uploader.parallel import run_parallel, upload_workers
from lma_tools.uploader.progress import (
    ProgressBoard,
    TransferSession,
    UploadBody,
    UploadCancelled,
    check_cancelled,
)
from lma_tools.uploader.recovery import RecoveryPolicy, upload_with_recovery
from tests.uploader.test_lma import ID, make_show, response
from tests.views.test_terminal import TtyBuffer


class SchedulerTests(unittest.TestCase):
    def test_bound_and_slot_reuse(self):
        lock = Lock()
        active = set()
        peak = 0
        first = Barrier(3, timeout=3)
        seen = []

        def transfer(index, slot, board, cancel):
            nonlocal peak
            with lock:
                self.assertNotIn(slot, active)
                active.add(slot)
                peak = max(peak, len(active))
                seen.append(index)
            if index < 3:
                first.wait()
            with lock:
                active.remove(slot)

        run_parallel(
            [(i,) for i in range(12)],
            3,
            transfer,
            board=ProgressBoard(io.StringIO()),
        )
        self.assertEqual(peak, 3)
        self.assertEqual(sorted(seen), list(range(12)))

    def test_fatal_failure_stops_scheduling_and_drains_active_work(self):
        started = Event()
        finished = Event()
        seen = []

        def transfer(index, slot, board, cancel):
            seen.append(index)
            if index == 0:
                self.assertTrue(started.wait(3))
                raise ValueError("failed")
            started.set()
            self.assertFalse(cancel.wait(0.01))
            finished.set()

        with self.assertRaisesRegex(ValueError, "failed"):
            run_parallel(
                [(i,) for i in range(6)],
                2,
                transfer,
                board=ProgressBoard(io.StringIO()),
            )
        self.assertEqual(sorted(seen), [0, 1])
        self.assertTrue(finished.is_set())

    def test_ctrl_c_sets_cancel_and_waits_for_worker_exit(self):
        started, finished = Event(), Event()

        def transfer(index, slot, board, cancel):
            started.set()
            try:
                self.assertTrue(cancel.wait(3))
                check_cancelled(cancel)
            finally:
                finished.set()

        def interrupt(*args, **kwargs):
            self.assertTrue(started.wait(3))
            raise KeyboardInterrupt()

        with patch("lma_tools.uploader.parallel.wait", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_parallel(
                    [(1,), (2,)],
                    1,
                    transfer,
                    board=ProgressBoard(io.StringIO()),
                )
        self.assertTrue(finished.is_set())

    def test_worker_setting_validation(self):
        self.assertEqual(upload_workers(3), 3)
        for value in (0, -1, True, 2.5, "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                upload_workers(value)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parent = ArchiveSession(
            config={"s3": {"access": "test", "secret": "test"}}
        )
        self.addCleanup(self.parent.close)
        self.journal = cli.Journal(self.root / "log", ID)
        self.lock = Lock()
        self.active = 0
        self.peak = 0
        self.calls = []
        self.task_requests = []
        self.closed = []
        self.first_wave = set()
        self.barrier = None
        self.fail_name = None
        self.lost = None
        self.counts = {}
        self.sessions = []
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        capture = redirect_stderr(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

        def send(session, request, **kwargs):
            if request.method == "POST":
                self.assertEqual(
                    urlsplit(request.url).path, "/services/tasks.php"
                )
                payload = json.loads(request.body)
                self.assertEqual(payload["cmd"], "derive.php")
                self.assertEqual(payload["identifier"], ID)
                self.assertEqual(self.active, 0)
                self.task_requests.append(payload)
                result = response(content=b'{"success":true}')
                result._content_consumed = True
                return result
            if request.method == "HEAD":
                result = response(404)
                result._content_consumed = True
                return result
            self.assertEqual(request.method, "PUT")
            name = unquote(urlsplit(request.url).path.rsplit("/", 1)[-1])
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.counts[name] = self.counts.get(name, 0) + 1
                self.calls.append((name, session, dict(request.headers)))
                attempt = self.counts[name]
            try:
                if name in self.first_wave:
                    self.barrier.wait(timeout=3)
                content = bytearray()
                while chunk := request.body.read(17):
                    content.extend(chunk)
                self.assertEqual(
                    bytes(content), (self.root / name).read_bytes()
                )
                self.assertEqual(
                    int(request.headers["Content-Length"]), len(content)
                )
                self.assertNotIn("Transfer-Encoding", request.headers)
                if self.lost == name and attempt == 1:
                    raise requests.ReadTimeout("lost acknowledgement")
                result = response(400 if self.fail_name == name else 200)
                result._content_consumed = True
                return result
            finally:
                with self.lock:
                    self.active -= 1

        self.transport = patch("requests.sessions.Session.send", new=send)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        owner = self

        class Session(TransferSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with owner.lock:
                    owner.sessions.append(self)

            def close(self):
                super().close()
                with owner.lock:
                    owner.closed.append(self)

        self.factory = patch.object(cli, "TransferSession", Session)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def files(self, names):
        result = []
        for name in names:
            path = self.root / name
            path.write_bytes((name.encode() + b"-audio") * 64)
            result.append(path)
        return result

    def item(self, exists=True):
        data = {"metadata": {"identifier": ID}, "files": []} if exists else {}
        return Item(self.parent, ID, data)

    def upload(self, files, exists=True, policy=None, tracker=None, workers=3):
        cli.upload_files(
            self.parent,
            self.item(exists),
            files,
            {"title": "Approved title"} if not exists else {},
            self.journal,
            0,
            0,
            recovery=policy or RecoveryPolicy(retries=0),
            workers=workers,
            tracker=tracker,
        )

    def test_all_file_types_overlap_with_independent_closed_sessions(self):
        names = [
            "notes.txt",
            "tracks.ffp",
            "cover.jpg",
            "one.flac",
            "two.flac",
        ]
        self.first_wave = set(names[:3])
        self.barrier = Barrier(3)
        self.upload(self.files(names))
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.calls), 5)
        for name, _, headers in self.calls:
            self.assertEqual(
                headers["x-archive-queue-derive"],
                "1" if name.endswith(".flac") else "0",
            )
        self.assertEqual(len(set(self.sessions)), 5)
        self.assertCountEqual(self.closed, self.sessions)
        self.assertTrue(all(c[1] is not self.parent for c in self.calls))
        self.assertTrue(
            all("x-archive-meta00-title" not in c[2] for c in self.calls)
        )
        self.assertEqual(len(self.journal.events()), 10)

    def test_dashboard_counts_each_concurrent_body_once(self):
        from lma_tools.uploader.dashboard import ShowProgress

        files = self.files(["one.flac", "two.flac", "three.flac"])
        self.first_wave = {p.name for p in files}
        self.barrier = Barrier(3)
        tracker = ShowProgress(ID, files, "test", self.root / "state")
        try:
            self.upload(files, tracker=tracker)
            tracker.publish()
            self.assertEqual(tracker.data["sent"], tracker.data["total"])
            self.assertEqual(tracker.data["confirmed"], tracker.data["total"])
            self.assertEqual(tracker.data["completed"], 3)
            self.assertEqual(tracker.wire, tracker.data["total"])
        finally:
            tracker.close("done")

    def test_sequential_and_single_file_use_local_bars_and_count_once(self):
        from lma_tools.uploader.dashboard import ShowProgress

        for workers, names in (
            (1, ["one.flac", "two.flac"]),
            (3, ["only.flac"]),
        ):
            with self.subTest(workers=workers):
                files = self.files(names)
                stream = TtyBuffer()
                tracker = ShowProgress(ID, files, "test", self.root / "state")
                try:
                    with (
                        patch.dict("os.environ", TERM="xterm"),
                        patch.object(
                            cli,
                            "ProgressBoard",
                            side_effect=lambda **kw: ProgressBoard(
                                stream, **kw
                            ),
                        ),
                    ):
                        self.upload(files, tracker=tracker, workers=workers)
                    tracker.publish()
                    self.assertEqual(tracker.wire, tracker.data["total"])
                    self.assertEqual(tracker.data["completed"], len(files))
                    self.assertIn("B/s", stream.getvalue())
                    self.assertIn("awaiting ack", stream.getvalue())
                    self.assertNotIn("send", self.parent.__dict__)
                finally:
                    tracker.close("done")

    def test_creation_txt_is_confirmed_before_other_files_start(self):
        names = ["notes.txt", "tracks.ffp", "cover.jpg", "one.flac"]
        self.first_wave = set(names[1:])
        self.barrier = Barrier(3)
        self.upload(self.files(names), exists=False)
        self.assertEqual(self.calls[0][0], "notes.txt")
        self.assertIs(self.calls[0][1], self.parent)
        self.assertEqual(
            self.calls[0][2]["x-archive-meta00-title"], "uri(Approved%20title)"
        )
        self.assertTrue(
            all("x-archive-meta00-title" not in c[2] for c in self.calls[1:])
        )
        self.assertEqual(self.peak, 3)

    def test_failed_creation_never_starts_other_files(self):
        self.fail_name = "notes.txt"
        with self.assertRaises(requests.HTTPError):
            self.upload(self.files(["notes.txt", "one.flac"]), exists=False)
        self.assertEqual([c[0] for c in self.calls], ["notes.txt"])
        self.assertEqual(self.sessions, [])

    def test_each_file_recovers_independently_without_duplicate_send(self):
        files = self.files(["one.flac", "two.flac", "three.flac"])
        self.lost = "one.flac"
        self.first_wave = {p.name for p in files}
        self.barrier = Barrier(3)
        with patch(
            "lma_tools.uploader.recovery.remote_state",
            return_value=("match", "metadata"),
        ):
            self.upload(files, policy=RecoveryPolicy(1, 1, 0.001, 0.001))
        self.assertEqual(self.counts["one.flac"], 1)
        success = [
            e for e in self.journal.events() if e["event"] == "upload_ok"
        ]
        self.assertEqual(len(success), 3)
        self.assertEqual(
            next(e for e in success if e["name"] == "one.flac")["verified_by"],
            "metadata",
        )
        self.assertCountEqual(self.closed, self.sessions)

    def run_upload_only(self, workers=None):
        folder = make_show(self.root / "shows")
        files = self.files(["one.flac", "two.flac", "three.flac"])
        for path in files:
            (folder / path.name).write_bytes(path.read_bytes())
        config = self.root / "config.toml"
        config.write_text(
            f'[uploader]\nshows_root = "{folder.parent.parent}"\n'
        )
        item = self.item()
        self.parent.get_item = Mock(return_value=item)
        self.parent.submit_task = Mock(
            side_effect=lambda *a, **k: self.derive()
        )
        self.first_wave = {p.name for p in files} if workers != 1 else set()
        self.barrier = Barrier(3)
        with patch.object(cli, "ArchiveSession", return_value=self.parent):
            with patch.object(
                cli, "audio_runtime", side_effect=AssertionError
            ):
                with patch("builtins.input", return_value="UPLOAD"):
                    options = (
                        [] if workers is None else ["--workers", str(workers)]
                    )
                    return cli.main(
                        ["-u", "--config", str(config), ID, *options]
                    )

    def test_upload_only_derives_after_all_threads_finish_without_metadata(
        self,
    ):
        self.assertEqual(self.run_upload_only(), 0)
        self.parent.submit_task.assert_called_once()
        self.assertEqual(len(self.task_requests), 1)
        self.assertEqual(self.peak, 3)

    def test_workers_one_override_preserves_the_sequential_session(self):
        self.assertEqual(self.run_upload_only(workers=1), 0)
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.sessions, [])
        self.assertTrue(all(c[1] is self.parent for c in self.calls))

    def test_failed_parallel_run_skips_final_derive_and_logs_other_successes(
        self,
    ):
        self.fail_name = "one.flac"
        with self.assertRaises(requests.HTTPError):
            self.run_upload_only()
        self.parent.submit_task.assert_not_called()
        self.assertEqual(self.active, 0)
        self.assertCountEqual(self.closed, self.sessions)

    def test_progress_counts_only_transport_bytes_not_checksum_reads(self):
        stream = TtyBuffer()
        rows = []
        with patch.dict("os.environ", TERM="xterm"):
            board = ProgressBoard(stream)
            original = board.row

            def track(path, slot):
                row = original(path, slot)
                with self.lock:
                    rows.append(row)
                return row

            board.row = track
            with patch(
                "lma_tools.uploader.parallel.ProgressBoard", return_value=board
            ):
                self.upload(self.files(["one.flac", "two.flac", "three.flac"]))
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row.order for row in rows), [1, 2, 3])
        self.assertNotIn("] Checking", stream.getvalue())
        self.assertNotIn(": Complete", stream.getvalue())
        for row in rows:
            self.assertEqual(row.bar.n, row.path.stat().st_size)
        self.assertTrue(all(row.bar.disable for row in rows))

    def derive(self):
        self.assertEqual(self.active, 0)
        self.assertEqual(len(self.calls), 3)
        call = self.parent.submit_task.call_args
        return ArchiveSession.submit_task(
            self.parent, *call.args, **call.kwargs
        )


class ProgressTests(unittest.TestCase):
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
