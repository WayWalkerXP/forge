"""Filing finished books into the Audiobookshelf library.

The last step of the pipeline.  ``forge run`` produces a validated m4b in the
output directory; ``forge shelve`` reads its tags, asks the library whether that
edition is already shelved, tidies the two tags Audiobookshelf reads wrongly,
and moves the file to::

    <library>/<Author>/<Series>/<NN - Album>/<Album (year) [asin]>.m4b

Planned first, executed second, so ``--dry-run`` walks the same code as a real
run and predicts the same names - including the collision suffixes, since a
name claimed earlier in the run counts as taken.

**The naming is fixed, not a matter of taste.** The existing library was built
by ``absmove``, whose sanitiser replaced every illegal character with ``_``
(``Space: 1969`` is on disk as ``Space_ 1969``).  Anything that normalises
differently files the same author or series under a second folder and splits
books Audiobookshelf currently shows as one.  :func:`library_component` is
therefore a deliberate duplicate of ``paths.sanitize_filename`` rather than a
call to it; the two namespaces have different histories and must keep them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import authors as authors_mod
from . import library as library_mod
from . import tags as tags_mod
from .archive import move_into_library
from .config import Config
from .errors import ForgeError
from .history import History
from .paths import INVALID_CHARS, WINDOWS_RESERVED, collision_safe_path
from .scan import Book, book_key, is_in_progress

LOGGER = logging.getLogger(__name__)

#: Only finished books are shelved.  A folder of loose tracks is a conversion
#: input, not a library item.
SHELVEABLE_EXTENSIONS = (".m4b",)

_INVALID_RE = re.compile(f"[{re.escape(INVALID_CHARS)}\x01-\x1f]")

#: The real filesystem limit on one path component, not forge's narrower
#: budget for conversion outputs: the library already holds a 207-byte author
#: folder (a fourteen-author anthology), and capping shorter would file its
#: next book under a new, truncated name.
MAX_LIBRARY_COMPONENT = 255

#: Room for the " (2)" … " (999)" a collision adds to a filename.
_COLLISION_ALLOWANCE = 6

#: Credits Audible attaches to contributors who are not the book's author.
#: An explicit list rather than "drop anything after a dash", so a hyphenated
#: surname that arrives with stray spaces ("Mary Smith - Jones") survives.
AUTHOR_ROLE_SUFFIXES = (
    "editor",
    "translator",
    "narrator",
    "foreword",
    "introduction",
    "contributor",
    "illustrator",
)
_ROLE = r"(?:" + "|".join(AUTHOR_ROLE_SUFFIXES) + r")s?"
# One credit may stack roles: "- Translator and Editor", "- Translator/Editor".
_ROLE_LIST = rf"{_ROLE}(?:\s*(?:,|/|&|\band\b)\s*{_ROLE})*"
ROLE_SUFFIX_RE = re.compile(rf"\s+-\s+{_ROLE_LIST}[.\s]*$", re.IGNORECASE)
# A comma-separated credit ("Julia Fouche - Translator, Editor") leaves an entry
# that is nothing but a role word, which is not an author either.
BARE_ROLE_RE = re.compile(rf"^{_ROLE_LIST}[.\s]*$", re.IGNORECASE)
# "Tom Clancy aka Jim Jones" is one tag entry naming two authors.
AKA_RE = re.compile(r"\s+(?:aka|a\.k\.a\.)\s+", re.IGNORECASE)


class ShelveStatus(Enum):
    """What the plan says should happen to a book."""

    READY = "ready"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"


class ShelveOutcome(Enum):
    """What actually happened."""

    SHELVED = "shelved"
    SET_ASIDE = "set aside"
    SKIPPED = "skipped"
    FAILED = "failed"


#: Skip reasons worth repeating in full at the end of a run, because they mean
#: something went wrong rather than that a book simply is not ready.
ERROR_CATEGORIES = frozenset(
    {"unreadable tags", "library error", "tag write error", "move error"}
)


@dataclass(frozen=True)
class ShelvePlan:
    """What shelving one book would do."""

    source: Path
    status: ShelveStatus
    target: Path | None = None
    #: Tag edits to apply before the move, as ``write_metadata`` updates.
    tag_fixes: dict[str, object] = field(default_factory=dict)
    reason: str = ""
    category: str = ""
    duplicate_of: library_mod.LibraryItem | None = None
    book_key: str = ""

    @property
    def name(self) -> str:
        return self.source.name

    def describe(self) -> str:
        if self.status is ShelveStatus.READY:
            notes = ", ".join(self.fix_notes())
            return f"→ {self.target}" + (f"  ({notes})" if notes else "")
        if self.status is ShelveStatus.DUPLICATE:
            return f"duplicate — {self.reason}"
        return f"skip — {self.reason}"

    def fix_notes(self) -> list[str]:
        notes = []
        for key, value in sorted(self.tag_fixes.items()):
            if key == "track":
                notes.append("track tag removed")
            else:
                notes.append(f"{key} → {value!r}")
        return notes


@dataclass(frozen=True)
class ShelveResult:
    """The outcome of executing one plan."""

    plan: ShelvePlan
    outcome: ShelveOutcome
    destination: Path | None = None
    group_id: int | None = None
    message: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.plan.name

    @property
    def ok(self) -> bool:
        return self.outcome is not ShelveOutcome.FAILED

    @property
    def category(self) -> str:
        return self.plan.category


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def library_component(value: str | None, *, budget: int = MAX_LIBRARY_COMPONENT) -> str:
    """Make one path component safe, the way the existing library was built.

    Illegal characters become ``_`` - not a space, and runs are not collapsed.
    See the module docstring: this is matched to what is already on disk.
    """

    name = _INVALID_RE.sub("_", str(value or ""))
    name = name.strip().rstrip(". ")
    if not name:
        name = "_"
    if name.upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    # absmove had no length budget, so a 300-character subtitle produced a
    # name no filesystem would accept and the move failed outright.  Capping
    # at the filesystem's own limit changes nothing for a name that is already
    # writable, which is every name currently in the library.
    if len(name.encode("utf-8")) > budget:
        name = _truncate_bytes(name, budget).rstrip(". ") or "_"
    return name


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut *text* to *limit* bytes without splitting a character."""

    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def format_series_part(value: object) -> str:
    """Zero-pad a series part to two digits so folders sort correctly."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return f"{int(text):02d}"
    except ValueError:
        # "1.5", "Prequel" - left alone rather than mangled.
        return text


def is_role_credit(name: str) -> bool:
    """True when a contributor name is a credit rather than an author.

    "Basil Creighton - Translator", "David Chadwick - editor", or the bare
    word "translator" left behind by a comma-separated credit.
    """

    entry = name.strip()
    return bool(ROLE_SUFFIX_RE.search(entry) or BARE_ROLE_RE.match(entry))


def clean_author(value: str | None) -> str:
    """Drop role-credited contributors and split "X aka Y" into both authors.

    Returns the original value when cleaning would leave nothing at all, so a
    book credited only to its editor still reports an author rather than none.
    """

    if not value or not value.strip():
        return value or ""

    authors: list[str] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if is_role_credit(entry):
            continue
        for name in AKA_RE.split(entry):
            name = name.strip()
            if name and name not in authors:
                authors.append(name)

    return ", ".join(authors) if authors else value


def _year_of(meta: tags_mod.BookMetadata) -> str:
    if meta.published_year:
        return str(meta.published_year)
    match = re.match(r"(\d{4})", str(meta.published_date or ""))
    return match.group(1) if match else ""


def library_path(
    root: Path,
    meta: tags_mod.BookMetadata,
    *,
    suffix: str = ".m4b",
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> Path:
    """Where *meta* belongs under *root*.

    ``Author/Series/NN - Album/Album (year) [asin].m4b``, with the series
    folder, the part prefix, the year and the ASIN each dropped when absent.
    """

    author = clean_author(meta.author or meta.albumartist)
    if registry is not None:
        author = registry.resolve(author).canonical or author
    album = (meta.album or meta.title or "").strip()
    series = (meta.series or "").strip()
    part = format_series_part(meta.series_sequence)
    year = _year_of(meta)
    asin = (meta.asin or "").strip()

    parts = [library_component(author)]
    if series:
        parts.append(library_component(series))
    parts.append(library_component(f"{part} - {album}" if part else album))

    filename = album
    if year:
        filename += f" ({year})"
    if asin:
        filename += f" [{asin}]"

    # The filename gets a smaller budget than a folder: the suffix and a
    # possible collision marker have to fit inside the same limit.
    budget = MAX_LIBRARY_COMPONENT - len(suffix.encode("utf-8")) - _COLLISION_ALLOWANCE
    return root.joinpath(*parts, library_component(filename, budget=budget) + suffix)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def existing_case(
    parent: Path,
    name: str,
    *,
    author_set: bool = False,
    fold_case: bool = True,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> str | None:
    """The on-disk spelling of a directory called *name*, ignoring spelling.

    Returns None when no such directory exists.  An exact match always wins;
    when two variants are already there (the library has a few, from before
    this existed) the sorted-first spelling is chosen, which is stable across
    runs and prefers the capitalised form.

    With *author_set*, two folders also match when they name the same people in
    a different order or spelling: ``Terry Maggert, J. N. Chaney`` is the same
    collaboration as ``J.N. Chaney, Terry Maggert`` and belongs on one shelf.
    Only the author level passes this, because only author names come in sets -
    a series called "Qi=MC^2, Book 2" must not be matched against anything.
    """

    try:
        entries = [entry.name for entry in parent.iterdir() if entry.is_dir()]
    except OSError:
        return None

    # The collaboration check comes before an exact match, because a book whose
    # own folder exists may still belong on a bigger shelf naming the same
    # people in another order: one book in "Rick Partlow, J.N. Chaney" against
    # thirteen in "J.N. Chaney, Rick Partlow".  Among candidates the fullest
    # shelf wins, which keeps the books that move to a minimum, and the name
    # breaks a tie so the answer does not depend on directory order.
    # A registry is required: without one there is no canonical spelling to
    # judge a candidate against, so folding by key alone would let a variant
    # spelling capture the books it is supposed to lose.
    if author_set and registry is not None:
        same = [
            entry
            for entry in entries
            if _same_collaboration(entry, name, registry=registry)
        ]
        if same:
            return _fullest(parent, same)

    if fold_case:
        # Before an exact match, because when two case variants both exist the
        # books have to end up in one of them, and the exact name is only one
        # of the two.  This is what merges "Earth's First StarFighter" with
        # "Earth's First Starfighter" rather than leaving each holding books
        # that Windows cannot reach.
        folded = name.casefold()
        variants = [entry for entry in entries if entry.casefold() == folded]
        if variants:
            return _fullest(parent, variants)

    if name in entries:
        return name
    return None


def _fullest(parent: Path, candidates: Sequence[str]) -> str:
    """The candidate holding the most books; the name breaks a tie.

    Fullest, so the fewest books have to move.  Alphabetical after that, which
    is stable between runs and tends to prefer the capitalised spelling.
    """

    return sorted(candidates, key=lambda entry: (-_shelf_size(parent / entry), entry))[0]


def _shelf_size(folder: Path) -> int:
    """How many books a shelf holds, for choosing between two of them."""

    try:
        return sum(
            1
            for path in folder.rglob("*")
            if path.suffix.casefold() in SHELVEABLE_EXTENSIONS
        )
    except OSError:
        return 0


def _same_collaboration(
    existing: str, wanted: str, *, registry: "authors_mod.AuthorRegistry | None"
) -> bool:
    """True when two shelf names differ only in the order of the same people.

    A difference in *spelling* does not qualify, even though both names reduce
    to the same key: "JN Chaney" must not keep hold of books whose canonical
    shelf is "J.N. Chaney", or the shelf could never be renamed.  So an
    existing folder qualifies only when it already spells every author the way
    the registry does - leaving order as the one thing it may differ by.
    """

    if authors_mod.shelf_key(existing) != authors_mod.shelf_key(wanted):
        return False
    if registry is None:  # pragma: no cover - the caller checks this first
        return False

    names = authors_mod.split_authors(existing)
    if not names:
        return False
    return all((registry.canonical_name(name) or name) == name for name in names)


def match_existing_case(
    root: Path,
    target: Path,
    *,
    fold_case: bool = True,
    fold_author_case: bool = True,
    fold_author_set: bool = True,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> Path:
    """Re-spell *target*'s directories to match ones already in the library.

    A book tagged "Funa" files into the existing ``FUNA/`` rather than beside
    it.  The library is read over SMB and from Windows, where two directories
    differing only in case cannot both be addressed: the client lists both and
    shows the contents of whichever the server matched first, so one of them
    becomes unreachable.  Four series in this library were split that way
    before this existed, with consecutive volumes in different folders.

    Author names are not normalised to do it - "eden Hudson", "FUNA" and
    "von Goethe" are all spelled the way their author spells them, and any rule
    that picked a capitalisation would be wrong often enough to make things
    worse.  Only an existing directory's spelling is reused, and only the
    directory components: the file name is left to
    :func:`forge.paths.collision_safe_path`, which compares without case.

    The two folds answer different questions and are controlled separately:

    *fold_case* keeps two folders differing only in case from ever existing at
    once, which is an SMB requirement rather than a preference.
    *fold_author_case* is the same thing for the author level alone, which the
    backfill switches off: an author has a canonical spelling to be renamed to,
    while a series or a book folder has nothing better to be than consistent
    with the folder already there.

    *fold_author_set* joins a collaboration filed in another *order* to the
    folder it already has - but only when that folder already spells every
    author canonically, so a variant spelling can still be renamed.
    """

    try:
        parts = target.relative_to(root).parts
    except ValueError:  # not under root; nothing to reconcile against
        return target

    resolved = root
    for index, part in enumerate(parts[:-1]):
        at_author_level = index == 0
        match = existing_case(
            resolved,
            part,
            fold_case=fold_author_case if at_author_level else fold_case,
            author_set=fold_author_set and at_author_level,
            registry=registry,
        )
        resolved = resolved / (match or part)
    return resolved / parts[-1]


def find_books(source: Path, *, exclude: Iterable[Path] = ()) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Return ``(candidates, in_progress)`` for the m4b files under *source*.

    Anything inside an excluded directory is ignored: the duplicates directory
    lives in the source tree by default, so without this a later run would pick
    those files back up and try to shelve them again.
    """

    excluded = [Path(path).resolve() for path in exclude]
    candidates: list[Path] = []
    in_progress: list[tuple[Path, str]] = []

    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in SHELVEABLE_EXTENSIONS:
            continue
        resolved = path.resolve()
        if any(directory == resolved or directory in resolved.parents for directory in excluded):
            continue
        if is_in_progress(path):
            in_progress.append((path, "a conversion is still writing it"))
            continue
        candidates.append(path)

    return candidates, in_progress


