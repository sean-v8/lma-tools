"""Upload discovery regression coverage."""

import tempfile
import unittest
from pathlib import Path

from lma_tools.upload import cli as lma
from tests.upload.helpers import ID, PROJECT, make_show


class DiscoveryTests(unittest.TestCase):
    def test_multiple_outputs_different_names_and_duplicate_disambiguation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = make_show(root)
            other = make_show(root, identifier=ID + ".mix2")
            (one.parent / "raw.wav").write_bytes(b"raw")
            nested = one.parent / "raw" / "nested2026-08-17"
            nested.mkdir(parents=True)
            (nested / "x.flac").write_bytes(b"x")
            (nested / "x.txt").write_bytes(b"x")
            outputs = lma.scan_outputs(root)
            self.assertEqual({p for _, p in outputs}, {one, other})
            self.assertEqual(lma.resolve_show(ID, root), (one, one.parent))
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                lma.resolve_show(PROJECT, root)
            duplicate = make_show(root, project=PROJECT + ".duplicate")
            tokens = lma.output_tokens(lma.scan_outputs(root), root)
            self.assertIn(str(one.relative_to(root)), tokens)
            self.assertIn(str(duplicate.relative_to(root)), tokens)
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                lma.resolve_show(ID, root)

    def test_explicit_project_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = make_show(root)
            self.assertEqual(
                lma.resolve_show(str(one), root), (one, one.parent)
            )
            self.assertEqual(
                lma.resolve_show(str(one.parent), root), (one, one.parent)
            )

    def test_config_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            with self.assertRaisesRegex(ValueError, "Create"):
                lma.load_config(p)
            p.write_text('shows_root = "relative"')
            with self.assertRaisesRegex(ValueError, "absolute"):
                lma.load_config(p)

    def test_project_sync_notes_do_not_make_a_second_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = make_show(root, project=ID)
            (output / "one.flac").rename(output / "01-Song.wav")
            (output.parent / "mix.wav").write_bytes(b"mix")
            (output.parent / "sync.txt").write_text("Sync markers")
            (output.parent / "project.aup3").touch()
            outputs = lma.scan_outputs(root)
            self.assertEqual(outputs, [(output.parent, output)])
            self.assertEqual(lma.output_tokens(outputs, root), [ID])
            for target in (ID, str(output.parent), str(output)):
                with self.subTest(target=target):
                    self.assertEqual(
                        lma.resolve_show(target, root),
                        (output, output.parent),
                    )

    def test_real_parent_and_child_outputs_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = make_show(root, project=ID)
            (output.parent / "mix.wav").write_bytes(b"mix")
            (output.parent / (ID + ".txt")).write_text("Show notes")
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                lma.resolve_show(ID, root)
