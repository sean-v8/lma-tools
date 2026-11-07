"""Preparation, no-op, rollback and manifest-only integration tests."""

import contextlib
import io
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC

from lma_tools.uploader.prepare import (
    apply_plan,
    build_plan,
    prepare_show,
    review_text,
    validate_manifests,
    validate_plan,
)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.show = self.root / "test2026-11-06"
        self.show.mkdir()
        (self.show / "notes.txt").write_text(
            "Artist\nNovember 06, 2026\nVenue\nCity\n\n"
            "SBD + audience microphones\n1. Song\n"
        )
        self.source = self.show / "Song.wav"
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((2, 3, 48000, 0, "NONE", "none"))
            audio.writeframes(b"\x01\x02\x03" * 8000)
        self.config = {"genre": "Jazz", "creator": "Artist"}

    def test_preview_prints_without_editor_or_writes(self):
        before = self.source.stat().st_mtime_ns
        with patch("lma_tools.uploader.prepare.ui.edit_document") as edit:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                plan = prepare_show(self.show, self.config, preview=True)
        edit.assert_not_called()
        self.assertIn('# "Song.wav"', output.getvalue())
        self.assertEqual(plan["album"], "2026-11-06 City (MTX)")
        self.assertEqual(plan["genre"], "Jazz")
        self.assertEqual(self.source.stat().st_mtime_ns, before)

    def test_single_set_omits_set_prefix_and_accepts_legacy_names(self):
        for name in ("test2026-11-06t01.wav", "test2026-11-06s1t01.wav"):
            with self.subTest(name=name):
                source = self.source.rename(self.show / name)
                plan = build_plan(self.show, self.config)
                row = plan["tracks"][0]
                self.assertEqual(row["source"], name)
                self.assertEqual(row["destination"], "test2026-11-06t01.flac")
                self.assertEqual(row["disc"], 1)
                source.rename(self.source)
        notes = self.show / "notes.txt"
        notes.write_text(notes.read_text().replace("Set 1\n", ""))
        self.assertEqual(
            build_plan(self.show, self.config)["tracks"][0]["destination"],
            "test2026-11-06t01.flac",
        )

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_single_set_disc_tags_remain_one_of_one(self):
        plan = apply_plan(self.show, build_plan(self.show, self.config))
        audio = FLAC(self.show / "test2026-11-06t01.flac")
        self.assertEqual(audio["discnumber"], ["1/1"])
        self.assertNotIn("totaldiscs", audio)
        self.assertEqual(plan["tracks"][0]["disc"], 1)

    def test_explicit_single_recorded_set_preserves_its_number(self):
        notes = self.show / "notes.txt"
        original = notes.read_text()
        for number in (1, 2, 3):
            with self.subTest(number=number):
                notes.write_text(
                    original.replace("1. Song", f"Set {number}\n08. Song")
                )
                plan = validate_plan(
                    review_text(build_plan(self.show, self.config)), self.show
                )
                row = plan["tracks"][0]
                self.assertEqual(row["disc"], number)
                self.assertEqual(row["track"], 1)
                self.assertEqual(
                    row["destination"],
                    f"test2026-11-06s{number}t01.flac",
                )

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_recorded_set_two_disc_tags(self):
        notes = self.show / "notes.txt"
        notes.write_text(
            notes.read_text().replace("1. Song", "Set 2\n1. Song")
        )
        apply_plan(self.show, build_plan(self.show, self.config))
        audio = FLAC(self.show / "test2026-11-06s2t01.flac")
        self.assertEqual(audio["discnumber"], ["2/2"])
        self.assertNotIn("totaldiscs", audio)

    def test_preview_shows_totals_and_validates_without_changing_plan(self):
        import tomllib

        plan = build_plan(self.show, self.config)
        preview = tomllib.loads(review_text(plan))
        self.assertEqual(preview["tracks"][0]["disc"], "1/1")
        self.assertEqual(preview["tracks"][0]["track"], "1/1")
        self.assertEqual(plan["tracks"][0]["disc"], 1)
        approved = validate_plan(review_text(plan), self.show)
        self.assertEqual(approved, plan)
        for field in ("disc", "track"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                text = review_text(plan).replace(
                    f'{field} = "1/1"', f'{field} = "1/2"'
                )
                validate_plan(text, self.show)

    def test_preview_shows_multiset_and_partial_set_totals(self):
        import tomllib

        notes = self.show / "notes.txt"
        original = notes.read_text()
        notes.write_text(original + "Set 2\n01. Other\n")
        shutil.copy2(self.source, self.show / "Other.wav")
        plan = build_plan(self.show, self.config)
        rows = tomllib.loads(review_text(plan))["tracks"]
        self.assertEqual([r["disc"] for r in rows], ["1/2", "2/2"])
        self.assertEqual([r["track"] for r in rows], ["1/2", "2/2"])
        self.assertEqual(validate_plan(review_text(plan), self.show), plan)
        (self.show / "Other.wav").unlink()
        notes.write_text(original.replace("1. Song", "Set 2\n1. Song"))
        plan = build_plan(self.show, self.config)
        rows = tomllib.loads(review_text(plan))["tracks"]
        self.assertEqual(rows[0]["disc"], "2/2")
        self.assertEqual(rows[0]["track"], "1/1")
        self.assertEqual(validate_plan(review_text(plan), self.show), plan)

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_split_totals_are_repaired_and_combined_tags_are_stable(self):
        from lma_tools.uploader.encoder import pcm_digest

        plan = apply_plan(self.show, build_plan(self.show, self.config))
        path = self.show / plan["tracks"][0]["destination"]
        digest = pcm_digest(path)
        audio = FLAC(path)
        audio["tracknumber"] = "1"
        audio["totaltracks"] = "1"
        audio["discnumber"] = "1"
        audio["totaldiscs"] = "1"
        audio.save()
        apply_plan(self.show, build_plan(self.show, self.config))
        audio = FLAC(path)
        self.assertEqual(audio["tracknumber"], ["1/1"])
        self.assertEqual(audio["discnumber"], ["1/1"])
        self.assertNotIn("totaltracks", audio)
        self.assertNotIn("totaldiscs", audio)
        self.assertEqual(pcm_digest(path), digest)
        self.assertTrue(validate_manifests(self.show)["valid"])
        stamp = path.stat().st_mtime_ns
        apply_plan(self.show, build_plan(self.show, self.config))
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_partial_set_headers_and_defined_guest_symbols(self):
        notes = self.show / "notes.txt"
        text = notes.read_text().replace(
            "1. Song\n",
            "Set 1 (partial)\n\n01. Song *#\n"
            "02. Other\n\nSet 2\n\n01. Third > $\n02. Fourth $\n\n"
            "* w/ Guest A on vocals\n# w/ Guest B on trumpet\n"
            "$ w/ Guest C on vocals\n",
        )
        notes.write_text(text)
        for name in ("02-Other.wav", "03-Third _.wav", "04-Fourth.wav"):
            shutil.copy2(self.source, self.show / name)
        plan = validate_plan(
            review_text(build_plan(self.show, self.config)), self.show
        )
        self.assertEqual(
            [row["title"] for row in plan["tracks"]],
            ["Song", "Other", "Third >", "Fourth"],
        )
        self.assertEqual(
            [row["destination"] for row in plan["tracks"]],
            [
                f"test2026-11-06s{s}t{t:02d}.flac"
                for s, t in ((1, 1), (1, 2), (2, 1), (2, 2))
            ],
        )
        self.assertEqual(notes.read_text(), text)

    def test_guest_symbols_without_legend_and_literal_sharps(self):
        notes = self.show / "notes.txt"
        original = notes.read_text()
        for title, legend in (
            ("Song *", ""),
            ("Song * > $", ""),
            ("Song in C#", "# w/ Guest"),
        ):
            with self.subTest(title=title):
                notes.write_text(
                    original.replace("1. Song", "1. " + title) + "\n" + legend
                )
                plan = build_plan(self.show, self.config)
                expected = {"Song *": "Song", "Song * > $": "Song >"}.get(
                    title, title
                )
                self.assertEqual(plan["tracks"][0]["title"], expected)
        notes.write_text(
            original.replace("1. Song", "Set 2 (partial)\n1. Song")
        )
        plan = validate_plan(
            review_text(build_plan(self.show, self.config)), self.show
        )
        self.assertEqual(
            plan["tracks"][0]["destination"], "test2026-11-06s2t01.flac"
        )

    def test_varied_guest_and_cut_markers_preserve_song_punctuation(self):
        notes = self.show / "notes.txt"
        original = notes.read_text()
        examples = {
            "// So Much Confusion": "// So Much Confusion",
            "Cherry Oh Baby > *": "Cherry Oh Baby >",
            "Waiting In Vain > #$": "Waiting In Vain >",
            "Sa Re Ga // *": "Sa Re Ga //",
            "Brain Damage &": "Brain Damage",
            "Lean On *&": "Lean On",
            "Ganja Man &%": "Ganja Man",
            "Song ⚑§": "Song",
            "Song _": "Song",
            "Song// > *": "Song// >",
            "// Song //": "// Song //",
            "Song * //": "Song //",
            "Song * // > #$": "Song // >",
            "Song // * > $": "Song // >",
            "Song †‡ > &%": "Song >",
            "Super Bad / Chank": "Super Bad / Chank",
            "What Is Hip?": "What Is Hip?",
            "Song (Reprise)": "Song (Reprise)",
            "Song in C#": "Song in C#",
        }
        for title, expected in examples.items():
            with self.subTest(title=title):
                text = original.replace(
                    "1. Song", "Set 1 (partial)\n1. " + title
                )
                notes.write_text(text)
                plan = build_plan(self.show, self.config)
                self.assertEqual(plan["tracks"][0]["title"], expected)
                self.assertEqual(
                    plan["tracks"][0]["destination"],
                    "test2026-11-06s1t01.flac",
                )
                self.assertEqual(notes.read_text(), text)

    def test_incomplete_mapping_rejected(self):
        plan = build_plan(self.show, self.config)
        plan["tracks"][0]["source"] = ""
        with self.assertRaises(ValueError):
            validate_plan(review_text(plan), self.show)

    def test_missing_manifest_policy(self):
        self.assertTrue(validate_manifests(self.show)["valid"])
        self.assertFalse(
            validate_manifests(self.show, allow_missing=False)["valid"]
        )

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_prepare_noop_and_manifest_repair(self):
        plan = apply_plan(self.show, build_plan(self.show, self.config))
        self.assertFalse(self.source.exists())
        target = self.show / plan["tracks"][0]["destination"]
        audio = FLAC(target)
        self.assertEqual(audio["date"], ["2026"])
        self.assertEqual(audio["tracknumber"], ["1/1"])
        self.assertNotIn("totaltracks", audio)
        self.assertNotIn("comment", audio)
        before = {p.name: p.stat().st_mtime_ns for p in self.show.iterdir()}
        plan = build_plan(self.show, self.config)
        with patch("lma_tools.uploader.prepare.tag_audio") as tag:
            with patch("lma_tools.uploader.prepare.verify_pcm") as verify:
                apply_plan(self.show, plan)
        tag.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(
            before, {p.name: p.stat().st_mtime_ns for p in self.show.iterdir()}
        )
        md5 = self.show / (self.show.name + ".md5")
        md5.write_text("0" * 32 + "  " + target.name + "\n")
        self.assertFalse(validate_manifests(self.show)["valid"])
        stamp = target.stat().st_mtime_ns
        self.assertTrue(validate_manifests(self.show, repair=True)["valid"])
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertTrue(validate_manifests(self.show)["valid"])
        self.assertEqual(list(self.root.iterdir()), [self.show])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_verify_failure_keeps_original(self):
        with patch(
            "lma_tools.uploader.prepare.verify_pcm",
            side_effect=ValueError("mismatch"),
        ):
            with self.assertRaises(ValueError):
                apply_plan(self.show, build_plan(self.show, self.config))
        self.assertTrue(self.source.exists())
        self.assertEqual(len(list(self.show.iterdir())), 2)
        self.assertEqual(list(self.root.iterdir()), [self.show])

    def test_unknown_fields_and_symlinks_rejected(self):
        plan = build_plan(self.show, self.config)
        plan["typo"] = "value"
        with self.assertRaises(ValueError):
            validate_plan(review_text(plan), self.show)
        outside = self.root / "outside.wav"
        outside.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(outside)
        with self.assertRaises(ValueError):
            build_plan(self.show, self.config)

    def test_absent_listed_files_warn_but_present_omissions_fail(self):
        manifest = self.show / "partial.md5"
        manifest.write_text("0" * 32 + "  absent.flac\n")
        result = validate_manifests(self.show)
        self.assertTrue(result["warnings"])
        self.assertFalse(result["valid"])
        self.assertIn("unlisted present", " ".join(result["errors"]))
        (self.show / "notes.txt").unlink()
        result = validate_manifests(self.show)
        self.assertTrue(result["valid"])
        self.assertTrue(result["warnings"])
        self.assertFalse(
            validate_manifests(self.show, allow_missing=False)["valid"]
        )

    def test_canonical_filename_overrides_conflicting_title(self):
        destination = self.show / "test2026-11-06s1t01.wav"
        self.source.rename(destination)
        other = self.show / "Song.wav"
        other.write_bytes(destination.read_bytes())
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][0]["source"], destination.name)
        with self.assertRaises(ValueError):
            validate_plan(review_text(plan), self.show)

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_transaction_rolls_back_commit_failure(self):
        plan = build_plan(self.show, self.config)
        before = {p.name: p.read_bytes() for p in self.show.iterdir()}
        original = Path.replace
        count = 0

        def fail_second(path, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("commit failed")
            return original(path, destination)

        with patch.object(Path, "replace", fail_second):
            with self.assertRaises(OSError):
                apply_plan(self.show, plan)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.show.iterdir()}
        )
        self.assertEqual(list(self.root.iterdir()), [self.show])

    def test_wav_id3_title_and_set_sections(self):
        from mutagen.id3 import TIT2
        from mutagen.wave import WAVE

        self.source.rename(self.show / "unrelated.wav")
        audio = WAVE(self.show / "unrelated.wav")
        audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=["Song"]))
        audio.save()
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][0]["source"], "unrelated.wav")
        notes = self.show / "notes.txt"
        notes.write_text(notes.read_text() + "Set2\n1. Other\n")
        shutil.copy2(self.show / "unrelated.wav", self.show / "Other.wav")
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][1]["track"], 2)
        self.assertEqual(plan["tracks"][1]["disc"], 2)
        self.assertEqual(
            plan["tracks"][1]["destination"], "test2026-11-06s2t01.flac"
        )

    def test_numbered_filename_and_classification_scope(self):
        self.source.rename(self.show / "01 - Song.wav")
        notes = self.show / "notes.txt"
        notes.write_text(
            "Artist\nNovember 06, 2026\nVenue\nCity\n"
            "Multitrack direct feed\nSet1\n1. Song\n2. Matrix\n"
        )
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][0]["source"], "01 - Song.wav")
        self.assertTrue(plan["album"].endswith("(SBD)"))

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_obsolete_manifests_removed_transactionally(self):
        (self.show / "old.ffp").write_text("stale\n")
        (self.show / "old.md5").write_text("stale\n")
        apply_plan(self.show, build_plan(self.show, self.config))
        self.assertFalse((self.show / "old.ffp").exists())
        self.assertFalse((self.show / "old.md5").exists())
        self.assertTrue(validate_manifests(self.show)["valid"])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_artwork_embedded_once_and_preserved_on_noop(self):
        import subprocess

        artwork = self.show / "cover.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=red:size=32x24",
                "-frames:v",
                "1",
                "-threads",
                "1",
                str(artwork),
            ],
            check=True,
        )
        plan = apply_plan(self.show, build_plan(self.show, self.config))
        target = self.show / plan["tracks"][0]["destination"]
        picture = FLAC(target).pictures
        self.assertEqual(len(picture), 1)
        self.assertEqual((picture[0].width, picture[0].height), (32, 24))
        self.assertEqual(picture[0].depth, 24)
        before = target.stat().st_mtime_ns
        with patch("lma_tools.uploader.prepare.tag_audio") as tag:
            apply_plan(self.show, build_plan(self.show, self.config))
        tag.assert_not_called()
        self.assertEqual(target.stat().st_mtime_ns, before)

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_filename_permutation_commits_without_losing_sources(self):
        notes = self.show / "notes.txt"
        notes.write_text(notes.read_text() + "2. Other\n")
        with wave.open(str(self.show / "Other.wav"), "wb") as audio:
            audio.setparams((2, 3, 48000, 0, "NONE", "none"))
            audio.writeframes(b"\x02\x03\x04" * 8000)
        apply_plan(self.show, build_plan(self.show, self.config))
        plan = build_plan(self.show, self.config)
        first, second = plan["tracks"]
        first["source"], second["source"] = second["source"], first["source"]
        from lma_tools.uploader.encoder import pcm_digest

        expected = [
            pcm_digest(self.show / t["source"]) for t in plan["tracks"]
        ]
        apply_plan(self.show, plan)
        self.assertEqual(
            expected,
            [pcm_digest(self.show / t["destination"]) for t in plan["tracks"]],
        )
        self.assertTrue(validate_manifests(self.show)["valid"])

    def _external_pair(self):
        from lma_tools.uploader.encoder import encode_verbatim

        flac = self.source.with_suffix(".flac")
        encode_verbatim(self.source, flac)
        return flac

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_reviewed_external_pair_removes_only_verified_wav(self):
        self._external_pair()
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][0]["source"], "Song.flac")
        self.assertEqual(
            plan["redundant_wavs"],
            [{"source": "Song.wav", "track_source": "Song.flac"}],
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            prepare_show(self.show, self.config, preview=True)
        self.assertIn("[[redundant_wavs]]", output.getvalue())
        self.assertTrue(self.source.exists())
        from lma_tools.uploader.encoder import verify_pcm

        with patch(
            "lma_tools.uploader.prepare.verify_pcm", wraps=verify_pcm
        ) as verify:
            with patch("lma_tools.uploader.prepare.encode_verbatim") as enc:
                apply_plan(self.show, plan)
        enc.assert_not_called()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(verify.call_args.args[0], self.source)
        self.assertFalse(self.source.exists())
        self.assertTrue(validate_manifests(self.show)["valid"])
        self.assertEqual(list(self.root.iterdir()), [self.show])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_external_pair_pcm_mismatch_keeps_every_original(self):
        self._external_pair()
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((2, 3, 48000, 0, "NONE", "none"))
            audio.writeframes(b"\x02\x03\x04" * 8000)
        before = {p.name: p.read_bytes() for p in self.show.iterdir()}
        with self.assertRaisesRegex(ValueError, "PCM differs"):
            apply_plan(self.show, build_plan(self.show, self.config))
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.show.iterdir()}
        )
        self.assertEqual(list(self.root.iterdir()), [self.show])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_redundant_mapping_requires_complete_unique_sources(self):
        self._external_pair()
        plan = build_plan(self.show, self.config)
        duplicate = dict(plan["redundant_wavs"][0])
        plan["redundant_wavs"].append(duplicate)
        with self.assertRaises(ValueError):
            validate_plan(review_text(plan), self.show)
        plan["redundant_wavs"] = []
        with self.assertRaisesRegex(ValueError, "every audio source"):
            validate_plan(review_text(plan), self.show)
        plan["redundant_wavs"] = [duplicate]
        duplicate["track_source"] = "Song.wav"
        with self.assertRaises(ValueError):
            validate_plan(review_text(plan), self.show)

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_redundant_wav_delete_failure_rolls_back_commit(self):
        self._external_pair()
        before = {p.name: p.read_bytes() for p in self.show.iterdir()}
        original = Path.unlink
        failed = False

        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path == self.source and not failed:
                failed = True
                raise OSError("WAV removal failed")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_once):
            with self.assertRaisesRegex(OSError, "WAV removal failed"):
                apply_plan(self.show, build_plan(self.show, self.config))
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.show.iterdir()}
        )
        self.assertEqual(list(self.root.iterdir()), [self.show])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_redundant_wav_changed_after_verification_aborts(self):
        from lma_tools.uploader.encoder import verify_pcm

        self._external_pair()

        def change_after_check(source, target):
            verify_pcm(source, target)
            if source == self.source:
                with self.source.open("ab") as stream:
                    stream.write(b"changed")

        with patch(
            "lma_tools.uploader.prepare.verify_pcm",
            side_effect=change_after_check,
        ):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                apply_plan(self.show, build_plan(self.show, self.config))
        self.assertTrue(self.source.exists())
        self.assertTrue(self.source.with_suffix(".flac").exists())
        self.assertEqual(len(list(self.show.iterdir())), 3)

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_existing_approved_album_and_genre_survive_portable_noop(self):
        plan = build_plan(self.show, self.config)
        plan["album"], plan["genre"] = "Approved custom album", "Live Jazz"
        apply_plan(self.show, plan)
        plan = build_plan(self.show, {})
        self.assertEqual(plan["album"], "Approved custom album")
        self.assertEqual(plan["genre"], "Live Jazz")
        before = {p.name: p.stat().st_mtime_ns for p in self.show.iterdir()}
        with patch("lma_tools.uploader.prepare.verify_pcm") as verify:
            with patch("lma_tools.uploader.prepare.tag_audio") as tag:
                apply_plan(self.show, plan)
        tag.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(
            before, {p.name: p.stat().st_mtime_ns for p in self.show.iterdir()}
        )
        configured = build_plan(self.show, {"genre": "Configured"})
        self.assertEqual(configured["genre"], "Configured")
        target = self.show / plan["tracks"][0]["source"]
        audio = FLAC(target)
        audio["artist"] = ["Wrong artist"]
        audio.save()
        inferred = build_plan(self.show, {})
        self.assertEqual(inferred["artist"], "Artist")
        self.assertEqual(inferred["album"], "2026-11-06 City (MTX)")
        self.assertEqual(inferred["genre"], "")

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_prepared_pair_cannot_bypass_wav_verification(self):
        from lma_tools.uploader.encoder import verify_pcm

        original = self.source.read_bytes()
        plan = apply_plan(self.show, build_plan(self.show, self.config))
        flac = self.show / plan["tracks"][0]["destination"]
        redundant = flac.with_suffix(".wav")
        redundant.write_bytes(original)
        stamp = flac.stat().st_mtime_ns
        plan = build_plan(self.show, self.config)
        self.assertEqual(plan["tracks"][0]["source"], flac.name)
        self.assertEqual(plan["redundant_wavs"][0]["source"], redundant.name)
        with patch(
            "lma_tools.uploader.prepare.verify_pcm", wraps=verify_pcm
        ) as verify:
            with patch("lma_tools.uploader.prepare.tag_audio") as tag:
                apply_plan(self.show, plan)
        tag.assert_not_called()
        self.assertEqual(verify.call_count, 2)
        self.assertFalse(redundant.exists())
        self.assertEqual(flac.stat().st_mtime_ns, stamp)
        self.assertTrue(validate_manifests(self.show)["valid"])

    @unittest.skipUnless(shutil.which("flac"), "reference flac required")
    def test_portable_defaults_require_consistent_identity_and_values(self):
        notes = self.show / "notes.txt"
        notes.write_text(notes.read_text() + "2. Other\n")
        shutil.copy2(self.source, self.show / "Other.wav")
        plan = build_plan(self.show, self.config)
        plan["album"], plan["genre"] = "Reviewed album", "Reviewed genre"
        apply_plan(self.show, plan)
        target = self.show / plan["tracks"][1]["destination"]
        audio = FLAC(target)
        audio["album"] = ["Different album"]
        audio.save()
        inferred = build_plan(self.show, {})
        self.assertEqual(inferred["album"], "2026-11-06 City (MTX)")
        self.assertEqual(inferred["genre"], "Reviewed genre")
        audio["venue"] = ["Different venue"]
        audio.save()
        inferred = build_plan(self.show, {})
        self.assertEqual(inferred["venue"], "Venue")
        self.assertEqual(inferred["genre"], "")
        audio["venue"] = ["Venue"]
        audio["date"] = ["2025"]
        audio.save()
        self.assertEqual(build_plan(self.show, {})["genre"], "")
