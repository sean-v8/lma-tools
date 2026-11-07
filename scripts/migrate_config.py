"""Import private defaults and collection mappings without losing originals."""

import argparse
import fcntl
import os
from pathlib import Path

import tomlkit

from lma_tools.config import SECTION_ALIASES, section_key
from lma_tools.upload.collections import (
    load_csv,
    replace_text,
    sort_prefixes,
    validate,
)


def import_collections(section, root):
    if "prefix" in section and "collections" in section:
        raise ValueError("Use only upload.prefix, not both mapping tables")
    values = section.get("prefix", section.get("collections", {}))
    if isinstance(values, str):
        source = Path(os.path.expandvars(values)).expanduser()
        if not source.is_absolute():
            source = root / source
        values = load_csv(source)
    elif (
        "prefix" not in section
        and "collections" not in section
        and (root / "collections.csv").exists()
    ):
        values = load_csv(root / "collections.csv")
    validate(values)
    table = tomlkit.table()
    table.update(values)
    sort_prefixes(table)
    section.pop("collections", None)
    section["prefix"] = table


def create(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation and private permissions from the outset.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def merge(upload, report, destination):
    upload, report, destination = map(Path, (upload, report, destination))
    text = (
        "[upload]\n"
        + (upload / "config.toml").read_text(encoding="utf-8")
        + "\n[report]\n"
        + (report / "config.toml").read_text(encoding="utf-8")
        + "\n"
    )
    document = tomlkit.parse(text)
    import_collections(document["upload"], upload)
    create(destination, tomlkit.dumps(document))
    print(f"Created {destination}; originals unchanged.")


def backup_path(path, backup_dir=None):
    path = Path(path).resolve()
    if backup_dir is None:
        parent = Path(os.environ.get("GIT", path.parent.parent)).expanduser()
        backup_dir = parent / ".bak"
    return Path(backup_dir) / (f"{path.parent.name}-{path.name}.before-prefix")


def upgrade(path, backup_dir=None):
    path = Path(path).resolve()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        original = path.read_text(encoding="utf-8")
        document = tomlkit.parse(original)
        section_key(document, "upload")
        for current, alias in SECTION_ALIASES.items():
            if alias in document:
                document[current] = document.pop(alias)
        if "upload" not in document:
            raise ValueError("Upgrade requires a merged [upload] config")
        section = document["upload"]
        if "prefix" in section and "collections" in section:
            raise ValueError("Use only upload.prefix, not both mapping tables")
        values = section.get("prefix", section.get("collections"))
        if "prefix" in section and not isinstance(values, dict):
            raise ValueError("upload.prefix must be a TOML table")
        if isinstance(values, dict):
            validate(values)
        else:
            import_collections(section, path.parent)
            values = section["prefix"]
        section.pop("collections", None)
        section.pop("prefix", None)
        sort_prefixes(values)
        tail = tomlkit.document()
        tail["upload"] = tomlkit.table(is_super_table=True)
        tail["upload"]["prefix"] = values
        text = tomlkit.dumps(document).rstrip() + "\n\n"
        text += tomlkit.dumps(tail).lstrip("\n")
        tomlkit.parse(text)
        if text == original:
            print("Prefix settings already current; nothing changed.")
            return
        backup = backup_path(path, backup_dir)
        create(backup, original)
        replace_text(path, text)
        print(f"Upgraded {path}; original saved as {backup}.")
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", "--uploader", dest="upload", type=Path)
    parser.add_argument("--report", "--views", dest="report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--upgrade", type=Path, help="upgrade a merged config")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.upgrade:
            if args.upload or args.report or args.output:
                parser.error("--upgrade cannot be combined with merge options")
            upgrade(args.upgrade, backup_dir=args.backup_dir)
        elif args.upload and args.report and args.output:
            merge(args.upload, args.report, args.output)
        else:
            parser.error("supply --upgrade or --upload, --report and --output")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
