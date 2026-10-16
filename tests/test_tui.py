"""The triage interface, driven headlessly through textual's test pilot.

These tests assert the two properties that matter most about triage:

*   it writes a disposition only when the user picks one, and
*   it never converts anything.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg, write_config

from forge import config as config_mod
from forge import disposition as d, tags
from forge.disposition import Disposition as D

try:
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAVE_TEXTUAL = False

requires_textual = unittest.skipUnless(HAVE_TEXTUAL, "textual is required")


@requires_ffmpeg
@requires_textual
class TriageInterface(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))

        self.tagged = self.cfg.source_dir / "tagged.mp3"
        copy_mp3(self.tagged, seconds=1)
        tags.write_metadata(
            self.tagged,
            {"author": "Bob Kowalski", "album": "A History of Science", "series": "Science"},
        )

        self.untagged = self.cfg.source_dir / "untagged.mp3"
        copy_mp3(self.untagged, seconds=1)

        self.triaged = self.cfg.source_dir / "triaged.mp3"
        copy_mp3(self.triaged, seconds=1)
        tags.write_metadata(self.triaged, {"author": "A", "album": "B"})
        d.write(self.triaged, D.HOLD)

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    async def wait_for_scan(self, pilot) -> None:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def test_the_list_populates(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            self.assertEqual(len(pilot.app.books), 3)
            self.assertEqual(len(pilot.app.shown), 3)

    async def test_the_status_line_says_what_it_is(self) -> None:
        # Unlabelled, the dim bottom line reads as one more field of the book
        # detail panel immediately above it rather than as the result of the
        # last action.
        from textual.widgets import Static

        from forge.tui.app import STATUS_LABEL

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            pilot.app._status("something happened")
            await pilot.pause()

            rendered = str(pilot.app.query_one("#status", Static).renderable)
            self.assertIn(STATUS_LABEL, rendered)
            self.assertLess(
                rendered.index(STATUS_LABEL), rendered.index("something happened"), rendered
            )

    async def test_an_empty_status_shows_no_label(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            pilot.app._status("")
            await pilot.pause()
            self.assertEqual(str(pilot.app.query_one("#status", Static).renderable), "")

    async def test_readiness_and_disposition_are_separate(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            by_name = {b.display_name: b for b in pilot.app.books}

            self.assertTrue(by_name["tagged.mp3"].is_ready)
            self.assertIs(by_name["tagged.mp3"].disposition, D.UNTRIAGED)

            self.assertFalse(by_name["untagged.mp3"].is_ready)
            self.assertIs(by_name["triaged.mp3"].disposition, D.HOLD)

    async def test_nothing_is_written_just_by_browsing(self) -> None:
        before = {p: p.read_bytes() for p in (self.tagged, self.untagged, self.triaged)}

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            await pilot.press("down", "down", "up")
            await pilot.pause()

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, f"{path.name} was modified")

    async def test_space_opens_the_disposition_screen(self) -> None:
        from forge.tui.screens import DispositionScreen

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            await pilot.press("space")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, DispositionScreen)

    async def test_choosing_a_disposition_writes_the_tag(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            target = pilot.app.shown[0]
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("1")  # downcode, the first assignable choice
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertIs(d.read(target.files[0]), D.DOWNCODE)
            self.assertIs(target.disposition, D.DOWNCODE)

    async def test_escaping_the_disposition_screen_writes_nothing(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = pilot.app.shown[0]
            original = d.read(target.files[0])

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertIs(d.read(target.files[0]), original)

    async def test_a_disposition_can_be_cleared(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "triaged.mp3"][0]
            index = pilot.app.shown.index(target)

            table = pilot.app.query_one("#books")
            table.move_cursor(row=index)
            await pilot.pause()

            # `x`, not `d`: `d` is the duplicate check.
            await pilot.press("x")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertIs(d.read(target.files[0]), D.UNTRIAGED)

    async def test_a_disposition_can_be_set_on_a_book_that_is_not_ready(self) -> None:
        # Disposition is intent; readiness is a fact.  The user may record the
        # intent first and fix the metadata afterwards.
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "untagged.mp3"][0]
            index = pilot.app.shown.index(target)

            pilot.app.query_one("#books").move_cursor(row=index)
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("1")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertIs(d.read(target.files[0]), D.DOWNCODE)
            self.assertFalse(target.is_ready)

    async def test_filters_narrow_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            self.assertEqual(len(pilot.app.shown), 3)

            await pilot.press("f")  # untriaged
            await pilot.pause()
            self.assertTrue(
                all(b.disposition is D.UNTRIAGED for b in pilot.app.shown)
            )

            await pilot.press("f")  # attention
            await pilot.pause()
            self.assertEqual(len(pilot.app.shown), 0)

            await pilot.press("f")  # blocked
            await pilot.pause()
            self.assertEqual(
                [b.display_name for b in pilot.app.shown], ["untagged.mp3"]
            )

            await pilot.press("f")  # ready
            await pilot.pause()
            self.assertTrue(all(b.is_ready for b in pilot.app.shown))

    async def test_search_filters_by_author_and_series(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            pilot.app.search_text = "Kowalski"
            pilot.app.refresh_table()
            await pilot.pause()
            self.assertEqual([b.display_name for b in pilot.app.shown], ["tagged.mp3"])

            pilot.app.search_text = "nothing matches this"
            pilot.app.refresh_table()
            await pilot.pause()
            self.assertEqual(pilot.app.shown, [])

    async def test_the_detail_panel_describes_the_selection(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "tagged.mp3"][0]
            pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
            await pilot.pause()

            rendered = str(pilot.app.query_one("#detail").renderable)
            self.assertIn("tagged.mp3", rendered)
            self.assertIn("mp3", rendered)

    # ------------------------------------------------------------- marking #

    async def mark(self, pilot, name: str) -> None:
        """Put the cursor on *name* and mark it."""

        target = [b for b in pilot.app.shown if b.display_name == name][0]
        pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()

    async def test_marking_a_book_marks_it_and_advances(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app
            first, second = app.shown[0], app.shown[1]

            app.query_one("#books").move_cursor(row=0)
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            self.assertEqual(app.marked, {first.key})
            # Marking a run of books should be one key per book.
            self.assertIs(app.selected, second)

    async def test_marking_the_last_book_leaves_the_cursor_there(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app
            last = app.shown[-1]
            app.query_one("#books").move_cursor(row=len(app.shown) - 1)
            await pilot.pause()

            await pilot.press("m")
            await pilot.pause()

            self.assertEqual(app.marked, {last.key})
            self.assertIs(app.selected, last)

    async def test_marking_the_same_book_twice_unmarks_it(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await self.mark(pilot, "tagged.mp3")
            self.assertEqual(len(app.marked), 1)
            await self.mark(pilot, "tagged.mp3")
            self.assertEqual(app.marked, set())

    async def test_a_mark_shows_in_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app
            row = app.shown.index(
                [b for b in app.shown if b.display_name == "tagged.mp3"][0]
            )
            await self.mark(pilot, "tagged.mp3")

            table = app.query_one("#books")
            self.assertIn("✓", str(table.get_row_at(row)[0]))

    async def test_mark_all_shown_toggles(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            self.assertEqual(app.marked, {b.key for b in app.shown})

            await pilot.press("M")
            await pilot.pause()
            self.assertEqual(app.marked, set())

    async def test_mark_all_shown_respects_the_filter(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("f")  # untriaged: two of the three books
            await pilot.pause()
            await pilot.press("M")
            await pilot.pause()

            self.assertEqual(len(app.marked), 2)
            self.assertNotIn(
                [b for b in app.books if b.display_name == "triaged.mp3"][0].key,
                app.marked,
            )

    async def test_unmark_all_clears_every_mark(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            await pilot.press("U")
            await pilot.pause()

            self.assertEqual(app.marked, set())

    async def test_marks_survive_a_resort_and_a_rescan(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await self.mark(pilot, "tagged.mp3")
            key = [b for b in app.books if b.display_name == "tagged.mp3"][0].key

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.marked, {key})

            await pilot.press("r")
            await self.wait_for_scan(pilot)
            self.assertEqual(app.marked, {key})

    async def test_a_mark_on_a_vanished_book_is_dropped(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await self.mark(pilot, "tagged.mp3")
            self.tagged.unlink()

            await pilot.press("r")
            await self.wait_for_scan(pilot)

            self.assertEqual(app.marked, set())

    # ------------------------------------------------------ batch writes #

    async def open_batch(self, pilot, key: str = "space"):
        """Press *key* with marks set, and return the dialog it opened."""

        from forge.tui.screens import ConfirmScreen, DispositionScreen

        await pilot.press(key)
        await pilot.pause()
        if key == "space":
            self.assertIsInstance(pilot.app.screen, DispositionScreen)
            self.assertTrue(pilot.app.screen.batch)
            await pilot.press("1")  # downcode
            await pilot.pause()
        self.assertIsInstance(pilot.app.screen, ConfirmScreen)
        return pilot.app.screen

    async def test_a_batch_sets_the_disposition_on_every_marked_book(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            marked = app.marked_books()
            self.assertEqual(len(marked), 3)

            await self.open_batch(pilot)
            await pilot.press("y")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            for book in marked:
                self.assertIs(d.read(book.files[0]), D.DOWNCODE, book.display_name)
                self.assertIs(book.disposition, D.DOWNCODE)

    async def test_a_batch_write_clears_the_marks_it_acted_on(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            await self.open_batch(pilot)
            await pilot.press("y")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # A stale mark is what would make the *next* batch write the wrong
            # thing, so a completed batch leaves nothing marked.
            self.assertEqual(app.marked, set())

    async def test_declining_the_confirmation_writes_nothing(self) -> None:
        before = {p: p.read_bytes() for p in (self.tagged, self.untagged, self.triaged)}

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            await self.open_batch(pilot)
            await pilot.press("escape")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(len(app.marked), 3)  # marks kept, so it can be redone

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path.name)

    async def test_escaping_the_batch_disposition_screen_asks_nothing(self) -> None:
        before = {p: p.read_bytes() for p in (self.tagged, self.untagged, self.triaged)}

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await pilot.press("M")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            # Back to the list with no confirmation in between.
            self.assertEqual(len(pilot.app.screen_stack), 1)
            self.assertEqual(len(pilot.app.marked), 3)

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path.name)

    async def test_a_single_book_still_writes_without_a_confirmation(self) -> None:
        from forge.tui.screens import ConfirmScreen

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = pilot.app.shown[0]

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()

            self.assertNotIsInstance(pilot.app.screen, ConfirmScreen)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIs(d.read(target.files[0]), D.DOWNCODE)

    async def test_one_marked_book_needs_no_confirmation_either(self) -> None:
        from forge.tui.screens import ConfirmScreen, DispositionScreen

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await self.mark(pilot, "tagged.mp3")
            await pilot.press("space")
            await pilot.pause()
            # One marked book is not a batch, so it reads as the normal dialog.
            self.assertIsInstance(app.screen, DispositionScreen)
            self.assertFalse(app.screen.batch)
            await pilot.press("1")
            await pilot.pause()

            self.assertNotIsInstance(app.screen, ConfirmScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIs(d.read(self.tagged), D.DOWNCODE)

    async def test_the_batch_applies_to_marks_the_filter_is_hiding(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await self.mark(pilot, "triaged.mp3")  # on hold, so the next filter hides it
            await self.mark(pilot, "tagged.mp3")
            await pilot.press("f")  # untriaged
            await pilot.pause()
            self.assertNotIn("triaged.mp3", [b.display_name for b in app.shown])
            self.assertEqual(len(app.marked), 2)

            await self.open_batch(pilot)
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()

            # The mark was explicit; a filter must not quietly narrow the batch.
            self.assertIs(d.read(self.triaged), D.DOWNCODE)

    async def test_a_batch_can_clear_dispositions(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app

            await pilot.press("M")
            await pilot.pause()
            await self.open_batch(pilot, key="x")
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertIs(d.read(self.triaged), D.UNTRIAGED)
            self.assertEqual(app.marked, set())

    async def test_a_failed_write_keeps_its_mark_and_is_reported(self) -> None:
        from textual.widgets import Static

        from forge.tui import app as app_mod

        original = app_mod.disposition_mod.write

        def fussy(path, value):
            if path.name == "untagged.mp3":
                raise OSError("disk went away")
            return original(path, value)

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app
            failing = [b for b in app.books if b.display_name == "untagged.mp3"][0]

            await pilot.press("M")
            await pilot.pause()

            app_mod.disposition_mod.write = fussy
            try:
                await self.open_batch(pilot)
                await pilot.press("y")
                await app.workers.wait_for_complete()
                await pilot.pause()
            finally:
                app_mod.disposition_mod.write = original

            # One bad file must not stop the rest of the run.
            self.assertIs(d.read(self.tagged), D.DOWNCODE)
            # ...and the one that failed keeps its mark, so it can be retried.
            self.assertEqual(app.marked, {failing.key})
            rendered = str(app.query_one("#status", Static).renderable)
            self.assertIn("could not be tagged", rendered)

    async def test_pressing_space_with_nothing_marked_uses_the_cursor(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            app = pilot.app
            target = app.shown[1]
            app.query_one("#books").move_cursor(row=1)
            await pilot.pause()

            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(app.screen.targets, [target])

    # ------------------------------------------------------------- sorting #

    async def open_sort(self, pilot):
        from forge.tui.screens import SortScreen

        await pilot.press("s")
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, SortScreen)
        return pilot.app.screen

    @staticmethod
    def order_key(name: str) -> str:
        """The number key that picks the order called *name*."""

        from forge.sorting import SORT_ORDERS

        return str([order.name for order in SORT_ORDERS].index(name))

    async def test_the_list_opens_in_the_default_order(self) -> None:
        from forge.sorting import DEFAULT_ORDER

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            self.assertEqual(pilot.app.sort_name, DEFAULT_ORDER.name)
            self.assertFalse(pilot.app.sort_descending)
            # What needs a decision first: untagged cannot be converted at all.
            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                ["untagged.mp3", "tagged.mp3", "triaged.mp3"],
            )

    async def test_picking_an_order_resorts_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(pilot.app.sort_name, "title")
            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                # "A History of Science", "B", then untagged by file name.
                ["tagged.mp3", "triaged.mp3", "untagged.mp3"],
            )

    async def test_reversing_flips_the_order(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("r")
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(pilot.app.sort_descending)
            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                ["untagged.mp3", "triaged.mp3", "tagged.mp3"],
            )

    async def test_the_direction_can_be_set_before_the_column(self) -> None:
        # The two keystrokes must commute: r then a number, or a number then r.
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press("r")
            await pilot.press(self.order_key("title"))
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                ["untagged.mp3", "triaged.mp3", "tagged.mp3"],
            )

    async def test_cancelling_the_sort_screen_changes_nothing(self) -> None:
        from forge.sorting import DEFAULT_ORDER

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            before = [b.display_name for b in pilot.app.shown]

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("escape")
            await pilot.pause()

            self.assertEqual(pilot.app.sort_name, DEFAULT_ORDER.name)
            self.assertEqual([b.display_name for b in pilot.app.shown], before)

    async def test_the_cursor_stays_on_the_same_book_across_a_resort(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "triaged.mp3"][0]
            pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
            await pilot.pause()

            await self.open_sort(pilot)
            await pilot.press(self.order_key("author"))
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(pilot.app.selected.display_name, "triaged.mp3")

    async def test_the_sort_screen_opens_on_the_current_order(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press(self.order_key("author"))
            await pilot.press("r")
            await pilot.press("enter")
            await pilot.pause()

            screen = await self.open_sort(pilot)
            from forge.sorting import SORT_ORDERS

            self.assertEqual(SORT_ORDERS[screen.chosen].name, "author")
            self.assertTrue(screen.reversed_)
            await pilot.press("escape")
            await pilot.pause()

    async def test_the_chosen_order_survives_a_rescan(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press("r")
            await self.wait_for_scan(pilot)

            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                ["tagged.mp3", "triaged.mp3", "untagged.mp3"],
            )

    async def test_the_chosen_order_holds_through_a_filter_change(self) -> None:
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)

            await self.open_sort(pilot)
            await pilot.press(self.order_key("title"))
            await pilot.press("r")
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press("f")  # the untriaged filter: two of the three books
            await pilot.pause()

            self.assertEqual(
                [b.display_name for b in pilot.app.shown],
                ["untagged.mp3", "tagged.mp3"],
            )

    async def test_sorting_writes_nothing(self) -> None:
        before = {p: p.read_bytes() for p in (self.tagged, self.untagged, self.triaged)}

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            await self.open_sort(pilot)
            await pilot.press(self.order_key("disposition"))
            await pilot.press("enter")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path.name)

    async def test_the_editor_opens_and_cancelling_writes_nothing(self) -> None:
        from forge.tui.screens import MetadataScreen

        before = self.tagged.read_bytes()
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "tagged.mp3"][0]
            pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, MetadataScreen)

            await pilot.press("escape")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        self.assertEqual(self.tagged.read_bytes(), before)

    async def test_the_editor_focuses_its_first_field_and_tab_walks_down(self) -> None:
        from textual.widgets import Input

        from forge.tags import EDITABLE_FIELDS
        from forge.tui.screens import MetadataScreen

        first, second = EDITABLE_FIELDS[0][0], EDITABLE_FIELDS[1][0]
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "tagged.mp3"][0]
            pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            screen = pilot.app.screen
            self.assertIsInstance(screen, MetadataScreen)

            # Keyboard-only editing: the first field is live on arrival, so
            # typed text lands in it without a click.
            self.assertIs(screen.focused, screen.query_one(f"#f-{first}", Input))
            await pilot.press("X")
            await pilot.pause()
            self.assertTrue(screen.query_one(f"#f-{first}", Input).value.endswith("X"))

            await pilot.press("tab")
            await pilot.pause()
            self.assertIs(screen.focused, screen.query_one(f"#f-{second}", Input))

            await pilot.press("escape")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

    async def test_editing_a_field_writes_it_and_records_history(self) -> None:
        from forge.history import History
        from forge.tui.screens import MetadataScreen

        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            target = [b for b in pilot.app.shown if b.display_name == "tagged.mp3"][0]
            pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(target))
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()

            screen = pilot.app.screen
            self.assertIsInstance(screen, MetadataScreen)
            from textual.widgets import Input

            screen.query_one("#f-narrator", Input).value = "A Narrator"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        self.assertEqual(tags.read_metadata(self.tagged).narrator, "A Narrator")

        with History(self.cfg.db_path) as store:
            groups = store.groups_for_file(self.tagged)
            self.assertTrue(groups)
            fields = {c.field for g in groups for c in store.changes_in_group(g.id)}
            self.assertIn("narrator", fields)

    async def test_triage_never_produces_an_output(self) -> None:
        # The whole point of splitting triage from run.
        async with self.app().run_test() as pilot:
            await self.wait_for_scan(pilot)
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("1")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])
        self.assertEqual(list(self.cfg.archive_dir.iterdir()), [])


@requires_ffmpeg
@requires_textual
class FolderBookTriage(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        folder = self.cfg.source_dir / "Odd Thomas"
        self.files = [folder / f"0{i} - Chapter {i}.mp3" for i in (1, 2, 3)]
        for index, path in enumerate(self.files, 1):
            copy_mp3(path, seconds=1)
            tags.write_metadata(
                path, {"author": "Dean Koontz", "album": "Odd Thomas", "track": index}
            )

    async def test_a_disposition_is_written_to_every_track(self) -> None:
        from forge.tui.app import TriageApp

        async with TriageApp(self.cfg, self.cfg.source_dir).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(len(pilot.app.books), 1)
            self.assertTrue(pilot.app.books[0].is_folder)

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("1")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        for path in self.files:
            self.assertIs(d.read(path), D.DOWNCODE)
        self.assertFalse(d.read_folder(self.files).conflict)

    async def test_editing_a_folder_book_records_one_undoable_group(self) -> None:
        # Three tracks, one edit, one group: `forge undo GROUP_ID` has to put
        # the whole thing back, not a third of it.
        from forge.history import History
        from forge.tui.app import TriageApp

        app = TriageApp(self.cfg, self.cfg.source_dir)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            app._write_tags(app.books[0], {"narrator": "David Aaron Baker"}, source="manual")
            await app.workers.wait_for_complete()
            await pilot.pause()

        with History(self.cfg.db_path) as store:
            groups = store.recent_groups()
            self.assertEqual(len(groups), 1, [g.describe() for g in groups])
            self.assertEqual(
                sorted(store.files_in_group(groups[0].id)),
                sorted(str(path) for path in self.files),
            )
            self.assertEqual(sorted(store.restore_values(groups[0].id)),
                             sorted(str(path) for path in self.files))

        for path in self.files:
            self.assertEqual(tags.read_metadata(path).narrator, "David Aaron Baker")


if __name__ == "__main__":
    unittest.main()


@requires_textual
class ScreenConstruction(unittest.TestCase):
    """Every screen must be constructible.

    textual's Widget and DOMNode carry read-only properties - ``region``,
    ``visible``, ``offset`` and friends - and assigning to one of them in
    ``__init__`` raises an AttributeError before any of the screen's own code
    runs.  Two such collisions shipped before this test existed, so the whole
    class of bug is checked here rather than one name at a time.
    """

    def _book(self):
        from pathlib import Path

        from forge.scan import Book
        from forge.tags import BookMetadata

        return Book(
            path=Path("/books/a.m4b"),
            is_folder=False,
            files=[Path("/books/a.m4b")],
            metadata=BookMetadata(path="/books/a.m4b", author="An Author", album="A Book"),
        )

    def test_every_screen_constructs(self) -> None:
        from forge.tags import BookMetadata
        from forge.tui.screens import (
            AudibleScreen,
            CompareScreen,
            ConfirmScreen,
            DispositionScreen,
            MetadataScreen,
            SortScreen,
        )

        book = self._book()
        metadata = BookMetadata(path="/books/a.m4b", author="An Author")

        for label, build in (
            ("DispositionScreen", lambda: DispositionScreen(book)),
            ("DispositionScreen(batch)", lambda: DispositionScreen(book, [book, book])),
            ("MetadataScreen", lambda: MetadataScreen(metadata, name_hint="a.m4b")),
            ("AudibleScreen", lambda: AudibleScreen(book)),
            ("AudibleScreen(region)", lambda: AudibleScreen(book, region="uk")),
            ("CompareScreen", lambda: CompareScreen(metadata, {"author": "X"}, name_hint="a")),
            ("ConfirmScreen", lambda: ConfirmScreen("Sure?", "detail")),
            ("SortScreen", lambda: SortScreen()),
            ("SortScreen(current)", lambda: SortScreen("author", True)),
        ):
            with self.subTest(screen=label):
                build()  # must not raise

    def test_no_screen_attribute_shadows_a_read_only_textual_property(self) -> None:
        import inspect

        from textual.screen import ModalScreen
        from forge.tui import screens as screens_mod

        offenders: list[str] = []
        for name, cls in vars(screens_mod).items():
            if not inspect.isclass(cls) or not issubclass(cls, ModalScreen):
                continue
            if cls.__module__ != screens_mod.__name__:
                continue

            source = inspect.getsource(cls.__init__) if cls.__init__ is not object.__init__ else ""
            assigned = {
                line.split("=")[0].strip().removeprefix("self.")
                for line in source.splitlines()
                if line.strip().startswith("self.") and "=" in line
            }
            for attribute in assigned:
                for klass in type.mro(cls):
                    if attribute not in vars(klass):
                        continue
                    existing = vars(klass)[attribute]
                    if isinstance(existing, property) and existing.fset is None:
                        offenders.append(f"{name}.{attribute} shadows {klass.__name__}.{attribute}")
                    break

        self.assertEqual(offenders, [])


@requires_ffmpeg
@requires_textual
class ModalMessageIsolation(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Messages from a modal's widgets must not confuse the app underneath.

    A ``@on`` handler declared on the App also receives messages bubbling out of
    modal screens.  The Audible screen contains its own DataTable, so an
    unscoped ``RowHighlighted`` handler on the app ran while the book list was
    not mounted and brought the whole interface down.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, seconds=1)
        tags.write_metadata(path, {"author": "An Author", "album": "A Book"})

    def fake_results(self):
        from forge.audible import SearchResult

        return [
            SearchResult(
                asin=f"B00000000{index}",
                title=f"Candidate {index}",
                author="An Author",
                narrator="A Narrator",
                series="",
                runtime_minutes=600 + index * 30,
                product={},
            )
            for index in range(1, 4)
        ]

    async def test_highlighting_rows_in_the_audible_modal_does_not_crash(self) -> None:
        from forge.tui.app import TriageApp
        from forge.tui.screens import AudibleScreen

        app = TriageApp(self.cfg, self.cfg.source_dir)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, AudibleScreen)

            # Populate the modal's table without touching the network.
            screen._show_results(self.fake_results(), 600 * 60)
            await pilot.pause()

            table = screen.query_one("#results")
            table.focus()
            for row in (0, 1, 2, 1, 0):
                table.move_cursor(row=row)
                await pilot.pause()

            # The app survived, and the modal is still the active screen.
            self.assertIsInstance(app.screen, AudibleScreen)
            self.assertEqual(len(screen.results), 3)

    async def test_the_book_list_is_still_usable_afterwards(self) -> None:
        from forge.tui.app import TriageApp

        app = TriageApp(self.cfg, self.cfg.source_dir)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            app.screen._show_results(self.fake_results(), 600 * 60)
            await pilot.pause()
            app.screen.query_one("#results").move_cursor(row=1)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            self.assertIsNotNone(app.selected)
            self.assertEqual(app.selected.display_name, "book.mp3")

    async def test_app_helpers_tolerate_a_missing_book_list(self) -> None:
        # Defence in depth: an app-level handler can run at a moment when the
        # main screen's widgets are not queryable.
        from forge.tui.app import TriageApp

        app = TriageApp(self.cfg, self.cfg.source_dir)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()

            self.assertIsNone(app.selected)
            app._refresh_detail()  # must not raise
            app._status("a message that has nowhere to go")
            app.refresh_table()


@requires_ffmpeg
@requires_textual
class ActivityIndicator(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """What the interface is doing, bottom right.

    Scanning a directory and writing tags to a large container both take long
    enough to look like a hang, and a static label would look the same whether
    the program were working or wedged - so the spinner animating is the part
    that carries the message, and the label says what the work is.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        for name in ("one.mp3", "two.mp3"):
            path = self.cfg.source_dir / name
            copy_mp3(path, seconds=1)
            tags.write_metadata(path, {"author": "An Author", "album": name})

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    def bar(self, app):
        from forge.tui.app import ActivityBar

        return app.query_one(ActivityBar)

    async def settled(self, pilot):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def test_it_says_idle_when_nothing_is_running(self) -> None:
        from forge.tui.app import IDLE_LABEL

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)
            self.assertFalse(bar.busy)
            self.assertEqual(str(bar.renderable), f"[{IDLE_LABEL}]")
            self.assertIn("idle", bar.classes)
            self.assertNotIn("busy", bar.classes)

    async def test_the_scan_announces_itself(self) -> None:
        # A real scan of two short files is over before anything can observe it,
        # so the scan is held open deliberately: the point is that the label is
        # up *while* the work runs, which is exactly the case that looked like a
        # hang.
        import threading
        from unittest import mock

        from forge import scan as scan_mod

        started = threading.Event()
        release = threading.Event()

        def slow_scan(root):
            started.set()
            release.wait(10)
            return scan_mod.scan(root)

        with mock.patch("forge.tui.app.scan", slow_scan):
            async with self.app().run_test() as pilot:
                bar = self.bar(pilot.app)
                for _ in range(200):
                    if started.is_set() and bar.busy:
                        break
                    await pilot.pause(0.01)

                self.assertTrue(bar.busy, "the scan never showed in the indicator")
                self.assertIn("Scanning", bar.label)
                self.assertIn("busy", bar.classes)
                self.assertNotIn("idle", bar.classes)

                release.set()
                await self.settled(pilot)
                self.assertFalse(bar.busy)

    async def test_writing_a_disposition_announces_itself(self) -> None:
        # The label has to be set from the worker thread, which cannot touch a
        # widget directly.
        from forge.disposition import Disposition

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            bar = self.bar(app)
            seen: list[str] = []

            original = bar.start

            def record(label: str, owner=None) -> int:
                seen.append(label)
                return original(label, owner)

            bar.start = record
            app._write_disposition(app.books[0], Disposition.HOLD)
            await self.settled(pilot)

            self.assertIn("Writing disposition", seen)
            self.assertFalse(bar.busy)

    async def test_writing_tags_announces_itself(self) -> None:
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            bar = self.bar(app)
            seen: list[str] = []
            original = bar.start
            bar.start = lambda label, owner=None: (
                seen.append(label), original(label, owner)
            )[1]

            app._write_tags(app.books[0], {"narrator": "A Narrator"}, source="manual")
            await self.settled(pilot)

            self.assertIn("Writing tags", seen)
            self.assertFalse(bar.busy)

    async def test_it_returns_to_idle_after_the_work_finishes(self) -> None:
        from forge.tui.app import IDLE_LABEL

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)
            self.assertEqual(str(bar.renderable), f"[{IDLE_LABEL}]")

    async def test_a_lost_stop_does_not_strand_the_indicator(self) -> None:
        # The reported bug: a "Writing tags" label that stayed up for the rest
        # of the session, even after later writes finished.  The stop travels
        # from a worker thread to the interface thread, so it can be lost; the
        # worker's own state is what gets the indicator back to idle.
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            bar = self.bar(app)

            app._activity_stop = lambda token: None  # the stop never arrives
            app._write_tags(app.books[0], {"narrator": "A Narrator"}, source="manual")
            await self.settled(pilot)

            for _ in range(100):
                if not bar.busy:
                    break
                await pilot.pause(0.02)

            self.assertFalse(bar.busy, f"indicator stuck on {bar.label!r}")
            self.assertIn("idle", bar.classes)

    async def test_a_stranded_label_does_not_mask_a_later_one(self) -> None:
        # The symptom that made the bug visible: the stale label was the one on
        # show, so a later write looked like it was still running.
        from forge.tui.app import IDLE_LABEL

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            bar = self.bar(app)

            app._activity_stop = lambda token: None
            app._write_tags(app.books[0], {"narrator": "First"}, source="manual")
            await self.settled(pilot)
            await pilot.pause(0.2)

            del app._activity_stop  # later writes behave normally
            app._write_tags(app.books[1], {"narrator": "Second"}, source="manual")
            await self.settled(pilot)
            await pilot.pause(0.2)

            self.assertEqual(bar.label, IDLE_LABEL)

    async def test_an_activity_on_the_interface_thread_is_never_pruned(self) -> None:
        # Pruning keys off the owning worker; work with no worker (the quit
        # notice, for one) has to stay up until it is stopped by hand.
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)

            token = bar.start("Finishing writes before quitting")
            await pilot.pause(0.3)

            self.assertTrue(bar.busy)
            self.assertEqual(bar.label, "Finishing writes before quitting")
            bar.stop(token)
            await pilot.pause()
            self.assertFalse(bar.busy)

    async def test_concurrent_activities_each_hold_the_indicator(self) -> None:
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)

            first = bar.start("Scanning directory")
            second = bar.start("Writing tags")
            await pilot.pause()
            self.assertEqual(bar.label, "Writing tags")

            bar.stop(second)
            await pilot.pause()
            self.assertTrue(bar.busy, "the scan was still running")
            self.assertEqual(bar.label, "Scanning directory")

            bar.stop(first)
            await pilot.pause()
            self.assertFalse(bar.busy)

    async def test_stopping_an_unknown_token_is_harmless(self) -> None:
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)
            bar.stop(999)
            await pilot.pause()
            self.assertFalse(bar.busy)

    async def test_the_spinner_animates_only_while_busy(self) -> None:
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)

            idle_frames = {str(bar.renderable)}
            for _ in range(5):
                await pilot.pause(bar.INTERVAL * 1.5)
                idle_frames.add(str(bar.renderable))
            self.assertEqual(len(idle_frames), 1, idle_frames)

            token = bar.start("Scanning directory")
            busy_frames = set()
            for _ in range(6):
                await pilot.pause(bar.INTERVAL * 1.5)
                busy_frames.add(str(bar.renderable))
            bar.stop(token)
            self.assertGreater(len(busy_frames), 1, "the spinner never moved")

    async def test_the_indicator_sits_inside_the_status_bar(self) -> None:
        # Asserting containment, not size: a widget pushed out of its row still
        # reports a plausible size while being entirely off screen.
        async with self.app().run_test(size=(100, 30)) as pilot:
            await self.settled(pilot)
            app = pilot.app
            bar = self.bar(app)
            row = app.query_one("#statusbar")

            self.assertTrue(
                row.content_region.contains_region(bar.region),
                f"activity bar at {bar.region} is outside {row.content_region}",
            )
            self.assertTrue(app.query_one("Footer").region.y > bar.region.y)

    async def test_a_long_label_is_not_clipped(self) -> None:
        async with self.app().run_test(size=(100, 30)) as pilot:
            await self.settled(pilot)
            bar = self.bar(pilot.app)
            token = bar.start("Saving tag snapshot")
            await pilot.pause()
            await pilot.pause()

            self.assertGreaterEqual(
                bar.region.width,
                len(str(bar.renderable)),
                f"{bar.region.width} cells for {str(bar.renderable)!r}",
            )
            bar.stop(token)

    def test_no_widget_member_shadows_a_textual_one(self) -> None:
        # `_render` is a Widget method that must return a Visual.  Defining a
        # same-named method that returned None broke layout for every screen
        # before any of this widget's own code ran, with an error that named
        # neither the widget nor the method.
        import inspect

        from textual.widget import Widget

        from forge.tui.app import ActivityBar

        import ast

        # Only the names actually written in the class body: textual's metaclass
        # injects plenty of its own (_reactives, _decorated_handlers and so on)
        # and inheriting those is the point of subclassing.
        tree = ast.parse(inspect.getsource(ActivityBar)).body[0]
        written = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                written.add(node.name)
            elif isinstance(node, ast.Assign):
                written.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                written.add(node.target.id)

        deliberate = {"__init__", "on_mount", "render", "DEFAULT_CSS"}
        bases = [klass for klass in type.mro(ActivityBar) if klass is not ActivityBar]
        offenders = []
        for name in sorted(written - deliberate):
            for klass in bases:
                if name in vars(klass):
                    offenders.append(f"ActivityBar.{name} shadows {klass.__name__}.{name}")
                    break

        self.assertEqual(offenders, [])
        self.assertIn("start", written, "the test is looking at the wrong class body")
        self.assertTrue(issubclass(ActivityBar, Widget))
        self.assertIsNotNone(inspect.getdoc(ActivityBar))


