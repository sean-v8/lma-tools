"""Private collection mappings with comment-preserving TOML updates."""

import csv
import fcntl
import os
import stat
import tempfile
from pathlib import Path

import tomlkit


def preference(value):
    """Normalize legacy names and named per-prefix preferences."""
    if isinstance(value, str) and value.strip():
        return {"collection": value}
    if not isinstance(value, dict) or set(value) - {
        "collection",
        "creator",
        "genre",
    }:
        raise ValueError("Invalid collection preference")
    collection = value.get("collection")
    if (
        not isinstance(collection, str)
        or not collection.strip()
        or any(not isinstance(v, str) for v in value.values())
    ):
        raise ValueError("Collection is required; preferences need text")
    return {k: v for k, v in value.items() if v.strip()}


def validate(values):
    if not isinstance(values, dict):
        raise ValueError("uploader.collections must map prefixes to names")
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Collection prefixes must be nonempty text")
        preference(value)
    return values


def get_preference(config, prefix):
    value = config.get("collections", {}).get(prefix)
    return preference(value) if value is not None else {}


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


def save_collection(path, abbreviation, collection, creator=None, genre=None):
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
        existing = values.get(abbreviation)
        if creator is None and genre is None:
            updated = collection
            if isinstance(existing, dict):
                updated = {**existing, "collection": collection}
        else:
            updated = preference(existing) if existing is not None else {}
            updated["collection"] = collection
            if creator:
                updated["creator"] = creator
            if genre is not None:
                if genre.strip():
                    updated["genre"] = genre.strip()
                else:
                    updated.pop("genre", None)
        if existing != updated:
            if isinstance(updated, dict):
                table = tomlkit.table()
                table.update(updated)
                comment = getattr(
                    getattr(existing, "trivia", None), "comment", ""
                )
                if comment:
                    table.comment(comment.lstrip("# "))
                values[abbreviation] = table
            else:
                values[abbreviation] = updated
        # Keep every collection block after ordinary uploader/views settings.
        del section["collections"]
        text = tomlkit.dumps(document).rstrip() + "\n\n"
        tail = tomlkit.document()
        if section is document:
            tail["collections"] = values
        else:
            tail["uploader"] = tomlkit.table(is_super_table=True)
            tail["uploader"]["collections"] = values
        text += tomlkit.dumps(tail)
        tomlkit.parse(text)
        if text != path.read_text(encoding="utf-8"):
            replace_text(path, text)
    finally:
        os.close(descriptor)
