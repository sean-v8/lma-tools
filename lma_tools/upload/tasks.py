"""Background task polling keeps overview rendering independent of HTTP."""

import time
from threading import Event, Lock, Thread

from internetarchive import ArchiveSession

from .audit import task_status


class TaskMonitor:
    def __init__(self):
        self.lock = Lock()
        self.stop = Event()
        self.pending = set()
        self.states = {}
        self.checked = {}
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def update(self, jobs):
        with self.lock:
            for job in jobs:
                if job["status"] not in {"verified", "deriving"}:
                    continue
                key = (job["identifier"], job["started"])
                if time.monotonic() - self.checked.get(key, 0) >= 30:
                    self.pending.add(key)
                if key in self.states:
                    job["status"] = self.states[key]
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
            except Exception:
                # Task visibility never invalidates a completed file audit.
                continue

    def close(self):
        self.stop.set()
        self.thread.join(timeout=0.1)