@requires_ffmpeg
@requires_textual
class AudibleScreenFlow(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """The two-step search flow, and that it says which step it is on.

    The first version ran the search on Enter and then moved focus to the
    results table without comment, which read as nothing having happened - so
    people pressed Enter again, which selected a row and looked like the
    *second* press had done the work.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, seconds=1)
        tags.write_metadata(path, {"author": "An Author", "album": "A Book"})

    def fake_results(self, count=3):
        from forge.audible import SearchResult

        return [
            SearchResult(
                asin=f"B00000000{index}",
                title=f"Candidate {index}",
                author="An Author",
                narrator="A Narrator",
                series="",
                runtime_minutes=600 + index * 30,
                product={},
            )
            for index in range(1, count + 1)
        ]

    async def open_screen(self, pilot, app):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        return app.screen

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    def label(self, screen) -> str:
        from textual.widgets import Button

        return str(screen.query_one("#primary", Button).label)

    def step(self, screen) -> str:
        from textual.widgets import Static

        return str(screen.query_one("#step", Static).renderable)

    async def test_it_opens_on_step_one(self) -> None:
        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            self.assertIn("Step 1 of 2", self.step(screen))
            self.assertEqual(self.label(screen), "Search")

    async def test_the_button_names_the_asin_precedence(self) -> None:
        # A filled ASIN field is searched directly and the title is ignored.
        # That used to be invisible; now the button says so.
        from textual.widgets import Input

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            self.assertEqual(self.label(screen), "Search")

            screen.query_one("#asin", Input).value = "B000000001"
            await pilot.pause()
            self.assertEqual(self.label(screen), "Look up ASIN")

            screen.query_one("#asin", Input).value = ""
            await pilot.pause()
            self.assertEqual(self.label(screen), "Search")

    async def test_results_move_it_to_step_two(self) -> None:
        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            screen._show_results(self.fake_results(), 600 * 60)
            await pilot.pause()

            self.assertIn("Step 2 of 2", self.step(screen))
            self.assertEqual(self.label(screen), "Use this match")
            self.assertEqual(screen.stage, screen.STAGE_CHOOSE)
            self.assertEqual(screen.query_one("#results").row_count, 3)

    async def test_an_empty_result_set_returns_to_step_one(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            screen._show_results([], 600 * 60)
            await pilot.pause()

            self.assertIn("Step 1 of 2", self.step(screen))
            self.assertEqual(self.label(screen), "Search")
            self.assertIn("no results", str(screen.query_one("#status", Static).renderable))

    async def test_the_status_says_what_to_do_next(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            screen._show_results(self.fake_results(), 600 * 60)
            await pilot.pause()

            status = str(screen.query_one("#status", Static).renderable)
            self.assertIn("3 result(s)", status)
            self.assertIn("Use this match", status)

    async def test_the_runtime_note_reflects_the_best_match(self) -> None:
        from textual.widgets import Static

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)

            # Top candidate is 630 minutes; a 630-minute file is a close match.
            screen._show_results(self.fake_results(), 630 * 60)
            await pilot.pause()
            self.assertIn("matches this file closely",
                          str(screen.query_one("#status", Static).renderable))

            # Nothing close to a 5-hour file.
            screen._show_results(self.fake_results(), 300 * 60)
            await pilot.pause()
            self.assertIn("none of these is a close match",
                          str(screen.query_one("#status", Static).renderable))

    async def test_the_primary_button_follows_the_stage(self) -> None:
        picked: list[str] = []

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            screen._lookup_asin = lambda asin: picked.append(asin)

            # Step 1: the button searches.
            searched: list[bool] = []
            screen._search = lambda title, author: searched.append(True)
            screen._primary()
            await pilot.pause()
            self.assertEqual(searched, [True])
            self.assertEqual(picked, [])

            # Step 2: the same button uses the highlighted match instead.
            screen._show_results(self.fake_results(), 600 * 60)
            await pilot.pause()
            screen.query_one("#results").move_cursor(row=1)
            await pilot.pause()
            screen._primary()
            await pilot.pause()
            self.assertEqual(picked, ["B000000002"])

    async def test_a_blank_query_is_reported_rather_than_searched(self) -> None:
        from textual.widgets import Input, Static

        async with self.app().run_test() as pilot:
            screen = await self.open_screen(pilot, pilot.app)
            searched: list[bool] = []
            screen._search = lambda title, author: searched.append(True)

            screen.query_one("#title-input", Input).value = ""
            screen.query_one("#asin", Input).value = ""
            await pilot.pause()
            screen.action_search()
            await pilot.pause()

            self.assertEqual(searched, [])
            self.assertIn("enter a title", str(screen.query_one("#status", Static).renderable))

    async def test_cancel_dismisses_without_a_lookup(self) -> None:
        from forge.tui.screens import AudibleScreen

        async with self.app().run_test() as pilot:
            app = pilot.app
            await self.open_screen(pilot, app)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, AudibleScreen)


@requires_ffmpeg
@requires_textual
class ScreenLayout(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Key widgets must sit inside the dialog that owns them.

    textual's ``Horizontal`` and ``Vertical`` default to ``height: 1fr``, so a
    row of inputs without an explicit height claims an equal share of its
    container.  That pushed the Audible screen's results table and buttons off
    the bottom of the dialog while every functional test still passed.

    Note what is asserted: not the widget's *size*, which stays plausible when
    this happens, but whether its region is actually contained by the dialog's
    content area.  A widget laid out at y=36 inside a box that ends at y=27 is
    simply not on screen.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, seconds=1)
        tags.write_metadata(path, {"author": "An Author", "album": "A Book"})

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    def assert_visible(self, screen, container_id: str, widget_ids: list[str]) -> None:
        box = screen.query_one(container_id)
        for widget_id in widget_ids:
            widget = screen.query_one(widget_id)
            with self.subTest(widget=widget_id):
                self.assertGreater(widget.size.height, 0, f"{widget_id} has no height")
                self.assertTrue(
                    box.content_region.contains_region(widget.region),
                    f"{widget_id} at {widget.region} is outside "
                    f"{container_id} content area {box.content_region}",
                )

    async def open_audible(self, pilot, app, size_note=""):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause(0.2)
        return app.screen

    async def test_the_audible_screen_fits_its_dialog(self) -> None:
        async with self.app().run_test(size=(120, 32)) as pilot:
            screen = await self.open_audible(pilot, pilot.app)
            self.assert_visible(screen, "#box", ["#query", "#results", "#buttons", "#status"])

    async def test_it_still_fits_once_results_arrive(self) -> None:
        from forge.audible import SearchResult

        async with self.app().run_test(size=(120, 32)) as pilot:
            screen = await self.open_audible(pilot, pilot.app)
            screen._show_results(
                [
                    SearchResult(
                        asin=f"B00000000{i}", title=f"Candidate {i}", author="A",
                        narrator="N", series="", runtime_minutes=600 + i, product={},
                    )
                    for i in range(1, 6)
                ],
                600 * 60,
            )
            await pilot.pause(0.2)
            self.assert_visible(screen, "#box", ["#query", "#results", "#buttons", "#status"])

    async def test_the_query_rows_do_not_claim_the_whole_dialog(self) -> None:
        # Three bordered inputs need nine rows. Anything far beyond that means
        # the 1fr default has taken over again.
        async with self.app().run_test(size=(120, 32)) as pilot:
            screen = await self.open_audible(pilot, pilot.app)
            self.assertLessEqual(screen.query_one("#query").size.height, 12)

    def batch_book(self):
        from pathlib import Path

        from forge.scan import Book
        from forge.tags import BookMetadata

        path = Path("/books/a.m4b")
        return Book(
            path=path,
            is_folder=False,
            files=[path],
            metadata=BookMetadata(path=str(path), author="An Author", album="A Book"),
        )

    async def test_the_other_dialogs_fit_too(self) -> None:
        from forge.tags import BookMetadata
        from forge.tui.screens import (
            CompareScreen,
            ConfirmScreen,
            DispositionScreen,
            MetadataScreen,
            SortScreen,
        )

        metadata = BookMetadata(path="/books/a.m4b", author="An Author")

        async with self.app().run_test(size=(120, 32)) as pilot:
            app = pilot.app
            await app.workers.wait_for_complete()
            await pilot.pause()

            for label, build, ids in (
                ("MetadataScreen", lambda: MetadataScreen(metadata, name_hint="a"),
                 ["#fields", "#buttons"]),
                ("CompareScreen", lambda: CompareScreen(metadata, {"author": "X"}, name_hint="a"),
                 ["#choices", "#buttons"]),
                ("ConfirmScreen", lambda: ConfirmScreen("Sure?"), ["#buttons"]),
                ("SortScreen", lambda: SortScreen(), ["#direction", "#choice-0", "#hint"]),
                ("DispositionScreen(batch)",
                 lambda: DispositionScreen(self.batch_book(), [self.batch_book()] * 40),
                 ["#title", "#suggestion", "#hint"]),
            ):
                app.push_screen(build())
                await pilot.pause(0.2)
                with self.subTest(screen=label):
                    self.assert_visible(app.screen, "#box", ids)
                app.pop_screen()
                await pilot.pause(0.1)


@requires_ffmpeg
@requires_textual
class WorkerIsolation(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Background work must not cancel unrelated background work.

    textual's ``exclusive`` cancels every worker sharing a node *and* group.
    All of this app's workers run on the App node, so while they shared the
    default group, starting a rescan cancelled an in-flight tag write - which on
    a folder book could tag some tracks and leave the rest, producing exactly
    the conflicted state the disposition design exists to avoid.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, seconds=1)
        tags.write_metadata(path, {"author": "An Author", "album": "A Book"})

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    def test_every_worker_has_its_own_group(self) -> None:
        import inspect

        from forge.tui.app import TriageApp

        source = inspect.getsource(TriageApp)
        decorators = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("@work(")
        ]
        self.assertTrue(decorators, "no workers found; has the app been restructured?")

        groups = []
        for decorator in decorators:
            self.assertIn("group=", decorator, f"{decorator} has no explicit group")
            groups.append(decorator.split('group="')[1].split('"')[0])

        self.assertEqual(
            len(groups), len(set(groups)), f"two workers share a group: {groups}"
        )

    def test_a_rescan_does_not_cancel_a_tag_write(self) -> None:
        # The behavioural counterpart to the check above.
        import time

        from forge.tui import app as app_mod

        original = app_mod.tags_mod.write_metadata
        started = []

        def slow(path, updates, **kwargs):
            started.append(path)
            time.sleep(0.6)
            return original(path, updates, **kwargs)

        async def scenario() -> str | None:
            async with self.app().run_test() as pilot:
                app = pilot.app
                await app.workers.wait_for_complete()
                await pilot.pause()

                book = app.shown[0]
                app_mod.tags_mod.write_metadata = slow
                try:
                    app._write_tags(book, {"narrator": "Probe"}, source="manual")
                    await pilot.pause(0.15)
                    app.load_books()          # the rescan that used to cancel it
                    for _ in range(60):
                        await pilot.pause(0.1)
                        if all(
                            worker.state.name in ("SUCCESS", "ERROR", "CANCELLED")
                            for worker in app.workers._workers
                        ):
                            break
                finally:
                    app_mod.tags_mod.write_metadata = original

                return tags.read_metadata(book.files[0]).narrator

        narrator = asyncio_run(scenario())
        self.assertTrue(started, "the slow write never ran; the test needs adjusting")
        self.assertEqual(narrator, "Probe")


def asyncio_run(coro):
    """Run *coro* on a fresh event loop, for use inside a sync test."""

    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


@requires_ffmpeg
@requires_textual
class NewAuthorIndicator(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """An author forge has never identified is flagged in the list.

    The spelling in the tag is about to become a folder in the library and an
    author in Audiobookshelf, and triage is the last point at which fixing it
    is cheap - so a name forge cannot place is coloured rather than silently
    accepted.
    """

    CHANEY = "B00W2ZAK7E"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))

        self.known = self.cfg.source_dir / "known.mp3"
        copy_mp3(self.known, seconds=1)
        tags.write_metadata(self.known, {"author": "J.N. Chaney", "album": "Known Book"})

        self.newcomer = self.cfg.source_dir / "newcomer.mp3"
        copy_mp3(self.newcomer, seconds=1)
        tags.write_metadata(
            self.newcomer, {"author": "A Total Newcomer", "album": "New Book"}
        )

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    def learn(self, name: str = "J.N. Chaney", asin: str | None = None) -> None:
        from forge.authors import AuthorRegistry, Contributor

        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            registry.learn([Contributor(asin or self.CHANEY, name)])

    def cell(self, app, display_name: str, column: int = 4) -> str:
        """One rendered cell, by book name.  Column 4 is the author."""

        row = [b.display_name for b in app.shown].index(display_name)
        return str(app.query_one("#books").get_row_at(row)[column])

    async def settled(self, pilot):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def test_an_unknown_author_is_coloured(self) -> None:
        from forge.tui.app import TriageApp

        self.learn()
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            flagged = self.cell(pilot.app, "newcomer.mp3")
            self.assertIn(TriageApp.NEW_AUTHOR_COLOUR, flagged)
            self.assertIn("A Total Newcomer", flagged)

    async def test_a_known_author_is_not(self) -> None:
        from forge.tui.app import TriageApp

        self.learn()
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(pilot.app, "known.mp3")
            )

    async def test_a_spelling_variant_counts_as_known(self) -> None:
        from forge.tui.app import TriageApp

        # The whole point of the registry: "JN Chaney" is the same person.
        self.learn(name="JN Chaney")
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(pilot.app, "known.mp3")
            )

    async def test_role_credits_are_stripped_before_judging(self) -> None:
        from forge.tui.app import TriageApp

        self.learn()
        credited = self.cfg.source_dir / "credited.mp3"
        copy_mp3(credited, seconds=1)
        tags.write_metadata(
            credited,
            {"author": "J.N. Chaney, Someone - translator", "album": "Credited"},
        )

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(pilot.app, "credited.mp3")
            )

    async def test_the_detail_panel_says_what_the_colour_means(self) -> None:
        from textual.widgets import Static

        self.learn()
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            row = [b.display_name for b in app.shown].index("newcomer.mp3")
            app.query_one("#books").move_cursor(row=row)
            await pilot.pause()

            rendered = str(app.query_one("#detail", Static).renderable)
            self.assertIn("new author", rendered)
            self.assertIn("A Total Newcomer", rendered)

    async def test_the_panel_says_when_shelving_will_rewrite_the_name(self) -> None:
        from textual.widgets import Static

        self.learn(name="J. N. Chaney")  # canonical differs from the tag
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            row = [b.display_name for b in app.shown].index("known.mp3")
            app.query_one("#books").move_cursor(row=row)
            await pilot.pause()

            self.assertIn(
                "shelves as J. N. Chaney",
                str(app.query_one("#detail", Static).renderable),
            )

    async def test_learning_an_identity_clears_the_flag_on_refresh(self) -> None:
        from forge.tui.app import TriageApp

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            self.assertIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3")
            )

            self.learn(name="A Total Newcomer", asin="B000000001")
            app.refresh_table()
            await pilot.pause()

            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3")
            )

    async def test_accepting_an_audible_match_identifies_the_author(self) -> None:
        # The flag has to clear as soon as the lookup settles the question.
        # Waiting for a shelve run hours later means a restart still shows the
        # author as new, which is exactly how this looks like a bug.
        from forge.audible import AudibleMetadata
        from forge.authors import AuthorRegistry, Contributor
        from forge.tui.app import TriageApp

        metadata = AudibleMetadata(
            asin="B0DKPH5RT9",
            title="New Book",
            author="A Total Newcomer",
            contributors=(Contributor("B0000NEW01", "A Total Newcomer"),),
        )

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            self.assertIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3")
            )

            app._learn_authors(metadata)
            app.refresh_table()
            await pilot.pause()

            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3")
            )

        # ...and it is on disk, so a restart agrees.
        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            self.assertEqual(registry.identity("a total newcomer"), "B0000NEW01")

    async def test_the_flag_is_still_clear_after_a_restart(self) -> None:
        from forge.audible import AudibleMetadata
        from forge.authors import Contributor
        from forge.tui.app import TriageApp

        metadata = AudibleMetadata(
            asin="B0DKPH5RT9",
            author="A Total Newcomer",
            contributors=(Contributor("B0000NEW01", "A Total Newcomer"),),
        )
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            pilot.app._learn_authors(metadata)

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(pilot.app, "newcomer.mp3")
            )

    async def test_a_role_credited_contributor_is_not_learned(self) -> None:
        from forge.audible import AudibleMetadata
        from forge.authors import AuthorRegistry, Contributor

        metadata = AudibleMetadata(
            author="A Total Newcomer",
            contributors=(
                Contributor("B0000NEW01", "A Total Newcomer"),
                Contributor("B0000NEW02", "Some Person - translator"),
            ),
        )
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            pilot.app._learn_authors(metadata)

        with AuthorRegistry(self.cfg.authors_db_path) as registry:
            self.assertEqual(
                [name for _, name, _ in registry.identities()], ["A Total Newcomer"]
            )

    async def test_a_match_with_no_contributor_asins_learns_nothing(self) -> None:
        from forge.audible import AudibleMetadata

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            self.assertEqual(
                app._learn_authors(AudibleMetadata(author="A Total Newcomer")), 0
            )

    async def test_learning_without_a_registry_is_harmless(self) -> None:
        from forge.audible import AudibleMetadata
        from forge.authors import Contributor

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app._authors = None
            self.assertEqual(
                app._learn_authors(
                    AudibleMetadata(contributors=(Contributor("B0000NEW01", "X"),))
                ),
                0,
            )

    async def test_the_whole_lookup_path_clears_the_flag(self) -> None:
        # The reported sequence, driven through the real screens: `a`, pick the
        # match, apply the tags, then reopen the app.
        from forge.audible import AudibleMetadata
        from forge.authors import Contributor
        from forge.tui.app import TriageApp
        from forge.tui.screens import AudibleScreen, CompareScreen

        metadata = AudibleMetadata(
            asin="B0DKPH5RT9",
            title="New Book",
            author="A Total Newcomer",
            narrator="A Narrator",
            contributors=(Contributor("B0000NEW01", "A Total Newcomer"),),
        )

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            row = [b.display_name for b in app.shown].index("newcomer.mp3")
            app.query_one("#books").move_cursor(row=row)
            await pilot.pause()
            self.assertIn(TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3"))

            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, AudibleScreen)

            # What the second stage does once it has the full product.
            app.screen.dismiss(metadata)
            await pilot.pause()
            self.assertIsInstance(app.screen, CompareScreen)

            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, "newcomer.mp3")
            )

        # Restarted: the identity came from the database, not from memory.
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            self.assertNotIn(
                TriageApp.NEW_AUTHOR_COLOUR, self.cell(pilot.app, "newcomer.mp3")
            )

    async def test_the_filter_shows_only_unidentified_authors(self) -> None:
        self.learn()
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            while app.filter_name() != "new-author":
                await pilot.press("f")
                await pilot.pause()

            self.assertEqual(
                [b.display_name for b in app.shown], ["newcomer.mp3"]
            )

    async def test_an_unopenable_registry_flags_nothing(self) -> None:
        from forge.tui.app import TriageApp

        # Advisory colour, never a dependency: with no registry the list is
        # exactly what it was before any of this existed.
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app._authors = None
            app.refresh_table()
            await pilot.pause()

            for name in ("known.mp3", "newcomer.mp3"):
                self.assertNotIn(
                    TriageApp.NEW_AUTHOR_COLOUR, self.cell(app, name), name
                )


@requires_ffmpeg
@requires_textual
class Quitting(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """Quitting must not abandon a file operation.

    A thread cannot be interrupted, so ``exit()`` during a tag write tore the
    interface down and left the process waiting silently for the write to
    finish - and a ctrl+c in that window printed a traceback on top of a write
    that was still running.  The work has to happen either way, so the
    interface stays up and says what it is waiting for.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        self.path = self.cfg.source_dir / "book.mp3"
        copy_mp3(self.path, seconds=1)
        tags.write_metadata(self.path, {"author": "An Author", "album": "A Book"})

    def app(self):
        from forge.tui.app import TriageApp

        return TriageApp(self.cfg, self.cfg.source_dir)

    async def settled(self, pilot):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def wait_for_exit(self, pilot, timeout: float = 10.0) -> bool:
        for _ in range(int(timeout / 0.05)):
            if not pilot.app.is_running:
                return True
            await pilot.pause(0.05)
        return not pilot.app.is_running

    async def test_quitting_with_nothing_running_exits_at_once(self) -> None:
        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(await self.wait_for_exit(pilot, 2.0))

    async def test_quitting_mid_write_waits_instead_of_exiting(self) -> None:
        import threading

        from textual.widgets import Static

        from forge.tui import app as app_mod

        original = app_mod.tags_mod.write_metadata
        in_write = threading.Event()
        release = threading.Event()

        def slow(path, updates, **kwargs):
            in_write.set()
            release.wait(10)
            return original(path, updates, **kwargs)

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app_mod.tags_mod.write_metadata = slow
            try:
                app._write_tags(app.books[0], {"narrator": "Patient"}, source="manual")
                for _ in range(200):
                    if in_write.is_set():
                        break
                    await pilot.pause(0.01)
                self.assertTrue(in_write.is_set(), "the write never started")

                await pilot.press("q")
                await pilot.pause(0.3)

                # Still up, and saying why.
                self.assertTrue(app.is_running)
                rendered = str(app.query_one("#status", Static).renderable)
                self.assertIn("file write", rendered)
                self.assertIn("Finishing writes before quitting", app.query_one(
                    app_mod.ActivityBar
                ).label)
            finally:
                release.set()
                app_mod.tags_mod.write_metadata = original

            # ...and it leaves of its own accord once the write is done.
            self.assertTrue(await self.wait_for_exit(pilot))

        self.assertEqual(tags.read_metadata(self.path).narrator, "Patient")

    async def test_the_same_holds_for_a_disposition_write(self) -> None:
        import threading

        from forge.disposition import Disposition
        from forge.tui import app as app_mod

        original = app_mod.disposition_mod.write
        in_write = threading.Event()
        release = threading.Event()

        def slow(path, value):
            in_write.set()
            release.wait(10)
            return original(path, value)

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app_mod.disposition_mod.write = slow
            try:
                app._write_disposition(app.books[0], Disposition.DOWNCODE)
                for _ in range(200):
                    if in_write.is_set():
                        break
                    await pilot.pause(0.01)

                await pilot.press("q")
                await pilot.pause(0.2)
                self.assertTrue(app.is_running)
            finally:
                release.set()
                app_mod.disposition_mod.write = original

            self.assertTrue(await self.wait_for_exit(pilot))

        self.assertIs(d.read(self.path), Disposition.DOWNCODE)

    async def test_quitting_during_a_scan_waits_too(self) -> None:
        # Read-only work cannot corrupt anything, but the process would sit
        # there just the same, so the wait is shown rather than hidden.
        import threading
        from unittest import mock

        from textual.widgets import Static

        from forge import scan as scan_mod

        started = threading.Event()
        release = threading.Event()

        def slow_scan(root):
            started.set()
            release.wait(10)
            return scan_mod.scan(root)

        with mock.patch("forge.tui.app.scan", slow_scan):
            async with self.app().run_test() as pilot:
                app = pilot.app
                for _ in range(200):
                    if started.is_set():
                        break
                    await pilot.pause(0.01)

                await pilot.press("q")
                await pilot.pause(0.2)
                self.assertTrue(app.is_running)
                rendered = str(app.query_one("#status", Static).renderable)
                self.assertIn("background task", rendered)

                release.set()
                self.assertTrue(await self.wait_for_exit(pilot))

    async def test_keys_are_ignored_once_quitting_has_started(self) -> None:
        import threading

        from forge.tui import app as app_mod

        original = app_mod.tags_mod.write_metadata
        in_write = threading.Event()
        release = threading.Event()

        def slow(path, updates, **kwargs):
            in_write.set()
            release.wait(10)
            return original(path, updates, **kwargs)

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app_mod.tags_mod.write_metadata = slow
            try:
                app._write_tags(app.books[0], {"narrator": "Patient"}, source="manual")
                for _ in range(200):
                    if in_write.is_set():
                        break
                    await pilot.pause(0.01)

                await pilot.press("q")
                await pilot.pause(0.2)

                # A stray key after `q` must not start work that extends the
                # wait, or open a dialog over a shutting-down interface.
                await pilot.press("r")
                await pilot.press("space")
                await pilot.press("e")
                await pilot.pause(0.2)

                self.assertEqual(len(app.screen_stack), 1)
                self.assertFalse(
                    any(
                        worker.group == "scan" and not worker.is_finished
                        for worker in app.workers
                    ),
                    "a rescan started while quitting",
                )
            finally:
                release.set()
                app_mod.tags_mod.write_metadata = original

            self.assertTrue(await self.wait_for_exit(pilot))

    async def test_pressing_q_again_while_waiting_is_harmless(self) -> None:
        import threading

        from forge.tui import app as app_mod

        original = app_mod.tags_mod.write_metadata
        in_write = threading.Event()
        release = threading.Event()

        def slow(path, updates, **kwargs):
            in_write.set()
            release.wait(10)
            return original(path, updates, **kwargs)

        async with self.app().run_test() as pilot:
            await self.settled(pilot)
            app = pilot.app
            app_mod.tags_mod.write_metadata = slow
            try:
                app._write_tags(app.books[0], {"narrator": "Patient"}, source="manual")
                for _ in range(200):
                    if in_write.is_set():
                        break
                    await pilot.pause(0.01)

                for _ in range(4):
                    await pilot.press("q")
                    await pilot.pause(0.05)
                self.assertTrue(app.is_running)
            finally:
                release.set()
                app_mod.tags_mod.write_metadata = original

            self.assertTrue(await self.wait_for_exit(pilot))

        # The impatient second press must not have cost the write.
        self.assertEqual(tags.read_metadata(self.path).narrator, "Patient")


@requires_ffmpeg
@requires_textual
class DuplicateChecking(TempCaseMixin, unittest.IsolatedAsyncioTestCase):
    """`d` checks one book against Audiobookshelf; `D` checks them all.

    Audiobookshelf is faked throughout: the point of these tests is what the
    interface does with the answer, and a test that needs a live server is a
    test nobody runs.
    """

    SHELVED = "B0CMYXVB65"
    UNSHELVED = "B000000000"

    def setUp(self) -> None:
        super().setUp()
        path = write_config(self.root)
        path.write_text(
            path.read_text()
            + "\n[audiobookshelf]\nurl = http://abs.invalid\napi_key = secret\n"
        )
        self.cfg = config_mod.load(path)

        self.shelved = self.cfg.source_dir / "shelved.mp3"
        copy_mp3(self.shelved, seconds=1)
        tags.write_metadata(
            self.shelved, {"author": "Adrian Tchaikovsky", "album": "Ironclads", "asin": self.SHELVED}
        )

        self.fresh = self.cfg.source_dir / "fresh.mp3"
        copy_mp3(self.fresh, seconds=1)
        tags.write_metadata(self.fresh, {"author": "A", "album": "B", "asin": self.UNSHELVED})

        self.no_asin = self.cfg.source_dir / "no-asin.mp3"
        copy_mp3(self.no_asin, seconds=1)
        tags.write_metadata(self.no_asin, {"author": "C", "album": "D"})

    def app(self, *, fake_library: bool = True):
        from forge.tui.app import TriageApp
        from tests.test_library import FakeSession, client

        app = TriageApp(self.cfg, self.cfg.source_dir)
        if fake_library:
            self.session = FakeSession()
            app._library = client(self.session)
        return app

    async def ready(self, pilot) -> None:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def select(self, pilot, name: str):
        book = [b for b in pilot.app.shown if b.display_name == name][0]
        pilot.app.query_one("#books").move_cursor(row=pilot.app.shown.index(book))
        await pilot.pause()
        return book

    async def press_and_settle(self, pilot, key: str) -> None:
        await pilot.press(key)
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def test_d_finds_a_book_that_is_already_in_the_library(self) -> None:
        from forge import library
        from forge.tui.screens import DuplicateScreen

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            book = await self.select(pilot, "shelved.mp3")
            await self.press_and_settle(pilot, "d")

            check = pilot.app.duplicates[book.key]
            self.assertEqual(check.status, library.DUPLICATE)
            self.assertEqual(check.matches[0].title, "Ironclads")
            # A match is worth interrupting for, rather than one dim line.
            self.assertIsInstance(pilot.app.screen, DuplicateScreen)

    async def test_d_on_a_book_the_library_does_not_have(self) -> None:
        from forge import library
        from forge.tui.screens import DuplicateScreen

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            book = await self.select(pilot, "fresh.mp3")
            await self.press_and_settle(pilot, "d")

            self.assertEqual(pilot.app.duplicates[book.key].status, library.UNIQUE)
            self.assertNotIsInstance(pilot.app.screen, DuplicateScreen)

    async def test_a_book_without_an_asin_is_not_sent_to_the_library(self) -> None:
        from forge import library

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            book = await self.select(pilot, "no-asin.mp3")
            await self.press_and_settle(pilot, "d")

            self.assertEqual(pilot.app.duplicates[book.key].status, library.NO_ASIN)
            self.assertEqual(self.session.calls, [], "the library was queried anyway")

    async def test_shift_d_checks_every_book_with_an_asin(self) -> None:
        from forge import library

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "D")

            by_name = {b.display_name: pilot.app.duplicates.get(b.key) for b in pilot.app.books}
            self.assertEqual(by_name["shelved.mp3"].status, library.DUPLICATE)
            self.assertEqual(by_name["fresh.mp3"].status, library.UNIQUE)
            # Never asked about, so no answer was invented for it.
            self.assertIsNone(by_name["no-asin.mp3"])

    async def test_the_result_shows_in_the_list(self) -> None:
        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "D")

            table = pilot.app.query_one("#books")
            column = [column.label.plain for column in table.columns.values()].index("dup")
            cells = {
                book.display_name: str(table.get_row(book.key)[column])
                for book in pilot.app.shown
            }
            self.assertIn("dup", cells["shelved.mp3"])
            self.assertIn("new", cells["fresh.mp3"])

    async def test_a_duplicate_filter_shows_only_the_matches(self) -> None:
        from forge.tui.app import _FILTERS

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "D")

            pilot.app.filter_index = [name for name, _ in _FILTERS].index("duplicate")
            pilot.app.refresh_table()
            await pilot.pause()
            self.assertEqual([b.display_name for b in pilot.app.shown], ["shelved.mp3"])

    async def test_the_check_changes_nothing_on_disk(self) -> None:
        before = {p: p.read_bytes() for p in (self.shelved, self.fresh, self.no_asin)}
        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "D")
            await self.select(pilot, "shelved.mp3")
            await self.press_and_settle(pilot, "d")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, f"{path.name} was modified")

    async def test_an_unreachable_library_says_so_and_keeps_going(self) -> None:
        import requests

        from forge import library

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            self.session.get = mock.Mock(side_effect=requests.ConnectionError("no route"))
            book = await self.select(pilot, "shelved.mp3")
            await self.press_and_settle(pilot, "d")

            self.assertEqual(pilot.app.duplicates[book.key].status, library.ERROR)
            self.assertTrue(pilot.app.is_running)

    async def test_without_credentials_the_key_explains_itself(self) -> None:
        from textual.widgets import Static

        self.cfg = config_mod.load(write_config(self.root / "bare"))
        self.shelved = self.cfg.source_dir / "shelved.mp3"
        copy_mp3(self.shelved, seconds=1)
        tags.write_metadata(self.shelved, {"author": "A", "album": "B", "asin": self.SHELVED})

        async with self.app(fake_library=False).run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "d")

            rendered = str(pilot.app.query_one("#status", Static).renderable)
            self.assertIn("Audiobookshelf is not configured", rendered)
            self.assertEqual(pilot.app.duplicates, {})

    async def test_the_library_is_indexed_once_for_the_session(self) -> None:
        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.press_and_settle(pilot, "D")
            await self.select(pilot, "fresh.mp3")
            await self.press_and_settle(pilot, "d")

            fetches = [url for url in self.session.calls if "/items" in url]
            self.assertEqual(len(fetches), 1, self.session.calls)

    async def test_a_series_with_brackets_in_it_does_not_break_the_display(self) -> None:
        # Audiobookshelf returns series like "Octaeteris Cycle #3", which is
        # valid console markup: unescaped, it took the whole interface down
        # while rendering the detail panel.
        from forge.tui.screens import DuplicateScreen
        from tests.test_library import FakeSession, client, item

        session = FakeSession(
            items={"results": [item(self.SHELVED, "A Book", "An Author", series="Cycle #3")]}
        )
        app = self.app(fake_library=False)
        app._library = client(session)

        async with app.run_test() as pilot:
            await self.ready(pilot)
            await self.select(pilot, "shelved.mp3")
            await self.press_and_settle(pilot, "d")

            self.assertTrue(app.is_running)
            self.assertIsInstance(app.screen, DuplicateScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_a_file_name_with_brackets_in_it_does_not_break_the_list(self) -> None:
        bracketed = self.cfg.source_dir / "[2026] Bracketed.mp3"
        copy_mp3(bracketed, seconds=1)
        tags.write_metadata(bracketed, {"author": "A [B]", "album": "C [D]"})

        async with self.app().run_test() as pilot:
            await self.ready(pilot)
            await self.select(pilot, "[2026] Bracketed.mp3")
            self.assertTrue(pilot.app.is_running)

    async def test_the_duplicate_dialog_fits_inside_its_box(self) -> None:
        # A container with no explicit height claims 1fr and pushes its
        # siblings off the dialog, while still reporting a plausible size -
        # hence containment, not size, is what is asserted.
        async with self.app().run_test(size=(120, 30)) as pilot:
            await self.ready(pilot)
            await self.select(pilot, "shelved.mp3")
            await self.press_and_settle(pilot, "d")

            box = pilot.app.screen.query_one("#box")
            for selector in ("#title", "#matches", "#hint", "#close"):
                widget = pilot.app.screen.query_one(selector)
                self.assertTrue(
                    box.content_region.contains_region(widget.region),
                    f"{selector} is outside the dialog: {widget.region} vs {box.content_region}",
                )
