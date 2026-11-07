"""Import private defaults and collection mappings without losing originals."""

import argparse
import os
from pathlib import Path

import tomlkit

from lma_tools.config import SECTION_ALIASES, section_key
from lma_tools.upload.collections import (
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


def upgrade(path):
    path = Path(path).resolve()
    original = path.read_text(encoding="utf-8")
    document = tomlkit.parse(original)
    section_key(document, "upload")
    renamed = False
    for current, legacy in SECTION_ALIASES.items():
        if legacy in document:
            document[current] = document.pop(legacy)
            renamed = True
    if "upload" not in document:
        raise ValueError("Upgrade requires a merged [upload] config")
    section = document["upload"]
    if isinstance(section.get("collections"), dict):
        validate(section["collections"])
        if not renamed:
            print("Collection mappings already use TOML; nothing changed.")
            return
    else:
        import_collections(section, path.parent)
    values = section.pop("collections")
    tail = tomlkit.document()
    tail["upload"] = tomlkit.table(is_super_table=True)
    tail["upload"]["collections"] = values
    text = tomlkit.dumps(document).rstrip() + "\n\n" + tomlkit.dumps(tail)
    tomlkit.parse(text)
    suffix = ".before-tool-names" if renamed else ".before-collections"
    backup = path.with_name(path.name + suffix)
    create(backup, original)
    replace_text(path, text)
    print(f"Upgraded {path}; original saved as {backup.name}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", "--uploader", dest="upload", type=Path)
    parser.add_argument("--report", "--views", dest="report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--upgrade", type=Path, help="upgrade a merged config")
    args = parser.parse_args()
    try:
        if args.upgrade:
            if args.upload or args.report or args.output:
                parser.error("--upgrade cannot be combined with merge options")
            upgrade(args.upgrade)
        elif args.upload and args.report and args.output:
            merge(args.upload, args.report, args.output)
        else:
            parser.error("supply --upgrade or --upload, --report and --output")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
