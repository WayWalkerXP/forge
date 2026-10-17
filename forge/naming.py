"""Where a book's name comes from, for both conversion and shelving.

One convention, two settings.  ``shelf_template`` names the directories a book
is filed under and ``filename_template`` names the file itself; ``forge run``
uses the filename alone, since a conversion output waits flat in the output
directory until ``forge shelve`` builds the tree.  Both come from the config so
that the pipeline has one answer to "what is this book called" rather than one
per stage - the old conversion name, ``Author - Title.m4b``, carried no series,
year or ASIN and collided whenever an author reused a title.

A template is literal text with ``<tag>`` placeholders::

    shelf_template    = <author>/<series>/<series_part> - <album>
    filename_template = <album> (<year>) [<asin>]

Three rules make the optional parts work, because most books have no series
and plenty have no ASIN:

* A parenthesised or bracketed run whose tags all come out empty is dropped
  whole, brackets included - ``(<year>)`` leaves nothing rather than ``()``.
* Literal glue touching an empty tag goes with it, so ``<series_part> - <album>``
  is just the album when the book is a standalone.
* A ``shelf_template`` directory that renders empty is skipped rather than
  becoming ``_``, which is what gives a standalone book no series folder.

**Templates are validated once, at startup, and a bad one is fatal.**  An
illegal character in the literal text, an unknown tag, an unclosed ``<`` or a
template that could only ever produce an empty name stops forge before a single
file moves.  Tag *values* are a different matter and are normalised, not
rejected: ``Space: 1969`` is a real album tag, and :func:`library_component`
replaces its colon with ``_`` exactly as the library was already built.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from . import authors as authors_mod
from . import tags as tags_mod
from . import titles as titles_mod
from .errors import BookError, ConfigError
from .paths import INVALID_CHARS, WINDOWS_RESERVED

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from .config import Config

LOGGER = logging.getLogger(__name__)

_INVALID_RE = re.compile(f"[{re.escape(INVALID_CHARS)}\x01-\x1f]")

#: The real filesystem limit on one path component, not forge's narrower
#: budget for conversion outputs: the library already holds a 207-byte author
#: folder (a fourteen-author anthology), and capping shorter would file its
#: next book under a new, truncated name.
MAX_LIBRARY_COMPONENT = 255

#: Room for the " (2)" … " (999)" a collision adds to a filename.
COLLISION_ALLOWANCE = 6

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


# --------------------------------------------------------------------------- #
# The tags a template may use
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TemplateTag:
    """One placeholder a template may contain."""

    name: str
    label: str
    example: str


#: Offered to the user in the sample config and by ``forge naming --tags``.
#: Deliberately a short list of the values that identify an edition: a template
#: is a filing rule, not a place to restate the description tag.
TEMPLATE_TAGS: tuple[TemplateTag, ...] = (
    TemplateTag("author", "Author", "David Eddings"),
    TemplateTag("album", "Album, the book's name", "Pawn of Prophecy"),
    TemplateTag("title", "Title tag, when it differs from the album", "Pawn of Prophecy"),
    TemplateTag("subtitle", "Subtitle", "The Belgariad, Book One"),
    TemplateTag("series", "Series", "The Belgariad"),
    TemplateTag("series_part", "Series part, zero-padded to two digits", "01"),
    TemplateTag("narrator", "Narrator", "Cameron Beierle"),
    TemplateTag("asin", "Audible ASIN", "B002V0QUOA"),
    TemplateTag("year", "Publication year", "1982"),
    TemplateTag("publisher", "Publisher", "Del Rey"),
    TemplateTag("language", "Language", "English"),
    TemplateTag("genre", "First genre listed", "Fantasy"),
)

TAG_NAMES = frozenset(tag.name for tag in TEMPLATE_TAGS)

#: Written into the config sample and used when a key is absent, so an existing
#: install keeps filing books exactly where it already files them.
DEFAULT_SHELF_TEMPLATE = "<author>/<series>/<series_part> - <album>"
DEFAULT_FILENAME_TEMPLATE = "<album> (<year>) [<asin>]"


def tag_help_lines() -> list[str]:
    """``<tag>  Label (example)`` for each available tag."""

    width = max(len(tag.name) for tag in TEMPLATE_TAGS) + 2
    return [
        f"{f'<{tag.name}>':<{width}} {tag.label} - e.g. {tag.example}"
        for tag in TEMPLATE_TAGS
    ]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Literal:
    text: str


@dataclass(frozen=True)
class _Tag:
    name: str


_Token = _Literal | _Tag

#: Characters a template's literal text may not contain.  ``<`` and ``>`` never
#: reach this check - the parser consumes them as tag delimiters - and ``/`` is
#: handled on its own, being a separator in a shelf template and an error in a
#: filename template.  Everything else would be illegal in a name however it
#: got there.
_ILLEGAL_LITERAL = frozenset(INVALID_CHARS) - {"/"}


@dataclass(frozen=True)
class Template:
    """A parsed, validated template."""

    source: str
    #: One group of tokens per path component.  A filename template has one.
    components: tuple[tuple[_Token, ...], ...]
    setting: str

    def tag_names(self) -> set[str]:
        return {
            token.name
            for component in self.components
            for token in component
            if isinstance(token, _Tag)
        }


def parse_template(text: str, *, setting: str, allow_slash: bool) -> Template:
    """Parse *text*, raising :class:`ConfigError` on anything unusable.

    *setting* only names the key in the error messages; a template is rejected
    with the setting it came from so the user knows which line to edit.
    """

    source = (text or "").strip()
    if not source:
        raise ConfigError(
            f"[naming] {setting} is empty; a template must name at least one "
            f"<tag>\n  available tags: "
            + ", ".join(f"<{t.name}>" for t in TEMPLATE_TAGS)
        )

    raw_components = source.split("/") if allow_slash else [source]
    if not allow_slash and "/" in source:
        raise ConfigError(
            f"[naming] {setting} must not contain '/': it names one file, not a "
            f"path. Put directories in shelf_template instead."
        )

    components: list[tuple[_Token, ...]] = []
    for raw in raw_components:
        component = raw.strip()
        if not component:
            raise ConfigError(
                f"[naming] {setting} has an empty path component (a '//' or a "
                f"leading or trailing '/'): {source!r}"
            )
        components.append(_parse_component(component, setting=setting, source=source))

    template = Template(source=source, components=tuple(components), setting=setting)
    if not template.tag_names():
        raise ConfigError(
            f"[naming] {setting} contains no <tag>, so every book would be given "
            f"the same name: {source!r}"
        )
    return template


def _parse_component(text: str, *, setting: str, source: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    literal: list[str] = []
    index = 0

    while index < len(text):
        char = text[index]
        if char == "<":
            close = text.find(">", index)
            if close == -1:
                raise ConfigError(
                    f"[naming] {setting} has an unclosed '<' at position "
                    f"{index + 1}: {source!r}"
                )
            name = text[index + 1 : close].strip().lower()
            if name not in TAG_NAMES:
                known = ", ".join(f"<{t.name}>" for t in TEMPLATE_TAGS)
                raise ConfigError(
                    f"[naming] {setting} uses an unknown tag <{name}>\n"
                    f"  available tags: {known}"
                )
            if literal:
                tokens.append(_Literal("".join(literal)))
                literal = []
            tokens.append(_Tag(name))
            index = close + 1
            continue
        if char == ">":
            raise ConfigError(
                f"[naming] {setting} has a '>' with no matching '<' at position "
                f"{index + 1}: {source!r}"
            )
        if char in _ILLEGAL_LITERAL or ord(char) < 0x20:
            shown = repr(char) if char.isprintable() else f"\\x{ord(char):02x}"
            raise ConfigError(
                f"[naming] {setting} has {shown} in its literal text, which is "
                f"not legal in a filename: {source!r}"
            )
        literal.append(char)
        index += 1

    if literal:
        tokens.append(_Literal("".join(literal)))
    return tuple(tokens)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

#: Bracket pairs whose contents are dropped together with their brackets when
#: every tag inside them is empty.
_BRACKETS = {"(": ")", "[": "]", "{": "}"}


def render_component(tokens: Sequence[_Token], values: Mapping[str, str]) -> str:
    """Fill one path component in, dropping the glue around empty tags."""

    tokens = _merge_literals(_drop_empty_bracketed(tokens, values))

    pieces: list[str] = []
    for position, token in enumerate(tokens):
        if isinstance(token, _Tag):
            pieces.append(values.get(token.name, "") or "")
            continue
        if _glue_survives(tokens, position, values):
            pieces.append(token.text)

    return "".join(pieces).strip()


def _drop_empty_bracketed(
    tokens: Sequence[_Token], values: Mapping[str, str]
) -> list[_Token]:
    """Remove ``(…)`` runs in which no tag produced anything.

    This is what keeps ``<album> (<year>) [<asin>]`` from rendering as
    ``Pawn of Prophecy () [B002V0QUOA]`` for a book with no year, and it has to
    happen before the glue rule below: the literal ``") ["`` sits between the
    year and the ASIN, so neither tag alone can decide its fate.
    """

    kept: list[_Token] = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        opener = _trailing_bracket(token)
        if opener is None:
            kept.append(token)
            index += 1
            continue

        end = _closing_bracket(tokens, index, _BRACKETS[opener])
        if end is None:
            kept.append(token)
            index += 1
            continue

        region = tokens[index : end + 1]
        inside = [tok for tok in region if isinstance(tok, _Tag)]
        # No tag inside means the brackets are part of a fixed phrase the user
        # typed, not an optional run, so they stay whatever the values are.
        if not inside or any((values.get(tok.name) or "").strip() for tok in inside):
            kept.append(token)
            index += 1
            continue

        # Everything inside came out empty: drop the brackets and their
        # contents, keeping whatever literal text sat before the opener.
        assert isinstance(token, _Literal)
        before = token.text[: token.text.rindex(opener)]
        if before:
            kept.append(_Literal(before))
        closer = tokens[end]
        assert isinstance(closer, _Literal)
        after = closer.text[closer.text.index(_BRACKETS[opener]) + 1 :]
        index = end + 1
        if after:
            # Re-examined, since a second bracketed run may start inside it.
            tokens = [*tokens[:index], _Literal(after), *tokens[index:]]
        continue

    return kept


def _merge_literals(tokens: Sequence[_Token]) -> list[_Token]:
    """Join literals that a drop left side by side, collapsing the seam.

    Parsing never emits two literals in a row, so a pair of them here is debris
    from a dropped bracketed run: ``<album> (<year>) [<asin>]`` without a year
    leaves the space that preceded ``(`` next to the one that preceded ``[``.
    Only this seam is collapsed - a double space inside a *tag value* is left
    alone, because the live library has folders that contain one.
    """

    merged: list[_Token] = []
    for token in tokens:
        if isinstance(token, _Literal) and merged and isinstance(merged[-1], _Literal):
            merged[-1] = _Literal(re.sub(r"\s{2,}", " ", merged[-1].text + token.text))
        else:
            merged.append(token)
    return merged


def _trailing_bracket(token: _Token) -> str | None:
    """The bracket this literal opens and does not close, if any."""

    if not isinstance(token, _Literal):
        return None
    for char in reversed(token.text):
        if char in _BRACKETS:
            return char
        if char in _BRACKETS.values():
            return None
    return None


def _closing_bracket(tokens: Sequence[_Token], start: int, closer: str) -> int | None:
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if isinstance(token, _Literal) and closer in token.text:
            return index
    return None


def _glue_survives(
    tokens: Sequence[_Token], position: int, values: Mapping[str, str]
) -> bool:
    """True when literal text at *position* has a non-empty tag beside it.

    Literal glue exists to join two values; with one of them missing it is
    punctuation attached to nothing, so ``<series_part> - <album>`` gives
    ``Pawn of Prophecy`` rather than ``- Pawn of Prophecy``.  Literal text with
    no tag on either side is not glue at all and always survives, which is how
    a template may carry a fixed word.
    """

    neighbours = [
        tokens[index]
        for index in (position - 1, position + 1)
        if 0 <= index < len(tokens)
    ]
    adjacent_tags = [token for token in neighbours if isinstance(token, _Tag)]
    if not adjacent_tags:
        return True
    return all((values.get(tag.name) or "").strip() for tag in adjacent_tags)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def library_component(value: str | None, *, budget: int = MAX_LIBRARY_COMPONENT) -> str:
    """Make one path component safe, the way the existing library was built.

    Illegal characters become ``_`` - not a space, and runs are not collapsed.
    The live library was built by ``absmove``, whose sanitiser did exactly this
    (``Space: 1969`` is on disk as ``Space_ 1969``); anything that normalises
    differently files the same author under a second folder and splits books
    Audiobookshelf currently shows as one, so this rule is matched to what is
    already on disk and is not a matter of taste.

    Values are normalised, never rejected: an illegal character here came from
    a publisher's tag, not from a mistake the user can fix in the config.
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


def year_of(meta: "tags_mod.BookMetadata") -> str:
    """The publication year, from the year tag or the front of the date tag."""

    if meta.published_year:
        return str(meta.published_year)
    match = re.match(r"(\d{4})", str(meta.published_date or ""))
    return match.group(1) if match else ""


def canonical_author(
    meta: "tags_mod.BookMetadata",
    *,
    registry: "authors_mod.AuthorRegistry | None" = None,
) -> str:
    """The author name a book is filed under, before folder ordering."""

    author = clean_author(meta.author or meta.albumartist)
    if registry is not None:
        author = registry.resolve(author).canonical or author
    return author


def tag_values(
    meta: "tags_mod.BookMetadata",
    *,
    registry: "authors_mod.AuthorRegistry | None" = None,
    phrases: Sequence[str] = (),
) -> dict[str, str]:
    """The value of every template tag for one book, before sanitising."""

    author = canonical_author(meta, registry=registry)
    # The folder lists collaborators alphabetically; the tag keeps the order it
    # was billed in.  Audible gives the same pair in a different order from one
    # book to the next, which without this makes a second shelf for the same
    # two people.  Only the name computed here is ordered: an existing folder
    # naming the same people wins, whatever order it used, so nothing already
    # shelved is renamed for the sake of it.
    author = authors_mod.join_authors(
        authors_mod.folder_order(authors_mod.split_authors(author))
    )
    # Named from the same cleaned-up title that goes into the tag, or the shelf
    # would still advertise "A LitRPG Adventure".
    album = (titles_mod.strip_phrases(meta.album or meta.title, phrases) or "").strip()
    title = (titles_mod.strip_phrases(meta.title or meta.album, phrases) or "").strip()

    return {
        "author": author,
        "album": album,
        "title": title,
        "subtitle": (meta.subtitle or "").strip(),
        "series": (meta.series or "").strip(),
        "series_part": format_series_part(meta.series_sequence),
        "narrator": (meta.narrator or "").strip(),
        "asin": (meta.asin or "").strip(),
        "year": year_of(meta),
        "publisher": (meta.publisher or "").strip(),
        "language": (meta.language or "").strip(),
        "genre": (meta.genres[0] if meta.genres else "").strip(),
    }


# --------------------------------------------------------------------------- #
# Putting it together
# --------------------------------------------------------------------------- #


def render_filename(
    template: Template, values: Mapping[str, str], *, suffix: str = ".m4b"
) -> str:
    """The file's name, sanitised and budgeted for the suffix and a collision.

    The filename gets a smaller budget than a folder: the suffix and a possible
    ``(2)`` marker have to fit inside the same limit.
    """

    rendered = render_component(template.components[0], values)
    if not rendered:
        raise EmptyName(template, values)
    budget = MAX_LIBRARY_COMPONENT - len(suffix.encode("utf-8")) - COLLISION_ALLOWANCE
    return library_component(rendered, budget=budget) + suffix


def render_shelf(template: Template, values: Mapping[str, str]) -> tuple[str, ...]:
    """The directories a book is filed under, innermost last.

    A component that renders empty is dropped rather than becoming ``_``: that
    is what gives a standalone book no series folder.  All of them coming out
    empty is an error, since the book would then be filed at the library root.
    """

    components = [
        library_component(rendered)
        for rendered in (
            render_component(tokens, values) for tokens in template.components
        )
        if rendered
    ]
    if not components:
        raise EmptyName(template, values)
    return tuple(components)


class EmptyName(BookError):
    """A template produced no name at all for a particular book.

    A :class:`~forge.errors.BookError`, not a
    :class:`~forge.errors.ConfigError`: the template is fine, this one book
    simply has none of the tags it asks for.  The fix is to edit the tags, so
    it skips one book rather than stopping the run.
    """

    def __init__(self, template: Template, values: Mapping[str, str]) -> None:
        wanted = sorted(template.tag_names())
        empty = ", ".join(f"<{name}>" for name in wanted if not values.get(name))
        super().__init__(
            f"[naming] {template.setting} ({template.source!r}) produces an empty "
            f"name for this book: {empty} are all missing"
        )
        self.template = template


def shelf_path(
    root: Path,
    meta: "tags_mod.BookMetadata",
    *,
    shelf_template: Template,
    filename_template: Template,
    suffix: str = ".m4b",
    registry: "authors_mod.AuthorRegistry | None" = None,
    phrases: Sequence[str] = (),
) -> Path:
    """Where *meta* belongs under *root*, per the two configured templates."""

    values = tag_values(meta, registry=registry, phrases=phrases)
    return root.joinpath(
        *render_shelf(shelf_template, values),
        render_filename(filename_template, values, suffix=suffix),
    )


# --------------------------------------------------------------------------- #
# The startup check
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Templates:
    """Both naming settings, parsed."""

    shelf: Template
    filename: Template


@lru_cache(maxsize=32)
def _compile(text: str, setting: str, allow_slash: bool) -> Template:
    return parse_template(text, setting=setting, allow_slash=allow_slash)


def default_templates() -> Templates:
    """The stock templates, which reproduce the layout the library was built with."""

    return Templates(
        shelf=_compile(DEFAULT_SHELF_TEMPLATE, "shelf_template", True),
        filename=_compile(DEFAULT_FILENAME_TEMPLATE, "filename_template", False),
    )


def require(cfg: "Config") -> Templates:
    """Parse both naming settings or refuse to go on.

    Called first by everything that names a file - ``forge run`` and
    ``forge shelve`` both check before they touch a book, as well as at config
    load - so a typo in a template is a message at startup rather than a
    half-shelved library.  Cheap enough to call per book: the parse is cached
    on the template text.
    """

    return Templates(
        shelf=_compile(cfg.shelf_template, "shelf_template", True),
        filename=_compile(cfg.filename_template, "filename_template", False),
    )
