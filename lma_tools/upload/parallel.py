"""Bounded scheduling with failure draining and cooperative cancellation."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event

from .progress import ProgressBoard


def upload_workers(value):
    if type(value) is not int or value < 1:
        raise ValueError("upload_workers/--workers must be a positive integer")
    return value


def run_parallel(entries, workers, transfer, board=None, tracker=None):
    board = board if board is not None else ProgressBoard(tracker=tracker)
    entries = iter(entries)
    cancel = Event()
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lmau")
    pending = {}
    failure = None
    halt = Event()

    def execute(entry, slot):
        if halt.is_set():
            return
        try:
            return transfer(*entry, slot, board, cancel)
        except BaseException:
            halt.set()
            raise

    def schedule(slot):
        if halt.is_set():
            return
        entry = next(entries, None)
        if entry is not None:
            pending[pool.submit(execute, entry, slot)] = slot

    try:
        for slot in range(workers):
            schedule(slot)
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            available = []
            for future in done:
                slot = pending.pop(future)
                available.append(slot)
                try:
                    future.result()
                except Exception as exc:
                    if failure is None:
                        failure = exc
                        board.message(
                            "Upload failed; no further files will start. "
                            "Waiting for active transfers to finish."
                        )
            if failure is None:
                for slot in available:
                    schedule(slot)
    except KeyboardInterrupt:
        cancel.set()
        board.message(
            "Cancelling uploads and retry waits. Requests awaiting a reply "
            "may need to reach their socket timeout."
        )
        for future in pending:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if failure is not None:
        raise failure
