"""The triage interface.

Triage reads, edits and tags.  It never converts: that is ``forge run``'s job,
and keeping the two apart is what lets the same executor run unattended from
cron with no interface attached.

The list shows **disposition** and **readiness** as separate columns, because the
dangerous state is a disposition set on a book that cannot be converted.
Readiness is computed on every refresh rather than stored, so it can never go
stale against the file on disk.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, Static
from textual.worker import NoActiveWorker, Worker, WorkerState, get_current_worker

from .. import authors as authors_mod
from .. import disposition as disposition_mod
from .. import library as library_mod
from .. import tags as tags_mod
from ..config import Config
from ..disposition import Disposition
from ..errors import ForgeError
from ..history import History
from ..scan import Book, scan
from ..shelve import clean_author, is_role_credit
from ..sorting import DEFAULT_ORDER, order_by_name, sort_books
from .markup import literal as escape
from .screens import (
    AudibleScreen,
    CompareScreen,
    ConfirmScreen,
    DispositionScreen,
    DuplicateScreen,
    MetadataScreen,
    SortScreen,
    TrackChoice,
    TrackScreen,
)

LOGGER = logging.getLogger(__name__)

_DISPOSITION_COLOUR = {
    Disposition.UNTRIAGED: "dim",
    Disposition.DOWNCODE: "cyan",
    Disposition.DRAMA: "magenta",
    Disposition.REMUX: "blue",
    Disposition.AS_IS: "green",
    Disposition.HOLD: "dim",
    Disposition.DELETE: "red",
    Disposition.NEEDS_ATTENTION: "yellow",
    Disposition.DONE: "dim",
}

_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "every book"),
    ("untriaged", "no disposition yet"),
    ("attention", "needs attention or conflicted"),
    ("blocked", "not ready to convert"),
    ("ready", "tagged and ready"),
    ("duplicate", "already in the Audiobookshelf library"),
    ("new-author", "an author forge has not identified before"),
)

_DUPLICATE_COLOUR = {
    library_mod.DUPLICATE: "red",
    library_mod.UNIQUE: "green",
    library_mod.NO_ASIN: "dim",
    library_mod.ERROR: "yellow",
}


#: Prefix on the bottom line, so it is readable as the result of the last
#: action rather than as part of the book detail panel above it.
STATUS_LABEL = "Last action:"

#: What the activity indicator says when nothing is running.
IDLE_LABEL = "Idle"

#: Shown when a duplicate check is asked for without Audiobookshelf configured.
_NO_LIBRARY = (
    "Audiobookshelf is not configured: add url and api_key under "
    "[audiobookshelf], or AUDIOBOOKSHELF_URL and AUDIOBOOKSHELF_API_KEY to a "
    ".env file beside the config"
)


class ActivityBar(Static):
    """What the interface is doing right now, in the bottom right corner.

    Scanning a large directory and writing tags to a big container both take
    long enough to look like a hang.  A label alone would not be enough - a
    frozen interface would show the same label - so the spinner animates while
    anything is running, which is the part that says the program is alive.

    Several things can run at once (a tag write while a rescan is going), so
    activities are tracked by token and the most recent one is shown.  The
    timer is paused while idle rather than repainting a static line eight times
    a second.

    Each activity remembers the worker that started it, and a repaint drops any
    whose worker has finished.  Pairing a start with a stop is not enough on its
    own: the stop travels from a worker thread to this thread, and anything that
    loses it - a shutting-down app, a dropped callback - would leave the
    indicator claiming forever that something is still running.  The worker's
    own state is the authority, so a lost stop now costs one frame rather than
    the rest of the session.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INTERVAL = 1 / 8

    def __init__(self) -> None:
        super().__init__("", id="activity")
        self._labels: dict[int, str] = {}
        #: The worker each activity belongs to, or None for work running on
        #: this thread, which cannot outlive its own context manager.
        self._owners: dict[int, Any] = {}
        self._token = 0
        self._frame = 0
        self._spinner = None

    def on_mount(self) -> None:
        self._spinner = self.set_interval(self.INTERVAL, self._advance, pause=True)
        self._repaint()

    def start(self, label: str, owner: Any = None) -> int:
        """Begin showing *label*; returns the token that stops it again."""

        self._token += 1
        self._labels[self._token] = label
        self._owners[self._token] = owner
        if self._spinner is not None:
            self._spinner.resume()
        self._repaint()
        return self._token

    def stop(self, token: int) -> None:
        self._labels.pop(token, None)
        self._owners.pop(token, None)
        self._idle_check()
        self._repaint()

    def _prune(self) -> None:
        """Drop activities whose worker has finished without stopping them."""

        stale = [
            token
            for token, owner in self._owners.items()
            if owner is not None and owner.is_finished
        ]
        for token in stale:
            LOGGER.debug(
                "dropping activity %s (%s): its worker has finished",
                token,
                self._labels.get(token),
            )
            self._labels.pop(token, None)
            self._owners.pop(token, None)
        if stale:
            self._idle_check()

    def _idle_check(self) -> None:
        if not self._labels and self._spinner is not None:
            self._spinner.pause()

    @property
    def label(self) -> str:
        """The activity being shown, or the idle label.  For tests."""

        return next(reversed(self._labels.values()), IDLE_LABEL)

    @property
    def busy(self) -> bool:
        return bool(self._labels)

    def _advance(self) -> None:
        self._frame += 1
        self._repaint()

    # Not `_render`: that is a Widget method which must return a Visual, and
    # shadowing it with something that returns None breaks layout before any
    # of this widget's own code runs.
    def _repaint(self) -> None:
        self._prune()
        self.set_class(self.busy, "busy")
        self.set_class(not self.busy, "idle")
        # Content rather than a markup string: the square brackets are part of
        # the label, and markup would try to read "[Idle]" as a style.
        if not self.busy:
            self.update(Content(f"[{IDLE_LABEL}]"))
            return
        frame = self.FRAMES[self._frame % len(self.FRAMES)]
        self.update(Content(f"[{frame} {self.label}]"))


