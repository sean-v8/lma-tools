"""Preparation worker limits and verified progress."""

import io
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload.encoder import _encode_progress
from lma_tools.upload.preparation_progress import PreparationProgress
from lma_tools.upload.prepare import (
    _prepare_tracks,
    preparation_workers,
)
from tests.helpers import TtyBuffer


class PreparationWorkerTests(unittest.TestCase):
    def test_default_uses_available_logical_cpus(self):
        with patch.object(
            os, "process_cpu_count", return_value=16, create=True
        ):
            self.assertEqual(preparation_workers(), 16)
        with patch.object(
            os, "process_cpu_count", return_value=None, create=True
        ):
            self.assertEqual(preparation_workers(), 1)

    def test_explicit_limit_and_invalid_values(self):
        self.assertEqual(preparation_workers(16), 16)
        for value in (0, -1, True, "16", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preparation_workers(value)

    def test_sixteen_whole_tracks_can_run_together(self):
        barrier = threading.Barrier(16, timeout=10)
        seen = []
        lock = threading.Lock()

        def prepare(index, track):
            with lock:
                seen.append((index, track))
            barrier.wait()

        _prepare_tracks(list(range(16)), 16, prepare)
        self.assertEqual(sorted(seen), list(enumerate(range(16), 1)))

    def test_pool_is_capped_by_track_count(self):
        with patch(
            "lma_tools.upload.prepare.ThreadPoolExecutor",
            wraps=ThreadPoolExecutor,
        ) as pool:
            _prepare_tracks(["one", "two"], 16, lambda *args: None)
        self.assertEqual(pool.call_args.kwargs["max_workers"], 2)

    def test_serial_failure_does_not_start_later_tracks(self):
        seen = []

        def prepare(index, track):
            seen.append(track)
            raise ValueError("encoding failed")

        with self.assertRaisesRegex(ValueError, "encoding failed"):
            _prepare_tracks(["one", "two"], 1, prepare)
        self.assertEqual(seen, ["one"])

    def test_failure_drains_running_jobs_before_returning(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        timer = threading.Timer(0.1, release.set)

        def prepare(index, track):
            if track == "fail":
                if not started.wait(5):
                    raise AssertionError("Second worker did not start")
                timer.start()
                raise ValueError("encoding failed")
            started.set()
            if not release.wait(5):
                raise AssertionError("Running worker was not released")
            finished.set()

        try:
            with self.assertRaisesRegex(ValueError, "encoding failed"):
                _prepare_tracks(["fail", "finish"], 2, prepare)
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            timer.cancel()


class PreparationProgressTests(unittest.TestCase):
    def test_real_percentages_and_verified_total_use_reusable_rows(self):
        with patch.dict("os.environ", TERM="xterm"):
            board = PreparationProgress(
                [{"destination": "one.flac"}, {"destination": "two.flac"}],
                1,
                stream=TtyBuffer(),
            )
        try:
            with board.track(1, "one.flac") as update:
                update(50)
                self.assertEqual(board.total.n, 0)
                update(100, "Tagging")
                self.assertEqual(board.total.n, 0)
                update(100, "Verified")
                self.assertEqual(board.total.n, 100)
                update(100, "Verified")
                self.assertEqual(board.total.n, 100)
            self.assertEqual(board.slots, [2])
            with board.track(2, "two.flac") as update:
                update(100, "Verified")
            self.assertEqual(board.total.n, 200)
        finally:
            board.close()

    def test_non_terminal_never_emits_control_sequences(self):
        stream = io.StringIO()
        board = PreparationProgress(
            [{"destination": "one.flac"}], 1, stream=stream
        )
        with board.track(1, "one.flac") as update:
            update(100, "Verified")
        board.close()
        self.assertEqual(stream.getvalue(), "")

    def process(self, code=0):
        process = Mock()
        process.stdin = io.BytesIO()
        process.returncode = code
        process.wait.return_value = code
        process.poll.return_value = code
        manager = Mock()
        manager.__enter__ = Mock(return_value=process)
        manager.__exit__ = Mock(return_value=False)
        return manager

    def test_encoder_input_percentages_are_monotonic_and_completed(self):
        seen = []
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            source.write_bytes(b"x" * (1536 * 1024))
            with patch(
                "lma_tools.upload.encoder.subprocess.Popen",
                return_value=self.process(),
            ) as start:
                _encode_progress(["flac", str(source)], source, seen.append)
        self.assertEqual(len(seen), 3)
        self.assertAlmostEqual(seen[0], 100 * 2 / 3)
        self.assertEqual(seen[-2:], [99, 100])
        self.assertEqual(start.call_args.args[0][-1], "-")

    def test_encoder_failure_does_not_report_success(self):
        seen = []
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            source.write_bytes(b"input")
            with patch(
                "lma_tools.upload.encoder.subprocess.Popen",
                return_value=self.process(1),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    _encode_progress(
                        ["flac", str(source)], source, seen.append
                    )
        self.assertEqual(seen, [99])
