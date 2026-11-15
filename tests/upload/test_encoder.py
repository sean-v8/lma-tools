"""Reference encoding, PCM verification and terminal isolation."""

import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from mutagen.flac import FLAC

from lma_tools.upload.encoder import (
    compression_options,
    encode_flac,
    pcm_digest,
    verify_pcm,
)


@unittest.skipUnless(shutil.which("flac"), "reference flac required")
class EncoderTests(unittest.TestCase):
    def test_compression_levels_preserve_pcm_and_reduce_repetitive_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "audio.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((2, 3, 48000, 0, "NONE", "none"))
                audio.writeframes(b"\x01\x02\x03\xfe\xff\x7f" * 9000)
            plain = root / "plain.flac"
            encode_flac(source, plain, compression="uncompressed")
            for level in (0, 5, 8):
                with self.subTest(level=level):
                    target = root / f"level-{level}.flac"
                    encode_flac(source, target, compression=level)
                    self.assertLess(
                        target.stat().st_size, plain.stat().st_size
                    )
                    self.assertEqual(FLAC(target).info.bits_per_sample, 24)
                    verify_pcm(source, target)
            default = root / "default.flac"
            encode_flac(source, default)
            self.assertEqual(
                default.read_bytes(), (root / "level-5.flac").read_bytes()
            )

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
            encode_flac(source, target, compression="uncompressed")
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
                encode_flac(source, target)


class CompressionSettingTests(unittest.TestCase):
    def test_valid_levels_and_uncompressed_mode(self):
        for level in range(9):
            self.assertEqual(compression_options(level), [f"-{level}"])
        self.assertIn(
            "--disable-fixed-subframes", compression_options("uncompressed")
        )

    def test_upload_config_accepts_and_validates_compression(self):
        from lma_tools.upload.cli import load_config

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            for value in ("0", "5", "8", '"uncompressed"'):
                config.write_text(
                    f'[upload]\nshows_root = "{root}"\n'
                    f"flac_compression = {value}\n"
                )
                self.assertIn("flac_compression", load_config(config))
            for value in ("true", "9", '"fast"'):
                config.write_text(
                    f'[upload]\nshows_root = "{root}"\n'
                    f"flac_compression = {value}\n"
                )
                with self.assertRaisesRegex(ValueError, "flac_compression"):
                    load_config(config)

    def test_invalid_settings_are_rejected(self):
        for value in (-1, 9, True, False, 5.0, "5", "fast", None, []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "flac_compression"):
                    compression_options(value)


class EncoderStdinTests(unittest.TestCase):
    def test_pcm_verification_disables_interactive_input(self):
        process = Mock()
        process.stdout = io.BytesIO(b"decoded pcm")
        process.wait.return_value = 0
        manager = Mock()
        manager.__enter__ = Mock(return_value=process)
        manager.__exit__ = Mock(return_value=False)
        with patch(
            "lma_tools.upload.encoder.subprocess.Popen", return_value=manager
        ) as start:
            digest = pcm_digest("track.flac")
        self.assertIn("-nostdin", start.call_args.args[0])
        self.assertEqual(start.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(digest, hashlib.sha256(b"decoded pcm").hexdigest())
