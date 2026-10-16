"""The author review interface.

Three properties matter more than any feature here, and each has its own test:
nothing reaches the database until a save, nothing outside the registry is
touched at all, and nothing destructive is on offer.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin, write_config

from forge import config as config_mod
from forge.authors import AuthorRegistry, Contributor

try:
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAVE_TEXTUAL = False

requires_textual = unittest.skipUnless(HAVE_TEXTUAL, "textual is required")

CHANEY = "B00W2ZAK7E"
MAGGERT = "B00EKN8RHG"
COBBLE = "B01GA7NZTE"


@requires_textual
class ReviewCase(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            registry.learn([Contributor(CHANEY, "JN Chaney")])
            registry.learn([Contributor(CHANEY, "Chaney, J.N.")])
            registry.learn([Contributor(MAGGERT, "Terry Maggert")])
            registry.learn([Contributor(COBBLE, "AC Cobble")])
            registry.learn([Contributor(None, "Rhaegar")])
            registry.rename(MAGGERT, "Terry Maggert")  # pinned by hand
            registry.set_book_counts({CHANEY: 55, MAGGERT: 9, COBBLE: 4})
            registry.note_unknown(["Full Cast", "40k Books"])

    def app(self):
        from forge.tui.authors_app import AuthorReviewApp

        return AuthorReviewApp(self.cfg)

    def db_hash(self) -> str:
        return hashlib.sha256(self.cfg.authors_db_path.read_bytes()).hexdigest()

    def row_named(self, app, name: str):
        return [row for row in app.shown if app.canonical_of(row) == name][0]

    def select(self, app, name: str) -> None:
        row = self.row_named(app, name)
        app.query_one("#authors").move_cursor(row=app.shown.index(row))

    def names(self, app) -> list[str]:
        return [app.canonical_of(row) for row in app.shown]

    async def cycle_to(self, pilot, filter_name: str) -> None:
        for _ in range(12):
            if pilot.app.filter_name() == filter_name:
                return
            await pilot.press("f")
            await pilot.pause()
        raise AssertionError(f"never reached the {filter_name} filter")


class Listing(ReviewCase):
    async def test_every_author_is_listed(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            # Four identities: Chaney twice is one author with two spellings.
            self.assertEqual(len(pilot.app.rows), 4)
            self.assertIn("JN Chaney", self.names(pilot.app))

    async def test_the_subtitle_says_when_the_counts_are_from(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            self.assertIn("at the last backfill", pilot.app.sub_title)

    async def test_a_pinned_name_is_marked(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            row = self.row_named(app, "Terry Maggert")
            self.assertTrue(app.pinned_of(row))
            self.assertFalse(app.pinned_of(self.row_named(app, "JN Chaney")))

    async def test_an_author_with_no_asin_says_so(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "Rhaegar")
            await pilot.pause()
            self.assertIn(
                "no contributor id", str(app.query_one("#detail", Static).renderable)
            )


class FilteringAndSorting(ReviewCase):
    async def test_the_respelled_filter_finds_the_review_worthy(self) -> None:
        async with self.app().run_test() as pilot:
            await self.cycle_to(pilot, "respelled")
            self.assertEqual(self.names(pilot.app), ["JN Chaney"])

    async def test_the_pinned_filters_split_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await self.cycle_to(pilot, "pinned")
            self.assertEqual(self.names(pilot.app), ["Terry Maggert"])
            await self.cycle_to(pilot, "unpinned")
            self.assertNotIn("Terry Maggert", self.names(pilot.app))

    async def test_the_no_asin_filter_finds_name_identities(self) -> None:
        async with self.app().run_test() as pilot:
            await self.cycle_to(pilot, "no-asin")
            self.assertEqual(self.names(pilot.app), ["Rhaegar"])

    async def test_the_unidentified_filter_is_read_only(self) -> None:
        async with self.app().run_test() as pilot:
            await self.cycle_to(pilot, "unidentified")
            app = pilot.app
            self.assertEqual(app.shown, [])  # nothing selectable, so nothing editable
            self.assertEqual(len(app.unidentified), 2)
            table = app.query_one("#authors")
            listed = {str(table.get_row_at(index)) for index in range(2)}
            self.assertTrue(any("Full Cast" in row for row in listed), listed)

            await pilot.press("enter")  # must not raise or edit anything
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.pending, {})

    async def test_searching_matches_a_spelling_variant(self) -> None:
        async with self.app().run_test() as pilot:
            app = pilot.app
            await pilot.press("slash")
            for char in "j.n. chaney":
                await pilot.press(char)
            await pilot.pause()
            self.assertEqual(self.names(app), ["JN Chaney"])

    async def test_searching_matches_an_identity(self) -> None:
        async with self.app().run_test() as pilot:
            app = pilot.app
            app.search_text = "name:rhaegar"
            app.refresh_table()
            await pilot.pause()
            self.assertEqual(self.names(app), ["Rhaegar"])

    async def test_sorting_by_books_puts_the_biggest_first(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            self.assertEqual(self.names(pilot.app)[0], "JN Chaney")  # 55 books

    async def test_sorting_by_name_reorders(self) -> None:
        from forge.tui.author_screens import AuthorSortScreen

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("o")
            await pilot.pause()
            self.assertIsInstance(app.screen, AuthorSortScreen)
            await pilot.press("1")  # name
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.names(app), sorted(self.names(app), key=str.casefold))


class Editing(ReviewCase):
    async def test_an_edit_is_pending_and_the_database_is_untouched(self) -> None:
        from forge.tui.author_screens import EditNameScreen

        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "JN Chaney")
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, EditNameScreen)
            app.screen.dismiss("J.N. Chaney")
            await pilot.pause()

            self.assertEqual(app.pending, {CHANEY: "J.N. Chaney"})
            self.assertEqual(self.db_hash(), before, "the database was written to")

        self.assertEqual(self.db_hash(), before)

    async def test_a_known_spelling_can_be_chosen_by_number(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "JN Chaney")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.screen.choices, ["Chaney, J.N."])

            await pilot.press("1")
            await pilot.pause()
            self.assertEqual(app.pending, {CHANEY: "Chaney, J.N."})

    async def test_typing_a_name_needs_the_input_focused_first(self) -> None:
        from textual.widgets import Input

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "JN Chaney")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

            # Focus starts off the input so the numbered spellings work; tab
            # reaches it, and then digits are text again.
            screen = app.screen
            self.assertIsNot(screen.focused, screen.query_one("#name", Input))
            await pilot.press("tab")
            await pilot.pause()
            self.assertIs(screen.focused, screen.query_one("#name", Input))

    async def test_an_author_with_no_other_spelling_starts_in_the_input(self) -> None:
        from textual.widgets import Input

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "Rhaegar")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

            screen = app.screen
            self.assertEqual(screen.choices, [])
            self.assertIs(screen.focused, screen.query_one("#name", Input))

    async def test_a_blank_name_is_refused_in_place(self) -> None:
        from textual.widgets import Input, Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "JN Chaney")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            screen = app.screen
            screen.query_one("#name", Input).value = "   "
            await pilot.press("ctrl+s")
            await pilot.pause()

            self.assertEqual(len(app.screen_stack), 2, "the dialog closed anyway")
            self.assertIn("needs a name", str(screen.query_one("#error", Static).renderable))
            self.assertEqual(app.pending, {})

    async def test_editing_back_to_the_original_clears_the_edit(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "Something Else"
            app.refresh_table()
            await pilot.pause()

            self.select(app, "Something Else")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            app.screen.dismiss("JN Chaney")
            await pilot.pause()

            self.assertEqual(app.pending, {})

    async def test_undo_drops_one_row_s_change(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"
            app.refresh_table()
            await pilot.pause()

            self.select(app, "J.N. Chaney")
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()

            self.assertEqual(app.pending, {})

    async def test_the_pin_can_be_toggled(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "Terry Maggert")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

            self.assertEqual(app.pinning, {MAGGERT: False})
            self.assertFalse(app.pinned_of(self.row_named(app, "Terry Maggert")))

    async def test_the_identity_is_never_editable(self) -> None:
        # Not a binding that refuses: there is no code path at all.
        import inspect

        from forge.tui import authors_app

        source = inspect.getsource(authors_app)
        self.assertNotIn("author_id =", source)
        self.assertNotIn("DELETE FROM", source.upper())


class Saving(ReviewCase):
    async def test_saving_asks_first_and_declining_keeps_the_edits(self) -> None:
        from forge.tui.screens import ConfirmScreen

        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"

            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.pending, {CHANEY: "J.N. Chaney"})
            self.assertEqual(self.db_hash(), before)

    async def test_confirming_writes_every_change_and_pins_it(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"
            app.pending[COBBLE] = "A. C. Cobble"

            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

            self.assertEqual(app.pending, {})

        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            self.assertEqual(registry.canonical_of(CHANEY), "J.N. Chaney")
            self.assertEqual(registry.canonical_of(COBBLE), "A. C. Cobble")
            self.assertTrue(registry.is_pinned(CHANEY))

    async def test_a_pin_change_is_saved_too(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pinning[MAGGERT] = False

            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            self.assertFalse(registry.is_pinned(MAGGERT))

    async def test_saving_nothing_says_so(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIn("nothing to save", str(app.query_one("#status", Static).renderable))


class Quitting(ReviewCase):
    async def test_quitting_clean_just_quits(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(pilot.app.is_running)

    async def test_quitting_with_edits_asks(self) -> None:
        from forge.tui.author_screens import UnsavedChangesScreen

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"

            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, UnsavedChangesScreen)
            self.assertTrue(app.is_running)

            await pilot.press("escape")  # stay
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(app.pending, {CHANEY: "J.N. Chaney"})

    async def test_discarding_quits_without_writing(self) -> None:
        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"

            await pilot.press("q")
            await pilot.pause()
            await pilot.press("2")  # quit without saving
            await pilot.pause()

            self.assertFalse(app.is_running)

        self.assertEqual(self.db_hash(), before)

    async def test_save_and_quit_writes_then_leaves(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"

            await pilot.press("q")
            await pilot.pause()
            await pilot.press("1")  # save and quit
            await pilot.pause()

            self.assertFalse(app.is_running)

        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            self.assertEqual(registry.canonical_of(CHANEY), "J.N. Chaney")


class NoSideEffects(ReviewCase):
    """Reviewing a name must not go and rewrite a library."""

    def tree_hash(self, root: Path) -> list[tuple[str, str]]:
        out = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out.append(
                    (
                        str(path.relative_to(root)),
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
        return out

    async def test_a_whole_session_touches_only_the_registry(self) -> None:
        from tests.helpers import copy_audio

        book = self.cfg.source_dir / "book.m4b"
        copy_audio(book, seconds=1)
        source_before = self.tree_hash(self.cfg.source_dir)
        history_existed = self.cfg.db_path.exists()

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.pending[CHANEY] = "J.N. Chaney"
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

        self.assertEqual(self.tree_hash(self.cfg.source_dir), source_before)
        # No change log either: a name is not a tag edit.
        self.assertEqual(self.cfg.db_path.exists(), history_existed)

    async def test_the_module_imports_nothing_that_writes(self) -> None:
        import inspect

        from forge.tui import authors_app

        source = inspect.getsource(authors_app)
        for forbidden in ("from ..history", "from ..shelve", "from ..backfill", "tags_mod"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
