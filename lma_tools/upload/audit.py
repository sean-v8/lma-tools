"""Read-only transfer summaries and post-upload checksum verification."""

import html
import time

from .recovery import remote_state


def inventory(files):
    from .cli import file_md5

    return {
        path.name: {"md5": digest, "size": size}
        for path in files
        for digest, size, _ in [file_md5(path)]
    }


def transfer_summary(item, files, force=False):
    """Describe known metadata evidence without authorizing replacement."""
    remote = {entry["name"]: entry for entry in item.files}
    groups = {name: [] for name in ("upload", "skip", "replace", "check")}
    for name, local in inventory(files).items():
        entry = remote.get(name)
        if entry is None:
            action = "upload"
        elif not entry.get("md5") or entry.get("size") is None:
            action = "check"
        elif (
            str(entry["md5"]).lower() == local["md5"]
            and str(entry["size"]) == str(local["size"])
            and not force
        ):
            action = "skip"
        else:
            action = "replace"
        groups[action].append(name)
    print("Transfer summary (current remote metadata):")
    for action, names in groups.items():
        print(f"  {action}: {len(names)}")
        if action != "skip":
            for name in names:
                print(f"    {name}")
    print("  Missing metadata entries are checked again before transfer.")
    return groups


def equivalent(key, expected, actual):
    values = actual if isinstance(actual, list) else [actual]
    if key == "collection":
        required = expected if isinstance(expected, list) else [expected]
        return all(value in values for value in required)
    if key == "description":
        return any(
            isinstance(value, str)
            and html.unescape(value).replace("\r\n", "\n") == expected
            for value in values
        )
    if key == "date":
        return any(str(value).split("T")[0] == expected for value in values)
    return expected in values


def audit_upload(session, identifier, files, metadata, attempts=3):
    """Never equate HTTP acceptance or a task submission with verification."""
    expected = inventory(files)
    errors = []
    for attempt in range(attempts):
        errors = []
        data = session.get_metadata(
            identifier, request_kwargs={"timeout": (15, 30)}
        )
        remote = data.get("metadata", {})
        for key, value in metadata.items():
            if not equivalent(key, value, remote.get(key)):
                errors.append(f"metadata: {key}")
        for name, local in expected.items():
            state, _ = remote_state(
                session, identifier, name, local["md5"], local["size"]
            )
            if state != "match":
                errors.append(f"file: {name} ({state})")
        if not errors:
            return {"files": len(expected), "fields": len(metadata)}
        if attempt + 1 < attempts:
            time.sleep(2)
    raise ValueError("Post-upload audit not verified: " + "; ".join(errors))


def task_status(session, identifier):
    """Query active derive tasks; completed transfers remain verified."""
    summary = session.get_tasks_summary(
        identifier,
        params={"cmd": "derive.php"},
        request_kwargs={"timeout": (5, 10)},
    )
    if not isinstance(summary, dict) or (
        summary.get("success") is False
        or isinstance(summary.get("error"), str)
    ):
        raise ValueError("Archive task status unavailable")
    if any(summary.get(key, 0) for key in ("error", "paused")):
        return "derive failed"
    if any(summary.get(key, 0) for key in ("queued", "running")):
        return "deriving"
    return "verified"
