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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 5

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
    canonical_name TEXT NOT NULL,
    -- 1 when a person chose this spelling, which no automatic pass may
    -- overrule.  The backfill picks canonical names by frequency across the
    -- library; a name set with `forge authors --rename` outranks the vote.
    canonical_pinned INTEGER NOT NULL DEFAULT 0,
    -- How many books in the library were by this author when the backfill last
    -- counted.  Stored because the backfill counts them anyway to run the
    -- frequency vote, and the review interface wants to show the authors who
    -- matter most first.  Nothing depends on it being current.
    book_count     INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- Every Audible ASIN that turns out to be this author.  One person, several
-- ids: Amazon keeps three contributor records for David Baldacci, and three
-- lookups of his books produced three identities, two of them unreachable
-- because the alias table can only point one name at one of them.
CREATE TABLE IF NOT EXISTS author_asin (
    asin       TEXT PRIMARY KEY,
    author_id  TEXT NOT NULL REFERENCES author(author_id) ON DELETE CASCADE,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS author_asin_by_author ON author_asin (author_id);

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
class ReviewRow:
    """One identity, with everything the review interface shows about it."""

    identity: str
    canonical: str
    pinned: bool
    spellings: tuple[str, ...]
    asins: int
    books: int
    updated_at: str

    @property
    def respelled(self) -> bool:
        """True when Amazon has recorded this author more than one way."""

        return len(self.spellings) > 1 or self.asins > 1

    @property
    def has_asin(self) -> bool:
        return not self.identity.startswith("name:")


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

        # sqlite3.complete_statement rather than a split on ";": a semicolon
        # inside one of the comments above would otherwise cut a CREATE TABLE
        # in half, which it did.
        buffered = ""
        for line in _SCHEMA.splitlines(keepends=True):
            buffered += line
            if sqlite3.complete_statement(buffered):
                self._connection.execute(buffered)
                buffered = ""
        if buffered.strip():
            self._connection.execute(buffered)

    def _migrate(self) -> None:
        self._upgrade_from_v1()
        self._upgrade_from_v2()
        self._create_tables()
        self._upgrade_from_v3()
        self._upgrade_from_v4()
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
            self._connection.execute(
                "CREATE TABLE author ("
                " author_id TEXT PRIMARY KEY,"
                " audible_asin TEXT,"
                " canonical_name TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE author_alias ("
                " name_key TEXT PRIMARY KEY,"
                " author_id TEXT NOT NULL REFERENCES author(author_id) ON DELETE CASCADE,"
                " spelling TEXT NOT NULL,"
                " source TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
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

    def _upgrade_from_v2(self) -> None:
        """Fold duplicate identities for one author into one, with many ASINs.

        v2 keyed an identity by a single ASIN, on the assumption that Amazon
        issues one per person.  It issues several: three lookups of David
        Baldacci's books made three identities, and because the alias table
        keys on the name, two of them were left with nothing pointing at them.

        Folding them loses nothing the registry could express - two authors
        with a byte-identical name were already indistinguishable through the
        alias table - and it means a later pass can group a library by identity
        without splitting one author across three shelves.
        """

        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(author)").fetchall()
        }
        if "audible_asin" not in columns:
            return

        LOGGER.info("upgrading the author registry from schema version 2")
        authors = self._connection.execute(
            "SELECT author_id, audible_asin, canonical_name, updated_at FROM author"
        ).fetchall()
        aliases = self._connection.execute(
            "SELECT name_key, author_id, spelling, source, updated_at FROM author_alias"
        ).fetchall()

        reachable = {row["author_id"] for row in aliases}
        # Which identity owns each name, so an orphan can be folded into it.
        by_key = {row["name_key"]: row["author_id"] for row in aliases}

        keep: list[tuple[str, str, str]] = []
        asins: list[tuple[str, str, str]] = []
        for row in authors:
            identity = row["author_id"]
            target = identity
            if identity not in reachable:
                target = by_key.get(name_key(row["canonical_name"]), identity)
            if target == identity:
                keep.append((identity, row["canonical_name"], row["updated_at"]))
            else:
                LOGGER.info(
                    "folding duplicate identity %s into %s (%s)",
                    identity,
                    target,
                    row["canonical_name"],
                )
            if row["audible_asin"]:
                asins.append((row["audible_asin"], target, row["updated_at"]))

        self._connection.execute("BEGIN")
        try:
            self._connection.execute("DROP TABLE author_alias")
            self._connection.execute("DROP TABLE author")
            self._create_tables()
            for identity, name, updated in keep:
                self._connection.execute(
                    "INSERT INTO author (author_id, canonical_name, updated_at) "
                    "VALUES (?, ?, ?)",
                    (identity, name, updated),
                )
            for row in aliases:
                self._connection.execute(
                    "INSERT INTO author_alias "
                    "(name_key, author_id, spelling, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (row["name_key"], row["author_id"], row["spelling"], row["source"],
                     row["updated_at"]),
                )
            for asin, identity, updated in asins:
                self._connection.execute(
                    "INSERT INTO author_asin (asin, author_id, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (asin) DO NOTHING",
                    (asin, identity, updated),
                )
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _upgrade_from_v3(self) -> None:
        """Add canonical_pinned, which v3 had no column for."""

        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(author)").fetchall()
        }
        if "canonical_pinned" in columns:
            return
        LOGGER.info("upgrading the author registry from schema version 3")
        self._connection.execute(
            "ALTER TABLE author ADD COLUMN canonical_pinned INTEGER NOT NULL DEFAULT 0"
        )

    def _upgrade_from_v4(self) -> None:
        """Add book_count, which v4 had no column for."""

        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(author)").fetchall()
        }
        if "book_count" in columns:
            return
        LOGGER.info("upgrading the author registry from schema version 4")
        self._connection.execute(
            "ALTER TABLE author ADD COLUMN book_count INTEGER NOT NULL DEFAULT 0"
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several writes as one, so a bad row writes nothing.

        The connection autocommits every statement otherwise, which is right
        for learning one author at a time and wrong for saving a screenful of
        renames.
        """

        self._connection.execute("BEGIN")
        try:
            yield self._connection
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

            # An identity this author already has wins over making a new one,
            # by the name first and then by the ASIN.  Amazon issues several
            # contributor ids for one person, so a second id is another way of
            # naming someone already known, not another person.
            identity = self.identity(contributor.name)
            if identity is None and contributor.asin:
                identity = self.identity_for_asin(contributor.asin)
            if identity is None:
                identity = contributor.identity
                self._connection.execute(
                    "INSERT INTO author (author_id, canonical_name, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (author_id) DO NOTHING",
                    (identity, contributor.name.strip(), self._now()),
                )

            kind = source if contributor.asin else SOURCE_AUDIBLE_NO_ASIN
            cursor = self._connection.execute(
                "INSERT INTO author_alias "
                "(name_key, author_id, spelling, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name_key) DO NOTHING",
                (key, identity, contributor.name.strip(), kind, self._now()),
            )
            learned += cursor.rowcount or 0
            if contributor.asin:
                self._connection.execute(
                    "INSERT INTO author_asin (asin, author_id, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (asin) DO NOTHING",
                    (contributor.asin, identity, self._now()),
                )
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
                "INSERT INTO author (author_id, canonical_name, updated_at) VALUES (?, ?, ?)",
                (author_id, canonical_name.strip(), self._now()),
            )
            if not author_id.startswith("name:"):
                self._connection.execute(
                    "INSERT INTO author_asin (asin, author_id, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (asin) DO NOTHING",
                    (author_id, author_id, self._now()),
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
        """Change the spelling an identity is written as, and pin it.

        Pinned because this is a person's decision: the backfill chooses
        canonical names by how often the library uses each spelling, and a vote
        must not quietly undo a choice someone made on purpose.
        """

        self._connection.execute(
            "UPDATE author SET canonical_name = ?, canonical_pinned = 1, updated_at = ? "
            "WHERE author_id = ?",
            (canonical_name.strip(), self._now(), author_id),
        )

    def set_canonical(self, author_id: str, canonical_name: str) -> bool:
        """Change the spelling, unless someone pinned it.  True when written."""

        if self.is_pinned(author_id):
            return False
        self._connection.execute(
            "UPDATE author SET canonical_name = ?, updated_at = ? WHERE author_id = ?",
            (canonical_name.strip(), self._now(), author_id),
        )
        return True

    def unpin(self, author_id: str) -> None:
        """Let an automatic pass choose this author's spelling again."""

        self._connection.execute(
            "UPDATE author SET canonical_pinned = 0, updated_at = ? WHERE author_id = ?",
            (self._now(), author_id),
        )

    def rename_many(self, names: Mapping[str, str]) -> int:
        """Apply several renames as one, pinning each.  Returns the count.

        Checked before anything is written, and written in one transaction, so
        a single bad row leaves the registry exactly as it was.  A blank name
        is refused here as well as by the interface that collected it: the
        canonical name ends up in a tag and a folder name, and an empty one
        would leave books with no shelf to go to.
        """

        cleaned: dict[str, str] = {}
        for identity, name in names.items():
            if not name or not name.strip():
                raise ValueError(f"{identity} was given a blank name")
            if self.canonical_of(identity) is None:
                raise ValueError(f"{identity} is not a known author")
            cleaned[identity] = name.strip()

        if not cleaned:
            return 0

        now = self._now()
        with self.transaction() as connection:
            for identity, name in cleaned.items():
                connection.execute(
                    "UPDATE author SET canonical_name = ?, canonical_pinned = 1, "
                    "updated_at = ? WHERE author_id = ?",
                    (name, now, identity),
                )
        return len(cleaned)

    def set_book_counts(self, counts: Mapping[str, int]) -> None:
        """Record how many books each author has, as last counted."""

        if not counts:
            return
        now = self._now()
        with self.transaction() as connection:
            for identity, count in counts.items():
                connection.execute(
                    "UPDATE author SET book_count = ?, updated_at = ? WHERE author_id = ?",
                    (int(count), now, identity),
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

    def canonical_of(self, author_id: str) -> str | None:
        """The spelling an identity is written as, by id rather than by name."""

        row = self._connection.execute(
            "SELECT canonical_name FROM author WHERE author_id = ?", (author_id,)
        ).fetchone()
        return row["canonical_name"] if row else None

    def is_pinned(self, author_id: str) -> bool:
        """True when a person chose this author's spelling by hand."""

        row = self._connection.execute(
            "SELECT canonical_pinned FROM author WHERE author_id = ?", (author_id,)
        ).fetchone()
        return bool(row["canonical_pinned"]) if row else False

    def identity_for_asin(self, asin: str) -> str | None:
        """The identity an Audible contributor ASIN belongs to."""

        row = self._connection.execute(
            "SELECT author_id FROM author_asin WHERE asin = ?", (asin,)
        ).fetchone()
        return row["author_id"] if row else None

    def asins_of(self, author_id: str) -> list[str]:
        """Every Audible contributor ASIN that turned out to be this author."""

        rows = self._connection.execute(
            "SELECT asin FROM author_asin WHERE author_id = ? ORDER BY asin",
            (author_id,),
        ).fetchall()
        return [row["asin"] for row in rows]

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

    #: Spellings are joined with the unit separator, not a comma: an author
    #: name may contain commas and often does.
    _JOIN = "\x1f"

    def review_rows(self) -> list[ReviewRow]:
        """Every identity, in one query.

        One query rather than :meth:`identities` plus a :meth:`aliases_of` per
        row, which for this library would be some six thousand of them.
        """

        rows = self._connection.execute(
            "SELECT author.author_id AS identity, "
            "       author.canonical_name AS canonical, "
            "       author.canonical_pinned AS pinned, "
            "       author.book_count AS books, "
            "       author.updated_at AS updated_at, "
            "       (SELECT GROUP_CONCAT(spelling, char(31)) FROM author_alias "
            "          WHERE author_alias.author_id = author.author_id) AS spellings, "
            "       (SELECT COUNT(*) FROM author_asin "
            "          WHERE author_asin.author_id = author.author_id) AS asins "
            "FROM author ORDER BY author.canonical_name"
        ).fetchall()

        return [
            ReviewRow(
                identity=row["identity"],
                canonical=row["canonical"],
                pinned=bool(row["pinned"]),
                spellings=tuple(
                    (row["spellings"] or "").split(self._JOIN)
                    if row["spellings"]
                    else ()
                ),
                asins=int(row["asins"] or 0),
                books=int(row["books"] or 0),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

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
            "asins": count("author_asin"),
            "unknown": count("author_unknown"),
        }
