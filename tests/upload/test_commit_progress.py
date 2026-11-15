"""Byte progress for preparation rollback copies and source checks."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload.prepare import _backup_and_check


class CommitProgressTests(unittest.TestCase):
    def test_counts_chunks_and_preserves_backup_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / "backup"
            backup.mkdir()
            source = directory / "track.wav"
            data = b"audio" * 300000
            source.write_bytes(data)
            source.chmod(0o640)
            bars = []

            def progress(label, paths):
                bar = Mock()
                bar.__enter__ = Mock(return_value=bar)
                bar.__exit__ = Mock(return_value=False)
                bars.append((label, sum(p.stat().st_size for p in paths), bar))
                return bar

            with patch(
                "lma_tools.upload.prepare._commit_progress",
                side_effect=progress,
            ):
                existing = _backup_and_check(
                    directory,
                    backup,
                    {"track.wav", "new.flac"},
                    {"track.wav": hashlib.md5(data).hexdigest()},
                )
            self.assertEqual(existing, {"track.wav"})
            self.assertEqual((backup / source.name).read_bytes(), data)
            self.assertEqual(
                (backup / source.name).stat().st_mode, source.stat().st_mode
            )
            self.assertEqual(len(bars), 2)
            for _, total, bar in bars:
                self.assertEqual(total, len(data))
                chunks = [call.args[0] for call in bar.update.call_args_list]
                self.assertGreater(len(chunks), 1)
                self.assertEqual(sum(chunks), len(data))
                bar.__exit__.assert_called_once()

    def test_changed_source_aborts_before_originals_are_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / "backup"
            backup.mkdir()
            source = directory / "track.wav"
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "Source changed"):
                _backup_and_check(
                    directory,
                    backup,
                    {source.name},
                    {source.name: hashlib.md5(b"original").hexdigest()},
                )
            self.assertEqual(source.read_bytes(), b"changed")
            self.assertEqual((backup / source.name).read_bytes(), b"changed")
