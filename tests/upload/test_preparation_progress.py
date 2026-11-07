"""Local percentage bars and reference FLAC progress parsing."""

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload.encoder import _encode_progress
from lma_tools.upload.preparation_progress import PreparationProgress
from tests.report.test_terminal import TtyBuffer


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
