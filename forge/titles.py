"""Marketing text in book titles, and the phrases to take back out.

Publishers treat the title field as advertising space, so the same book
arrives as "The Land: A LitRPG Adventure", "The Land (A LitRPG Adventure)" or
"The Land - A LitRPG Adventure".  The series name, the genre and the format all
end up in a field that should hold the name of the book.

There is no rule that can tell marketing from a title, so this does not try:
it keeps a list of phrases *you* have said are not part of a title, and removes
them wherever they appear.  Each phrase is removed with whatever punctuation was
holding it on - a colon, a dash, a bracket pair - back to the end of the real
title.

A question mark or exclamation mark immediately after the title survives, since
it belongs to the title rather than to the punctuation:

    "What Could Go Wrong?: A LitRPG Adventure"  ->  "What Could Go Wrong?"
    "The Land: A LitRPG Adventure!"             ->  "The Land"

The first keeps its "?" because the "?" touches the title.  The second loses the
"!" because a colon and a space stand between it and the title - it was
punctuating the marketing, not the book.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

-- phrase_key is the phrase casefolded with its spacing collapsed, so the same
-- phrase cannot be added twice in two styles.
CREATE TABLE IF NOT EXISTS title_phrase (
    phrase_key TEXT PRIMARY KEY,
    phrase     TEXT NOT NULL,
    added_at   TEXT NOT NULL
);
"""

#: Pairs that are removed when a phrase leaves one of them empty.
_EMPTY_WRAPPERS = (
    re.compile(r"\(\s*\)"),
    re.compile(r"\[\s*\]"),
    re.compile(r"\{\s*\}"),
    re.compile(r"\"\s*\""),
    re.compile(r"'\s*'"),
)

#: Two separators left adjacent by a removal in the middle of a title.
_DOUBLED_SEPARATOR = re.compile(r"\s*([:;,\-–—])\s*(?=[:;,\-–—])")

#: Punctuation that may end a title and is kept when it touches the text.
_KEPT_ENDINGS = frozenset("?!")

#: Openers whose closer is kept when the text inside is kept.
_BRACKETS = {"(": ")", "[": "]", "{": "}"}


def phrase_key(phrase: str) -> str:
    """The form a phrase is stored under: casefolded, spacing collapsed."""

    return " ".join(phrase.casefold().split())


def _pattern(phrase: str) -> re.Pattern[str]:
    """A phrase matcher that tolerates case and spacing.

    Bounded by "not a word character" rather than ``\\b``, because a phrase may
    begin or end with punctuation ("Book 1:") and ``\\b`` behaves differently
    either side of one.
    """

    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def tidy(text: str) -> str:
    """Clear up the punctuation a removal leaves behind."""

    for wrapper in _EMPTY_WRAPPERS:
        text = wrapper.sub("", text)
    text = _DOUBLED_SEPARATOR.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)

    # Everything after the last letter or digit goes, except a run of "?" or
    # "!" that touches it.
    last = -1
    for index, character in enumerate(text):
        if character.isalnum():
            last = index
    if last == -1:
        return ""

    end = last + 1
    while end < len(text) and text[end] in _KEPT_ENDINGS:
        end += 1

    # A bracket the kept text opened has to be closed again: cutting at the
    # last letter of "The Land (Deluxe Edition): A LitRPG Adventure" would
    # leave "The Land (Deluxe Edition".
    unclosed: list[str] = []
    for character in text[:end]:
        if character in _BRACKETS:
            unclosed.append(_BRACKETS[character])
        elif unclosed and character == unclosed[-1]:
            unclosed.pop()
    while unclosed and end < len(text) and text[end] == unclosed[-1]:
        end += 1
        unclosed.pop()

    text = text[:end]

    # A leading separator is left when the phrase was at the front.
    start = 0
    while start < len(text) and not (text[start].isalnum() or text[start] in "('\"[{"):
        start += 1
    return text[start:].strip()


def strip_phrases(value: str | None, phrases: Iterable[str]) -> str | None:
    """Remove every phrase from *value*, with the punctuation holding it on.

    Returns the value unchanged when nothing matched, and - deliberately - also
    when removing everything would leave no title at all.  A book with an empty
    title has no name and no shelf; a book with marketing in its title is
    merely untidy, so the untidy version wins.
    """

    if not value:
        return value

    text = value
    for phrase in phrases:
        if not phrase.strip():
            continue
        text = _pattern(phrase).sub(" ", text)

    if text == value:
        return value

    cleaned = tidy(text)
    if not cleaned:
        LOGGER.debug("not stripping %r: nothing would be left of the title", value)
        return value
    return cleaned