class BookTable(DataTable):
    """The book list, with one addition: it keeps its cursor on screen.

    textual scrolls the cursor into view whenever the *cursor* moves, but not
    when the viewport shrinks underneath it - which happens when the terminal
    is resized, and when the detail panel below grows by a line because the
    newly selected book has a blocking reason or a new author.  The highlighted
    row then sat below the bottom edge, under the horizontal scrollbar: still
    selected, still what the next keystroke would act on, and invisible.

    Neither name here exists on ``DataTable``; both were checked against the
    MRO, because a method that shadows one of textual's breaks layout with an
    error that names neither the widget nor the method.
    """

    def on_resize(self, event: events.Resize) -> None:
        # After the refresh, not in this handler: the height being measured is
        # only the new one once the layout has run.
        self.call_after_refresh(self.keep_cursor_visible)

    def keep_cursor_visible(self, offset: int | None = None) -> None:
        """Scroll to *offset* - or stay put - without losing the cursor row.

        Preferring the offset the list already had is what keeps the view
        still after a rebuild; clamping it around the cursor is what keeps the
        selected row on screen.
        """

        row = self.cursor_row
        if not self.row_count or not 0 <= row < self.row_count:
            return

        # How many *rows* are on screen, which is not the height of the
        # scrollable region: the header sits inside it and the horizontal
        # scrollbar is drawn over its last line.  Counting those made the
        # bottom row look visible while it was behind the scrollbar - the one
        # place a book could be highlighted, acted on, and never seen.
        height = self.scrollable_content_region.height - self.fixed_rows
        if self.show_header:
            height -= self.header_height
        height = max(1, height)

        wanted = self.scroll_offset.y if offset is None else offset
        # The offsets that leave the cursor visible run from "cursor on the
        # last line" to "cursor on the first".
        lowest = max(0, row - height + 1)
        target = max(0, min(min(max(wanted, lowest), row), self.max_scroll_y))
        if target != self.scroll_offset.y:
            self.scroll_to(y=target, animate=False)


