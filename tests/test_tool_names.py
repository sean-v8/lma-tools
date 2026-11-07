"""Installed tool names match their public entry points."""

import tomllib
import unittest
from pathlib import Path

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
