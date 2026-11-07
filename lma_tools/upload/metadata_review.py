"""Read-only comparisons of fresh local metadata against Archive values."""

import html
import json
from difflib import unified_diff

from internetarchive import ArchiveSession
from requests import RequestException


def snapshot(identifier):
    """None means unavailable; an empty dict means the item does not exist."""
    try:
        with ArchiveSession() as session:
            item = session.get_item(
                identifier, request_kwargs={"timeout": (5, 10)}
            )
            return dict(item.metadata) if item.exists else {}
    except (RequestException, ValueError, TypeError, OSError):
        return None


def comparison(remote, proposed):
    """Return TOML comments only; never turn remote fields into defaults."""
    if remote is None:
        return "# Remote comparison unavailable; review proposed values.\n"
    if not remote:
        return "# New item: proposed metadata below.\n"
    lines = ["# Changes from current Archive metadata (- remote, + proposed):"]
    for key, value in proposed.items():
        old = remote.get(key)
        if isinstance(old, list) and len(old) == 1:
            old = old[0]
        if key == "description" and isinstance(old, str):
            old = html.unescape(old).rstrip("\r\n")
        if old == value:
            continue
        lines.append(f"# {key}:")
        if isinstance(old, str) and ("\n" in old or "\n" in value):
            lines.extend(
                "# " + line
                for line in unified_diff(
                    old.splitlines(),
                    value.splitlines(),
                    fromfile="Archive",
                    tofile="Proposed",
                    lineterm="",
                    n=1,
                )
            )
        else:
            lines.append("# - " + json.dumps(old, ensure_ascii=False))
            lines.append("# + " + json.dumps(value, ensure_ascii=False))
    if len(lines) == 1:
        lines.append("# No proposed metadata differences.")
    return "\n".join(lines) + "\n"
