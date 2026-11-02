"""Private per-run progress snapshots and a read-only tmux dashboard."""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from .progress import OrderedProgress, SharedColumns

LIVE = {"checking", "uploading", "deriving"}


class DashboardJobs:
    """Retain observed runs for this view, without restoring old history."""

    def __init__(self, opened=None):
        self.opened = time.time() if opened is None else opened
        self.jobs = {}
        self.samples = {}

    def update(self, jobs, now=None):
        now = time.time() if now is None else now
        for job in jobs:
            key = job["started"]
            if (
                key not in self.jobs
                and job["status"] not in LIVE | {"stale"}
                and job["heartbeat"] < self.opened
            ):
                continue
            job = dict(job)
            heartbeat, rate = self.samples.get(
                key, (job["heartbeat"], job["rate"])
            )
            if job["heartbeat"] > heartbeat:
                weight = -math.expm1(-(job["heartbeat"] - heartbeat) / 30)
                rate += weight * (job["rate"] - rate)
            self.samples[key] = job["heartbeat"], rate
            job["rate"] = rate if job["status"] in LIVE else 0
            self.jobs[key] = job
        for job in self.jobs.values():
            if job["status"] in LIVE and now - job["heartbeat"] > 10:
                job.update(status="stale", rate=0)
        return sorted(self.jobs.values(), key=lambda job: job["started"])


def total_progress(jobs):
    """Sum current work and retained successes, excluding stopped attempts."""
    included = [
        job
        for job in jobs
        if job["status"] in LIVE | {"done", "derive failed"}
    ]
    total = {
        key: sum(job[key] for job in included)
        for key in ("total", "sent", "confirmed", "completed", "files")
    }
    running = sum(job["status"] in LIVE for job in included)
    total.update(
        started="total",
        identifier="TOTAL",
        elapsed=elapsed_time(included),
        rate=sum(job["rate"] for job in included if job["status"] in LIVE),
        status=f"{running} running"
        if running
        else "derive failed"
        if any(job["status"] == "derive failed" for job in included)
        else "done"
        if included
        else "idle",
    )
    return total


def elapsed_time(jobs, now=None):
    """Current-run wall time, frozen when work stops or loses its heartbeat."""
    if not jobs:
        return 0
    now = time.time() if now is None else now
    end = (
        now
        if any(job["status"] in LIVE for job in jobs)
        else max(job["heartbeat"] for job in jobs)
    )
    return max(0, end - min(job["started"] for job in jobs))


def status_text(job):
    """Normal transfers need no status label beside their moving bar."""
    text = f"{job['completed']}/{job['files']} completed"
    state = job["status"]
    if state == "uploading":
        return text
    return (
        text
        + ", "
        + {"checking": "check", "deriving": "derive"}.get(state, state)
    )


def state_directory():
    if directory := os.environ.get("LMA_UPLOAD_STATE_DIR"):
        return Path(directory).expanduser().resolve()
    base = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state"))
    return base.expanduser().resolve() / "lma-tools/uploads"


class ShowProgress:
    def __init__(self, identifier, files, session, directory=None):
        self.directory = directory or state_directory()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / (uuid4().hex + ".json")
        self.sizes = {p.name: p.stat().st_size for p in files}
        self.flacs = {
            name for name in self.sizes if name.lower().endswith(".flac")
        }
        self.confirmed = set()
        self.active = {}
        self.partial = {}
        self.wire = 0
        self.lock = RLock()
        self.stop = Event()
        self.data = {
            "identifier": identifier,
            "session": session,
            "pid": os.getpid(),
            "started": time.time(),
            "total": sum(self.sizes.values()),
            "files": len(self.flacs),
            "status": "checking",
        }
        self.previous = (time.monotonic(), 0)
        self.publish()
        self.thread = Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self):
        while not self.stop.wait(1):
            self.publish()

    def publish(self):
        with self.lock:
            now = time.monotonic()
            before, wire = self.previous
            rate = (self.wire - wire) / max(now - before, 0.001)
            if self.data["status"] not in (
                "checking",
                "uploading",
                "deriving",
            ):
                rate = 0
            self.previous = now, self.wire
            confirmed = sum(self.sizes[n] for n in self.confirmed)
            sent = sum(
                min(self.sizes[n], size)
                for n, size in self.active.items()
                if n not in self.confirmed
            )
            self.data.update(
                heartbeat=time.time(),
                confirmed=confirmed,
                sent=confirmed + sent,
                completed=len(self.confirmed & self.flacs),
                rate=rate,
            )
            temp = self.path.with_suffix(".tmp")
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    os.chmod(temp, 0o600)
                    json.dump(self.data, handle)
                os.replace(temp, self.path)
            except OSError:
                # Dashboard trouble must never stop an upload.
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def begin(self, name):
        with self.lock:
            self.active[name] = 0
            self.data["status"] = "uploading"

    def advance(self, name, size):
        with self.lock:
            self.active[name] = self.active.get(name, 0) + size
            self.wire += size

    def resume(self, name, offset):
        with self.lock:
            self.active[name] = offset
            self.partial[name] = offset
            self.data["status"] = "uploading"

    def complete(self, name):
        with self.lock:
            self.confirmed.add(name)
            self.active.pop(name, None)
            self.partial.pop(name, None)

    def status(self, value):
        with self.lock:
            self.data["status"] = value

    def close(self, status):
        self.stop.set()
        self.thread.join()
        with self.lock:
            self.data["status"] = status
            self.active = dict(self.partial)
        self.publish()


