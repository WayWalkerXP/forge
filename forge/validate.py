"""Verifying a converted file before anything irreversible happens.

Validation runs on the temporary output, before it is promoted into place and
before the original is archived.  That ordering is the whole safety model: if
any check fails, the temporary file is deleted and the source is still exactly
where it was.

Every check compares the output against the *source* or the *plan*, never
against a hardcoded expectation, so the checks stay meaningful as the rules
change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .disposition import Disposition
from .errors import ProbeError
from .plan import Plan
from .probe import AudioInfo, probe

LOGGER = logging.getLogger(__name__)

#: VBR AAC overshoots and undershoots its nominal rate considerably, especially
#: on speech, so this tolerance has to be generous to avoid false failures.
BITRATE_TOLERANCE_RATIO = 0.25
BITRATE_TOLERANCE_FLOOR_BPS = 8_000

#: Duration must match closely: a truncated output is the failure that matters
#: most, because it is the one a listener discovers hours in.
DURATION_TOLERANCE_SECONDS = 2.0
DURATION_TOLERANCE_RATIO = 0.01


@dataclass
class ValidationResult:
    """The outcome of validating one converted file."""

    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_info: AudioInfo | None = None

    def summary(self) -> str:
        if self.ok:
            return "validated" if not self.warnings else f"validated with {len(self.warnings)} warning(s)"
        return "; ".join(self.failures)


def validate(plan: Plan, output_path: Path) -> ValidationResult:
    """Check that *output_path* is a usable conversion of *plan*'s source."""

    result = ValidationResult(ok=False)

    if not output_path.exists():
        result.failures.append("the converted file does not exist")
        return result
    if output_path.stat().st_size == 0:
        result.failures.append("the converted file is empty")
        return result

    try:
        info = probe(output_path)
    except ProbeError as exc:
        result.failures.append(f"the converted file is not readable: {exc}")
        return result

    result.output_info = info
    source = plan.source_info

    _check_channels(plan, info, result)
    _check_bitrate(plan, info, result)
    _check_duration(plan, source, info, result)
    _check_chapters(plan, source, info, result)
    _check_container(plan, info, result)

    result.ok = not result.failures
    if result.ok:
        LOGGER.info("validated %s: %s", output_path.name, result.summary())
    else:
        LOGGER.error("validation failed for %s: %s", output_path.name, result.summary())
    return result


def _check_channels(plan: Plan, info: AudioInfo, result: ValidationResult) -> None:
    expected = plan.target.channels
    if expected is None:
        return
    if plan.disposition is Disposition.REMUX:
        # A remux copies the stream, so the channel count must be untouched.
        if plan.source_info and info.channels != plan.source_info.channels:
            result.failures.append(
                f"remux changed the channel count from "
                f"{plan.source_info.channels} to {info.channels}"
            )
        return
    if info.channels != expected:
        result.failures.append(
            f"expected {expected} channel(s), found {info.channels}"
        )


def _check_bitrate(plan: Plan, info: AudioInfo, result: ValidationResult) -> None:
    if not plan.disposition.re_encodes:
        return
    expected_kbps = plan.target.bitrate_kbps
    if not expected_kbps:
        return

    expected_bps = expected_kbps * 1000
    tolerance = max(BITRATE_TOLERANCE_FLOOR_BPS, round(expected_bps * BITRATE_TOLERANCE_RATIO))
    if abs(info.bitrate_bps - expected_bps) > tolerance:
        result.failures.append(
            f"expected about {expected_kbps} kbps, found {info.bitrate_kbps} kbps"
        )


def _check_duration(
    plan: Plan, source: AudioInfo | None, info: AudioInfo, result: ValidationResult
) -> None:
    expected = plan.total_duration or (source.duration_seconds if source else 0.0)
    if expected <= 0:
        result.warnings.append("source duration unknown, so duration was not verified")
        return

    tolerance = max(DURATION_TOLERANCE_SECONDS, expected * DURATION_TOLERANCE_RATIO)
    drift = abs(expected - info.duration_seconds)
    if drift > tolerance:
        result.failures.append(
            f"duration differs by {drift:.1f}s "
            f"(source {_hms(expected)}, output {_hms(info.duration_seconds)})"
        )


def _check_chapters(
    plan: Plan, source: AudioInfo | None, info: AudioInfo, result: ValidationResult
) -> None:
    if plan.is_folder:
        expected = len(plan.chapters)
        if expected and info.chapter_count != expected:
            result.failures.append(
                f"expected {expected} generated chapter(s), found {info.chapter_count}"
            )
        return

    if source is None:
        return
    if source.chapter_count and info.chapter_count != source.chapter_count:
        # Losing chapters turns a navigable audiobook into one long track, which
        # is a real regression even though the audio is intact.
        result.failures.append(
            f"source has {source.chapter_count} chapter(s) but the output has "
            f"{info.chapter_count}"
        )


def _check_container(plan: Plan, info: AudioInfo, result: ValidationResult) -> None:
    if plan.output_path is None:
        return
    if plan.output_path.suffix.casefold() in {".m4b", ".m4a"}:
        if "mp4" not in info.format_name.casefold():
            result.failures.append(
                f"expected an MP4 container, found {info.format_name or 'unknown'}"
            )


def _hms(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def verify_copy(source: Path, destination: Path, *, hash_check: bool = True) -> list[str]:
    """Confirm *destination* is a faithful copy of *source*.

    Used by the ``as-is`` path, which moves a file the user has declared already
    correct.  Size alone catches truncation; the hash catches corruption, which
    matters because the source is deleted immediately afterwards.
    """

    failures: list[str] = []

    if not destination.exists():
        return ["the copy does not exist"]

    try:
        source_size = source.stat().st_size
        dest_size = destination.stat().st_size
    except OSError as exc:
        return [f"could not stat the copy: {exc}"]

    if source_size != dest_size:
        return [f"size differs: source {source_size} bytes, copy {dest_size} bytes"]

    if hash_check:
        try:
            if _digest(source) != _digest(destination):
                failures.append("contents differ from the source")
        except OSError as exc:
            failures.append(f"could not read both files to compare them: {exc}")

    return failures


def _digest(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
