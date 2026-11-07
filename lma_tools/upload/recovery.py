"""Bounded recovery for uploads whose acknowledgement was lost."""

import math
import re
import time
from builtins import TimeoutError as SocketTimeout
from dataclasses import dataclass
from urllib.parse import quote

import requests
from urllib3.exceptions import MaxRetryError, ProtocolError, TimeoutError

from .core import check_response
from .progress import UploadCancelled, check_cancelled

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


class RemoteConflict(ValueError):
    """Known remote bytes conflict with the selected local file."""


@dataclass(frozen=True)
class RecoveryPolicy:
    retries: int = 3
    checks: int = 3
    delay: float = 30.0
    max_delay: float = 300.0


def load_policy(data):
    if not isinstance(data, dict):
        raise ValueError("upload.recovery must be a TOML table")
    unknown = set(data) - set(RecoveryPolicy.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown recovery settings: {sorted(unknown)}")
    policy = RecoveryPolicy(**data)
    for key, minimum in (("retries", 0), ("checks", 1)):
        value = getattr(policy, key)
        if type(value) is not int or value < minimum:
            raise ValueError(f"recovery.{key} must be an integer >= {minimum}")
    for key in ("delay", "max_delay"):
        value = getattr(policy, key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"recovery.{key} must be positive and finite")
    if policy.max_delay < policy.delay:
        raise ValueError("recovery.max_delay cannot be less than delay")
    return policy


def retryable(error):
    if isinstance(error, requests.exceptions.SSLError):
        return False
    if isinstance(error, requests.HTTPError):
        return (
            error.response is not None
            and error.response.status_code in TRANSIENT_HTTP
        )
    if isinstance(error, MaxRetryError):
        return retryable(error.reason)
    return isinstance(
        error,
        (
            requests.Timeout,
            requests.ConnectionError,
            ConnectionError,
            TimeoutError,
            SocketTimeout,
            ProtocolError,
        ),
    )


def unchanged(path, size, stamp):
    current = path.stat()
    if (current.st_size, current.st_mtime_ns) != (size, stamp):
        raise ValueError(f"File changed during upload: {path.name}")


def evidence(digest, size, remote_digest, remote_size, force, name):
    try:
        remote_size = int(remote_size)
    except (TypeError, ValueError):
        remote_size = None
    remote_digest = str(remote_digest or "").strip('"').lower()
    known_digest = re.fullmatch(r"[0-9a-f]{32}", remote_digest)
    if known_digest and remote_digest == digest and remote_size == size:
        return "match"
    if known_digest and (
        remote_digest != digest
        or (remote_size is not None and remote_size != size)
    ):
        if not force:
            raise RemoteConflict(
                f"Remote file differs: {name}. "
                "Inspect it; --force explicitly allows replacement."
            )
        return "replace"
    return "unknown"


def remote_state(session, identifier, name, digest, size, force=False):
    """Require MD5 plus size for success, or two absence checks for resend."""
    absent = False
    metadata_state = "unknown"
    try:
        data = session.get_metadata(
            identifier, request_kwargs={"timeout": (15, 30)}
        )
        if isinstance(data, dict):
            entries = data.get("files")
            if isinstance(entries, list) and all(
                isinstance(entry, dict) and isinstance(entry.get("name"), str)
                for entry in entries
            ):
                entry = next((e for e in entries if e["name"] == name), None)
                absent = entry is None
                if entry is not None:
                    metadata_state = evidence(
                        digest,
                        size,
                        entry.get("md5"),
                        entry.get("size"),
                        force,
                        name,
                    )
                    if metadata_state == "match":
                        return "match", "metadata"
            elif not data:
                absent = True
    except RemoteConflict:
        raise
    except Exception as exc:
        if not retryable(exc) and not isinstance(exc, ValueError):
            raise
    state, source = head_state(session, identifier, name, digest, size, force)
    if state == "missing" and not absent:
        state = "unknown"
    if state == "replace":
        state = "missing" if metadata_state == "replace" else "unknown"
    return state, source


def head_state(session, identifier, name, digest, size, force=False):
    url = (
        f"https://archive.org/download/{quote(identifier, safe='')}"
        f"/{quote(name, safe='')}"
    )
    try:
        response = session.head(url, allow_redirects=True, timeout=(15, 30))
        try:
            if response.status_code == 404:
                return "missing", "HEAD"
            response.raise_for_status()
            if response.status_code == 200:
                return evidence(
                    digest,
                    size,
                    response.headers.get("ETag"),
                    response.headers.get("Content-Length"),
                    force,
                    name,
                ), "HEAD"
        finally:
            response.close()
    except RemoteConflict:
        raise
    except Exception as exc:
        if not retryable(exc) and not isinstance(exc, ValueError):
            raise
    return "unknown", "HEAD"


def upload_with_recovery(
    send,
    session,
    identifier,
    path,
    digest,
    size,
    stamp,
    journal,
    policy,
    force=False,
    notify=None,
    cancel=None,
    on_state=None,
):
    emit = notify if notify is not None else print
    pending = None
    for cycle in range(policy.retries + 1):
        check_cancelled(cancel)
        unchanged(path, size, stamp)
        if pending is None:
            try:
                response = send()
                if response is None or response.status_code is None:
                    raise ValueError(
                        "Upload returned no HTTP acknowledgement; "
                        "file is not marked successful."
                    )
                check_response(response)
                if not 200 <= response.status_code < 300:
                    raise ValueError(
                        f"Unexpected upload status: {response.status_code}"
                    )
                unchanged(path, size, stamp)
                return {"status": response.status_code}
            except Exception as exc:
                if not retryable(exc) or policy.retries == 0:
                    raise
                pending = exc
                emit(
                    "  Upload acknowledgement uncertain "
                    f"({type(exc).__name__}); "
                    "checking remote checksums before resending.",
                    flush=True,
                )
                journal.append(
                    "upload_uncertain",
                    name=path.name,
                    md5=digest,
                    size=size,
                    error=type(exc).__name__,
                )
        if on_state is not None:
            on_state("verifying remote checksum")
        last_state = "unknown"
        for check in range(policy.checks):
            delay = min(
                policy.max_delay,
                policy.delay * 2 ** min(cycle * policy.checks + check, 20),
            )
            emit(
                f"  Waiting {delay:g}s before remote verification "
                f"{check + 1}/{policy.checks}.",
                flush=True,
            )
            if cancel is None:
                time.sleep(delay)
            elif cancel.wait(delay):
                raise UploadCancelled("Upload cancelled during recovery wait")
            check_cancelled(cancel)
            unchanged(path, size, stamp)
            last_state, source = remote_state(
                session, identifier, path.name, digest, size, force
            )
            journal.append(
                "upload_verification",
                name=path.name,
                md5=digest,
                size=size,
                state=last_state,
                source=source,
                recovery_round=cycle + 1,
            )
            if last_state == "match":
                unchanged(path, size, stamp)
                emit(
                    "  Confirmed: remote MD5 and size match; continuing.",
                    flush=True,
                )
                return {"verified_by": source}
        if cycle < policy.retries:
            if last_state == "missing":
                emit(
                    f"  Retrying {path.name} from the beginning "
                    f"({cycle + 1}/{policy.retries}).",
                    flush=True,
                )
                journal.append(
                    "upload_retry",
                    name=path.name,
                    md5=digest,
                    size=size,
                    attempt=cycle + 2,
                )
                pending = None
                if on_state is not None:
                    on_state("retrying")
            else:
                emit(
                    "  Remote state is still unknown; continuing checks "
                    "without resending.",
                    flush=True,
                )
    emit("  Automatic recovery exhausted; stopping this run.", flush=True)
    raise pending
