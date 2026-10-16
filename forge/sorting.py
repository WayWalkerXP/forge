"""How the triage list is ordered.

The orders live here rather than in the interface so they can be tested
without a terminal, and so the sort screen and the book list are guaranteed to
agree on what each choice means.

Two rules hold for every order:

* **Blanks sink.**  A book with nothing to sort on - no author, no series, no
  bitrate because it could not be probed - goes to the bottom in *both*
  directions.  Reversing the order should not promote the books that have the
  least information.
* **Ties break by name.**  Every key ends with the on-disk name, so two books
  that compare equal keep a stable, predictable position between refreshes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import library as library_mod
from .disposition import Disposition
from .scan import Book, triage_sort_key

#: Rank for the disposition order: declaration order of the enum, which runs
#: from untriaged through the actionable values to done.
_DISPOSITION_RANK = {member: index for index, member in enumerate(Disposition)}

#: Rank for the dup order: unchecked last, then the answers worth acting on
#: first.  An unchecked book has no value at all, so it sinks as a blank.
_DUPLICATE_RANK = {
    library_mod.DUPLICATE: 0,
    library_mod.UNIQUE: 1,
    library_mod.NO_ASIN: 2,
    library_mod.ERROR: 3,
}


def _name(book: Book) -> str:
    return book.display_name.casefold()


def _sequence_number(value: str | None) -> float:
    """The leading number in a series sequence, for ordering.

    Sequences are free text in the wild: "3", "3.5", "Book 2", "II".  Anything
    without a leading number sorts after everything that has one, rather than
    being dropped from the series it belongs to.
    """

    if not value:
        return float("inf")
    match = re.search(r"\d+(?:\.\d+)?", value)
    return float(match.group()) if match else float("inf")


@dataclass(frozen=True)
class SortOrder:
    """One way of ordering the book list."""

    name: str
    description: str
    #: The sort key.  Takes the duplicate-check map too, because the dup column
    #: is interface state rather than something stored on the book.
    key: Callable[[Book, Mapping[str, Any]], Any]
    #: False when this book has no value for this order, so it sinks.
    present: Callable[[Book, Mapping[str, Any]], bool] = lambda book, dups: True


SORT_ORDERS: tuple[SortOrder, ...] = (
    SortOrder(
        "triage",
        "needs attention first, then by name",
        key=lambda book, dups: triage_sort_key(book),
    ),
    SortOrder(
        "title",
        "album or title, falling back to the file name",
        key=lambda book, dups: ((book.title or book.display_name).casefold(), _name(book)),
    ),
    SortOrder(
        "author",
        "author, then title within an author",
        key=lambda book, dups: (
            (book.author or "").casefold(),
            (book.title or "").casefold(),
            _name(book),
        ),
        present=lambda book, dups: bool(book.author),
    ),
    SortOrder(
        "series",
        "series, then number within a series",
        key=lambda book, dups: (
            (book.metadata.series or "").casefold() if book.metadata else "",
            _sequence_number(book.metadata.series_sequence if book.metadata else None),
            _name(book),
        ),
        present=lambda book, dups: bool(book.metadata and book.metadata.series),
    ),
    SortOrder(
        "name",
        "the name on disk",
        key=lambda book, dups: _name(book),
    ),
    SortOrder(
        "disposition",
        "untriaged first, then by what forge will do",
        key=lambda book, dups: (_DISPOSITION_RANK.get(book.disposition, 99), _name(book)),
    ),
    SortOrder(
        "readiness",
        "books that cannot be converted first",
        key=lambda book, dups: (book.is_ready, _name(book)),
    ),
    SortOrder(
        "duplicate",
        "library duplicates first; unchecked books last",
        key=lambda book, dups: (
            _DUPLICATE_RANK.get(dups[book.key].status, 98) if book.key in dups else 99,
            _name(book),
        ),
        present=lambda book, dups: book.key in dups,
    ),
    SortOrder(
        "bitrate",
        "encoded bitrate, lowest first",
        key=lambda book, dups: (
            book.info.normalized_bitrate_kbps if book.info else 0,
            book.info.channels if book.info else 0,
            _name(book),
        ),
        present=lambda book, dups: book.info is not None,
    ),
    SortOrder(
        "duration",
        "shortest first",
        key=lambda book, dups: (book.total_duration, _name(book)),
        present=lambda book, dups: book.total_duration > 0,
    ),
)

#: The order the list opens in.
DEFAULT_ORDER = SORT_ORDERS[0]


def order_by_name(name: str) -> SortOrder:
    """Look up an order, falling back to the default for an unknown name."""

    for order in SORT_ORDERS:
        if order.name == name:
            return order
    return DEFAULT_ORDER


def sort_books(
    books: Sequence[Book],
    order: SortOrder,
    *,
    descending: bool = False,
    duplicates: Mapping[str, Any] | None = None,
) -> list[Book]:
    """Return *books* in *order*, with the valueless ones at the end."""

    dups: Mapping[str, Any] = duplicates if duplicates is not None else {}
    ordered = sorted(books, key=lambda book: order.key(book, dups), reverse=descending)
    # Partition after sorting, not as part of the key: a missing-value flag in
    # the key would be reversed along with everything else, and reversing is
    # exactly when blanks must stay put.
    present = [book for book in ordered if order.present(book, dups)]
    missing = [book for book in ordered if not order.present(book, dups)]
    return present + missing
