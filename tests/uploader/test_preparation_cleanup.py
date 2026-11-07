"""Manifest-only repair and preparation events in the upload journal."""

import contextlib
import io
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from lma_tools.uploader.cli import Journal
from lma_tools.uploader.prepare import (
    apply_plan,
    build_plan,
    prepare_show,
    validate_manifests,
)


@unittest.skipUnless(shutil.which("flac"), "reference flac required")
class PreparationCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.show = self.project / "artist2026-11-07"
        self.show.mkdir()
        self.notes = self.show / "notes.txt"
        self.notes.write_text(
            "Artist\nNovember 07, 2026\nVenue\nCity\n\n01. Song\n"
        )
        self.wav = self.show / "Song.wav"
        with wave.open(str(self.wav), "wb") as audio:
            audio.setparams((2, 3, 48000, 0, "NONE", "none"))
            audio.writeframes(b"\x01\x02\x03" * 8000)
        self.log = self.project / (self.show.name + "_lma.log")
        self.journal = Journal(self.log, self.show.name)
        self.config = {"genre": "Jazz"}

    def prepare(self):
        plan = build_plan(self.show, self.config)
        with patch(
            "lma_tools.uploader.prepare.ui.edit_document", return_value=plan
        ):
            return prepare_show(self.show, self.config, journal=self.journal)

    def test_changed_notes_or_missing_md5_never_stage_audio(self):
        plan = self.prepare()
        flac = self.show / plan["tracks"][0]["destination"]
        ffp = self.show / (self.show.name + ".ffp")
        md5 = self.show / (self.show.name + ".md5")
        stamps = {
            p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
            for p in (flac, ffp)
        }
        for remove in (False, True):
            with self.subTest(missing_md5=remove):
                self.notes.write_text(self.notes.read_text() + "\nNote\n")
                if remove:
                    md5.unlink()
                with (
                    patch("lma_tools.uploader.prepare.encode_verbatim") as enc,
                    patch("lma_tools.uploader.prepare.verify_pcm") as verify,
                    patch("lma_tools.uploader.prepare.tag_audio") as tag,
                ):
                    self.prepare()
                enc.assert_not_called()
                verify.assert_not_called()
                tag.assert_not_called()
                for path, stamp in stamps.items():
                    self.assertEqual(
                        (
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                            path.read_bytes(),
                        ),
                        stamp,
                    )
                self.assertTrue(validate_manifests(self.show)["valid"])
        self.assertEqual(self.journal.events()[-2]["changed"], [md5.name])

    def test_logs_staging_then_commit_without_upload_acknowledgements(self):
        self.journal.append("existing_event")
        self.prepare()
        events = [event["event"] for event in self.journal.events()]
        self.assertEqual(
            events,
            [
                "existing_event",
                "prepare_start",
                "prepare_track_start",
                "prepare_track_staged",
                "prepare_complete",
            ],
        )
        self.assertFalse(self.wav.exists())
        self.assertFalse(any(self.show.glob("*.log")))

    def test_failure_keeps_wav_and_never_claims_committed_success(self):
        with patch(
            "lma_tools.uploader.prepare.verify_pcm",
            side_effect=ValueError("PCM mismatch"),
        ):
            with self.assertRaisesRegex(ValueError, "PCM mismatch"):
                self.prepare()
        events = [event["event"] for event in self.journal.events()]
        self.assertIn("prepare_track_failed", events)
        self.assertEqual(events[-1], "prepare_failed")
        self.assertNotIn("prepare_complete", events)
        self.assertTrue(self.wav.exists())
        self.assertFalse(list(self.show.glob("*.flac")))

    def test_preview_and_cancel_never_create_log(self):
        with contextlib.redirect_stdout(io.StringIO()):
            prepare_show(
                self.show, self.config, preview=True, journal=self.journal
            )
        self.assertFalse(self.log.exists())
        with patch(
            "lma_tools.uploader.prepare.ui.edit_document",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                prepare_show(self.show, self.config, journal=self.journal)
        self.assertFalse(self.log.exists())

    def test_disabled_logging_creates_no_log(self):
        self.journal.enabled = False
        self.prepare()
        self.assertFalse(self.log.exists())

    def test_manifest_replacement_failure_restores_old_md5(self):
        self.prepare()
        md5 = self.show / (self.show.name + ".md5")
        original = md5.read_bytes(), md5.stat().st_mtime_ns
        self.notes.write_text(self.notes.read_text() + "\nChanged\n")
        replace = Path.replace

        def fail(path, destination):
            if Path(destination) == md5:
                raise OSError("simulated manifest failure")
            return replace(path, destination)

        with patch.object(Path, "replace", fail):
            with self.assertRaisesRegex(OSError, "manifest failure"):
                apply_plan(self.show, build_plan(self.show, self.config))
        self.assertEqual((md5.read_bytes(), md5.stat().st_mtime_ns), original)
