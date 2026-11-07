"""Shared preparation totals never become independent audio tags."""

import shutil
from unittest import TestCase, skipUnless

from mutagen.flac import FLAC

from lma_tools.upload.prepare import (
    apply_plan,
    build_plan,
    review_text,
    validate_plan,
)
from tests.upload import test_tag_overrides


@skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class DiscTotalTests(TestCase):
    setUp = test_tag_overrides.TagOverrideTests.setUp

    def test_one_edit_updates_discnumber_without_extra_total_tags(self):
        plan = build_plan(self.show, {})
        self.assertEqual(plan["disc_total"], 2)
        text = review_text(plan).replace("disc_total = 2", "disc_total = 4")
        approved = validate_plan(text, self.show)
        apply_plan(self.show, approved)
        audio = FLAC(self.path)
        self.assertEqual(audio["discnumber"], ["1/4"])
        self.assertNotIn("disc_total", audio)
        self.assertNotIn("totaldiscs", audio)
        again = build_plan(self.show, {})
        self.assertEqual(again["disc_total"], 4)
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        apply_plan(self.show, again)
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns), before
        )

    def test_explicit_pair_overrides_shared_total(self):
        plan = build_plan(self.show, {})
        plan["disc_total"] = 3
        plan["tracks"][0]["disc"] = "1/5"
        approved = validate_plan(review_text(plan), self.show)
        self.assertEqual(approved["tracks"][0]["disc"], "1/5")
        apply_plan(self.show, approved)
        self.assertEqual(FLAC(self.path)["discnumber"], ["1/5"])

    def test_invalid_shared_totals_rejected(self):
        for value in (0, -1, True, "2"):
            with self.subTest(value=value):
                plan = build_plan(self.show, {})
                plan["disc_total"] = value
                with self.assertRaises(ValueError):
                    validate_plan(review_text(plan), self.show)

    def test_shared_total_applies_to_every_set(self):
        other = self.show / "artist2026-11-07s2t01.flac"
        shutil.copy2(self.path, other)
        self.notes.write_text(self.notes.read_text() + "Set 2\n01. Song\n")
        audio = FLAC(other)
        audio["DISCNUMBER"] = "2/2"
        audio.save()
        plan = build_plan(self.show, {})
        plan["disc_total"] = 4
        approved = validate_plan(review_text(plan), self.show)
        apply_plan(self.show, approved)
        self.assertEqual(FLAC(self.path)["discnumber"], ["1/4"])
        self.assertEqual(FLAC(other)["discnumber"], ["2/4"])
