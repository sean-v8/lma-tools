"""Approve remote replacements and optional cleanup before starting workers."""

import hashlib
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote

from internetarchive import iarequest

from .core import SMALL, check_response
from .recovery import RecoveryPolicy, remote_state

SUPPORTED = SMALL | {".flac"}


def digest_file(path):
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def original(entry):
    name = entry.get("name", "")
    return (
        entry.get("source") == "original"
        and isinstance(name, str)
        and name.casefold() != "__ia_thumb.jpg"
        and PurePosixPath(name).name == name
        and PurePosixPath(name).suffix.lower() in SUPPORTED
        and not name.startswith(".")
        and bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(entry.get("md5", ""))))
        and str(entry.get("size", "")).isdigit()
    )


def pending(journal, name):
    state = journal.resume_state(name)
    return isinstance(state, dict) and state.get("phase") in {
        "uploading",
        "completing",
        "assembling",
    }


def review_remote(
    item,
    files,
    journal,
    *,
    clean=False,
    force=False,
    replacements_only=False,
    session=None,
):
    """Return per-name replacement permission and approved original entries."""
    entries = {
        e["name"]: e
        for e in item.files
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    replacements = {}
    all_conflicts = force
    for path in [] if replacements_only else files:
        entry = entries.get(path.name)
        if entry is None and session is not None:
            url = (
                "https://archive.org/download/"
                + quote(item.identifier, safe="")
                + "/"
                + quote(path.name, safe="")
            )
            response = session.head(
                url, allow_redirects=True, timeout=(15, 30)
            )
            try:
                if response.status_code == 200:
                    digest = response.headers.get("ETag", "").strip('"')
                    size = response.headers.get("Content-Length", "")
                    if re.fullmatch(r"[a-fA-F0-9]{32}", digest):
                        entry = dict(name=path.name, md5=digest, size=size)
                elif response.status_code != 404:
                    response.raise_for_status()
            finally:
                response.close()
        if entry is None or not entry.get("md5"):
            continue
        digest, size = digest_file(path)
        if digest == entry["md5"] and str(size) == str(entry.get("size")):
            continue
        state = journal.resume_state(path.name)
        same_payload = isinstance(state, dict) and state.get("signature") == {
            "identifier": item.identifier,
            "name": path.name,
            "md5": digest,
            "size": size,
        }
        if pending(journal, path.name) and not same_payload:
            raise ValueError(
                f"{path.name}: a multipart checkpoint is still active. "
                "Resolve its pending assembly before replacing this file."
            )
        print(f"Remote conflict: {path.name}")
        print(f"  Local:  {size} bytes, MD5 {digest}")
        print(f"  Remote: {entry.get('size')} bytes, MD5 {entry['md5']}")
        if not all_conflicts:
            choice = (
                input("Replace [r], replace all conflicts [a], cancel [c]: ")
                .strip()
                .lower()
            )
            if choice not in {"r", "a"}:
                raise ValueError("Replacement cancelled; no files transferred")
            all_conflicts = choice == "a"
        replacements[path.name] = (digest, size)
    deletions = []
    if clean:
        names = {path.name for path in files}
        candidates = []
        for name, entry in sorted(entries.items()):
            if name in names or not original(entry):
                continue
            if pending(journal, name):
                print(f"Cleanup blocked by multipart checkpoint: {name}")
                continue
            candidates.append(dict(entry))
        if candidates:
            print("Remote originals absent locally (none selected):")
            for index, entry in enumerate(candidates, 1):
                print(f"  {index}. {entry['name']} ({entry['size']} bytes)")
            choice = (
                input(
                    "Delete numbers separated by spaces, all, "
                    "or Enter for none: "
                )
                .strip()
                .lower()
            )
            if choice == "all":
                deletions = candidates
            elif choice:
                try:
                    indices = {int(value) for value in choice.split()}
                except ValueError as exc:
                    raise ValueError("Invalid cleanup selection") from exc
                if not indices <= set(range(1, len(candidates) + 1)):
                    raise ValueError("Invalid cleanup selection")
                deletions = [candidates[i - 1] for i in sorted(indices)]
            if deletions:
                if input("Type DELETE to approve selected originals: ") != (
                    "DELETE"
                ):
                    raise ValueError("Cleanup cancelled; no files transferred")
    return replacements, deletions


def delete_originals(session, identifier, entries, journal, policy=None):
    """Recheck approved evidence; issue conditional, non-cascading deletes."""
    policy = policy or RecoveryPolicy()
    for approved in entries:
        name = approved["name"]
        if pending(journal, name):
            raise ValueError(f"Cleanup blocked by pending assembly: {name}")
        data = session.get_metadata(
            identifier, request_kwargs={"timeout": (30, 60)}
        )
        if not isinstance(data, dict) or not isinstance(
            data.get("files"), list
        ):
            raise ValueError("Cannot verify current remote cleanup inventory")
        current = next(
            (e for e in data["files"] if e.get("name") == name), None
        )
        if current is None:
            journal.append("delete_absent", name=name)
            wait_for_deletion(session, identifier, approved, journal, policy)
            continue
        if not original(current) or any(
            str(current.get(key)) != str(approved.get(key))
            for key in ("md5", "size", "source")
        ):
            raise ValueError(
                f"Remote file changed after cleanup review: {name}"
            )
        request = iarequest.S3Request(
            method="DELETE",
            url=(
                "https://s3.us.archive.org/"
                + quote(identifier, safe="")
                + "/"
                + quote(name, safe="")
            ),
            access_key=session.access_key,
            secret_key=session.secret_key,
            headers={
                "If-Match": '"' + approved["md5"] + '"',
                "x-archive-cascade-delete": "0",
                "x-archive-queue-derive": "0",
            },
        )
        journal.append(
            "delete_started",
            name=name,
            md5=approved["md5"],
            size=approved["size"],
            source="original",
        )
        response = session.send(
            session.prepare_request(request), timeout=(30, 60)
        )
        try:
            check_response(response)
            if not 200 <= response.status_code < 300:
                raise ValueError(f"Deletion not accepted: {name}")
            journal.append("delete_accepted", name=name)
            print(f"Deletion accepted: {name}")
        except Exception:
            journal.append("delete_rejected", name=name)
            raise
        finally:
            response.close()
        wait_for_deletion(session, identifier, approved, journal, policy)


def unfinished_cleanup(journal):
    """Retain accepted/uncertain deletions until a final derive is accepted."""
    entries = {}
    for event in journal.events():
        if event.get("event") in {"derive_queued", "derive_already_pending"}:
            entries.clear()
        elif event.get("event") == "delete_rejected":
            entries.pop(event.get("name"), None)
        elif event.get("event") == "delete_started":
            entry = {
                key: event.get(key)
                for key in ("name", "md5", "size", "source")
            }
            if original(entry):
                entries[entry["name"]] = entry
    return list(entries.values())


def wait_for_deletion(session, identifier, approved, journal, policy=None):
    """Poll without repeating an uncertain DELETE or deriving old inventory."""
    policy = policy or RecoveryPolicy()
    name = approved["name"]
    checks = (policy.retries + 1) * policy.checks
    for attempt in range(checks):
        state, _ = remote_state(
            session,
            identifier,
            name,
            approved["md5"],
            int(approved["size"]),
        )
        if state == "missing":
            journal.append("delete_verified", name=name)
            return
        if attempt + 1 == checks:
            raise ValueError(
                f"Deletion still pending: {name}. Rerun cleanup later; "
                "no final derivation was requested."
            )
        print(f"Waiting for remote deletion: {name}", flush=True)
        time.sleep(min(policy.max_delay, policy.delay * 2 ** min(attempt, 20)))