class TriageApp(App[int]):
    """A list of books, and the means to decide what happens to each."""

    TITLE = "forge triage"

    CSS = """
    #body { height: 1fr; }
    #books { height: 1fr; }
    #detail {
        height: auto; max-height: 12;
        border-top: solid $accent; padding: 0 1;
    }
    #search { display: none; }
    #search.visible { display: block; }
    /* An explicit height: Horizontal defaults to 1fr, which would claim a
       whole share of the screen and push the footer off the bottom. */
    #statusbar { height: 1; }
    #status { width: 1fr; padding: 0 1; color: $text-muted; }
    #activity { width: auto; padding: 0 1; }
    #activity.idle { color: $text-muted; }
    #activity.busy { color: $accent; text-style: bold; }
    """

    BINDINGS = [
        Binding("space", "set_disposition", "disposition"),
        Binding("m", "toggle_mark", "mark"),
        Binding("M", "mark_shown", "mark all shown", show=False),
        Binding("U", "unmark_all", "unmark all", show=False),
        Binding("e", "edit", "edit tags"),
        Binding("a", "audible", "audible"),
        Binding("slash", "search", "search"),
        Binding("f", "cycle_filter", "filter"),
        Binding("s", "sort", "sort"),
        Binding("r", "rescan", "rescan"),
        Binding("d", "check_duplicate", "dup check (D=all)"),
        Binding("D", "check_all_duplicates", "dup check all", show=False),
        # Was `d` until the duplicate check took that key.  Clearing is also
        # choice 0 on the disposition screen, which is where it is discoverable.
        Binding("x", "clear_disposition", "clear", show=False),
        Binding("escape", "clear_search", "", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, config: Config, root: Path) -> None:
        super().__init__()
        self.config = config
        self.root = root
        self.books: list[Book] = []
        self.shown: list[Book] = []
        self.filter_index = 0
        self.search_text = ""
        #: The chosen ordering, by name, and its direction.  Held as a name so
        #: the sort screen and the list cannot drift apart.
        self.sort_name = DEFAULT_ORDER.name
        self.sort_descending = False
        self.sub_title = str(root)
        #: The author identity registry, opened read-only for the indicator.
        #: None when it could not be opened: an advisory colour must never be
        #: the thing that stops triage running.
        self._authors: authors_mod.AuthorRegistry | None = None
        #: Author state by the raw tag string, since many books share an
        #: author.  Cleared on every refresh so a lookup shows up at once.
        self._author_cache: dict[str, tuple[str, str | None]] = {}
        #: Books marked for a batch action, by key rather than by row: a mark
        #: has to survive a re-sort, a filter change and a rescan.
        self.marked: set[str] = set()
        #: Duplicate-check results, keyed by book key.  Held here rather than
        #: on the Book so a rescan does not throw away what the library said.
        self.duplicates: dict[str, library_mod.DuplicateCheck] = {}
        self._library: library_mod.AudiobookshelfClient | None = None
        #: The thread the interface runs on; worker threads must hand their
        #: activity updates back to it rather than touching widgets directly.
        self._ui_thread: int | None = None
        #: Set once the user has asked to quit and something was still running.
        self._quitting = False
        self._quit_timer: Any = None

    # ------------------------------------------------------------- layout #

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Input(placeholder="search title, author, series or ASIN", id="search")
            table = BookTable(id="books", cursor_type="row", zebra_stripes=True)
            table.add_columns(
                "sel", "disposition", "ok", "title", "author", "series", "pt",
                "asin", "dup", "rate", "notes",
            )
            yield table
            yield Static("", id="detail")
        with Horizontal(id="statusbar"):
            yield Static("", id="status")
            yield ActivityBar()
        yield Footer()

    def on_mount(self) -> None:
        self._ui_thread = threading.get_ident()
        self._authors = self._open_author_registry()
        self.query_one("#books", DataTable).focus()
        self.load_books()

    def on_unmount(self) -> None:
        if self._authors is not None:
            self._authors.close()
            self._authors = None

    def _open_author_registry(self) -> authors_mod.AuthorRegistry | None:
        try:
            return authors_mod.AuthorRegistry(self.config.authors_db_path)
        except Exception:
            LOGGER.debug("could not open the author registry", exc_info=True)
            return None

    # --------------------------------------------------------- author state #

    #: An author forge has never identified.  Shown in the list in this colour
    #: because the spelling is about to become a shelf in the library and an
    #: Audiobookshelf author, and this is the last point at which it is cheap
    #: to fix.
    NEW_AUTHOR_COLOUR = "yellow"

    AUTHOR_NEW = "new"
    AUTHOR_RENAME = "rename"
    AUTHOR_KNOWN = "known"
    AUTHOR_NONE = "none"

    def author_state(self, book: Book) -> tuple[str, str | None]:
        """What the registry makes of this book's author.

        Returns one of AUTHOR_NEW (never seen), AUTHOR_RENAME (known, but the
        tag says something else and shelving will rewrite it), AUTHOR_KNOWN or
        AUTHOR_NONE, with the canonical spelling when there is one.

        Role credits are stripped first, so "Mary Smith - Translator" is judged
        on the name shelving will actually use.
        """

        raw = book.author or ""
        if not raw.strip():
            return self.AUTHOR_NONE, None
        if self._authors is None:
            return self.AUTHOR_KNOWN, None
        if raw in self._author_cache:
            return self._author_cache[raw]

        cleaned = clean_author(raw)
        resolution = self._authors.resolve(cleaned)
        if resolution.unknown:
            state: tuple[str, str | None] = (self.AUTHOR_NEW, None)
        elif resolution.canonical:
            state = (self.AUTHOR_RENAME, resolution.canonical)
        else:
            state = (self.AUTHOR_KNOWN, cleaned)

        self._author_cache[raw] = state
        return state

    def _learn_authors(self, metadata: Any) -> int:
        """Record the identities an accepted Audible match brings with it."""

        if self._authors is None:
            return 0
        contributors = [
            c for c in getattr(metadata, "contributors", ()) or () if not is_role_credit(c.name)
        ]
        if not contributors:
            return 0
        try:
            learned = self._authors.learn(contributors)
        except Exception:
            # The registry is advisory; failing to write it must not cost the
            # tag write that follows.
            LOGGER.debug("could not record author identities", exc_info=True)
            return 0
        self._author_cache.clear()
        if learned:
            LOGGER.debug(
                "learned %s author spelling(s): %s",
                learned,
                ", ".join(c.name for c in contributors),
            )
        return learned

    def _author_cell(self, book: Book) -> str:
        """The author column: coloured when it is one forge has not seen."""

        if not book.author:
            return "[dim]—[/dim]"

        state, _ = self.author_state(book)
        text = escape(book.author)[:22]
        if state == self.AUTHOR_NEW:
            return f"[{self.NEW_AUTHOR_COLOUR}]{text}[/{self.NEW_AUTHOR_COLOUR}]"
        return text

    # ------------------------------------------------------------ activity #

    @contextmanager
    def activity(self, label: str) -> Iterator[None]:
        """Show *label* in the activity indicator for the duration of a block.

        Safe to use from a worker thread, and safe if the indicator has gone
        away: an indicator that something is running must never be the thing
        that stops it running.
        """

        try:
            owner: Any = get_current_worker()
        except NoActiveWorker:
            # Running on the interface thread, where a context manager cannot
            # be abandoned halfway.
            owner = None

        token = self._activity_start(label, owner)
        try:
            yield
        finally:
            self._activity_stop(token)

    def _on_ui_thread(self) -> bool:
        return self._ui_thread is None or threading.get_ident() == self._ui_thread

    def _activity_start(self, label: str, owner: Any = None) -> int | None:
        if not self._on_ui_thread():
            return self._from_thread(self._activity_start, label, owner)
        try:
            return self.query_one(ActivityBar).start(label, owner)
        except NoMatches:
            return None

    def _activity_stop(self, token: int | None) -> None:
        if token is None:
            return
        if not self._on_ui_thread():
            self._from_thread(self._activity_stop, token)
            return
        try:
            self.query_one(ActivityBar).stop(token)
        except NoMatches:
            LOGGER.debug("activity %s ended after the indicator went away", token)

    def _from_thread(self, callback, *args):
        try:
            return self.call_from_thread(callback, *args)
        except Exception:
            # The app is shutting down, or this is not a worker thread after
            # all.  Either way the indicator is cosmetic.
            LOGGER.debug("could not update the activity indicator", exc_info=True)
            return None

    # -------------------------------------------------------------- data #

    # Each worker gets its own group.  All of these run on the App node, and
    # textual's `exclusive` cancels every worker sharing a node *and* group - so
    # with everything in the default group, starting a rescan silently cancelled
    # an in-flight tag write, which for a folder book could leave some tracks
    # written and others not.
    @work(thread=True, exclusive=True, group="scan")
    def load_books(self) -> None:
        """Scan in a thread: probing a directory of books takes real time."""

        self.app.call_from_thread(self._status, f"scanning {self.root}…")
        try:
            with self.activity("Scanning directory"):
                result = scan(self.root)
        except Exception as exc:  # a scan failure must not kill the interface
            LOGGER.exception("scan failed")
            self.app.call_from_thread(self._status, f"[red]scan failed: {escape(str(exc))}[/red]")
            return

        self.app.call_from_thread(self._scan_finished, result.books, result.errors)

    def _scan_finished(self, books: list[Book], errors: list[str]) -> None:
        self.books = books
        # A mark on a book that is no longer there would silently widen the
        # next batch action to nothing, or to the wrong book if a key is reused.
        self.marked &= {book.key for book in books}
        self.refresh_table()
        message = f"{len(books)} book(s)"
        if errors:
            message += f" · [yellow]{len(errors)} scan error(s)[/yellow]"
        self._status(message)
        self._snapshot(books)

    @work(thread=True, group="snapshot", exit_on_error=False)
    def _snapshot(self, books: list[Book]) -> None:
        """Record the tag state of everything scanned, for later diffing."""

        try:
            with self.activity("Saving tag snapshot"), History(self.config.db_path) as store:
                for book in books:
                    if book.metadata is not None:
                        store.snapshot(book.key, book.path, book.metadata.to_dict())
        except Exception:
            LOGGER.debug("could not snapshot the scan", exc_info=True)

    # --------------------------------------------------------- rendering #

    def _matches(self, book: Book) -> bool:
        name = _FILTERS[self.filter_index][0]
        if name == "untriaged" and book.disposition is not Disposition.UNTRIAGED:
            return False
        if name == "attention" and not (
            book.disposition is Disposition.NEEDS_ATTENTION or book.disposition_conflict
        ):
            return False
        if name == "blocked" and book.is_ready:
            return False
        if name == "ready" and not book.is_ready:
            return False
        if name == "duplicate":
            check = self.duplicates.get(book.key)
            if check is None or not check.is_duplicate:
                return False
        if name == "new-author" and self.author_state(book)[0] != self.AUTHOR_NEW:
            return False

        if not self.search_text:
            return True

        needle = self.search_text.casefold()
        meta = book.metadata
        haystack = [book.display_name, book.title or "", book.author or ""]
        if meta is not None:
            haystack += [meta.series or "", meta.asin or "", meta.narrator or ""]
        return any(needle in value.casefold() for value in haystack)

    def refresh_table(self) -> None:
        try:
            table = self.query_one("#books", DataTable)
        except NoMatches:
            return

        # What the user was looking at, so the rebuild can put it back: the row
        # the cursor was on and where the list was scrolled to, which `clear()`
        # rewinds to the top.
        #
        # The row, deliberately, and not the book that was on it.  The triage
        # order moves a book as soon as it is triaged, so the row the cursor is
        # on holds the next book to look at - following the old one would send
        # the cursor chasing it down the list after every keystroke.
        previous_row = table.cursor_row
        previous_offset = table.scroll_offset.y

        table.clear()
        # Rebuilt from the registry each time, so an identity learned since the
        # last refresh stops being flagged.
        self._author_cache.clear()

        self.shown = sort_books(
            [book for book in self.books if self._matches(book)],
            order_by_name(self.sort_name),
            descending=self.sort_descending,
            duplicates=self.duplicates,
        )
        for book in self.shown:
            table.add_row(*self._row(book), key=book.key)

        if self.shown:
            # scroll=False: move_cursor scrolls the row into view, and from the
            # top of a table `clear()` has just rewound that lands it on the
            # *last* line of the viewport.  Every action taken part-way down a
            # long list therefore jumped the list under the cursor, which made
            # working through one a fight.
            table.move_cursor(
                row=max(0, min(previous_row, len(self.shown) - 1)), scroll=False
            )
        self._refresh_detail()
        self._keep_cursor_visible(previous_offset)

    def _keep_cursor_visible(self, offset: int | None = None) -> None:
        """Ask the list to settle at *offset* with the cursor still on screen.

        After the next refresh on purpose.  The detail panel's height follows
        the selected book - a new author or a blocking reason adds a line - so
        the table above it has not been resized yet at the point this is asked
        for, and a height read now is the previous one.
        """

        try:
            table = self.query_one("#books", BookTable)
        except NoMatches:
            return
        self.call_after_refresh(table.keep_cursor_visible, offset)

    def _row(self, book: Book) -> tuple[str, ...]:
        colour = "red" if book.disposition_conflict else _DISPOSITION_COLOUR[book.disposition]
        label = book.disposition_label

        if book.is_ready:
            ready = "[dim]·[/dim]" if book.disposition is Disposition.UNTRIAGED else "[green]ok[/green]"
        else:
            ready = "[yellow]![/yellow]"

        meta = book.metadata
        rate = (
            f"{book.info.normalized_bitrate_kbps}k/{book.info.channels}"
            if book.info is not None
            else "—"
        )
        folder = " [dim](folder)[/dim]" if book.is_folder else ""

        return (
            "[bold cyan]✓[/bold cyan]" if book.key in self.marked else "[dim]·[/dim]",
            f"[{colour}]{label}[/{colour}]",
            ready,
            (escape(book.title) if book.title else f"[dim]{escape(book.display_name)}[/dim]")[:38]
            + folder,
            self._author_cell(book),
            (escape(meta.series) if meta and meta.series else "[dim]—[/dim]")[:20],
            (escape(meta.series_sequence) if meta and meta.series_sequence else "[dim]—[/dim]"),
            (escape(meta.asin) if meta and meta.asin else "[dim]—[/dim]"),
            self._duplicate_cell(book),
            rate,
            escape("; ".join(book.blocking_reasons()))[:34],
        )

    def _duplicate_cell(self, book: Book) -> str:
        """The dup column: blank until the library has been asked about it."""

        check = self.duplicates.get(book.key)
        if check is None:
            return "[dim]·[/dim]"
        colour = _DUPLICATE_COLOUR.get(check.status, "dim")
        return f"[{colour}]{check.label}[/{colour}]"

    def _cursor_row(self) -> int | None:
        """The highlighted row, for logging; None when the table is not there."""

        try:
            return self.query_one("#books", DataTable).cursor_row
        except NoMatches:
            return None

    @property
    def selected(self) -> Book | None:
        """The highlighted book, or None when there is nothing to point at.

        Tolerates the book list not being mounted: a modal screen can be in
        front, and app-level handlers still run.
        """

        try:
            table = self.query_one("#books", DataTable)
        except NoMatches:
            return None
        if not self.shown or not 0 <= table.cursor_row < len(self.shown):
            return None
        return self.shown[table.cursor_row]

    @on(DataTable.RowHighlighted, "#books")
    def _highlighted(self) -> None:
        # Scoped to #books deliberately: an unscoped handler on the app also
        # receives messages bubbling out of modal screens, and the Audible
        # results table would land here with no book list mounted to query.
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        book = self.selected
        try:
            panel = self.query_one("#detail", Static)
        except NoMatches:
            return
        if book is None:
            panel.update("[dim]no book selected[/dim]")
            return

        meta = book.metadata
        lines = [f"[bold]{escape(book.display_name)}[/bold]"]

        if book.info is not None:
            info = book.info
            duration = book.total_duration or info.duration_seconds
            hours, rest = divmod(int(duration), 3600)
            lines.append(
                f"  {info.codec} · {info.normalized_bitrate_kbps} kbps · "
                f"{'mono' if info.channels == 1 else f'{info.channels} ch'} · "
                f"{hours}h{rest // 60:02d}m · {info.chapter_count} chapter(s)"
                + (f" · {len(book.files)} track(s)" if book.is_folder else "")
            )

        if meta is not None:
            if meta.narrator:
                lines.append(f"  narrated by {escape(meta.narrator)}")
            if meta.publisher or meta.published_year:
                lines.append(
                    f"  {escape(meta.publisher or '—')} · {meta.published_year or '—'}"
                )
            if meta.genres:
                lines.append(f"  {escape(', '.join(meta.genres[:4]))}")

        if book.disposition.destroys:
            # No encoding preview, because there will be no encoding: say what
            # the next run will actually do instead.
            lines.append(
                "  [red]→ the next run deletes "
                + ("this folder and everything in it" if book.is_folder else "this file")
                + "[/red]"
            )
        elif book.info is not None and book.disposition.is_actionable:
            from ..rules import decide

            target = decide(
                book.info, book.disposition, max_bitrate_kbps=self.config.max_bitrate_kbps
            )
            colour = "yellow" if target.declined else "green"
            lines.append(f"  [{colour}]→ {target.rationale}[/{colour}]")

        state, canonical = self.author_state(book)
        if state == self.AUTHOR_NEW:
            lines.append(
                f"  [{self.NEW_AUTHOR_COLOUR}]★ new author: "
                f"{escape(book.author or '')}[/{self.NEW_AUTHOR_COLOUR}]\n"
                f"    [dim]forge has not identified this author before, so this "
                f"spelling becomes a shelf in the library[/dim]"
            )
        elif state == self.AUTHOR_RENAME and canonical:
            lines.append(
                f"  [dim]⇒ shelves as {escape(canonical)}[/dim]"
            )

        check = self.duplicates.get(book.key)
        if check is not None and check.status != library_mod.NO_ASIN:
            colour = _DUPLICATE_COLOUR.get(check.status, "dim")
            lines.append(f"  [{colour}]⧉ {escape(check.describe())}[/{colour}]")

        for reason in book.blocking_reasons():
            lines.append(f"  [yellow]! {escape(reason)}[/yellow]")
        for error in book.errors[:3]:
            lines.append(f"  [red]! {escape(error)}[/red]")

        panel.update("\n".join(lines))
        # This panel's height is what shrinks the list above it, so a taller
        # one must not be able to push the selected row off the bottom.
        self._keep_cursor_visible()

    def _status(self, message: str) -> None:
        # Labelled, because an unlabelled dim line directly under the detail
        # panel reads as one more field of the selected book rather than as the
        # result of the last thing the user did.
        text = f"[bold]{STATUS_LABEL}[/bold] {message}" if message else ""
        try:
            self.query_one("#status", Static).update(text)
        except NoMatches:
            LOGGER.debug("dropped a status message: %s", message)

    # ---------------------------------------------------------- actions #

    # --------------------------------------------------------- marking #

    def marked_books(self) -> list[Book]:
        """Every marked book, in the order the list is showing them.

        Marks outside the current filter come last rather than being dropped:
        a mark is an explicit instruction, and silently forgetting one would
        make a batch action quietly narrower than what was asked for.
        """

        if not self.marked:
            return []
        seen: set[str] = set()
        ordered: list[Book] = []
        for book in [*self.shown, *self.books]:
            if book.key in self.marked and book.key not in seen:
                seen.add(book.key)
                ordered.append(book)
        return ordered

    def _mark_status(self, prefix: str = "") -> None:
        if not self.marked:
            self._status(f"{prefix}nothing marked" if prefix else "marks cleared")
            return
        shown = {book.key for book in self.shown}
        hidden = len(self.marked - shown)
        message = f"{prefix}{len(self.marked)} marked"
        if hidden:
            # Worth saying: the next batch action will include these, and the
            # current filter is hiding them.
            message += f" ([yellow]{hidden} not shown[/yellow])"
        self._status(message)

    def action_toggle_mark(self) -> None:
        book = self.selected
        if book is None:
            self._status("[yellow]no book selected[/yellow]")
            return
        if book.key in self.marked:
            self.marked.discard(book.key)
        else:
            self.marked.add(book.key)

        self.refresh_table()
        # Advance, so marking a run of books is one key per book.  Marking the
        # last row leaves the cursor where it is rather than wrapping around.
        # The row is read back after the refresh, which clamps it to the list
        # that is actually there now.
        table = self.query_one("#books", DataTable)
        row = table.cursor_row
        if row + 1 < len(self.shown):
            table.move_cursor(row=row + 1)
            self._refresh_detail()
        self._mark_status()

    def action_mark_shown(self) -> None:
        """Mark every book the filter is showing, or unmark them if all are."""

        shown = {book.key for book in self.shown}
        if not shown:
            self._status("[yellow]nothing to mark[/yellow]")
            return
        if shown <= self.marked:
            self.marked -= shown
        else:
            self.marked |= shown
        self.refresh_table()
        self._mark_status()

    def action_unmark_all(self) -> None:
        if not self.marked:
            self._status("nothing marked")
            return
        self.marked.clear()
        self.refresh_table()
        self._mark_status()

    # ---------------------------------------------------- dispositions #

    def _disposition_targets(self) -> list[Book]:
        """What a disposition key applies to: the marked books, else the cursor."""

        marked = self.marked_books()
        if marked:
            return marked
        book = self.selected
        return [book] if book is not None else []

    def action_set_disposition(self) -> None:
        targets = self._disposition_targets()
        if not targets:
            self._status("[yellow]no book selected[/yellow]")
            return

        # The anchor only supplies the per-file suggestion, so prefer the book
        # under the cursor when it is one of the targets.
        selected = self.selected
        anchor = selected if selected in targets else targets[0]

        def apply(chosen: Disposition | None) -> None:
            if chosen is None:
                return
            # delete asks even for one book: it is the only disposition whose
            # execution cannot be undone, so one keystroke should not be the
            # whole of the decision.
            if len(targets) == 1 and not chosen.destroys:
                self._write_disposition(targets[0], chosen)
            else:
                self._confirm_batch(targets, chosen)

        self.push_screen(DispositionScreen(anchor, targets), apply)

    def action_clear_disposition(self) -> None:
        targets = self._disposition_targets()
        if not targets:
            return
        if len(targets) == 1:
            self._write_disposition(targets[0], Disposition.UNTRIAGED)
        else:
            self._confirm_batch(targets, Disposition.UNTRIAGED)

    def _confirm_batch(self, books: list[Book], value: Disposition) -> None:
        """Ask before writing to more than one book, or before any delete.

        A batch write is one keystroke away from an accidental `M`, and it
        touches files rather than a database, so it is the one disposition path
        that stops to ask - along with `delete`, however few books it is going
        to, because that is the one value the run cannot take back.
        """

        shown = [escape(book.display_name) for book in books[:6]]
        detail = "; ".join(shown)
        if len(books) > 6:
            detail += f"; and {len(books) - 6} more"

        if value is Disposition.DELETE:
            noun = "book" if len(books) == 1 else "books"
            question = f"Delete {len(books)} {noun} on the next run?"
            # Ahead of the names, because it is the part that is not obvious
            # from having pressed the key.
            detail = f"no archived copy; the file(s) go for good — {detail}"
        else:
            question = f"Set {len(books)} books to {value.value}?"

        def go(confirmed: bool) -> None:
            if confirmed:
                self._write_dispositions(books, value)
            else:
                self._status("nothing written")

        self.push_screen(ConfirmScreen(question, detail), go)

    @work(thread=True, group="disposition", exit_on_error=False)
    def _write_disposition(self, book: Book, value: Disposition) -> None:
        try:
            with self.activity("Writing disposition"):
                self._write_disposition_tags(book, value)
        except Exception as exc:
            # Deliberately broad.  Only ForgeError was caught before, so a
            # sqlite3 or OS error escaped the worker, and textual's default
            # exit_on_error took the whole interface down with it.
            LOGGER.exception("could not write a disposition to %s", book.path)
            self.app.call_from_thread(
                self._status, f"[red]could not tag {escape(book.display_name)}: {escape(str(exc))}[/red]"
            )
            return

        book.disposition = value
        book.disposition_conflict = False
        self.app.call_from_thread(
            self._after_change, book, f"{escape(book.display_name)}: {value.value}"
        )

    # Its own group, like every other worker here: sharing one is how an
    # unrelated action came to cancel a tag write in the first place.
    @work(thread=True, group="disposition-batch", exit_on_error=False)
    def _write_dispositions(self, books: list[Book], value: Disposition) -> None:
        """Write one disposition to several books, one file at a time.

        A book that cannot be tagged does not stop the rest: the run reports
        what it managed, and the failures keep their marks so they can be
        retried without marking everything again.
        """

        written: list[Book] = []
        failed: list[tuple[Book, str]] = []
        total = len(books)

        with self.activity(f"Writing disposition to {total} books"):
            for index, book in enumerate(books, 1):
                self.app.call_from_thread(
                    self._status, f"writing {value.value} — {index}/{total}…"
                )
                try:
                    self._write_disposition_tags(book, value)
                except Exception as exc:
                    LOGGER.exception("could not write a disposition to %s", book.path)
                    failed.append((book, str(exc)))
                    continue
                book.disposition = value
                book.disposition_conflict = False
                written.append(book)

        self.app.call_from_thread(self._batch_finished, written, failed, value)

    def _batch_finished(
        self, written: list[Book], failed: list[tuple[Book, str]], value: Disposition
    ) -> None:
        # Clear the marks that were acted on, keep the ones that were not: a
        # stale mark is what makes the *next* batch write the wrong thing.
        self.marked -= {book.key for book in written}
        self.refresh_table()

        message = f"{len(written)} book(s) set to {value.value}"
        if failed:
            first = failed[0]
            message += (
                f" · [red]{len(failed)} could not be tagged[/red] "
                f"({escape(first[0].display_name)}: {escape(first[1])})"
            )
        self._status(message)

    def _write_disposition_tags(self, book: Book, value: Disposition) -> None:
        if book.is_folder:
            failures = disposition_mod.write_folder(book.files, value)
            if failures:
                raise ForgeError(
                    f"{len(failures)} of {len(book.files)} track(s) could not be tagged"
                )
        else:
            disposition_mod.write(book.files[0], value)

    # ------------------------------------------------------ duplicate check #

    def _library_client(self) -> library_mod.AudiobookshelfClient:
        """The Audiobookshelf client, built once and kept for the session.

        Keeping it means the library index is fetched once: the first check
        pays for the fetch and every later one answers from memory.
        """

        if self._library is None:
            self._library = library_mod.AudiobookshelfClient(
                self.config.abs_url or "",
                self.config.abs_api_key or "",
                library_id=self.config.abs_library_id,
            )
        return self._library

    def action_check_duplicate(self) -> None:
        book = self.selected
        if book is None:
            self._status("[yellow]no book selected[/yellow]")
            return
        if not self.config.has_library:
            self._status(f"[yellow]{_NO_LIBRARY}[/yellow]")
            return

        asin = book.metadata.asin if book.metadata else None
        if not asin:
            # Not an error: the check is only meaningful for a known edition.
            self._status(
                f"[yellow]{escape(book.display_name)}: no ASIN, so there is nothing to "
                f"match against the library[/yellow]"
            )
            self.duplicates[book.key] = library_mod.DuplicateCheck(
                status=library_mod.NO_ASIN
            )
            self.refresh_table()
            return

        self._check_duplicates([book], show_detail=True)

    def action_check_all_duplicates(self) -> None:
        if not self.config.has_library:
            self._status(f"[yellow]{_NO_LIBRARY}[/yellow]")
            return
        candidates = [
            book for book in self.books if book.metadata and book.metadata.asin
        ]
        if not candidates:
            self._status("[yellow]no books with an ASIN to check[/yellow]")
            return
        self._status(f"checking {len(candidates)} book(s) against the library…")
        self._check_duplicates(candidates, show_detail=False)

    @work(thread=True, group="duplicates", exit_on_error=False)
    def _check_duplicates(self, books: list[Book], *, show_detail: bool) -> None:
        """Ask Audiobookshelf about each book, in a thread.

        The first call fetches the whole library index, which takes seconds on
        a large server - hence the activity indicator and the thread.
        """

        label = (
            "Checking library"
            if self._library is not None and self._library.indexed
            else "Indexing library"
        )
        try:
            with self.activity(label):
                client = self._library_client()
                results = library_mod.check_books(client, books)
        except Exception as exc:
            LOGGER.exception("duplicate check failed")
            self.app.call_from_thread(
                self._status, f"[red]library check failed: {escape(str(exc))}[/red]"
            )
            return

        self.app.call_from_thread(self._duplicates_finished, books, results, show_detail)

    def _duplicates_finished(
        self,
        books: list[Book],
        results: dict[str, library_mod.DuplicateCheck],
        show_detail: bool,
    ) -> None:
        self.duplicates.update(results)
        self.refresh_table()

        if show_detail and len(books) == 1:
            book = books[0]
            check = results[book.key]
            self._status(f"{escape(book.display_name)}: {escape(check.describe())}")
            if check.is_duplicate:
                # A match is the answer worth stopping on, so it gets a screen
                # with the shelved copy's details rather than one status line.
                self.push_screen(DuplicateScreen(book.display_name, check))
            return

        tally: dict[str, int] = {}
        for check in results.values():
            tally[check.status] = tally.get(check.status, 0) + 1
        duplicates = tally.get(library_mod.DUPLICATE, 0)
        parts = [f"{duplicates} already in the library", f"{tally.get(library_mod.UNIQUE, 0)} new"]
        if tally.get(library_mod.NO_ASIN):
            parts.append(f"{tally[library_mod.NO_ASIN]} without an ASIN")
        if tally.get(library_mod.ERROR):
            parts.append(f"[yellow]{tally[library_mod.ERROR]} could not be checked[/yellow]")
        colour = "red" if duplicates else "green"
        self._status(f"checked {len(results)}: [{colour}]{', '.join(parts)}[/{colour}]")

    def action_edit(self) -> None:
        book = self.selected
        if book is None:
            self._status("[yellow]no book selected[/yellow]")
            return
        if book.metadata is None:
            self._status(
                f"[yellow]{escape(book.display_name)}: tags could not be read, so there is "
                f"nothing to edit[/yellow]"
            )
            return

        # A folder book is asked about first: which track, or the book itself.
        # One file in a folder has nothing to choose between, so it goes
        # straight to the fields like a single-file book does.
        if book.is_folder and len(book.files) > 1:
            self._pick_track(book)
            return

        self._edit_metadata(book, book.metadata)

    @work(thread=True, group="track-tags", exit_on_error=False)
    def _pick_track(self, book: Book) -> None:
        """Read every track's tags, then offer the picker.

        Here rather than in the screen, because screens do no file I/O: a
        300-track book is 300 tag reads, which belongs on a worker with the
        activity indicator running.  A track whose tags cannot be read is kept
        in the list with its own note rather than hidden - it is probably the
        one the user came to look at.
        """

        tracks: list[tuple[Path, tags_mod.BookMetadata | None]] = []
        try:
            with self.activity("Reading track tags"):
                for path in book.files:
                    try:
                        tracks.append((path, tags_mod.read_metadata(path)))
                    except Exception as exc:
                        LOGGER.warning("could not read tags from %s: %s", path, exc)
                        tracks.append((path, None))
        except Exception as exc:
            # Deliberately broad: an exception escaping a worker takes the
            # whole interface down with textual's default exit_on_error.
            LOGGER.exception("could not list the tracks of %s", book.path)
            self.app.call_from_thread(
                self._status,
                f"[red]could not read {escape(book.display_name)}: {escape(str(exc))}[/red]",
            )
            return

        self.app.call_from_thread(self._show_track_picker, book, tracks)

    def _show_track_picker(
        self, book: Book, tracks: list[tuple[Path, tags_mod.BookMetadata | None]]
    ) -> None:
        def chosen(choice: TrackChoice | None) -> None:
            if choice is None:
                return
            if choice.file is None:
                self._edit_metadata(
                    book,
                    book.metadata,
                    fields=tags_mod.BOOK_LEVEL_EDITABLE_FIELDS,
                    note=f"applies to all {len(book.files)} tracks",
                )
                return
            if choice.metadata is None:
                self._status(
                    f"[yellow]{escape(choice.file.name)}: tags could not be read, so "
                    f"there is nothing to edit[/yellow]"
                )
                return
            self._edit_metadata(
                book,
                choice.metadata,
                name_hint=f"{book.display_name} / {choice.file.name}",
                note="this track only",
                only=choice.file,
            )

        self.push_screen(TrackScreen(book.display_name, tracks), chosen)

    def _edit_metadata(
        self,
        book: Book,
        metadata: tags_mod.BookMetadata | None,
        *,
        fields: Sequence[tuple[str, str]] = tags_mod.EDITABLE_FIELDS,
        name_hint: str | None = None,
        note: str = "",
        only: Path | None = None,
    ) -> None:
        """Open the field editor and write what comes back.

        *only* names the one file to write to, for a track edit; without it a
        folder book's edit spreads across its tracks as it always has.
        """

        if metadata is None:  # pragma: no cover - the callers check this
            return

        def apply(updates: dict | None) -> None:
            if updates:
                self._write_tags(book, updates, source="manual", only=only)

        self.push_screen(
            MetadataScreen(
                metadata,
                name_hint=name_hint or book.display_name,
                fields=fields,
                note=note,
            ),
            apply,
        )

    def action_audible(self) -> None:
        book = self.selected
        LOGGER.debug(
            "action_audible: book=%s shown=%d cursor=%s stack=%s",
            book.display_name if book else None,
            len(self.shown),
            self._cursor_row(),
            [type(screen).__name__ for screen in self.screen_stack],
        )
        if book is None:
            # Say so rather than appearing to ignore the key.
            self._status("[yellow]no book selected[/yellow]")
            return

        def picked(metadata: Any) -> None:
            if metadata is None:
                return

            def apply(updates: dict | None) -> None:
                if not updates:
                    return
                # Accepting a match settles who the author is: the metadata
                # carries Audible's contributor ASINs, which is the one thing
                # that identifies an author across spellings.  Learn it here so
                # the new-author flag clears, rather than waiting for a shelve
                # run hours later to discover what this screen already knew.
                self._learn_authors(metadata)
                self._write_tags(
                    book, updates, source="audible", description=f"ASIN {metadata.asin}"
                )

            self.push_screen(
                CompareScreen(
                    book.metadata or tags_mod.BookMetadata(path=str(book.path)),
                    metadata.to_updates(),
                    name_hint=book.display_name,
                ),
                apply,
            )

        try:
            self.push_screen(AudibleScreen(book), picked)
        except Exception as exc:
            # textual swallows exceptions raised inside an action, which is how
            # a broken screen looks exactly like a key that did nothing.
            LOGGER.exception("could not open the Audible screen for %s", book.path)
            self._status(f"[red]could not open the Audible lookup: {escape(str(exc))}[/red]")

    @work(thread=True, group="tag-write", exit_on_error=False)
    def _write_tags(
        self,
        book: Book,
        updates: dict,
        *,
        source: str,
        description: str | None = None,
        only: Path | None = None,
    ) -> None:
        """Write tags to every file of a book, recording what changed.

        With *only*, exactly one track is written and every field goes to it:
        the book-level filter below exists to spread a book's tags across its
        tracks, which is the opposite of what a track edit asked for.
        """

        targets = book.files if book.is_folder else book.files[:1]
        per_file = updates
        if only is not None:
            targets = [only]
        elif book.is_folder:
            # Title, track and disc belong to a track; everything else is a
            # property of the book and applies to all of them.
            per_file = {
                key: value
                for key, value in updates.items()
                if key in tags_mod.BOOK_LEVEL_FIELDS
            }
            if not per_file:
                targets = book.files[:1]
                per_file = updates

        written = 0
        group_id: int | None = None
        try:
            with self.activity("Writing tags"), History(self.config.db_path) as store:
                for path in targets:
                    current = tags_mod.read_metadata(path)
                    changes = tags_mod.diff_metadata(current, per_file)
                    if not changes:
                        continue
                    tags_mod.write_metadata(path, per_file)
                    # One group for the whole edit, however many tracks it
                    # touched, so `forge undo` puts it all back in one go.
                    group_id = store.record(
                        book.key, changes, file_path=path, source=source,
                        description=description, group_id=group_id,
                    )
                    written += len(changes)
        except Exception as exc:
            LOGGER.exception("could not write tags to %s", book.path)
            self.app.call_from_thread(
                self._status, f"[red]could not write {escape(book.display_name)}: {escape(str(exc))}[/red]"
            )
            return

        try:
            book.metadata = tags_mod.read_metadata(book.files[0])
        except ForgeError:
            LOGGER.debug("could not re-read %s", book.files[0], exc_info=True)

        message = (
            f"{escape(book.display_name)}: wrote {written} field(s)"
            if written
            else f"{escape(book.display_name)}: already up to date"
        )
        self.app.call_from_thread(self._after_change, book, message)

    def _after_change(self, book: Book, message: str) -> None:
        self.refresh_table()
        self._status(message)

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
        self.query_one("#books", DataTable).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search_text = event.value.strip()
        self.refresh_table()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#books", DataTable).focus()

    def filter_name(self) -> str:
        """The active filter's name.  For the status line and for tests."""

        return _FILTERS[self.filter_index][0]

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(_FILTERS)
        name, description = _FILTERS[self.filter_index]
        self.refresh_table()
        self._status(f"filter: {name} — {description} ({len(self.shown)} shown)")

    def action_sort(self) -> None:
        def apply(result: tuple[str, bool] | None) -> None:
            if result is None:
                return
            name, descending = result
            self.sort_name, self.sort_descending = name, descending
            # Keep the highlighted book under the cursor across a re-sort: the
            # row index means nothing once the order changes.
            keeping = self.selected
            self.refresh_table()
            if keeping is not None:
                self._move_to(keeping.key)
            order = order_by_name(name)
            direction = "descending" if descending else "ascending"
            self._status(f"sort: {order.name} {direction} — {order.description}")

        self.push_screen(SortScreen(self.sort_name, self.sort_descending), apply)

    def _move_to(self, key: str) -> None:
        """Put the cursor back on the book with *key*, if it is still shown."""

        for index, book in enumerate(self.shown):
            if book.key == key:
                self.query_one("#books", DataTable).move_cursor(row=index)
                self._refresh_detail()
                return

    # ------------------------------------------------------------- quitting #

    #: Workers that touch audio files.  Exiting while one of these is running
    #: is the only way forge could leave a file half-written, so quitting waits
    #: for them by name rather than by guesswork.
    WRITE_GROUPS = frozenset({"tag-write", "disposition", "disposition-batch"})

    def _unfinished_workers(self) -> list[Worker]:
        return [
            worker
            for worker in self.workers
            if worker.state in (WorkerState.PENDING, WorkerState.RUNNING)
        ]

    def action_quit(self) -> None:
        """Quit, but not in the middle of a file operation.

        A thread cannot be interrupted, so ``exit()`` during a tag write used to
        tear the interface down and leave the process sitting there silently
        until the write finished - and a ctrl+c in that window printed a
        traceback over a write that was still running.  The work has to finish
        either way, so the interface stays up and says what it is waiting for.
        """

        running = self._unfinished_workers()
        if not running:
            self.exit(0)
            return

        writing = [w for w in running if w.group in self.WRITE_GROUPS]
        if not self._quitting:
            self._quitting = True
            label = (
                "Finishing writes before quitting"
                if writing
                else "Finishing up before quitting"
            )
            self._activity_start(label)
            self._quit_timer = self.set_interval(0.1, self._quit_when_idle)

        if writing:
            self._status(
                f"[yellow]quitting[/yellow] — finishing {len(writing)} file write(s) "
                f"first, so nothing is left half-written"
            )
        else:
            self._status(
                f"[yellow]quitting[/yellow] — waiting for {len(running)} background "
                f"task(s) to finish"
            )

    def on_key(self, event: Any) -> None:
        """Once quitting has been asked for, stop taking on new work.

        Without this, a stray `r` or `space` after `q` would start something
        new and the wait would never end.
        """

        if not self._quitting or event.key in ("q", "ctrl+q"):
            return
        event.prevent_default()
        event.stop()

    def _quit_when_idle(self) -> None:
        if self._unfinished_workers():
            return
        if self._quit_timer is not None:
            self._quit_timer.stop()
            self._quit_timer = None
        self.exit(0)

    def action_rescan(self) -> None:
        self.load_books()


def run_triage(config: Config, root: Path) -> int:
    """Launch the triage interface.  Returns a process exit code."""

    import sys

    if not sys.stdout.isatty():
        print(
            "forge triage needs an interactive terminal.\n"
            "  for an unattended run, use: forge run",
            file=sys.stderr,
        )
        return 2

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    TriageApp(config, root).run()
    return 0
