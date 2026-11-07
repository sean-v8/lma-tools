"""Verification subprocesses must never read the interactive terminal."""

import hashlib
import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from lma_tools.uploader.encoder import pcm_digest


class EncoderStdinTests(unittest.TestCase):
    def test_pcm_verification_disables_interactive_input(self):
        process = Mock()
        process.stdout = io.BytesIO(b"decoded pcm")
        process.wait.return_value = 0
        manager = Mock()
        manager.__enter__ = Mock(return_value=process)
        manager.__exit__ = Mock(return_value=False)
        with patch(
            "lma_tools.uploader.encoder.subprocess.Popen", return_value=manager
        ) as start:
            digest = pcm_digest("track.flac")
        self.assertIn("-nostdin", start.call_args.args[0])
        self.assertEqual(start.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(digest, hashlib.sha256(b"decoded pcm").hexdigest())
