#!/usr/bin/env python3
"""Prepare, review, create and resume Live Music Archive uploads."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from threading import RLock

from internetarchive import ArchiveSession
from internetarchive.item import Item
from mutagen import MutagenError
from requests import HTTPError, RequestException

from lma_tools.config import config_path, read_section

from .collections import (
    get_preference,
    save_collection,
    validate,
)
from .core import (
    REQUIRED,
    audio_runtime,
    check_response,
    discover,
    parse_notes,
    show_identity,
    validate_fingerprints,
)
from .derivation import queue_final_derive
from .multipart import (
    MultipartOptions,
    MultipartUpload,
    load_options,
)
from .parallel import run_parallel, upload_workers
from .progress import (
    ProgressBoard,
    TransferSession,
    check_cancelled,
    watch_transfer,
)
from .recovery import (
    RecoveryPolicy,
    head_state,
    load_policy,
    upload_with_recovery,
)
from .remote import (
    delete_originals,
    review_remote,
    unfinished_cleanup,
    wait_for_deletion,
)


def transport_metadata(metadata):
    """Encode reviewed plain text for Archive's HTML description field."""
    payload = dict(metadata)
    collection = payload.get("collection")
    if payload.get("mediatype") == "etree" and isinstance(collection, str):
        payload["collection"] = list(dict.fromkeys([collection, "etree"]))
    if "description" in payload:
        payload["description"] = html.escape(
            payload["description"], quote=False
        )
    return payload


def load_config(path):
    if not path.is_file():
        raise ValueError(
            f"Create {path} from config.example.toml "
            "and set shows_root for this machine."
        )
    config = read_section(path, "upload")
    unknown = set(config) - {
        "shows_root",
        "log",
        "prefix",
        "recovery",
        "upload_workers",
        "prepare_workers",
        "resume",
    }
    if unknown:
        raise ValueError(f"Unknown upload config keys: {sorted(unknown)}")
    root = config.get("shows_root")
    if not isinstance(root, str) or not root:
        raise ValueError('config.toml needs shows_root = "/absolute/path".')
    root = Path(os.path.expandvars(root)).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(
            f"shows_root is not an existing absolute directory: {root}"
        )
    config["shows_root"] = root.resolve()
    if not isinstance(config.get("log", True), bool):
        raise ValueError("config log must be true or false.")
    validate(config.get("prefix", {}))
    load_policy(config.get("recovery", {}))
    upload_workers(config.get("upload_workers", 3))
    from .prepare import preparation_workers

    preparation_workers(config.get("prepare_workers"))
    load_options(config.get("resume", {}))
    return config


def is_output(path):
    if not path.is_dir():
        return False
    try:
        show_identity(path.name)
        suffixes = {
            p.suffix.lower()
            for p in path.iterdir()
            if p.is_file() and p.name.casefold() != "sync.txt"
        }
        return ".txt" in suffixes and bool({".wav", ".flac"} & suffixes)
    except (OSError, ValueError):
        return False


