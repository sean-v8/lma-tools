"""Private per-run progress snapshots and a read-only tmux dashboard."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from .progress import OrderedProgress


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
        self.confirmed = set()
        self.active = {}
        self.wire = 0
        self.lock = RLock()
        self.stop = Event()
        self.data = {
            "identifier": identifier,
            "session": session,
            "pid": os.getpid(),
            "started": time.time(),
            "total": sum(self.sizes.values()),
            "files": len(files),
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
                completed=len(self.confirmed),
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

    def complete(self, name):
        with self.lock:
            self.confirmed.add(name)
            self.active.pop(name, None)

    def status(self, value):
        with self.lock:
            self.data["status"] = value

    def close(self, status):
        self.stop.set()
        self.thread.join()
        with self.lock:
            self.data["status"] = status
            self.active.clear()
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
    if not sys.stderr.isatty():
        raise ValueError("The upload dashboard needs a terminal.")
    try:
        while True:
            jobs = read_jobs(directory)
            for bar in bars.values():
                bar.clear()
            present = {job["started"] for job in jobs}
            for key in list(bars):
                if key not in present:
                    bars.pop(key).close()
            # Keep all live/stale jobs and the six latest finished runs.
            live = {"checking", "uploading", "deriving", "stale"}
            finished = [j for j in jobs if j["status"] not in live][-6:]
            jobs = [j for j in jobs if j["status"] in live or j in finished]
            visible = {job["started"] for job in jobs}
            for key in list(bars):
                if key not in visible:
                    bars.pop(key).close()
            for position, job in enumerate(jobs):
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
                bar.pos = -position
                bar.n = job["sent"]
                bar._ema_dn = lambda value=job["rate"]: value
                bar._ema_dt = lambda: 1
                bar.set_postfix_str(
                    f"{job['completed']}/{job['files']} confirmed, "
                    + job["status"],
                    refresh=False,
                )
                bar.refresh()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for bar in bars.values():
            bar.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=state_directory())
    args = parser.parse_args()
    print("Upload dashboard — Ctrl-b d detaches; Ctrl-C closes this view.")
    sys.stdout.flush()
    dashboard(args.state_dir)


if __name__ == "__main__":
    main()
