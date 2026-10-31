"""Private collection mappings with comment-preserving TOML updates."""

import csv
import fcntl
import os
import stat
import tempfile
from pathlib import Path

import tomlkit


def validate(values):
    if not isinstance(values, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in values.items()
    ):
        raise ValueError("uploader.collections must map prefixes to names")
    return values


def load_csv(path):
    """Read legacy data for explicit migration or a CLI override."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        values = {}
        for number, row in enumerate(csv.reader(handle), 1):
            if not row:
                continue
            if len(row) != 2 or not all(part.strip() for part in row):
                raise ValueError(f"Invalid collection CSV row {number}")
            if row[0] in values and values[row[0]] != row[1]:
                raise ValueError(f"Conflicting collection prefix: {row[0]}")
            values[row[0]] = row[1]
    return values


def replace_text(path, text):
    """Atomically replace a config while retaining its permission bits."""
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".config-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_collection(path, abbreviation, collection):
    """Read fresh under a directory lock, retaining other tools' settings."""
    path = Path(path).resolve()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        section = (
            document["uploader"]
            if {"uploader", "views"}.intersection(document)
            else document
        )
        if "collections" not in section:
            section["collections"] = tomlkit.table()
        values = section["collections"]
        validate(values)
        if values.get(abbreviation) == collection:
            return
        values[abbreviation] = collection
        text = tomlkit.dumps(document)
        tomlkit.parse(text)
        replace_text(path, text)
    finally:
        os.close(descriptor)
