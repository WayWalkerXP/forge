"""The author review interface.

The backfill chooses each author's canonical spelling by how often the library
uses it, which is the best rule available to a machine and still wrong often
enough to need a person: it picked "AC Cobble" over "A. C. Cobble" and dropped
the accent from "China Miéville".  That name decides both the tag written into
every one of the author's books and the shelf they are filed on, so this is
where it gets reviewed.

Three properties hold, and the tests hold them:

*   **Nothing is written until a save.**  Edits live in a dictionary on the app
    and reach SQLite only when the user says so, with a confirmation.  Quitting
    with unsaved work asks what to do with it.
*   **Only the registry is touched.**  No tags, no files, no change log - this
    module deliberately imports none of them.  Reviewing a name is not an
    instruction to go and rewrite a library.
*   **Nothing destructive is on offer.**  No identity can be deleted, no key
    edited, no name left blank.  The riskiest thing here is a spelling.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, Static

from .. import authors as authors_mod
from ..config import Config
from .author_screens import AuthorSortScreen, EditNameScreen, UnsavedChangesScreen
from .markup import literal as escape
from .screens import ConfirmScreen

LOGGER = logging.getLogger(__name__)

STATUS_LABEL = "Last action:"

#: name, description, and the key each row is ordered by.
SORT_ORDERS: tuple[tuple[str, str, Callable[[authors_mod.ReviewRow], Any]], ...] = (
    ("books", "most books first", lambda row: (-row.books, row.canonical.casefold())),
    ("name", "the spelling it is written as", lambda row: row.canonical.casefold()),
    (
        "spellings",
        "authors Amazon records several ways first",
        lambda row: (-len(row.spellings), -row.asins, row.canonical.casefold()),
    ),
    ("ids", "most Audible contributor ids first", lambda row: (-row.asins, row.canonical.casefold())),
    ("pinned", "names chosen by hand first", lambda row: (not row.pinned, row.canonical.casefold())),
    ("updated", "most recently changed first", lambda row: (row.updated_at, row.canonical.casefold())),
)

FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "every author"),
    ("respelled", "recorded more than one way"),
    ("pinned", "names chosen by hand"),
    ("unpinned", "names chosen by the frequency vote"),
    ("no-asin", "authors Amazon has no contributor id for"),
    ("edited", "unsaved changes"),
    ("unidentified", "spellings with no identity at all (read only)"),
)


class AuthorReviewApp(App[int]):
    """Review and correct the canonical spelling of every known author."""

    TITLE = "forge authors"

    CSS = """
    #body { height: 1fr; }
    #authors { height: 1fr; }
    #detail {
        height: auto; max-height: 8;
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
        # `enter` is handled through DataTable.RowSelected instead: a focused
        # table consumes the key as a selection, so an app-level binding for it
        # would simply never fire.
        Binding("e", "edit", "edit name"),
        Binding("p", "toggle_pin", "pin"),
        Binding("u", "undo", "undo row"),
        Binding("slash", "search", "search"),
        Binding("f", "cycle_filter", "filter"),
        Binding("o", "sort", "sort"),
        Binding("s", "save", "save"),
        Binding("escape", "clear_search", "", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.registry = authors_mod.AuthorRegistry(config.authors_db_path)
        #: Edits not yet written, by identity.  The whole safety model is that
        #: this is the only place a change exists until a save.
        self.pending: dict[str, str] = {}
        #: Pins toggled but not yet written, by identity, as the new value.
        self.pinning: dict[str, bool] = {}
        self.rows: list[authors_mod.ReviewRow] = []
        self.shown: list[authors_mod.ReviewRow] = []
        self.unidentified: list[tuple[str, int]] = []
        self.filter_index = 0
        self.search_text = ""
        self.sort_name = SORT_ORDERS[0][0]
        self.sort_descending = False

    # ------------------------------------------------------------- layout #

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Input(placeholder="search name, spelling or identity", id="search")
            table = DataTable(id="authors", cursor_type="row", zebra_stripes=True)
            table.add_columns("", "pin", "written as", "books", "spellings", "ids", "identity")
            yield table
            yield Static("", id="detail")
        with Horizontal(id="statusbar"):
            yield Static("", id="status")
            yield Static("", id="pending")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#authors", DataTable).focus()
        self.load()

    def on_unmount(self) -> None:
        self.registry.close()

    # --------------------------------------------------------------- data #

    def load(self) -> None:
        self.rows = self.registry.review_rows()
        self.unidentified = self.registry.unknown_names()
        counted = sum(row.books for row in self.rows)
        self.sub_title = (
            f"{len(self.rows)} authors · {counted} books counted at the last backfill"
        )
        self.refresh_table()

    def canonical_of(self, row: authors_mod.ReviewRow) -> str:
        """The name this author would be saved as, pending edits included."""

        return self.pending.get(row.identity, row.canonical)

    def pinned_of(self, row: authors_mod.ReviewRow) -> bool:
        if row.identity in self.pending:
            return True  # saving a name pins it
        return self.pinning.get(row.identity, row.pinned)

    def _matches(self, row: authors_mod.ReviewRow) -> bool:
        name = FILTERS[self.filter_index][0]
        if name == "respelled" and not row.respelled:
            return False
        if name == "pinned" and not self.pinned_of(row):
            return False
        if name == "unpinned" and self.pinned_of(row):
            return False
        if name == "no-asin" and row.has_asin:
            return False
        if name == "edited" and row.identity not in self.pending and row.identity not in self.pinning:
            return False

        if not self.search_text:
            return True
        needle = authors_mod.name_key(self.search_text)
        plain = self.search_text.casefold()
        haystack = [self.canonical_of(row), *row.spellings]
        return (
            any(needle and needle in authors_mod.name_key(value) for value in haystack)
            or plain in row.identity.casefold()
        )

    def refresh_table(self) -> None:
        try:
            table = self.query_one("#authors", DataTable)
        except NoMatches:
            return
        previous = table.cursor_row
        table.clear()

        if self.filter_name() == "unidentified":
            self.shown = []
            for spelling, seen in self.unidentified:
                table.add_row(
                    "", "", escape(spelling), "", f"seen {seen}×", "",
                    "[dim]no identity[/dim]",
                )
        else:
            self.shown = self._sorted(
                [row for row in self.rows if self._matches(row)]
            )
            for row in self.shown:
                table.add_row(*self._row(row), key=row.identity)

        count = len(self.shown) if self.shown else len(self.unidentified)
        if count:
            table.move_cursor(row=min(previous, count - 1))
        self._refresh_detail()
        self._refresh_pending()

    def _sorted(self, rows: list[authors_mod.ReviewRow]) -> list[authors_mod.ReviewRow]:
        key = next(
            (key for name, _, key in SORT_ORDERS if name == self.sort_name),
            SORT_ORDERS[0][2],
        )
        return sorted(rows, key=key, reverse=self.sort_descending)

    def _row(self, row: authors_mod.ReviewRow) -> tuple[str, ...]:
        edited = row.identity in self.pending or row.identity in self.pinning
        name = self.canonical_of(row)
        spellings = ", ".join(row.spellings[:3])
        if len(row.spellings) > 3:
            spellings += f" +{len(row.spellings) - 3}"
        return (
            "[yellow]•[/yellow]" if edited else "",
            "[cyan]*[/cyan]" if self.pinned_of(row) else "[dim]·[/dim]",
            f"[yellow]{escape(name)}[/yellow]" if edited else escape(name),
            str(row.books) if row.books else "[dim]—[/dim]",
            escape(spellings)[:40] if spellings else "[dim]—[/dim]",
            str(row.asins) if row.asins else "[dim]—[/dim]",
            f"[dim]{escape(row.identity)}[/dim]",
        )

    @property
    def selected(self) -> authors_mod.ReviewRow | None:
        try:
            table = self.query_one("#authors", DataTable)
        except NoMatches:
            return None
        if not self.shown or not 0 <= table.cursor_row < len(self.shown):
            return None
        return self.shown[table.cursor_row]

    @on(DataTable.RowHighlighted, "#authors")
    def _highlighted(self) -> None:
        self._refresh_detail()

    @on(DataTable.RowSelected, "#authors")
    def _selected_row(self) -> None:
        """`enter` on the table opens the editor."""

        self.action_edit()

    def _refresh_detail(self) -> None:
        try:
            panel = self.query_one("#detail", Static)
        except NoMatches:
            return
        row = self.selected
        if row is None:
            if self.filter_name() == "unidentified":
                panel.update(
                    "[dim]spellings with no identity: shown so they are visible, "
                    "not editable here.  `forge authors --link` settles one.[/dim]"
                )
            else:
                panel.update("[dim]no author selected[/dim]")
            return

        lines = [f"[bold]{escape(self.canonical_of(row))}[/bold]"]
        if row.identity in self.pending:
            lines.append(
                f"  [yellow]unsaved: was {escape(row.canonical)}[/yellow]"
            )
        lines.append(f"  [dim]identity {escape(row.identity)}[/dim]")
        if row.spellings:
            lines.append(f"  seen as: {escape(', '.join(row.spellings))}")
        if not row.has_asin:
            lines.append(
                "  [dim]Audible has no contributor id for this author, so the "
                "identity comes from the name[/dim]"
            )
        if self.pinned_of(row):
            lines.append("  [cyan]* chosen by hand; the frequency vote will not change it[/cyan]")
        panel.update("\n".join(lines))

    def _refresh_pending(self) -> None:
        try:
            box = self.query_one("#pending", Static)
        except NoMatches:
            return
        total = len(self.pending) + len(self.pinning)
        box.set_class(bool(total), "dirty")
        box.set_class(not total, "clean")
        # Content, not a markup string: the square brackets are part of the
        # label, and "[1 unsaved]" parsed as markup raises MarkupError - the
        # same way "[Idle]" did in the triage activity bar.
        box.update(Content(f"[{total} unsaved]" if total else "[saved]"))

    def _status(self, message: str) -> None:
        try:
            self.query_one("#status", Static).update(
                f"[bold]{STATUS_LABEL}[/bold] {message}" if message else ""
            )
        except NoMatches:
            LOGGER.debug("dropped a status message: %s", message)

    # ------------------------------------------------------------ actions #

    def filter_name(self) -> str:
        return FILTERS[self.filter_index][0]

    def action_edit(self) -> None:
        row = self.selected
        if row is None:
            self._status("[yellow]no author selected[/yellow]")
            return

        def apply(name: str | None) -> None:
            if name is None:
                return
            if name == row.canonical:
                self.pending.pop(row.identity, None)
            else:
                self.pending[row.identity] = name
            self.refresh_table()
            self._status(
                f"{escape(row.canonical)} → {escape(name)} "
                f"[dim](unsaved; s saves)[/dim]"
            )

        self.push_screen(
            EditNameScreen(row.identity, self.canonical_of(row), row.spellings), apply
        )

    def action_toggle_pin(self) -> None:
        row = self.selected
        if row is None:
            return
        if row.identity in self.pending:
            self._status(
                "[yellow]this name is already being changed, which pins it[/yellow]"
            )
            return
        now = not self.pinned_of(row)
        if now == row.pinned:
            self.pinning.pop(row.identity, None)
        else:
            self.pinning[row.identity] = now
        self.refresh_table()
        self._status(
            f"{escape(row.canonical)}: {'pinned' if now else 'unpinned'} "
            f"[dim](unsaved; s saves)[/dim]"
        )

    def action_undo(self) -> None:
        row = self.selected
        if row is None:
            return
        if (
            self.pending.pop(row.identity, None) is None
            and self.pinning.pop(row.identity, None) is None
        ):
            self._status("nothing to undo on this row")
            return
        self.refresh_table()
        self._status(f"{escape(row.canonical)}: change dropped")

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
        self.query_one("#authors", DataTable).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search_text = event.value.strip()
        self.refresh_table()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#authors", DataTable).focus()

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        name, description = FILTERS[self.filter_index]
        self.refresh_table()
        count = len(self.unidentified) if name == "unidentified" else len(self.shown)
        self._status(f"filter: {name} — {description} ({count} shown)")

    def action_sort(self) -> None:
        def apply(result: tuple[str, bool] | None) -> None:
            if result is None:
                return
            self.sort_name, self.sort_descending = result
            keeping = self.selected
            self.refresh_table()
            if keeping is not None:
                self._move_to(keeping.identity)
            description = next(
                (text for name, text, _ in SORT_ORDERS if name == self.sort_name), ""
            )
            direction = "descending" if self.sort_descending else "ascending"
            self._status(f"sort: {self.sort_name} {direction} — {description}")

        self.push_screen(
            AuthorSortScreen(
                [(name, text) for name, text, _ in SORT_ORDERS],
                self.sort_name,
                self.sort_descending,
            ),
            apply,
        )

    def _move_to(self, identity: str) -> None:
        for index, row in enumerate(self.shown):
            if row.identity == identity:
                self.query_one("#authors", DataTable).move_cursor(row=index)
                self._refresh_detail()
                return

    # -------------------------------------------------------------- saving #

    def action_save(self) -> None:
        if not self.pending and not self.pinning:
            self._status("nothing to save")
            return

        names = [
            f"{row.canonical!r} → {self.pending[row.identity]!r}"
            for row in self.rows
            if row.identity in self.pending
        ]
        detail = "; ".join(escape(name) for name in names[:4])
        if len(names) > 4:
            detail += f"; and {len(names) - 4} more"
        if self.pinning:
            detail += f" · {len(self.pinning)} pin change(s)"

        def go(confirmed: bool) -> None:
            if confirmed:
                self._write()
            else:
                self._status("nothing written; the changes are still here")

        question = (
            f"Save {len(self.pending)} name change(s)"
            f"{f' and {len(self.pinning)} pin change(s)' if self.pinning else ''}?"
        )
        self.push_screen(ConfirmScreen(question, detail), go)

    def _write(self) -> None:
        """Apply the pending edits.  The only method here that writes."""

        try:
            saved = self.registry.rename_many(self.pending)
            for identity, pinned in self.pinning.items():
                if pinned:
                    self.registry.rename(identity, self.registry.canonical_of(identity) or "")
                else:
                    self.registry.unpin(identity)
        except Exception as exc:
            LOGGER.exception("could not save author names")
            self._status(f"[red]nothing was saved: {escape(str(exc))}[/red]")
            return

        pins = len(self.pinning)
        self.pending.clear()
        self.pinning.clear()
        self.load()
        self._status(
            f"saved {saved} name(s)" + (f" and {pins} pin change(s)" if pins else "")
        )

    # ------------------------------------------------------------ quitting #

    def action_quit(self) -> None:
        if not self.pending and not self.pinning:
            self.exit(0)
            return

        def decide(answer: str | None) -> None:
            if answer is None:
                self._status("still here; nothing saved")
                return
            if answer == UnsavedChangesScreen.DISCARD:
                self.pending.clear()
                self.pinning.clear()
                self.exit(0)
                return
            self._write()
            if not self.pending and not self.pinning:
                self.exit(0)

        self.push_screen(
            UnsavedChangesScreen(len(self.pending) + len(self.pinning)), decide
        )


def run_author_review(config: Config) -> int:
    """Launch the author review interface.  Returns a process exit code."""

    import sys

    if not sys.stdout.isatty():
        print(
            "forge authors needs an interactive terminal for review.\n"
            "  for the text report, use: forge authors --report",
            file=sys.stderr,
        )
        return 2

    AuthorReviewApp(config).run()
    return 0
