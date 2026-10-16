"""Bring a library that already exists into line with the author registry.

``forge shelve`` files new books under a canonical author name.  Everything
shelved before that existed carries whatever spelling Amazon happened to
return, so one author can hold several shelves: 31 folders and 245 books for
J.N. Chaney here, plus folders differing only in case and collaborations filed
in two different orders.

This is the pass that reconciles them.  Four phases, in order, because each
needs the answers from the one before:

1.  **Survey.**  Read every book's author tag and ASIN.  Reads only.
2.  **Identify.**  One Audible lookup per *author spelling* that has no
    identity yet - not per book - so a library of 11,000 books costs as many
    requests as it has unknown authors.
3.  **Choose.**  Pick each author's canonical spelling by how often it appears
    in the library, which is the one vote worth having and the thing a single
    lookup cannot know.  A name set by hand with ``forge authors --rename`` is
    pinned and left alone.
4.  **Plan and apply.**  For each book, work out where it belongs now and move
    it there, tags first.

**Rerunning is the resume.**  No checkpoint file: a book already carrying its
canonical tags and sitting at its canonical path produces no action, so an
interrupted pass is continued by running it again.  That also makes the whole
thing safe to run while only half-decided - stop it, change a canonical name,
run it again.

What moves is the book's **folder contents**, not just its audio file.
Audiobookshelf writes ``metadata.json`` and ``cover.jpg`` beside each book, and
leaving those behind would strand them in a folder that is about to be pruned.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from . import authors as authors_mod
from . import tags as tags_mod
from .archive import move_into_library
from .config import Config
from .errors import ForgeError
from .history import History
from .paths import collision_safe_path
from .scan import book_key
from .shelve import (
    SHELVEABLE_EXTENSIONS,
    clean_author,
    is_role_credit,
    library_path,
    match_existing_case,
    tag_fixes_for,
)

LOGGER = logging.getLogger(__name__)

#: Files that travel with a book: Audiobookshelf's sidecars and its artwork.
TRAVELLING_SUFFIXES = frozenset({".json", ".jpg", ".jpeg", ".png", ".webp", ".nfo", ".txt"})

ProgressCallback = Callable[[int, int, Path], None]

#: Which tag fixes can be applied to a BookMetadata to preview the result.
_METADATA_FIELDS = frozenset(f.name for f in fields(tags_mod.BookMetadata))


@dataclass(frozen=True)
class BookSurvey:
    """One book as the library holds it now."""

    path: Path
    author_raw: str
    asin: str | None
    cleaned: str

    @property
    def folder(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class BackfillAction:
    """What one book needs, if anything."""

    source: Path
    target: Path
    tag_fixes: dict = field(default_factory=dict)
    travelling: tuple[Path, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def moves(self) -> bool:
        return self.source != self.target

    @property
    def retags(self) -> bool:
        return bool(self.tag_fixes)

    @property
    def wanted(self) -> bool:
        return self.moves or self.retags

    def describe(self) -> str:
        return "; ".join(self.reasons) or "nothing to do"


@dataclass(frozen=True)
class IdentityReport:
    """What the identify phase managed."""

    spellings: int = 0
    already_known: int = 0
    looked_up: int = 0
    learned: int = 0
    no_asin: int = 0
    failed: int = 0

    def describe(self) -> str:
        parts = [
            f"{self.spellings} author spelling(s)",
            f"{self.already_known} already identified",
        ]
        if self.looked_up:
            parts.append(f"{self.looked_up} looked up")
        if self.learned:
            parts.append(f"{self.learned} newly identified")
        if self.no_asin:
            parts.append(f"{self.no_asin} with no ASIN to look up")
        if self.failed:
            parts.append(f"{self.failed} lookup(s) failed")
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
# 1. survey
# --------------------------------------------------------------------------- #


def find_books(library: Path) -> list[Path]:
    """Every audio file in the library, in a stable order."""

    return sorted(
        path
        for path in library.rglob("*")
        if path.is_file() and path.suffix.casefold() in SHELVEABLE_EXTENSIONS
    )


def survey(
    library: Path, *, progress: ProgressCallback | None = None
) -> tuple[list[BookSurvey], list[tuple[Path, str]]]:
    """Read the author tag of every book.  Writes nothing."""

    paths = find_books(library)
    surveyed: list[BookSurvey] = []
    unreadable: list[tuple[Path, str]] = []

    for index, path in enumerate(paths, 1):
        if progress is not None:
            progress(index, len(paths), path)
        try:
            meta = tags_mod.read_metadata(path)
        except ForgeError as exc:
            unreadable.append((path, str(exc)))
            continue
        raw = (meta.author or meta.albumartist or "").strip()
        if not raw:
            unreadable.append((path, "no author tag"))
            continue
        surveyed.append(
            BookSurvey(
                path=path,
                author_raw=raw,
                asin=(meta.asin or "").strip() or None,
                cleaned=clean_author(raw),
            )
        )

    return surveyed, unreadable


# --------------------------------------------------------------------------- #
# 2. identify
# --------------------------------------------------------------------------- #


def unidentified_spellings(
    surveyed: Sequence[BookSurvey], registry: authors_mod.AuthorRegistry
) -> dict[str, list[BookSurvey]]:
    """Author names with no identity, each with the books that carry them.

    Keyed by the contributor name rather than by the whole tag, so a
    collaboration where one author is already known only asks about the other.
    """

    pending: dict[str, list[BookSurvey]] = defaultdict(list)
    for book in surveyed:
        for name in authors_mod.split_authors(book.cleaned):
            if registry.identity(name) is None:
                pending[name].append(book)
    return dict(pending)


def identify(
    surveyed: Sequence[BookSurvey],
    registry: authors_mod.AuthorRegistry,
    audible: object | None,
    *,
    progress: ProgressCallback | None = None,
) -> IdentityReport:
    """Ask Audible who the unidentified authors are, once per spelling.

    A spelling is looked up through one representative book that has an ASIN.
    The lookup names every contributor of that book, so one request can settle
    a whole collaboration.
    """

    pending = unidentified_spellings(surveyed, registry)
    total_spellings = len({
        name for book in surveyed for name in authors_mod.split_authors(book.cleaned)
    })
    looked_up = learned = no_asin = failed = 0
    seen_asins: set[str] = set()

    names = sorted(pending)
    for index, name in enumerate(names, 1):
        if registry.identity(name) is not None:
            continue  # settled by an earlier book's contributor list
        books = [book for book in pending[name] if book.asin]
        if not books:
            no_asin += 1
            registry.note_unknown([name])
            continue

        book = books[0]
        if progress is not None:
            progress(index, len(names), book.path)
        if audible is None or book.asin in seen_asins:
            no_asin += 1
            registry.note_unknown([name])
            continue

        seen_asins.add(book.asin)
        try:
            contributors = audible.contributors(book.asin)
        except Exception as exc:
            LOGGER.debug("could not look up %s for %r: %s", book.asin, name, exc)
            failed += 1
            registry.note_unknown([name])
            continue

        looked_up += 1
        usable = [c for c in contributors if not is_role_credit(c.name)]
        if usable:
            learned += registry.learn(usable)
        if registry.identity(name) is None:
            registry.note_unknown([name])

    return IdentityReport(
        spellings=total_spellings,
        already_known=total_spellings - len(pending),
        looked_up=looked_up,
        learned=learned,
        no_asin=no_asin,
        failed=failed,
    )


# --------------------------------------------------------------------------- #
# 3. choose a canonical spelling
# --------------------------------------------------------------------------- #


def spelling_votes(
    surveyed: Sequence[BookSurvey], registry: authors_mod.AuthorRegistry
) -> dict[str, Counter]:
    """How often each spelling of each identity appears in the library."""

    votes: dict[str, Counter] = defaultdict(Counter)
    for book in surveyed:
        for name in authors_mod.split_authors(book.cleaned):
            identity = registry.identity(name)
            if identity is not None:
                votes[identity][name] += 1
    return dict(votes)


def choose_canonical_names(
    surveyed: Sequence[BookSurvey], registry: authors_mod.AuthorRegistry
) -> list[tuple[str, str, str]]:
    """Set each author's canonical spelling by how often the library uses it.

    Returns the changes made, as (identity, from, to).  Also records each
    author's book count, which this is the only pass in a position to know.

    Frequency is the only defensible vote: Amazon's own display name for an
    author varies between products, so the first lookup to arrive is no more
    authoritative than the last.  Ties break towards the longer spelling and
    then alphabetically, which prefers "J. N. Chaney" over "JN Chaney" and is
    at least stable between runs.
    """

    votes = spelling_votes(surveyed, registry)
    # The counts are wanted beyond this function: the review interface sorts by
    # them, and counting them again would mean re-reading the whole library.
    registry.set_book_counts(
        {identity: sum(counter.values()) for identity, counter in votes.items()}
    )

    changes: list[tuple[str, str, str]] = []
    for identity, counter in votes.items():
        if registry.is_pinned(identity):
            continue
        winner = sorted(counter.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))[0][0]
        current = registry.canonical_of(identity)
        if current is not None and winner != current:
            registry.set_canonical(identity, winner)
            changes.append((identity, current, winner))
    return changes


# --------------------------------------------------------------------------- #
# 4. plan and apply
# --------------------------------------------------------------------------- #


def travelling_files(book: Path) -> tuple[Path, ...]:
    """Audiobookshelf's sidecars beside a book, which move with it."""

    try:
        entries = sorted(book.parent.iterdir())
    except OSError:
        return ()
    return tuple(
        path
        for path in entries
        if path.is_file()
        and path != book
        and path.suffix.casefold() in TRAVELLING_SUFFIXES
    )


