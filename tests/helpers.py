"""Shared terminal streams and config-script loading."""

import importlib.util
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "migration", ROOT / "scripts/migrate_config.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True
