"""Reference FLAC encoding and streaming decoded-PCM verification.

Runtime dependencies: reference flac CLI, ffmpeg and ffprobe on PATH.
No compression-level shortcut is used: all predictive and constant
subframes are disabled explicitly. Callers own transactional staging.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from mutagen import File
from mutagen.flac import FLAC, Picture


def pcm_digest(path):
    """Hash signed 32-bit interleaved PCM without buffering a whole show."""
    command = [
        "ffmpeg",
        "-nostdin",
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
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
    ) as process:
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


def encode_verbatim(source, destination, *, progress=None, verify=True):
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
        if progress is None:
            subprocess.run(command, check=True, capture_output=True)
        else:
            _encode_progress(command, source, progress)
        if verify:
            verify_pcm(source, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _encode_progress(command, source, progress):
    """Stream WAV input to FLAC; report bytes fed and confirmed completion."""
    command = [*command[:-1], "-"]
    size = source.stat().st_size
    # An anonymous file avoids a full stderr pipe blocking encoder input.
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        ) as process,
    ):
        try:
            sent = 0
            with source.open("rb") as stream:
                try:
                    while chunk := stream.read(1024 * 1024):
                        process.stdin.write(chunk)
                        sent += len(chunk)
                        progress(min(99, sent * 100 / max(1, size)))
                except BrokenPipeError:
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
            if process.wait():
                errors.seek(max(0, errors.tell() - 8192))
                raise subprocess.CalledProcessError(
                    process.returncode, command, stderr=errors.read()
                )
            progress(100)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise


MANAGED_TAGS = {
    "artist",
    "album",
    "genre",
    "venue",
    "date",
    "title",
    "tracknumber",
    "discnumber",
}
LEGACY_TOTALS = {"totaltracks", "totaldiscs", "tracktotal", "disctotal"}


def number_tag(value, total):
    """Keep an approved number/total instead of recomputing its denominator."""
    return value if isinstance(value, str) else f"{value}/{total}"


def expected_tags(common, track, totaltracks, totaldiscs):
    """Managed fields plus explicit common/per-track custom tag overrides."""
    expected = {
        key: [common[key][:4] if key == "date" else common[key]]
        for key in ("artist", "album", "venue", "date")
    }
    expected.update(
        genre=[common["genre"]] if common.get("genre", "").strip() else [],
        title=[track["title"]],
        tracknumber=[number_tag(track["track"], totaltracks)],
        discnumber=[
            number_tag(track["disc"], common.get("disc_total", totaldiscs))
        ],
    )
    for tags in (common.get("tags", {}), track.get("tags", {})):
        for key, value in tags.items():
            key = key.lower()
            if key in MANAGED_TAGS | LEGACY_TOTALS:
                raise ValueError("Use the preview field for managed tags.")
            expected[key] = (
                [value]
                if isinstance(value, str) and value
                else []
                if isinstance(value, str)
                else value
            )
    return expected


def tag_audio(path, common, track, totaltracks, totaldiscs, artwork=None):
    """Update approved fields, preserving unmanaged FLAC comments."""
    audio = FLAC(path)
    for key in LEGACY_TOTALS:
        audio.pop(key, None)
    for key, values in expected_tags(
        common, track, totaltracks, totaldiscs
    ).items():
        if values:
            audio["Title" if key == "title" else key.upper()] = values
        else:
            audio.pop(key, None)
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
    if any(key in audio for key in LEGACY_TOTALS):
        return False
    for key, values in expected_tags(
        common, track, totaltracks, totaldiscs
    ).items():
        if audio.get(key, []) != values:
            return False
    if not artwork:
        return not audio.pictures
    return (
        len(audio.pictures) == 1
        and audio.pictures[0].type == 3
        and audio.pictures[0].mime == "image/jpeg"
        and audio.pictures[0].data == Path(artwork).read_bytes()
    )
