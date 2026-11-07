from fractions import Fraction
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from lma_tools.upload import core as lma_core


class ExactRuntimeTests(TestCase):
    def runtime(self, *pairs):
        streams = [
            SimpleNamespace(
                info=SimpleNamespace(total_samples=n, sample_rate=r)
            )
            for n, r in pairs
        ]
        with patch.object(lma_core, "FLAC", side_effect=streams):
            return lma_core.audio_runtime(range(len(streams)))

    def test_below_at_and_above_half_second(self):
        self.assertEqual(self.runtime((119999, 48000)), "0:00:02")
        self.assertEqual(self.runtime((120000, 48000)), "0:00:03")
        self.assertEqual(self.runtime((120001, 48000)), "0:00:03")

    def test_float_accumulation_regression(self):
        # Old sum([0.1] * 95) lies just below 9.5 seconds.
        self.assertEqual(self.runtime(*[(4800, 48000)] * 95), "0:00:10")

    def test_sum_before_rounding(self):
        self.assertEqual(
            self.runtime((28800, 48000), (28800, 48000)), "0:00:01"
        )

    def test_mixed_sample_rates(self):
        self.assertEqual(
            self.runtime((12000, 48000), (11025, 44100)), "0:00:01"
        )

    def test_duration_remains_exact(self):
        stream = SimpleNamespace(
            info=SimpleNamespace(total_samples=1, sample_rate=44100)
        )
        with patch.object(lma_core, "FLAC", return_value=stream):
            self.assertEqual(
                lma_core.audio_duration(["x"]), Fraction(1, 44100)
            )

    def test_invalid_unknown_or_empty(self):
        for pair in [(0, 48000), (1, 0), (-1, 48000)]:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                self.runtime(pair)
        with self.assertRaises(ValueError):
            lma_core.audio_runtime([])
