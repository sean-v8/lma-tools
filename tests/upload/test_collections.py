"""Prefix preferences, alphabetical ordering and atomic writes."""

import stat
import tempfile
import tomllib
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lma_tools.upload.collections import (
    get_preference,
    save_collection,
    validate,
)


class CollectionTests(unittest.TestCase):
    def config(self, root):
        path = root / "config.toml"
        path.write_text(
            '# Private defaults\n[upload]\nshows_root = "/recordings"\n'
            "log = false # keep me\n\n[upload.prefix]\n"
            'old = "OldCollection" # keep this too\n\n[report]\n'
            'query = ["venue:demo"] # private query\nsort = "publicdate"\n'
        )
        path.chmod(0o600)
        return path

    def test_saves_only_mapping_and_preserves_comments_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.config(root)
            old = tomllib.loads(path.read_text())
            save_collection(path, "new.band", "NewCollection")
            text = path.read_text()
            new = tomllib.loads(text)
            self.assertEqual(new["report"], old["report"])
            self.assertEqual(new["upload"]["log"], False)
            self.assertEqual(
                new["upload"]["prefix"],
                {"old": "OldCollection", "new.band": "NewCollection"},
            )
            self.assertIn('old = "OldCollection" # keep this too', text)
            self.assertIn("log = false # keep me", text)
            self.assertIn("# private query", text)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse((root / "collections.csv").exists())
            before = path.read_bytes()
            with patch("os.replace") as replace:
                save_collection(path, "new.band", "NewCollection")
                replace.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_learned_mappings_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(Path(tmp))
            with ProcessPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(save_collection, path, f"band{i}", f"Name{i}")
                    for i in range(12)
                ]
                for future in futures:
                    future.result(timeout=15)
            values = tomllib.loads(path.read_text())["upload"]["prefix"]
            self.assertEqual(len(values), 13)
            self.assertEqual(values["old"], "OldCollection")

    def test_failed_replace_leaves_original_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.config(root)
            original = path.read_bytes()
            with patch("os.replace", side_effect=OSError("test failure")):
                with self.assertRaises(OSError):
                    save_collection(path, "band", "Collection")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [path])

    def test_invalid_mapping_types(self):
        for values in ("collections.csv", [], {"band": 1}, {"": "Name"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate(values)


class PreferenceTests(unittest.TestCase):
    def test_string_and_named_collection_entries(self):
        config = {
            "prefix": {
                "old": "Old",
                "new": {
                    "collection": "New",
                    "creator": "Artist",
                    "genre": "Jazz",
                },
            }
        }
        validate(config["prefix"])
        self.assertEqual(get_preference(config, "old"), {"collection": "Old"})
        self.assertEqual(get_preference(config, "new")["genre"], "Jazz")
        self.assertEqual(get_preference(config, "missing"), {})
        for value in ({"creator": "A"}, {"collection": "X", "genre": 3}):
            with self.assertRaises(ValueError):
                validate({"bad": value})

    def test_preferences_at_end_preserve_other_sections_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '# private\n[upload]\nshows_root="/shows"\n'
                '[upload.prefix]\nband="Collection" # retained\n'
                '[report]\nsort="views" # sorting\n'
            )
            save_collection(path, "band", "Collection", creator="Artist")
            save_collection(path, "band", "Collection", genre="Jazz")
            text = path.read_text()
            data = tomllib.loads(text)
            self.assertEqual(
                data["upload"]["prefix"]["band"],
                {
                    "collection": "Collection",
                    "creator": "Artist",
                    "genre": "Jazz",
                },
            )
            self.assertEqual(data["report"], {"sort": "views"})
            self.assertLess(
                text.index("[report]"),
                text.index("[upload.prefix.band]"),
            )
            self.assertIn("# private", text)
            self.assertIn("# sorting", text)

    def test_named_blocks_sort_by_prefix_and_preserve_table_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[upload]\nshows_root="/shows"\n'
                '[report]\nsort="views" # report setting\n\n'
                "[upload.prefix.zulu] # zulu header\n"
                'collection="Zulu" # zulu value\n'
                '[upload.prefix.alpha]\ncollection="Alpha"\n'
            )
            save_collection(path, "middle", "Middle", creator="Artist")
            data = tomllib.loads(path.read_text())
            self.assertEqual(
                list(data["upload"]["prefix"]),
                ["alpha", "middle", "zulu"],
            )
            text = path.read_text()
            self.assertIn("# zulu header", text)
            self.assertIn("# zulu value", text)
            self.assertIn("# report setting", text)
            self.assertLess(
                text.index("[report]"),
                text.index("[upload.prefix.alpha]"),
            )
            before = path.read_bytes()
            save_collection(path, "middle", "Middle", creator="Artist")
            self.assertEqual(path.read_bytes(), before)
