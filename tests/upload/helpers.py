"""Shared upload fixtures; no test modules are imported here."""

import io
import subprocess
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from mutagen.flac import FLAC
from requests import Response

from lma_tools.upload import cli as lma
from lma_tools.upload.encoder import tag_audio

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


@contextmanager
def metadata_fixture(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = make_show(root)
        config = root / "config.toml"
        config.write_text(
            f'[upload]\nshows_root = "{root}"\nupload_workers = 1\n'
            '[upload.prefix]\nartist = "ExampleArtistCollection"\n'
        )
        metadata = lma.parse_notes(NOTES, ID)
        metadata.update(
            collection=["ExampleArtistCollection", "etree"],
            uploader="taper@example.com",
        )
        item = Mock(exists=True, identifier=ID, files=[], metadata=metadata)
        item.modify_metadata.return_value = response()
        item.upload_file.return_value = response()
        session = Mock(access_key="test", secret_key="test")
        session.send = Mock()
        session.get_item.side_effect = [
            item,
            Mock(exists=True, metadata={"mediatype": "collection"}),
        ]
        session.submit_task.return_value = response()
        args = ["--config", str(config), ID]
        with (
            patch("lma_tools.upload.prepare.prepare_show", return_value={}),
            patch.object(lma, "audio_runtime", return_value="2:05:50"),
            patch.object(
                lma, "edit_review", side_effect=lambda i, m, **kw: (i, m)
            ),
            patch.object(lma, "ArchiveSession", return_value=session),
            patch("builtins.input", return_value="UPLOAD"),
            redirect_stdout(io.StringIO()),
        ):
            yield folder, item, session, args


def set_up_tagged_show(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.show = self.root / "artist2026-11-07"
    self.show.mkdir()
    self.path = self.show / "artist2026-11-07s1t01.flac"
    self.notes = self.show / "notes.txt"
    self.notes.write_text(
        "Artist\nNovember 07, 2026\nVenue\nCity\n\nSet 1\n01. Song\n"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "0.01",
            str(self.path),
        ],
        check=True,
        capture_output=True,
    )
    self.common = dict(
        artist="Artist",
        album="Album",
        genre="Jazz",
        venue="Venue",
        date="2026-11-07",
    )
    self.track = dict(title="Song", track=1, disc="1/2")
    tag_audio(self.path, self.common, self.track, 1, 1)
    audio = FLAC(self.path)
    audio["Composer"] = ["Composer A", "Composer B"]
    audio["COMMENT"] = ["Keep this comment"]
    audio["BPM"] = ["120"]
    audio.save()