def scan_outputs(root):
    """Only project roots and their direct children; never recurse into raw
    audio trees.
    """
    results = []
    for project in sorted(root.iterdir()):
        if not project.is_dir() or project.name.startswith("."):
            continue
        try:
            project.resolve().relative_to(root.resolve())
            candidates = [project] + sorted(
                p
                for p in project.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
            for path in candidates:
                path.resolve().relative_to(root.resolve())
                if is_output(path):
                    results.append((project, path))
        except (OSError, ValueError):
            continue
    return results


def output_tokens(outputs, root):
    counts = {}
    for _, output in outputs:
        counts[output.name] = counts.get(output.name, 0) + 1
    return sorted(
        output.name
        if counts[output.name] == 1
        else str(output.relative_to(root))
        for _, output in outputs
    )


def resolve_show(target, root):
    # Explicit paths remain useful for ad hoc tests, independently of
    # configured scanning.
    if target and (
        Path(target).is_absolute() or target.startswith(".") or "/" in target
    ):
        path = Path(target).expanduser()
        if not path.is_absolute() and not path.exists():
            path = root / path
        if is_output(path):
            return path.resolve(), path.resolve().parent
        if not path.is_dir():
            raise ValueError(f"Show/project directory not found: {target}")
        found = [p for p in path.iterdir() if is_output(p)]
        if len(found) == 1:
            return found[0].resolve(), path.resolve()
        raise ValueError(
            f"Expected one output folder in {path}; "
            f"found {len(found)}. Select the output explicitly."
        )
    if target is None:
        cwd = Path.cwd()
        if is_output(cwd):
            return cwd.resolve(), cwd.resolve().parent
        raise ValueError(
            "Supply a show identifier. Use lma --list or lma <Tab>."
        )
    outputs = scan_outputs(root)
    found = [
        (project, path) for project, path in outputs if path.name == target
    ]
    if not found:
        found = [
            (project, path)
            for project, path in outputs
            if project.name == target
        ]
    if len(found) == 1:
        project, path = found[0]
        # A show stored directly at the projects root logs outside the output
        # folder.
        return path.resolve(), (root if path == project else project).resolve()
    if len(found) > 1:
        choices = ", ".join(str(path.relative_to(root)) for _, path in found)
        raise ValueError(
            f"Ambiguous show/project {target}: {choices}. "
            "Use one of those relative paths."
        )
    raise ValueError(f"No output folder found for {target}. Use lma --list.")


def resolve_cleanup(target, root):
    """Cleanup needs a named local directory, not upload-ready audio."""
    if target is None:
        path = Path.cwd()
    else:
        path = Path(target).expanduser()
        if not path.is_dir():
            path = root / target
        if not path.is_dir():
            matches = [
                child
                for parent in root.iterdir()
                if parent.is_dir()
                for child in parent.iterdir()
                if child.is_dir() and child.name == target
            ]
            if len(matches) != 1:
                raise ValueError("Select an unambiguous cleanup directory")
            path = matches[0]
    show_identity(path.name)
    return path.resolve(), path.resolve().parent


def review_text(identifier, metadata):
    lines = [
        "# Ctrl-S accepts; Ctrl-C cancels. No review file is written.",
        "# Identifier prefix and collection are independent.",
        f"identifier = {json.dumps(identifier)}",
        "",
        "[metadata]",
    ]
    for key, value in metadata.items():
        if "\n" in value and "'''" not in value and "\r" not in value:
            value = "'''\n" + value + "'''"
        else:
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def parse_review(text):
    document = tomllib.loads(text)
    identifier, metadata = document["identifier"], document["metadata"]
    show_identity(identifier)
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a TOML table.")
    for key in ("mediatype", *REQUIRED):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"Missing/empty text field: {key}")
    if any(
        not re.fullmatch(r"[a-z][a-z0-9_]*", k) or not isinstance(v, str)
        for k, v in metadata.items()
    ):
        raise ValueError(
            "Metadata keys must be lowercase names, values must be strings."
        )
    if "uploader" in metadata:
        raise ValueError(
            "uploader is the account identity; use transferer for the "
            "display name."
        )
    if "setlist" in metadata:
        raise ValueError("A separate setlist field is not supported.")
    if metadata["mediatype"] != "etree":
        raise ValueError("Live Music Archive mediatype must remain etree.")
    if metadata["date"] != show_identity(identifier)[1]:
        raise ValueError("Identifier date and metadata date disagree.")
    metadata["description"] = metadata["description"].rstrip("\r\n")
    return identifier, metadata


def edit_review(identifier, metadata, *, history=True):
    from .history import suggest
    from .metadata_review import comparison, snapshot
    from .ui import edit_document

    remote = snapshot(identifier)
    note = "Fresh TXT defaults; saved collection naming retained."
    if history and remote == {}:
        print("Checking recent collection naming...", flush=True)
        metadata, note = suggest(identifier, metadata)
    document = (
        "# "
        + note
        + "\n"
        + comparison(remote, metadata)
        + review_text(identifier, metadata)
    )
    result = edit_document(document, parse_review)
    if result[0] != identifier:
        remote = snapshot(result[0])
    print(comparison(remote, result[1]), end="")
    return result


