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


SECTION_NAMES = {"upload", "report"}


def read_section(path, section):
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    unknown = set(data) - SECTION_NAMES
    if unknown:
        raise ValueError(f"Unknown config sections: {sorted(unknown)}")
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"[{key}] must be a TOML table")
    return data.get(section, {})
