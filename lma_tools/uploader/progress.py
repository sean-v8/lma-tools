"""Coordinate upload bars without patching the Archive library globally."""

import os
import sys
from contextlib import contextmanager
from threading import RLock
from urllib.parse import urlsplit

from internetarchive import ArchiveSession
from internetarchive.item import _UploadProgress


class UploadCancelled(Exception):
    """The user requested cancellation of this upload run."""


def check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UploadCancelled("Upload cancelled")


class OrderedProgress(_UploadProgress):
    """Keep byte/rate fields visible when a filename needs shortening."""

    @property
    def format_dict(self):
        values = super().format_dict
        columns = values.get("ncols")
        if columns:
            elapsed = values["elapsed"]
            rate = values["rate"]
            if rate is None and elapsed > 0 and values["n"]:
                rate = values["n"] / elapsed
            remaining = (
                self.format_interval(
                    max(0, values["total"] - values["n"]) / rate
                )
                if rate
                else "?"
            )
            postfix = ", " + values["postfix"] if values["postfix"] else ""
            fixed = (
                f" 100%|| {values['uploaded']}/{values['expected']} "
                f"[{self.format_interval(elapsed)}<{remaining}, "
                f"{values['upload_rate']}{postfix}]"
            )
            width = max(8, columns - len(fixed) - 4)
            label = values["prefix"]
            if len(label) > width:
                head = (width - 1) // 2
                values["prefix"] = (
                    label[:head] + "…" + label[-(width - head - 1) :]
                )
        return values


class ProgressBoard:
    def __init__(self, stream=None, tracker=None):
        self.tracker = tracker
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = self.stream.isatty() and os.environ.get("TERM") not in (
            None,
            "",
            "dumb",
        )
        self.lock = RLock()
        self.rows = []

    def message(self, text, **kwargs):
        with self.lock:
            if self.enabled:
                _UploadProgress.write(str(text), file=self.stream)
            else:
                print(text, file=self.stream, flush=True)

    def _clear(self):
        for row in self.rows:
            if row.bar is not None:
                row.bar.clear()

    def _redraw(self):
        self.rows.sort(key=lambda row: row.order)
        for position, row in enumerate(self.rows):
            if row.bar is not None:
                row.bar.pos = -position
                row.bar.refresh()

    def row(self, path, order):
        with self.lock:
            self._clear()
            row = ProgressRow(self, path, order)
            self.rows.append(row)
            self._redraw()
            return row

    def remove(self, row):
        with self.lock:
            if row not in self.rows:
                return
            self._clear()
            if row.bar is not None:
                row.bar.close()
            self.rows.remove(row)
            self._redraw()


class ProgressRow:
    def __init__(self, board, path, order):
        self.board = board
        self.path = path
        self.order = order
        self.bar = None
        if board.enabled:
            self.bar = OrderedProgress(
                total=path.stat().st_size,
                desc=path.name,
                position=0,
                delay=1,
                leave=False,
                dynamic_ncols=True,
                unit="B",
                miniters=1,
                file=board.stream,
                bar_format=(
                    "{desc} {percentage:3.0f}%|{bar}| "
                    "{uploaded}/{expected} [{elapsed}<{remaining}, "
                    "{upload_rate}{postfix}]"
                ),
            )
            self.bar.delay = 0
            self.bar.set_postfix_str("checking", refresh=False)

    def message(self, text, **kwargs):
        self.board.message(f"{self.path.name}: {str(text).strip()}")

    def status(self, text):
        with self.board.lock:
            if self.bar is not None:
                text = {"verifying remote checksum": "verifying"}.get(
                    text, text
                )
                self.bar.set_postfix_str(text)

    def begin(self):
        if self.board.tracker is not None:
            self.board.tracker.begin(self.path.name)
        with self.board.lock:
            if self.bar is not None:
                self.bar.reset()
                self.bar.set_postfix_str("")

    def advance(self, size):
        if self.board.tracker is not None:
            self.board.tracker.advance(self.path.name, size)
        with self.board.lock:
            if self.bar is not None:
                self.bar.update(size)
                if self.bar.n >= self.bar.total:
                    self.bar.set_postfix_str("awaiting ack")

    def finish(self, state):
        if self.board.enabled:
            self.status(state)
        else:
            self.message(state.capitalize())

    def close(self):
        self.board.remove(self)


class UploadBody:
    """Count only reads by the HTTP transport, not local checksum passes."""

    def __init__(self, body, row, cancel):
        self.body, self.row, self.cancel = body, row, cancel

    def read(self, size=-1):
        check_cancelled(self.cancel)
        data = self.body.read(size)
        self.row.advance(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self.body, name)


class TransferSession(ArchiveSession):
    """One independent session for each active transfer and recovery loop."""

    def __init__(self, row, cancel, **kwargs):
        self.row, self.cancel = row, cancel
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        check_cancelled(self.cancel)
        original = request.body
        body = original.body if isinstance(original, UploadBody) else original
        if (
            request.method == "PUT"
            and (
                urlsplit(request.url).hostname == "s3.us.archive.org"
                or isinstance(original, UploadBody)
            )
            and hasattr(body, "read")
        ):
            self.row.begin()
            request.body = UploadBody(body, self.row, self.cancel)
        try:
            return super().send(request, **kwargs)
        finally:
            request.body = original


@contextmanager
def watch_transfer(session, path, tracker):
    """Observe sequential PUTs without changing their existing display."""
    if tracker is None or isinstance(session, TransferSession):
        yield
        return
    original_send = session.send
    previous = session.__dict__.get("send")

    class Observer:
        def begin(self):
            tracker.begin(path.name)

        def advance(self, size):
            tracker.advance(path.name, size)

    observer = Observer()

    def send(request, **kwargs):
        original = request.body
        body = original.body if isinstance(original, UploadBody) else original
        if request.method == "PUT" and hasattr(body, "read"):
            observer.begin()
            request.body = UploadBody(body, observer, None)
        try:
            return original_send(request, **kwargs)
        finally:
            request.body = original

    session.send = send
    try:
        yield
    finally:
        if previous is None:
            del session.send
        else:
            session.send = previous