class Journal:
    """Append-only JSON lines, readable and reusable without a separate state
    database.
    """

    def __init__(self, path, identifier, enabled=True):
        self.path, self.identifier, self.enabled = path, identifier, enabled
        self._lock = RLock()
        self._resume_cache = None
        self._resume_stamp = None

    def _resume_file_stamp(self):
        try:
            stat = self.path.stat()
            return stat.st_size, stat.st_mtime_ns
        except FileNotFoundError:
            return None

    def _load_resume(self):
        stamp = self._resume_file_stamp()
        if self._resume_cache is not None and stamp == self._resume_stamp:
            return
        self._resume_cache = {}
        for event in self._events():
            name = event.get("name")
            if not isinstance(name, str):
                continue
            kind = event.get("event")
            if kind == "resume_start":
                self._resume_cache[name] = event.get("state")
                continue
            state = self._resume_cache.get(name)
            if not isinstance(state, dict) or (
                event.get("upload_id") != state.get("upload_id")
            ):
                continue
            if kind == "resume_part" and isinstance(state.get("parts"), dict):
                state["parts"][str(event.get("number"))] = event.get("md5")
            elif kind == "resume_phase":
                state["phase"] = event.get("phase")
            elif kind == "resume_clear":
                self._resume_cache.pop(name, None)
        self._resume_stamp = stamp

    def resume_state(self, name):
        with self._lock:
            self._load_resume()
            return deepcopy(self._resume_cache.get(name))

    def save_resume(self, name, state):
        if not self.enabled:
            raise ValueError("File resume requires upload logging.")
        with self._lock:
            self._load_resume()
            previous = self._resume_cache.get(name)
            restart = not isinstance(previous, dict) or any(
                previous.get(key) != state.get(key)
                for key in ("upload_id", "signature", "part_bytes", "version")
            )
            if not restart:
                restart = not set(previous["parts"]) <= set(state["parts"])
            if restart:
                self._append("resume_start", name=name, state=state)
            else:
                for number, digest in state["parts"].items():
                    if previous["parts"].get(number) != digest:
                        self._append(
                            "resume_part",
                            name=name,
                            upload_id=state["upload_id"],
                            number=int(number),
                            md5=digest,
                        )
                if previous["phase"] != state["phase"]:
                    self._append(
                        "resume_phase",
                        name=name,
                        upload_id=state["upload_id"],
                        phase=state["phase"],
                    )
            self._resume_cache[name] = deepcopy(state)
            self._resume_stamp = self._resume_file_stamp()

    def clear_resume(self, name, upload_id):
        with self._lock:
            self._load_resume()
            state = self._resume_cache.get(name)
            if isinstance(state, dict) and state.get("upload_id") == upload_id:
                self._append("resume_clear", name=name, upload_id=upload_id)
                self._resume_cache.pop(name, None)
                self._resume_stamp = self._resume_file_stamp()

    def events(self):
        with self._lock:
            return self._events()

    def _events(self):
        if not self.enabled or not self.path.exists():
            return []
        events = []
        with self.path.open("rb") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if (
                        isinstance(record, dict)
                        and record.get("identifier") == self.identifier
                    ):
                        events.append(record)
                except (ValueError, UnicodeError):
                    # A torn final line after a crash is not a successful
                    # upload.
                    continue
        return events

    def append(self, event, **data):
        with self._lock:
            self._append(event, **data)

    def _append(self, event, **data):
        if not self.enabled:
            return
        record = dict(
            time=datetime.now(timezone.utc).isoformat(),
            identifier=self.identifier,
            event=event,
            **data,
        )
        # Prefix newline means even a previous torn write cannot consume this
        # event.
        existed = self.path.exists()
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("\n" + json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def acknowledgement(self, name, digest, size):
        for record in reversed(self.events()):
            if record.get("name") == name and record.get("event") in (
                "upload_ok",
                "upload_failed",
                "upload_started",
            ):
                return (
                    record.get("event") == "upload_ok"
                    and record.get("md5") == digest
                    and record.get("size") == size
                )
        return False

    def reviewed_metadata(self):
        for record in reversed(self.events()):
            if (
                record.get("event") == "run_start"
                and record.get("mode") in ("full", "metadata-only")
                and isinstance(record.get("metadata"), dict)
            ):
                return record["metadata"]
        return None


@contextmanager
def show_lock(directory):
    # Advisory lock on the existing directory inode; no lock file is created.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another lma process has this output directory locked."
            ) from None
        yield
    finally:
        os.close(fd)


def file_md5(path):
    before = path.stat()
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "md5").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"File changed while hashing: {path.name}")
    return digest, after.st_size, after.st_mtime_ns


def remote_inventory(item):
    # Read the item metadata response already fetched directly, not the search
    # index.
    return {
        entry["name"]: entry
        for entry in item.files
        if isinstance(entry, dict) and "name" in entry
    }


def ensure_complete_runtime(item, flacs, ignored=()):
    local = {p.name for p in flacs}
    remote = {
        name
        for name, entry in remote_inventory(item).items()
        if name.lower().endswith(".flac")
        and entry.get("source", "original") == "original"
    }
    missing = remote - local - set(ignored)
    if missing:
        raise ValueError(
            "Runtime requires the complete show; remote FLACs missing "
            "locally: "
            + ", ".join(sorted(missing))
            + ". Restore them, or use -u to leave runtime unchanged."
        )


def remote_matches(entry, digest, size):
    try:
        return (
            str(entry.get("md5", "")).lower() == digest
            and int(entry.get("size", -1)) == size
        )
    except (TypeError, ValueError):
        return False


def corroborate_log(session, identifier, name, digest, size):
    # Direct file checks can confirm a lost reply, even without a success log.
    state, _ = head_state(session, identifier, name, digest, size)
    return state == "match"


