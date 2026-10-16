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
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

-- author_id is an Audible contributor ASIN where there is one, and
-- "name:<key>" where there is not.  The first version of this table called the
-- column author_asin, which was a lie for every self-published author in the
-- catalogue: Amazon returns their name with no id at all.
CREATE TABLE IF NOT EXISTS author (
    author_id      TEXT PRIMARY KEY,
    audible_asin   TEXT,
    canonical_name TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS author_alias (
    name_key    TEXT PRIMARY KEY,
    author_id   TEXT NOT NULL REFERENCES author(author_id) ON DELETE CASCADE,
    spelling    TEXT NOT NULL,
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS author_alias_by_author ON author_alias (author_id);

-- Spellings seen with no identity behind them yet: tier 3.  Kept so the
-- candidate list is a query rather than a re-scan of the whole library.
CREATE TABLE IF NOT EXISTS author_unknown (
    name_key   TEXT PRIMARY KEY,
    spelling   TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""

#: Anything that is not an unaccented letter or a digit is noise for identity
#: purposes: it is the difference between "J.N." and "J N", and nothing else.
_NOISE = re.compile(r"[^0-9a-z]+")

#: Author tags separate contributors with commas.  Semicolons appear too
#: ("Tao Wong; Jason J. Willis"), so both split.
_SEPARATORS = re.compile(r"\s*[;,]\s*")

SOURCE_AUDIBLE = "audible"
#: Audible named the author but gave no contributor id, so the identity is
#: derived from the name.  Recorded separately because a backfill could promote
#: one of these if Amazon ever issues the author an ASIN.
SOURCE_AUDIBLE_NO_ASIN = "audible-no-asin"
SOURCE_MANUAL = "manual"


def name_key(name: str) -> str:
    """A spelling-insensitive key for one contributor's name.

    ``J.N. Chaney``, ``J. N. Chaney``, ``JN Chaney`` and ``J N Chaney`` all
    reduce to ``jnchaney``.  ``Brandon Morris`` and ``Brandon Q. Morris`` do
    not reduce to the same thing, on purpose: see the module docstring.
    """

    # Accents are folded, not dropped.  Amazon writes this author's surname
    # both ways - "Mira González" in the tag, "Mira Gonzalez" elsewhere - and
    # deleting the "ó" would key them as "gonzlez" and "gonzalez", two people.
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NOISE.sub("", stripped)


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
    """One author as Audible names them.

    ``asin`` is None for the many self-published authors Amazon holds no
    contributor record for: the catalogue returns ``{"name": "Rhaegar"}`` with
    no id at all.  Those authors still need an identity, or nothing downstream
    can ever record that they have been seen - so the name becomes one.
    """

    asin: str | None
    name: str

    @property
    def identity(self) -> str:
        """What this author is keyed by: an Audible ASIN, or their own name.

        A ``name:`` identity is weaker and says so.  Two different authors who
        reduce to one key would share it - but with no ASIN there is nothing to
        tell them apart anyway, so this is as good as the data gets.
        """

        if self.asin:
            return self.asin
        return f"name:{name_key(self.name)}"


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

    def _create_tables(self) -> None:
        """Run the schema one statement at a time.

        Not ``executescript``: that commits before it starts, which silently
        ends any transaction the caller had open - and the v1 upgrade needs the
        new tables created *inside* its transaction.
        """

        for statement in _SCHEMA.split(";"):
            if statement.strip():
                self._connection.execute(statement)

    def _migrate(self) -> None:
        self._upgrade_from_v1()
        self._create_tables()
        row = self._connection.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif row["version"] != SCHEMA_VERSION:
            self._connection.execute(
                "UPDATE schema_info SET version = ?", (SCHEMA_VERSION,)
            )

    def _upgrade_from_v1(self) -> None:
        """Rebuild a v1 registry: author_asin became author_id.

        Alias keys are recomputed rather than copied, because v1 built them
        with a key that deleted accented letters instead of folding them.
        """

        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(author)").fetchall()
        }
        if "author_asin" not in columns:
            return

        LOGGER.info("upgrading the author registry from schema version 1")
        authors = self._connection.execute(
            "SELECT author_asin, canonical_name, updated_at FROM author"
        ).fetchall()
        aliases = self._connection.execute(
            "SELECT author_asin, spelling, source, updated_at FROM author_alias"
        ).fetchall()

        self._connection.execute("BEGIN")
        try:
            self._connection.execute("DROP TABLE author_alias")
            self._connection.execute("DROP TABLE author")
            self._create_tables()
            for row in authors:
                self._connection.execute(
                    "INSERT INTO author (author_id, audible_asin, canonical_name, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (row["author_asin"], row["author_asin"], row["canonical_name"],
                     row["updated_at"]),
                )
            for row in aliases:
                self._connection.execute(
                    "INSERT INTO author_alias "
                    "(name_key, author_id, spelling, source, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name_key) DO NOTHING",
                    (name_key(row["spelling"]), row["author_asin"], row["spelling"],
                     row["source"], row["updated_at"]),
                )
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

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
            key = name_key(contributor.name)
            if not key:
                continue
            identity = contributor.identity
            kind = source if contributor.asin else SOURCE_AUDIBLE_NO_ASIN
            self._connection.execute(
                "INSERT INTO author (author_id, audible_asin, canonical_name, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (author_id) DO NOTHING",
                (identity, contributor.asin or None, contributor.name.strip(), self._now()),
            )
            cursor = self._connection.execute(
                "INSERT INTO author_alias "
                "(name_key, author_id, spelling, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name_key) DO NOTHING",
                (key, identity, contributor.name.strip(), kind, self._now()),
            )
            learned += cursor.rowcount or 0
            self._connection.execute("DELETE FROM author_unknown WHERE name_key = ?", (key,))
        return learned

    def link(self, spelling: str, author_id: str, *, canonical_name: str | None = None) -> None:
        """Point a spelling at an identity by hand.

        This is tier 3: the ``Brandon Morris`` / ``Brandon Q. Morris`` case,
        which no key and no lookup can settle.  Stored once, honoured forever.

        *author_id* is an Audible contributor ASIN, or the ``name:<key>`` form
        that an author Amazon has no id for is kept under.
        """

        key = name_key(spelling)
        if not key:
            raise ValueError(f"{spelling!r} has nothing to key on")
        row = self._connection.execute(
            "SELECT canonical_name FROM author WHERE author_id = ?", (author_id,)
        ).fetchone()
        if row is None:
            if not canonical_name:
                raise ValueError(
                    f"{author_id} is not a known author; pass canonical_name to create it"
                )
            self._connection.execute(
                "INSERT INTO author (author_id, audible_asin, canonical_name, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    author_id,
                    None if author_id.startswith("name:") else author_id,
                    canonical_name.strip(),
                    self._now(),
                ),
            )
        elif canonical_name:
            self.rename(author_id, canonical_name)
        self._connection.execute(
            "INSERT INTO author_alias (name_key, author_id, spelling, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (name_key) DO UPDATE SET "
            "author_id = excluded.author_id, spelling = excluded.spelling, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (key, author_id, spelling.strip(), SOURCE_MANUAL, self._now()),
        )
        self._connection.execute("DELETE FROM author_unknown WHERE name_key = ?", (key,))

    def rename(self, author_id: str, canonical_name: str) -> None:
        """Change the spelling an identity is written as."""

        self._connection.execute(
            "UPDATE author SET canonical_name = ?, updated_at = ? WHERE author_id = ?",
            (canonical_name.strip(), self._now(), author_id),
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
        """The identity behind a spelling, if it is known.

        An Audible contributor ASIN, or ``name:<key>`` for an author Amazon
        holds no contributor record for.
        """

        row = self._connection.execute(
            "SELECT author_id FROM author_alias WHERE name_key = ?", (name_key(name),)
        ).fetchone()
        return row["author_id"] if row else None

    def canonical_name(self, name: str) -> str | None:
        """How this author should be written, if the registry knows them."""

        row = self._connection.execute(
            "SELECT author.canonical_name AS canonical_name "
            "FROM author_alias JOIN author USING (author_id) "
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

    def aliases_of(self, author_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT spelling FROM author_alias WHERE author_id = ? ORDER BY spelling",
            (author_id,),
        ).fetchall()
        return [row["spelling"] for row in rows]

    def identities(self) -> list[tuple[str, str, int]]:
        """Every identity: id, canonical name, number of known spellings."""

        rows = self._connection.execute(
            "SELECT author.author_id AS id, author.canonical_name AS name, "
            "COUNT(author_alias.name_key) AS aliases "
            "FROM author LEFT JOIN author_alias USING (author_id) "
            "GROUP BY author.author_id ORDER BY aliases DESC, name"
        ).fetchall()
        return [(row["id"], row["name"], row["aliases"]) for row in rows]

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "authors": count("author"),
            "aliases": count("author_alias"),
            "unknown": count("author_unknown"),
        }
