import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lma_tools.uploader import cli

ID = "artist2026-08-17"


def state(name, upload_id):
    return {
        "signature": {
            "identifier": ID,
            "name": name,
            "md5": "a" * 32,
            "size": 100,
        },
        "upload_id": upload_id,
        "part_bytes": 16 * 1024 * 1024,
        "parts": {},
        "phase": "uploading",
        "version": 1,
    }


class ResumeLogTests(unittest.TestCase):
    def test_parallel_delta_events_replay_in_the_existing_private_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "show_lma.log"
            path.write_text("existing log preamble\n")
            path.chmod(0o644)
            journal = cli.Journal(path, ID)

            def record(name):
                checkpoint = state(name, name + "-upload")
                journal.save_resume(name, checkpoint)
                for number in range(1, 13):
                    checkpoint["parts"][str(number)] = hashlib.md5(
                        str(number).encode()
                    ).hexdigest()
                    journal.save_resume(name, checkpoint)
                checkpoint["phase"] = "completing"
                journal.save_resume(name, checkpoint)

            with ThreadPoolExecutor(3) as pool:
                list(pool.map(record, ["one.flac", "two.flac", "three.flac"]))
            restored = cli.Journal(path, ID)
            for name in ("one.flac", "two.flac", "three.flac"):
                checkpoint = restored.resume_state(name)
                self.assertEqual(len(checkpoint["parts"]), 12)
                self.assertEqual(checkpoint["phase"], "completing")
            events = restored.events()
            self.assertEqual(
                sum(e["event"] == "resume_part" for e in events), 36
            )
            starts = [e for e in events if e["event"] == "resume_start"]
            self.assertEqual(len(starts), 3)
            self.assertTrue(all(not e["state"]["parts"] for e in starts))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_torn_writes_and_old_attempt_clear_do_not_lose_resume_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "show_lma.log"
            first = cli.Journal(path, ID)
            checkpoint = state("one.flac", "upload-1")
            first.save_resume("one.flac", checkpoint)
            checkpoint["parts"]["1"] = "b" * 32
            first.save_resume("one.flac", checkpoint)
            with path.open("ab") as handle:
                handle.write(b'\n{"event":"resume_part","identifier":')
                handle.write(
                    b'\n{"event":"resume_part","identifier":"\xe2\x80'
                )
            second = cli.Journal(path, ID)
            restored = second.resume_state("one.flac")
            self.assertEqual(restored["parts"], {"1": "b" * 32})
            restored["parts"]["2"] = "c" * 32
            second.save_resume("one.flac", restored)
            self.assertEqual(len(first.resume_state("one.flac")["parts"]), 2)
            second.save_resume("one.flac", state("one.flac", "upload-2"))
            first.clear_resume("one.flac", "upload-1")
            self.assertEqual(
                first.resume_state("one.flac")["upload_id"], "upload-2"
            )
            first.clear_resume("one.flac", "upload-2")
            self.assertIsNone(cli.Journal(path, ID).resume_state("one.flac"))
            records = [
                json.loads(line)
                for line in path.read_bytes().splitlines()
                if line.startswith(b'{"time":')
            ]
            self.assertEqual(records[-1]["event"], "resume_clear")

    def test_resume_requires_logging_before_any_network_or_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            for logging, flags in ((False, []), (True, ["--no-log"])):
                config.write_text(
                    f'[uploader]\nshows_root = "{tmp}"\n'
                    f"log = {str(logging).lower()}\n"
                )
                with (
                    self.subTest(logging=logging),
                    patch.object(cli, "ArchiveSession") as session,
                    patch.object(cli, "edit_review") as editor,
                ):
                    with self.assertRaisesRegex(ValueError, "requires upload"):
                        cli.main(
                            ["--config", str(config), "--resume", ID, *flags]
                        )
                    session.assert_not_called()
                    editor.assert_not_called()
            config.write_text(
                f'[uploader]\nshows_root = "{tmp}"\nlog = false\n'
                "[uploader.resume]\nenabled = true\n"
            )
            self.assertEqual(
                cli.main(["--config", str(config), "--no-resume", "--list"]), 0
            )
