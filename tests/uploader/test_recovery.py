"""Lost acknowledgements must neither lose progress nor imply success."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from lma_tools.uploader import cli
from lma_tools.uploader.recovery import (
    RecoveryPolicy,
    RemoteConflict,
    load_policy,
    remote_state,
    retryable,
    upload_with_recovery,
)
from tests.uploader.test_lma import ID, make_show, response

POLICY = RecoveryPolicy(retries=1, checks=2, delay=1, max_delay=2)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "one.flac"
        self.path.write_bytes(b"audio")
        self.digest, self.size, self.stamp = cli.file_md5(self.path)
        self.journal = cli.Journal(self.root / "log", ID)
        self.session = Mock()
        self.session.send = Mock()
        self.session.get_metadata.return_value = {"files": []}
        self.head = Mock(status_code=404, headers={})
        self.session.head.return_value = self.head
        self.sleep = patch("lma_tools.uploader.recovery.time.sleep").start()
        self.addCleanup(patch.stopall)
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def entry(self, **kwargs):
        return dict(
            name=self.path.name, md5=self.digest, size=self.size, **kwargs
        )

    def recover(self, send, policy=POLICY):
        return upload_with_recovery(
            send,
            self.session,
            ID,
            self.path,
            self.digest,
            self.size,
            self.stamp,
            self.journal,
            policy,
        )

    def test_confirmed_timeout_advances_to_next_file_without_resending(self):
        other = self.root / "two.flac"
        other.write_bytes(b"second")
        item = Mock(identifier=ID, exists=True, files=[])
        item.upload_file.side_effect = [
            requests.ConnectionError(ReadTimeoutError(None, "url", "timeout")),
            response(),
        ]
        self.session.get_metadata.return_value = {"files": [self.entry()]}
        cli.upload_files(
            self.session,
            item,
            [self.path, other],
            {},
            self.journal,
            0,
            0,
            recovery=POLICY,
        )
        calls = item.upload_file.call_args_list
        self.assertEqual(
            [c.args[0] for c in calls], [str(self.path), str(other)]
        )
        successes = [
            e for e in self.journal.events() if e["event"] == "upload_ok"
        ]
        self.assertEqual(len(successes), 2)
        self.assertEqual(successes[0]["verified_by"], "metadata")
        self.assertNotIn("status", successes[0])
        self.assertTrue(
            self.journal.acknowledgement(
                self.path.name, self.digest, self.size
            )
        )

    def test_delayed_ingestion_waits_then_uses_head(self):
        self.session.get_metadata.side_effect = [
            {"files": []},
            requests.Timeout("metadata unavailable"),
        ]
        matched = Mock(status_code=200)
        matched.headers = {
            "ETag": f'"{self.digest}"',
            "Content-Length": self.size,
        }
        self.session.head.side_effect = [self.head, matched]
        send = Mock(side_effect=requests.ReadTimeout("response lost"))
        self.assertEqual(self.recover(send), {"verified_by": "HEAD"})
        send.assert_called_once()
        self.assertEqual(self.sleep.call_count, 2)
        matched.close.assert_called_once()

    def test_confirmed_absence_retries_same_file_then_succeeds(self):
        send = Mock(side_effect=[requests.Timeout("failed"), response()])
        self.assertEqual(self.recover(send), {"status": 200})
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn(
            "upload_retry", [e["event"] for e in self.journal.events()]
        )

    def test_unknown_remote_state_never_resends_or_claims_success(self):
        self.session.get_metadata.side_effect = requests.Timeout("offline")
        self.session.head.side_effect = requests.ConnectionError("offline")
        send = Mock(side_effect=requests.ReadTimeout("lost response"))
        with self.assertRaises(requests.ReadTimeout):
            self.recover(send)
        send.assert_called_once()
        self.assertEqual(self.sleep.call_count, 4)
        self.assertFalse(
            self.journal.acknowledgement(
                self.path.name, self.digest, self.size
            )
        )

    def test_retries_are_bounded_and_final_lost_response_is_checked(self):
        send = Mock(side_effect=requests.Timeout("lost"))
        self.session.get_metadata.side_effect = [
            {"files": []},
            {"files": []},
            {"files": [self.entry()]},
        ]
        self.assertEqual(self.recover(send), {"verified_by": "metadata"})
        self.assertEqual(send.call_count, 2)
        send.reset_mock()
        self.session.get_metadata.side_effect = None
        self.session.get_metadata.return_value = {"files": []}
        with self.assertRaises(requests.Timeout):
            self.recover(send)
        self.assertEqual(send.call_count, 2)

    def test_known_remote_conflict_stops_without_another_upload(self):
        self.session.get_metadata.return_value = {
            "files": [
                {"name": self.path.name, "md5": "0" * 32, "size": self.size}
            ]
        }
        send = Mock(side_effect=requests.Timeout("lost"))
        with self.assertRaises(RemoteConflict):
            self.recover(send)
        send.assert_called_once()

    def test_auth_errors_local_errors_and_missing_ack_do_not_retry(self):
        forbidden = requests.HTTPError(response=Mock(status_code=403))
        for error in (
            forbidden,
            OSError("disk error"),
            ValueError("bad data"),
        ):
            with self.subTest(error=type(error).__name__):
                self.sleep.reset_mock()
                with self.assertRaises(type(error)):
                    self.recover(Mock(side_effect=error))
                self.sleep.assert_not_called()
        with self.assertRaisesRegex(ValueError, "no HTTP acknowledgement"):
            self.recover(Mock(return_value=None))

    def test_local_change_or_interrupt_during_wait_stops(self):
        send = Mock(side_effect=requests.Timeout("lost"))
        self.sleep.side_effect = lambda delay: self.path.write_bytes(
            b"changed"
        )
        with self.assertRaisesRegex(ValueError, "File changed"):
            self.recover(send)
        self.session.get_metadata.assert_not_called()
        self.path.write_bytes(b"audio")
        self.stamp = self.path.stat().st_mtime_ns
        self.sleep.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.recover(send)

    def test_disabled_recovery_and_timeout_classification(self):
        with self.assertRaises(requests.Timeout):
            self.recover(
                Mock(side_effect=requests.Timeout()), RecoveryPolicy(0)
            )
        self.sleep.assert_not_called()
        self.assertTrue(retryable(ReadTimeoutError(None, "url", "lost")))
        self.assertTrue(retryable(ConnectionResetError("reset")))
        self.assertFalse(
            retryable(requests.exceptions.SSLError("certificate"))
        )
        self.assertTrue(
            retryable(requests.HTTPError(response=Mock(status_code=503)))
        )

    def test_remote_size_and_checksum_are_both_required(self):
        self.session.get_metadata.return_value = {
            "files": [{"name": self.path.name, "md5": self.digest}]
        }
        self.head.status_code = 200
        self.head.headers = {"Content-Length": self.size}
        self.assertEqual(
            remote_state(
                self.session, ID, self.path.name, self.digest, self.size
            )[0],
            "unknown",
        )
        self.head.headers["ETag"] = '"multipart-etag-2"'
        self.assertEqual(
            remote_state(
                self.session, ID, self.path.name, self.digest, self.size
            )[0],
            "unknown",
        )
        self.head.headers["ETag"] = f'"{self.digest}"'
        self.assertEqual(
            remote_state(
                self.session, ID, self.path.name, self.digest, self.size
            )[0],
            "match",
        )

    def test_head_preflight_skips_lost_old_upload_without_success_log(self):
        item = Mock(identifier=ID, exists=True, files=[])
        self.head.status_code = 200
        self.head.headers = {
            "ETag": f'"{self.digest}"',
            "Content-Length": str(self.size),
        }
        with redirect_stderr(self.output):
            cli.upload_files(
                self.session,
                item,
                [self.path],
                {},
                self.journal,
                0,
                0,
                recovery=POLICY,
            )
        item.upload_file.assert_not_called()
        self.assertIn("Skipped: remote HEAD", self.output.getvalue())

    def test_head_preflight_conflict_is_not_overridden_by_trust_log(self):
        item = Mock(identifier=ID, exists=True, files=[])
        self.head.status_code = 200
        self.head.headers = {"ETag": '"' + "0" * 32 + '"', "Content-Length": 5}
        self.journal.append(
            "upload_ok", name=self.path.name, md5=self.digest, size=self.size
        )
        with self.assertRaises(RemoteConflict):
            cli.upload_files(
                self.session,
                item,
                [self.path],
                {},
                self.journal,
                0,
                0,
                trust_log=True,
                recovery=POLICY,
            )
        item.upload_file.assert_not_called()

    def test_forced_stale_remote_entry_can_be_replaced_after_failure(self):
        self.session.get_metadata.return_value = {
            "files": [{"name": self.path.name, "md5": "0" * 32, "size": 1}]
        }
        self.head.status_code = 200
        self.head.headers = {"ETag": "0" * 32, "Content-Length": 1}
        send = Mock(side_effect=[requests.Timeout("lost"), response()])
        result = upload_with_recovery(
            send,
            self.session,
            ID,
            self.path,
            self.digest,
            self.size,
            self.stamp,
            self.journal,
            POLICY,
            force=True,
        )
        self.assertEqual(result, {"status": 200})
        self.assertEqual(send.call_count, 2)

    def test_recovery_does_not_depend_on_logging(self):
        self.journal.enabled = False
        self.session.get_metadata.return_value = {"files": [self.entry()]}
        result = self.recover(Mock(side_effect=requests.Timeout("lost")))
        self.assertEqual(result, {"verified_by": "metadata"})
        self.assertFalse(self.journal.path.exists())

    def test_upload_only_recovers_without_metadata_or_runtime_writes(self):
        folder = make_show(self.root)
        path = folder / "one.flac"
        digest, size, _ = cli.file_md5(path)
        config = self.root / "config.toml"
        config.write_text(
            f'[uploader]\nshows_root = "{self.root}"\nupload_workers = 1\n'
            "[uploader.recovery]\nretries = 1\nchecks = 1\n"
            "delay = 1.0\nmax_delay = 2.0\n"
        )
        item = Mock(identifier=ID, exists=True, files=[])

        def send(filename, **kwargs):
            if filename.endswith(".flac"):
                raise requests.ReadTimeout("lost")
            return response()

        item.upload_file.side_effect = send
        self.session.access_key = "test"
        self.session.secret_key = "test"
        self.session.get_item.return_value = item
        self.session.get_metadata.return_value = {
            "files": [{"name": path.name, "md5": digest, "size": size}]
        }
        self.session.submit_task.return_value = response()
        with (
            patch.object(cli, "ArchiveSession", return_value=self.session),
            patch.object(cli, "audio_runtime", side_effect=AssertionError),
            patch("builtins.input", return_value="UPLOAD"),
        ):
            self.assertEqual(cli.main(["-u", "--config", str(config), ID]), 0)
        item.modify_metadata.assert_not_called()
        self.assertEqual(item.upload_file.call_count, 3)
        self.assertEqual(item.upload_file.call_args.kwargs["metadata"], {})
        self.session.submit_task.assert_called_once()

    def test_policy_validation(self):
        self.assertEqual(load_policy({}), RecoveryPolicy())
        for data in (
            {"retries": -1},
            {"retries": True},
            {"checks": 0},
            {"delay": 0},
            {"delay": float("nan")},
            {"max_delay": 1},
            {"unexpected": 1},
            [],
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_policy(data)