def read_jobs(directory, now=None):
    now = time.time() if now is None else now
    jobs = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            numeric = (
                "heartbeat",
                "started",
                "total",
                "sent",
                "confirmed",
                "completed",
                "files",
                "rate",
            )
            if not isinstance(data, dict) or not all(
                isinstance(data.get(key), (int, float)) for key in numeric
            ):
                continue
            if not all(
                isinstance(data.get(key), str)
                for key in ("identifier", "status")
            ):
                continue
            if now - data["heartbeat"] > 86400:
                path.unlink(missing_ok=True)
                continue
            if data["status"] in ("checking", "uploading", "deriving"):
                if now - data["heartbeat"] > 10:
                    data["status"] = "stale"
                    data["rate"] = 0
            jobs.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(jobs, key=lambda job: job["started"])


def dashboard(directory):
    bars = {}
    separator = OrderedProgress(
        total=1, position=1, leave=False, delay=1, bar_format="{desc}"
    )
    separator.delay = 0
    columns = SharedColumns()
    history = DashboardJobs()
    if not sys.stderr.isatty():
        raise ValueError("The upload dashboard needs a terminal.")
    sys.stderr.write("\x1b[?25l")
    try:
        while True:
            jobs = history.update(read_jobs(directory))
            jobs = [total_progress(jobs), *jobs]
            for bar in bars.values():
                bar.clear()
            separator.sp = separator.status_printer(separator.fp)
            separator.clear()
            present = {job["started"] for job in jobs}
            for key in list(bars):
                if key not in present:
                    bars.pop(key).close()
            for index, job in enumerate(jobs):
                position = index if index == 0 else index + 1
                key = job["started"]
                if key not in bars:
                    bars[key] = OrderedProgress(
                        total=job["total"],
                        desc=job["identifier"],
                        position=position,
                        leave=False,
                        delay=1,
                        dynamic_ncols=True,
                        unit="B",
                        miniters=1,
                        bar_format=(
                            "{desc} {percentage:3.0f}%|{bar}| "
                            "{uploaded}/{expected} [{upload_rate}{postfix}]"
                        ),
                    )
                    bars[key].delay = 0
                bar = bars[key]
                bar.total = job["total"]
                bar.pos = -position
                bar.n = job["sent"]
                bar._display_elapsed = (
                    job["elapsed"] if "elapsed" in job else elapsed_time([job])
                )
                bar._ema_dn = lambda value=job["rate"]: value
                bar._ema_dt = lambda: 1
                bar.set_postfix_str(
                    status_text(job),
                    refresh=False,
                )
            columns.prepare([bars[job["started"]] for job in jobs])
            separator.display(
                msg="-" * max(1, columns.terminal_width - 1), pos=1
            )
            for job in jobs:
                bars[job["started"]].refresh()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        separator.close()
        for bar in bars.values():
            bar.close()
        sys.stderr.write("\x1b[?25h")
        sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=state_directory())
    args = parser.parse_args()
    print("Upload dashboard — Ctrl-b d detaches; Ctrl-C closes this view.")
    sys.stdout.flush()
    dashboard(args.state_dir)


if __name__ == "__main__":
    main()
