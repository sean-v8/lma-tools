"""Command names and private config compatibility during the tool rename."""

import io
import os
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lma_tools.config import read_section
from lma_tools.report.config import load_config
from lma_tools.upload import cli
from lma_tools.upload.collections import save_collection
from tests.helpers import migration

ROOT = Path(__file__).resolve().parents[1]


class ToolNameTests(unittest.TestCase):
    def test_entry_points_have_matching_unique_short_names(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["lmau"], scripts["lma-upload"])
        self.assertEqual(scripts["lmar"], scripts["lma-report"])
        self.assertNotIn("lmav", scripts)
        self.assertEqual(scripts["lmau"], "lma_tools.upload.launcher:main")
        self.assertEqual(scripts["lmar"], "lma_tools.report.cli:main")

    def config(self, root):
        return (
            f"# private header\n[uploader] # upload settings\n"
            f'shows_root = "{root}"\nlog = false\n'
            '[uploader.prefix.artist]\ncollection = "Artist"\n'
            'creator = "Artist Name" # keep\ngenre = "Jazz"\n'
            '[views] # report settings\nsort = "views"\n'
        )

    def test_old_headers_read_without_changing_the_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.config(tmp))
            original = path.read_bytes()
            self.assertEqual(cli.load_config(path)["shows_root"], path.parent)
            self.assertEqual(load_config(path).sort, "views")
            self.assertEqual(path.read_bytes(), original)

    def test_collection_updates_preserve_legacy_headers(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.config(tmp))
            save_collection(path, "artist", "Artist", genre="Rock")
            data = tomllib.loads(path.read_text())
            self.assertNotIn("upload", data)
            self.assertNotIn("report", data)
            self.assertEqual(data["views"]["sort"], "views")
            self.assertEqual(
                data["uploader"]["prefix"]["artist"],
                {
                    "collection": "Artist",
                    "creator": "Artist Name",
                    "genre": "Rock",
                },
            )
            self.assertIn("# keep", path.read_text())

    def test_explicit_upgrade_keeps_values_comments_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = self.config(tmp)
            path.write_text(original)
            with redirect_stdout(io.StringIO()):
                migration.upgrade(path, backup_dir=path.parent / ".bak")
            migrated = path.read_text()
            data = tomllib.loads(migrated)
            self.assertEqual(set(data), {"upload", "report"})
            self.assertEqual(data["report"], {"sort": "views"})
            self.assertEqual(
                data["upload"]["prefix"]["artist"],
                {
                    "collection": "Artist",
                    "creator": "Artist Name",
                    "genre": "Jazz",
                },
            )
            self.assertIn("# private header", migrated)
            self.assertIn("# report settings", migrated)
            self.assertIn("# keep", migrated)
            self.assertLess(
                migrated.index("[report]"),
                migrated.index("[upload.prefix.artist]"),
            )
            backup = migration.backup_path(path, path.parent / ".bak")
            self.assertEqual(backup.read_text(), original)
            with redirect_stdout(io.StringIO()):
                migration.upgrade(path, backup_dir=path.parent / ".bak")
            self.assertEqual(path.read_text(), migrated)
            self.assertEqual(backup.read_text(), original)

    def test_conflicting_headers_are_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = self.config(tmp) + "\n[upload]\nlog = true\n"
            path.write_text(original)
            with self.assertRaisesRegex(ValueError, "not both"):
                read_section(path, "upload")
            with self.assertRaisesRegex(ValueError, "not both"):
                migration.upgrade(path, backup_dir=path.parent / ".bak")
            self.assertEqual(path.read_text(), original)

    def test_existing_migration_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = self.config(tmp)
            path.write_text(original)
            backup = migration.backup_path(path, path.parent / ".bak")
            backup.parent.mkdir()
            backup.write_text("old backup")
            with self.assertRaises(FileExistsError):
                migration.upgrade(path, backup_dir=path.parent / ".bak")
            self.assertEqual(path.read_text(), original)
            self.assertEqual(backup.read_text(), "old backup")

    def test_legacy_tool_cleanup_only_follows_successful_install(self):
        for fail in (False, True):
            with (
                self.subTest(failed_install=fail),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                log = root / "calls"
                uv = root / "uv"
                uv.write_text(
                    '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TRACE"\n'
                    'if [[ "$1 $2" == "tool list" ]]; then\n'
                    '  printf "lma-views v0.1\\n- lmav\\n"\nfi\n'
                    'if [[ "$1 $2" == "tool install" && "$FAIL" == 1 ]]; '
                    "then exit 1; fi\n"
                )
                uv.chmod(0o755)
                environment = {
                    **os.environ,
                    "PATH": str(root) + ":" + os.environ["PATH"],
                    "TRACE": str(log),
                    "FAIL": "1" if fail else "0",
                }
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts/install.sh")],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1 if fail else 0)
                calls = log.read_text()
                if fail:
                    self.assertNotIn("tool uninstall", calls)
                else:
                    self.assertIn("tool uninstall lma-views", calls)
                    self.assertLess(
                        calls.index("tool install"),
                        calls.index("tool uninstall"),
                    )
