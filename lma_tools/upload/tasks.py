"""Background task polling keeps overview rendering independent of HTTP."""

import json
import math
import os
import tempfile
import time
from pathlib import Path
from threading import Event, Lock, Thread

from internetarchive import ArchiveSession

from .audit import task_status


def saved_completion(job):
    """Read an observed terminal result without altering uploader snapshots."""
    if not job.get("_state_path"):
        return None
    path = Path(job["_state_path"]).with_suffix(".done")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        finished = result["finished"]
        if (
            result["identifier"] == job["identifier"]
            and result["started"] == job["started"]
            and result["status"] in {"verified", "derive failed"}
            and type(finished) in (int, float)
            and math.isfinite(finished)
            and finished >= job["started"]
        ):
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_completion(job, status, finished):
    """Publish once, atomically, keeping concurrent observers' first result."""
    result = dict(
        identifier=job["identifier"],
        started=job["started"],
        status=status,
        finished=finished,
    )
    if not job.get("_state_path"):
        return result
    if saved := saved_completion(job):
        return saved
    path = Path(job["_state_path"]).with_suffix(".done")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    except OSError:
        # A read-only state directory must not break the dashboard.
        pass
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return saved_completion(job) or result


class TaskMonitor:
    def __init__(self):
        self.lock = Lock()
        self.stop = Event()
        self.pending = set()
        self.states = {}
        self.checked = {}
        self.finished = {}
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def update(self, jobs):
        with self.lock:
            for job in jobs:
                if job.get("_task_complete"):
                    continue
                if job["status"] not in {"verified", "deriving"}:
                    continue
                key = (job["identifier"], job["started"])
                if time.monotonic() - self.checked.get(key, 0) >= 30:
                    self.pending.add(key)
                if key in self.states:
                    job["status"] = self.states[key]
                    if key in self.finished:
                        result = save_completion(
                            job, self.states[key], self.finished[key]
                        )
                        job["heartbeat"] = max(
                            job.get("heartbeat", 0), result["finished"]
                        )
                        job["status"] = result["status"]
                        job["_task_complete"] = True
                    if not job.get("transfer_finished"):
                        job["rate"] = 0
        return jobs

    def _run(self):
        while not self.stop.wait(0.5):
            with self.lock:
                if not self.pending:
                    continue
                key = self.pending.pop()
                self.checked[key] = time.monotonic()
            try:
                with ArchiveSession() as session:
                    if not session.access_key or not session.secret_key:
                        continue
                    state = task_status(session, key[0])
                with self.lock:
                    self.states[key] = state
                    if state == "deriving":
                        self.finished.pop(key, None)
                    else:
                        # Freeze at first detection, not each later poll.
                        self.finished.setdefault(key, time.time())
            except Exception:
                # Task visibility never invalidates a completed file audit.
                continue

    def close(self):
        self.stop.set()
        self.thread.join(timeout=0.1)
