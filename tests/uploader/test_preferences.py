"""Named collection preferences survive atomic updates and migration."""

import tempfile
import tomllib
import unittest
from pathlib import Path

from lma_tools.uploader.collections import (
    get_preference,
    save_collection,
    validate,
)


class PreferenceTests(unittest.TestCase):
    def test_legacy_and_named_entries(self):
        config = {
            "collections": {
                "old": "Old",
                "new": {
                    "collection": "New",
                    "creator": "Artist",
                    "genre": "Jazz",
                },
            }
        }
        validate(config["collections"])
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
                '# private\n[uploader]\nshows_root="/shows"\n'
                '[uploader.collections]\nband="Collection" # retained\n'
                '[views]\nsort="views" # sorting\n'
            )
            save_collection(path, "band", "Collection", creator="Artist")
            save_collection(path, "band", "Collection", genre="Jazz")
            text = path.read_text()
            data = tomllib.loads(text)
            self.assertEqual(
                data["uploader"]["collections"]["band"],
                {
                    "collection": "Collection",
                    "creator": "Artist",
                    "genre": "Jazz",
                },
            )
            self.assertEqual(data["views"], {"sort": "views"})
            self.assertLess(
                text.index("[views]"),
                text.index("[uploader.collections.band]"),
            )
            self.assertIn("# private", text)
            self.assertIn("# sorting", text)
