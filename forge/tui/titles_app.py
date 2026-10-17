"""The title-phrase editor.

The list of phrases to take out of book titles, edited the way the author
names are: nothing is written until a save, and quitting with unsaved work asks
what to do with it.

Unlike an author identity, a phrase *can* be deleted - a phrase is a decision
about text, not a record of anything, and the worst a wrong deletion costs is
that the next book keeps its marketing.  So delete is on offer, behind a
confirmation like every other write here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, Static

from ..config import Config
from ..titles import PhraseStore, phrase_key, strip_phrases
from .author_screens import UnsavedChangesScreen
from .markup import literal as escape
from .screens import ConfirmScreen
from .title_screens import EditPhraseScreen, TestTitleScreen

LOGGER = logging.getLogger(__name__)

STATUS_LABEL = "Last action:"

SORT_ORDERS: tuple[tuple[str, str, Callable[[tuple[str, str]], Any]], ...] = (
    ("length", "longest first, the order they are removed in",
     lambda row: (-len(row[0]), row[0].casefold())),
    ("phrase", "alphabetically", lambda row: row[0].casefold()),
    ("added", "most recently added first", lambda row: (row[1], row[0].casefold())),
)


class PhraseReviewApp(App[int]):
    """Add, change and remove the phrases stripped from book titles."""

    TITLE = "forge titles"

    CSS = """
    #body { height: 1fr; }
    #phrases { height: 1fr; }
    #detail {
        height: auto; max-height: 6;
        border-top: solid $accent; padding: 0 1;
    }
    #search { display: none; }
    #search.visible { display: block; }
    #statusbar { height: 1; }
    #status { width: 1fr; padding: 0 1; color: $text-muted; }
    #pending { width: auto; padding: 0 1; }
    #pending.dirty { color: $warning; text-style: bold; }
    #pending.clean { color: $text-muted; }
    """

    BINDINGS = [
        Binding("a", "add", "add"),
        Binding("e", "edit", "edit"),
        Binding("d", "delete", "delete"),
        Binding("u", "undo", "undo row"),
        Binding("t", "try_title", "try a title"),
        Binding("slash", "search", "search"),
        Binding("o", "sort", "sort"),
        Binding("s", "save", "save"),
        Binding("escape", "clear_search", "", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.store = PhraseStore(config.titles_db_path)
        #: The list as it would be after saving.  Nothing reaches the database
        #: until then, so this is the only place an edit exists.
        self.working: list[tuple[str, str]] = []
        self.stored: list[tuple[str, str]] = []
        self.shown: list[tuple[str, str]] = []
        self.search_text = ""
        self.sort_name = SORT_ORDERS[0][0]
        self.sort_descending = False

    # ------------------------------------------------------------- layout #

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Input(placeholder="search phrases", id="search")
            table = DataTable(id="phrases", cursor_type="row", zebra_stripes=True)
            table.add_columns("", "phrase", "characters", "added")
            yield table
            yield Static("", id="detail")
        with Horizontal(id="statusbar"):
            yield Static("", id="status")
            yield Static("", id="pending")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#phrases", DataTable).focus()
        self.load()

    def on_unmount(self) -> None:
        self.store.close()

    # --------------------------------------------------------------- data #

    def load(self) -> None:
        self.stored = self.store.rows()
        self.working = list(self.stored)
        self.sub_title = f"{len(self.stored)} phrase(s) removed from titles when shelving"
        self.refresh_table()

    @property
    def added(self) -> list[str]:
        known = {phrase_key(phrase) for phrase, _ in self.stored}
        return [phrase for phrase, _ in self.working if phrase_key(phrase) not in known]

    @property
    def removed(self) -> list[str]:
        kept = {phrase_key(phrase) for phrase, _ in self.working}
        return [phrase for phrase, _ in self.stored if phrase_key(phrase) not in kept]

    @property
    def dirty(self) -> int:
        return len(self.added) + len(self.removed)

    def _matches(self, row: tuple[str, str]) -> bool:
        if not self.search_text:
            return True
        return self.search_text.casefold() in row[0].casefold()

    def refresh_table(self) -> None:
        try:
            table = self.query_one("#phrases", DataTable)
        except NoMatches:
            return
        previous = table.cursor_row
        table.clear()

        key = next(
            (key for name, _, key in SORT_ORDERS if name == self.sort_name),
            SORT_ORDERS[0][2],
        )
        self.shown = sorted(
            (row for row in self.working if self._matches(row)),
            key=key,
            reverse=self.sort_descending,
        )
        new_keys = {phrase_key(phrase) for phrase in self.added}
        for phrase, added_at in self.shown:
            fresh = phrase_key(phrase) in new_keys
            table.add_row(
                "[yellow]+[/yellow]" if fresh else "",
                f"[yellow]{escape(phrase)}[/yellow]" if fresh else escape(phrase),
                str(len(phrase)),
                "[dim]unsaved[/dim]" if fresh else f"[dim]{added_at[:10]}[/dim]",
            )

        if self.shown:
            table.move_cursor(row=min(previous, len(self.shown) - 1))
        self._refresh_detail()
        self._refresh_pending()

    @property
    def selected(self) -> tuple[str, str] | None:
        try:
            table = self.query_one("#phrases", DataTable)
        except NoMatches:
            return None
        if not self.shown or not 0 <= table.cursor_row < len(self.shown):
            return None
        return self.shown[table.cursor_row]

    @on(DataTable.RowHighlighted, "#phrases")
    def _highlighted(self) -> None:
        self._refresh_detail()

    @on(DataTable.RowSelected, "#phrases")
    def _selected_row(self) -> None:
        self.action_edit()

    def _refresh_detail(self) -> None:
        try:
            panel = self.query_one("#detail", Static)
        except NoMatches:
            return
        row = self.selected
        if row is None:
            if self.removed:
                panel.update(
                    f"[yellow]{len(self.removed)} phrase(s) marked for removal; "
                    f"s saves[/yellow]"
                )
            else:
                panel.update(
                    "[dim]no phrases — a adds one.  Each is removed from the title "
                    "and album tags when a book is shelved.[/dim]"
                )
            return

        phrase = row[0]
        sample = EditPhraseScreen.SAMPLE
        cleaned = strip_phrases(sample, [phrase])
        lines = [f"[bold]{escape(phrase)}[/bold]"]
        if cleaned != sample:
            lines.append(f"  [dim]{escape(sample)}[/dim] → [bold]{escape(str(cleaned))}[/bold]")
        else:
            lines.append("  [dim]t tries it against a title of your own[/dim]")
        if self.removed:
            lines.append(
                f"  [yellow]{len(self.removed)} phrase(s) marked for removal[/yellow]"
            )
        panel.update("\n".join(lines))

    def _refresh_pending(self) -> None:
        try:
            box = self.query_one("#pending", Static)
        except NoMatches:
            return
        total = self.dirty
        box.set_class(bool(total), "dirty")
        box.set_class(not total, "clean")
        # Content, not markup: the brackets are the label.
        box.update(Content(f"[{total} unsaved]" if total else "[saved]"))

    def _status(self, message: str) -> None:
        try:
            self.query_one("#status", Static).update(
                f"[bold]{STATUS_LABEL}[/bold] {message}" if message else ""
            )
        except NoMatches:
            LOGGER.debug("dropped a status message: %s", message)

    # ------------------------------------------------------------ actions #

    def action_add(self) -> None:
        def apply(phrase: str | None) -> None:
            if phrase is None:
                return
            if any(phrase_key(phrase) == phrase_key(known) for known, _ in self.working):
                self._status(f"{escape(phrase)} is already on the list")
                return
            self.working.append((phrase, ""))
            self.refresh_table()
            self._status(f"added {escape(phrase)} [dim](unsaved; s saves)[/dim]")

        self.push_screen(EditPhraseScreen(), apply)

    def action_edit(self) -> None:
        row = self.selected
        if row is None:
            self._status("[yellow]no phrase selected[/yellow]")
            return

        def apply(phrase: str | None) -> None:
            if phrase is None:
                return
            self.working = [entry for entry in self.working if entry != row]
            self.working.append((phrase, ""))
            self.refresh_table()
            self._status(
                f"{escape(row[0])} → {escape(phrase)} [dim](unsaved; s saves)[/dim]"
            )

        self.push_screen(EditPhraseScreen(row[0]), apply)

    def action_delete(self) -> None:
        """Remove a phrase.  Allowed, unlike an author identity.

        A phrase is a decision about text rather than a record of anything, so
        the worst a wrong deletion costs is that the next book keeps its
        marketing - and it is not written until a save in any case.
        """

        row = self.selected
        if row is None:
            self._status("[yellow]no phrase selected[/yellow]")
            return
        self.working = [entry for entry in self.working if entry != row]
        self.refresh_table()
        self._status(
            f"removed {escape(row[0])} [dim](unsaved; s saves, u puts it back)[/dim]"
        )

    def action_undo(self) -> None:
        """Put back everything unsaved: the one undo that cannot be got wrong."""

        if not self.dirty:
            self._status("nothing to undo")
            return
        count = self.dirty
        self.working = list(self.stored)
        self.refresh_table()
        self._status(f"dropped {count} unsaved change(s)")

    def action_try_title(self) -> None:
        self.push_screen(TestTitleScreen([phrase for phrase, _ in self.working]))

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.add_class("visible")
        box.focus()

    def action_clear_search(self) -> None:
        box = self.query_one("#search", Input)
        box.value = ""
        box.remove_class("visible")
        self.search_text = ""
        self.refresh_table()
        self.query_one("#phrases", DataTable).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search_text = event.value.strip()
        self.refresh_table()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#phrases", DataTable).focus()

    def action_sort(self) -> None:
        from .author_screens import AuthorSortScreen

        def apply(result: tuple[str, bool] | None) -> None:
            if result is None:
                return
            self.sort_name, self.sort_descending = result
            self.refresh_table()
            direction = "descending" if self.sort_descending else "ascending"
            self._status(f"sort: {self.sort_name} {direction}")

        self.push_screen(
            AuthorSortScreen(
                [(name, text) for name, text, _ in SORT_ORDERS],
                self.sort_name,
                self.sort_descending,
            ),
            apply,
        )

    # -------------------------------------------------------------- saving #

    def action_save(self) -> None:
        if not self.dirty:
            self._status("nothing to save")
            return

        parts = []
        if self.added:
            parts.append("add " + "; ".join(escape(p) for p in self.added[:3]))
        if self.removed:
            parts.append("remove " + "; ".join(escape(p) for p in self.removed[:3]))

        def go(confirmed: bool) -> None:
            if confirmed:
                self._write()
            else:
                self._status("nothing written; the changes are still here")

        self.push_screen(
            ConfirmScreen(
                f"Save {len(self.added)} addition(s) and {len(self.removed)} removal(s)?",
                " · ".join(parts),
            ),
            go,
        )

    def _write(self) -> None:
        """Apply the pending changes.  The only method here that writes."""

        try:
            added, removed = self.store.apply(add=self.added, remove=self.removed)
        except Exception as exc:
            LOGGER.exception("could not save the title phrases")
            self._status(f"[red]nothing was saved: {escape(str(exc))}[/red]")
            return
        self.load()
        self._status(f"saved: {added} added, {removed} removed")

    # ------------------------------------------------------------ quitting #

    def action_quit(self) -> None:
        if not self.dirty:
            self.exit(0)
            return

        def decide(answer: str | None) -> None:
            if answer is None:
                self._status("still here; nothing saved")
                return
            if answer == UnsavedChangesScreen.DISCARD:
                self.working = list(self.stored)
                self.exit(0)
                return
            self._write()
            if not self.dirty:
                self.exit(0)

        self.push_screen(UnsavedChangesScreen(self.dirty), decide)


def run_phrase_review(config: Config) -> int:
    """Launch the title-phrase editor.  Returns a process exit code."""

    import sys

    if not sys.stdout.isatty():
        print(
            "forge titles needs an interactive terminal to edit.\n"
            "  to list the phrases, use: forge titles --report",
            file=sys.stderr,
        )
        return 2

    PhraseReviewApp(config).run()
    return 0
