import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lma_tools.uploader.progress import (
    OrderedProgress,
    ProgressBoard,
    SharedColumns,
)
from tests.uploader.test_parallel import TtyBuffer


class ColumnTests(unittest.TestCase):
    def assert_aligned(self, bars, width):
        lines = [str(bar) for bar in bars]
        self.assertEqual(len({line.index("|") for line in lines}), 1)
        self.assertEqual(len({line.rindex("|") for line in lines}), 1)
        self.assertEqual(len({len(line) for line in lines}), 1)
        for line in lines:
            self.assertLessEqual(len(line), width)
            self.assertIn("B/s", line)
            self.assertTrue(line.endswith("]"), line)
        return lines

    def test_file_rows_share_columns_across_metrics_and_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            with patch.dict("os.environ", TERM="xterm"):
                board = ProgressBoard(TtyBuffer())
                try:
                    for index, name in enumerate(
                        [
                            "short.flac",
                            "artist2026-01-01.long.s1t01.flac",
                            "another.s1t02.flac",
                        ]
                    ):
                        path = Path(tmp) / name
                        with path.open("wb") as handle:
                            handle.truncate(1024 ** (index + 1))
                        row = board.row(path, index)
                        rows.append(row)
                        row.bar.dynamic_ncols = None
                        row.bar.ncols = 160
                        row.bar.n = 123 * (index + 1)
                        row.bar._ema_dn = lambda n=index: 10 ** (n + 1)
                        row.bar._ema_dt = lambda: 1
                    board._redraw()
                    bars = [row.bar for row in rows]
                    lines = self.assert_aligned(bars, 160)
                    self.assertIn("short.flac ", lines[0])
                    rows[1].status("awaiting ack")
                    self.assert_aligned(bars, 160)
                    for bar in bars:
                        bar.ncols = 80
                    board._redraw()
                    self.assert_aligned(bars, 80)
                    rows[1].close()
                    self.assert_aligned([rows[0].bar, rows[2].bar], 80)
                finally:
                    for row in rows:
                        row.close()

    def test_dashboard_rows_use_longest_label_and_same_bar_width(self):
        bars = []
        columns = SharedColumns(timing=False)
        try:
            for index, name in enumerate(
                [
                    "short2026-01-01",
                    "longer-artist2026-01-01.recording",
                    "third2026-01-02",
                ]
            ):
                bar = OrderedProgress(
                    total=1024 ** (index + 1),
                    desc=name,
                    file=TtyBuffer(),
                    ncols=140,
                    delay=1,
                )
                bar.n = index + 1
                bar.set_postfix_str(
                    f"{index}/12 confirmed, uploading", refresh=False
                )
                bars.append(bar)
            columns.prepare(bars)
            lines = self.assert_aligned(bars, 140)
            self.assertEqual(lines[0].index("|"), len(bars[1].desc) + 5)
            bars[-1].set_description_str("a-much-longer-show2026-01-02", False)
            columns.prepare(bars)
            self.assert_aligned(bars, 140)
        finally:
            for bar in bars:
                bar.close()
