"""Discovering books on disk.

A "book" is either a single audio file or a directory of audio files that make
up one title.  The distinction matters because a folder book becomes one output
with generated chapters, while each single file converts on its own.

Discovery never modifies anything and never raises for one bad file: a file that
cannot be read is recorded as an error and the scan continues.  A triage session
that aborts half-way through a directory is useless.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .disposition import Disposition, read_folder
from .errors import ProbeError, TagError
from .probe import AudioInfo, probe
from .tags import SUPPORTED_EXTENSIONS, BookMetadata, read_metadata

LOGGER = logging.getLogger(__name__)

#: Temporary outputs written by a conversion still in flight.  Checked against
#: the stem rather than the suffix list, because author names are full of dots:
#: "J.R. Mathews - Portal.tmp.m4b".suffixes is a mess.
_IN_PROGRESS_RE = re.compile(r"\.(tmp|part|partial|forge-tmp)$", re.IGNORECASE)

ProgressCallback = Callable[[int, int, Path | None], None]


def is_audio_file(path: Path) -> bool:
    return path.suffix.casefold() in SUPPORTED_EXTENSIONS


def is_in_progress(path: Path) -> bool:
    """True for a file another process is still writing."""

    if path.name.startswith("."):
        return True
    return bool(_IN_PROGRESS_RE.search(path.stem))


def book_key(path: Path) -> str:
    """A stable identifier for a book, derived from its absolute path."""

    return hashlib.sha256(str(Path(path).absolute()).encode("utf-8")).hexdigest()[:16]


@dataclass
class Book:
    """One book discovered on disk, with everything triage needs to show it."""

    path: Path
    is_folder: bool
    files: list[Path]
    metadata: BookMetadata | None = None
    info: AudioInfo | None = None
    disposition: Disposition = Disposition.UNTRIAGED
    disposition_conflict: bool = False
    errors: list[str] = field(default_factory=list)
    #: False when the scan deliberately skipped ffprobe, so that a missing
    #: :attr:`info` is not reported as a probe failure.
    probed: bool = True

    @property
    def key(self) -> str:
        return book_key(self.path)

    @property
    def display_name(self) -> str:
        return self.path.name

    @property
    def title(self) -> str | None:
        if self.metadata is None:
            return None
        return self.metadata.album or self.metadata.title

    @property
    def author(self) -> str | None:
        if self.metadata is None:
            return None
        return self.metadata.author or self.metadata.albumartist

    @property
    def total_duration(self) -> float:
        """Combined duration in seconds, summed across a folder book's tracks."""

        if self.info is not None and not self.is_folder:
            return self.info.duration_seconds
        if self.metadata is not None and self.metadata.duration:
            return float(self.metadata.duration)
        return 0.0

    @property
    def disposition_label(self) -> str:
        return "conflict" if self.disposition_conflict else self.disposition.label

    def blocking_reasons(self) -> list[str]:
        """Why this book cannot be converted as it stands.

        Computed, never stored, so it cannot go stale against the file.  An
        empty list means ready; a disposition set on a book with a non-empty
        list is an instruction forge will decline to carry out.
        """

        reasons: list[str] = []
        if not self.files:
            reasons.append("no audio files")
        if self.probed and self.info is None:
            reasons.append("could not be probed")
        if self.disposition_conflict:
            reasons.append("files disagree on disposition")
        if not self.author:
            reasons.append("missing author")
        if not self.title:
            reasons.append("missing album or title")
        return reasons

    @property
    def is_ready(self) -> bool:
        return not self.blocking_reasons()

    @property
    def readiness_label(self) -> str:
        if self.disposition is Disposition.UNTRIAGED and self.is_ready:
            return "·"
        return "ok" if self.is_ready else "!"


@dataclass
class ScanResult:
    """Everything one scan found."""

    books: list[Book] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_in_progress: list[Path] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.books)

    @property
    def actionable(self) -> list[Book]:
        return [b for b in self.books if b.disposition.is_actionable and not b.disposition_conflict]


def scan(
    root: Path | str,
    *,
    with_probe: bool = True,
    progress: ProgressCallback | None = None,
) -> ScanResult:
    """Find every book under *root*.

    Set *with_probe* false to skip ffprobe, which is much faster when only tags
    are needed.  Without it, ``Book.info`` is ``None`` and the bitrate column is
    unavailable.
    """

    root = Path(root).expanduser().resolve()
    result = ScanResult()

    if not root.is_dir():
        result.errors.append(f"not a directory: {root}")
        return result

    grouped, walk_errors, in_progress = _group_by_directory(root)
    result.errors.extend(walk_errors)
    result.skipped_in_progress.extend(in_progress)

    total = sum(len(files) for files in grouped.values())
    done = 0
    if progress:
        progress(0, total, None)

    for directory, files in grouped.items():
        # A directory holding more than one audio file is one book with several
        # tracks.  Files sitting directly in the scan root are always treated
        # individually, since the root is a drop folder, not a book.
        as_folder_book = directory != root and len(files) > 1

        if as_folder_book:
            book = _build_book(directory, files, is_folder=True, with_probe=with_probe)
            result.books.append(book)
            done += len(files)
            if progress:
                progress(done, total, directory)
            continue

        for path in files:
            book = _build_book(path, [path], is_folder=False, with_probe=with_probe)
            result.books.append(book)
            done += 1
            if progress:
                progress(done, total, path)

    result.books.sort(key=triage_sort_key)
    return result


def _group_by_directory(root: Path) -> tuple[dict[Path, list[Path]], list[str], list[Path]]:
    grouped: dict[Path, list[Path]] = {}
    errors: list[str] = []
    in_progress: list[Path] = []

    def on_error(exc: OSError) -> None:
        errors.append(f"{exc.filename}: {exc.strerror}")

    for current, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames.sort()
        directory = Path(current)
        found: list[Path] = []
        for name in sorted(filenames):
            path = directory / name
            if not is_audio_file(path):
                continue
            if is_in_progress(path):
                in_progress.append(path)
                continue
            found.append(path)
        if found:
            grouped[directory] = found

    return grouped, errors, in_progress


def _build_book(path: Path, files: list[Path], *, is_folder: bool, with_probe: bool) -> Book:
    book = Book(path=path, is_folder=is_folder, files=list(files), probed=with_probe)

    try:
        book.metadata = read_metadata(files[0])
    except TagError as exc:
        book.errors.append(str(exc))

    if with_probe:
        try:
            book.info = probe(files[0])
        except ProbeError as exc:
            book.errors.append(str(exc))

    folder_state = read_folder(files)
    book.disposition = folder_state.value
    book.disposition_conflict = folder_state.conflict

    if is_folder and with_probe and book.info is not None:
        # A folder book's duration is the sum of its tracks, not the first one.
        total = 0.0
        for track in files:
            try:
                total += probe(track).duration_seconds
            except ProbeError as exc:
                book.errors.append(f"{track.name}: {exc}")
        if total > 0 and book.metadata is not None:
            book.metadata.duration = int(total)

    return book


def triage_sort_key(book: Book) -> tuple[int, str]:
    """Sort needs-attention and conflicts first, then alphabetically.

    The books that need a decision are the reason triage was opened, so they go
    where the cursor already is.
    """

    if book.disposition_conflict:
        rank = 0
    elif book.disposition is Disposition.NEEDS_ATTENTION:
        rank = 1
    elif not book.is_ready:
        rank = 2
    elif book.disposition is Disposition.UNTRIAGED:
        rank = 3
    else:
        rank = 4
    return rank, book.display_name.casefold()
