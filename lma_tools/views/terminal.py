"""Temporary terminal display for interactive fetch output."""

import os
import sys

from .client import ArchiveError


class FetchDisplay:
    """Restore the primary screen even when fetching raises or is cancelled."""

    def __init__(self, stream=None, sort="views"):
        self.width = 0
        self.sort = sort
        self.runtimes = {}
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = (
            self.stream.isatty() and os.environ.get("TERM") != "dumb"
        )

    def __enter__(self):
        if self.enabled:
            self.stream.write("\033[?1049h\033[H")
            self.stream.flush()
        return self

    def prepare(self, docs):
        self.width = max((len(doc["identifier"]) for doc in docs), default=0)
        self.runtimes = {
            doc["identifier"]: doc.get("runtime", "?") for doc in docs
        }

    def progress(self, identifier, value, completed, total):
        if not self.enabled:
            return
        display = "?" if isinstance(value, ArchiveError) else str(value)
        if self.sort == "runtime":
            display = str(self.runtimes.get(identifier, "?"))
        self.stream.write(f"{identifier:<{self.width + 2}}{display:>4}\n")
        self.stream.flush()

    def __exit__(self, exc_type, exc, traceback):
        if self.enabled:
            self.stream.write("\033[?1049l")
            self.stream.flush()
        return False
