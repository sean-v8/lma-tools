"""Import private defaults and collection mappings without losing originals."""

import argparse
import os
from pathlib import Path

import tomlkit

from lma_tools.uploader.collections import (
    load_csv,
    replace_text,
    validate,
)


def import_collections(section, root):
    values = section.get("collections", {})
    if isinstance(values, str):
        source = Path(os.path.expandvars(values)).expanduser()
        if not source.is_absolute():
            source = root / source
        values = load_csv(source)
    elif "collections" not in section and (root / "collections.csv").exists():
        values = load_csv(root / "collections.csv")
    validate(values)
    table = tomlkit.table()
    table.update(values)
    section["collections"] = table


def create(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation and private permissions from the outset.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def merge(uploader, views, destination):
    uploader, views, destination = map(Path, (uploader, views, destination))
    text = (
        "[uploader]\n"
        + (uploader / "config.toml").read_text(encoding="utf-8")
        + "\n[views]\n"
        + (views / "config.toml").read_text(encoding="utf-8")
        + "\n"
    )
    document = tomlkit.parse(text)
    import_collections(document["uploader"], uploader)
    create(destination, tomlkit.dumps(document))
    print(f"Created {destination}; originals unchanged.")


def upgrade(path):
    path = Path(path).resolve()
    original = path.read_text(encoding="utf-8")
    document = tomlkit.parse(original)
    if "uploader" not in document:
        raise ValueError("Upgrade requires a merged [uploader] config")
    section = document["uploader"]
    if isinstance(section.get("collections"), dict):
        validate(section["collections"])
        print("Collection mappings already use TOML; nothing changed.")
        return
    import_collections(section, path.parent)
    backup = path.with_name(path.name + ".before-collections")
    create(backup, original)
    replace_text(path, tomlkit.dumps(document))
    print(f"Upgraded {path}; original saved as {backup.name}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uploader", type=Path)
    parser.add_argument("--views", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--upgrade", type=Path, help="upgrade a merged config")
    args = parser.parse_args()
    try:
        if args.upgrade:
            if args.uploader or args.views or args.output:
                parser.error("--upgrade cannot be combined with merge options")
            upgrade(args.upgrade)
        elif args.uploader and args.views and args.output:
            merge(args.uploader, args.views, args.output)
        else:
            parser.error(
                "supply --upgrade or --uploader, --views and --output"
            )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