def plan_book(
    book: BookSurvey,
    cfg: Config,
    registry: authors_mod.AuthorRegistry,
    *,
    reserved: set[Path] | None = None,
) -> BackfillAction:
    """Where this book belongs now, and what its tags should say."""

    assert cfg.library_dir is not None
    meta = tags_mod.read_metadata(book.path)
    fixes = tag_fixes_for(meta, registry=registry)

    # The target is computed from the tags *after* the fixes, which is what
    # shelving a fresh copy of this book would do today.
    patched = replace(meta, **{
        name: value for name, value in fixes.items() if name in _METADATA_FIELDS
    }) if fixes else meta
    # fold_author_case is off at the author level only: this pass exists to
    # rename author folders, so one differing by case must not keep hold of the
    # books - the old one is emptied and pruned in the same run.  Deeper levels
    # keep folding, because a series folder has no canonical spelling to be
    # renamed to and consistency with what is there is the whole aim: it is
    # what merges "Earth's First StarFighter" with "Earth's First Starfighter".
    ideal = match_existing_case(
        cfg.library_dir,
        library_path(cfg.library_dir, patched, suffix=book.path.suffix.casefold()),
        fold_author_case=False,
        registry=registry,
    )
    target = ideal if ideal == book.path else collision_safe_path(ideal, reserved=reserved)

    reasons: list[str] = []
    if "author" in fixes or "albumartist" in fixes:
        reasons.append(f"author → {fixes.get('author') or fixes.get('albumartist')!r}")
    if "track" in fixes:
        reasons.append("stray track number removed")
    if target != book.path:
        if target.parent.parent != book.path.parent.parent:
            reasons.append(f"shelf → {target.relative_to(cfg.library_dir).parts[0]!r}")
        else:
            reasons.append("folder renamed")

    return BackfillAction(
        source=book.path,
        target=target,
        tag_fixes=fixes,
        travelling=travelling_files(book.path),
        reasons=tuple(reasons),
    )


