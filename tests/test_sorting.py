"""List ordering: the orders themselves, and where valueless books land."""

from __future__ import annotations

import unittest
from pathlib import Path

from forge import library
from forge.disposition import Disposition as D
from forge.probe import AudioInfo
from forge.scan import Book
from forge.sorting import (
    DEFAULT_ORDER,
    SORT_ORDERS,
    order_by_name,
    sort_books,
)
from forge.tags import BookMetadata


def book(
    name: str,
    *,
    title: str | None = None,
    author: str | None = None,
    series: str | None = None,
    sequence: str | None = None,
    bitrate: int | None = None,
    seconds: float = 0.0,
    disposition: D = D.UNTRIAGED,
) -> Book:
    path = Path("/books") / name
    metadata = BookMetadata(
        path=str(path),
        album=title,
        author=author,
        series=series,
        series_sequence=sequence,
    )
    info = (
        AudioInfo(
            path=path,
            bitrate_bps=bitrate * 1000,
            channels=2,
            codec="aac",
            duration_seconds=seconds,
            chapter_count=0,
            size_bytes=1,
        )
        if bitrate is not None
        else None
    )
    return Book(
        path=path,
        is_folder=False,
        files=[path],
        metadata=metadata,
        info=info,
        disposition=disposition,
    )


def names(books: list[Book]) -> list[str]:
    return [b.display_name for b in books]


class Orders(unittest.TestCase):
    def test_every_order_has_a_unique_name(self) -> None:
        found = [order.name for order in SORT_ORDERS]
        self.assertEqual(len(found), len(set(found)))

    def test_an_unknown_name_falls_back_to_the_default(self) -> None:
        self.assertIs(order_by_name("no such order"), DEFAULT_ORDER)
        self.assertIs(order_by_name("title"), SORT_ORDERS[1])

    def test_every_order_sorts_a_mixed_list_without_raising(self) -> None:
        # Keys must not mix types: one unorderable pair would only surface
        # when a book happens to be missing the field the key reads.
        books = [
            book("full.m4b", title="T", author="A", series="S", sequence="2", bitrate=64),
            book("bare.m4b"),
            book("partial.m4b", author="B", seconds=60),
        ]
        dups = {books[0].key: library.DuplicateCheck(status=library.DUPLICATE)}
        for order in SORT_ORDERS:
            for descending in (False, True):
                with self.subTest(order=order.name, descending=descending):
                    result = sort_books(
                        books, order, descending=descending, duplicates=dups
                    )
                    self.assertEqual(sorted(names(result)), sorted(names(books)))

    def test_title_sorts_alphabetically_and_reverses(self) -> None:
        books = [
            book("c.m4b", title="Cormorant"),
            book("a.m4b", title="Albatross"),
            book("b.m4b", title="Bittern"),
        ]
        order = order_by_name("title")
        self.assertEqual(names(sort_books(books, order)), ["a.m4b", "b.m4b", "c.m4b"])
        self.assertEqual(
            names(sort_books(books, order, descending=True)),
            ["c.m4b", "b.m4b", "a.m4b"],
        )

    def test_a_missing_title_falls_back_to_the_file_name(self) -> None:
        # Untitled "mmm" sorts between nothing and "Zebra" only if its file
        # name stands in for the missing title; an empty string would put it
        # first instead.
        books = [book("aaa.m4b", title="Zebra"), book("mmm.m4b")]
        self.assertEqual(
            names(sort_books(books, order_by_name("title"))), ["mmm.m4b", "aaa.m4b"]
        )

    def test_blanks_sink_in_both_directions(self) -> None:
        books = [
            book("none.m4b"),
            book("beta.m4b", author="Beta"),
            book("alpha.m4b", author="Alpha"),
        ]
        order = order_by_name("author")
        self.assertEqual(
            names(sort_books(books, order)), ["alpha.m4b", "beta.m4b", "none.m4b"]
        )
        # Reversing must not promote the book that has no author at all.
        self.assertEqual(
            names(sort_books(books, order, descending=True)),
            ["beta.m4b", "alpha.m4b", "none.m4b"],
        )

    def test_series_orders_by_number_not_by_text(self) -> None:
        books = [
            book("ten.m4b", series="Cycle", sequence="10"),
            book("two.m4b", series="Cycle", sequence="2"),
            book("half.m4b", series="Cycle", sequence="2.5"),
        ]
        self.assertEqual(
            names(sort_books(books, order_by_name("series"))),
            ["two.m4b", "half.m4b", "ten.m4b"],
        )

    def test_a_non_numeric_sequence_sorts_after_the_numbered_ones(self) -> None:
        books = [
            book("novella.m4b", series="Cycle", sequence="a novella"),
            book("one.m4b", series="Cycle", sequence="1"),
        ]
        self.assertEqual(
            names(sort_books(books, order_by_name("series"))),
            ["one.m4b", "novella.m4b"],
        )

    def test_bitrate_orders_lowest_first_and_unprobed_books_sink(self) -> None:
        books = [
            book("mid.m4b", bitrate=96),
            book("unprobed.m4b"),
            book("low.m4b", bitrate=32),
        ]
        self.assertEqual(
            names(sort_books(books, order_by_name("bitrate"))),
            ["low.m4b", "mid.m4b", "unprobed.m4b"],
        )

    def test_duration_orders_shortest_first(self) -> None:
        books = [
            book("long.m4b", bitrate=64, seconds=3600),
            book("short.m4b", bitrate=64, seconds=60),
        ]
        self.assertEqual(
            names(sort_books(books, order_by_name("duration"))),
            ["short.m4b", "long.m4b"],
        )

    def test_disposition_orders_untriaged_first(self) -> None:
        books = [
            book("done.m4b", disposition=D.DONE),
            book("new.m4b", disposition=D.UNTRIAGED),
            book("downcode.m4b", disposition=D.DOWNCODE),
        ]
        self.assertEqual(
            names(sort_books(books, order_by_name("disposition"))),
            ["new.m4b", "downcode.m4b", "done.m4b"],
        )

    def test_duplicates_come_first_and_unchecked_books_sink(self) -> None:
        dup = book("dup.m4b", title="Dup")
        new = book("new.m4b", title="New")
        unchecked = book("unchecked.m4b", title="Unchecked")
        dups = {
            dup.key: library.DuplicateCheck(status=library.DUPLICATE),
            new.key: library.DuplicateCheck(status=library.UNIQUE),
        }
        self.assertEqual(
            names(
                sort_books(
                    [unchecked, new, dup], order_by_name("duplicate"), duplicates=dups
                )
            ),
            ["dup.m4b", "new.m4b", "unchecked.m4b"],
        )

    def test_the_duplicate_order_tolerates_an_empty_map(self) -> None:
        books = [book("b.m4b"), book("a.m4b")]
        self.assertEqual(
            sorted(names(sort_books(books, order_by_name("duplicate")))),
            ["a.m4b", "b.m4b"],
        )

    def test_the_default_order_puts_what_needs_attention_first(self) -> None:
        attention = book("attention.m4b", title="T", author="A", bitrate=64,
                         disposition=D.NEEDS_ATTENTION)
        ready = book("ready.m4b", title="T", author="A", bitrate=64)
        self.assertEqual(
            names(sort_books([ready, attention], DEFAULT_ORDER)),
            ["attention.m4b", "ready.m4b"],
        )

    def test_sorting_does_not_mutate_the_list_it_was_given(self) -> None:
        books = [book("b.m4b", title="B"), book("a.m4b", title="A")]
        original = list(books)
        sort_books(books, order_by_name("title"))
        self.assertEqual(books, original)


if __name__ == "__main__":
    unittest.main()
