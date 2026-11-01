"""Coordinate upload bars without patching the Archive library globally."""

import os
import sys
from contextlib import contextmanager
from threading import RLock
from urllib.parse import urlsplit

from internetarchive import ArchiveSession
from tqdm import tqdm
from tqdm.utils import disp_len, disp_trim


class UploadCancelled(Exception):
    """The user requested cancellation of this upload run."""


def check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UploadCancelled("Upload cancelled")


def upload_size(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024


class UploadProgress(tqdm):
    """Display actual byte counts and non-inverted IEC transfer rates."""

    @property
    def format_dict(self):
        values = super().format_dict
        rate = values["rate"]
        if rate is None and values["elapsed"] > 0 and values["n"]:
            rate = values["n"] / values["elapsed"]
        values["upload_rate"] = (
            "? B/s" if rate is None else upload_size(rate) + "/s"
        )
        values["uploaded"] = upload_size(values["n"])
        values["expected"] = (
            "?" if values["total"] is None else upload_size(values["total"])
        )
        return values


class OrderedProgress(UploadProgress):
    """Keep byte/rate fields visible when a filename needs shortening."""

    def display(self, *args, **kwargs):
        if columns := getattr(self, "columns", None):
            columns.prepare(columns.bars)
        return super().display(*args, **kwargs)

    def clear(self, *args, **kwargs):
        if columns := getattr(self, "columns", None):
            columns.prepare(columns.bars)
        return super().clear(*args, **kwargs)

    @property
    def format_dict(self):
        values = progress_values(self)
        if columns := getattr(self, "columns", None):
            return columns.format(self, values)
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


def progress_values(bar):
    values = UploadProgress.format_dict.fget(bar)
    if hasattr(bar, "_display_elapsed"):
        values["elapsed"] = bar._display_elapsed
    if getattr(bar, "_resume_offset", 0) and values["rate"] is None:
        # Stored parts are progress, but were not transmitted in this run.
        values["rate"] = 0
        values["upload_rate"] = "0.00 B/s"
    return values


class SharedColumns:
    """One label, bar and metric grid for every currently displayed row."""

    def __init__(self, timing=True):
        self.timing = timing
        self.widths = {}
        self.label_width = 1
        self.bar_width = 1
        self.bars = []
        self.terminal_width = None

    def metrics(self, values):
        result = {
            "uploaded": values["uploaded"],
            "expected": values["expected"],
            "speed": values["upload_rate"],
            "status": values["postfix"] or "",
        }
        if self.timing:
            rate = values["rate"]
            if rate is None and values["elapsed"] > 0 and values["n"]:
                rate = values["n"] / values["elapsed"]
            result["elapsed"] = UploadProgress.format_interval(
                values["elapsed"]
            )
            result["remaining"] = (
                UploadProgress.format_interval(
                    max(0, values["total"] - values["n"]) / rate
                )
                if rate
                else "00:00"
                if values["n"] >= values["total"]
                else "?"
            )
        return result

    def tail(self, values):
        metrics = self.metrics(values)

        def field(key):
            return metrics[key].rjust(self.widths[key])

        text = field("uploaded") + "/" + field("expected") + " ["
        if self.timing:
            text += field("elapsed") + "<" + field("remaining") + ", "
        text += field("speed")
        if self.widths["status"]:
            separator = ", " if metrics["status"] else "  "
            text += separator + metrics["status"].ljust(self.widths["status"])
        return text + "]"

    def prepare(self, bars):
        if not bars:
            return
        self.bars = bars
        snapshots = [progress_values(bar) for bar in bars]
        for values in snapshots:
            for key, value in self.metrics(values).items():
                self.widths[key] = max(self.widths.get(key, 0), len(value))
        columns = min(values.get("ncols") or 80 for values in snapshots)
        if self.terminal_width not in (None, columns):
            # Reflow invalidates tqdm's old row positions and padding lengths.
            # Clear the visible viewport, preserving terminal scrollback.
            if bars[0].fp.isatty():
                bars[0].fp.write("\x1b[2J\x1b[H")
            for bar in bars:
                bar.sp = bar.status_printer(bar.fp)
        self.terminal_width = columns
        columns -= 1  # Never write the terminal's automatic-wrap column.
        longest = max(disp_len(bar.desc) for bar in bars)
        budget = columns - len(self.tail(snapshots[0])) - 8
        self.label_width = min(longest, max(1, budget - 4))
        self.bar_width = max(1, budget - self.label_width)
        for bar in bars:
            bar.columns = self
            bar.bar_format = (
                "{desc} {percentage:3.0f}%|"
                + "{bar:"
                + str(self.bar_width)
                + "}| {column_tail}"
            )

    def format(self, bar, values):
        label = bar.desc
        width = self.label_width
        if disp_len(label) > width:
            head = disp_trim(label, (width - 1) // 2)
            tail = label
            while tail and disp_len(tail) > width - disp_len(head) - 1:
                tail = tail[1:]
            label = head + "…" + tail
        values["prefix"] = label + " " * max(0, width - disp_len(label))
        values["column_tail"] = self.tail(values)
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
        self.columns = SharedColumns()

    def message(self, text, **kwargs):
        with self.lock:
            if self.enabled:
                UploadProgress.write(str(text), file=self.stream)
            else:
                print(text, file=self.stream, flush=True)

    def _clear(self):
        for row in self.rows:
            if row.bar is not None:
                row.bar.clear()

    def _redraw(self):
        self.rows.sort(key=lambda row: row.order)
        self.columns.prepare(
            [row.bar for row in self.rows if row.bar is not None]
        )
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
                self.bar.set_postfix_str(text, refresh=False)
                self.board._redraw()

    def begin(self):
        if self.board.tracker is not None:
            self.board.tracker.begin(self.path.name)
        with self.board.lock:
            if self.bar is not None:
                self.bar.reset()
                self.bar.set_postfix_str("", refresh=False)
                self.board._redraw()

    def resume(self, offset):
        if self.board.tracker is not None:
            self.board.tracker.resume(self.path.name, offset)
        with self.board.lock:
            if self.bar is not None:
                self.bar.reset()
                self.bar.n = self.bar.initial = self.bar.last_print_n = offset
                self.bar._resume_offset = offset
                self.board._redraw()

    def advance(self, size):
        if self.board.tracker is not None:
            self.board.tracker.advance(self.path.name, size)
        with self.board.lock:
            if self.bar is not None:
                refreshed = self.bar.update(size)
                if self.bar.n >= self.bar.total:
                    self.bar.set_postfix_str("awaiting ack", refresh=False)
                    refreshed = True
                if refreshed:
                    self.board._redraw()

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
            and not getattr(body, "_lma_multipart", False)
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
def watch_transfer(session, path, tracker, row=None, cancel=None):
    """Observe sequential PUTs with local bars and restore the session."""
    if (tracker is None and row is None) or isinstance(
        session, TransferSession
    ):
        yield
        return
    original_send = session.send
    previous = session.__dict__.get("send")

    class Observer:
        def begin(self):
            tracker.begin(path.name)

        def advance(self, size):
            tracker.advance(path.name, size)

    observer = row if row is not None else Observer()

    def send(request, **kwargs):
        original = request.body
        body = original.body if isinstance(original, UploadBody) else original
        if (
            request.method == "PUT"
            and hasattr(body, "read")
            and not getattr(body, "_lma_multipart", False)
        ):
            observer.begin()
            request.body = UploadBody(body, observer, cancel)
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
