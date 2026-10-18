"""The disposition tag: what the user decided should happen to a book.

Disposition is stored inside the audio file, as
``----:com.apple.iTunes:FORGE_DISPOSITION`` (MP4) or ``TXXX:FORGE_DISPOSITION``
(ID3).  Attended runs write it; unattended runs read it and act.

Three rules govern everything here:

1.  **Absence means skip.**  An untagged book is untriaged, never a candidate.
2.  **An unrecognised value means skip.**  A disposition forge does not
    understand is treated as :data:`Disposition.HOLD`, never as a default to
    process.  A future version adding a value cannot make an older version
    destructive.
3.  **Intent only.**  A disposition records *what* should happen, not the
    parameters.  Bitrate and channels are computed from the rules at conversion
    time, so retuning the rules applies to every already-triaged book.

:data:`Disposition.DELETE` is the one value that destroys something, and it
obeys the same three rules: nothing is deleted while triage is running, the tag
can be cleared again right up to the moment the run reads it, and a version of
forge that predates the value treats it as ``hold``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import TagError
from .tags import open_audio, read_freeform, safe_save, write_freeform

LOGGER = logging.getLogger(__name__)

#: The tag name, identical in both containers apart from the storage mechanism.
TAG_NAME = "FORGE_DISPOSITION"


class Disposition(StrEnum):
    """What forge should do with a book."""

    UNTRIAGED = "untriaged"
    """No tag present.  Never written to a file."""

    DOWNCODE = "downcode"
    """Re-encode using the standard rules."""

    DRAMA = "drama"
    """Re-encode using the dramatic-audio rules, preserving stereo."""

    REMUX = "remux"
    """Change container to m4b without re-encoding the audio."""

    AS_IS = "as-is"
    """Already correct; relocate to the output directory unchanged."""

    HOLD = "hold"
    """Looked at, deliberately left alone."""

    DELETE = "delete"
    """Throw this book away: the file, or the folder and everything in it.

    The one disposition with no undo.  Marking a book costs nothing - the tag
    is as reversible as any other - but the next ``forge run`` removes the
    files outright, with no archived copy behind them, which is why it is kept
    off the numbered part of the triage menu.
    """

    NEEDS_ATTENTION = "needs-attention"
    """forge tried and could not proceed.  Written by the executor."""

    DONE = "done"
    """Written onto conversion outputs so they are never reprocessed."""

    @property
    def is_actionable(self) -> bool:
        """True when ``forge run`` should act on a book carrying this value."""

        return self in _ACTIONABLE

    @property
    def destroys(self) -> bool:
        """True when acting on this disposition removes files for good.

        Callers that reason about "what will this run produce" need to tell a
        book being converted from one being thrown away: there is no target
        bitrate, no output name and no archive copy for a deletion.
        """

        return self is Disposition.DELETE

    @property
    def converts(self) -> bool:
        """True when acting on this disposition runs the audio through ffmpeg."""

        return self in (Disposition.DOWNCODE, Disposition.DRAMA, Disposition.REMUX)

    @property
    def re_encodes(self) -> bool:
        """True when the audio is re-encoded rather than copied."""

        return self in (Disposition.DOWNCODE, Disposition.DRAMA)

    @property
    def label(self) -> str:
        """Short display form for the triage list."""

        return "—" if self is Disposition.UNTRIAGED else str(self)


_ACTIONABLE = frozenset(
    {
        Disposition.DOWNCODE,
        Disposition.DRAMA,
        Disposition.REMUX,
        Disposition.AS_IS,
        Disposition.DELETE,
    }
)

#: Values a user may assign during triage, in menu order.  The triage menu
#: numbers these 1..n, so :data:`Disposition.DELETE` is deliberately not here:
#: a value that deletes files should not be one key away from one that encodes
#: them.
ASSIGNABLE: tuple[Disposition, ...] = (
    Disposition.DOWNCODE,
    Disposition.DRAMA,
    Disposition.REMUX,
    Disposition.AS_IS,
    Disposition.HOLD,
)

#: Assignable, but held back from the numbered menu and reached by a key of
#: its own.  Separate rather than simply last, because "the fifth choice" is a
#: slip away from "the sixth".
GUARDED: tuple[Disposition, ...] = (Disposition.DELETE,)

#: Everything a user may assign, for the non-interactive paths that accept a
#: value typed out in full and have no menu to mis-press.
SETTABLE: tuple[Disposition, ...] = ASSIGNABLE + GUARDED

_BY_VALUE = {str(member).casefold(): member for member in Disposition}
#: Spellings accepted on read so a hand-typed tag still works.
_ALIASES = {
    "as_is": Disposition.AS_IS,
    "asis": Disposition.AS_IS,
    "needs_attention": Disposition.NEEDS_ATTENTION,
    "needsattention": Disposition.NEEDS_ATTENTION,
    "dramatic": Disposition.DRAMA,
}


def parse(value: str | None) -> Disposition:
    """Parse a raw tag value into a :class:`Disposition`.

    An empty value is :data:`Disposition.UNTRIAGED`; anything unrecognised is
    :data:`Disposition.HOLD`, so forge never processes a book it cannot
    confidently classify.
    """

    if value is None:
        return Disposition.UNTRIAGED
    text = str(value).strip().casefold()
    if not text:
        return Disposition.UNTRIAGED
    if text == str(Disposition.UNTRIAGED):
        return Disposition.UNTRIAGED
    found = _BY_VALUE.get(text) or _ALIASES.get(text.replace("-", "_"))
    if found is not None:
        return found
    LOGGER.warning("unrecognised disposition %r; treating as %s", value, Disposition.HOLD)
    return Disposition.HOLD


def read(path: Path | str) -> Disposition:
    """Read the disposition from a single file."""

    audio = open_audio(path)
    return parse(read_freeform(audio, TAG_NAME))


def write(path: Path | str, value: Disposition, *, in_place: bool = False) -> None:
    """Write *value* to a single file, or clear the tag for ``UNTRIAGED``."""

    raw = None if value is Disposition.UNTRIAGED else str(value)

    def mutate(audio: object) -> None:
        write_freeform(audio, TAG_NAME, raw)

    safe_save(path, mutate, in_place=in_place)


@dataclass(frozen=True)
class FolderDisposition:
    """The disposition of a folder book, which lives across several files.

    ``conflict`` is true when the files disagree.  A conflicted folder is never
    processed: forge cannot know which of two intents the user meant, and
    guessing is exactly what this design exists to prevent.
    """

    value: Disposition
    conflict: bool = False
    per_file: tuple[tuple[str, Disposition], ...] = ()

    @property
    def is_actionable(self) -> bool:
        return not self.conflict and self.value.is_actionable

    @property
    def label(self) -> str:
        return "conflict" if self.conflict else self.value.label


def read_folder(paths: list[Path] | tuple[Path, ...]) -> FolderDisposition:
    """Read the shared disposition of a folder book, requiring unanimity."""

    per_file: list[tuple[str, Disposition]] = []
    for path in paths:
        try:
            per_file.append((str(path), read(path)))
        except TagError as exc:
            LOGGER.warning("could not read disposition from %s: %s", path, exc)
            per_file.append((str(path), Disposition.UNTRIAGED))

    distinct = {value for _, value in per_file}
    if not distinct:
        return FolderDisposition(Disposition.UNTRIAGED, per_file=())
    if len(distinct) == 1:
        return FolderDisposition(distinct.pop(), per_file=tuple(per_file))
    return FolderDisposition(Disposition.HOLD, conflict=True, per_file=tuple(per_file))


def write_folder(
    paths: list[Path] | tuple[Path, ...],
    value: Disposition,
    *,
    in_place: bool = False,
) -> list[tuple[Path, Exception]]:
    """Write *value* to every file of a folder book.

    Returns the files that could not be written.  A partial write leaves the
    folder conflicted rather than silently half-tagged, which triage surfaces.
    """

    failures: list[tuple[Path, Exception]] = []
    for path in paths:
        try:
            write(path, value, in_place=in_place)
        except TagError as exc:
            LOGGER.error("could not write disposition to %s: %s", path, exc)
            failures.append((Path(path), exc))
    return failures
