"""Reconciling a library that was built before the author registry existed."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin, copy_audio, requires_ffmpeg, write_config

from forge import backfill as backfill_mod
from forge import config as config_mod
from forge import tags
from forge.authors import AuthorRegistry, Contributor

CHANEY = "B00W2ZAK7E"
MAGGERT = "B00EKN8RHG"


class FakeAudible:
    """Audible, as far as the identify phase is concerned."""

    def __init__(self, answers: dict | None = None, fail: bool = False) -> None:
        self.answers = answers or {}
        self.fail = fail
        self.calls: list[str] = []

    def contributors(self, asin):
        self.calls.append(asin)
        if self.fail:
            raise RuntimeError("Audible is having a day")
        return self.answers.get(asin, ())


@requires_ffmpeg
class BackfillCase(TempCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.cfg = config_mod.load(write_config(self.root, library_dir=self.library))
        self.registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(self.registry.close)

    def shelve_book(self, *parts: str, sidecars: bool = True, **fields) -> Path:
        """Put a book in the library at a literal path, as the old code would."""

        path = self.library.joinpath(*parts)
        copy_audio(path, seconds=1)
        if fields:
            tags.write_metadata(path, fields)
        if sidecars:
            (path.parent / "metadata.json").write_text('{"tags": []}')
            (path.parent / "cover.jpg").write_bytes(b"not really a jpeg")
        return path

    def survey(self):
        surveyed, unreadable = backfill_mod.survey(self.library)
        return surveyed, unreadable

    def plan(self):
        surveyed, _ = self.survey()
        return backfill_mod.plan(surveyed, self.cfg, self.registry)


class Surveying(BackfillCase):
    def test_every_book_is_found_with_its_author(self) -> None:
        self.shelve_book("JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book")
        self.shelve_book("Rhaegar", "Azarinth", "Azarinth.m4b", author="Rhaegar", album="Azarinth")

        surveyed, unreadable = self.survey()
        self.assertEqual(
            sorted(book.author_raw for book in surveyed), ["JN Chaney", "Rhaegar"]
        )
        self.assertEqual(unreadable, [])

    def test_a_book_with_no_author_is_reported_not_planned(self) -> None:
        self.shelve_book("Unknown", "A Book", "A Book.m4b", album="A Book")
        surveyed, unreadable = self.survey()
        self.assertEqual(surveyed, [])
        self.assertEqual(len(unreadable), 1)
        self.assertIn("no author", unreadable[0][1])

    def test_role_credits_are_cleaned_in_the_survey(self) -> None:
        self.shelve_book(
            "X", "A Book", "A Book.m4b",
            author="J.N. Chaney, Someone - translator", album="A Book",
        )
        surveyed, _ = self.survey()
        self.assertEqual(surveyed[0].cleaned, "J.N. Chaney")


class Identifying(BackfillCase):
    def test_one_lookup_settles_a_spelling_however_many_books_use_it(self) -> None:
        for index in (1, 2, 3):
            self.shelve_book(
                "JN Chaney", f"Book {index}", f"Book {index}.m4b",
                author="JN Chaney", album=f"Book {index}", asin=f"B00000000{index}",
            )
        audible = FakeAudible({f"B00000000{i}": (Contributor(CHANEY, "J.N. Chaney"),)
                               for i in (1, 2, 3)})

        surveyed, _ = self.survey()
        report = backfill_mod.identify(surveyed, self.registry, audible)

        self.assertEqual(len(audible.calls), 1)
        self.assertEqual(report.looked_up, 1)
        self.assertEqual(self.registry.identity("JN Chaney"), CHANEY)

    def test_a_collaboration_is_settled_by_one_lookup(self) -> None:
        self.shelve_book(
            "Pair", "A Book", "A Book.m4b",
            author="JN Chaney, Terry Maggert", album="A Book", asin="B000000001",
        )
        audible = FakeAudible({
            "B000000001": (Contributor(CHANEY, "J.N. Chaney"),
                           Contributor(MAGGERT, "Terry Maggert")),
        })

        surveyed, _ = self.survey()
        backfill_mod.identify(surveyed, self.registry, audible)

        self.assertEqual(len(audible.calls), 1)
        self.assertEqual(self.registry.identity("Terry Maggert"), MAGGERT)

    def test_an_author_already_known_costs_no_lookup(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b",
            author="JN Chaney", album="A Book", asin="B000000001",
        )
        audible = FakeAudible()

        surveyed, _ = self.survey()
        report = backfill_mod.identify(surveyed, self.registry, audible)

        self.assertEqual(audible.calls, [])
        self.assertEqual(report.already_known, 1)

    def test_a_book_with_no_asin_cannot_be_identified(self) -> None:
        self.shelve_book("Nobody", "A Book", "A Book.m4b", author="Nobody", album="A Book")
        surveyed, _ = self.survey()
        report = backfill_mod.identify(surveyed, self.registry, FakeAudible())

        self.assertEqual(report.no_asin, 1)
        self.assertEqual(self.registry.unknown_names(), [("Nobody", 1)])

    def test_a_failed_lookup_leaves_the_author_alone(self) -> None:
        self.shelve_book(
            "Nobody", "A Book", "A Book.m4b", author="Nobody", album="A Book",
            asin="B000000001",
        )
        surveyed, _ = self.survey()
        report = backfill_mod.identify(surveyed, self.registry, FakeAudible(fail=True))

        self.assertEqual(report.failed, 1)
        self.assertIsNone(self.registry.identity("Nobody"))


class ChoosingNames(BackfillCase):
    def books(self, spellings: dict[str, int]) -> None:
        index = 0
        for spelling, count in spellings.items():
            for _ in range(count):
                index += 1
                self.shelve_book(
                    spelling, f"Book {index}", f"Book {index}.m4b",
                    author=spelling, album=f"Book {index}",
                )

    def test_the_commonest_spelling_in_the_library_wins(self) -> None:
        self.registry.learn([Contributor(CHANEY, "JN Chaney")])
        self.books({"J.N. Chaney": 5, "J. N. Chaney": 2, "JN Chaney": 1})

        surveyed, _ = self.survey()
        changes = backfill_mod.choose_canonical_names(surveyed, self.registry)

        self.assertEqual(changes, [(CHANEY, "JN Chaney", "J.N. Chaney")])
        self.assertEqual(self.registry.canonical_of(CHANEY), "J.N. Chaney")

    def test_a_tie_prefers_the_longer_spelling(self) -> None:
        # "J. N. Chaney" over "JN Chaney": the spaced form is the one a person
        # would have typed on purpose.
        self.registry.learn([Contributor(CHANEY, "JN Chaney")])
        self.books({"J. N. Chaney": 2, "JN Chaney": 2})

        surveyed, _ = self.survey()
        backfill_mod.choose_canonical_names(surveyed, self.registry)
        self.assertEqual(self.registry.canonical_of(CHANEY), "J. N. Chaney")

    def test_a_pinned_name_is_left_alone(self) -> None:
        self.registry.learn([Contributor(CHANEY, "JN Chaney")])
        self.registry.rename(CHANEY, "J. N. Chaney")
        self.books({"J.N. Chaney": 9})

        surveyed, _ = self.survey()
        self.assertEqual(backfill_mod.choose_canonical_names(surveyed, self.registry), [])
        self.assertEqual(self.registry.canonical_of(CHANEY), "J. N. Chaney")

    def test_an_unidentified_author_gets_no_vote(self) -> None:
        self.books({"A Stranger": 3})
        surveyed, _ = self.survey()
        self.assertEqual(backfill_mod.choose_canonical_names(surveyed, self.registry), [])


class Planning(BackfillCase):
    def test_a_book_already_correct_needs_nothing(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.shelve_book(
            "J.N. Chaney", "A Book", "A Book.m4b", author="J.N. Chaney", album="A Book"
        )
        action = self.plan()[0]
        self.assertFalse(action.wanted)
        self.assertEqual(action.describe(), "nothing to do")

    def test_a_variant_spelling_is_retagged_and_moved(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        action = self.plan()[0]

        self.assertEqual(action.tag_fixes.get("author"), "J.N. Chaney")
        self.assertEqual(
            action.target.relative_to(self.library).parts[0], "J.N. Chaney"
        )
        self.assertTrue(action.moves and action.retags)

    def test_the_sidecars_travel_with_the_book(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        action = self.plan()[0]
        self.assertEqual(
            sorted(p.name for p in action.travelling), ["cover.jpg", "metadata.json"]
        )

    def test_a_collaboration_is_filed_one_way_round(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )
        self.shelve_book(
            "J.N. Chaney, Terry Maggert", "First", "First.m4b",
            author="J.N. Chaney, Terry Maggert", album="First",
        )
        self.shelve_book(
            "Terry Maggert, J. N. Chaney", "Second", "Second.m4b",
            author="Terry Maggert, JN Chaney", album="Second",
        )

        shelves = {
            action.target.relative_to(self.library).parts[0] for action in self.plan()
        }
        self.assertEqual(shelves, {"J.N. Chaney, Terry Maggert"})

    def test_two_books_are_never_planned_onto_one_name(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        for folder in ("JN Chaney", "J. N. Chaney"):
            self.shelve_book(
                folder, "Same Book", "Same Book.m4b",
                author=folder, album="Same Book",
            )
        targets = [action.target for action in self.plan()]
        self.assertEqual(len(set(targets)), 2)

    def test_an_unidentified_author_keeps_its_shelf(self) -> None:
        self.shelve_book("A Stranger", "A Book", "A Book.m4b", author="A Stranger", album="A Book")
        action = self.plan()[0]
        self.assertFalse(action.wanted)


class ConsolidatingFolders(BackfillCase):
    """The folder cases the live library actually had."""

    def setUp(self) -> None:
        super().setUp()
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )

    def test_a_variant_spelling_loses_its_books_to_the_canonical_shelf(self) -> None:
        self.shelve_book(
            "J.N. Chaney", "Kept", "Kept.m4b", author="J.N. Chaney", album="Kept"
        )
        self.shelve_book(
            "JN Chaney", "Moving", "Moving.m4b", author="JN Chaney", album="Moving"
        )
        shelves = {
            action.target.relative_to(self.library).parts[0] for action in self.plan()
        }
        self.assertEqual(shelves, {"J.N. Chaney"})

    def test_a_case_variant_author_folder_is_merged(self) -> None:
        # "Funa" against "FUNA", which hid half a series over SMB.
        self.registry.learn([Contributor("B000000009", "FUNA")])
        self.shelve_book("FUNA", "One", "One.m4b", author="FUNA", album="One")
        self.shelve_book("Funa", "Two", "Two.m4b", author="Funa", album="Two")

        shelves = {
            action.target.relative_to(self.library).parts[0] for action in self.plan()
        }
        self.assertEqual(shelves, {"FUNA"})

    def test_a_case_variant_series_folder_is_merged_too(self) -> None:
        # The series level has no canonical spelling to be renamed to, so it
        # folds onto whatever is already there - which is what consolidates
        # "Earth's First StarFighter" and "Earth's First Starfighter".
        self.shelve_book(
            "J.N. Chaney", "Series A", "01 - One", "One.m4b",
            author="J.N. Chaney", album="One", series="Series A", series_sequence="1",
        )
        self.shelve_book(
            "J.N. Chaney", "series a", "02 - Two", "Two.m4b",
            author="J.N. Chaney", album="Two", series="series a", series_sequence="2",
        )
        series = {
            action.target.relative_to(self.library).parts[1] for action in self.plan()
        }
        self.assertEqual(series, {"Series A"})

    def test_the_fuller_shelf_wins_a_collaboration(self) -> None:
        # One book in "Terry Maggert, J.N. Chaney" against two the other way
        # round: the single book moves, not the pair.
        for album in ("One", "Two"):
            self.shelve_book(
                "J.N. Chaney, Terry Maggert", album, f"{album}.m4b",
                author="J.N. Chaney, Terry Maggert", album=album,
            )
        self.shelve_book(
            "Terry Maggert, J.N. Chaney", "Three", "Three.m4b",
            author="Terry Maggert, J.N. Chaney", album="Three",
        )

        moving = [action for action in self.plan() if action.moves]
        self.assertEqual(len(moving), 1)
        self.assertEqual(moving[0].source.name, "Three.m4b")
        self.assertEqual(
            moving[0].target.relative_to(self.library).parts[0],
            "J.N. Chaney, Terry Maggert",
        )

    def test_a_different_line_up_keeps_its_own_shelf(self) -> None:
        self.shelve_book(
            "J.N. Chaney, Terry Maggert", "Pair", "Pair.m4b",
            author="J.N. Chaney, Terry Maggert", album="Pair",
        )
        self.shelve_book(
            "J.N. Chaney", "Solo", "Solo.m4b", author="J.N. Chaney", album="Solo"
        )
        shelves = {
            action.target.relative_to(self.library).parts[0] for action in self.plan()
        }
        self.assertEqual(shelves, {"J.N. Chaney, Terry Maggert", "J.N. Chaney"})


class Applying(BackfillCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])

    def test_a_dry_run_changes_nothing(self) -> None:
        path = self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        action = self.plan()[0]
        backfill_mod.apply(action, self.cfg, dry_run=True)

        self.assertTrue(path.exists())
        self.assertEqual(tags.read_metadata(path).author, "JN Chaney")

    def test_the_book_is_retagged_and_moved_with_its_sidecars(self) -> None:
        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        action = self.plan()[0]
        backfill_mod.apply(action, self.cfg)

        self.assertTrue(action.target.exists())
        self.assertEqual(tags.read_metadata(action.target).author, "J.N. Chaney")
        self.assertEqual(
            sorted(p.name for p in action.target.parent.iterdir()),
            ["A Book.m4b", "cover.jpg", "metadata.json"],
        )

    def test_the_emptied_folders_are_pruned(self) -> None:
        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        backfill_mod.apply(self.plan()[0], self.cfg)

        self.assertFalse((self.library / "JN Chaney").exists())
        self.assertTrue((self.library / "J.N. Chaney").is_dir())

    def test_a_shelf_is_pruned_only_once_it_is_empty(self) -> None:
        # A half-finished run is the normal case, since it can be interrupted
        # between books: the shelf has to survive until the last one leaves.
        self.shelve_book(
            "JN Chaney", "First", "First.m4b", author="JN Chaney", album="First"
        )
        self.shelve_book(
            "JN Chaney", "Second", "Second.m4b", author="JN Chaney", album="Second"
        )

        actions = [action for action in self.plan() if action.wanted]
        self.assertEqual(len(actions), 2)

        backfill_mod.apply(actions[0], self.cfg)
        self.assertTrue((self.library / "JN Chaney").is_dir())

        backfill_mod.apply(actions[1], self.cfg)
        self.assertFalse((self.library / "JN Chaney").exists())

    def test_the_move_is_recorded_in_the_history(self) -> None:
        from forge.history import History

        self.shelve_book(
            "JN Chaney", "A Book", "A Book.m4b", author="JN Chaney", album="A Book"
        )
        action = self.plan()[0]
        backfill_mod.apply(action, self.cfg)

        with History(self.cfg.db_path) as store:
            groups = store.groups_for_file(action.target)
            self.assertTrue(groups, "the move was not recorded")
            changes = store.changes_in_group(groups[0].id)
        fields = {change.field for change in changes}
        self.assertIn("path", fields)
        self.assertIn("author", fields)

    def test_rerunning_is_the_resume(self) -> None:
        # No checkpoint file: a book already correct plans no action, so an
        # interrupted pass is continued by running it again.
        self.shelve_book(
            "JN Chaney", "One", "One.m4b", author="JN Chaney", album="One"
        )
        self.shelve_book(
            "JN Chaney", "Two", "Two.m4b", author="JN Chaney", album="Two"
        )

        first = self.plan()
        backfill_mod.apply(first[0], self.cfg)  # as if interrupted after one

        second = [action for action in self.plan() if action.wanted]
        self.assertEqual(len(second), 1)
        backfill_mod.apply(second[0], self.cfg)
        self.assertEqual([action for action in self.plan() if action.wanted], [])

    def test_the_summary_counts_what_is_needed(self) -> None:
        self.shelve_book(
            "JN Chaney", "One", "One.m4b", author="JN Chaney", album="One"
        )
        self.shelve_book(
            "J.N. Chaney", "Two", "Two.m4b", author="J.N. Chaney", album="Two"
        )
        counts = backfill_mod.summarise(self.plan())
        self.assertEqual(counts["books"], 2)
        self.assertEqual(counts["correct"], 1)
        self.assertEqual(counts["move"], 1)

    def test_the_shelf_count_shows_the_consolidation(self) -> None:
        for folder, author in (
            ("JN Chaney", "JN Chaney"),
            ("J. N. Chaney", "J. N. Chaney"),
            ("J.N. Chaney", "J.N. Chaney"),
        ):
            self.shelve_book(
                folder, f"Book {folder}", f"Book {folder}.m4b",
                author=author, album=f"Book {folder}",
            )
        before, after = backfill_mod.shelves_before_and_after(self.plan(), self.library)
        self.assertEqual((before, after), (3, 1))


if __name__ == "__main__":
    unittest.main()
