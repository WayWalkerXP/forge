"""Filename and path helpers.

Output names are built from tag values, which means they contain whatever a
publisher or ripper decided to put in a tag: slashes, colons, trailing dots,
emoji, and occasionally 400 characters of subtitle.  Everything here exists to
turn that into a name that survives Linux, Windows, SMB shares, and the trip
through Audiobookshelf.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: Illegal on Windows and troublesome on SMB shares even when the server is Linux.
INVALID_CHARS = '<>:"/\\|?*\0'

WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

#: Leaves room for a " (12)" collision suffix and a ".forge-tmp" prefix inside
#: the 255-byte limit most filesystems impose on a single component.
MAX_COMPONENT_LENGTH = 180


def sanitize_filename(value: str, *, fallback: str = "Untitled") -> str:
    """Make *value* safe to use as a single path component."""

    if value is None:
        return fallback

    cleaned = "".join(" " if char in INVALID_CHARS else char for char in str(value))
    cleaned = " ".join(cleaned.split())
    # Windows cannot address a name ending in a dot or space, and such names are
    # a nuisance to delete by hand even on Linux.
    cleaned = cleaned.rstrip(" .")

    if not cleaned:
        return fallback

    if cleaned.split(".")[0].upper() in WINDOWS_RESERVED:
        cleaned = f"{cleaned}_"

    if len(cleaned) > MAX_COMPONENT_LENGTH:
        cleaned = cleaned[:MAX_COMPONENT_LENGTH].rstrip(" .")

    return cleaned or fallback


def output_filename(author: str | None, title: str | None, *, suffix: str = ".m4b") -> str:
    """Build the ``Author - Title.m4b`` name used throughout the pipeline.

    The whole name is budgeted against :data:`MAX_COMPONENT_LENGTH`, not each
    half separately: a long author and a long title would otherwise combine into
    a name that exceeds the 255-byte component limit on ext4 and fails at write
    time.  The title is truncated first, since losing the end of a subtitle is
    less confusing than losing part of an author's name.
    """

    safe_author = sanitize_filename(author, fallback="Unknown Author")
    safe_title = sanitize_filename(title, fallback="Unknown Title")

    budget = MAX_COMPONENT_LENGTH - len(suffix) - len(" - ")
    if len(safe_author) + len(safe_title) <= budget:
        return f"{safe_author} - {safe_title}{suffix}"

    # Give the author at most half the budget, then let the title use the rest.
    author_budget = min(len(safe_author), max(20, budget // 2))
    safe_author = safe_author[:author_budget].rstrip(" .") or "Unknown Author"
    title_budget = max(1, budget - len(safe_author))
    safe_title = safe_title[:title_budget].rstrip(" .") or "Unknown Title"

    return f"{safe_author} - {safe_title}{suffix}"


def collision_safe_path(path: Path, *, reserved: set[Path] | None = None) -> Path:
    """Return *path*, or *path* with a ``(2)``, ``(3)``… suffix if taken.

    *reserved* holds names already claimed earlier in the same run, so a dry run
    predicts exactly the names a real run would produce.

    "Taken" ignores case, even though Linux does not: these trees are read over
    SMB and from Windows, where two names differing only in case cannot both be
    addressed - the client lists both and opens whichever the server matched
    first.  Writing one beside the other would hide a file rather than add it.
    """

    reserved = reserved or set()

    # One listing, not one per candidate: the loop below can ask a thousand
    # times and the answer cannot change while we hold the lock.
    siblings = {entry.name.casefold() for entry in _iterdir(path.parent)}
    reserved_folded = {
        candidate.name.casefold()
        for candidate in reserved
        if candidate.parent == path.parent
    }

    def taken(candidate: Path) -> bool:
        folded = candidate.name.casefold()
        return folded in siblings or folded in reserved_folded or candidate in reserved

    if not taken(path):
        return path

    # Directories are named whole: "Book 1.5" has a ".5" suffix as far as
    # pathlib is concerned, and splitting it would produce "Book 1 (2).5".
    if path.is_dir():
        stem, suffix = path.name, ""
    else:
        stem, suffix = path.stem, path.suffix

    for index in range(2, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not taken(candidate):
            return candidate

    raise OSError(f"could not find a free filename near {path}")


def _iterdir(directory: Path) -> list[Path]:
    """The contents of *directory*, or nothing when it is absent or unreadable."""

    try:
        return list(directory.iterdir())
    except OSError:
        return []


def temp_output_path(final_path: Path) -> Path:
    """The in-progress name for *final_path*.

    Lives beside the final output so promotion is a same-filesystem rename, and
    carries a ``.tmp`` stem that :func:`forge.scan.is_in_progress` recognises so
    a concurrent scan never picks it up.
    """

    return final_path.with_name(f"{final_path.stem}.tmp{final_path.suffix}")


def relative_to_or_name(path: Path, root: Path) -> Path:
    """Path of *path* relative to *root*, or just its name if it is outside.

    Used to mirror the source tree's shape into the archive, without letting a
    path outside the tree escape into ``../..`` territory.
    """

    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)
