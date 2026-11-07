"""Simple ordered percentage bars for local track preparation."""

import heapq
import os
import sys
from contextlib import contextmanager

from tqdm import tqdm
from tqdm.utils import disp_trim


class PreparationProgress:
    """Reuse one row per active worker, with a show-wide percentage total."""

    def __init__(self, tracks, workers, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = (
            self.stream.isatty() and os.environ.get("TERM") != "dumb"
        )
        self.count = len(tracks)
        self.lock = tqdm.get_lock()
        self.slots = list(range(2, min(workers, self.count) + 2))
        self.values = {track["destination"]: 0 for track in tracks}
        self.verified = set()
        self.total = self._bar("TOTAL", 0, self.count * 100)
        self.separator = tqdm(
            total=1,
            position=1,
            delay=1,
            leave=False,
            dynamic_ncols=True,
            disable=not self.enabled,
            file=self.stream,
            bar_format="{desc}",
        )
        self.separator.delay = 0
        self._separator()

    def _separator(self):
        if self.enabled:
            width = self.total.format_dict["ncols"] or 80
            self.separator.desc = "-" * max(1, width - 1)
            self.separator.display(msg=self.separator.desc, pos=1)

    def _bar(self, label, position, total=100):
        return tqdm(
            total=total,
            desc=disp_trim(label, 65),
            position=position,
            dynamic_ncols=True,
            leave=False,
            disable=not self.enabled,
            file=self.stream,
            bar_format="{desc}: {percentage:3.0f}%|{bar}|{postfix}",
        )

    @contextmanager
    def track(self, index, name):
        with self.lock:
            slot = heapq.heappop(self.slots)
            width = len(str(self.count))
            label = f"[{index:>{width}}/{self.count}] Preparing {name}"
            bar = self._bar(label, slot)
        try:

            def update(percent, phase="Converting"):
                with self.lock:
                    value = max(self.values[name], min(100, percent))
                    self.values[name] = value
                    if phase == "Verified" and name not in self.verified:
                        self.verified.add(name)
                        self.total.update(100)
                    if not self.enabled:
                        return
                    changed = bar.postfix != phase
                    bar.set_postfix_str(phase, refresh=False)
                    bar.update(value - bar.n)
                    if changed:
                        bar.refresh()
                    self._separator()

            yield update
        finally:
            with self.lock:
                bar.close()
                heapq.heappush(self.slots, slot)

    def close(self):
        with self.lock:
            self.separator.close()
            self.total.close()