def plan(
    surveyed: Sequence[BookSurvey],
    cfg: Config,
    registry: authors_mod.AuthorRegistry,
    *,
    progress: ProgressCallback | None = None,
) -> list[BackfillAction]:
    """Plan the whole library, so two books cannot be sent to one name."""

    reserved: set[Path] = set()
    actions: list[BackfillAction] = []
    for index, book in enumerate(surveyed, 1):
        if progress is not None:
            progress(index, len(surveyed), book.path)
        action = plan_book(book, cfg, registry, reserved=reserved)
        if action.moves:
            reserved.add(action.target)
        actions.append(action)
    return actions


def apply(action: BackfillAction, cfg: Config, *, dry_run: bool = False) -> str:
    """Carry out one action.  Returns a one-line account of what happened.

    Tags first, then the move, as shelving does: a failure then leaves the book
    where it was rather than half-filed.
    """

    if not action.wanted:
        return "already correct"
    if dry_run:
        return action.describe()

    key = book_key(action.source)
    group_id: int | None = None

    if action.tag_fixes:
        current = tags_mod.read_metadata(action.source)
        changes = tags_mod.diff_metadata(current, action.tag_fixes)
        tags_mod.write_metadata(action.source, action.tag_fixes)
        if changes:
            with History(cfg.db_path) as store:
                group_id = store.record(
                    key, changes, file_path=action.source,
                    source="backfill", description="author name reconciled",
                )

    if not action.moves:
        return action.describe()

    moved = move_into_library(action.source, action.target)
    for extra in action.travelling:
        destination = action.target.parent / extra.name
        if destination.exists():
            continue
        try:
            move_into_library(extra, destination)
        except (OSError, ForgeError):
            # A sidecar left behind is untidy, not harmful: Audiobookshelf
            # rewrites metadata.json on its next scan.
            LOGGER.warning("could not move %s alongside the book", extra, exc_info=True)

    try:
        with History(cfg.db_path) as store:
            group_id = store.record(
                key, {"path": (str(action.source), str(moved.destination))},
                file_path=moved.destination, source="backfill",
                description="moved to its canonical shelf", group_id=group_id,
            )
            if group_id is not None:
                store.relocate(action.source, moved.destination, group_id=group_id)
    except Exception:
        LOGGER.warning("moved %s but could not record it", moved.destination, exc_info=True)

    prune_empty(action.source.parent, stop_at=cfg.library_dir)
    return action.describe()


def prune_empty(folder: Path, *, stop_at: Path | None) -> int:
    """Remove a folder the books have left, and any empty parents above it."""

    removed = 0
    current = folder
    while stop_at is not None and current != stop_at and stop_at in current.parents:
        try:
            if any(current.iterdir()):
                break
            current.rmdir()
        except OSError:
            break
        removed += 1
        current = current.parent
    return removed


def summarise(actions: Iterable[BackfillAction]) -> dict[str, int]:
    actions = list(actions)
    return {
        "books": len(actions),
        "retag": sum(1 for a in actions if a.retags),
        "move": sum(1 for a in actions if a.moves),
        "correct": sum(1 for a in actions if not a.wanted),
    }


def shelves_before_and_after(
    actions: Sequence[BackfillAction], library: Path
) -> tuple[int, int]:
    """How many author shelves the library has now, and would have after."""

    def shelf(path: Path) -> str:
        try:
            return path.relative_to(library).parts[0]
        except ValueError:  # pragma: no cover - targets are always under it
            return str(path)

    return (
        len({shelf(a.source) for a in actions}),
        len({shelf(a.target) for a in actions}),
    )
