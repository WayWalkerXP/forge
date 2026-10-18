"""Turning a triaged book into a concrete, inspectable conversion plan.

A plan is pure: building one touches nothing on disk beyond reading.  That is
what makes ``--dry-run`` trustworthy and what lets the triage UI show exactly
what a run would do.

Three outcomes, and the distinction matters for what the executor does next:

``PLANNED``
    Everything checks out; the executor may proceed.
``SKIPPED``
    A deliberate, non-error decision - the rules declined the source, or the
    disposition is not one that acts.  Nothing is wrong.
``INVALID``
    The disposition says to act but the book cannot be acted on as it stands.
    This is the case that becomes ``needs-attention``: forge was told to do
    something it cannot do, and a human needs to look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import naming, rules
from .config import Config
from .disposition import Disposition
from .errors import ProbeError
from .paths import relative_to_or_name, temp_output_path
from .probe import AudioInfo, probe
from .scan import Book
from .tags import parse_int

LOGGER = logging.getLogger(__name__)


class PlanStatus(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    INVALID = "invalid"


@dataclass(frozen=True)
class Chapter:
    """One chapter of a generated folder-book output."""

    title: str
    start_seconds: float
    duration_seconds: float

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True)
class Plan:
    """Everything the executor needs, and everything a dry run should show."""

    book_key: str
    source_path: Path
    input_paths: tuple[Path, ...]
    is_folder: bool
    disposition: Disposition
    status: PlanStatus
    target: rules.Target
    codec: str | None = None
    output_path: Path | None = None
    temp_path: Path | None = None
    archive_path: Path | None = None
    source_info: AudioInfo | None = None
    total_duration: float = 0.0
    chapters: tuple[Chapter, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is PlanStatus.PLANNED

    @property
    def needs_attention(self) -> bool:
        """True when forge was told to act but cannot."""

        return self.status is PlanStatus.INVALID

    @property
    def target_summary(self) -> str:
        """The target as a renderer should print it.

        ``delete`` has no encoding target, and showing the empty one through
        ``Target.__str__`` would print "declined: ..." - which reads as forge
        refusing the book rather than as the whole point of the disposition.
        """

        if self.disposition.destroys:
            return "the source will be deleted"
        return str(self.target)

    @property
    def estimated_output_bytes(self) -> int:
        if self.source_info is None:
            return 0
        if self.disposition.re_encodes and self.target.bitrate_kbps:
            return int(self.total_duration * self.target.bitrate_kbps * 1000 / 8)
        # remux and as-is keep the audio, so the output is about the input size.
        return self.source_info.size_bytes

    def describe(self) -> str:
        """One-line summary for a dry run or a log."""

        name = self.output_path.name if self.output_path else self.source_path.name
        if self.status is PlanStatus.PLANNED:
            if self.disposition.destroys:
                files = "" if not self.is_folder else f" and its {len(self.input_paths)} track(s)"
                return f"delete {self.source_path.name}{files}"
            return f"{self.disposition} -> {name} ({self.target})"
        reason = "; ".join(self.errors or self.warnings) or self.target.rationale
        return f"{self.status}: {reason}"


def plan(book: Book, config: Config, *, codec: str | None = None) -> Plan:
    """Build a plan for *book*.

    *codec* should be the result of :func:`forge.ffmpeg.select_codec`, resolved
    once per run.  Passing ``None`` leaves it unset, which is fine for a dry run
    but makes the plan non-executable.
    """

    # First, before anything is probed: the naming templates decide what this
    # run produces, and an unusable one is a config error, not a book error.
    templates = naming.require(config)

    warnings: list[str] = []
    errors: list[str] = []
    disposition = book.disposition

    def result(status: PlanStatus, **extra: object) -> Plan:
        return Plan(
            book_key=book.key,
            source_path=book.path,
            input_paths=tuple(book.files),
            is_folder=book.is_folder,
            disposition=disposition,
            status=status,
            warnings=tuple(warnings),
            errors=tuple(errors),
            **extra,  # type: ignore[arg-type]
        )

    # --- things that are not errors, just nothing to do ------------------- #
    if book.disposition_conflict:
        errors.append(
            "the files in this folder carry different dispositions; "
            "re-tag the whole folder before converting"
        )
        return result(PlanStatus.INVALID, target=rules.Target(None, None, "conflicted disposition"))

    if not disposition.is_actionable:
        return result(
            PlanStatus.SKIPPED,
            target=rules.Target(None, None, f"disposition is {disposition.label}"),
        )

    if not book.files:
        errors.append("no audio files found")
        return result(PlanStatus.INVALID, target=rules.Target(None, None, "no input files"))

    if disposition is Disposition.DELETE:
        # Nothing below this point applies: there is no bitrate to decide, no
        # name to render and no archive copy to file.  Returning here is also
        # what makes the junk this disposition exists for deletable at all - a
        # rip with no author tag would be INVALID on naming grounds, and
        # demanding tags from a file on its way to the bin is backwards.
        return result(
            PlanStatus.PLANNED,
            target=rules.Target(None, None, "tagged for deletion, so nothing is encoded"),
        )

    if disposition is Disposition.AS_IS and book.is_folder:
        # "as-is" means this file is already the finished product.  A directory
        # of separate tracks is by definition not one finished m4b, so relocating
        # it unchanged would just move the problem.
        errors.append(
            "as-is cannot apply to a folder of separate tracks; "
            "use downcode or remux to combine them into one m4b"
        )
        return result(
            PlanStatus.INVALID,
            target=rules.Target(None, None, "as-is is not valid for a folder book"),
        )

    # --- facts about the source ------------------------------------------- #
    source_info = book.info
    if source_info is None:
        try:
            source_info = probe(book.files[0])
        except ProbeError as exc:
            errors.append(f"could not probe the source: {exc}")
            return result(PlanStatus.INVALID, target=rules.Target(None, None, "unreadable source"))

    chapters, total_duration, chapter_warnings = _build_chapters(book, source_info)
    warnings.extend(chapter_warnings)

    # --- the encoding decision -------------------------------------------- #
    target = rules.decide(source_info, disposition, max_bitrate_kbps=config.max_bitrate_kbps)
    if target.declined:
        # The rules say this source is not worth re-encoding.  The user asked
        # for it anyway, so this is a case for a human, not a silent pass.
        errors.append(target.rationale)
        return result(PlanStatus.INVALID, target=target, source_info=source_info,
                      total_duration=total_duration, chapters=chapters)

    # --- naming ----------------------------------------------------------- #
    # The same template that names the book in the library, so a conversion
    # output does not have to be renamed to be shelved, and two books by one
    # author sharing a title do not collide on "Author - Title.m4b".
    #
    # From the raw tags, not the canonical author or the cleaned-up title:
    # planning is pure, and those come from databases that shelving opens.  A
    # book whose title is tidied later is renamed by the move, which is what
    # the move does anyway.
    if not book.author:
        errors.append("missing author tag, and a book with no author cannot be shelved")
    if not book.title:
        errors.append("missing album and title tags, so the book has no name")
    if errors:
        return result(PlanStatus.INVALID, target=target, source_info=source_info,
                      total_duration=total_duration, chapters=chapters)

    assert book.metadata is not None  # implied by book.author and book.title
    try:
        name = naming.render_filename(
            templates.filename, naming.tag_values(book.metadata)
        )
    except naming.EmptyName as exc:
        errors.append(str(exc))
        return result(PlanStatus.INVALID, target=target, source_info=source_info,
                      total_duration=total_duration, chapters=chapters)
    output_path = config.output_dir / name
    temp_path = temp_output_path(output_path)
    archive_path = config.archive_dir / relative_to_or_name(book.path, config.source_dir)

    # --- conflicts -------------------------------------------------------- #
    if output_path.exists():
        errors.append(f"output already exists: {output_path}")
    if temp_path.exists():
        warnings.append(f"a stale temporary file will be removed: {temp_path.name}")
    if output_path in set(book.files) or output_path == book.path:
        errors.append("the output path is the same as the source")
    if disposition is not Disposition.AS_IS and archive_path.exists():
        warnings.append(
            f"archive destination exists; the original will be filed alongside it "
            f"as {archive_path.name}"
        )

    if codec is None and disposition.re_encodes:
        warnings.append("no encoder resolved; this plan cannot be executed as-is")

    status = PlanStatus.INVALID if errors else PlanStatus.PLANNED
    return result(
        status,
        target=target,
        codec=codec if disposition.re_encodes else None,
        output_path=output_path,
        temp_path=temp_path,
        archive_path=None if disposition is Disposition.AS_IS else archive_path,
        source_info=source_info,
        total_duration=total_duration,
        chapters=chapters,
    )


def plan_many(books: list[Book], config: Config, *, codec: str | None = None) -> list[Plan]:
    """Plan a batch, cross-checking for outputs that would collide.

    Two books whose tags render to the same filename would otherwise race: the
    first converts, the second fails on "output already exists" only after
    wasting a full transcode.  Catching it here makes both visible up front.
    """

    plans = [plan(book, config, codec=codec) for book in books]

    claimed: dict[Path, list[int]] = {}
    for index, item in enumerate(plans):
        if item.ok and item.output_path is not None:
            claimed.setdefault(item.output_path, []).append(index)

    for output_path, indexes in claimed.items():
        if len(indexes) < 2:
            continue
        names = [plans[i].source_path.name for i in indexes]
        for i in indexes:
            others = [n for n in names if n != plans[i].source_path.name] or names
            plans[i] = _invalidate(
                plans[i],
                f"several books would produce {output_path.name}: {', '.join(others)}",
            )

    return plans


def _invalidate(item: Plan, message: str) -> Plan:
    from dataclasses import replace

    return replace(item, status=PlanStatus.INVALID, errors=item.errors + (message,))


# --------------------------------------------------------------------------- #
# Folder books
# --------------------------------------------------------------------------- #


def _build_chapters(
    book: Book, source_info: AudioInfo
) -> tuple[tuple[Chapter, ...], float, list[str]]:
    """Derive chapter markers for a folder book, one chapter per track."""

    warnings: list[str] = []

    if not book.is_folder:
        return (), source_info.duration_seconds, warnings

    ordered, order_certain = _ordered_tracks(book.files)
    if not order_certain:
        warnings.append(
            "track order is uncertain, so chapters were ordered by disc, track "
            "tag, then filename; check the result before archiving the original"
        )

    chapters: list[Chapter] = []
    cursor = 0.0
    for path in ordered:
        try:
            info = probe(path)
            duration = info.duration_seconds
        except ProbeError as exc:
            warnings.append(f"{path.name}: {exc}; chapter times after this point are approximate")
            duration = 0.0
        chapters.append(
            Chapter(
                title=_chapter_title(path),
                start_seconds=cursor,
                duration_seconds=duration,
            )
        )
        cursor += duration

    return tuple(chapters), cursor, warnings


def _ordered_tracks(files: list[Path]) -> tuple[list[Path], bool]:
    """Sort a folder book's tracks, reporting whether the order is trustworthy."""

    from .tags import read_metadata

    entries: list[tuple[int, int, int, str, Path]] = []
    numbers: list[int | None] = []

    for path in files:
        disc = 0
        track: int | None = None
        try:
            meta = read_metadata(path)
            disc = meta.disc or 0
            track = meta.track
        except Exception:  # a bad tag must not stop the ordering
            LOGGER.debug("could not read track tags from %s", path, exc_info=True)
        if track is None:
            track = _track_from_filename(path)
        numbers.append(track)
        entries.append(
            (disc, 0 if track is not None else 1, track or 0, path.name.casefold(), path)
        )

    entries.sort(key=lambda item: item[:4])
    ordered = [item[4] for item in entries]

    # The order is trustworthy only when every track carries a number and no two
    # tracks share one.  Anything less and the chapters could come out shuffled,
    # which is why the planner warns rather than silently proceeding.
    present = [n for n in numbers if n is not None]
    all_numbered = len(present) == len(files)
    all_unique = len(set(present)) == len(present)
    return ordered, all_numbered and all_unique


def _track_from_filename(path: Path) -> int | None:
    """Pull a leading track number out of a filename.

    Handles ``01 - Title``, ``[01] Title``, ``(01) Title`` and ``1-01 Title``.
    """

    import re

    stem = path.stem
    match = re.match(r"^\s*[\[\(]?\s*(?:\d+\s*[-_.]\s*)?(\d{1,4})\s*[\]\)]?\s*[-_. ]", stem)
    if match:
        number = int(match.group(1))
        return number if number > 0 else None
    return parse_int(stem) or None


def _chapter_title(path: Path) -> str:
    """A chapter title for one track, preferring its title tag."""

    from .tags import read_metadata

    try:
        meta = read_metadata(path)
        if meta.title:
            return meta.title
    except Exception:
        LOGGER.debug("could not read a title from %s", path, exc_info=True)
    return path.stem
