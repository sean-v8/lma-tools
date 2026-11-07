"""Mapping writes preserve private config and require approved workflows."""

import io
import stat
import tempfile
import tomllib
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lma_tools.upload.collections import load_csv, save_collection, validate
from tests.test_integration import migration


class CollectionTests(unittest.TestCase):
    def config(self, root):
        path = root / "config.toml"
        path.write_text(
            '# Private defaults\n[upload]\nshows_root = "/recordings"\n'
            "log = false # keep me\n\n[upload.collections]\n"
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
                new["upload"]["collections"],
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
            values = tomllib.loads(path.read_text())["upload"]["collections"]
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

    def test_upgrade_imports_csv_and_retains_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.toml"
            text = (
                '# Private comment\n[upload]\nshows_root = "/recordings"\n'
                'collections = "bands.csv"\n[report]\nsort = "views"\n'
            )
            path.write_text(text)
            csv = root / "bands.csv"
            csv.write_text('band,Collection\n"band.other",OtherCollection\n')
            with redirect_stdout(io.StringIO()):
                migration.upgrade(path)
                migration.upgrade(path)
            document = tomllib.loads(path.read_text())
            self.assertEqual(
                document["upload"]["collections"],
                {"band": "Collection", "band.other": "OtherCollection"},
            )
            self.assertIn("# Private comment", path.read_text())
            self.assertEqual(document["report"], {"sort": "views"})
            self.assertEqual(
                (root / "config.toml.before-collections").read_text(), text
            )
            self.assertTrue(csv.exists())

    def test_missing_csv_and_existing_backup_do_not_modify_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.toml"
            path.write_text('[upload]\ncollections = "missing.csv"\n')
            original = path.read_bytes()
            with self.assertRaises(FileNotFoundError):
                migration.upgrade(path)
            self.assertEqual(path.read_bytes(), original)
            (root / "missing.csv").write_text("band,Name\n")
            (root / "config.toml.before-collections").write_text("backup")
            with self.assertRaises(FileExistsError):
                migration.upgrade(path)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_mapping_types_and_conflicting_csv(self):
        for values in ("collections.csv", [], {"band": 1}, {"": "Name"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate(values)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collections.csv"
            path.write_text("band,One\nband,Two\n")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                load_csv(path)
