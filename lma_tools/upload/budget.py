"""Cross-process transfer slots for one reviewed batch."""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .progress import check_cancelled


@contextmanager
def transfer_slot(cancel=None):
    directory = os.environ.get("LMA_UPLOAD_BATCH_SLOTS")
    if not directory:
        yield
        return
    slots = sorted(Path(directory).glob("slot-*"))
    if not slots:
        raise ValueError("Batch transfer slots are missing")
    handle = None
    try:
        while handle is None:
            check_cancelled(cancel)
            for path in slots:
                candidate = path.open("r+")
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    candidate.close()
                else:
                    handle = candidate
                    break
            if handle is None:
                time.sleep(0.1)
        yield
    finally:
        if handle is not None:
            handle.close()
