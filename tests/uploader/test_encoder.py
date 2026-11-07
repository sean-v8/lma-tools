"""Real reference encoding and decoded PCM tests."""

import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from mutagen.flac import FLAC

from lma_tools.uploader.encoder import encode_verbatim, verify_pcm


@unittest.skipUnless(shutil.which("flac"), "reference flac required")
class EncoderTests(unittest.TestCase):
    def test_rate_mismatch_is_not_equivalent_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("a.wav", "b.wav")]
            for path, rate in zip(paths, (44100, 48000)):
                with wave.open(str(path), "wb") as audio:
                    audio.setparams((2, 3, rate, 0, "NONE", "none"))
                    audio.writeframes(b"\x01\x02\x03" * 100)
            with self.assertRaisesRegex(ValueError, "rate, channels"):
                verify_pcm(*paths)

    def test_verbatim_24bit_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "audio.wav", root / "audio.flac"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((2, 3, 48000, 0, "NONE", "none"))
                audio.writeframes(b"\x01\x02\x03\xfe\xff\x7f" * 9000)
            encode_verbatim(source, target)
            self.assertTrue(source.exists())
            self.assertEqual(FLAC(target).info.bits_per_sample, 24)
            verify_pcm(source, target)
            analysis = root / "analysis.txt"
            subprocess.run(
                [
                    "flac",
                    "--silent",
                    "--analyze",
                    "-o",
                    str(analysis),
                    str(target),
                ],
                check=True,
            )
            rows = [
                line
                for line in analysis.read_text().splitlines()
                if "subframe=" in line
            ]
            self.assertGreater(len(rows), 2)
            self.assertTrue(all("type=VERBATIM" in row for row in rows))
            with self.assertRaises(FileExistsError):
                encode_verbatim(source, target)
