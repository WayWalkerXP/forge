"""The title-phrase editor."""

from __future__ import annotations

import hashlib
import unittest

from tests.helpers import TempCaseMixin, write_config

from forge import config as config_mod
from forge.titles import PhraseStore

try:
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAVE_TEXTUAL = False

requires_textual = unittest.skipUnless(HAVE_TEXTUAL, "textual is required")


@requires_textual
class EditorCase(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        with PhraseStore(self.cfg.titles_db_path) as store:
            store.add("A LitRPG Adventure")
            store.add("Book One of the Chronicles")

    def app(self):
        from forge.tui.titles_app import PhraseReviewApp

        return PhraseReviewApp(self.cfg)

    def db_hash(self) -> str:
        return hashlib.sha256(self.cfg.titles_db_path.read_bytes()).hexdigest()

    def phrases(self, app) -> list[str]:
        return sorted(phrase for phrase, _ in app.working)

    def select(self, app, phrase: str) -> None:
        row = [r for r in app.shown if r[0] == phrase][0]
        app.query_one("#phrases").move_cursor(row=app.shown.index(row))


class Listing(EditorCase):
    async def test_the_phrases_are_listed_longest_first(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                [row[0] for row in pilot.app.shown],
                ["Book One of the Chronicles", "A LitRPG Adventure"],
            )

    async def test_the_detail_shows_what_a_phrase_does(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "A LitRPG Adventure")
            await pilot.pause()
            rendered = str(app.query_one("#detail", Static).renderable)
            self.assertIn("The Land", rendered)

    async def test_searching_narrows_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.search_text = "chronicles"
            app.refresh_table()
            await pilot.pause()
            self.assertEqual([row[0] for row in app.shown], ["Book One of the Chronicles"])


class Editing(EditorCase):
    async def test_adding_is_pending_until_saved(self) -> None:
        from forge.tui.title_screens import EditPhraseScreen

        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, EditPhraseScreen)
            app.screen.dismiss("An Epic Fantasy Saga")
            await pilot.pause()

            self.assertIn("An Epic Fantasy Saga", self.phrases(app))
            self.assertEqual(app.dirty, 1)
            self.assertEqual(self.db_hash(), before)

    async def test_a_duplicate_is_refused(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("a")
            await pilot.pause()
            app.screen.dismiss("a litrpg adventure")
            await pilot.pause()
            self.assertEqual(app.dirty, 0)

    async def test_a_blank_phrase_is_refused_in_place(self) -> None:
        from textual.widgets import Input, Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#phrase", Input).value = "   "
            await pilot.press("ctrl+s")
            await pilot.pause()

            self.assertEqual(len(app.screen_stack), 2)
            self.assertIn("needs some text", str(screen.query_one("#error", Static).renderable))

    async def test_the_edit_dialog_previews_the_effect(self) -> None:
        from textual.widgets import Input, Static

        async with self.app().run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#phrase", Input).value = "A LitRPG Adventure"
            await pilot.pause()
            self.assertIn("The Land", str(screen.query_one("#preview", Static).renderable))

    async def test_editing_replaces_the_phrase(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "A LitRPG Adventure")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            app.screen.dismiss("A LitRPG Epic")
            await pilot.pause()

            self.assertIn("A LitRPG Epic", self.phrases(app))
            self.assertNotIn("A LitRPG Adventure", self.phrases(app))
            self.assertEqual(app.dirty, 2)  # one added, one removed

    async def test_a_phrase_can_be_deleted(self) -> None:
        # Allowed here, unlike an author identity: a phrase is a decision
        # about text, not a record of anything.
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "A LitRPG Adventure")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()

            self.assertEqual(app.removed, ["A LitRPG Adventure"])
            self.assertNotIn("A LitRPG Adventure", self.phrases(app))

    async def test_undo_puts_everything_unsaved_back(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            self.select(app, "A LitRPG Adventure")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()

            self.assertEqual(app.dirty, 0)
            self.assertIn("A LitRPG Adventure", self.phrases(app))

    async def test_a_title_can_be_tried_against_the_list(self) -> None:
        from textual.widgets import Input, Static

        from forge.tui.title_screens import TestTitleScreen

        async with self.app().run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            screen = pilot.app.screen
            self.assertIsInstance(screen, TestTitleScreen)

            screen.query_one("#probe", Input).value = "The Sea (A LitRPG Adventure)"
            await pilot.pause()
            self.assertIn("The Sea", str(screen.query_one("#result", Static).renderable))


class Saving(EditorCase):
    async def test_saving_asks_first(self) -> None:
        from forge.tui.screens import ConfirmScreen

        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.working.append(("An Epic Fantasy Saga", ""))
            app.refresh_table()

            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("escape")
            await pilot.pause()

            self.assertEqual(app.dirty, 1)
            self.assertEqual(self.db_hash(), before)

    async def test_confirming_writes_the_additions_and_removals(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.working.append(("An Epic Fantasy Saga", ""))
            app.working = [r for r in app.working if r[0] != "A LitRPG Adventure"]
            app.refresh_table()

            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app.dirty, 0)

        with PhraseStore(self.cfg.titles_db_path) as store:
            self.assertEqual(
                sorted(store.phrases()),
                ["An Epic Fantasy Saga", "Book One of the Chronicles"],
            )

    async def test_discarding_on_quit_writes_nothing(self) -> None:
        from forge.tui.author_screens import UnsavedChangesScreen

        before = self.db_hash()
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.working.append(("An Epic Fantasy Saga", ""))
            app.refresh_table()

            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, UnsavedChangesScreen)
            await pilot.press("2")
            await pilot.pause()
            self.assertFalse(app.is_running)

        self.assertEqual(self.db_hash(), before)

    async def test_save_and_quit_writes_then_leaves(self) -> None:
        async with self.app().run_test() as pilot:
            await pilot.pause()
            app = pilot.app
            app.working.append(("An Epic Fantasy Saga", ""))
            app.refresh_table()

            await pilot.press("q")
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
            self.assertFalse(app.is_running)

        with PhraseStore(self.cfg.titles_db_path) as store:
            self.assertIn("An Epic Fantasy Saga", store.phrases())

    async def test_the_editor_touches_nothing_else(self) -> None:
        import inspect

        from forge.tui import titles_app

        source = inspect.getsource(titles_app)
        for forbidden in ("from ..history", "from ..shelve", "tags_mod", "from ..backfill"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
