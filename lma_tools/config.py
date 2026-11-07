"""Stable config discovery, independent of the caller's directory."""

import os
import tomllib
from pathlib import Path


def config_path(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    if value := os.environ.get("LMA_TOOLS_CONFIG"):
        return Path(value).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / "pyproject.toml").is_file():
        return checkout / "config.toml"
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "lma-tools/config.toml"


SECTION_ALIASES = {"upload": "uploader", "report": "views"}
SECTION_NAMES = set(SECTION_ALIASES) | set(SECTION_ALIASES.values())


def section_key(data, section):
    """Read old private config headers without rewriting their contents."""
    for current, legacy in SECTION_ALIASES.items():
        if current in data and legacy in data:
            raise ValueError(f"Use only [{current}] or [{legacy}], not both.")
    if section in data:
        return section
    legacy = SECTION_ALIASES.get(section)
    return legacy if legacy in data else None


def read_section(path, section):
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    if SECTION_NAMES.intersection(data):
        unknown = set(data) - SECTION_NAMES
        if unknown:
            raise ValueError(f"Unknown config sections: {sorted(unknown)}")
        key = section_key(data, section)
        data = data[key] if key else {}
        if not isinstance(data, dict):
            raise ValueError(f"[{section}] must be a TOML table")
    # Flat files remain accepted for explicit legacy configs.
    return data
