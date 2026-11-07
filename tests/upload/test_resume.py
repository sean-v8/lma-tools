"""Upload resume regression coverage."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests import HTTPError

from lma_tools.upload import cli as lma
from tests.upload.helpers import ID, response


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def upload(self, item, paths, journal, **kwargs):
        lma.upload_files(
            Mock(),
            item,
            paths,
            {},
            journal,
            0,
            0,
            recovery=lma.RecoveryPolicy(retries=0),
            **kwargs,
        )

    def test_remote_checksum_skips_without_library_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "one.flac"
            p.write_bytes(b"audio")
            digest, size, _ = lma.file_md5(p)
            item = Mock(
                exists=True,
                identifier=ID,
                files=[{"name": p.name, "md5": digest, "size": str(size)}],
            )
            self.upload(item, [p], lma.Journal(Path(tmp) / "log", ID))
            item.upload_file.assert_not_called()

    def test_log_does_not_override_remote_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "one.flac"
            p.write_bytes(b"audio")
            digest, size, _ = lma.file_md5(p)
            j = lma.Journal(Path(tmp) / "log", ID)
            j.append("upload_ok", name=p.name, md5=digest, size=size)
            item = Mock(
                exists=True,
                identifier=ID,
                files=[{"name": p.name, "md5": "0" * 32, "size": size}],
            )
            with self.assertRaisesRegex(ValueError, "Remote file differs"):
                self.upload(item, [p], j, trust_log=True)
            item.upload_file.assert_not_called()

    def test_log_requires_corroboration_or_explicit_trust(self):
        for trust, corroborated, should_skip in [
            (False, False, False),
            (False, True, True),
            (True, False, True),
        ]:
            with (
                self.subTest(trust=trust, corroborated=corroborated),
                tempfile.TemporaryDirectory() as tmp,
            ):
                p = Path(tmp) / "one.flac"
                p.write_bytes(b"audio")
                digest, size, _ = lma.file_md5(p)
                j = lma.Journal(Path(tmp) / "log", ID)
                j.append("upload_ok", name=p.name, md5=digest, size=size)
                item = Mock(exists=True, identifier=ID, files=[])
                item.upload_file.return_value = response()
                with patch.object(
                    lma, "corroborate_log", return_value=corroborated
                ):
                    self.upload(item, [p], j, trust_log=trust)
                self.assertEqual(
                    item.upload_file.call_count, 0 if should_skip else 1
                )

    def test_changed_local_file_does_not_match_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "one.flac"
            p.write_bytes(b"old")
            digest, size, _ = lma.file_md5(p)
            j = lma.Journal(Path(tmp) / "log", ID)
            j.append("upload_ok", name=p.name, md5=digest, size=size)
            p.write_bytes(b"new")
            digest, size, _ = lma.file_md5(p)
            self.assertFalse(j.acknowledgement(p.name, digest, size))

    def test_failure_stops_and_log_never_claims_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / n for n in ["one.flac", "two.flac"]]
            for p in paths:
                p.write_bytes(b"audio")
            j = lma.Journal(Path(tmp) / "log", ID)
            item = Mock(exists=False, files=[])
            item.upload_file.return_value = response(503, b"spam")
            with self.assertRaises(HTTPError):
                self.upload(item, paths, j)
            self.assertEqual(item.upload_file.call_count, 1)
            self.assertNotIn("upload_ok", [e["event"] for e in j.events()])
            self.assertFalse(item.upload_file.call_args.kwargs["checksum"])
            self.assertTrue(item.upload_file.call_args.kwargs["verify"])
            self.assertEqual(
                item.upload_file.call_args.kwargs["request_kwargs"]["timeout"],
                (300, 300),
            )

    def test_torn_log_and_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "log"
            p.write_text("{torn")
            j = lma.Journal(p, ID)
            j.append("upload_ok", name="a", md5="a", size=1)
            self.assertTrue(j.acknowledgement("a", "a", 1))
            self.assertFalse(
                lma.Journal(p, ID + ".other").acknowledgement("a", "a", 1)
            )
            j.append("upload_started", name="a", md5="a", size=1)
            self.assertFalse(j.acknowledgement("a", "a", 1))

    def test_force_reuploads_matching_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "one.flac"
            p.write_bytes(b"audio")
            digest, size, _ = lma.file_md5(p)
            item = Mock(
                exists=True,
                identifier=ID,
                files=[{"name": p.name, "md5": digest, "size": size}],
            )
            item.upload_file.return_value = response()
            self.upload(
                item, [p], lma.Journal(Path(tmp) / "log", ID), force=True
            )
            item.upload_file.assert_called_once()
