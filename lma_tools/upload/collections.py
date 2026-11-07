"""Private collection mappings with comment-preserving TOML updates."""

import fcntl
import os
import stat
import tempfile
from pathlib import Path

import tomlkit

from lma_tools.config import SECTION_NAMES


def preference(value):
    """Normalize a collection name or per-prefix preference table."""
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
        raise ValueError("upload.prefix must map prefixes to preferences")
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Collection prefixes must be nonempty text")
        preference(value)
    return values


def get_preference(config, prefix):
    value = config.get("prefix", {}).get(prefix)
    return preference(value) if value is not None else {}


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


def sort_prefixes(values):
    """Sort preference entries by prefix while retaining TOML value trivia."""
    entries = sorted(
        values.items(), key=lambda item: (item[0].casefold(), item[0])
    )
    if list(values) != [name for name, _ in entries]:
        for name, _ in entries:
            del values[name]
        for name, value in entries:
            values[name] = value


def save_collection(path, abbreviation, collection, creator=None, genre=None):
    """Read fresh under a directory lock, retaining other tools' settings."""
    path = Path(path).resolve()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        unknown = set(document) - SECTION_NAMES
        if unknown:
            raise ValueError(f"Unknown config sections: {sorted(unknown)}")
        section = document.setdefault("upload", tomlkit.table())
        if "collections" in section:
            raise ValueError("Unknown upload setting: collections")
        if "prefix" not in section:
            section["prefix"] = tomlkit.table()
        values = section["prefix"]
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
        sort_prefixes(values)
        # Keep prefix blocks after ordinary upload/report settings.
        del section["prefix"]
        text = tomlkit.dumps(document).rstrip() + "\n\n"
        tail = tomlkit.document()
        tail["upload"] = tomlkit.table(is_super_table=True)
        tail["upload"]["prefix"] = values
        text += tomlkit.dumps(tail).lstrip("\n")
        tomlkit.parse(text)
        if text != path.read_text(encoding="utf-8"):
            replace_text(path, text)
    finally:
        os.close(descriptor)
