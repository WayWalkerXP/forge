"""Modal screens for triage: setting a disposition, editing tags, Audible lookup.

Each screen returns its result through ``dismiss`` and does no file I/O of its
own, so the app owns every write.  Keeping the writes in one place is what makes
the "nothing happens that the user did not ask for" guarantee checkable.
"""

from __future__ import annotations

from typing import Any, Sequence

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    LoadingIndicator,
    SelectionList,
    Static,
)

from ..audible import AudibleClient, AudibleError, AudibleMetadata, SearchResult, rank_by_runtime
from ..disposition import ASSIGNABLE, Disposition
from ..rules import suggest_disposition
from ..sorting import SORT_ORDERS
from ..tags import EDITABLE_FIELDS, BookMetadata, format_genres
from .markup import literal as escape

_DISPOSITION_HELP = {
    Disposition.DOWNCODE: "re-encode with the standard rules",
    Disposition.DRAMA: "re-encode, keeping stereo for a full-cast production",
    Disposition.REMUX: "repackage as m4b without re-encoding the audio",
    Disposition.AS_IS: "already correct; just move it to the output directory",
    Disposition.HOLD: "leave this book alone",
}


class DispositionScreen(ModalScreen[Disposition | None]):
    """Pick what should happen to a book, or to every marked book.

    One screen for both cases on purpose: the choice being made is the same,
    and a separate batch dialog would be a second place for the list of
    dispositions to go out of date.
    """

    CSS = """
    DispositionScreen { align: center middle; }
    #box {
        width: 72; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #suggestion { color: $text-muted; margin-bottom: 1; }
    .choice { height: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("1", "choose(0)", "", show=False),
        Binding("2", "choose(1)", "", show=False),
        Binding("3", "choose(2)", "", show=False),
        Binding("4", "choose(3)", "", show=False),
        Binding("5", "choose(4)", "", show=False),
        Binding("0", "clear", "", show=False),
    ]

    def __init__(self, book: Any, targets: Sequence[Any] | None = None) -> None:
        super().__init__()
        self.book = book
        #: Every book the choice will be written to.  The anchor book is only
        #: used for the suggestion, which is a per-file judgement.
        self.targets: list[Any] = list(targets) if targets else [book]

    @property
    def batch(self) -> bool:
        return len(self.targets) > 1

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            if self.batch:
                heading = f"Set disposition — {len(self.targets)} marked books"
            else:
                heading = f"Set disposition — {escape(self.book.display_name)}"
            yield Static(heading, id="title")

            suggestion = ""
            if self.batch:
                # No per-book suggestion: the rules read one file's bitrate and
                # channel count, and averaging that across a marked set would
                # be a recommendation about no book in particular.
                blocked = sum(1 for book in self.targets if not book.is_ready)
                suggestion = f"applies to all {len(self.targets)} marked books"
                if blocked:
                    suggestion += f" — [yellow]{blocked} of them are not ready[/yellow]"
            elif self.book.info is not None:
                proposed, reason = suggest_disposition(self.book.info)
                suggestion = f"suggested: {proposed.value} — {reason}"
            yield Static(suggestion, id="suggestion")

            shared = {book.disposition for book in self.targets}
            current = shared.pop() if len(shared) == 1 else None
            for index, member in enumerate(ASSIGNABLE, 1):
                marker = "▸" if member is current else " "
                yield Static(
                    f"{marker} [bold]{index}[/bold]  {member.value:<9} "
                    f"[dim]{_DISPOSITION_HELP[member]}[/dim]",
                    classes="choice",
                )
            yield Static(
                "  [bold]0[/bold]  clear     [dim]back to untriaged[/dim]", classes="choice"
            )

            blocking = [] if self.batch else self.book.blocking_reasons()
            if blocking:
                yield Static(
                    f"\n[yellow]![/yellow] this book is not ready: {'; '.join(blocking)}\n"
                    f"[dim]a disposition can still be set; forge will defer it until "
                    f"the metadata is fixed[/dim]",
                    id="hint",
                )
            else:
                yield Static("press a number, or escape to cancel", id="hint")

    def action_choose(self, index: int) -> None:
        if 0 <= index < len(ASSIGNABLE):
            self.dismiss(ASSIGNABLE[index])

    def action_clear(self) -> None:
        self.dismiss(Disposition.UNTRIAGED)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SortScreen(ModalScreen[tuple[str, bool] | None]):
    """Choose how the book list is ordered.

    Selecting and applying are separate keys on purpose: a number moves the
    marker and ``enter`` applies, so the direction can be set either side of
    picking a column without the order of the two keystrokes mattering.
    """

    CSS = """
    SortScreen { align: center middle; }
    #box {
        width: 72; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #direction { margin-bottom: 1; }
    .choice { height: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("enter", "apply", "sort"),
        Binding("r", "reverse", "reverse"),
        Binding("left", "ascending", "", show=False),
        Binding("right", "descending", "", show=False),
        Binding("up", "move(-1)", "", show=False),
        Binding("down", "move(1)", "", show=False),
        Binding("0", "pick(0)", "", show=False),
        Binding("1", "pick(1)", "", show=False),
        Binding("2", "pick(2)", "", show=False),
        Binding("3", "pick(3)", "", show=False),
        Binding("4", "pick(4)", "", show=False),
        Binding("5", "pick(5)", "", show=False),
        Binding("6", "pick(6)", "", show=False),
        Binding("7", "pick(7)", "", show=False),
        Binding("8", "pick(8)", "", show=False),
        Binding("9", "pick(9)", "", show=False),
    ]

    def __init__(self, current: str = "", descending: bool = False) -> None:
        super().__init__()
        self.chosen = next(
            (i for i, order in enumerate(SORT_ORDERS) if order.name == current), 0
        )
        # Not `self.descending`: a widget attribute of that name would be a
        # collision waiting to happen, and this one is only ever read here.
        self.reversed_ = descending

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Sort the book list", id="title")
            yield Static("", id="direction")
            for index, order in enumerate(SORT_ORDERS):
                yield Static("", id=f"choice-{index}", classes="choice")
            yield Static(
                "a number or the arrows picks a column, [bold]r[/bold] reverses, "
                "[bold]enter[/bold] sorts, escape cancels",
                id="hint",
            )

    def on_mount(self) -> None:
        self._repaint()

    def _repaint(self) -> None:
        arrow = "descending ↓" if self.reversed_ else "ascending ↑"
        self.query_one("#direction", Static).update(
            f"direction: [bold]{arrow}[/bold]  [dim](r reverses)[/dim]"
        )
        for index, order in enumerate(SORT_ORDERS):
            marker = "▸" if index == self.chosen else " "
            style = "bold" if index == self.chosen else "default"
            self.query_one(f"#choice-{index}", Static).update(
                f"{marker} [bold]{index}[/bold]  [{style}]{order.name:<12}[/{style}]"
                f"[dim]{order.description}[/dim]"
            )

    # Only the first ten orders are reachable by number; the arrows reach any
    # of them, so a tenth order would still be usable.
    def action_pick(self, index: int) -> None:
        if 0 <= index < len(SORT_ORDERS):
            self.chosen = index
            self._repaint()

    def action_move(self, delta: int) -> None:
        self.chosen = (self.chosen + delta) % len(SORT_ORDERS)
        self._repaint()

    def action_reverse(self) -> None:
        self.reversed_ = not self.reversed_
        self._repaint()

    def action_ascending(self) -> None:
        self.reversed_ = False
        self._repaint()

    def action_descending(self) -> None:
        self.reversed_ = True
        self._repaint()

    def action_apply(self) -> None:
        self.dismiss((SORT_ORDERS[self.chosen].name, self.reversed_))

    def action_cancel(self) -> None:
        self.dismiss(None)


