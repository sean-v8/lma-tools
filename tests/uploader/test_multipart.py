import base64
import hashlib
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from internetarchive import ArchiveSession
from internetarchive.item import Item
from requests import ReadTimeout, Response

from lma_tools.uploader import cli, multipart
from lma_tools.uploader.progress import TransferSession, UploadCancelled
from lma_tools.uploader.recovery import RecoveryPolicy


def xml_response(text, status=200):
    response = Response()
    response.status_code = status
    response._content = text.encode()
    response._content_consumed = True
    return response


class MultipartTests(unittest.TestCase):
    def test_end_of_part_reports_waiting_for_ack_once_per_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.flac"
            path.write_bytes(b"abcde")
            progress, sent = Mock(), Mock()
            body = multipart.PartReader(path, 0, 5, progress, None)
            body.on_sent = sent
            try:
                body.read(3)
                progress.status.assert_not_called()
                body.read(2)
                progress.status.assert_called_once_with("awaiting ack")
                body.read(2)
                sent.assert_called_once_with()
                body.restart()
                body.read()
                self.assertEqual(sent.call_count, 2)
            finally:
                body.close()

    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        output = self.root / "output"
        output.mkdir()
        self.path = output / "one.flac"
        with self.path.open("wb") as handle:
            for value, count in ((b"A", 80), (b"B", 16)):
                for _ in range(count):
                    handle.write(value * (64 * 1024))
        self.digest, self.size, self.stamp = cli.file_md5(self.path)
        self.session = ArchiveSession(
            config={"s3": {"access": "test-access", "secret": "test-secret"}}
        )
        self.addCleanup(self.session.close)
        self.progress = Mock()
        self.uploads = {}
        self.calls = []
        self.puts = []
        self.stop_part = None
        self.drop_part_ack = False
        self.drop_complete_ack = False
        self.list_supported = True
        self.complete = False
        self.visible = True
        self.bad_etag = False
        self.bad_complete = False
        self.redirect = None
        self.next_id = 0
        self.log = self.root / "show.log"
        self.policy = RecoveryPolicy(0, 1, 0.001, 0.001)
        transport = patch.object(
            self.session, "send", side_effect=self.transport
        )
        transport.start()
        self.addCleanup(transport.stop)
        verify = patch.object(
            multipart,
            "remote_state",
            side_effect=(
                lambda *args, **kw: (
                    ("match", "metadata")
                    if self.complete and self.visible
                    else ("missing", "HEAD")
                )
            ),
        )
        verify.start()
        self.addCleanup(verify.stop)

    def upload(self, session=None, options=None):
        return multipart.MultipartUpload(
            session or self.session,
            "artist2026-08-17",
            self.path,
            self.digest,
            self.size,
            self.stamp,
            cli.Journal(self.log, "artist2026-08-17"),
            options or multipart.MultipartOptions(True, 5),
            self.policy,
            self.progress,
        )

    def error(self, code, status=404):
        return xml_response(
            f"<Error><Code>{code}</Code><Message>test</Message></Error>",
            status,
        )

    def transport(self, request, **kwargs):
        self.calls.append(request)
        if request.method == "HEAD":
            return xml_response("", 404)
        self.assertFalse(kwargs.get("allow_redirects", False))
        self.assertEqual(
            request.headers["Authorization"], "LOW test-access:test-secret"
        )
        query = parse_qs(urlsplit(request.url).query, keep_blank_values=True)
        if request.method == "POST" and "uploads" in query:
            self.assertEqual(urlsplit(request.url).query, "uploads")
            self.next_id += 1
            uid = f"upload-{self.next_id}"
            self.uploads[uid] = {}
            self.assertEqual(request.headers["x-archive-queue-derive"], "0")
            return xml_response(
                f"<Initiate><UploadId>{uid}</UploadId></Initiate>"
            )
        uid = query["uploadId"][0]
        if request.method == "GET" and not self.list_supported:
            return self.error("NotImplemented", 501)
        if uid not in self.uploads:
            return self.error("NoSuchUpload")
        if request.method == "GET":
            text = "<ListPartsResult>"
            for number, (digest, size) in self.uploads[uid].items():
                text += (
                    f"<Part><PartNumber>{number}</PartNumber>"
                    f'<ETag>"{digest}"</ETag><Size>{size}</Size></Part>'
                )
            return xml_response(text + "</ListPartsResult>")
        if request.method == "PUT":
            if self.redirect:
                response = xml_response("", 307)
                response.headers["Location"] = self.redirect
                self.redirect = None
                return response
            number = int(query["partNumber"][0])
            self.puts.append(number)
            if number == self.stop_part:
                raise UploadCancelled("simulated interruption")
            digest = hashlib.md5()
            size = 0
            while chunk := request.body.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
            self.assertEqual(int(request.headers["Content-Length"]), size)
            self.assertNotIn("Transfer-Encoding", request.headers)
            self.assertEqual(
                request.headers["Content-MD5"],
                base64.b64encode(digest.digest()).decode(),
            )
            self.assertEqual(request.headers["x-archive-queue-derive"], "0")
            self.uploads[uid][number] = (digest.hexdigest(), size)
            if self.drop_part_ack:
                self.drop_part_ack = False
                raise ReadTimeout("accepted part; lost response")
            response = xml_response("")  # IAS3 need not return an ETag.
            if self.bad_etag:
                response.headers["ETag"] = '"' + "a" * 32 + '"'
            return response
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["x-archive-queue-derive"], "1")
        self.assertFalse(
            any(key.startswith("x-archive-meta") for key in request.headers)
        )
        if self.bad_complete:
            return self.error("InvalidPart", 200)
        xml = ET.fromstring(request.body)
        for part in xml.findall("Part"):
            number = int(part.findtext("PartNumber"))
            etag = part.findtext("ETag")
            self.assertEqual(etag, '"' + self.uploads[uid][number][0] + '"')
        self.complete = True
        del self.uploads[uid]
        if self.drop_complete_ack:
            self.drop_complete_ack = False
            raise ReadTimeout("accepted completion; lost response")
        return xml_response("<CompleteMultipartUploadResult/>")

    def test_streaming_resume_reuses_parts_and_creates_no_audio_copies(self):
        self.stop_part = 2
        upload = self.upload()
        with self.assertRaises(UploadCancelled):
            upload.run()
        checkpoint = upload.journal.resume_state(self.path.name)
        self.assertEqual(list(checkpoint["parts"]), ["1"])
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-secret", self.log.read_text())
        self.stop_part = None
        result = self.upload().run()
        self.assertTrue(result["multipart"])
        self.assertEqual(self.puts.count(1), 1)
        self.assertEqual(self.puts.count(2), 2)
        self.assertEqual(self.next_id, 1)
        self.assertIsNone(upload.journal.resume_state(self.path.name))
        self.assertEqual(
            {p for p in self.root.rglob("*") if p.is_file()},
            {self.path, self.log},
        )
        self.assertEqual(cli.file_md5(self.path)[0], self.digest)

    def test_lost_part_ack_is_reconciled_without_resending_that_part(self):
        self.drop_part_ack = True
        with self.assertRaises(ReadTimeout):
            self.upload().run()
        self.upload().run()
        self.assertEqual(self.puts, [1, 2])

    def test_unsupported_listing_uses_checkpoint_then_server_verification(
        self,
    ):
        self.stop_part = 2
        with self.assertRaises(UploadCancelled):
            self.upload().run()
        self.stop_part = None
        self.list_supported = False
        self.upload().run()
        self.assertEqual(self.puts.count(1), 1)

    def test_expired_upload_id_starts_fresh_only_before_completion(self):
        self.stop_part = 2
        with self.assertRaises(UploadCancelled):
            self.upload().run()
        self.uploads.clear()
        self.stop_part = None
        self.upload().run()
        self.assertEqual(self.next_id, 2)
        self.assertEqual(self.puts.count(1), 2)

    def test_lost_completion_ack_verifies_without_reuploading(self):
        self.drop_complete_ack = True
        result = self.upload().run()
        self.assertEqual(result["verified_by"], "metadata")
        self.assertEqual(self.puts, [1, 2])
        self.assertEqual(self.next_id, 1)

    def test_pending_assembly_keeps_state_and_rerun_only_verifies(self):
        self.visible = False
        upload = self.upload()
        with self.assertRaises(multipart.AssemblyPending):
            upload.run()
        self.assertEqual(
            upload.journal.resume_state(self.path.name)["phase"], "assembling"
        )
        count = len(self.calls)
        self.visible = True
        self.upload().run()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.next_id, 1)

    def test_crash_before_completion_post_can_finish_without_resending_parts(
        self,
    ):
        upload = self.upload()
        original = upload.request

        def interrupted(method, params, **kwargs):
            if method == "POST" and "uploadId" in params:
                raise KeyboardInterrupt
            return original(method, params, **kwargs)

        with patch.object(upload, "request", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                upload.run()
        self.assertEqual(
            upload.journal.resume_state(self.path.name)["phase"], "completing"
        )
        self.upload().run()
        self.assertEqual(self.puts, [1, 2])
        self.assertEqual(self.next_id, 1)

    def test_local_content_change_does_not_reuse_old_checkpoint(self):
        self.stop_part = 2
        with self.assertRaises(UploadCancelled):
            self.upload().run()
        with self.path.open("r+b") as handle:
            handle.write(b"changed")
        self.digest, self.size, self.stamp = cli.file_md5(self.path)
        count = len(self.calls)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.upload().run()
        self.assertEqual(len(self.calls), count)

    def test_bad_etag_or_xml_error_never_counts_as_success(self):
        self.bad_etag = True
        with self.assertRaisesRegex(multipart.MultipartError, "ETag mismatch"):
            self.upload().run()
        self.bad_etag = False
        self.bad_complete = True
        with self.assertRaisesRegex(multipart.MultipartError, "InvalidPart"):
            self.upload().run()
        self.assertFalse(self.complete)
        count = len(self.puts)
        self.bad_complete = False
        self.upload().run()
        self.assertEqual(len(self.puts), count)
        self.assertTrue(self.complete)

    def test_external_redirect_never_receives_credentials_or_audio(self):
        self.redirect = "https://example.com/steal"
        with self.assertRaisesRegex(multipart.MultipartError, "Untrusted"):
            self.upload().run()
        self.assertTrue(
            all(
                urlsplit(r.url).hostname == "s3.us.archive.org"
                for r in self.calls
            )
        )

    def test_trusted_redirect_retains_part_query_and_checksums(self):
        self.redirect = "https://ia800001.us.archive.org/item/one.flac"
        self.upload().run()
        redirected = [
            r
            for r in self.calls
            if urlsplit(r.url).hostname != "s3.us.archive.org"
        ]
        self.assertEqual(len(redirected), 1)
        self.assertIn("partNumber=1", redirected[0].url)
        self.assertIn("uploadId=upload-1", redirected[0].url)

    def test_cli_backend_uses_existing_log_and_confirms_file(
        self,
    ):
        item = Item(
            self.session,
            "artist2026-08-17",
            {"metadata": {"identifier": "artist2026-08-17"}, "files": []},
        )
        journal = cli.Journal(self.root / "show.log", item.identifier)
        self.stop_part = 2
        with self.assertRaises(UploadCancelled):
            cli.upload_files(
                self.session,
                item,
                [self.path],
                {},
                journal,
                0,
                0,
                recovery=self.policy,
                multipart=multipart.MultipartOptions(True, 5),
            )
        self.stop_part = None
        # A larger new default must not bypass the existing checkpoint.
        cli.upload_files(
            self.session,
            item,
            [self.path],
            {},
            journal,
            0,
            0,
            recovery=self.policy,
            multipart=multipart.MultipartOptions(True, 16),
        )
        self.assertEqual(self.puts.count(1), 1)
        self.assertTrue(self.complete)
        self.assertTrue(journal.events()[-1]["multipart"])
        self.assertFalse((self.root / ".lmau-upload-state").exists())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_transfer_session_does_not_double_count_part_body_reads(self):
        from requests.sessions import Session

        active = TransferSession(
            self.progress, None, config=self.session.config
        )
        try:
            with patch.object(
                Session,
                "send",
                new=lambda owner, request, **kw: self.transport(request, **kw),
            ):
                self.upload(session=active).run()
            self.progress.begin.assert_not_called()
            self.assertEqual(
                sum(
                    call.args[0]
                    for call in self.progress.advance.call_args_list
                ),
                self.size,
            )
        finally:
            active.close()

    def test_retry_resends_only_current_part_under_the_same_id(self):
        self.policy = RecoveryPolicy(1, 1, 0.001, 0.001)
        self.drop_part_ack = True
        self.upload().run()
        self.assertEqual(self.puts, [1, 1, 2])
        self.assertEqual(self.next_id, 1)
        events = self.upload().journal.events()
        failure = next(
            e for e in events if e["event"] == "resume_request_failed"
        )
        self.assertEqual(failure["operation"], "part 1")
        self.assertEqual(failure["error"], "ReadTimeout")

    def test_initiation_errors_are_logged_before_a_checkpoint_exists(self):
        from requests import ConnectionError

        self.policy = RecoveryPolicy(1, 1, 0.001, 0.001)
        with patch.object(
            self.session,
            "request",
            side_effect=ConnectionError("test-secret connection closed"),
        ):
            with self.assertRaises(ConnectionError):
                self.upload().run()
        events = self.upload().journal.events()
        errors = [e for e in events if e["event"] == "resume_request_failed"]
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0]["operation"], "initiate")
        self.assertNotIn("test-secret", errors[0]["detail"])
        self.assertIsNone(self.upload().journal.resume_state(self.path.name))

    def test_parallel_files_use_separate_ids_and_stream_parts(self):
        from requests.sessions import Session

        other = self.path.with_name("two.flac")
        with other.open("wb") as handle:
            handle.truncate(self.size)
        item = Item(
            self.session,
            "artist2026-08-17",
            {"metadata": {"identifier": "artist2026-08-17"}, "files": []},
        )
        journal = cli.Journal(self.root / "show.log", item.identifier)
        with patch.object(
            Session,
            "send",
            new=lambda owner, request, **kw: self.transport(request, **kw),
        ):
            cli.upload_files(
                self.session,
                item,
                [self.path, other],
                {},
                journal,
                0,
                0,
                recovery=self.policy,
                workers=2,
                multipart=multipart.MultipartOptions(True, 5),
            )
        self.assertEqual(self.next_id, 2)
        self.assertEqual(
            len([e for e in journal.events() if e["event"] == "upload_ok"]), 2
        )
        self.assertIsNone(journal.resume_state(self.path.name))
        self.assertIsNone(journal.resume_state(other.name))
        self.assertFalse((self.root / ".lmau-upload-state").exists())

    def test_existing_checkpoint_keeps_its_original_part_size(self):
        self.stop_part = 2
        with self.assertRaises(UploadCancelled):
            self.upload().run()
        self.stop_part = None
        self.upload(options=multipart.MultipartOptions(True, 6)).run()
        self.assertEqual(self.puts.count(1), 1)
        self.assertEqual(self.next_id, 1)

    def test_invalid_options_and_override_parsing(self):
        self.assertFalse(multipart.load_options({}).enabled)
        for data in (
            {"enabled": "true"},
            {"part_size_mib": True},
            {"part_size_mib": 4},
            {"unknown": 1},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                multipart.load_options(data)
        self.assertTrue(cli.parser().parse_args(["--resume"]).resume)
        self.assertFalse(cli.parser().parse_args(["--no-resume"]).resume)
