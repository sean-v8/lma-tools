#!/usr/bin/env python3
"""Review and upload a prepared Live Music Archive show directory."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from fractions import Fraction

from mutagen.flac import FLAC

from .notes import parse_recording_section

SMALL = {".txt", ".jpg", ".md5", ".ffp"}
REQUIRED = (
    "creator",
    "title",
    "date",
    "collection",
    "description",
    "taper",
    "transferer",
    "source",
    "lineage",
    "coverage",
    "venue",
    "runtime",
)


def show_identity(identifier):
    match = re.fullmatch(
        r"([A-Za-z0-9_.-]+?)(\d{4}-\d{2}-\d{2})([A-Za-z0-9_.-]*)", identifier
    )
    if not match:
        raise ValueError(
            "Folder/identifier must contain a band abbreviation followed by "
            "YYYY-MM-DD."
        )
    date.fromisoformat(match[2])
    return match[1], match[2]


def parse_notes(text, identifier):
    """Parse a four-line show header and blank-separated recording notes."""
    _, show_date = show_identity(identifier)
    text = text.rstrip("\r\n")
    lines = text.splitlines()
    if len(lines) < 4 or any(not x.strip() for x in lines[:4]):
        raise ValueError(
            "Expected artist, date, venue and city on the first four lines."
        )
    creator, notes_date, venue, coverage = [x.strip() for x in lines[:4]]
    try:
        parsed = datetime.strptime(notes_date, "%B %d, %Y").date()
    except ValueError:
        parsed = None
    if parsed and parsed.isoformat() != show_date:
        raise ValueError(
            "Notes date and folder date disagree; correct them before "
            "proceeding."
        )
    person = None
    person_index = None
    for index, line in enumerate(lines[4:], 4):
        match = re.fullmatch(r"\s*(.+?)\s*<[^<>]+@[^<>]+>\s*", line)
        if match:
            person, person_index = match, index
            break
    if person is None:
        raise ValueError(
            "Cannot find a Name <email> line for taper/transferer."
        )
    source, lineage = parse_recording_section(lines[4:person_index])
    return dict(
        mediatype="etree",
        creator=creator,
        title=f"{creator} Live at {venue} on {show_date}",
        date=show_date,
        description=text,
        taper=person[1].strip(),
        transferer=person[1].strip(),
        source=source,
        lineage=lineage,
        coverage=coverage,
        venue=venue,
    )


def discover(directory, notes=None):
    flacs = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() == ".flac"
    )
    small = sorted(
        (
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in SMALL
        ),
        key=lambda p: (p.suffix.lower() != ".txt", p.stat().st_size, p.name),
    )
    notes = notes or directory / (directory.name + ".txt")
    if not notes.is_file():
        candidates = [p for p in small if p.suffix.lower() == ".txt"]
        if len(candidates) != 1:
            raise ValueError(
                "Select the show notes explicitly with --notes PATH."
            )
        notes = candidates[0]
    if notes not in small:
        raise ValueError("--notes must be a .txt file in the show directory.")
    small.remove(notes)
    small.insert(0, notes)
    if not flacs:
        raise ValueError("No FLAC files found in the show directory.")
    return notes, small, flacs


def audio_duration(flacs):
    """Exact seconds from FLAC STREAMINFO; no floating-point arithmetic."""
    total = Fraction(0)
    count = 0
    for path in flacs:
        info = FLAC(path).info
        samples, rate = info.total_samples, info.sample_rate
        if (
            not isinstance(samples, int)
            or not isinstance(rate, int)
            or samples <= 0
            or rate <= 0
        ):
            raise ValueError(
                f"Missing/invalid FLAC sample count or sample rate: {path}"
            )
        total += Fraction(samples, rate)
        count += 1
    if not count:
        raise ValueError("Cannot calculate runtime without FLAC files.")
    return total


def audio_runtime(flacs):
    """Round the exact show total once; half-second ties round upward."""
    seconds = (audio_duration(flacs) + Fraction(1, 2)) // 1
    return str(timedelta(seconds=seconds))


def validate_fingerprints(directory, flacs, allow_missing=False):
    """Check FFP track inventory and embedded FLAC stream fingerprints, if
    supplied.
    """
    actual = {p.name: p for p in flacs}
    manifests = list(directory.glob("*.ffp"))
    for manifest in manifests:
        expected = {}
        for line in manifest.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.startswith(";"):
                continue
            match = re.fullmatch(r"(.+):\s*([0-9a-fA-F]{32})\s*", line)
            if not match or match[1] in expected:
                raise ValueError(
                    f"Invalid/duplicate fingerprint in {manifest.name}: {line}"
                )
            expected[match[1]] = match[2].lower()
        if set(actual) - set(expected) or (
            not allow_missing and set(expected) != set(actual)
        ):
            raise ValueError(
                f"{manifest.name}: FLAC inventory differs. "
                f"Missing: {sorted(set(expected) - set(actual))}; "
                f"unlisted: {sorted(set(actual) - set(expected))}"
            )
        for name, digest in expected.items():
            if name not in actual:
                continue
            if f"{FLAC(actual[name]).info.md5_signature:032x}" != digest:
                raise ValueError(
                    f"FLAC stream fingerprint disagrees with FFP: {name}"
                )


def check_response(response):
    response.raise_for_status()
    # Metadata/task endpoints can report application failures with HTTP 200.
    try:
        result = response.json()
    except ValueError:
        return
    if isinstance(result, dict) and (
        result.get("success") is False or result.get("error")
    ):
        raise ValueError(f"Archive.org rejected request: {result}")
