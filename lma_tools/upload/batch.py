"""Review shows in order and launch approved transfers without waiting."""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from requests import RequestException

from .audit import inventory
from .dashboard import state_directory
from .parallel import upload_workers


def file_evidence(entries):
    return {
        entry["name"]: (entry.get("md5"), str(entry.get("size")))
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def validate_approval(plan, item, files, metadata):
    """A queued approval grants no permission to upload changed input."""
    if plan["inventory"] != inventory(files):
        raise ValueError("Local files changed after approval; review again")
    if plan["existed"] != bool(item.exists):
        raise ValueError("Remote item existence changed; review again")
    before = file_evidence(plan["remote_files"])
    after = file_evidence(item.files)
    names = set(plan["inventory"]) | {
        entry["name"] for entry in plan["deletions"]
    }
    if any(before.get(name) != after.get(name) for name in names):
        raise ValueError("Remote files changed after approval; review again")
    if any(
        plan["remote_metadata"].get(key) != item.metadata.get(key)
        for key in metadata
    ):
        raise ValueError(
            "Remote metadata changed after approval; review again"
        )


def write_plan(directory, payload):
    fd, name = tempfile.mkstemp(
        prefix="approved-", suffix=".json", dir=directory
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return Path(name)


def consume_plan(path):
    from . import cli

    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Approved plan must be a private, owned regular file")
    claimed = path.with_suffix(".running")
    path.rename(claimed)
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        os.environ["LMA_UPLOAD_BATCH_SLOTS"] = str(path.parent / "slots")
        return cli.main(payload["argv"], approved=payload["approval"])
    finally:
        claimed.unlink(missing_ok=True)
        cleanup_batch(path.parent)


def run_batch(argv, args, config):
    from . import cli, launcher

    if args.identifier or args.notes or args.clean:
        raise ValueError(
            "Use an explicit show with --identifier/--notes/--clean"
        )
    outputs = cli.scan_outputs(config["shows_root"])
    if not outputs:
        print("No shows found in shows_root.")
        return 0
    asynchronous = not (args.prepare or args.preview or args.dry_run)
    if asynchronous and not shutil.which("tmux"):
        raise ValueError("Batch uploads require tmux; use an explicit show")
    directory = None
    if asynchronous:
        launcher.ensure_dashboard()
        state = state_directory()
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="batch-", dir=state))
        (directory / ".reviewing").touch(mode=0o600)
        slots = directory / "slots"
        slots.mkdir(mode=0o700)
        workers = upload_workers(
            args.workers
            if args.workers is not None
            else config.get("upload_workers", 3)
        )
        for index in range(workers):
            (slots / f"slot-{index:04d}").touch(mode=0o600)
    try:
        return review_outputs(argv, args, config, outputs, directory)
    finally:
        if directory is not None:
            (directory / ".reviewing").unlink(missing_ok=True)
            cleanup_batch(directory)


def review_outputs(argv, args, config, outputs, directory):
    from . import cli, launcher

    asynchronous = directory is not None
    result = 0
    for number, (_, output) in enumerate(outputs, 1):
        print(f"\nShow {number}/{len(outputs)}: {output.name}")
        pending = []
        arguments = [*argv, str(output.resolve())]
        try:
            code = cli.main(
                arguments, enqueue=pending.append if asynchronous else None
            )
            result = max(result, code)
            for approval in pending:
                plan = write_plan(
                    directory, {"argv": arguments, "approval": approval}
                )
                try:
                    launcher.start_window(
                        approval["identifier"],
                        ["--approved-plan", str(plan)],
                        args.config,
                        select=False,
                    )
                except BaseException:
                    plan.unlink(missing_ok=True)
                    raise
        except KeyboardInterrupt:
            print("\nBatch review stopped; approved uploads continue.")
            return 130
        except (ValueError, OSError, RequestException) as error:
            print(f"Skipped {output.name}: {error}", file=sys.stderr)
            result = 1
    if asynchronous:
        print("Batch review complete; approved uploads continue in tmux.")
    return result


def cleanup_batch(directory):
    """Remove transfer slots only after review and every child have ended."""
    if (directory / ".reviewing").exists() or any(
        directory.glob("approved-*")
    ):
        return
    try:
        for slot in (directory / "slots").glob("slot-*"):
            slot.unlink()
        (directory / "slots").rmdir()
        directory.rmdir()
    except OSError:
        pass
