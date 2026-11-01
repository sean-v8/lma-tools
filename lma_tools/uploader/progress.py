"""Coordinate upload bars without patching the Archive library globally."""

import os
import sys
from threading import RLock
from urllib.parse import urlsplit

from internetarchive import ArchiveSession
from internetarchive.item import _UploadProgress


class UploadCancelled(Exception):
    """The user requested cancellation of this upload run."""


def check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UploadCancelled("Upload cancelled")


class ProgressBoard:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = self.stream.isatty() and os.environ.get("TERM") not in (
            None,
            "",
            "dumb",
        )
        self.lock = RLock()

    def message(self, text, **kwargs):
        with self.lock:
            if self.enabled:
                _UploadProgress.write(str(text), file=self.stream)
            else:
                print(text, file=self.stream, flush=True)

    def row(self, path, slot):
        return ProgressRow(self, path, slot)


class ProgressRow:
    def __init__(self, board, path, slot):
        self.board = board
        self.path = path
        self.bar = None
        if board.enabled:
            self.bar = _UploadProgress(
                total=path.stat().st_size,
                desc=f" uploading {path.name}",
                position=slot,
                leave=False,
                dynamic_ncols=True,
                unit="B",
                miniters=1,
                file=board.stream,
                bar_format=(
                    "{desc}: {percentage:3.0f}%|{bar}| "
                    "{uploaded}/{expected} [{elapsed}<{remaining}, "
                    "{upload_rate}{postfix}]"
                ),
            )
            self.bar.set_postfix_str("checking")

    def message(self, text, **kwargs):
        self.board.message(f"{self.path.name}: {str(text).strip()}")

    def status(self, text):
        with self.board.lock:
            if self.bar is not None:
                self.bar.set_postfix_str(text)

    def begin(self):
        with self.board.lock:
            if self.bar is not None:
                self.bar.reset()
                self.bar.set_postfix_str("uploading")

    def advance(self, size):
        with self.board.lock:
            if self.bar is not None:
                self.bar.update(size)
                if self.bar.n >= self.bar.total:
                    self.bar.set_postfix_str("awaiting acknowledgement")

    def close(self):
        with self.board.lock:
            if self.bar is not None:
                self.bar.close()


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
