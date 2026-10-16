"""Moving files around without ever losing one.

Three operations, each refusing to overwrite:

:func:`promote`
    Temporary output -> final output.  Uses a hard link so the file appears at
    its destination atomically, with an exclusive-create copy as the fallback
    for cross-device moves.
:func:`archive_original`
    Source -> archive directory, after a conversion is validated.  Nothing is
    ever deleted; the archive is the undo of last resort.
:func:`safe_relocate`
    Source -> destination with a hash check before the source is removed.  Used
    by ``as-is``, where the file being moved *is* the product, so there is no
    archived copy to fall back on.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import DiskSpaceError, ForgeError
from .ffmpeg import is_disk_space_error
from .paths import collision_safe_path
from .validate import verify_copy

LOGGER = logging.getLogger(__name__)


class PromotionError(ForgeError):
    """A validated output could not be moved into its final place."""


@dataclass(frozen=True)
class RelocateResult:
    """The outcome of a verified relocate."""

    destination: Path
    verified: bool
    source_removed: bool
    message: str = ""


def promote(temp_path: Path, final_path: Path) -> Path:
    """Move a validated temporary output to *final_path* without overwriting.

    ``os.link`` is tried first: it either creates the destination or fails with
    ``EEXIST``, so there is no window in which a half-written file is visible
    under the final name.  When the link cannot be made - a different
    filesystem, or one that does not support links - the fallback opens the
    destination with ``"xb"``, which is also an exclusive create.

    Either way, an existing destination is never clobbered.
    """

    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists():
        raise PromotionError(f"refusing to overwrite an existing file: {final_path}")

    try:
        os.link(temp_path, final_path)
    except FileExistsError as exc:
        raise PromotionError(f"refusing to overwrite an existing file: {final_path}") from exc
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EMLINK):
            if is_disk_space_error(exc):
                raise DiskSpaceError(str(exc)) from exc
            raise PromotionError(f"could not link {temp_path.name} into place: {exc}") from exc

        LOGGER.debug("hard link unavailable for %s; copying instead", final_path)
        try:
            with open(temp_path, "rb") as src, open(final_path, "xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        except FileExistsError as exc:
            raise PromotionError(f"refusing to overwrite an existing file: {final_path}") from exc
        except OSError as copy_exc:
            # A partial destination is worse than none: remove it so the next
            # run does not mistake it for a finished output.
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("could not clean up partial output %s", final_path)
            if is_disk_space_error(copy_exc):
                raise DiskSpaceError(str(copy_exc)) from copy_exc
            raise PromotionError(f"could not copy {temp_path.name} into place: {copy_exc}") from copy_exc

    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("promoted %s but could not remove the temporary file", final_path.name)

    LOGGER.info("promoted %s", final_path)
    return final_path


def archive_original(source_path: Path, archive_path: Path) -> Path:
    """File *source_path* under *archive_path*, never overwriting.

    The archive mirrors the source tree's shape.  A name that is already taken
    gets a ``(2)`` suffix rather than replacing what is there - two different
    books can legitimately share a filename, and the archive is the only
    remaining copy of the original.
    """

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    destination = collision_safe_path(archive_path)

    try:
        shutil.move(str(source_path), str(destination))
    except OSError as exc:
        if is_disk_space_error(exc):
            raise DiskSpaceError(str(exc)) from exc
        raise

    LOGGER.info("archived %s -> %s", source_path.name, destination)
    return destination


def safe_relocate(source_path: Path, destination_path: Path, *, hash_check: bool = True) -> RelocateResult:
    """Copy *source_path* to *destination_path*, verify it, then remove the source.

    Copy-verify-delete rather than a plain move: a move across filesystems is a
    copy and a delete anyway, but without the check in between.  Here the source
    is only removed once the copy is proven byte-identical, because for an
    ``as-is`` book this file is the product and there is no archived copy behind
    it.
    """

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination = collision_safe_path(destination_path)

    try:
        shutil.copy2(source_path, destination)
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("could not clean up partial copy %s", destination)
        if is_disk_space_error(exc):
            raise DiskSpaceError(str(exc)) from exc
        raise

    failures = verify_copy(source_path, destination, hash_check=hash_check)
    if failures:
        # The copy is untrustworthy, so throw it away and keep the original.
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("verification failed and the bad copy %s remains", destination)
        return RelocateResult(
            destination=destination,
            verified=False,
            source_removed=False,
            message="; ".join(failures),
        )

    try:
        source_path.unlink()
    except OSError as exc:
        # The copy is good, so this is not a data-loss failure - but the source
        # still being there means a later run would see the book twice.
        LOGGER.error("copied and verified %s but could not remove the original: %s", source_path, exc)
        return RelocateResult(
            destination=destination,
            verified=True,
            source_removed=False,
            message=f"copy verified but the original could not be removed: {exc}",
        )

    LOGGER.info("relocated %s -> %s (verified)", source_path.name, destination)
    return RelocateResult(destination=destination, verified=True, source_removed=True)


def move_into_library(source_path: Path, destination_path: Path) -> RelocateResult:
    """Move *source_path* to *destination_path*, choosing the safest fast path.

    Within one filesystem - which the output directory and the library normally
    share - the file is moved by linking it into place and then unlinking the
    original: instant whatever its size, and with no window in which the data
    exists only as a half-written copy.  ``os.link`` is used rather than
    ``os.rename`` for the same reason :func:`promote` uses it - rename replaces
    an existing destination without a word, while link fails.

    Across filesystems there is no such operation, so this falls back to
    :func:`safe_relocate`, which copies, hashes both ends, and only then
    removes the original.

    Either way the destination is chosen with :func:`collision_safe_path`
    first, so an existing file is never replaced.
    """

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination = collision_safe_path(destination_path)

    try:
        os.link(source_path, destination)
    except FileExistsError as exc:
        # Something claimed the name between the check and the link.
        raise PromotionError(f"refusing to overwrite an existing file: {destination}") from exc
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EMLINK):
            if is_disk_space_error(exc):
                raise DiskSpaceError(str(exc)) from exc
            raise
        LOGGER.debug("cannot link %s into place; copying instead", destination)
        return safe_relocate(source_path, destination)

    try:
        source_path.unlink()
    except OSError as exc:
        # The book is in the library, but the original is still in the output
        # directory, so a later run would shelve it a second time.
        LOGGER.error("moved %s but could not remove the original: %s", destination, exc)
        return RelocateResult(
            destination=destination,
            verified=True,
            source_removed=False,
            message=f"moved into place but the original could not be removed: {exc}",
        )

    LOGGER.info("moved %s -> %s", source_path.name, destination)
    return RelocateResult(destination=destination, verified=True, source_removed=True)


def remove_quietly(path: Path | None) -> None:
    """Delete *path* if it exists, logging rather than raising on failure."""

    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if is_disk_space_error(exc):
            raise DiskSpaceError(str(exc)) from exc
        LOGGER.warning("could not remove %s: %s", path, exc)
