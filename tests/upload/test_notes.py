"""Upload metadata regression coverage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload import cli as lma
from lma_tools.upload import core as lma_core
from lma_tools.upload.core import validate_fingerprints
from tests.upload.helpers import ID, NOTES


class MetadataTests(unittest.TestCase):
    def test_exact_metadata_and_multiline_roundtrip(self):
        md = lma.parse_notes(NOTES, ID)
        self.assertEqual(md["year"], "2026")
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
                "lma_tools.upload.core.FLAC",
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
