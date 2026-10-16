"""Author identity: who is this, really?"""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin

from forge.authors import (
    AuthorRegistry,
    Contributor,
    join_authors,
    name_key,
    shelf_key,
    split_authors,
)

CHANEY = "B00W2ZAK7E"
MAGGERT = "B00EKN8RHG"
MORRIS = "B01N7UFRX4"


class Keys(unittest.TestCase):
    def test_punctuation_and_spacing_do_not_change_identity(self) -> None:
        # Every spelling of this author that Amazon has actually produced.
        spellings = ["J.N. Chaney", "J. N. Chaney", "JN Chaney", "J N Chaney", "j.n.chaney"]
        self.assertEqual(len({name_key(s) for s in spellings}), 1)

    def test_a_missing_initial_is_a_different_key(self) -> None:
        # The whole point: these may be two people, so nothing automatic may
        # join them.  Only an author ASIN or a human can.
        self.assertNotEqual(name_key("Brandon Morris"), name_key("Brandon Q. Morris"))

    def test_different_authors_stay_different(self) -> None:
        self.assertNotEqual(name_key("Terry Maggert"), name_key("Terry Mixon"))

    def test_an_author_tag_splits_on_commas_and_semicolons(self) -> None:
        self.assertEqual(
            split_authors("Tao Wong; Jason J. Willis"), ["Tao Wong", "Jason J. Willis"]
        )
        self.assertEqual(split_authors("A, B , C"), ["A", "B", "C"])
        self.assertEqual(split_authors(None), [])

    def test_joining_keeps_order_and_drops_repeats(self) -> None:
        self.assertEqual(join_authors(["B", "A", "B"]), "B, A")

    def test_a_collaboration_is_the_same_shelf_in_either_order(self) -> None:
        self.assertEqual(
            shelf_key("Terry Maggert, J. N. Chaney"),
            shelf_key("J.N. Chaney, Terry Maggert"),
        )

    def test_a_different_line_up_is_a_different_shelf(self) -> None:
        self.assertNotEqual(
            shelf_key("J. N. Chaney"), shelf_key("J. N. Chaney, Terry Maggert")
        )


class Registry(TempCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(self.registry.close)

    def test_an_unknown_author_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.registry.identity("J.N. Chaney"))
        self.assertIsNone(self.registry.canonical_name("J.N. Chaney"))

    def test_learning_one_spelling_resolves_the_others(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        for spelling in ("J. N. Chaney", "JN Chaney", "J N Chaney"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.registry.identity(spelling), CHANEY)
                self.assertEqual(self.registry.canonical_name(spelling), "J.N. Chaney")

    def test_the_first_canonical_name_wins(self) -> None:
        # Audible returns different display names for one ASIN depending on
        # which product is asked about; letting the latest win would make the
        # library rename itself at random.
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.assertEqual(self.registry.canonical_name("JN Chaney"), "J.N. Chaney")

    def test_a_second_spelling_is_recorded_as_an_alias(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "Chaney, J.N.")])
        self.assertEqual(
            sorted(self.registry.aliases_of(CHANEY)), ["Chaney, J.N.", "J.N. Chaney"]
        )

    def test_a_contributor_without_an_asin_is_ignored(self) -> None:
        self.registry.learn([Contributor("", "Nobody In Particular")])
        self.assertIsNone(self.registry.identity("Nobody In Particular"))

    def test_resolving_a_tag_keeps_the_billing_order(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )
        self.assertEqual(
            self.registry.resolve("Terry Maggert, J N Chaney").canonical,
            "Terry Maggert, J.N. Chaney",
        )

    def test_an_unknown_contributor_is_left_exactly_as_it_was(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        outcome = self.registry.resolve("JN Chaney, Some Newcomer")
        self.assertEqual(outcome.canonical, "J.N. Chaney, Some Newcomer")
        self.assertEqual(outcome.unknown, ("Some Newcomer",))
        self.assertFalse(outcome.complete)

    def test_a_tag_that_needs_nothing_reports_no_change(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.assertIsNone(self.registry.resolve("J.N. Chaney").canonical)

    def test_one_alias_row_covers_every_spelling_of_the_same_key(self) -> None:
        # "J. N. CHANEY" in a tag needs no second row: it reduces to the key
        # the contributor Audible named is already stored under.
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.assertEqual(self.registry.aliases_of(CHANEY), ["J.N. Chaney"])
        self.assertEqual(self.registry.canonical_name("J. N. CHANEY"), "J.N. Chaney")

    def test_linking_by_hand_settles_the_missing_initial_case(self) -> None:
        self.registry.learn([Contributor(MORRIS, "Brandon Q. Morris")])
        self.assertIsNone(self.registry.identity("Brandon Morris"))

        self.registry.link("Brandon Morris", MORRIS)
        self.assertEqual(self.registry.identity("Brandon Morris"), MORRIS)
        self.assertEqual(
            self.registry.canonical_name("Brandon Morris"), "Brandon Q. Morris"
        )

    def test_linking_can_create_an_identity_with_a_name(self) -> None:
        self.registry.link("Some Author", "B000000000", canonical_name="Some Author")
        self.assertEqual(self.registry.canonical_name("some author"), "Some Author")

    def test_linking_an_unknown_asin_without_a_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.link("Some Author", "B000000000")

    def test_a_manual_link_overrides_an_earlier_one(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MORRIS, "Brandon Q. Morris")]
        )
        self.registry.link("Mystery Name", CHANEY)
        self.registry.link("Mystery Name", MORRIS)
        self.assertEqual(self.registry.identity("Mystery Name"), MORRIS)

    def test_renaming_changes_every_spelling_at_once(self) -> None:
        self.registry.learn([Contributor(CHANEY, "JN Chaney")])
        self.registry.rename(CHANEY, "J. N. Chaney")
        self.assertEqual(self.registry.canonical_name("J.N. Chaney"), "J. N. Chaney")

    def test_unplaced_spellings_are_remembered_with_a_count(self) -> None:
        self.registry.note_unknown(["A Newcomer"])
        self.registry.note_unknown(["A Newcomer", "Another One"])
        self.assertEqual(
            self.registry.unknown_names(), [("A Newcomer", 2), ("Another One", 1)]
        )

    def test_an_identified_author_leaves_the_unplaced_list(self) -> None:
        self.registry.note_unknown(["J.N. Chaney"])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.assertEqual(self.registry.unknown_names(), [])

    def test_note_unknown_ignores_names_it_already_knows(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.note_unknown(["JN Chaney"])
        self.assertEqual(self.registry.unknown_names(), [])

    def test_the_registry_survives_being_reopened(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.close()
        with AuthorRegistry(self.root / "authors.sqlite3") as reopened:
            self.assertEqual(reopened.canonical_name("J N Chaney"), "J.N. Chaney")

    def test_stats_count_what_is_there(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )
        self.registry.learn([Contributor(CHANEY, "Chaney, J.N.")])
        self.registry.note_unknown(["A Newcomer"])
        self.assertEqual(
            self.registry.stats(), {"authors": 2, "aliases": 3, "unknown": 1}
        )

    def test_identities_report_the_most_respelled_author_first(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.registry.learn([Contributor(MAGGERT, "Terry Maggert")])
        self.assertEqual(self.registry.identities()[0][0], CHANEY)


if __name__ == "__main__":
    unittest.main()