class MetadataScreen(ModalScreen[dict | None]):
    """Edit a book's tags by hand."""

    CSS = """
    MetadataScreen { align: center middle; }
    #box {
        width: 90; height: 90%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #fields { height: 1fr; }
    .row { height: 3; }
    .fieldlabel { width: 18; padding-top: 1; color: $text-muted; }
    #buttons { height: auto; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, metadata: BookMetadata, *, name_hint: str = "") -> None:
        super().__init__()
        self.metadata = metadata
        self.name_hint = name_hint

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"Edit metadata — {escape(self.name_hint)}", id="title")
            with VerticalScroll(id="fields"):
                for field, label in EDITABLE_FIELDS:
                    with Horizontal(classes="row"):
                        yield Label(label, classes="fieldlabel")
                        yield Input(
                            value=self._current(field),
                            id=f"f-{field}",
                            placeholder="—",
                        )
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
            yield Static("[dim]tab moves, ctrl+s saves, escape cancels[/dim]")

    def on_mount(self) -> None:
        # Focus the first field so the screen is usable without the mouse:
        # from here tab walks down the fields and on to the buttons.
        first, _ = EDITABLE_FIELDS[0]
        self.query_one(f"#f-{first}", Input).focus()

    def _current(self, field: str) -> str:
        value = getattr(self.metadata, field, None)
        if value is None:
            return ""
        if field == "genres":
            return format_genres(value) or ""
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def action_save(self) -> None:
        updates: dict[str, Any] = {}
        for field, _ in EDITABLE_FIELDS:
            widget = self.query_one(f"#f-{field}", Input)
            raw = widget.value.strip()
            original = self._current(field)
            if raw == original:
                continue
            # An emptied field is an explicit instruction to clear the tag.
            updates[field] = raw if raw else None
        self.dismiss(updates or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class AudibleScreen(ModalScreen[AudibleMetadata | None]):
    """Search Audible, then pick a match.

    Two steps, because the catalogue only returns genres and large cover art for
    a known ASIN, so the chosen candidate is fetched again in full.

    The two steps are stated on screen and reflected in the primary button,
    because an earlier version ran the search and then silently moved focus to
    the results table - which read as nothing having happened at all.
    """

    CSS = """
    AudibleScreen { align: center middle; }
    #box {
        width: 110; height: 85%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; }
    #step { color: $text-muted; margin-bottom: 1; }
    #query { height: auto; }
    /* Horizontal defaults to height: 1fr in textual, so without this each
       query row claims an equal share of the box and squeezes the results
       table and the buttons out of view entirely. */
    #query Horizontal { height: 3; }
    #results { height: 1fr; min-height: 5; }
    #buttons { height: auto; margin-top: 1; }
    #status { height: auto; min-height: 2; }
    .qlabel { width: 8; padding-top: 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    #: Which step the screen is on.  Drives the caption and the primary button.
    STAGE_QUERY = "query"
    STAGE_SEARCHING = "searching"
    STAGE_CHOOSE = "choose"
    STAGE_FETCHING = "fetching"

    def __init__(self, book: Any, *, region: str = "us") -> None:
        super().__init__()
        self.book = book
        # Not self.region: Widget.region is a read-only property for the widget's
        # screen rectangle, and assigning to it raises at construction.
        self.audible_region = region
        self.results: list[SearchResult] = []
        self.stage = self.STAGE_QUERY

    def compose(self) -> ComposeResult:
        meta = self.book.metadata
        with Vertical(id="box"):
            yield Static(f"Audible lookup — {escape(self.book.display_name)}", id="title")
            yield Static("", id="step")
            with Vertical(id="query"):
                with Horizontal():
                    yield Label("author", classes="qlabel")
                    yield Input(value=(self.book.author or ""), id="author")
                with Horizontal():
                    yield Label("title", classes="qlabel")
                    yield Input(
                        value=(self.book.title or self.book.path.stem), id="title-input"
                    )
                with Horizontal():
                    yield Label("asin", classes="qlabel")
                    yield Input(
                        value=(meta.asin if meta and meta.asin else ""),
                        id="asin",
                        placeholder="10 characters; searched directly if filled",
                    )
            table = DataTable(id="results", cursor_type="row", zebra_stripes=True)
            table.add_columns("match", "gap", "asin", "runtime", "title", "author")
            yield table
            with Horizontal(id="buttons"):
                yield Button("Search", variant="primary", id="primary")
                yield Button("Cancel", id="cancel")
            yield Static("", id="status")

    def on_mount(self) -> None:
        self._set_stage(self.STAGE_QUERY)
        self.query_one("#title-input", Input).focus()

    # ------------------------------------------------------------------ stages #

    def _set_stage(self, stage: str) -> None:
        self.stage = stage
        step = self.query_one("#step", Static)
        button = self.query_one("#primary", Button)

        if stage == self.STAGE_QUERY:
            has_asin = bool(self.query_one("#asin", Input).value.strip())
            step.update("Step 1 of 2 — find the book")
            # Naming the ASIN case on the button makes the precedence visible:
            # a filled ASIN field is searched directly and the title is ignored.
            button.label = "Look up ASIN" if has_asin else "Search"
            button.disabled = False
        elif stage == self.STAGE_SEARCHING:
            step.update("Step 1 of 2 — searching…")
            button.label = "Searching…"
            button.disabled = True
        elif stage == self.STAGE_CHOOSE:
            step.update("Step 2 of 2 — choose the closest match")
            button.label = "Use this match"
            button.disabled = False
        elif stage == self.STAGE_FETCHING:
            step.update("Step 2 of 2 — fetching full details…")
            button.label = "Fetching…"
            button.disabled = True

    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    # ----------------------------------------------------------------- actions #

    @on(Input.Changed, "#asin")
    def _asin_changed(self) -> None:
        # Keep the button honest about what it is about to do.
        if self.stage == self.STAGE_QUERY:
            self._set_stage(self.STAGE_QUERY)

    @on(Button.Pressed, "#primary")
    def _primary(self) -> None:
        if self.stage == self.STAGE_CHOOSE:
            self._use_highlighted()
        elif self.stage == self.STAGE_QUERY:
            self.action_search()

    @on(Button.Pressed, "#cancel")
    def _cancel_button(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_search()

    @on(DataTable.RowSelected, "#results")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self._use(event.cursor_row)

    def action_search(self) -> None:
        if self.stage in (self.STAGE_SEARCHING, self.STAGE_FETCHING):
            return

        asin = self.query_one("#asin", Input).value.strip()
        if asin:
            self._lookup_asin(asin)
            return

        title = self.query_one("#title-input", Input).value.strip()
        if not title:
            self._status("[yellow]enter a title, or an ASIN[/yellow]")
            return
        self._search(title, self.query_one("#author", Input).value.strip() or None)

    def _use_highlighted(self) -> None:
        table = self.query_one("#results", DataTable)
        self._use(table.cursor_row)

    def _use(self, row: int) -> None:
        if 0 <= row < len(self.results):
            self._lookup_asin(self.results[row].asin)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # ----------------------------------------------------------------- workers #

    @work(thread=True, exclusive=True)
    def _search(self, title: str, author: str | None) -> None:
        self.app.call_from_thread(self._set_stage, self.STAGE_SEARCHING)
        self.app.call_from_thread(self._status, "")
        try:
            with AudibleClient(region=self.audible_region) as client:
                found = client.search(title=title, author=author)
        except AudibleError as exc:
            self.app.call_from_thread(self._set_stage, self.STAGE_QUERY)
            self.app.call_from_thread(self._status, f"[red]{exc}[/red]")
            return

        source_seconds = self.book.total_duration or None
        ranked = rank_by_runtime(found, source_seconds)
        self.app.call_from_thread(self._show_results, ranked, source_seconds)

    def _show_results(self, results: list[SearchResult], source_seconds: float | None) -> None:
        self.results = results
        table = self.query_one("#results", DataTable)
        table.clear()

        if not results:
            self._set_stage(self.STAGE_QUERY)
            self._status("[yellow]no results — try a shorter title, or an ASIN[/yellow]")
            return

        colours = {"high": "green", "medium": "yellow", "low": "red", "unknown": "dim"}
        for item in results:
            confidence = item.confidence(source_seconds)
            gap = item.runtime_delta_minutes(source_seconds)
            runtime = (
                f"{item.runtime_minutes // 60}h{item.runtime_minutes % 60:02d}m"
                if item.runtime_minutes
                else "—"
            )
            table.add_row(
                f"[{colours[confidence]}]{confidence}[/{colours[confidence]}]",
                f"{gap}m" if gap is not None else "—",
                escape(item.asin),
                runtime,
                escape(item.title)[:44],
                escape(item.author)[:24],
            )

        self._set_stage(self.STAGE_CHOOSE)
        table.focus()

        best = results[0].confidence(source_seconds)
        note = (
            "the top result's runtime matches this file closely"
            if best == "high"
            else "check the runtimes: none of these is a close match"
            if best in ("low", "unknown")
            else "the top result is within 15 minutes of this file"
        )
        self._status(
            f"{len(results)} result(s), closest runtime first — {note}.\n"
            f"[dim]move with ↑↓, then press enter or Use this match[/dim]"
        )

    @work(thread=True, exclusive=True)
    def _lookup_asin(self, asin: str) -> None:
        self.app.call_from_thread(self._set_stage, self.STAGE_FETCHING)
        self.app.call_from_thread(self._status, f"fetching {asin}…")
        try:
            with AudibleClient(region=self.audible_region) as client:
                metadata = client.lookup(asin)
        except AudibleError as exc:
            self.app.call_from_thread(
                self._set_stage,
                self.STAGE_CHOOSE if self.results else self.STAGE_QUERY,
            )
            self.app.call_from_thread(self._status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self.dismiss, metadata)


class CompareScreen(ModalScreen[dict | None]):
    """Choose which Audible fields to write over the local tags."""

    CSS = """
    CompareScreen { align: center middle; }
    #box {
        width: 110; height: 85%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #choices { height: 1fr; border: none; }
    #buttons { height: auto; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "apply", "apply"),
    ]

    def __init__(self, current: BookMetadata, incoming: dict[str, Any], *, name_hint: str = "") -> None:
        super().__init__()
        self.current = current
        self.incoming = incoming
        self.name_hint = name_hint

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"Apply Audible metadata — {escape(self.name_hint)}", id="title")

            options = []
            for field, value in sorted(self.incoming.items()):
                existing = getattr(self.current, field, None)
                existing_text = (
                    format_genres(existing) if field == "genres" else existing
                ) or "—"
                new_text = format_genres(value) if field == "genres" else value
                changed = str(existing_text) != str(new_text)
                # Escaped: a title or series with square brackets in it is
                # valid console markup and would raise while rendering.
                label = (
                    f"{field:<16} {escape(str(new_text))[:40]:<42} "
                    f"[dim]was {escape(str(existing_text))[:26]}[/dim]"
                )
                # Fields that would not change anything start unselected, so the
                # confirmation shows only real edits.
                options.append((label, field, changed))

            yield SelectionList[str](*options, id="choices")
            with Horizontal(id="buttons"):
                yield Button("Apply selected", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")
            yield Static("[dim]space toggles · ctrl+s applies · escape cancels[/dim]")

    def action_apply(self) -> None:
        chosen = set(self.query_one("#choices", SelectionList).selected)
        updates = {k: v for k, v in self.incoming.items() if k in chosen}
        self.dismiss(updates or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        self.action_apply()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class DuplicateScreen(ModalScreen[None]):
    """What Audiobookshelf already holds for this ASIN.

    Shown only for a match: the interesting case is the one where the user is
    about to spend an hour converting a book they already own, and a status
    line is too easy to miss for that.
    """

    CSS = """
    DuplicateScreen { align: center middle; }
    #box {
        width: 94; height: auto; max-height: 80%;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    /* A scroll container defaults to 1fr, which would leave a mostly empty
       box for the usual single match. */
    #matches { height: auto; max-height: 14; }
    .match { height: auto; margin-bottom: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("enter", "close", "", show=False),
    ]

    def __init__(self, name: str, check: Any) -> None:
        super().__init__()
        self.name_hint = name
        self.check = check

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"Already in the library — {escape(self.name_hint)}", id="title")
            yield Static(f"ASIN [bold]{escape(self.check.asin or '')}[/bold] matches:")
            with VerticalScroll(id="matches"):
                # Everything here came from the server, and a series like
                # "Octaeteris Cycle #3" is valid console markup, so it is
                # escaped rather than rendered.
                for item in self.check.matches:
                    yield Static(
                        f"  [bold]{escape(item.title or '(untitled)')}[/bold]\n"
                        f"  {escape(item.author or '—')}"
                        + (f" · {escape(item.series)}" if item.series else "")
                        + f"\n  [dim]{escape(item.path or '—')}[/dim]",
                        classes="match",
                    )
            yield Static(
                "[dim]forge has changed nothing. Set the disposition to hold if "
                "you want to leave this book alone.[/dim]",
                id="hint",
            )
            with Horizontal(id="buttons"):
                yield Button("Close", variant="primary", id="close")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.action_close()


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #box {
        width: 70; height: auto;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #buttons { height: auto; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "no", "no"), Binding("y", "yes", "yes")]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self.question)
            if self.detail:
                yield Static(f"[dim]{self.detail}[/dim]")
            with Horizontal(id="buttons"):
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)
