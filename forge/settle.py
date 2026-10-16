"""Waiting for files to stop changing.

An unattended run fires on a schedule, not when work is ready, so it will
sometimes find a file that a download or another converter is still writing.
Converting a partial file produces a truncated audiobook that validates
perfectly well against its own truncated self.

Two independent checks, because each catches a case the other misses:

*   **Stability** - size and mtime are sampled, a moment passes, and they are
    sampled again.  Catches a file being actively written right now.
*   **Age** - the last write must be at least ``min_age`` ago.  Catches a
    transfer that happens to be stalled during the stability window.

One shared wait covers the whole batch rather than one wait per file.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def describe_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def partition_settled(
    books: Sequence[T],
    *,
    min_age: float = 120.0,
    settle_seconds: float = 5.0,
) -> tuple[list[T], list[tuple[T, str]]]:
    """Split *books* into those safe to process and those still in flux.

    Each item must expose a ``files`` sequence of paths.  A book is ready only
    when every one of its files is ready: converting three settled tracks and
    one half-written one would silently produce a short book.
    """

    snapshots: dict[Path, tuple[int, int] | OSError] = {}
    for book in books:
        for path in _files_of(book):
            snapshots[path] = _sample(path)

    if settle_seconds > 0 and snapshots:
        time.sleep(settle_seconds)

    now = time.time()
    ready: list[T] = []
    pending: list[tuple[T, str]] = []

    for book in books:
        reason = None
        for path in _files_of(book):
            reason = _why_not_ready(path, snapshots.get(path), now=now, min_age=min_age)
            if reason:
                break
        if reason:
            pending.append((book, reason))
        else:
            ready.append(book)

    return ready, pending


def _files_of(book: object) -> list[Path]:
    files = getattr(book, "files", None)
    if files:
        return [Path(f) for f in files]
    path = getattr(book, "path", None)
    return [Path(path)] if path else []


def _sample(path: Path) -> tuple[int, int] | OSError:
    try:
        stat = path.stat()
    except OSError as exc:
        return exc
    return stat.st_size, stat.st_mtime_ns


def _why_not_ready(
    path: Path,
    before: tuple[int, int] | OSError | None,
    *,
    now: float,
    min_age: float,
) -> str | None:
    if before is None:
        return f"{path.name} was not checked"
    if isinstance(before, OSError):
        return f"{path.name} could not be read: {before}"

    try:
        stat = path.stat()
    except OSError:
        # Renamed or removed between the two samples - most likely a converter
        # finishing and swapping its temporary file into place.
        return f"{path.name} disappeared during the check"

    if (stat.st_size, stat.st_mtime_ns) != before:
        return f"{path.name} is still being written"

    age = now - stat.st_mtime
    if age < min_age:
        return f"{path.name} was written {describe_age(age)} ago, waiting for it to settle"

    return None
