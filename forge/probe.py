"""Inspecting audio files with ffprobe.

:class:`AudioInfo` is the factual view of a file - what is actually in the
container.  Nothing here makes decisions; :mod:`forge.rules` does that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ProbeError
from .ffmpeg import run

LOGGER = logging.getLogger(__name__)

#: Tags worth carrying for naming and dramatic-audio detection.
_INTERESTING_TAGS = (
    "title",
    "album",
    "artist",
    "album_artist",
    "author",
    "composer",
    "genre",
    "narrator",
    "series",
)


@dataclass(frozen=True)
class ArtworkStream:
    """An embedded cover image stream."""

    index: int
    extension: str
    is_front_cover: bool


@dataclass(frozen=True)
class AudioInfo:
    """What ffprobe reports about one audio file."""

    path: Path
    bitrate_bps: int
    channels: int
    codec: str
    duration_seconds: float
    chapter_count: int
    size_bytes: int
    format_name: str = ""
    sample_rate: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    artwork: ArtworkStream | None = None

    @property
    def bitrate_kbps(self) -> int:
        return round(self.bitrate_bps / 1000)

    @property
    def normalized_bitrate_kbps(self) -> int:
        """The bitrate rounded to the nearest 8 kbps.

        Encoders rarely hit a round number, so comparing a raw 62 kbps or
        65 kbps against a 64 kbps threshold gives inconsistent answers.
        Bucketing first makes the rules reproducible.
        """

        return round(self.bitrate_kbps / 8) * 8

    @property
    def is_mono(self) -> bool:
        return self.channels == 1

    def estimated_output_bytes(self, target_bitrate_kbps: int) -> int:
        """Roughly how large an output at *target_bitrate_kbps* will be."""

        return int(self.duration_seconds * target_bitrate_kbps * 1000 / 8)


def probe(path: Path | str) -> AudioInfo:
    """Inspect *path* with ffprobe.

    Raises :class:`ProbeError` when the file is not readable media or when a
    field the rules depend on (bitrate, channels, duration) cannot be
    determined.  Refusing to guess here is what keeps a damaged file out of the
    conversion path.
    """

    path = Path(path)
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ]
    )
    if not result.ok:
        raise ProbeError(result.stderr.strip() or f"ffprobe could not read {path.name}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path.name}") from exc

    stream = _primary_audio_stream(data)
    if stream is None:
        raise ProbeError(f"{path.name} contains no audio stream")

    fmt = data.get("format", {}) or {}

    bitrate = _first_int(stream.get("bit_rate"), fmt.get("bit_rate"))
    if not bitrate or bitrate <= 0:
        bitrate = _derive_bitrate(fmt)
    if not bitrate or bitrate <= 0:
        raise ProbeError(f"could not determine the bitrate of {path.name}")

    channels = _first_int(stream.get("channels"))
    if not channels or channels <= 0:
        raise ProbeError(f"could not determine the channel count of {path.name}")

    duration = _first_float(stream.get("duration"), fmt.get("duration"))
    if not duration or duration <= 0:
        raise ProbeError(f"could not determine the duration of {path.name}")

    try:
        size_bytes = int(fmt.get("size") or path.stat().st_size)
    except (OSError, TypeError, ValueError):
        size_bytes = 0

    return AudioInfo(
        path=path,
        bitrate_bps=bitrate,
        channels=channels,
        codec=str(stream.get("codec_name", "unknown")),
        duration_seconds=duration,
        chapter_count=len(data.get("chapters", []) or []),
        size_bytes=size_bytes,
        format_name=str(fmt.get("format_name", "")),
        sample_rate=_first_int(stream.get("sample_rate")),
        metadata=_extract_tags(data, stream),
        artwork=_select_artwork(data),
    )


def _primary_audio_stream(data: dict[str, Any]) -> dict[str, Any] | None:
    for stream in data.get("streams", []) or []:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def _derive_bitrate(fmt: dict[str, Any]) -> int | None:
    """Compute bitrate from size and duration when ffprobe reports neither.

    Some MP3s written by older tools carry no bitrate field at all.
    """

    size = _first_int(fmt.get("size"))
    duration = _first_float(fmt.get("duration"))
    if not size or not duration or duration <= 0:
        return None
    return int(size * 8 / duration)


def _extract_tags(data: dict[str, Any], stream: dict[str, Any]) -> dict[str, str]:
    combined: dict[str, Any] = {}
    combined.update(stream.get("tags", {}) or {})
    combined.update((data.get("format", {}) or {}).get("tags", {}) or {})
    folded = {str(k).casefold(): str(v).strip() for k, v in combined.items()}
    return {key: folded[key] for key in _INTERESTING_TAGS if folded.get(key)}


def _select_artwork(data: dict[str, Any]) -> ArtworkStream | None:
    candidates: list[tuple[int, ArtworkStream]] = []
    for stream in data.get("streams", []) or []:
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition", {}) or {}
        tags = {str(k).casefold(): str(v).casefold() for k, v in (stream.get("tags", {}) or {}).items()}
        attached = _first_int(disposition.get("attached_pic")) == 1
        hint = f"{tags.get('title', '')} {tags.get('comment', '')}"
        if not attached and "cover" not in hint:
            continue
        index = _first_int(stream.get("index"))
        if index is None or index < 0:
            continue
        is_front = "front" in hint
        candidates.append(
            (
                0 if is_front else 1,
                ArtworkStream(
                    index=index,
                    extension=_artwork_extension(str(stream.get("codec_name", ""))),
                    is_front_cover=is_front,
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1].index))
    return candidates[0][1]


def _artwork_extension(codec_name: str) -> str:
    return "png" if codec_name.casefold() in {"png", "apng"} else "jpg"


def _first_int(*values: Any) -> int | None:
    for value in values:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None
