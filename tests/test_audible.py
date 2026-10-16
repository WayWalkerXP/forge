"""Audible payload mapping and search heuristics.

Offline only: every test here works on captured payload shapes, so the suite
never depends on the network or on a particular book still being for sale.
"""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin  # noqa: F401  (sys.path setup)

from forge import audible as A

PRODUCT = {
    "asin": "B0D8XMMWLF",
    "title": "Pawn of Prophecy",
    "subtitle": "The Belgariad, Book 1",
    "authors": [{"name": "David Eddings"}],
    "narrators": [{"name": "Brian Wiggins"}],
    "series": [{"title": "The Belgariad", "sequence": "1"}],
    "publisher_name": "Tantor Media",
    "release_date": "2024-07-16",
    "language": "english",
    "runtime_length_min": 614,
    "is_adult_product": False,
    "publisher_summary": "<p>A magnificent epic.<br>Second line.</p>",
    "category_ladders": [
        {"ladder": [{"name": "Science Fiction & Fantasy"}, {"name": "Fantasy"}]},
        {"ladder": [{"name": "Fantasy"}, {"name": "Epic"}]},
    ],
    "product_images": {"500": "https://example/500.jpg", "1000": "https://example/1000.jpg"},
}


class Mapping(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = A.normalize_product(PRODUCT)

    def test_core_fields(self) -> None:
        self.assertEqual(self.meta.asin, "B0D8XMMWLF")
        self.assertEqual(self.meta.title, "Pawn of Prophecy")
        self.assertEqual(self.meta.subtitle, "The Belgariad, Book 1")
        self.assertEqual(self.meta.author, "David Eddings")
        self.assertEqual(self.meta.narrator, "Brian Wiggins")
        self.assertEqual(self.meta.publisher, "Tantor Media")
        self.assertEqual(self.meta.language, "english")

    def test_series_and_sequence(self) -> None:
        self.assertEqual(self.meta.series, "The Belgariad")
        self.assertEqual(self.meta.series_sequence, "1")

    def test_year_is_derived_from_the_release_date(self) -> None:
        self.assertEqual(self.meta.published_date, "2024-07-16")
        self.assertEqual(self.meta.published_year, "2024")

    def test_runtime_is_converted_to_seconds(self) -> None:
        self.assertEqual(self.meta.duration, 614 * 60)

    def test_genres_are_flattened_and_deduplicated(self) -> None:
        self.assertEqual(self.meta.genres, ["Science Fiction & Fantasy", "Fantasy", "Epic"])

    def test_the_largest_cover_is_chosen(self) -> None:
        self.assertEqual(self.meta.cover_url, "https://example/1000.jpg")

    def test_html_is_stripped_from_the_description(self) -> None:
        self.assertNotIn("<", self.meta.description)
        self.assertIn("A magnificent epic.", self.meta.description)

    def test_multiple_contributors_are_joined(self) -> None:
        meta = A.normalize_product({**PRODUCT, "authors": [{"name": "A"}, {"name": "B"}]})
        self.assertEqual(meta.author, "A, B")

    def test_missing_fields_are_none_not_errors(self) -> None:
        meta = A.normalize_product({"asin": "B000000000"})
        self.assertIsNone(meta.title)
        self.assertIsNone(meta.series)
        self.assertEqual(meta.genres, [])


class Updates(unittest.TestCase):
    def setUp(self) -> None:
        self.updates = A.normalize_product(PRODUCT).to_updates()

    def test_title_populates_both_title_and_album(self) -> None:
        # Audiobook tooling reads the book name from the album tag.
        self.assertEqual(self.updates["album"], "Pawn of Prophecy")
        self.assertEqual(self.updates["title"], "Pawn of Prophecy")

    def test_runtime_is_never_written(self) -> None:
        self.assertNotIn("duration", self.updates)

    def test_cover_url_is_not_written_directly(self) -> None:
        self.assertNotIn("cover_url", self.updates)

    def test_every_key_is_a_writable_field(self) -> None:
        from forge import tags

        writable = {name for name, _ in tags.EDITABLE_FIELDS}
        self.assertEqual(set(self.updates) - writable, set())

    def test_empty_values_are_omitted(self) -> None:
        updates = A.normalize_product({"asin": "B000000000"}).to_updates()
        self.assertEqual(set(updates), {"asin"})


class SearchHeuristics(unittest.TestCase):
    def test_trailing_disc_and_part_noise_is_stripped(self) -> None:
        for raw, expected in (
            ("Pawn of Prophecy - cd 3", "Pawn of Prophecy"),
            ("Pawn of Prophecy part 2", "Pawn of Prophecy"),
            ("Pawn of Prophecy (Unabridged)", "Pawn of Prophecy"),
            ("Book (Narrated by Someone)", "Book"),
            ("Dune Book 1", "Dune"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(A.clean_search_title(raw), expected)

    def test_a_clean_title_is_untouched(self) -> None:
        self.assertEqual(A.clean_search_title("Odd Thomas"), "Odd Thomas")

    def test_cleaning_never_returns_empty(self) -> None:
        self.assertTrue(A.clean_search_title("part 2"))

    def test_asin_validation(self) -> None:
        self.assertEqual(A.validate_asin(" b0d8xmmwlf "), "B0D8XMMWLF")
        for bad in (None, "", "TOO SHORT", "B0D8XMMWL", "B0D8XMMWLFX", "b0d8-mmwlf"):
            with self.subTest(bad=bad):
                with self.assertRaises(A.AudibleError):
                    A.validate_asin(bad)

    def test_unknown_region_is_rejected(self) -> None:
        with self.assertRaises(A.AudibleError):
            A.AudibleClient(region="zz")


class RuntimeConfidence(unittest.TestCase):
    def result(self, minutes):
        return A.SearchResult(
            asin="B000000000", title="T", author="A", narrator="N", series="",
            runtime_minutes=minutes, product={},
        )

    def test_a_close_runtime_is_high_confidence(self) -> None:
        source = 600 * 60
        self.assertEqual(self.result(600).confidence(source), "high")
        self.assertEqual(self.result(604).confidence(source), "high")

    def test_a_moderate_gap_is_medium(self) -> None:
        self.assertEqual(self.result(610).confidence(600 * 60), "medium")

    def test_a_large_gap_is_low(self) -> None:
        # Usually a different edition or an abridgement.
        self.assertEqual(self.result(700).confidence(600 * 60), "low")

    def test_an_unknown_runtime_is_unknown(self) -> None:
        self.assertEqual(self.result(None).confidence(600 * 60), "unknown")
        self.assertEqual(self.result(600).confidence(None), "unknown")

    def test_ranking_puts_the_closest_first(self) -> None:
        results = [self.result(700), self.result(601), self.result(650)]
        ranked = A.rank_by_runtime(results, 600 * 60)
        self.assertEqual([r.runtime_minutes for r in ranked], [601, 650, 700])

    def test_unknown_runtimes_rank_last(self) -> None:
        # They cannot be assessed, so they should not displace one that can.
        results = [self.result(None), self.result(700)]
        ranked = A.rank_by_runtime(results, 600 * 60)
        self.assertEqual([r.runtime_minutes for r in ranked], [700, None])


if __name__ == "__main__":
    unittest.main()