def upload_files(
    session,
    item,
    files,
    metadata,
    journal,
    retries,
    retry_sleep,
    trust_log=False,
    force=False,
    recovery=None,
    workers=1,
    tracker=None,
    multipart=None,
    replacements=None,
    defer_derive=False,
):
    options = multipart if multipart is not None else MultipartOptions()
    policy = recovery if recovery is not None else RecoveryPolicy()
    workers = upload_workers(workers)
    selected = list(files)
    replacements = replacements or {}
    remote = remote_inventory(item) if item.exists else {}
    total_size = sum(p.stat().st_size for p in selected)
    board = ProgressBoard(files=selected, tracker=tracker)
    assemblies = []
    assembly_lock = RLock()

    def transfer(
        session,
        item,
        path,
        index,
        metadata,
        verbose=False,
        notify=None,
        cancel=None,
        on_state=None,
        quiet=False,
        progress=None,
    ):
        check_cancelled(cancel)
        if notify is None:
            notify = board.message if board.enabled else print
        if not quiet:
            notify(
                f"[{index}/{len(selected)}] Checking {path.name}", flush=True
            )
        digest, size, stamp = file_md5(path)
        replace = force or path.name in replacements
        if isinstance(replacements, dict) and path.name in replacements:
            if replacements[path.name] != (digest, size):
                raise ValueError(
                    f"Local file changed after replacement review: {path.name}"
                )
        entry = remote.get(path.name)
        reason = None
        if (
            not replace
            and entry is not None
            and remote_matches(entry, digest, size)
        ):
            reason = "remote MD5 and size match"
        elif not replace and item.exists and entry is None:
            if corroborate_log(
                session, item.identifier, path.name, digest, size
            ):
                reason = "remote HEAD confirms MD5 and size"
            elif trust_log and journal.acknowledgement(
                path.name, digest, size
            ):
                reason = (
                    "explicit --trust-log: matching successful server "
                    "acknowledgement"
                )
        if reason:
            state = journal.resume_state(path.name)
            if (
                isinstance(state, dict)
                and not reason.startswith("explicit --trust-log")
                and state.get("signature")
                == {
                    "identifier": item.identifier,
                    "name": path.name,
                    "md5": digest,
                    "size": size,
                }
            ):
                journal.clear_resume(path.name, state["upload_id"])
            board.complete(path.name, "skipped")
            if tracker is not None:
                tracker.complete(path.name)
            if not quiet:
                notify(f"  Skipped: {reason}")
            journal.append(
                "skip", name=path.name, md5=digest, size=size, reason=reason
            )
            return "skipped"
        if (
            entry is not None
            and not replace
            and not remote_matches(entry, digest, size)
        ):
            if entry.get("md5"):
                raise ValueError(
                    f"Remote file differs: {path.name}. "
                    "Inspect it; --force explicitly allows replacement."
                )
        journal.append("upload_started", name=path.name, md5=digest, size=size)
        own_row = progress is None
        if own_row:
            progress = board.row(path, index)
        try:

            def send():
                return item.upload_file(
                    str(path),
                    key=path.name,
                    metadata=transport_metadata(metadata),
                    checksum=False,
                    verify=True,
                    queue_derive=(
                        not defer_derive and path.suffix.lower() == ".flac"
                    ),
                    retries=retries,
                    retries_sleep=retry_sleep,
                    headers={"x-archive-size-hint": str(total_size)},
                    request_kwargs={"timeout": (300, 300)},
                    verbose=False,
                )

            if (
                options.enabled
                and path.suffix.lower() == ".flac"
                and (
                    size > options.part_bytes
                    or journal.resume_state(path.name) is not None
                )
            ):
                upload = MultipartUpload(
                    session,
                    item.identifier,
                    path,
                    digest,
                    size,
                    stamp,
                    journal,
                    options,
                    policy,
                    progress,
                    cancel,
                    queue_derive=not defer_derive,
                )
                outcome = upload.run(defer_assembly=True)
                if outcome is None:
                    with assembly_lock:
                        assemblies.append((index, upload))
                    journal.append(
                        "assembly_pending",
                        name=path.name,
                        md5=digest,
                        size=size,
                    )
                    return "assembling"
            else:
                with watch_transfer(session, path, tracker, progress, cancel):
                    outcome = upload_with_recovery(
                        send,
                        session,
                        item.identifier,
                        path,
                        digest,
                        size,
                        stamp,
                        journal,
                        policy,
                        replace,
                        notify=notify if not verbose else None,
                        cancel=cancel,
                        on_state=on_state,
                    )
            board.complete(path.name)
            if tracker is not None:
                tracker.complete(path.name)
            journal.append(
                "upload_ok", name=path.name, md5=digest, size=size, **outcome
            )
        except BaseException as exc:
            progress.finish("failed")
            journal.append(
                "upload_failed",
                name=path.name,
                md5=digest,
                size=size,
                error=type(exc).__name__,
            )
            raise
        finally:
            if own_row:
                progress.close()
        return "complete"

    def sequential(index, path, metadata):
        row = board.row(path, index)
        try:
            state = transfer(
                session,
                item,
                path,
                index,
                metadata,
                quiet=board.enabled,
                progress=row,
                on_state=row.status,
            )
            if board.enabled:
                row.finish(state)
        except BaseException as exc:
            row.finish(
                "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
            )
            raise
        finally:
            row.close()

    def verify_assembly(index, upload, slot, board, cancel):
        row = board.row(upload.path, index)
        try:
            with TransferSession(
                row, cancel, config=deepcopy(session.config)
            ) as active:
                active.cookies.update(session.cookies)
                upload.session, upload.cancel, upload.progress = (
                    active,
                    cancel,
                    row,
                )
                outcome = upload.verify()
                board.complete(upload.path.name)
                if tracker is not None:
                    tracker.complete(upload.path.name)
                journal.append(
                    "upload_ok",
                    name=upload.path.name,
                    md5=upload.digest,
                    size=upload.size,
                    **outcome,
                )
                if not board.enabled:
                    row.finish("complete")
        except BaseException as exc:
            row.finish("failed")
            journal.append(
                "upload_failed",
                name=upload.path.name,
                md5=upload.digest,
                size=upload.size,
                error=type(exc).__name__,
            )
            raise
        finally:
            row.close()

    try:
        if workers == 1 or len(selected) <= 1:
            for index, path in enumerate(selected, 1):
                sequential(index, path, metadata)
        else:
            entries = list(enumerate(selected, 1))
            snapshot = deepcopy(item.item_metadata)
            if not item.exists:
                if selected[0].suffix.lower() != ".txt":
                    raise ValueError(
                        "A new item must start with its notes TXT"
                    )
                sequential(1, selected[0], metadata)
                entries = entries[1:]
                snapshot = {
                    "metadata": {**metadata, "identifier": item.identifier}
                }
            active_workers = min(workers, len(entries))

            def concurrent(index, path, slot, board, cancel):
                row = board.row(path, index)
                try:
                    with TransferSession(
                        row, cancel, config=deepcopy(session.config)
                    ) as active:
                        active.cookies.update(session.cookies)
                        worker_item = Item(
                            active, item.identifier, deepcopy(snapshot)
                        )
                        state = transfer(
                            active,
                            worker_item,
                            path,
                            index,
                            {},
                            verbose=False,
                            notify=row.message,
                            cancel=cancel,
                            on_state=row.status,
                            quiet=board.enabled,
                            progress=row,
                        )
                        row.finish(state)
                except Exception as exc:
                    detail = " ".join(str(exc).splitlines())[:300]
                    row.finish("failed")
                    row.message(f"Failed: {type(exc).__name__}: {detail}")
                    raise
                finally:
                    row.close()

            run_parallel(
                entries,
                active_workers,
                concurrent,
                board=board,
                tracker=tracker,
            )
        if assemblies:
            run_parallel(
                sorted(assemblies, key=lambda entry: entry[0]),
                min(workers, len(assemblies)),
                verify_assembly,
                board=board,
                tracker=tracker,
            )

    finally:
        board.close()


