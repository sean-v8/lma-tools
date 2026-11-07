"""Reference FLAC encoding and streaming decoded-PCM verification.

Runtime dependencies: reference flac CLI, ffmpeg and ffprobe on PATH.
No compression-level shortcut is used: all predictive and constant
subframes are disabled explicitly. Callers own transactional staging.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from mutagen import File
from mutagen.flac import FLAC, Picture


def pcm_digest(path):
    """Hash signed 32-bit interleaved PCM without buffering a whole show."""
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        "s32le",
        "-acodec",
        "pcm_s32le",
        "-",
    ]
    digest = hashlib.sha256()
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        while chunk := process.stdout.read(1024 * 1024):
            digest.update(chunk)
        if process.wait():
            raise ValueError(f"PCM decoding failed: {path}")
    return digest.hexdigest()


def verify_pcm(source, destination):
    """Raise before callers may remove the original audio."""
    original, converted = File(source), File(destination)
    if (
        original is None
        or converted is None
        or any(
            getattr(original.info, field) != getattr(converted.info, field)
            for field in ("sample_rate", "channels", "bits_per_sample")
        )
    ):
        raise ValueError("Audio rate, channels or bit depth differs.")
    if pcm_digest(source) != pcm_digest(destination):
        raise ValueError("Decoded PCM differs from source.")


def encode_verbatim(source, destination):
    """Encode with reference FLAC; never overwrite or delete the source."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    command = [
        "flac",
        "--silent",
        "--verify",
        "--no-padding",
        "--disable-constant-subframes",
        "--disable-fixed-subframes",
        "-l0",
        "--no-mid-side",
        "-o",
        str(destination),
        str(source),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
        verify_pcm(source, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def tag_audio(path, common, track, totaltracks, totaldiscs, artwork=None):
    """Replace tags and artwork, retaining only the approved sample fields."""
    audio = FLAC(path)
    audio.clear()
    for key in ("artist", "album", "genre", "venue", "date"):
        if key != "genre" or common.get("genre", "").strip():
            value = common[key][:4] if key == "date" else common[key]
            audio[key] = [value]
    audio["title"] = [track["title"]]
    audio["tracknumber"] = [f"{track['track']}/{totaltracks}"]
    audio["discnumber"] = [f"{track['disc']}/{totaldiscs}"]
    audio.clear_pictures()
    if artwork:
        picture = Picture()
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.data = Path(artwork).read_bytes()
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(artwork),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        picture.width, picture.height = stream["width"], stream["height"]
        picture.depth = 24
        audio.add_picture(picture)
    audio.save(padding=lambda _: 1024)


def tags_match(path, common, track, totaltracks, totaldiscs, artwork=None):
    """Compare approved tags and single artwork without changing the file."""
    audio = FLAC(path)
    expected = {
        key: [common[key][:4] if key == "date" else common[key]]
        for key in ("artist", "album", "genre", "venue", "date")
        if key != "genre" or common.get("genre", "").strip()
    }
    expected.update(
        {
            "title": [track["title"]],
            "tracknumber": [f"{track['track']}/{totaltracks}"],
            "discnumber": [f"{track['disc']}/{totaldiscs}"],
        }
    )
    if dict(audio.tags or {}) != expected:
        return False
    if not artwork:
        return not audio.pictures
    return (
        len(audio.pictures) == 1
        and audio.pictures[0].type == 3
        and audio.pictures[0].mime == "image/jpeg"
        and audio.pictures[0].data == Path(artwork).read_bytes()
    )
