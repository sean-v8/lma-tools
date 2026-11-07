"""Whole-track parallel preparation and worker limit validation."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from lma_tools.uploader.prepare import (
    _prepare_tracks,
    preparation_workers,
)


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
            "lma_tools.uploader.prepare.ThreadPoolExecutor",
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
