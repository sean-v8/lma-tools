#!/usr/bin/env python3
"""Review, create and resume Live Music Archive uploads without altering show
files.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import tomllib
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from internetarchive import ArchiveSession
from internetarchive.item import Item
from requests import HTTPError, RequestException

from lma_tools.config import config_path, read_section

from .collections import load_csv, save_collection, validate
from .core import (
    REQUIRED,
    audio_runtime,
    check_response,
    discover,
    parse_notes,
    show_identity,
    validate_fingerprints,
)
from .parallel import run_parallel, upload_workers
from .progress import (
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


def transport_metadata(metadata):
    """Encode reviewed plain text for Archive's HTML description field."""
    payload = dict(metadata)
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
    config = read_section(path, "uploader")
    unknown = set(config) - {
        "shows_root",
        "log",
        "collections",
        "recovery",
        "upload_workers",
    }
    if unknown:
        raise ValueError(f"Unknown uploader config keys: {sorted(unknown)}")
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
    validate(config.get("collections", {}))
    load_policy(config.get("recovery", {}))
    upload_workers(config.get("upload_workers", 3))
    return config


def is_output(path):
    if not path.is_dir():
        return False
    try:
        show_identity(path.name)
        suffixes = {p.suffix.lower() for p in path.iterdir() if p.is_file()}
        return {".txt", ".flac"} <= suffixes
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
    from .ui import edit_document

    note = "Saved review retained; history suggestions not reapplied."
    if history:
        print("Checking recent collection naming...", flush=True)
        metadata, note = suggest(identifier, metadata)
    document = "# " + note + "\n" + review_text(identifier, metadata)
    return edit_document(document, parse_review)


class Journal:
    """Append-only JSON lines, readable and reusable without a separate state
    database.
    """

    def __init__(self, path, identifier, enabled=True):
        self.path, self.identifier, self.enabled = path, identifier, enabled
        self._lock = RLock()

    def events(self):
        with self._lock:
            return self._events()

    def _events(self):
        if not self.enabled or not self.path.exists():
            return []
        events = []
        with self.path.open(encoding="utf-8") as handle:
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
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

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


