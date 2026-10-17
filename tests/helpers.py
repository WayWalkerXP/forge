"""Shared fixtures for the forge test suite.

Audio fixtures are synthesised with ffmpeg rather than committed: a handful of
real m4b files would dwarf the source tree, and generated noise encodes at
whatever bitrate is asked for, which is exactly what the rules need to be
exercised against.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_CACHE: dict[tuple, Path] = {}
_CACHE_DIR: Path | None = None


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


requires_ffmpeg = unittest.skipUnless(ffmpeg_available(), "ffmpeg and ffprobe are required")


def cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.mkdtemp(prefix="forge-test-fixtures-"))
    return _CACHE_DIR


def cleanup() -> None:
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        shutil.rmtree(_CACHE_DIR, ignore_errors=True)
        _CACHE_DIR = None
        _CACHE.clear()


def make_audio(
    *,
    seconds: int = 5,
    channels: int = 2,
    bitrate_kbps: int = 128,
    suffix: str = ".m4b",
    chapters: int = 0,
) -> Path:
    """Synthesise an audio file, cached so repeated requests are free."""

    key = (seconds, channels, bitrate_kbps, suffix, chapters)
    if key in _CACHE and _CACHE[key].exists():
        return _CACHE[key]

    name = f"a{seconds}s-{channels}ch-{bitrate_kbps}k-{chapters}chap{suffix}"
    target = cache_dir() / name

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=44100:a=0.5",
    ]

    if chapters:
        meta = cache_dir() / f"{name}.meta.txt"
        step_ms = max(1, (seconds * 1000) // chapters)
        lines = [";FFMETADATA1"]
        for index in range(chapters):
            lines += [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={index * step_ms}",
                f"END={(index + 1) * step_ms}",
                f"title=Chapter {index + 1}",
            ]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command += ["-i", str(meta), "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    else:
        command += ["-map", "0:a"]

    command += ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels), "-f", "mp4"]
    command.append(str(target))

    subprocess.run(command, check=True, capture_output=True)
    _CACHE[key] = target
    return target


#: MPEG-1 Layer III frame headers, keyed by (bitrate_kbps, channels), with the
#: frame size each implies at 44100 Hz.  Built by hand because not every ffmpeg
#: build ships an MP3 *encoder* - this one does not - and the ID3 paths still
#: need testing.  The frames carry silence, which is all a tag test needs.
_MP3_FRAMES = {
    (128, 2): (b"\xff\xfb\x90\x00", 417),
    (128, 1): (b"\xff\xfb\x90\xc0", 417),
}


def make_mp3(*, seconds: float = 2.0, bitrate_kbps: int = 128, channels: int = 2) -> Path:
    """Write a synthetic MP3 of silent frames.

    ffprobe reports the real codec, bitrate, channel count and duration, and
    mutagen reads and writes ID3 tags on it, which is everything the tests need.
    """

    try:
        header, frame_size = _MP3_FRAMES[(bitrate_kbps, channels)]
    except KeyError:
        raise unittest.SkipTest(
            f"no hand-built MP3 frame for {bitrate_kbps}k/{channels}ch"
        ) from None

    key = ("mp3", seconds, bitrate_kbps, channels)
    if key in _CACHE and _CACHE[key].exists():
        return _CACHE[key]

    frame = header + b"\x00" * (frame_size - len(header))
    frames_per_second = bitrate_kbps * 1000 / 8 / frame_size
    count = max(8, int(round(seconds * frames_per_second)))

    target = cache_dir() / f"m{seconds}s-{channels}ch-{bitrate_kbps}k.mp3"
    target.write_bytes(frame * count)
    _CACHE[key] = target
    return target


def copy_mp3(destination: Path, **kwargs) -> Path:
    source = make_mp3(**kwargs)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def copy_audio(destination: Path, **kwargs) -> Path:
    """Copy a synthesised fixture to *destination*."""

    source = make_audio(**kwargs)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def write_config(root: Path, **overrides: object) -> Path:
    """Write a valid config.ini under *root* and return its path."""

    settings = {
        "source_dir": root / "src",
        "output_dir": root / "out",
        "archive_dir": root / "arch",
        "state_dir": root / "state",
        "max_bitrate": 64,
        "min_age_seconds": 0,
        "settle_seconds": 0,
    }
    settings.update(overrides)

    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.ini"
    # Only written when asked for: a library directory makes `shelve` possible,
    # and most tests want a config without one.
    optional = "".join(
        f"{key} = {settings[key]}\n"
        for key in ("library_dir", "duplicates_dir")
        if settings.get(key) is not None
    )
    # Omitted unless a test is about naming, so everything else exercises the
    # defaults, which are what an existing install runs with.
    templates = "\n            ".join(
        f"{key} = {settings[key]}"
        for key in ("shelf_template", "filename_template")
        if settings.get(key) is not None
    )
    path.write_text(
        textwrap.dedent(
            f"""
            [paths]
            source_dir = {settings['source_dir']}
            output_dir = {settings['output_dir']}
            archive_dir = {settings['archive_dir']}
            {optional}

            [naming]
            {templates}

            [encoding]
            max_bitrate = {settings['max_bitrate']}

            [general]
            state_dir = {settings['state_dir']}

            [safety]
            min_age_seconds = {settings['min_age_seconds']}
            settle_seconds = {settings['settle_seconds']}
            """
        ).strip()
        + "\n"
    )
    return path


class TempCaseMixin(unittest.TestCase):
    """A test case with its own temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="forge-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        # No test may inherit real Audiobookshelf credentials: a suite that
        # quietly talks to the live server is worse than one that fails.
        patch = mock.patch.dict(
            os.environ,
            {"FORGE_ENV": str(self.root / "absent.env")},
            clear=False,
        )
        patch.start()
        self.addCleanup(patch.stop)
        # Popped after start(), so patch.dict puts them back afterwards.
        for name in ("AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_API_KEY", "AUDIOBOOKSHELF_LIBRARY_ID"):
            os.environ.pop(name, None)