class PhraseStore:
    """The phrases to remove from titles.

    Its own database file, beside the author registry rather than inside it:
    the two have nothing to do with each other, and a migration of one should
    never be able to touch the other.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------ lifecycle #

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PhraseStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _migrate(self) -> None:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                self._connection.execute(statement)
        row = self._connection.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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

    # ---------------------------------------------------------------- write #

    def add(self, phrase: str) -> bool:
        """Remember a phrase.  False when it was already there."""

        key = phrase_key(phrase)
        if not key:
            raise ValueError("a phrase needs some text in it")
        cursor = self._connection.execute(
            "INSERT INTO title_phrase (phrase_key, phrase, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT (phrase_key) DO NOTHING",
            (key, phrase.strip(), self._now()),
        )
        return bool(cursor.rowcount)

    def remove(self, phrase: str) -> bool:
        """Forget a phrase.  False when it was not there."""

        cursor = self._connection.execute(
            "DELETE FROM title_phrase WHERE phrase_key = ?", (phrase_key(phrase),)
        )
        return bool(cursor.rowcount)

    def apply(self, *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> tuple[int, int]:
        """Add and remove phrases in one go.  Returns (added, removed).

        Checked before anything is written and written in one transaction, so
        a screenful of edits either lands or does not.
        """

        additions = []
        for phrase in add:
            if not phrase or not phrase.strip():
                raise ValueError("a phrase needs some text in it")
            additions.append(phrase.strip())
        removals = [phrase for phrase in remove]

        now = self._now()
        added = removed = 0
        with self.transaction() as connection:
            for phrase in removals:
                cursor = connection.execute(
                    "DELETE FROM title_phrase WHERE phrase_key = ?", (phrase_key(phrase),)
                )
                removed += cursor.rowcount or 0
            for phrase in additions:
                cursor = connection.execute(
                    "INSERT INTO title_phrase (phrase_key, phrase, added_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (phrase_key) DO NOTHING",
                    (phrase_key(phrase), phrase, now),
                )
                added += cursor.rowcount or 0
        return added, removed

    # ----------------------------------------------------------------- read #

    def rows(self) -> list[tuple[str, str]]:
        """Every phrase with the date it was added, longest first."""

        return [
            (row["phrase"], row["added_at"])
            for row in self._connection.execute(
                "SELECT phrase, added_at FROM title_phrase "
                "ORDER BY LENGTH(phrase) DESC, phrase"
            ).fetchall()
        ]

    def phrases(self) -> list[str]:
        """Every phrase, longest first.

        Longest first so that a phrase containing another is removed whole:
        with both "A LitRPG Adventure" and "LitRPG" on the list, the longer one
        must go first or it would be left as "A Adventure".
        """

        rows = self._connection.execute(
            "SELECT phrase FROM title_phrase ORDER BY LENGTH(phrase) DESC, phrase"
        ).fetchall()
        return [row["phrase"] for row in rows]

    def __len__(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) FROM title_phrase").fetchone()[0]
        )

    def strip(self, value: str | None) -> str | None:
        """Remove every stored phrase from *value*."""

        return strip_phrases(value, self.phrases())


def suggest_phrases(titles: Sequence[str], *, minimum: int = 2) -> list[tuple[str, int]]:
    """Candidate phrases, from text that trails a lot of titles.

    Looks at what follows a colon or a dash, and at what sits inside a bracket
    pair at the end, and counts how many titles share it.  Only a suggestion:
    "Book 2" trails plenty of titles and belongs to none of them, which is why
    nothing here is ever added automatically.
    """

    counts: dict[str, int] = {}
    seen: dict[str, str] = {}
    patterns = (
        re.compile(r"[:\-–—]\s*([^:\-–—()\[\]]{3,60})$"),
        re.compile(r"[(\[]\s*([^()\[\]]{3,60})\s*[)\]]\s*$"),
    )
    for title in titles:
        for pattern in patterns:
            match = pattern.search(title or "")
            if not match:
                continue
            candidate = " ".join(match.group(1).split())
            key = phrase_key(candidate)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            seen.setdefault(key, candidate)

    return sorted(
        ((seen[key], count) for key, count in counts.items() if count >= minimum),
        key=lambda item: (-item[1], item[0].casefold()),
    )
