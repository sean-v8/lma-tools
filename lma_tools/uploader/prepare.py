"""Editable local show preparation, staged outside the output directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime
from pathlib import Path

from mutagen import File
from mutagen.flac import FLAC

from . import ui
from .core import SMALL, show_identity
from .encoder import encode_verbatim, tag_audio, tags_match, verify_pcm
from .notes import ROOM_HINTS, SPOT_HINTS


def _normal(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _name(value):
    return (
        isinstance(value, str)
        and bool(value)
        and Path(value).name == value
        and value not in {".", ".."}
        and "\\" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _reject_symlinks(directory):
    for path in directory.iterdir():
        if path.is_symlink() and path.suffix.lower() in SMALL | {
            ".wav",
            ".flac",
        }:
            raise ValueError(
                f"Symlink not allowed in preparation: {path.name}"
            )


def _portable_defaults(sources, artist, venue, show_date):
    """Reuse consistent reviewed tags only when notes identity still agrees."""
    if not sources or any(p.suffix.lower() != ".flac" for p in sources):
        return {}
    tags = [FLAC(path) for path in sources]
    if any(
        audio.get("artist") != [artist]
        or audio.get("venue") != [venue]
        or audio.get("date") not in ([show_date], [show_date[:4]])
        for audio in tags
    ):
        return {}
    result = {}
    for key in ("album", "genre"):
        values = [audio.get(key, []) for audio in tags]
        if (
            all(value == values[0] for value in values)
            and len(values[0]) == 1
            and values[0][0].strip()
        ):
            result[key] = values[0][0]
    return result


def build_plan(directory, config_entry, notes=None):
    """Discover notes, set sections and uniquely matched audio titles."""
    directory = Path(directory).resolve()
    _reject_symlinks(directory)
    prefix, folder_date = show_identity(directory.name)
    candidates = sorted(directory.glob("*.txt"))
    if notes is None:
        preferred = directory / f"{directory.name}.txt"
        if preferred in candidates:
            notes = preferred
        elif len(candidates) == 1:
            notes = candidates[0]
        else:
            raise ValueError("Select one notes file explicitly.")
    notes = Path(notes)
    if not notes.is_absolute():
        notes = directory / notes
    if notes.parent.resolve() != directory or not notes.is_file():
        raise ValueError("Notes must be a file in the show directory.")
    text = notes.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if len(lines) < 4 or any(not line.strip() for line in lines[:4]):
        raise ValueError("Expected artist, date, venue and coverage header.")
    artist, raw_date, venue, coverage = map(str.strip, lines[:4])
    try:
        show_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        show_date = datetime.strptime(raw_date, "%B %d, %Y").date()
        show_date = show_date.isoformat()
    if show_date != folder_date:
        raise ValueError("Notes date differs from directory date.")
    recording = []
    for line in lines[4:]:
        if re.match(r"\s*(?:Set\s*\d|\d+\s*[.)-])", line, re.I):
            break
        recording.append(line)
    recording = "\n".join(recording)
    room = bool(ROOM_HINTS.search(recording)) or bool(
        re.search(r"\b(?:microphones?|mic pair)\b", recording, re.I)
    )
    direct = bool(
        re.search(
            r"\b(?:sbd|soundboard|multitrack|direct feed)\b", recording, re.I
        )
    ) or any(
        SPOT_HINTS.search(line) and not ROOM_HINTS.search(line)
        for line in recording.splitlines()
    )
    kind = "MTX" if room and direct else "SBD" if direct else "AUD"
    if re.search(r"\b(?:mtx|matrix)\b", recording, re.I):
        kind = "MTX"
    rows = []
    disc = 1
    local = 0
    explicit_sets = False
    for line in lines[4:]:
        section = re.fullmatch(
            r"\s*Set\s*(\d+)\s*(?:\([^()\r\n]*\))?\s*:?\s*",
            line,
            re.I,
        )
        if section:
            explicit_sets = True
            disc, local = int(section[1]), 0
            continue
        match = re.match(r"^\s*\d+\s*[.)-]\s*(\S.*)$", line)
        if match:
            local += 1
            title = match[1].strip()
            title = re.sub(r"(?:\s+(?:[^\w\s>/]|_)+)+$", "", title).rstrip()
            title = re.sub(
                r"(?:\s+(?:[^\w\s>/]|_)+)+"
                r"(?=\s*(?:(?:>|//)\s*)+$)",
                "",
                title,
            ).rstrip()
            rows.append((disc, local, title))
    if not rows:
        raise ValueError("No numbered setlist rows found.")
    sources = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {".wav", ".flac"}
    )
    defaults = _portable_defaults(sources, artist, venue, show_date)
    redundant_wavs = []
    for source in sources:
        if source.suffix.lower() != ".wav":
            continue
        paired = [
            p
            for p in sources
            if p.stem == source.stem and p.suffix.lower() == ".flac"
        ]
        if len(paired) == 1:
            redundant_wavs.append(
                dict(
                    source=source.name,
                    track_source=paired[0].name,
                )
            )
    redundant_names = {row["source"] for row in redundant_wavs}
    sources = [p for p in sources if p.name not in redundant_names]
    titles = {}
    for source in sources:
        audio = File(source)
        tags = audio.tags if audio is not None else None
        values = list(tags.get("title", [])) if tags else []
        if tags and hasattr(tags, "getall"):
            values.extend(
                str(value)
                for frame in tags.getall("TIT2")
                for value in frame.text
            )
        titles[source.name] = values
    tracks = []
    for number, (disc, local, title) in enumerate(rows, 1):
        set_part = f"s{disc}" if explicit_sets else ""
        stem = f"{prefix}{show_date}{set_part}t{local:02d}"
        accepted = {stem, f"{prefix}{show_date}s{disc}t{local:02d}"}
        matches = [
            p.name
            for p in sources
            if _normal(title)
            in {
                _normal(p.stem),
                _normal(re.sub(r"^\d+[ ._-]+", "", p.stem)),
                *(_normal(value) for value in titles[p.name]),
            }
            or p.stem in accepted
        ]
        canonical = [p.name for p in sources if p.stem in accepted]
        if len(canonical) == 1:
            matches = canonical
        elif len(matches) > 1:
            numbered = []
            for name in matches:
                ordinal = re.match(r"^(\d+)[ ._-]+", Path(name).stem)
                if ordinal and int(ordinal[1]) == number:
                    numbered.append(name)
            if len(numbered) == 1:
                matches = numbered
        tracks.append(
            dict(
                source=matches[0] if len(matches) == 1 else "",
                destination=stem + ".flac",
                title=title,
                disc=disc,
                track=number,
            )
        )
    artwork = sorted(directory.glob("*.jpg"))
    return dict(
        artist=artist,
        album=defaults.get("album", f"{show_date} {coverage} ({kind})"),
        genre=config_entry.get("genre") or defaults.get("genre", ""),
        venue=venue,
        date=show_date,
        artwork=artwork[0].name if len(artwork) == 1 else "",
        tracks=tracks,
        redundant_wavs=redundant_wavs,
    )


def review_text(plan):
    """Render editable TOML without creating a review file."""
    lines = ["# Ctrl-S accepts; Ctrl-C cancels."]
    for key, value in plan.items():
        if key not in {"tracks", "redundant_wavs"}:
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    if not plan.get("redundant_wavs"):
        lines.append("redundant_wavs = []")
    disc_total = max(
        (
            row["disc"]
            for row in plan.get("tracks", [])
            if type(row.get("disc")) is int
        ),
        default=1,
    )
    for table in ("tracks", "redundant_wavs"):
        if table == "redundant_wavs" and plan.get(table):
            lines.extend(
                [
                    "",
                    "# Remove these WAVs only after staged PCM verification.",
                ]
            )
        for row in plan.get(table, []):
            lines.extend(["", f"[[{table}]]"])
            for key, value in row.items():
                if table == "tracks" and type(value) is int:
                    if key == "disc":
                        value = f"{value}/{disc_total}"
                    elif key == "track":
                        value = f"{value}/{len(plan['tracks'])}"
                encoded = json.dumps(value, ensure_ascii=False)
                lines.append(f"{key} = {encoded}")
    return "\n".join(lines) + "\n"


def validate_plan(text, directory):
    """Require a complete one-to-one mapping before any filesystem writes."""
    directory = Path(directory)
    _reject_symlinks(directory)
    try:
        plan = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(str(exc)) from exc
    plan.setdefault("genre", "")
    if not isinstance(plan["genre"], str):
        raise ValueError("Genre must be text or blank.")
    plan["genre"] = plan["genre"].strip()
    for key in ("artist", "album", "venue", "date"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ValueError(f"Missing text field: {key}")
    plan.setdefault("redundant_wavs", [])
    if set(plan) != {
        "artist",
        "album",
        "genre",
        "venue",
        "date",
        "artwork",
        "tracks",
        "redundant_wavs",
    }:
        raise ValueError("Unknown or missing preparation fields.")
    date.fromisoformat(plan["date"])
    artwork = plan.get("artwork")
    if artwork != "" and (
        not _name(artwork)
        or Path(artwork).suffix.lower() != ".jpg"
        or not (directory / artwork).is_file()
    ):
        raise ValueError("Artwork must name an existing JPG or be empty.")
    tracks = plan.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("Tracks must be a nonempty array.")
    sources, destinations, discs = set(), set(), set()
    displayed_totals = []
    for number, track in enumerate(tracks, 1):
        if not isinstance(track, dict) or set(track) != {
            "source",
            "destination",
            "title",
            "disc",
            "track",
        }:
            raise ValueError("Unknown or missing track fields.")
        for field in ("disc", "track"):
            value = track[field]
            if isinstance(value, str):
                match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
                if match is None:
                    raise ValueError(f"{field} must use number/total.")
                track[field] = int(match[1])
                displayed_totals.append((field, int(match[2])))
        for key in ("source", "destination"):
            if not _name(track.get(key)):
                raise ValueError(f"Invalid or ambiguous {key} at {number}.")
        source, destination = track["source"], track["destination"]
        if source in sources or destination.casefold() in destinations:
            raise ValueError("Duplicate source or destination.")
        if not (directory / source).is_file():
            raise ValueError(f"Missing source: {source}")
        if Path(source).suffix.lower() not in {".wav", ".flac"}:
            raise ValueError("Source must be WAV or FLAC.")
        if Path(destination).suffix != ".flac":
            raise ValueError("Destination must end in .flac.")
        if type(track.get("disc")) is not int or track["disc"] < 1:
            raise ValueError("Disc must be a positive integer.")
        if type(track.get("track")) is not int or track["track"] != number:
            raise ValueError("Track numbers must be global and consecutive.")
        if (
            not isinstance(track.get("title"), str)
            or not track["title"].strip()
        ):
            raise ValueError("Track title is required.")
        sources.add(source)
        destinations.add(destination.casefold())
        discs.add(track["disc"])
    for field, total in displayed_totals:
        expected = max(discs) if field == "disc" else len(tracks)
        if total != expected:
            raise ValueError(f"{field} total must be {expected}.")
    redundant = plan["redundant_wavs"]
    if not isinstance(redundant, list):
        raise ValueError("redundant_wavs must be an array.")
    track_sources = {t["source"] for t in tracks}
    for row in redundant:
        if not isinstance(row, dict) or set(row) != {"source", "track_source"}:
            raise ValueError("Redundant WAV needs source and track_source.")
        source, linked = row["source"], row["track_source"]
        if (
            not _name(source)
            or not _name(linked)
            or Path(source).suffix.lower() != ".wav"
            or Path(linked).suffix.lower() != ".flac"
            or linked not in track_sources
            or source in sources
            or not (directory / source).is_file()
        ):
            raise ValueError("Invalid or duplicate redundant WAV mapping.")
        sources.add(source)
    audio = {
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {".wav", ".flac"}
    }
    if any(
        (directory / t["destination"]).exists()
        and t["destination"] not in sources
        for t in tracks
    ):
        raise ValueError("Destination would overwrite an unrelated file.")
    if sources != audio:
        raise ValueError("Map every audio source exactly once.")
    return plan


def _md5(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def _manifests(directory, plan):
    flacs = sorted(directory.glob("*.flac"))
    ffp = "".join(
        f"{p.name}:{FLAC(p).info.md5_signature:032x}\n" for p in flacs
    )
    files = sorted(
        p
        for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() in SMALL | {".flac"}
        and p.suffix.lower() != ".md5"
    )
    md5 = "".join(f"{_md5(p)}  {p.name}\n" for p in files)
    return ffp, md5


def preparation_workers(value=None):
    """Default to one whole-track job per available logical CPU."""
    if value is None:
        count = getattr(os, "process_cpu_count", os.cpu_count)()
        return count or 1
    if type(value) is not int or value < 1:
        raise ValueError("prepare_workers must be a positive integer")
    return value


def _prepare_tracks(tracks, workers, prepare, announce=None):
    """Bound active jobs; drain failures before staged files can be removed."""
    workers = min(preparation_workers(workers), len(tracks))
    if workers <= 1:
        for entry in enumerate(tracks, 1):
            if announce:
                announce(*entry)
            prepare(*entry)
        return
    entries = iter(enumerate(tracks, 1))
    pool = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lmau-prepare"
    )
    pending = set()

    def submit(entry):
        if announce:
            announce(*entry)
        return pool.submit(prepare, *entry)

    try:
        for _ in range(workers):
            pending.add(submit(next(entries)))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            for _ in done:
                entry = next(entries, None)
                if entry is not None:
                    pending.add(submit(entry))
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def apply_plan(directory, plan, *, workers=None):
    """Stage, verify and commit, rolling back failures before WAV removal."""
    directory = Path(directory).resolve()
    workers = preparation_workers(workers)
    plan = validate_plan(review_text(plan), directory)
    artwork = directory / plan["artwork"] if plan["artwork"] else None
    total = len(plan["tracks"])
    discs = max(t["disc"] for t in plan["tracks"])
    matching = not plan["redundant_wavs"] and all(
        t["source"] == t["destination"]
        and tags_match(directory / t["source"], plan, t, total, discs, artwork)
        for t in plan["tracks"]
    )
    if matching:
        print("Checking prepared tags, artwork and manifests...", flush=True)
        ffp, md5 = _manifests(directory, plan)
        fp = directory / (directory.name + ".ffp")
        mp = directory / (directory.name + ".md5")
        if (
            fp.is_file()
            and mp.is_file()
            and fp.read_text(encoding="utf-8") == ffp
            and mp.read_text(encoding="utf-8") == md5
            and list(directory.glob("*.ffp")) == [fp]
            and list(directory.glob("*.md5")) == [mp]
        ):
            return plan
    fingerprints = {
        t["source"]: _md5(directory / t["source"])
        for t in [*plan["tracks"], *plan["redundant_wavs"]]
    }
    with tempfile.TemporaryDirectory(
        prefix=".lma-prepare-", dir=directory.parent
    ) as temporary:
        stage = Path(temporary) / directory.name
        stage.mkdir()
        for path in directory.iterdir():
            if (
                path.is_file()
                and path.suffix.lower() in SMALL
                and path.suffix.lower() not in {".ffp", ".md5"}
            ):
                shutil.copy2(path, stage / path.name)

        def announce(index, track):
            print(
                f"[{index}/{total}] Preparing {track['destination']}",
                flush=True,
            )

        def prepare_track(index, track):
            source = directory / track["source"]
            target = stage / track["destination"]
            if source.suffix.lower() == ".wav":
                encode_verbatim(source, target)
            else:
                shutil.copy2(source, target)
            if not tags_match(target, plan, track, total, discs, artwork):
                tag_audio(target, plan, track, total, discs, artwork)
            verify_pcm(source, target)

        _prepare_tracks(plan["tracks"], workers, prepare_track, announce)
        by_source = {t["source"]: t for t in plan["tracks"]}
        for row in plan["redundant_wavs"]:
            target = stage / by_source[row["track_source"]]["destination"]
            verify_pcm(directory / row["source"], target)
        ffp_name = directory.name + ".ffp"
        md5_name = directory.name + ".md5"
        ffp, _ = _manifests(stage, plan)
        for path in {*stage.glob("*.ffp"), stage / ffp_name}:
            path.write_text(ffp, encoding="utf-8")
        _, md5 = _manifests(stage, plan)
        for path in {*stage.glob("*.md5"), stage / md5_name}:
            path.write_text(md5, encoding="utf-8")
        changed = [
            p
            for p in stage.iterdir()
            if not (directory / p.name).exists()
            or _md5(p) != _md5(directory / p.name)
        ]
        removed = {
            t["source"]
            for t in plan["tracks"]
            if t["source"]
            not in {row["destination"] for row in plan["tracks"]}
        }
        removed.update(row["source"] for row in plan["redundant_wavs"])
        removed.update(
            p.name
            for p in directory.iterdir()
            if p.suffix.lower() in {".ffp", ".md5"}
            and p.name not in {ffp_name, md5_name}
        )
        backup = Path(temporary) / "backup"
        backup.mkdir()
        touched = {p.name for p in changed} | removed
        existing = set()
        for name in touched:
            path = directory / name
            if path.exists():
                shutil.copy2(path, backup / name)
                existing.add(name)
        for name, digest in fingerprints.items():
            if _md5(directory / name) != digest:
                raise ValueError("Source changed during preparation.")
        try:
            for path in changed:
                path.replace(directory / path.name)
            for name in removed:
                (directory / name).unlink()
        except BaseException:
            for name in touched:
                if name in existing:
                    shutil.copy2(backup / name, directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
            raise
    return plan


def prepare_show(
    directory, config_entry, preview=False, *, notes=None, workers=None
):
    """Print/return a draft in preview; otherwise edit, validate and apply.

    notes selects a TXT within directory. config_entry supplies genre; missing
    genre falls back to consistent existing FLAC tags, then blank for review.
    The dict includes common tags, tracks and redundant_wavs. Each reviewed
    redundant WAV is PCM-verified against its staged track before removal.
    FLAC DATE tags use the year only.
    """
    directory = Path(directory).resolve()
    plan = build_plan(directory, config_entry, notes)
    document = review_text(plan) + "\n# Available sources:\n"
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".wav", ".flac", ".jpg"}:
            document += "# " + json.dumps(path.name) + "\n"
    document += "# Rebuild canonical FFP/MD5 after final tagging.\n"
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".ffp", ".md5"} and path.name not in {
            directory.name + ".ffp",
            directory.name + ".md5",
        }:
            document += "# Remove superseded local manifest: "
            document += json.dumps(path.name) + "\n"
    document += "# WAV removal requires successful decoded-PCM verification.\n"
    if preview:
        print(document, end="")
        return plan
    approved = ui.edit_document(
        document, lambda text: validate_plan(text, directory)
    )
    return apply_plan(directory, approved, workers=workers)


def validate_manifests(directory, repair=False, allow_missing=True):
    """Verify manifests against the present supported-file inventory.

    Return valid/missing/errors/repaired/warnings. With allow_missing=True,
    absent manifests are allowed and absent listed files produce warnings.
    Present files omitted from existing manifests remain errors. Repair
    regenerates canonical complete manifests without rewriting any audio.
    """
    directory = Path(directory).resolve()
    errors, missing, warnings = [], [], []
    for suffix in (".ffp", ".md5"):
        paths = sorted(directory.glob("*" + suffix))
        if not paths:
            missing.append(suffix)
            if not allow_missing:
                errors.append(f"Missing {suffix} manifest.")
        inventory = set()
        for path in paths:
            if path.is_symlink():
                errors.append(f"Unsafe manifest symlink: {path.name}")
                continue
            seen = set()
            for number, line in enumerate(
                path.read_text(encoding="utf-8-sig").splitlines(), 1
            ):
                if not line.strip() or line.startswith(("#", ";")):
                    continue
                if suffix == ".ffp":
                    match = re.fullmatch(r"(.+):\s*([a-fA-F0-9]{32})", line)
                    name, digest = (match[1], match[2]) if match else ("", "")
                else:
                    match = re.fullmatch(r"([a-fA-F0-9]{32}) [ *](.+)", line)
                    digest, name = (match[1], match[2]) if match else ("", "")
                target = directory / name
                if (
                    _name(name)
                    and name not in seen
                    and not target.exists()
                    and not target.is_symlink()
                    and allow_missing
                ):
                    seen.add(name)
                    warnings.append(f"{path.name}: absent {name}, unchecked")
                    continue
                valid = (
                    _name(name)
                    and name not in seen
                    and target.is_file()
                    and not target.is_symlink()
                    and target != path
                )
                if suffix == ".ffp":
                    valid = valid and target.suffix.lower() == ".flac"
                if not valid:
                    errors.append(f"{path.name}:{number}: invalid entry")
                    continue
                seen.add(name)
                inventory.add(name)
                if suffix == ".ffp":
                    actual = f"{FLAC(target).info.md5_signature:032x}"
                else:
                    actual = _md5(target)
                if actual != digest.lower():
                    errors.append(f"{path.name}:{number}: stale {name}")
        if paths:
            expected = {
                p.name
                for p in directory.iterdir()
                if p.is_file()
                and (
                    p.suffix.lower() == ".flac"
                    if suffix == ".ffp"
                    else p.suffix.lower() in SMALL | {".flac"}
                    and p.suffix.lower() != ".md5"
                )
            }
            for name in sorted(expected - inventory):
                errors.append(f"{suffix}: unlisted present file: {name}")
    if repair and (errors or missing):
        _reject_symlinks(directory)
        with tempfile.TemporaryDirectory(
            prefix=".lma-manifests-", dir=directory.parent
        ) as temporary:
            stage = Path(temporary) / directory.name
            stage.mkdir()
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() in SMALL | {".flac"}:
                    if path.suffix.lower() not in {".md5", ".ffp"}:
                        # Hard links avoid copying or rewriting any audio.
                        (stage / path.name).hardlink_to(path)
            ffp, _ = _manifests(stage, {})
            ffp_name = directory.name + ".ffp"
            md5_name = directory.name + ".md5"
            (stage / ffp_name).write_text(ffp, encoding="utf-8")
            _, md5 = _manifests(stage, {})
            (stage / md5_name).write_text(md5, encoding="utf-8")
            # Replace only manifests. Keep rollback copies outside output.
            old = list(directory.glob("*.ffp"))
            old += list(directory.glob("*.md5"))
            backups = {p.name: p.read_bytes() for p in old}
            try:
                for path in old:
                    path.unlink()
                for name in (ffp_name, md5_name):
                    (stage / name).replace(directory / name)
            except BaseException:
                for name in (ffp_name, md5_name):
                    (directory / name).unlink(missing_ok=True)
                for name, data in backups.items():
                    (directory / name).write_bytes(data)
                raise
        return dict(
            valid=True, missing=[], errors=[], repaired=True, warnings=[]
        )
    return dict(
        valid=not errors,
        missing=missing,
        errors=errors,
        repaired=False,
        warnings=warnings,
    )
