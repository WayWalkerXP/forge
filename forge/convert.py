"""Executing a plan.

The order of operations is the safety model, and it is deliberate:

1.  Convert to a *temporary* file beside the final output.
2.  Validate that temporary file against the source.
3.  Only then promote it to its final name.
4.  Only then archive the original.
5.  Finally, stamp the output ``done`` so it can never be reprocessed.

Nothing touches the source until steps 2 and 3 have both succeeded.  Any failure
deletes the temporary file and leaves the source exactly where it was, so a
failed run is always safe to simply run again.

A book tagged ``delete`` skips all of it: there is nothing to convert, nothing
to validate and nothing to archive, so the executor simply removes the files
the user condemned during triage.  That path is the one place in forge where a
failure cannot be undone by running again, which is why the checks live in
:func:`forge.archive.delete_book` rather than here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from . import archive, disposition as disposition_mod, ffmpeg, tags, validate as validate_mod
from .config import Config
from .disposition import Disposition
from .errors import DiskSpaceError, ForcedTermination, ForgeError
from .ffmpeg import Progress
from .plan import Chapter, Plan, PlanStatus
from .probe import probe

LOGGER = logging.getLogger(__name__)


class Stage(StrEnum):
    PREPARING = "preparing"
    CONVERTING = "converting"
    VALIDATING = "validating"
    TAGGING = "tagging"
    PROMOTING = "promoting"
    ARCHIVING = "archiving"
    RELOCATING = "relocating"
    DELETING = "deleting"
    COMPLETE = "complete"


class Outcome(StrEnum):
    CONVERTED = "converted"
    RELOCATED = "relocated"
    DELETED = "deleted"
    SKIPPED = "skipped"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs-attention"


@dataclass
class ConversionResult:
    """What happened to one book."""

    plan: Plan
    outcome: Outcome
    message: str = ""
    output_path: Path | None = None
    archived_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    retagged: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.CONVERTED, Outcome.RELOCATED, Outcome.DELETED)

    @property
    def source_name(self) -> str:
        return self.plan.source_path.name


@dataclass
class StageEvent:
    """A progress notification from the executor."""

    stage: Stage
    plan: Plan
    message: str = ""
    progress: Progress | None = None


StageCallback = Callable[[StageEvent], None]

#: Truncated in user-facing messages; the full text always goes to the log.
MAX_ERROR_CHARS = 800


def execute(
    plan: Plan,
    config: Config,
    *,
    on_stage: StageCallback | None = None,
    dry_run: bool = False,
    mark_needs_attention: bool = True,
) -> ConversionResult:
    """Carry out *plan*.

    When *mark_needs_attention* is true, a book whose disposition says to act but
    which cannot be acted on has its disposition rewritten to
    ``needs-attention``, so it surfaces at the top of the next triage session
    instead of being re-reported identically on every run.
    """

    def emit(stage: Stage, message: str = "", progress: Progress | None = None) -> None:
        if on_stage is None:
            return
        try:
            on_stage(StageEvent(stage=stage, plan=plan, message=message, progress=progress))
        except Exception:  # observation must never break a conversion
            LOGGER.debug("stage callback raised", exc_info=True)

    if plan.status is PlanStatus.SKIPPED:
        return ConversionResult(plan=plan, outcome=Outcome.SKIPPED, message=plan.describe())

    if plan.status is PlanStatus.INVALID:
        return _handle_invalid(plan, dry_run=dry_run, mark=mark_needs_attention)

    if dry_run:
        return ConversionResult(
            plan=plan,
            outcome=Outcome.SKIPPED,
            message=f"dry run: would {plan.describe()}",
            output_path=plan.output_path,
            warnings=list(plan.warnings),
        )

    if plan.disposition is Disposition.DELETE:
        return _execute_delete(plan, config, emit)

    if plan.disposition is Disposition.AS_IS:
        return _execute_as_is(plan, emit)

    return _execute_conversion(plan, config, emit)


# --------------------------------------------------------------------------- #
# delete: remove the source
# --------------------------------------------------------------------------- #


def _execute_delete(
    plan: Plan, config: Config, emit: Callable[..., None]
) -> ConversionResult:
    """Throw away a book the user condemned during triage.

    No archive copy and no verification pass, because there is nothing left to
    verify against: the user's decision *was* that these bytes are not worth
    keeping.  The source directory is passed in so the deletion can be refused
    for anything outside it - ``forge run`` takes explicit paths, and a stray
    ``delete`` tag on a file in the output directory or the library must not be
    honoured there.
    """

    what = plan.source_path.name
    emit(Stage.DELETING, f"deleting {what}")

    try:
        removed = archive.delete_book(
            plan.source_path, root=config.source_dir, is_folder=plan.is_folder
        )
    except archive.DeletionRefused as exc:
        LOGGER.error("refused to delete %s: %s", plan.source_path, exc)
        return ConversionResult(plan=plan, outcome=Outcome.FAILED, message=str(exc))
    except OSError as exc:
        # A part-deleted folder is possible here and cannot be undone, so the
        # message says what was asked for rather than claiming either result.
        LOGGER.exception("deleting %s failed", plan.source_path)
        return ConversionResult(
            plan=plan,
            outcome=Outcome.FAILED,
            message=f"could not delete {what}: {exc}",
        )

    detail = f"deleted {what}" + (f" and its {removed} file(s)" if plan.is_folder else "")
    emit(Stage.COMPLETE, detail)
    return ConversionResult(plan=plan, outcome=Outcome.DELETED, message=detail)


# --------------------------------------------------------------------------- #
# as-is: relocate only
# --------------------------------------------------------------------------- #


def _execute_as_is(plan: Plan, emit: Callable[..., None]) -> ConversionResult:
    """Relocate a book the user declared already correct.

    No conversion and no archive copy: this file *is* the product, which is why
    the copy is verified by hash before the original is removed.
    """

    assert plan.output_path is not None
    emit(Stage.RELOCATING, f"verifying copy to {plan.output_path.name}")

    try:
        result = archive.safe_relocate(plan.input_paths[0], plan.output_path)
    except DiskSpaceError:
        raise
    except OSError as exc:
        LOGGER.exception("as-is relocate failed for %s", plan.source_path)
        return ConversionResult(plan=plan, outcome=Outcome.FAILED, message=f"relocate failed: {exc}")

    if not result.verified:
        return ConversionResult(
            plan=plan,
            outcome=Outcome.FAILED,
            message=f"copy could not be verified, original kept: {result.message}",
        )

    warnings: list[str] = []
    if not result.source_removed:
        warnings.append(result.message)

    _stamp_done(result.destination, warnings)

    emit(Stage.COMPLETE, f"relocated to {result.destination.name}")
    return ConversionResult(
        plan=plan,
        outcome=Outcome.RELOCATED,
        message=f"relocated to {result.destination.name}",
        output_path=result.destination,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# downcode / drama / remux
# --------------------------------------------------------------------------- #


def _execute_conversion(
    plan: Plan, config: Config, emit: Callable[..., None]
) -> ConversionResult:
    assert plan.output_path is not None and plan.temp_path is not None

    warnings: list[str] = list(plan.warnings)
    sidecars: list[Path] = []

    try:
        emit(Stage.PREPARING, "checking space and preparing inputs")
        ffmpeg.check_free_space(plan.output_path.parent, plan.estimated_output_bytes)

        # A leftover temporary file from an interrupted run would otherwise make
        # ffmpeg's -y overwrite semantics the only thing standing between us and
        # a half-converted file being validated.
        archive.remove_quietly(plan.temp_path)

        if plan.is_folder:
            sidecars = _write_folder_sidecars(plan)

        command = _build_command(plan, sidecars)
        LOGGER.info("converting %s -> %s", plan.source_path.name, plan.output_path.name)
        LOGGER.debug("ffmpeg command: %s", " ".join(command))

        emit(Stage.CONVERTING, f"encoding {plan.target}")

        def on_progress(progress: Progress) -> None:
            emit(Stage.CONVERTING, "", progress)

        result = ffmpeg.run_with_progress(command, plan.total_duration or 0.0, on_progress)
        if not result.ok:
            detail = result.stderr.strip() or "ffmpeg reported no detail"
            LOGGER.error("ffmpeg failed for %s: %s", plan.source_path, detail)
            archive.remove_quietly(plan.temp_path)
            return ConversionResult(
                plan=plan,
                outcome=Outcome.FAILED,
                message=f"ffmpeg failed: {_truncate(detail)}",
                warnings=warnings,
            )

        emit(Stage.VALIDATING, "verifying the converted file")
        verdict = validate_mod.validate(plan, plan.temp_path)
        warnings.extend(verdict.warnings)
        if not verdict.ok:
            archive.remove_quietly(plan.temp_path)
            return ConversionResult(
                plan=plan,
                outcome=Outcome.FAILED,
                message=f"validation failed: {verdict.summary()}",
                warnings=warnings,
            )

        emit(Stage.TAGGING, "copying metadata and cover art")
        warnings.extend(_copy_metadata(plan))

        emit(Stage.PROMOTING, f"moving into place as {plan.output_path.name}")
        final_path = archive.promote(plan.temp_path, plan.output_path)

        # The loop guard.  -map_metadata copied the source's disposition onto the
        # output; left alone, the output would be converted again if it ever
        # appeared in a scanned tree.
        _stamp_done(final_path, warnings)

        archived_path = None
        if plan.archive_path is not None:
            emit(Stage.ARCHIVING, "filing the original")
            try:
                # A folder book's original is the whole directory, not its first
                # track.  Archiving only input_paths[0] would strand the rest of
                # the tracks in the source tree, half-archived.
                original = plan.source_path if plan.is_folder else plan.input_paths[0]
                archived_path = archive.archive_original(original, plan.archive_path)
            except DiskSpaceError:
                raise
            except OSError as exc:
                # The conversion succeeded and is in place; failing to archive is
                # untidy but not a data loss, so it is a warning, not a failure.
                LOGGER.exception("could not archive %s", plan.source_path)
                warnings.append(f"converted successfully but could not archive the original: {exc}")

        emit(Stage.COMPLETE, f"converted to {final_path.name}")
        return ConversionResult(
            plan=plan,
            outcome=Outcome.CONVERTED,
            message=f"converted to {final_path.name}",
            output_path=final_path,
            archived_path=archived_path,
            warnings=warnings,
        )

    except (DiskSpaceError, ForcedTermination):
        archive.remove_quietly(plan.temp_path)
        raise
    except ForgeError as exc:
        archive.remove_quietly(plan.temp_path)
        LOGGER.exception("conversion failed for %s", plan.source_path)
        return ConversionResult(
            plan=plan, outcome=Outcome.FAILED, message=str(exc), warnings=warnings
        )
    except Exception as exc:  # one bad book must not stop the batch
        archive.remove_quietly(plan.temp_path)
        LOGGER.exception("unexpected failure converting %s", plan.source_path)
        return ConversionResult(
            plan=plan,
            outcome=Outcome.FAILED,
            message=f"unexpected error: {_truncate(str(exc))}",
            warnings=warnings,
        )
    finally:
        for sidecar in sidecars:
            archive.remove_quietly(sidecar)


def _build_command(plan: Plan, sidecars: list[Path]) -> list[str]:
    """Build the ffmpeg command for *plan*."""

    assert plan.temp_path is not None
    command = ["ffmpeg", "-hide_banner", "-y", *ffmpeg.progress_flags()]

    if plan.is_folder:
        concat_path, metadata_path = sidecars
        command += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_path),
            "-i", str(metadata_path),
            "-map", "0:a:0",
            "-map_metadata", "1",
            "-map_chapters", "1",
        ]
    else:
        command += [
            "-i", str(plan.input_paths[0]),
            "-map", "0:a:0",
            "-map_metadata", "0",
            "-map_chapters", "0",
        ]

    # Cover art is copied afterwards with mutagen rather than mapped through
    # ffmpeg: an attached picture stream confuses some muxers, and a failed
    # cover copy should not fail the conversion.
    command += ["-vn"]

    if plan.disposition is Disposition.REMUX:
        command += ["-c:a", "copy"]
    else:
        assert plan.codec and plan.target.bitrate_kbps and plan.target.channels
        command += [
            "-c:a", plan.codec,
            "-b:a", f"{plan.target.bitrate_kbps}k",
            "-ac", str(plan.target.channels),
        ]

    command += ["-f", "mp4", str(plan.temp_path)]
    return command


def _copy_metadata(plan: Plan) -> list[str]:
    """Re-apply the source's tags and cover onto the converted file.

    ``-map_metadata`` only carries the atoms ffmpeg's mp4 muxer knows about, so
    every custom ``----:com.apple.iTunes:`` atom - series, series-part, ASIN,
    subtitle, publisher, language, explicit - is dropped on the floor during
    conversion.  Writing them back with mutagen is what makes a converted book
    look like the one that went in.  Cover art goes in the same save: an
    attached picture stream confuses some muxers, so it is never mapped through
    ffmpeg, and one container rewrite is cheaper than two.
    """

    assert plan.temp_path is not None
    warnings: list[str] = []

    try:
        source_meta = tags.read_metadata(plan.input_paths[0], with_cover=True)
    except ForgeError as exc:
        return [f"could not read the source tags, so none were copied: {exc}"]

    # A folder book is many tracks becoming one file, so track 1's track and
    # disc numbers do not describe the result, and its track title ("01", or a
    # chapter name) is not the book's title.  The ffmetadata sidecar titles the
    # output after the album; keep that.
    updates = tags.carryover_updates(source_meta, include_track=not plan.is_folder)
    if plan.is_folder and source_meta.album:
        updates["title"] = source_meta.album

    cover: object | None = None
    if source_meta.cover_data:
        from mutagen.mp4 import MP4Cover

        mime = source_meta.cover_mime or "image/jpeg"
        if mime == "image/png":
            cover = MP4Cover(source_meta.cover_data, imageformat=MP4Cover.FORMAT_PNG)
        elif mime == "image/jpeg":
            cover = MP4Cover(source_meta.cover_data, imageformat=MP4Cover.FORMAT_JPEG)
        else:
            warnings.append(f"cover art is {mime}, which an m4b cannot hold; it was not copied")

    if not updates and cover is None:
        return warnings

    try:
        audio = tags.open_audio(plan.temp_path)
        tags.ensure_tags(audio)
        if updates:
            tags.apply_updates(audio, updates)
        if cover is not None:
            audio.tags["covr"] = [cover]
        audio.save()
    except Exception as exc:
        # The audio itself is fine and already validated; losing the tags is
        # worth a loud warning but not worth discarding the conversion.
        LOGGER.warning("could not copy tags to %s: %s", plan.temp_path.name, exc)
        warnings.append(f"tags and cover art could not be copied onto the output: {exc}")

    return warnings


def _stamp_done(path: Path, warnings: list[str]) -> None:
    """Mark *path* as a finished output so it is never reprocessed."""

    try:
        disposition_mod.write(path, Disposition.DONE, in_place=True)
    except ForgeError as exc:
        # Serious enough to surface: without the stamp, this output could be
        # converted a second time if it ever lands in a scanned directory.
        LOGGER.error("could not stamp %s as done: %s", path, exc)
        warnings.append(
            f"could not mark the output as done ({exc}); "
            f"re-tag it before placing it in a scanned directory"
        )


def _handle_invalid(plan: Plan, *, dry_run: bool, mark: bool) -> ConversionResult:
    """An actionable disposition that cannot be carried out."""

    reason = "; ".join(plan.errors) or "cannot be converted"
    if dry_run:
        return ConversionResult(
            plan=plan,
            outcome=Outcome.NEEDS_ATTENTION,
            message=f"dry run: would mark needs-attention ({reason})",
        )

    retagged = False
    if mark and plan.disposition is not Disposition.NEEDS_ATTENTION:
        try:
            if plan.is_folder:
                disposition_mod.write_folder(list(plan.input_paths), Disposition.NEEDS_ATTENTION)
            else:
                disposition_mod.write(plan.input_paths[0], Disposition.NEEDS_ATTENTION)
            retagged = True
        except ForgeError as exc:
            LOGGER.error("could not mark %s as needs-attention: %s", plan.source_path, exc)

    LOGGER.warning("%s needs attention: %s", plan.source_path.name, reason)
    return ConversionResult(
        plan=plan,
        outcome=Outcome.NEEDS_ATTENTION,
        message=reason,
        retagged=retagged,
    )


# --------------------------------------------------------------------------- #
# Folder-book sidecars
# --------------------------------------------------------------------------- #


def _write_folder_sidecars(plan: Plan) -> list[Path]:
    """Write the concat list and chapter metadata a folder book needs."""

    assert plan.temp_path is not None
    stem = plan.temp_path.with_suffix("")
    concat_path = stem.with_name(f"{stem.name}.concat.txt")
    metadata_path = stem.with_name(f"{stem.name}.ffmetadata.txt")

    ordered = list(plan.chapters)
    lines = ["ffconcat version 1.0"]
    for path in _concat_order(plan):
        lines.append(f"file '{_escape_concat(path)}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    metadata_path.write_text(_ffmetadata_text(plan, ordered), encoding="utf-8")
    return [concat_path, metadata_path]


def _concat_order(plan: Plan) -> list[Path]:
    """The input paths in the order the chapters were built from."""

    by_stem = {path.stem: path for path in plan.input_paths}
    ordered: list[Path] = []
    for chapter in plan.chapters:
        match = by_stem.get(chapter.title)
        if match is not None:
            ordered.append(match)
    if len(ordered) == len(plan.input_paths):
        return ordered
    # Chapter titles came from tags rather than filenames, so fall back to the
    # planner's own ordering of the inputs.
    return list(plan.input_paths)


def _escape_concat(path: Path) -> str:
    resolved = str(path.resolve())
    return resolved.replace("\\", "\\\\").replace("'", r"'\''")


def _ffmetadata_text(plan: Plan, chapters: list[Chapter]) -> str:
    lines = [";FFMETADATA1"]

    meta = None
    try:
        meta = tags.read_metadata(plan.input_paths[0])
    except ForgeError:
        LOGGER.debug("could not read folder metadata from %s", plan.input_paths[0])

    if meta is not None:
        for key, value in (
            ("title", meta.album or meta.title),
            ("album", meta.album),
            ("artist", meta.author or meta.albumartist),
            ("album_artist", meta.albumartist or meta.author),
            ("composer", meta.narrator),
            ("genre", tags.format_genres(meta.genres)),
            ("date", meta.published_date),
            ("publisher", meta.publisher),
            ("copyright", meta.copyright),
            ("description", meta.description),
        ):
            if value:
                lines.append(f"{key}={_escape_ffmetadata(str(value))}")

    for chapter in chapters:
        start_ms = max(0, round(chapter.start_seconds * 1000))
        end_ms = max(start_ms + 1, round(chapter.end_seconds * 1000))
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={_escape_ffmetadata(chapter.title)}",
        ]

    return "\n".join(lines) + "\n"


def _escape_ffmetadata(value: str) -> str:
    out = value
    for char in ("\\", "=", ";", "#"):
        out = out.replace(char, f"\\{char}")
    return out.replace("\n", "\\\n")


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_ERROR_CHARS:
        return collapsed
    return collapsed[:MAX_ERROR_CHARS] + "… (see the log for the full message)"