def tmux_session_name(identifier):
    show_identity(identifier)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
    return f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "show",
        nargs="?",
        help="output identifier, project name or explicit directory",
    )
    modes = p.add_mutually_exclusive_group()
    modes.add_argument(
        "-p",
        "--prepare",
        action="store_true",
        help="prepare local audio, tags and manifests; no upload",
    )
    modes.add_argument(
        "--clean",
        action="store_true",
        help="review obsolete remote originals; no preparation or upload",
    )
    p.add_argument(
        "--no-clean",
        action="store_true",
        help="omit remote cleanup review during a normal upload",
    )
    modes.add_argument(
        "-u",
        "--upload",
        action="store_true",
        help="upload all supported files; no item metadata or runtime changes",
    )
    modes.add_argument(
        "-r",
        "--runtime",
        action="store_true",
        help="update runtime only; requires complete FLAC inventory",
    )
    modes.add_argument(
        "-m",
        "--metadata",
        action="store_true",
        help=(
            "update all editable metadata, including runtime; no file uploads"
        ),
    )
    p.add_argument(
        "--session-name", action="store_true", help=argparse.SUPPRESS
    )
    p.add_argument(
        "--config", type=Path, help="use a different private config file"
    )
    p.add_argument(
        "--collection", help="override the configured artist collection"
    )
    p.add_argument(
        "--identifier", help="override the destination Archive identifier"
    )
    p.add_argument(
        "--notes", type=Path, help="select a specific TXT file in the show"
    )
    p.add_argument(
        "--preview",
        action="store_true",
        help="print plan without editor, logs or network",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="list discovered output identifiers",
    )
    p.add_argument("--complete", metavar="PREFIX", help=argparse.SUPPRESS)
    p.add_argument(
        "--fresh-metadata",
        action="store_true",
        help="drop saved extra metadata; TXT fields always refresh",
    )
    p.add_argument(
        "--trust-log",
        action="store_true",
        help="trust matching logged HTTP success if remote entry is absent",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="upload all selected files, allowing remote replacement",
    )
    p.add_argument(
        "--no-log",
        action="store_true",
        help="disable project logging and log-based recovery",
    )
    p.add_argument(
        "-w",
        "--workers",
        type=int,
        help="simultaneous uploads; config default 3",
    )
    p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="use ordinary whole-file PUTs",
    )
    p.set_defaults(resume=None)
    p.add_argument(
        "--retries",
        type=int,
        default=10,
        help="upload-library 503 retries (default: 10)",
    )
    p.add_argument(
        "--retry-sleep",
        type=int,
        default=60,
        help="seconds between upload-library 503 retries (default: 60)",
    )
    return p


