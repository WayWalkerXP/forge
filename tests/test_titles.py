"""Marketing text in titles, and taking it back out."""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin

from forge.titles import (
    PhraseStore,
    phrase_key,
    strip_phrases,
    suggest_phrases,
    tidy,
)

LITRPG = ["A LitRPG Adventure"]


class Stripping(unittest.TestCase):
    def test_every_way_a_publisher_hangs_it_on(self) -> None:
        for title in (
            "The Land: A LitRPG Adventure",
            "The Land (A LitRPG Adventure)",
            "The Land [A LitRPG Adventure]",
            "The Land - A LitRPG Adventure",
            "The Land – A LitRPG Adventure",
            "The Land, A LitRPG Adventure",
            "The Land A LitRPG Adventure",
        ):
            with self.subTest(title=title):
                self.assertEqual(strip_phrases(title, LITRPG), "The Land")

    def test_the_match_ignores_case_and_spacing(self) -> None:
        self.assertEqual(
            strip_phrases("the land: a   litrpg   adventure", LITRPG), "the land"
        )

    def test_a_question_mark_on_the_title_survives(self) -> None:
        self.assertEqual(
            strip_phrases("What Could Go Wrong?: A LitRPG Adventure", LITRPG),
            "What Could Go Wrong?",
        )

    def test_an_exclamation_mark_on_the_title_survives(self) -> None:
        self.assertEqual(
            strip_phrases("Welcome to the Land!: A LitRPG Adventure", LITRPG),
            "Welcome to the Land!",
        )

    def test_punctuation_belonging_to_the_marketing_does_not(self) -> None:
        # The "!" is behind a colon and a space, so it was punctuating the
        # phrase, not the book.
        self.assertEqual(
            strip_phrases("The Land: A LitRPG Adventure!", LITRPG), "The Land"
        )

    def test_a_phrase_at_the_front_is_removed_too(self) -> None:
        self.assertEqual(
            strip_phrases("A LitRPG Adventure: The Land", LITRPG), "The Land"
        )

    def test_a_phrase_in_the_middle_leaves_one_separator(self) -> None:
        self.assertEqual(
            strip_phrases("The Land: A LitRPG Adventure, Book 1", LITRPG),
            "The Land, Book 1",
        )

    def test_a_title_that_is_only_the_phrase_is_left_alone(self) -> None:
        # A book with no title has no name and no shelf; an untidy title is
        # merely untidy.
        self.assertEqual(strip_phrases("A LitRPG Adventure", LITRPG), "A LitRPG Adventure")

    def test_a_title_without_the_phrase_is_untouched(self) -> None:
        self.assertEqual(strip_phrases("The Land", LITRPG), "The Land")
        self.assertIsNone(strip_phrases(None, LITRPG))
        self.assertEqual(strip_phrases("", LITRPG), "")

    def test_a_partial_word_is_not_a_match(self) -> None:
        self.assertEqual(
            strip_phrases("The Landing: A LitRPG Adventures Guide", ["LitRPG Adventure"]),
            "The Landing: A LitRPG Adventures Guide",
        )

    def test_several_phrases_are_all_removed(self) -> None:
        phrases = ["A LitRPG Adventure", "Book One of the Chronicles"]
        self.assertEqual(
            strip_phrases(
                "The Land: A LitRPG Adventure (Book One of the Chronicles)", phrases
            ),
            "The Land",
        )

    def test_a_phrase_inside_another_is_removed_whole(self) -> None:
        # The store hands them over longest first for exactly this reason.
        with_both = ["A LitRPG Adventure", "LitRPG"]
        self.assertEqual(
            strip_phrases("The Land: A LitRPG Adventure", with_both), "The Land"
        )

    def test_brackets_around_real_text_are_kept(self) -> None:
        self.assertEqual(
            strip_phrases("The Land (Deluxe Edition): A LitRPG Adventure", LITRPG),
            "The Land (Deluxe Edition)",
        )

    def test_no_phrases_means_no_change(self) -> None:
        self.assertEqual(strip_phrases("The Land: A LitRPG Adventure", []), "The Land: A LitRPG Adventure")

    def test_tidy_leaves_an_ordinary_title_alone(self) -> None:
        self.assertEqual(tidy("The Land"), "The Land")
        self.assertEqual(tidy("Who Goes There?"), "Who Goes There?")

    def test_tidy_on_nothing_but_punctuation(self) -> None:
        self.assertEqual(tidy(" : () "), "")


class Store(TempCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = PhraseStore(self.root / "titles.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_phrase_can_be_added_and_listed(self) -> None:
        self.assertTrue(self.store.add("A LitRPG Adventure"))
        self.assertEqual(self.store.phrases(), ["A LitRPG Adventure"])
        self.assertEqual(len(self.store), 1)

    def test_the_same_phrase_in_another_style_is_not_added_twice(self) -> None:
        self.store.add("A LitRPG Adventure")
        self.assertFalse(self.store.add("a   litrpg adventure"))
        self.assertEqual(len(self.store), 1)

    def test_a_blank_phrase_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add("   ")

    def test_a_phrase_can_be_removed(self) -> None:
        self.store.add("A LitRPG Adventure")
        self.assertTrue(self.store.remove("A LITRPG ADVENTURE"))
        self.assertEqual(self.store.phrases(), [])
        self.assertFalse(self.store.remove("A LitRPG Adventure"))

    def test_phrases_come_back_longest_first(self) -> None:
        for phrase in ("LitRPG", "A LitRPG Adventure", "An Adventure"):
            self.store.add(phrase)
        self.assertEqual(self.store.phrases()[0], "A LitRPG Adventure")

    def test_the_store_survives_being_reopened(self) -> None:
        self.store.add("A LitRPG Adventure")
        self.store.close()
        with PhraseStore(self.root / "titles.sqlite3") as reopened:
            self.assertEqual(reopened.strip("The Land: A LitRPG Adventure"), "The Land")

    def test_stripping_through_the_store(self) -> None:
        self.store.add("A LitRPG Adventure")
        self.assertEqual(self.store.strip("The Land (A LitRPG Adventure)"), "The Land")


class Suggesting(unittest.TestCase):
    def test_text_trailing_several_titles_is_offered(self) -> None:
        titles = [
            "The Land: A LitRPG Adventure",
            "The Sea: A LitRPG Adventure",
            "The Sky (A LitRPG Adventure)",
            "A Quiet Book",
        ]
        self.assertEqual(suggest_phrases(titles)[0], ("A LitRPG Adventure", 3))

    def test_text_trailing_one_title_is_not(self) -> None:
        self.assertEqual(suggest_phrases(["The Land: A LitRPG Adventure"]), [])

    def test_the_key_collapses_case_and_spacing(self) -> None:
        self.assertEqual(phrase_key("  A   LitRPG   Adventure "), "a litrpg adventure")


if __name__ == "__main__":
    unittest.main()
