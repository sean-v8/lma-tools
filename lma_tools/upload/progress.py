"""Coordinate upload bars without patching the Archive library globally."""

import math
import os
import sys
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic
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


class TransferRate:
    """Sample current wire traffic, independent of restored progress."""

    def __init__(self):
        self.previous = None
        self.value = 0
        self.sampled = False

    def sample(self, wire, now):
        if self.previous is None or now < self.previous[0]:
            self.previous = now, wire
            return self.value
        before, count = self.previous
        elapsed = now - before
        if elapsed >= 0.5:
            current = max(0, wire - count) / elapsed
            weight = -math.expm1(-elapsed / 3)
            self.value = (
                self.value + weight * (current - self.value)
                if self.sampled
                else current
            )
            self.sampled = True
            self.previous = now, wire
        return self.value


class UploadProgress(tqdm):
    """Display actual byte counts and non-inverted IEC transfer rates."""

    def moveto(self, n):
        if getattr(self, "_in_place", False):
            if n:
                self.fp.write(f"\x1b[{abs(n)}{'B' if n > 0 else 'A'}")
                self.fp.flush()
            return
        super().moveto(n)

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
        result = super().display(*args, **kwargs)
        self._rendered_width = disp_len(str(self))
        return result

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
    if hasattr(bar, "_wire_rate"):
        rate = bar._wire_rate()
        values["rate"] = rate
        values["upload_rate"] = upload_size(rate) + "/s"
    if hasattr(bar, "_display_rate"):
        values["rate"] = bar._display_rate
        values["upload_rate"] = (
            upload_size(bar._display_rate) + "/s"
            if bar._display_rate is not None
            else "? B/s"
        )
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
        self.bounded_clear = False

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
        # Size metrics from the current frame, not historical ETA extremes.
        self.widths = {}
        for values in snapshots:
            for key, value in self.metrics(values).items():
                self.widths[key] = max(self.widths.get(key, 0), len(value))
        columns = min(values.get("ncols") or 80 for values in snapshots)
        if self.terminal_width not in (None, columns):
            # Erase only the progress region, including its reflowed rows.
            if bars[0].fp.isatty():
                width = getattr(bars[0], "_rendered_width", 0)
                wrapped = max(0, width - 1) // max(1, columns)
                up = f"\x1b[{wrapped}A" if wrapped else ""
                if self.bounded_clear:
                    heights = [
                        1
                        + max(0, getattr(bar, "_rendered_width", 0) - 1)
                        // max(1, columns)
                        for bar in bars
                    ]
                    # The separator is also part of the owned upload grid.
                    separator = 1 + max(0, self.terminal_width - 2) // columns
                    rows = sum(heights) + separator
                    erase = "\x1b[2K" + "\x1b[B\x1b[2K" * (rows - 1)
                    back = f"\x1b[{rows - 1}A" if rows > 1 else ""
                    bars[0].fp.write("\r" + up + erase + back + "\r")
                else:
                    bars[0].fp.write("\r" + up + "\x1b[J")
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
    def __init__(self, stream=None, tracker=None, files=None):
        files = tuple(files) if files is not None else None
        self.tracker = tracker
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = self.stream.isatty() and os.environ.get("TERM") not in (
            None,
            "",
            "dumb",
        )
        self.persistent = self.enabled and files is not None
        # Use tqdm's own reentrant lock, including during monitor refreshes.
        self.lock = tqdm.get_lock()
        self.rows = []
        self.columns = SharedColumns()
        self.columns.bounded_clear = files is not None
        self.sizes = (
            {path.name: path.stat().st_size for path in files}
            if files is not None
            else None
        )
        self.active = {}
        self.confirmed = set()
        self.wire = 0
        self.started = monotonic()
        self.transfer_started = None
        self.last_transfer = None
        self.rate = TransferRate()
        self.stop = Event()
        self.thread = None
        self.closed = False
        self.total = None
        self.separator = None
        if self.sizes is not None and self.enabled:
            with self.lock:
                print(file=self.stream, flush=True)
                self.total = OrderedProgress(
                    total=sum(self.sizes.values()),
                    desc="TOTAL",
                    position=0,
                    delay=1,
                    leave=False,
                    dynamic_ncols=True,
                    unit="B",
                    file=self.stream,
                )
                self.separator = UploadProgress(
                    total=1,
                    position=1,
                    delay=1,
                    leave=False,
                    dynamic_ncols=True,
                    file=self.stream,
                    bar_format="{desc}",
                )
                self.separator.delay = 0
                self.total._wire_rate = self._rate
                self.total.delay = 0
                self.rows = [
                    ProgressRow(self, path, index)
                    for index, path in enumerate(files, 1)
                ]
                # Reserve rows once; later redraws only move the cursor.
                height = len(self.rows) + 2
                self.stream.write("\n" * (height - 1))
                self.stream.write(f"\x1b[{height - 1}A\r")
                for bar in [self.total, self.separator]:
                    bar._in_place = True
                for row in self.rows:
                    row.bar._in_place = True
                self._redraw()
            self.thread = Thread(target=self._watch, daemon=True)
            self.thread.start()

    def _watch(self):
        while not self.stop.wait(0.5):
            with self.lock:
                if self.closed:
                    return
                self._redraw()

    def _rate(self):
        finished = self.sizes is not None and (
            sum(self.sizes[n] for n in self.confirmed)
            + sum(self.active.values())
            >= sum(self.sizes.values())
        )
        if finished:
            elapsed = (
                self.last_transfer - self.transfer_started
                if self.last_transfer is not None
                else 0
            )
            return self.wire / max(elapsed, 0.001)
        if self.tracker is not None and hasattr(
            type(self.tracker), "current_rate"
        ):
            return self.tracker.current_rate()
        return self.rate.sample(self.wire, monotonic())

    def _record(self, name, event, value=0):
        if self.sizes is None or name not in self.sizes or self.closed:
            return
        now = monotonic()
        if self.transfer_started is None and event in {"begin", "resume"}:
            if event == "begin" or value < self.sizes[name]:
                self.transfer_started = now
                self.rate.sample(self.wire, now)
        if event == "complete":
            self.confirmed.add(name)
            self.active.pop(name, None)
        elif name not in self.confirmed:
            if event == "advance":
                self.wire += max(0, value)
                if value > 0:
                    self.last_transfer = now
                    if self.transfer_started is None:
                        self.transfer_started = now
                value = self.active.get(name, 0) + max(0, value)
            self.active[name] = min(self.sizes[name], max(0, value))
        if self.total is not None:
            self.total.n = sum(self.sizes[n] for n in self.confirmed)
            self.total.n += sum(self.active.values())

    def complete(self, name, state="complete"):
        """Credit a confirmed upload or skip; retain it until board close.

        Tracker completion remains the caller's responsibility, as before.
        """
        with self.lock:
            if self.closed:
                return
            self._record(name, "complete")
            if self.persistent:
                row = next(r for r in self.rows if r.path.name == name)
                row.bar.n = row.bar.total
                if state == "skipped":
                    row.bar._display_rate = 0
                row.finish(state)
            self._redraw()

    def close(self):
        """Close live bars and retain one final file inventory on terminals."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        with self.lock:
            if self.closed:
                return
            lines = []
            if self.persistent:
                self._redraw()
                lines = [str(self.total), self.separator.desc]
                lines.extend(str(row.bar) for row in self.rows)
            self.closed = True
            for row in list(self.rows):
                self.remove(row)
            if self.separator is not None:
                self.separator.close()
            if self.total is not None:
                self.total.close()
            self.columns.bars = []
            if lines:
                print("\n".join(lines), file=self.stream, flush=True)

    def message(self, text, **kwargs):
        with self.lock:
            if self.enabled:
                UploadProgress.write(
                    str(text),
                    file=self.stream,
                    end="\n\n" if self.total is not None else "\n",
                )
            else:
                print(text, file=self.stream, flush=True)

    def _clear(self):
        if self.total is not None:
            self.total.clear()
        if self.separator is not None:
            self.separator.clear()
        for row in self.rows:
            if row.bar is not None:
                row.bar.clear()

    def _redraw(self):
        self.rows.sort(key=lambda row: row.order)
        if self.closed:
            return
        bars = [row.bar for row in self.rows if row.bar is not None]
        if self.total is not None:
            bars.insert(0, self.total)
        self.columns.bars = bars
        self.columns.prepare(bars)
        for position, bar in enumerate(bars):
            if self.total is not None and position:
                position += 1
            bar.pos = -position
            bar.refresh()
        if self.separator is not None:
            width = max(1, self.columns.terminal_width - 1)
            self.separator.desc = "-" * width
            self.separator.display(msg=self.separator.desc, pos=1)

    def row(self, path, order):
        with self.lock:
            if self.closed:
                raise RuntimeError("Progress board is closed")
            if self.persistent:
                row = next(r for r in self.rows if r.path == path)
                if path.name not in self.confirmed:
                    row.status("checking")
                return row
            self._clear()
            row = ProgressRow(self, path, order)
            self.rows.append(row)
            self._redraw()
            return row

    def remove(self, row):
        with self.lock:
            if row not in self.rows or (self.persistent and not self.closed):
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
        self.started = False
        self.wire = 0
        self.transfer_started = None
        self.last_transfer = None
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
            self.bar.set_postfix_str(
                "waiting" if board.persistent else "checking", refresh=False
            )
            if board.persistent:
                self.bar._display_elapsed = 0

    def message(self, text, *, transient=False, **kwargs):
        if transient and self.board.enabled:
            # The current retry status already occupies this file's row.
            # Keep full details in the journal, not terminal scrollback.
            return
        self.board.message(f"{self.path.name}: {str(text).strip()}")

    def status(self, text):
        with self.board.lock:
            if self.bar is not None:
                if not self.started:
                    self.started = True
                    if hasattr(self.bar, "_display_elapsed"):
                        del self.bar._display_elapsed
                    self.bar.start_t = self.bar.last_print_t = self.bar._time()
                text = {"verifying remote checksum": "verifying"}.get(
                    text, text
                )
                if text in {"completing", "assembling"}:
                    elapsed = (
                        self.last_transfer - self.transfer_started
                        if self.last_transfer is not None
                        else 0
                    )
                    self.bar._display_rate = self.wire / max(elapsed, 0.001)
                self.bar.set_postfix_str(text, refresh=False)
                self.board._redraw()

    def _start_transfer(self):
        if self.transfer_started is None:
            self.transfer_started = monotonic()
        if self.bar is not None:
            for attribute in ("_display_rate", "_display_elapsed"):
                if hasattr(self.bar, attribute):
                    delattr(self.bar, attribute)

    def begin(self):
        if self.board.tracker is not None:
            self.board.tracker.begin(self.path.name)
        with self.board.lock:
            self.board._record(self.path.name, "begin")
            self._start_transfer()
            if self.bar is not None:
                self.bar._resume_offset = 0
                self.bar.reset()
                self.bar.set_postfix_str("", refresh=False)
                self.board._redraw()

    def resume(self, offset):
        if self.board.tracker is not None:
            self.board.tracker.resume(self.path.name, offset)
        with self.board.lock:
            self.board._record(self.path.name, "resume", offset)
            if self.bar is None or offset < self.bar.total:
                self._start_transfer()
            if self.bar is not None:
                self.bar.reset()
                self.bar.n = self.bar.initial = self.bar.last_print_n = offset
                self.bar._resume_offset = offset
                self.board._redraw()

    def advance(self, size):
        if self.board.tracker is not None:
            self.board.tracker.advance(self.path.name, size)
        with self.board.lock:
            self.board._record(self.path.name, "advance", size)
            if size > 0:
                if self.transfer_started is None:
                    self._start_transfer()
                self.wire += size
                self.last_transfer = monotonic()
            if self.bar is not None:
                refreshed = self.bar.update(size)
                if self.bar.n >= self.bar.total:
                    self.bar.set_postfix_str("awaiting ack", refresh=False)
                    refreshed = True
                if refreshed or self.board.total is not None:
                    self.board._redraw()

    def finish(self, state):
        if self.board.enabled:
            self.status(state)
            if self.board.persistent and state in {
                "complete",
                "skipped",
                "failed",
                "cancelled",
            }:
                values = progress_values(self.bar)
                self.bar._display_elapsed = values["elapsed"]
                self.bar._display_rate = values["rate"]
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