def run_upload(args, directory, project, config):
    identifier = args.identifier or directory.name
    abbreviation, _ = show_identity(identifier)
    if args.clean:
        notes = None
        from .core import SMALL

        local = sorted(p for p in directory.iterdir() if p.is_file())
        small = [p for p in local if p.suffix.lower() in SMALL]
        flacs = [p for p in local if p.suffix.lower() == ".flac"]
    else:
        notes, small, flacs = discover(
            directory, args.notes.resolve() if args.notes else None
        )
    if args.upload:
        from .prepare import validate_manifests

        report = validate_manifests(directory)
        for warning in report.get("warnings", []):
            print("Manifest warning: " + warning)
        if not report["valid"]:
            print("Local manifests need attention:")
            for error in report["errors"]:
                print("  " + error)
            if args.preview:
                raise ValueError("Stale manifests; preview made no changes")
            if input("Type REBUILD to regenerate FFP/MD5 only: ") != "REBUILD":
                raise ValueError("Manifest repair cancelled")
            validate_manifests(directory, repair=True)
            notes, small, flacs = discover(directory, notes)
    if not args.clean:
        validate_fingerprints(directory, flacs, allow_missing=args.upload)
    # -u deliberately never computes a partial runtime, even if tracks were
    # moved out.
    runtime = None if args.upload or args.clean else audio_runtime(flacs)
    journal = Journal(
        project / f"{identifier}_lma.log",
        identifier,
        enabled=config.get("log", True) and not args.no_log,
    )
    metadata = {}
    if args.runtime:
        metadata = {"runtime": runtime}
    elif not (args.upload or args.clean):
        metadata = parse_notes(
            notes.read_text(encoding="utf-8-sig"), identifier
        )
        previous = None if args.fresh_metadata else journal.reviewed_metadata()
        if previous:
            previous = dict(previous)
            previous.pop("uploader", None)
            # TXT-derived fields are authoritative on every ordinary run.
            # Retain only extra fields that have no local notes equivalent.
            metadata.update(
                {
                    key: value
                    for key, value in previous.items()
                    if key not in metadata
                    and key
                    not in {"setlist", "runtime", "scanner", "collection"}
                }
            )
        collection = (
            args.collection
            or get_preference(config, abbreviation).get("collection")
            or (previous or {}).get("collection")
        )
        if not collection and not args.preview:
            collection = input(
                f"Collection identifier for {abbreviation}: "
            ).strip()
        saved = get_preference(config, abbreviation)
        if saved.get("creator"):
            metadata["creator"] = saved["creator"]
            metadata["title"] = (
                f"{saved['creator']} Live at {metadata['venue']} "
                f"on {metadata['date']}"
            )
        metadata.update(collection=collection or "", runtime=runtime)
        metadata["scanner"] = f"lma-tools v{version('lma-tools')}"
        if not args.preview:
            identifier, metadata = edit_review(
                identifier,
                metadata,
                history=not bool(saved.get("creator")),
            )
            metadata["runtime"] = runtime
            journal = Journal(
                project / f"{identifier}_lma.log", identifier, journal.enabled
            )
    files = (
        [] if (args.runtime or args.metadata or args.clean) else small + flacs
    )
    print(f"Output folder: {directory}\nProject root: {project}")
    print(f"Destination: https://archive.org/details/{identifier}")
    print(review_text(identifier, metadata))
    print(
        f"{len(files)} selected files; runtime: {runtime or 'unchanged (-u)'}"
    )
    print("Log: " + (str(journal.path) if journal.enabled else "disabled"))
    if args.preview:
        print("Preview only: no files written, no network requests.")
        return 0
    if (
        input("Type UPLOAD to proceed (anything else cancels): ").strip()
        != "UPLOAD"
    ):
        print("Cancelled; no Archive.org changes made. Preparation retained.")
        return 0
    journal.append(
        "run_start",
        mode="upload-only"
        if args.upload
        else "runtime-only"
        if args.runtime
        else "metadata-only"
        if args.metadata
        else "full",
        directory=str(directory),
        metadata=metadata,
        files=[p.name for p in files],
        trust_log=args.trust_log,
        force=args.force,
    )
    tracker = None
    files_verified = False
    if files and os.environ.get("LMA_UPLOAD_SESSION"):
        from .dashboard import ShowProgress

        try:
            tracker = ShowProgress(
                identifier, files, os.environ["LMA_UPLOAD_SESSION"]
            )
        except OSError:
            print("Dashboard unavailable; continuing upload.", flush=True)
    try:
        session = ArchiveSession()
        if not session.access_key or not session.secret_key:
            raise ValueError("Run: ia configure")
        item = session.get_item(
            identifier, request_kwargs={"timeout": (30, 60)}
        )
        if (
            args.upload or args.runtime or args.metadata or args.clean
        ) and not item.exists:
            raise ValueError(
                "Item does not exist; run without -u/-r/-m to create it."
            )
        existed = bool(item.exists)
        replacements, deletions = set(), []
        prior_cleanup = []
        if existed and not (args.runtime or args.metadata):
            prior_cleanup = unfinished_cleanup(journal)
            for entry in prior_cleanup:
                wait_for_deletion(
                    session,
                    identifier,
                    entry,
                    journal,
                    load_policy(config.get("recovery", {})),
                )
        if prior_cleanup:
            item = session.get_item(
                identifier, request_kwargs={"timeout": (30, 60)}
            )
        if existed and not (args.runtime or args.metadata):
            replacements, deletions = review_remote(
                item,
                small + flacs,
                journal,
                clean=args.clean or (not args.upload and not args.no_clean),
                force=args.force,
                replacements_only=args.clean,
                session=session,
            )
        journal.append(
            "remote_review",
            replacements=sorted(replacements),
            deletions=[entry["name"] for entry in deletions],
        )
        if existed and not (args.upload or args.clean):
            ensure_complete_runtime(
                item, flacs, ignored={entry["name"] for entry in deletions}
            )
        if existed and not (args.upload or args.runtime or args.clean):
            fields = ["date", "collection", "mediatype"]
            for key in fields:
                remote = item.metadata.get(key)
                if metadata[key] not in (
                    remote if isinstance(remote, list) else [remote]
                ):
                    raise ValueError(
                        f"Existing creation field {key} differs: {remote!r}. "
                        "It is protected by this script; "
                        "inspect the item before proceeding."
                    )
        if not (args.upload or args.runtime or args.clean):
            collection = session.get_item(
                metadata["collection"], request_kwargs={"timeout": (30, 60)}
            )
            if (
                not collection.exists
                or collection.metadata.get("mediatype") != "collection"
            ):
                raise ValueError(
                    "Collection is not an existing Archive.org collection."
                )
            save_collection(
                args.config,
                show_identity(identifier)[0],
                metadata["collection"],
                creator=metadata["creator"],
                genre=config.get("prepared_genre"),
            )
        if args.clean:
            delete_originals(
                session,
                identifier,
                deletions,
                journal,
                policy=load_policy(config.get("recovery", {})),
            )
            if deletions or prior_cleanup:
                queue_final_derive(session, identifier, journal)
        elif args.runtime:
            check_response(
                item.modify_metadata({"runtime": runtime}, timeout=60)
            )
            journal.append("runtime_ok", runtime=runtime)
        else:
            if existed and not args.upload:
                protected = {
                    "identifier",
                    "mediatype",
                    "collection",
                    "uploader",
                }
                payload = {
                    key: value
                    for key, value in metadata.items()
                    if key not in protected
                }
                print("Updating metadata before file transfers...", flush=True)
                options = {"timeout": 60}
                if any(
                    metadata[key] != item.metadata.get(key)
                    for key in ("creator", "title")
                ):
                    options["expect"] = {
                        key: item.metadata[key]
                        for key in ("creator", "title")
                        if key in item.metadata
                        and metadata[key] != item.metadata[key]
                    }
                check_response(
                    item.modify_metadata(
                        transport_metadata(payload), **options
                    )
                )
                journal.append("metadata_ok", metadata=payload)
                print(f"Metadata accepted; runtime {runtime}.", flush=True)
            elif not existed:
                print(
                    f"Creating item: https://archive.org/details/{identifier}",
                    flush=True,
                )
            if not args.metadata:
                upload_files(
                    session,
                    item,
                    files,
                    {} if existed else metadata,
                    journal,
                    args.retries,
                    args.retry_sleep,
                    args.trust_log,
                    args.force,
                    recovery=load_policy(config.get("recovery", {})),
                    tracker=tracker,
                    multipart=config["multipart_options"],
                    replacements=replacements,
                    defer_derive=bool(deletions),
                    workers=upload_workers(
                        args.workers
                        if args.workers is not None
                        else config.get("upload_workers", 3)
                    ),
                )
                files_verified = True
                if deletions:
                    from .recovery import remote_state

                    for path in files:
                        digest, size, _ = file_md5(path)
                        state, _ = remote_state(
                            session, identifier, path.name, digest, size
                        )
                        if state != "match":
                            raise ValueError(
                                "Cleanup postponed: corrected files are "
                                "not all remotely checksum-confirmed."
                            )
                    delete_originals(
                        session,
                        identifier,
                        deletions,
                        journal,
                        policy=load_policy(config.get("recovery", {})),
                    )
                if tracker is not None:
                    tracker.status("deriving")
                queue_final_derive(session, identifier, journal)
        journal.append("run_ok")
        if tracker is not None:
            tracker.close("done")
    except BaseException as exc:
        if tracker is not None:
            tracker.close(
                "cancelled"
                if isinstance(exc, KeyboardInterrupt)
                else "derive failed"
                if files_verified
                else "failed"
            )
        data = {
            "error": type(exc).__name__,
            "message": str(exc)[:2000],
            "files_verified": files_verified,
        }
        if isinstance(exc, HTTPError) and exc.response is not None:
            data.update(
                status=exc.response.status_code,
                server_response=exc.response.text[:12000],
            )
        journal.append("run_failed", **data)
        raise
    print(f"Done: https://archive.org/details/{identifier}")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    if args.retries < 0 or args.retry_sleep < 0:
        raise ValueError("Retry settings cannot be negative.")
    args.config = config_path(args.config)
    config = load_config(args.config)
    if args.workers is not None:
        upload_workers(args.workers)
    multipart = load_options(config.get("resume", {}))
    if args.resume is not None:
        multipart = MultipartOptions(args.resume, multipart.part_size_mib)
    config["multipart_options"] = multipart
    if (
        multipart.enabled
        and (args.no_log or not config.get("log", True))
        and not (
            args.preview
            or args.list
            or args.complete is not None
            or args.session_name
            or args.runtime
            or args.metadata
            or args.prepare
            or args.clean
        )
    ):
        raise ValueError(
            "File resume requires upload logging; enable log and omit "
            "--no-log, or use --no-resume."
        )
    root = config["shows_root"]
    if args.complete is not None or args.list:
        prefix = args.complete or ""
        if prefix.startswith("-"):
            tokens = [
                option
                for action in parser()._actions
                for option in action.option_strings
                if option not in ("--complete", "--session-name")
            ]
        else:
            tokens = output_tokens(scan_outputs(root), root)
        for token in sorted(
            token for token in tokens if token.startswith(prefix)
        ):
            print(token)
        return 0
    if args.clean:
        directory, project = resolve_cleanup(args.show, root)
    else:
        directory, project = resolve_show(args.show, root)
    if args.session_name:
        print(tmux_session_name(args.identifier or directory.name))
        return 0
    if directory == project:
        raise ValueError("Project log must be outside the output folder.")
    with show_lock(directory):
        if args.prepare or not (
            args.upload or args.runtime or args.metadata or args.clean
        ):
            from .prepare import prepare_show

            prefix, _ = show_identity(args.identifier or directory.name)
            prepared = prepare_show(
                directory,
                get_preference(config, prefix),
                preview=args.preview,
                notes=args.notes,
                workers=config.get("prepare_workers"),
                journal=Journal(
                    project / f"{args.identifier or directory.name}_lma.log",
                    args.identifier or directory.name,
                    enabled=config.get("log", True) and not args.no_log,
                ),
            )
            config["prepared_genre"] = prepared.get("genre")
            if not args.preview and "prepared_genre" in config:
                saved = get_preference(config, prefix)
                if saved.get("collection"):
                    save_collection(
                        args.config,
                        prefix,
                        saved["collection"],
                        genre=config["prepared_genre"],
                    )
            if args.prepare or (
                args.preview
                and any(
                    p.suffix.lower() == ".wav" for p in directory.iterdir()
                )
            ):
                if args.preview:
                    print(
                        "Preview only: no files written, no network requests."
                    )
                return 0
        return run_upload(args, directory, project, config)


def cli(argv=None, runner=main):
    try:
        return runner(argv)
    except KeyboardInterrupt:
        print(
            "\nCancelled/interrupted. Any completed preparation is retained.",
            file=sys.stderr,
        )
        return 130
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RequestException,
        subprocess.SubprocessError,
        MutagenError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, HTTPError) and exc.response is not None:
            print(
                "Server response:\n" + exc.response.text[:12000],
                file=sys.stderr,
            )
        print(
            "Stopped. Re-run to inspect remote checksums and resume.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(cli())