def as_book(path: Path) -> Book:
    """Wrap a path as a one-file Book, so settle detection can take it.

    ``settle.partition_settled`` works on anything with a ``files`` sequence,
    and reusing it means shelving waits for a file to stop changing by exactly
    the same rule a conversion does.
    """

    return Book(path=path, is_folder=False, files=[path])


def tag_fixes_for(
    meta: tags_mod.BookMetadata,
    *,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> dict[str, object]:
    """The tag edits a book needs before it goes into the library.

    Three of them, all for Audiobookshelf's benefit, because it indexes the
    author *tag* rather than the folder:

    *   role credits stripped, so "Mary Smith - Translator" does not become her
        own shelf;
    *   each remaining contributor written the way the registry says, so one
        author is one author however Amazon spelled them this time; and
    *   no track number, because a single-file book carrying ``trkn`` is shown
        as track 1 of a set.

    The canonical pass only renames contributors the registry can identify.  An
    unknown spelling is left exactly as it is: a tag forge cannot fully resolve
    is still a correct tag, and guessing is what this whole mechanism exists to
    avoid.
    """

    fixes: dict[str, object] = {}
    for field_name in ("author", "albumartist"):
        current = getattr(meta, field_name, None)
        if not current:
            continue
        cleaned = clean_author(current)
        if registry is not None:
            cleaned = registry.resolve(cleaned).canonical or cleaned
        if cleaned != current:
            fixes[field_name] = cleaned
    if meta.track is not None:
        fixes["track"] = None
    return fixes


def plan_book(
    path: Path,
    cfg: Config,
    *,
    client: library_mod.AudiobookshelfClient | None = None,
    reserved: set[Path] | None = None,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> ShelvePlan:
    """Decide what shelving *path* would do.  Never writes anything."""

    if cfg.library_dir is None:
        raise ForgeError("no library directory configured")

    key = book_key(path)
    try:
        meta = tags_mod.read_metadata(path)
    except ForgeError as exc:
        return ShelvePlan(
            source=path, status=ShelveStatus.SKIPPED, book_key=key,
            reason=f"tags could not be read: {exc}", category="unreadable tags",
        )

    author = clean_author(meta.author or meta.albumartist)
    if registry is not None:
        author = registry.resolve(author).canonical or author
    if not author.strip():
        return ShelvePlan(
            source=path, status=ShelveStatus.SKIPPED, book_key=key,
            reason="no author tag, so there is no shelf to file it on",
            category="missing author",
        )
    if not (meta.album or meta.title):
        return ShelvePlan(
            source=path, status=ShelveStatus.SKIPPED, book_key=key,
            reason="no album or title tag, so the book has no name",
            category="missing album",
        )

    if meta.asin and client is not None:
        check = client.check_asin(meta.asin)
        if check.status == library_mod.ERROR:
            # Not shelved and not skipped silently: without an answer there is
            # no way to tell a new book from a second copy, and guessing wrong
            # puts a duplicate in the library.
            return ShelvePlan(
                source=path, status=ShelveStatus.SKIPPED, book_key=key,
                reason=f"the library could not be checked: {check.message}",
                category="library error",
            )
        if check.is_duplicate:
            match = check.matches[0]
            return ShelvePlan(
                source=path, status=ShelveStatus.DUPLICATE, book_key=key,
                reason=f"ASIN {meta.asin} is already in the library: {match.summary}",
                category="duplicate ASIN",
                duplicate_of=match,
            )

    target = collision_safe_path(
        match_existing_case(
            cfg.library_dir,
            library_path(
                cfg.library_dir, meta, suffix=path.suffix.casefold(), registry=registry
            ),
            registry=registry,
        ),
        reserved=reserved,
    )
    return ShelvePlan(
        source=path,
        status=ShelveStatus.READY,
        target=target,
        tag_fixes=tag_fixes_for(meta, registry=registry),
        book_key=key,
    )


def plan_many(
    paths: Sequence[Path],
    cfg: Config,
    *,
    client: library_mod.AudiobookshelfClient | None = None,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> list[ShelvePlan]:
    """Plan a whole batch, so two books cannot be sent to the same name."""

    reserved: set[Path] = set()
    plans: list[ShelvePlan] = []
    for path in paths:
        plan = plan_book(path, cfg, client=client, reserved=reserved, registry=registry)
        if plan.target is not None:
            reserved.add(plan.target)
        plans.append(plan)
    return plans


@dataclass(frozen=True)
class IdentityPass:
    """What one identity-learning sweep managed."""

    looked_up: int = 0
    learned: int = 0
    already_known: int = 0
    no_asin: int = 0
    failed: int = 0

    def describe(self) -> str:
        parts = [f"{self.already_known} already known"]
        if self.looked_up:
            parts.append(f"{self.looked_up} looked up")
        if self.learned:
            parts.append(f"{self.learned} new spelling(s)")
        if self.no_asin:
            parts.append(f"{self.no_asin} without an ASIN")
        if self.failed:
            parts.append(f"{self.failed} could not be looked up")
        return ", ".join(parts)


def learn_identities(
    paths: Sequence[Path],
    registry: "authors_mod.AuthorRegistry",
    audible: object | None,
    *,
    progress: Callable[[Path, str], None] | None = None,
) -> IdentityPass:
    """Teach the registry who wrote these books, before anything is planned.

    One Audible call per book whose contributors are not all known already, and
    none at all for a book the registry has seen the authors of - so a second
    run over the same library is free and offline.

    A lookup that fails is not an error: the book shelves under the spelling it
    arrived with, which is what forge did before any of this existed.
    """

    looked_up = learned = known = no_asin = failed = 0

    for path in paths:
        try:
            meta = tags_mod.read_metadata(path)
        except ForgeError:
            continue

        names = authors_mod.split_authors(
            clean_author(meta.author or meta.albumartist)
        )
        if not names:
            continue
        if all(registry.identity(name) is not None for name in names):
            known += 1
            continue
        if not meta.asin or audible is None:
            no_asin += 1
            registry.note_unknown(names)
            continue

        try:
            contributors = audible.contributors(meta.asin)
        except Exception as exc:  # a lookup failure must not stop shelving
            LOGGER.debug("could not look up contributors for %s: %s", path.name, exc)
            failed += 1
            registry.note_unknown(names)
            continue

        looked_up += 1
        # Audible lists role-credited contributors as authors, ASIN and all
        # ("David Chadwick - editor").  An identity for one could never be
        # used, because clean_author drops those names before anything is
        # resolved, so learning it would only clutter the registry.
        contributors = tuple(
            c for c in contributors if not is_role_credit(c.name)
        )
        if not contributors:
            registry.note_unknown(names)
            continue
        learned += registry.learn(contributors)
        # A tag spelling that reduces to the same key as one of Audible's
        # contributors is now resolved.  One that does not - "Brandon Morris"
        # against Audible's "Brandon Q. Morris" - cannot be attached without
        # guessing, so it is recorded as a candidate for someone to settle.
        registry.note_unknown(
            [name for name in names if registry.identity(name) is None]
        )
        if progress is not None:
            progress(path, ", ".join(c.name for c in contributors))

    return IdentityPass(
        looked_up=looked_up,
        learned=learned,
        already_known=known,
        no_asin=no_asin,
        failed=failed,
    )


def make_client(cfg: Config) -> library_mod.AudiobookshelfClient | None:
    """The Audiobookshelf client, or None when it is not configured.

    Shelving works without it - a book simply goes into the library without the
    duplicate question being asked, which is what absmove did for a book with
    no ASIN anyway.
    """

    if not cfg.has_library:
        LOGGER.warning(
            "Audiobookshelf is not configured, so shelved books are not checked "
            "for duplicates"
        )
        return None
    return library_mod.AudiobookshelfClient(
        cfg.abs_url or "", cfg.abs_api_key or "", library_id=cfg.abs_library_id
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def execute(
    plan: ShelvePlan,
    cfg: Config,
    *,
    duplicates_dir: Path | None = None,
    dry_run: bool = False,
) -> ShelveResult:
    """Carry out one plan.

    Order matters: the tag fixes happen *before* the move, while the file is
    still in the output directory, so a failure leaves the book where it was
    rather than half-filed.
    """

    if plan.status is ShelveStatus.SKIPPED:
        return ShelveResult(plan=plan, outcome=ShelveOutcome.SKIPPED, message=plan.reason)

    if plan.status is ShelveStatus.DUPLICATE:
        destination = (duplicates_dir or cfg.duplicates_for(plan.source.parent)) / plan.source.name
        if dry_run:
            return ShelveResult(
                plan=plan, outcome=ShelveOutcome.SET_ASIDE,
                destination=collision_safe_path(destination), message=plan.reason,
            )
        try:
            moved = move_into_library(plan.source, destination)
        except (OSError, ForgeError) as exc:
            LOGGER.exception("could not set %s aside", plan.source)
            return ShelveResult(
                plan=_recategorise(plan, "move error"), outcome=ShelveOutcome.FAILED,
                message=f"could not set it aside: {exc}",
            )
        group_id = _record_move(
            cfg, plan, moved.destination,
            description=f"set aside as a duplicate of {plan.duplicate_of.asin if plan.duplicate_of else 'a shelved book'}",
        )
        return ShelveResult(
            plan=plan, outcome=ShelveOutcome.SET_ASIDE, destination=moved.destination,
            group_id=group_id, message=plan.reason,
        )

    assert plan.target is not None  # READY always has one
    warnings: list[str] = []

    if dry_run:
        return ShelveResult(
            plan=plan, outcome=ShelveOutcome.SHELVED, destination=plan.target,
            message="; ".join(plan.fix_notes()),
        )

    group_id: int | None = None
    if plan.tag_fixes:
        try:
            # Recorded before the move, so a move that fails afterwards still
            # leaves the tag edits undoable.
            group_id = _apply_tag_fixes(cfg, plan)
        except (OSError, ForgeError) as exc:
            LOGGER.exception("could not tidy the tags on %s", plan.source)
            return ShelveResult(
                plan=_recategorise(plan, "tag write error"), outcome=ShelveOutcome.FAILED,
                message=f"could not tidy its tags: {exc}",
            )

    try:
        moved = move_into_library(plan.source, plan.target)
    except (OSError, ForgeError) as exc:
        LOGGER.exception("could not shelve %s", plan.source)
        return ShelveResult(
            plan=_recategorise(plan, "move error"), outcome=ShelveOutcome.FAILED,
            message=f"could not move it into the library: {exc}", group_id=group_id,
        )

    if not moved.source_removed and moved.message:
        # Copied and verified, but the original is still there: not data loss,
        # but the next run would see the book twice.
        warnings.append(moved.message)

    group_id = _record_move(
        cfg, plan, moved.destination, description="shelved", group_id=group_id
    )
    _follow_the_file(cfg, plan.source, moved.destination, group_id)
    return ShelveResult(
        plan=plan, outcome=ShelveOutcome.SHELVED, destination=moved.destination,
        group_id=group_id, message="; ".join(plan.fix_notes()),
        warnings=tuple(warnings),
    )


def _recategorise(plan: ShelvePlan, category: str) -> ShelvePlan:
    return replace(plan, category=category)


def _apply_tag_fixes(cfg: Config, plan: ShelvePlan) -> int | None:
    """Write the fixes, recording them so ``forge undo`` can put them back."""

    current = tags_mod.read_metadata(plan.source)
    changes = tags_mod.diff_metadata(current, plan.tag_fixes)
    tags_mod.write_metadata(plan.source, plan.tag_fixes)
    if not changes:
        return None
    with History(cfg.db_path) as store:
        # Described as "shelved" rather than as the tag edit: the edit and the
        # move are one action to the user, and the group holds both.
        return store.record(
            plan.book_key, changes, file_path=plan.source,
            source="shelve", description="shelved",
        )


def _follow_the_file(cfg: Config, source: Path, destination: Path, group_id: int | None) -> None:
    """Re-point this group's recorded changes at where the book now lives.

    The tag edits were recorded against the output directory, which the book
    has just left.  Without this, ``forge history`` on the shelved file shows
    only the move, and an undo would write the old values to a path that no
    longer exists.
    """

    if group_id is None or source == destination:
        return
    try:
        with History(cfg.db_path) as store:
            store.relocate(source, destination, group_id=group_id)
    except Exception:
        LOGGER.warning("could not re-point the change log at %s", destination, exc_info=True)


def _record_move(
    cfg: Config,
    plan: ShelvePlan,
    destination: Path,
    *,
    description: str,
    group_id: int | None = None,
) -> int | None:
    """Log where a book went, in the same group as any tag fixes.

    Recorded under the field name ``path``, which ``history`` already lists as
    unrestorable - so the move shows up in ``forge history`` while ``forge
    undo`` leaves it alone instead of trying to write a tag called "path".
    """

    try:
        with History(cfg.db_path) as store:
            return store.record(
                plan.book_key,
                {"path": (str(plan.source), str(destination))},
                file_path=destination,
                source="shelve",
                description=description,
                group_id=group_id,
            )
    except Exception:
        # The book is already in the library; failing to write the log entry
        # must not turn a completed move into a reported failure.
        LOGGER.warning("shelved %s but could not record it", destination, exc_info=True)
        return group_id


def count_outcomes(results: Sequence[ShelveResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    return counts


def count_categories(results: Sequence[ShelveResult]) -> dict[str, int]:
    """Why books were not shelved, by category, most common first."""

    counts: dict[str, int] = {}
    for result in results:
        if result.outcome in (ShelveOutcome.SHELVED,):
            continue
        category = result.category or result.outcome.value
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
