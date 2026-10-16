"""Author identity: one person, however Amazon spelled them this time.

Amazon stores contributors in a database that does not insist on one spelling,
so the same author arrives as ``J.N. Chaney``, ``J. N. Chaney``, ``JN Chaney``
and ``J N Chaney`` - sometimes two of those from the same API call.  Audiobookshelf
indexes the author *tag*, so each spelling becomes its own shelf there, and the
library tree grows a folder per spelling to match.

What makes this tractable is that Audible gives every contributor an ASIN, and
that ASIN does not vary:

    tag "JN Chaney"     -> B00W2ZAK7E
    tag "J. N. Chaney"  -> B00W2ZAK7E
    tag "J.N. Chaney"   -> B00W2ZAK7E

So identity is a lookup, not a guess.  This module keeps the answers: an
``authors`` table of identities, and an ``author_aliases`` table mapping every
spelling ever seen to one of them.  Three tiers, strongest first:

1.  The book has an ASIN, so Audible names its contributors and their ASINs.
    No guessing, no human.
2.  No ASIN, but the spelling reduces to a key already in the alias table, so
    ``J N Chaney`` resolves offline.
3.  Neither, in which case nothing happens.  The name is left exactly as it is
    and reported as a candidate, because the alternative is guessing.

**The key deliberately does not drop initials.**  ``Brandon Morris`` and
``Brandon Q. Morris`` reduce to different keys and are never merged
automatically: they might be two people, and only an author ASIN - or someone
saying so once - can tell.  A rule clever enough to join them is also clever
enough to join two real authors who share a surname.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS author (
    author_asin    TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS author_alias (
    name_key    TEXT PRIMARY KEY,
    author_asin TEXT NOT NULL REFERENCES author(author_asin) ON DELETE CASCADE,
    spelling    TEXT NOT NULL,
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS author_alias_by_author ON author_alias (author_asin);

-- Spellings seen with no identity behind them yet: tier 3.  Kept so the
-- candidate list is a query rather than a re-scan of the whole library.
CREATE TABLE IF NOT EXISTS author_unknown (
    name_key   TEXT PRIMARY KEY,
    spelling   TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""

#: Anything that is not a letter or a digit is noise for identity purposes:
#: it is the difference between "J.N." and "J N", and nothing else.
_NOISE = re.compile(r"[^0-9a-z]+")

#: Author tags separate contributors with commas.  Semicolons appear too
#: ("Tao Wong; Jason J. Willis"), so both split.
_SEPARATORS = re.compile(r"\s*[;,]\s*")

SOURCE_AUDIBLE = "audible"
SOURCE_MANUAL = "manual"


def name_key(name: str) -> str:
    """A spelling-insensitive key for one contributor's name.

    ``J.N. Chaney``, ``J. N. Chaney``, ``JN Chaney`` and ``J N Chaney`` all
    reduce to ``jnchaney``.  ``Brandon Morris`` and ``Brandon Q. Morris`` do
    not reduce to the same thing, on purpose: see the module docstring.
    """

    return _NOISE.sub("", name.casefold())


def split_authors(value: str | None) -> list[str]:
    """One author tag into its contributors, in the order they were billed."""

    if not value:
        return []
    return [part.strip() for part in _SEPARATORS.split(value) if part.strip()]


def join_authors(names: Iterable[str]) -> str:
    """Contributors back into a tag, keeping order and dropping repeats."""

    return ", ".join(dict.fromkeys(name.strip() for name in names if name.strip()))


def shelf_key(value: str | None) -> frozenset[str]:
    """The identity of a *set* of authors, order ignored.

    ``Terry Maggert, J. N. Chaney`` and ``J.N. Chaney, Terry Maggert`` are the
    same collaboration and belong on the same shelf.
    """

    return frozenset(name_key(name) for name in split_authors(value) if name_key(name))


@dataclass(frozen=True)
class Contributor:
    """One author as Audible names them."""

    asin: str
    name: str


@dataclass(frozen=True)
class Resolution:
    """What the registry made of one author tag."""

    #: The tag as it should be written, or None when nothing is known.
    canonical: str | None
    #: Spellings that resolved to an identity.
    resolved: tuple[str, ...] = ()
    #: Spellings with no identity behind them, left exactly as they were.
    unknown: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unknown


class AuthorRegistry:
    """The stored answers to "who is this, really?".

    Reads are cheap and offline.  Nothing here talks to Audible: the caller
    fetches contributors and hands them to :meth:`learn`, so the registry stays
    testable and a shelve run with no network still resolves everything it has
    seen before.

    There is deliberately no "learn this tag's spelling too" step.  A spelling
    that reduces to the same key as the contributor Audible named is already
    covered by that one alias row, and a spelling that reduces to a different
    key cannot be attached without guessing - which is :meth:`link`'s job, and
    a human's decision.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------ lifecycle #

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AuthorRegistry":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self._connection.executescript(_SCHEMA)
        row = self._connection.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif row["version"] != SCHEMA_VERSION:
            LOGGER.warning(
                "author registry is schema version %s, this build expects %s",
                row["version"],
                SCHEMA_VERSION,
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ----------------------------------------------------------------- write #

    def learn(
        self, contributors: Sequence[Contributor], *, source: str = SOURCE_AUDIBLE
    ) -> int:
        """Record identities and the spelling each one arrived under.

        The first canonical name wins.  Audible itself returns different display
        names for one ASIN depending on which product you ask about, so letting
        a later lookup rewrite the canonical form would make the library churn.
        """

        learned = 0
        for contributor in contributors:
            if not contributor.asin or not contributor.name.strip():
                continue
            key = name_key(contributor.name)
            if not key:
                continue
            self._connection.execute(
                "INSERT INTO author (author_asin, canonical_name, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT (author_asin) DO NOTHING",
                (contributor.asin, contributor.name.strip(), self._now()),
            )
            cursor = self._connection.execute(
                "INSERT INTO author_alias "
                "(name_key, author_asin, spelling, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name_key) DO NOTHING",
                (key, contributor.asin, contributor.name.strip(), source, self._now()),
            )
            learned += cursor.rowcount or 0
            self._connection.execute("DELETE FROM author_unknown WHERE name_key = ?", (key,))
        return learned

    def link(self, spelling: str, author_asin: str, *, canonical_name: str | None = None) -> None:
        """Point a spelling at an identity by hand.

        This is tier 3: the ``Brandon Morris`` / ``Brandon Q. Morris`` case,
        which no key and no lookup can settle.  Stored once, honoured forever.
        """

        key = name_key(spelling)
        if not key:
            raise ValueError(f"{spelling!r} has nothing to key on")
        row = self._connection.execute(
            "SELECT canonical_name FROM author WHERE author_asin = ?", (author_asin,)
        ).fetchone()
        if row is None:
            if not canonical_name:
                raise ValueError(
                    f"{author_asin} is not a known author; pass canonical_name to create it"
                )
            self._connection.execute(
                "INSERT INTO author (author_asin, canonical_name, updated_at) VALUES (?, ?, ?)",
                (author_asin, canonical_name.strip(), self._now()),
            )
        elif canonical_name:
            self.rename(author_asin, canonical_name)
        self._connection.execute(
            "INSERT INTO author_alias (name_key, author_asin, spelling, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (name_key) DO UPDATE SET "
            "author_asin = excluded.author_asin, spelling = excluded.spelling, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (key, author_asin, spelling.strip(), SOURCE_MANUAL, self._now()),
        )
        self._connection.execute("DELETE FROM author_unknown WHERE name_key = ?", (key,))

    def rename(self, author_asin: str, canonical_name: str) -> None:
        """Change the spelling an identity is written as."""

        self._connection.execute(
            "UPDATE author SET canonical_name = ?, updated_at = ? WHERE author_asin = ?",
            (canonical_name.strip(), self._now(), author_asin),
        )

    def note_unknown(self, names: Iterable[str]) -> None:
        """Remember a spelling with no identity, for the candidate list."""

        for name in names:
            key = name_key(name)
            if not key or self.identity(name) is not None:
                continue
            self._connection.execute(
                "INSERT INTO author_unknown (name_key, spelling, seen, updated_at) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT (name_key) DO UPDATE SET seen = seen + 1, updated_at = excluded.updated_at",
                (key, name.strip(), self._now()),
            )

    # ------------------------------------------------------------------ read #

    def identity(self, name: str) -> str | None:
        """The author ASIN behind a spelling, if it is known."""

        row = self._connection.execute(
            "SELECT author_asin FROM author_alias WHERE name_key = ?", (name_key(name),)
        ).fetchone()
        return row["author_asin"] if row else None

    def canonical_name(self, name: str) -> str | None:
        """How this author should be written, if the registry knows them."""

        row = self._connection.execute(
            "SELECT author.canonical_name AS canonical_name "
            "FROM author_alias JOIN author USING (author_asin) "
            "WHERE author_alias.name_key = ?",
            (name_key(name),),
        ).fetchone()
        return row["canonical_name"] if row else None

    def resolve(self, value: str | None) -> Resolution:
        """Canonicalise a whole author tag, keeping the billing order.

        Unknown contributors are passed through untouched rather than dropped:
        a tag forge cannot fully resolve is still a correct tag.
        """

        names = split_authors(value)
        if not names:
            return Resolution(canonical=None)

        resolved: list[str] = []
        unknown: list[str] = []
        out: list[str] = []
        for name in names:
            canonical = self.canonical_name(name)
            if canonical is None:
                unknown.append(name)
                out.append(name)
            else:
                resolved.append(name)
                out.append(canonical)

        joined = join_authors(out)
        return Resolution(
            canonical=joined if joined != join_authors(names) else None,
            resolved=tuple(resolved),
            unknown=tuple(unknown),
        )

    def unknown_names(self) -> list[tuple[str, int]]:
        """Spellings with no identity, most often seen first."""

        rows = self._connection.execute(
            "SELECT spelling, seen FROM author_unknown ORDER BY seen DESC, spelling"
        ).fetchall()
        return [(row["spelling"], row["seen"]) for row in rows]

    def aliases_of(self, author_asin: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT spelling FROM author_alias WHERE author_asin = ? ORDER BY spelling",
            (author_asin,),
        ).fetchall()
        return [row["spelling"] for row in rows]

    def identities(self) -> list[tuple[str, str, int]]:
        """Every identity: ASIN, canonical name, number of known spellings."""

        rows = self._connection.execute(
            "SELECT author.author_asin AS asin, author.canonical_name AS name, "
            "COUNT(author_alias.name_key) AS aliases "
            "FROM author LEFT JOIN author_alias USING (author_asin) "
            "GROUP BY author.author_asin ORDER BY aliases DESC, name"
        ).fetchall()
        return [(row["asin"], row["name"], row["aliases"]) for row in rows]

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "authors": count("author"),
            "aliases": count("author_alias"),
            "unknown": count("author_unknown"),
        }
