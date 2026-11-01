import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests import HTTPError, Response

from lma_tools.uploader import cli as lma
from lma_tools.uploader import core as lma_core
from lma_tools.uploader.core import validate_fingerprints

ID = "artist2026-08-17.audience.t-flac2448"
PROJECT = "artist2026-08-17.audience.multitrack.t-flac2448"
NOTES = """Artist
August 17, 2026
Venue
Example City, ST

Microphone pair A (12' high, 15' from stage) > Recorder A
Microphone pair B (stage lip, 10' split) > Recorder B

> WAV (32/48) > Audacity > WAV (24/48) > FLAC
Example Taper <taper@example.com>

Set 1
01. Song One >
02. Song Two
"""


def response(status=200, content=b"{}"):
    r = Response()
    r.status_code = status
    r._content = content
    return r


def make_show(root, project=PROJECT, identifier=ID):
    folder = root / project / identifier
    folder.mkdir(parents=True)
    (folder / (identifier + ".txt")).write_text(NOTES)
    (folder / "one.flac").write_bytes(b"audio-one")
    (folder / "cover.jpg").write_bytes(b"picture")
    return folder


class MetadataTests(unittest.TestCase):
    def test_exact_metadata_and_multiline_roundtrip(self):
        md = lma.parse_notes(NOTES, ID)
        self.assertEqual(md["title"], "Artist Live at Venue on 2026-08-17")
        self.assertEqual(
            md["source"],
            "[Microphone pair A (12' high, 15' from stage) > Recorder A] + "
            "[Microphone pair B (stage lip, 10' split) > Recorder B]",
        )
        self.assertEqual(
            md["lineage"], "WAV (32/48) > Audacity > WAV (24/48) > FLAC"
        )
        self.assertEqual(md["description"], NOTES.rstrip("\n"))
        self.assertNotIn("setlist", md)
        self.assertNotIn(".jpeg", lma_core.SMALL)
        md.update(collection="ExampleArtistCollection", runtime="1:23:45")
        self.assertEqual(lma.parse_review(lma.review_text(ID, md)), (ID, md))
        md["description"] = "a ''' b\nUnicode ’"
        self.assertEqual(lma.parse_review(lma.review_text(ID, md))[1], md)

    def test_bad_dates(self):
        with self.assertRaisesRegex(ValueError, "disagree"):
            lma.parse_notes(NOTES, ID.replace("2026", "2027"))

    def test_partial_ffp_is_only_permitted_in_upload_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            track = p / "present.flac"
            track.write_bytes(b"fake")
            (p / "x.ffp").write_text(
                "present.flac:"
                + "a" * 32
                + "\nmissing.flac:"
                + "b" * 32
                + "\n"
            )
            with self.assertRaisesRegex(ValueError, "Missing"):
                validate_fingerprints(p, [track])
            with patch(
                "lma_tools.uploader.core.FLAC",
                return_value=Mock(info=Mock(md5_signature=int("a" * 32, 16))),
            ):
                validate_fingerprints(p, [track], allow_missing=True)

    def test_runtime_refuses_missing_remote_tracks(self):
        item = Mock(
            files=[
                {"name": "one.flac", "source": "original"},
                {"name": "two.flac", "source": "original"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "two.flac"):
            lma.ensure_complete_runtime(item, [Path("one.flac")])

    def test_runtime_half_up(self):
        with patch(
            "lma_tools.uploader.core.FLAC",
            return_value=Mock(
                info=Mock(total_samples=60000, sample_rate=48000)
            ),
        ):
            self.assertEqual(lma.audio_runtime(["a", "b"]), "0:00:03")


class DiscoveryTests(unittest.TestCase):
    def test_multiple_outputs_different_names_and_duplicate_disambiguation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = make_show(root)
            other = make_show(root, identifier=ID + ".mix2")
            (one.parent / "raw.wav").write_bytes(b"raw")
            nested = one.parent / "raw" / "nested2026-08-17"
            nested.mkdir(parents=True)
            (nested / "x.flac").write_bytes(b"x")
            (nested / "x.txt").write_bytes(b"x")
            outputs = lma.scan_outputs(root)
            self.assertEqual({p for _, p in outputs}, {one, other})
            self.assertEqual(lma.resolve_show(ID, root), (one, one.parent))
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                lma.resolve_show(PROJECT, root)
            duplicate = make_show(root, project=PROJECT + ".duplicate")
            tokens = lma.output_tokens(lma.scan_outputs(root), root)
            self.assertIn(str(one.relative_to(root)), tokens)
            self.assertIn(str(duplicate.relative_to(root)), tokens)
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                lma.resolve_show(ID, root)

    def test_explicit_project_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = make_show(root)
            self.assertEqual(
                lma.resolve_show(str(one), root), (one, one.parent)
            )
            self.assertEqual(
                lma.resolve_show(str(one.parent), root), (one, one.parent)
            )

    def test_config_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            with self.assertRaisesRegex(ValueError, "Create"):
                lma.load_config(p)
            p.write_text('shows_root = "relative"')
            with self.assertRaisesRegex(ValueError, "absolute"):
                lma.load_config(p)


class ResumeTests(unittest.TestCase):
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


class WorkflowTests(unittest.TestCase):
    def setup_show(self, root):
        folder = make_show(root)
        config = root / "config.toml"
        config.write_text(
            f'[uploader]\nshows_root = "{root}"\nupload_workers = 1\n'
            '[uploader.collections]\nartist = "ExampleArtistCollection"\n'
        )
        return folder, ["--config", str(config), ID]

    def test_preview_and_completion_write_nothing_and_need_no_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            before = {
                p: p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            with (
                patch.object(
                    lma,
                    "ArchiveSession",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch.object(lma, "audio_runtime", return_value="1:00:00"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["--preview"]), 0)
                self.assertEqual(
                    lma.main(
                        [
                            "--config",
                            str(root / "config.toml"),
                            "--complete",
                            "art",
                        ]
                    ),
                    0,
                )
            after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((folder / ".lma-upload.toml").exists())

    def test_cancel_creates_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            with (
                patch.object(lma, "audio_runtime", return_value="1:00:00"),
                patch.object(
                    lma, "edit_review", side_effect=lambda i, m: (i, m)
                ),
                patch("builtins.input", return_value="no"),
                patch.object(
                    lma,
                    "ArchiveSession",
                    side_effect=AssertionError("network forbidden"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args), 0)
            self.assertEqual(list(root.rglob("*.log")), [])

    def test_upload_only_never_computes_or_updates_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            item = Mock(exists=True, identifier=ID, files=[])
            item.upload_file.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.get_item.return_value = item
            session.submit_task.return_value = response()
            with (
                patch.object(
                    lma,
                    "audio_runtime",
                    side_effect=AssertionError("runtime forbidden"),
                ),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["-u"]), 0)
            item.modify_metadata.assert_not_called()
            self.assertEqual(
                [c.kwargs["key"] for c in item.upload_file.call_args_list],
                ["one.flac"],
            )
            self.assertEqual(list(folder.glob("*.log")), [])
            self.assertEqual(len(list(folder.parent.glob("*_lma.log"))), 1)

    def test_runtime_only_never_uploads_or_derives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            item = Mock(exists=True, identifier=ID, files=[])
            item.modify_metadata.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.get_item.return_value = item
            with (
                patch.object(lma, "audio_runtime", return_value="2:05:50"),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args + ["-r"]), 0)
            item.modify_metadata.assert_called_once_with(
                {"runtime": "2:05:50"}, timeout=60
            )
            item.upload_file.assert_not_called()
            session.submit_task.assert_not_called()

    def test_creation_upload_order_and_single_derive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, args = self.setup_show(root)
            item = Mock(exists=False, identifier=ID, files=[])
            item.upload_file.return_value = response()
            session = Mock(access_key="test", secret_key="test")
            session.get_item.side_effect = [
                item,
                Mock(exists=True, metadata={"mediatype": "collection"}),
            ]
            session.submit_task.return_value = response()
            with (
                patch.object(lma, "audio_runtime", return_value="2:05:50"),
                patch.object(
                    lma, "edit_review", side_effect=lambda i, m: (i, m)
                ),
                patch.object(lma, "ArchiveSession", return_value=session),
                patch("builtins.input", return_value="UPLOAD"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lma.main(args), 0)
            calls = item.upload_file.call_args_list
            self.assertEqual(
                [c.kwargs["key"] for c in calls],
                [ID + ".txt", "cover.jpg", "one.flac"],
            )
            self.assertEqual(calls[0].kwargs["metadata"]["runtime"], "2:05:50")
            self.assertNotIn("setlist", calls[0].kwargs["metadata"])
            self.assertTrue(all(not c.kwargs["queue_derive"] for c in calls))
            session.submit_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
