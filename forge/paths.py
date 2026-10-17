"""Path helpers: what a filename may contain, and finding a free one.

How a book's name is *composed* lives in :mod:`forge.naming`, which owns the
two configurable templates.  What is left here is the plumbing around it - the
character rules every name is held to, the collision suffix, and the
in-progress and archive paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: Illegal on Windows and troublesome on SMB shares even when the server is
#: Linux.  :mod:`forge.naming` builds every name forge writes and reads these
#: two from here, so there is one answer to "what may a filename contain".
INVALID_CHARS = '<>:"/\\|?*\0'

WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


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
