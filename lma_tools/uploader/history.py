"""Suggest collection naming from a small, bounded recent-item sample."""

import re
from collections import Counter

from internetarchive import ArchiveSession
from requests import RequestException


def quoted(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def naming(record):
    creator, title = record.get("creator"), record.get("title")
    if isinstance(creator, list) and len(creator) == 1:
        creator = creator[0]
    if not isinstance(creator, str) or not isinstance(title, str):
        return None
    match = re.fullmatch(r"(.+) Live at .+ on \d{4}-\d{2}-\d{2}", title)
    if not creator.strip() or match is None:
        return None
    return creator, match[1]


def consensus(records):
    choices = [choice for record in records if (choice := naming(record))]
    if not choices:
        return None
    choice, count = Counter(choices).most_common(1)[0]
    if count >= 2 and count > len(choices) / 2:
        return choice, count, len(choices)
    return None


def suggest(identifier, metadata):
    """Read only; failure leaves the local notes available for review."""
    original = dict(metadata)
    collection = metadata.get("collection")
    if not collection:
        return original, "No collection selected; using local naming."
    try:
        with ArchiveSession() as session:
            item = session.get_item(
                identifier, request_kwargs={"timeout": (5, 10)}
            )
            if item.exists:
                for key in ("creator", "title"):
                    value = item.metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        original[key] = value
                return original, "Creator/Title from the existing item."
            email = session.user_email
            if isinstance(email, str) and email:
                identity = "uploader:" + quoted(email)
                owner = "your uploads"
            else:
                identity = "transferer:" + quoted(metadata["transferer"])
                owner = "matching transferer uploads"
            base = (
                "collection:"
                + quoted(collection)
                + " AND mediatype:etree AND NOT identifier:"
                + quoted(identifier)
            )

            def search(query):
                response = session.get(
                    "https://archive.org/advancedsearch.php",
                    params={
                        "q": query,
                        "fl[]": ["identifier", "creator", "title"],
                        "sort[]": ["publicdate desc", "identifier asc"],
                        "rows": 3,
                        "output": "json",
                    },
                    timeout=(5, 10),
                )
                response.raise_for_status()
                docs = response.json()["response"]["docs"]
                if not isinstance(docs, list) or not all(
                    isinstance(doc, dict) for doc in docs
                ):
                    raise ValueError("Invalid collection search response")
                return docs[:3]

            own = search(base + " AND (" + identity + ")")
            result = consensus(own)
            if result is None:
                others = search(base + " AND NOT (" + identity + ")")
                # Fill a short personal sample before trying others alone.
                sample = [doc for doc in own if naming(doc)]
                seen = {doc.get("identifier") for doc in sample}
                sample += [
                    doc
                    for doc in others
                    if naming(doc) and doc.get("identifier") not in seen
                ][: max(0, 3 - len(sample))]
                result = consensus(sample) or consensus(others)
                owner = "recent collection uploads (yours checked first)"
            if result is None:
                return original, "No naming consensus; using local naming."
            (creator, title_artist), count, total = result
            original["creator"] = creator
            original["title"] = (
                f"{title_artist} Live at {metadata['venue']} "
                f"on {metadata['date']}"
            )
            return original, f"Naming: {count}/{total} {owner}; review below."
    except (RequestException, ValueError, KeyError, TypeError, OSError):
        return dict(metadata), "History unavailable; using local naming."
