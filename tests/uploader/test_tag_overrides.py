"""Preserve custom FLAC tags and apply explicit preview values exactly."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC

from lma_tools.uploader.encoder import tag_audio, tags_match
from lma_tools.uploader.prepare import (
    apply_plan,
    build_plan,
    review_text,
    validate_manifests,
    validate_plan,
)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class TagOverrideTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.show = self.root / "artist2026-11-07"
        self.show.mkdir()
        self.path = self.show / "artist2026-11-07s1t01.flac"
        self.notes = self.show / "notes.txt"
        self.notes.write_text(
            "Artist\nNovember 07, 2026\nVenue\nCity\n\nSet 1\n01. Song\n"
        )
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "0.01",
                str(self.path),
            ],
            check=True,
            capture_output=True,
        )
        self.common = dict(
            artist="Artist",
            album="Album",
            genre="Jazz",
            venue="Venue",
            date="2026-11-07",
        )
        self.track = dict(title="Song", track=1, disc="1/2")
        tag_audio(self.path, self.common, self.track, 1, 1)
        audio = FLAC(self.path)
        audio["Composer"] = ["Composer A", "Composer B"]
        audio["COMMENT"] = ["Keep this comment"]
        audio["BPM"] = ["120"]
        audio.save()

    def test_unmanaged_fields_do_not_trigger_rewrite_or_pcm_checks(self):
        plan = build_plan(self.show, {})
        self.assertEqual(plan["tracks"][0]["disc"], "1/2")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with (
            patch("lma_tools.uploader.prepare.tag_audio") as write,
            patch("lma_tools.uploader.prepare.verify_pcm") as verify,
        ):
            apply_plan(self.show, plan)
        write.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns), before
        )
        self.assertTrue(validate_manifests(self.show)["valid"])

    def test_retag_preserves_extra_fields_values_and_casing(self):
        plan = build_plan(self.show, {})
        plan["tracks"][0]["title"] = "Revised title"
        fingerprint = FLAC(self.path).info.md5_signature
        apply_plan(self.show, plan)
        audio = FLAC(self.path)
        self.assertEqual(audio["title"], ["Revised title"])
        self.assertEqual(audio["composer"], ["Composer A", "Composer B"])
        self.assertEqual(audio["comment"], ["Keep this comment"])
        self.assertEqual(audio["bpm"], ["120"])
        self.assertEqual(audio.info.md5_signature, fingerprint)
        self.assertIn(b"Composer=Composer A", self.path.read_bytes())
        self.assertIn(b"Title=Revised title", self.path.read_bytes())

    def test_custom_common_and_track_overrides_and_removal(self):
        plan = build_plan(self.show, {})
        plan["tags"] = {
            "COMPOSER": ["New Composer"],
            "COMMENT": "",
            "BPM": "130",
        }
        plan["tracks"][0]["tags"] = {"bpm": "140", "CUSTOM": "Value"}
        approved = validate_plan(review_text(plan), self.show)
        self.assertEqual(approved, plan)
        apply_plan(self.show, approved)
        audio = FLAC(self.path)
        self.assertEqual(audio["composer"], ["New Composer"])
        self.assertNotIn("comment", audio)
        self.assertEqual(audio["bpm"], ["140"])
        self.assertEqual(audio["custom"], ["Value"])
        self.assertIn(b"CUSTOM=Value", self.path.read_bytes())
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        apply_plan(self.show, approved)
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns), before
        )

    def test_manual_numbers_and_totals_survive_write_and_rerun(self):
        plan = build_plan(self.show, {})
        plan["tracks"][0].update(track="7/18", disc="3/5")
        approved = validate_plan(review_text(plan), self.show)
        self.assertEqual(approved["tracks"][0]["track"], "7/18")
        self.assertEqual(approved["tracks"][0]["disc"], "3/5")
        apply_plan(self.show, approved)
        audio = FLAC(self.path)
        self.assertEqual(audio["tracknumber"], ["7/18"])
        self.assertEqual(audio["discnumber"], ["3/5"])
        again = build_plan(self.show, {})
        self.assertEqual(again["tracks"][0]["track"], "7/18")
        self.assertEqual(again["tracks"][0]["disc"], "3/5")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        apply_plan(self.show, again)
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns), before
        )

    def test_common_fields_title_and_destination_overrides_are_applied(self):
        plan = build_plan(self.show, {})
        plan.update(
            artist="Edited Artist",
            album="Edited Album",
            genre="",
            venue="Edited Venue",
            date="2025-10-03",
        )
        plan["tracks"][0].update(
            title="Edited Song", destination="edited.flac"
        )
        approved = validate_plan(review_text(plan), self.show)
        apply_plan(self.show, approved)
        target = self.show / "edited.flac"
        self.assertFalse(self.path.exists())
        audio = FLAC(target)
        for key, value in (
            ("artist", "Edited Artist"),
            ("album", "Edited Album"),
            ("venue", "Edited Venue"),
            ("date", "2025"),
            ("title", "Edited Song"),
        ):
            self.assertEqual(audio[key], [value])
        self.assertNotIn("genre", audio)
        self.assertEqual(audio["bpm"], ["120"])

    def test_invalid_numbers_and_custom_managed_names_are_rejected(self):
        for field in ("track", "disc"):
            for value in ("0/2", "3/2", "1/0", "bad", -1, True):
                with self.subTest(field=field, value=value):
                    plan = build_plan(self.show, {})
                    plan["tracks"][0][field] = value
                    with self.assertRaises(ValueError):
                        validate_plan(review_text(plan), self.show)
        for tags in (
            {"TITLE": "Other"},
            {"x": "a", "X": "b"},
            {"bad=key": "value"},
            {"BPM": 120},
        ):
            with self.subTest(tags=tags):
                plan = build_plan(self.show, {})
                plan["tags"] = tags
                with self.assertRaises(ValueError):
                    validate_plan(review_text(plan), self.show)

    def test_legacy_totals_are_removed_but_custom_tags_remain(self):
        audio = FLAC(self.path)
        audio["TOTALTRACKS"] = ["1"]
        audio["TOTALDISCS"] = ["2"]
        audio.save()
        self.assertFalse(tags_match(self.path, self.common, self.track, 1, 1))
        tag_audio(self.path, self.common, self.track, 1, 1)
        audio = FLAC(self.path)
        self.assertNotIn("totaltracks", audio)
        self.assertNotIn("totaldiscs", audio)
        self.assertEqual(audio["comment"], ["Keep this comment"])
        self.assertTrue(tags_match(self.path, self.common, self.track, 1, 1))

    def test_separate_legacy_numbers_and_totals_seed_combined_preview(self):
        audio = FLAC(self.path)
        audio["TRACKNUMBER"] = ["7"]
        audio["TOTALTRACKS"] = ["18"]
        audio["DISCNUMBER"] = ["1"]
        audio["TOTALDISCS"] = ["4"]
        audio.save()
        plan = build_plan(self.show, {})
        self.assertEqual(plan["tracks"][0]["track"], "7/18")
        self.assertEqual(plan["tracks"][0]["disc"], "1/4")
        apply_plan(self.show, plan)
        audio = FLAC(self.path)
        self.assertEqual(audio["tracknumber"], ["7/18"])
        self.assertEqual(audio["discnumber"], ["1/4"])
        self.assertNotIn("totaltracks", audio)
        self.assertNotIn("totaldiscs", audio)

    def test_existing_managed_tag_casing_does_not_force_a_rewrite(self):
        audio = FLAC(self.path)
        audio["artist"] = audio["artist"]
        audio.save()
        self.assertIn(b"artist=Artist", self.path.read_bytes())
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        apply_plan(self.show, build_plan(self.show, {}))
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns), before
        )

    def test_bare_numbers_are_rejected_in_review_before_any_writes(self):
        for field in ("disc", "track"):
            with self.subTest(field=field):
                plan = build_plan(self.show, {})
                text = review_text(plan)
                original = '"1/2"' if field == "disc" else '"1/1"'
                text = text.replace(f"{field} = {original}", f"{field} = 7")
                with self.assertRaisesRegex(ValueError, "number/total"):
                    validate_plan(text, self.show)
