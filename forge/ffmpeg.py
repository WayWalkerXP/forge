"""Running ffmpeg and ffprobe.

Beyond wrapping ``subprocess``, this module adds the one piece neither source
project had: incremental progress.  Both of them called
``Popen.communicate()``, which blocks until ffmpeg exits, so a conversion was a
single opaque wait.  :func:`run_with_progress` reads ``-progress pipe:1`` as it
streams, which is what lets a long conversion show a live bar and an ETA.

Child processes are registered in a module-level set so that a quit request can
terminate whatever is running.  Each child gets its own process group on POSIX,
so terminating it does not also signal forge.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .errors import DiskSpaceError, ExternalToolError

LOGGER = logging.getLogger(__name__)

_PROCESS_LOCK = threading.Lock()
_ACTIVE: set[subprocess.Popen] = set()

#: ffmpeg writes these to stderr when it runs out of room.  Matched as a
#: fallback for platforms or filesystems that do not surface ENOSPC cleanly.
_DISK_FULL_MARKERS = (
    "no space left on device",
    "disk full",
    "not enough space",
    "insufficient disk space",
    "enospc",
)


@dataclass(frozen=True)
class CommandResult:
    """A finished subprocess, decoded so odd byte sequences cannot raise."""

    returncode: int
    stdout: str
    stderr: str
    command: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Progress:
    """One progress sample taken while ffmpeg runs."""

    seconds_done: float
    total_seconds: float
    speed: float | None = None

    @property
    def fraction(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return min(1.0, max(0.0, self.seconds_done / self.total_seconds))

    @property
    def eta_seconds(self) -> float | None:
        if not self.speed or self.speed <= 0 or self.total_seconds <= 0:
            return None
        remaining = max(0.0, self.total_seconds - self.seconds_done)
        return remaining / self.speed


ProgressCallback = Callable[[Progress], None]


def require_binary(name: str) -> str:
    """Return the resolved path to *name*, or raise with an actionable message."""

    found = shutil.which(name)
    if not found:
        raise ExternalToolError(
            f"{name} was not found on PATH. Install ffmpeg "
            f"(apt install ffmpeg) or add it to PATH."
        )
    return found


def run(command: Iterable[str], *, timeout: float | None = None) -> CommandResult:
    """Run *command* to completion and capture its output."""

    argv = [str(part) for part in command]
    require_binary(argv[0])
    LOGGER.debug("running: %s", " ".join(argv))

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    _register(process)
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(process)
        raw_out, raw_err = process.communicate()
    finally:
        _unregister(process)

    return CommandResult(
        returncode=process.returncode,
        stdout=_decode(raw_out),
        stderr=_decode(raw_err),
        command=tuple(argv),
    )


def run_with_progress(
    command: Iterable[str],
    total_seconds: float,
    on_progress: ProgressCallback | None = None,
    *,
    poll_interval: float = 0.25,
) -> CommandResult:
    """Run an ffmpeg *command*, reporting progress as it goes.

    The command must already include ``-progress pipe:1`` (see
    :func:`progress_flags`).  stdout carries the progress stream; stderr is
    drained on a thread so a chatty ffmpeg cannot fill the pipe buffer and
    deadlock - which is the trap the blocking implementation avoided only by
    never reading either stream until exit.
    """

    argv = [str(part) for part in command]
    require_binary(argv[0])
    LOGGER.debug("running with progress: %s", " ".join(argv))

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    _register(process)

    stderr_chunks: list[bytes] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        for chunk in iter(lambda: process.stderr.read(8192), b""):
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, name="ffmpeg-stderr", daemon=True)
    stderr_thread.start()

    last_emit = 0.0
    try:
        assert process.stdout is not None
        fields: dict[str, str] = {}
        for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()

            if key.strip() != "progress":
                continue
            # A "progress" line terminates each sample block.
            snapshot = _snapshot(fields, total_seconds)
            fields.clear()
            if snapshot is None or on_progress is None:
                continue
            now = time.monotonic()
            if value.strip() == "end" or now - last_emit >= poll_interval:
                last_emit = now
                try:
                    on_progress(snapshot)
                except Exception:  # observation must never break the conversion
                    LOGGER.debug("progress callback raised", exc_info=True)
    finally:
        process.wait()
        stderr_thread.join(timeout=5)
        _unregister(process)

    stderr_text = _decode(b"".join(stderr_chunks))
    if process.returncode != 0 and is_disk_space_error(stderr_text):
        raise DiskSpaceError(stderr_text.strip() or "ffmpeg reported that the disk is full")

    return CommandResult(
        returncode=process.returncode,
        stdout="",
        stderr=stderr_text,
        command=tuple(argv),
    )


def progress_flags() -> list[str]:
    """Flags that make ffmpeg emit a machine-readable progress stream."""

    return ["-progress", "pipe:1", "-nostats"]


def _snapshot(fields: dict[str, str], total_seconds: float) -> Progress | None:
    raw_us = fields.get("out_time_us") or fields.get("out_time_ms")
    seconds: float | None = None
    if raw_us:
        try:
            # out_time_ms is also microseconds in ffmpeg's output, despite the name.
            seconds = max(0.0, int(raw_us) / 1_000_000)
        except ValueError:
            seconds = None
    if seconds is None:
        raw_time = fields.get("out_time")
        if raw_time:
            seconds = _parse_timestamp(raw_time)
    if seconds is None:
        return None

    speed: float | None = None
    raw_speed = fields.get("speed", "").rstrip("x").strip()
    if raw_speed and raw_speed not in ("N/A", "-"):
        try:
            speed = float(raw_speed)
        except ValueError:
            speed = None

    return Progress(seconds_done=seconds, total_seconds=total_seconds, speed=speed)


def _parse_timestamp(text: str) -> float | None:
    match = re.match(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def available_audio_encoders() -> set[str]:
    """Return the audio encoder names this ffmpeg build actually provides.

    Both source projects shipped this and never called it, then hardcoded
    ``libfdk_aac`` - which fails at conversion time on a stock ffmpeg build.
    """

    result = run(["ffmpeg", "-hide_banner", "-encoders"])
    if not result.ok:
        raise ExternalToolError(f"ffmpeg -encoders failed: {result.stderr.strip()}")

    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        parts = stripped.split()
        if len(parts) < 2:
            continue
        flags = parts[0]
        # Encoder rows look like " A..... aac   AAC (Advanced Audio Coding)".
        if flags.startswith("A") and set(flags) <= set("ADEFILSVXadefilsvx."):
            encoders.add(parts[1])
    return encoders


def select_codec(preferred: str, fallback: str) -> str:
    """Pick the best available encoder, preferring *preferred*."""

    available = available_audio_encoders()
    if preferred in available:
        return preferred
    if fallback in available:
        LOGGER.warning("encoder %s is unavailable; falling back to %s", preferred, fallback)
        return fallback
    raise ExternalToolError(
        f"neither {preferred} nor {fallback} is available in this ffmpeg build"
    )


def is_disk_space_error(value: BaseException | str) -> bool:
    """True when *value* indicates the disk filled up."""

    if isinstance(value, OSError) and value.errno == errno.ENOSPC:
        return True
    text = str(value).casefold()
    return any(marker in text for marker in _DISK_FULL_MARKERS)


def check_free_space(destination: Path, needed_bytes: int, *, margin: float = 1.15) -> None:
    """Raise :class:`DiskSpaceError` when *destination* lacks room.

    A pre-flight check, because discovering a full disk half-way through a
    ten-hour transcode wastes the whole run.  The margin covers container
    overhead and the temporary file living beside the final output.
    """

    target = Path(destination)
    probe_dir = target if target.is_dir() else target.parent
    try:
        free = shutil.disk_usage(probe_dir).free
    except OSError as exc:
        LOGGER.warning("could not check free space on %s: %s", probe_dir, exc)
        return

    required = int(needed_bytes * margin)
    if free < required:
        raise DiskSpaceError(
            f"{probe_dir} has {free / 1e6:.0f} MB free but about "
            f"{required / 1e6:.0f} MB is needed"
        )


# --------------------------------------------------------------------------- #
# Process control
# --------------------------------------------------------------------------- #


def _register(process: subprocess.Popen) -> None:
    with _PROCESS_LOCK:
        _ACTIVE.add(process)


def _unregister(process: subprocess.Popen) -> None:
    with _PROCESS_LOCK:
        _ACTIVE.discard(process)


def terminate(process: subprocess.Popen) -> None:
    """Stop one child process, escalating from SIGTERM to SIGKILL."""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def terminate_all() -> None:
    """Stop every running child.  The cancellation primitive."""

    with _PROCESS_LOCK:
        processes = tuple(_ACTIVE)
    for process in processes:
        terminate(process)


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")