def ensure_complete_runtime(item, flacs):
    local = {p.name for p in flacs}
    remote = {
        name
        for name, entry in remote_inventory(item).items()
        if name.lower().endswith(".flac")
        and entry.get("source", "original") == "original"
    }
    missing = remote - local
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
):
    policy = recovery if recovery is not None else RecoveryPolicy()
    workers = upload_workers(workers)
    selected = list(files)
    remote = remote_inventory(item) if item.exists else {}
    total_size = sum(p.stat().st_size for p in selected)

    def transfer(
        session,
        item,
        path,
        index,
        metadata,
        verbose=True,
        notify=print,
        cancel=None,
        on_state=None,
        quiet=False,
    ):
        check_cancelled(cancel)
        if not quiet:
            notify(
                f"[{index}/{len(selected)}] Checking {path.name}", flush=True
            )
        digest, size, stamp = file_md5(path)
        entry = remote.get(path.name)
        reason = None
        if (
            not force
            and entry is not None
            and remote_matches(entry, digest, size)
        ):
            reason = "remote MD5 and size match"
        elif not force and item.exists and entry is None:
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
            and not force
            and not remote_matches(entry, digest, size)
        ):
            if entry.get("md5"):
                raise ValueError(
                    f"Remote file differs: {path.name}. "
                    "Inspect it; --force explicitly allows replacement."
                )
        journal.append("upload_started", name=path.name, md5=digest, size=size)
        try:

            def send():
                return item.upload_file(
                    str(path),
                    key=path.name,
                    metadata=transport_metadata(metadata),
                    checksum=False,
                    verify=True,
                    queue_derive=path.suffix.lower() == ".flac",
                    retries=retries,
                    retries_sleep=retry_sleep,
                    headers={"x-archive-size-hint": str(total_size)},
                    request_kwargs={"timeout": (300, 300)},
                    verbose=verbose,
                )

            with watch_transfer(session, path, tracker):
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
                    force,
                    notify=notify if not verbose else None,
                    cancel=cancel,
                    on_state=on_state,
                )
            if tracker is not None:
                tracker.complete(path.name)
            journal.append(
                "upload_ok", name=path.name, md5=digest, size=size, **outcome
            )
        except BaseException as exc:
            journal.append(
                "upload_failed",
                name=path.name,
                md5=digest,
                size=size,
                error=type(exc).__name__,
            )
            raise
        return "complete"

    if workers == 1 or len(selected) <= 1:
        for index, path in enumerate(selected, 1):
            transfer(session, item, path, index, metadata)
        return
    entries = list(enumerate(selected, 1))
    snapshot = deepcopy(item.item_metadata)
    if not item.exists:
        if selected[0].suffix.lower() != ".txt":
            raise ValueError("A new item must start with its notes TXT")
        transfer(session, item, selected[0], 1, metadata)
        entries = entries[1:]
        snapshot = {"metadata": {**metadata, "identifier": item.identifier}}
    workers = min(workers, len(entries))

    def concurrent(index, path, slot, board, cancel):
        row = board.row(path, index)
        try:
            with TransferSession(
                row, cancel, config=deepcopy(session.config)
            ) as active:
                active.cookies.update(session.cookies)
                worker_item = Item(active, item.identifier, deepcopy(snapshot))
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
                )
                row.finish(state)
        except Exception as exc:
            row.message(f"Failed: {type(exc).__name__}")
            raise
        finally:
            row.close()

    run_parallel(entries, workers, concurrent, tracker=tracker)


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
        "-u",
        "--upload",
        action="store_true",
        help="FLACs only; never calculate or modify runtime",
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
    p.add_argument("--config", type=Path, default=None)
    p.add_argument(
        "--collections",
        type=Path,
        default=None,
        help="legacy CSV override; learned mappings save to config.toml",
    )
    p.add_argument("--collection")
    p.add_argument("--identifier")
    p.add_argument("--notes", type=Path)
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
        help="ignore metadata from prior runs in project log",
    )
    p.add_argument(
        "--reset-review",
        action="store_true",
        help="alias for --fresh-metadata",
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
    p.add_argument("--retries", type=int, default=10)
    p.add_argument("--retry-sleep", type=int, default=60)
    return p


def run_upload(args, directory, project, config):
    identifier = args.identifier or directory.name
    abbreviation, _ = show_identity(identifier)
    notes, small, flacs = discover(
        directory, args.notes.resolve() if args.notes else None
    )
    validate_fingerprints(directory, flacs, allow_missing=args.upload)
    # -u deliberately never computes a partial runtime, even if tracks were
    # moved out.
    runtime = None if args.upload else audio_runtime(flacs)
    journal = Journal(
        project / f"{identifier}_lma.log",
        identifier,
        enabled=config.get("log", True) and not args.no_log,
    )
    metadata = {}
    if args.runtime:
        metadata = {"runtime": runtime}
    elif not args.upload:
        metadata = parse_notes(
            notes.read_text(encoding="utf-8-sig"), identifier
        )
        previous = (
            None
            if args.fresh_metadata or args.reset_review
            else journal.reviewed_metadata()
        )
        if previous:
            previous = dict(previous)
            legacy_name = previous.pop("uploader", None)
            if legacy_name and "@" not in legacy_name:
                previous.setdefault("transferer", legacy_name)
            metadata.update(previous)
            metadata.pop("setlist", None)
        collection = (
            args.collection
            or metadata.get("collection")
            or config.get("collections", {}).get(abbreviation)
        )
        if not collection and not args.preview:
            collection = input(
                f"Collection identifier for {abbreviation}: "
            ).strip()
        metadata.update(collection=collection or "", runtime=runtime)
        if not args.preview:
            identifier, metadata = edit_review(
                identifier, metadata, history=not bool(previous)
            )
            metadata["runtime"] = runtime
            journal = Journal(
                project / f"{identifier}_lma.log", identifier, journal.enabled
            )
    files = (
        []
        if (args.runtime or args.metadata)
        else flacs
        if args.upload
        else small + flacs
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
        print("Cancelled; no log or Archive.org changes made.")
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
        if (args.upload or args.runtime or args.metadata) and not item.exists:
            raise ValueError(
                "Item does not exist; run without -u/-r/-m to create it."
            )
        existed = bool(item.exists)
        if existed and not args.upload:
            ensure_complete_runtime(item, flacs)
        if existed and not (args.upload or args.runtime):
            fields = ["date", "collection", "mediatype"]
            if not args.metadata:
                fields += ["creator", "title"]
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
        if not (args.upload or args.runtime):
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
            )
        if args.runtime:
            check_response(
                item.modify_metadata({"runtime": runtime}, timeout=60)
            )
            journal.append("runtime_ok", runtime=runtime)
        else:
            if existed and not args.upload:
                protected = {
                    "identifier",
                    "creator",
                    "title",
                    "mediatype",
                    "collection",
                    "uploader",
                }
                if args.metadata:
                    protected -= {"creator", "title"}
                payload = {
                    key: value
                    for key, value in metadata.items()
                    if key not in protected
                }
                print("Updating metadata before file transfers...", flush=True)
                options = {"timeout": 60}
                if args.metadata:
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
                    workers=upload_workers(
                        args.workers
                        if args.workers is not None
                        else config.get("upload_workers", 3)
                    ),
                )
                if tracker is not None:
                    tracker.status("deriving")
                check_response(
                    session.submit_task(
                        identifier,
                        "derive",
                        comment="lma.py: selected uploads complete",
                        request_kwargs={"timeout": (30, 60)},
                    )
                )
                journal.append("derive_queued")
        journal.append("run_ok")
        if tracker is not None:
            tracker.close("done")
    except BaseException as exc:
        if tracker is not None:
            tracker.close(
                "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
            )
        data = {"error": type(exc).__name__}
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
    if args.collections is not None:
        config["collections"] = {
            **config.get("collections", {}),
            **load_csv(args.collections),
        }
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
    directory, project = resolve_show(args.show, root)
    if args.session_name:
        print(tmux_session_name(args.identifier or directory.name))
        return 0
    if directory == project:
        raise ValueError("Project log must be outside the output folder.")
    with show_lock(directory):
        return run_upload(args, directory, project, config)


def cli(argv=None, runner=main):
    try:
        return runner(argv)
    except KeyboardInterrupt:
        print(
            "\nCancelled/interrupted. No show files were changed.",
            file=sys.stderr,
        )
        return 130
    except (ValueError, KeyError, TypeError, OSError, RequestException) as exc:
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
