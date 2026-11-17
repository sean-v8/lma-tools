"""Preparation timestamps follow musical order without changing contents."""

import hashlib
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from lma_tools.upload.prepare import (
    _order_track_times,
    apply_plan,
    build_plan,
    validate_manifests,
)


class TrackTimeTests(unittest.TestCase):
    def test_disc_track_order_preserves_bytes_and_access_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            tracks = [
                dict(destination="encore.flac", disc=2, track="1/3"),
                dict(destination="second.flac", disc=1, track="2/3"),
                dict(destination="first.flac", disc=1, track="1/3"),
            ]
            for track in tracks:
                path = stage / track["destination"]
                path.write_bytes(b"unchanged audio")
                os.utime(path, ns=(10_000_000_000, 20_000_000_000))
            artwork = stage / "cover.jpg"
            artwork.write_bytes(b"art")
            art_time = artwork.stat().st_mtime_ns
            finished = 100_000_000_000
            with patch(
                "lma_tools.upload.prepare.time_ns", return_value=finished
            ):
                _order_track_times(stage, tracks)
            for index, name in enumerate(
                ("first.flac", "second.flac", "encore.flac")
            ):
                path = stage / name
                self.assertEqual(
                    path.stat().st_mtime_ns,
                    finished - (2 - index) * 1_000_000_000,
                )
                self.assertEqual(path.stat().st_atime_ns, 10_000_000_000)
                self.assertEqual(path.read_bytes(), b"unchanged audio")
            self.assertEqual(artwork.stat().st_mtime_ns, art_time)


@unittest.skipUnless(shutil.which("flac"), "reference flac required")
class PreparedTrackTimeTests(unittest.TestCase):
    def test_one_retag_orders_all_tracks_without_changing_other_checksums(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            show = Path(temporary) / "artist2026-11-07"
            show.mkdir()
            (show / "notes.txt").write_text(
                "Artist\nNovember 07, 2026\nVenue\nCity\n"
                "\n01. First\n02. Second\n"
            )
            for name in ("01-First.wav", "02-Second.wav"):
                with wave.open(str(show / name), "wb") as audio:
                    audio.setparams((2, 3, 48000, 0, "NONE", "none"))
                    audio.writeframes(b"\x01\x02\x03" * 8000)
            plan = build_plan(show, {"genre": "Jazz"})
            apply_plan(show, plan)
            plan = build_plan(show, {"genre": "Jazz"})
            tracks = [show / row["destination"] for row in plan["tracks"]]
            before = [
                hashlib.md5(path.read_bytes()).hexdigest() for path in tracks
            ]
            plan["tracks"][0]["title"] += " revised"
            finished = 200_000_000_000
            with patch(
                "lma_tools.upload.prepare.time_ns", return_value=finished
            ):
                apply_plan(show, plan)
            after = [
                hashlib.md5(path.read_bytes()).hexdigest() for path in tracks
            ]
            self.assertNotEqual(before[0], after[0])
            self.assertEqual(before[1], after[1])
            stamps = [path.stat().st_mtime_ns for path in tracks]
            self.assertEqual(stamps, [finished - 1_000_000_000, finished])
            self.assertTrue(validate_manifests(show)["valid"])
            with patch("lma_tools.upload.prepare._order_track_times") as order:
                apply_plan(show, plan)
            order.assert_not_called()
            self.assertEqual(
                [path.stat().st_mtime_ns for path in tracks], stamps
            )
