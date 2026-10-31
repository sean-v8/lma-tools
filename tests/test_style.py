"""Ruff checks and strict line limits for repository text files."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUFF = shutil.which("ruff")


class StyleTests(unittest.TestCase):
    @unittest.skipUnless(RUFF, "Ruff not installed")
    def test_ruff(self):
        for arguments in (("check",), ("format", "--check")):
            result = subprocess.run(
                [RUFF, *arguments, "."],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )

    def test_text_lines(self):
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            ignored = {".venv", ".git", ".ruff_cache", "dist", "__pycache__"}
            if ignored.intersection(relative.parts):
                continue
            if relative == Path("config.toml"):
                continue
            if path.suffix not in (
                ".py",
                ".md",
                ".toml",
                ".yml",
                ".bash",
                ".sh",
            ):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertLessEqual(len(line), 79, f"{relative}:{number}")
