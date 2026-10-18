"""Modal screens for the author review interface.

Kept apart from :mod:`forge.tui.screens`, which belongs to triage: these three
screens are only ever opened over the author list, and that file is long enough.

Nothing here writes anything.  A screen collects an answer and dismisses with
it; the app decides what to do, and only a save reaches the database.
"""

from __future__ import annotations

from typing import Any, Sequence

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .markup import literal as escape


class EditNameScreen(ModalScreen[str | None]):
    """Change the spelling one author is written as.

    The known spellings are offered as numbered choices, because the usual fix
    is to choose a spelling Amazon has already used for this author - "AC
    Cobble" back to "A. C. Cobble" - and that should be one keystroke rather
    than retyping a name.  The input is there for everything else.
    """

    CSS = """
    EditNameScreen { align: center middle; }
    #box {
        width: 84; max-width: 100%; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #identity { color: $text-muted; margin-bottom: 1; }
    /* Capped so the dialog still fits on a 32-row terminal with nine
       spellings listed; the rest scrolls. */
    #choices { height: auto; max-height: 8; margin-bottom: 1; }
    .choice { height: 1; }
    #row { height: 3; }
    #label { width: 12; padding-top: 1; color: $text-muted; }
    /* textual's Input is `width: 100%`, so beside a fixed-width label it
       overflows the row by the label's width; 1fr takes what is left. */
    #row Input { width: 1fr; }
    #buttons { height: auto; margin-top: 1; }
    #error { color: $error; height: auto; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "use this name"),
        *[Binding(str(index), f"choose({index})", "", show=False) for index in range(1, 10)],
    ]

    def __init__(
        self, identity: str, current: str, spellings: Sequence[str] = ()
    ) -> None:
        super().__init__()
        self.identity = identity
        self.current = current
        # The current name is not offered as a choice: picking it changes
        # nothing, and it is already in the input.
        self.choices = [
            spelling for spelling in dict.fromkeys(spellings) if spelling != current
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(f"Written as — {escape(self.current)}", id="title")
            yield Static(f"identity: {escape(self.identity)}", id="identity")

            with VerticalScroll(id="choices"):
                if self.choices:
                    yield Static("[dim]spellings already seen for this author:[/dim]")
                    for index, spelling in enumerate(self.choices[:9], 1):
                        yield Static(
                            f"  [bold]{index}[/bold]  {escape(spelling)}",
                            classes="choice",
                        )
                else:
                    yield Static(
                        "[dim]no other spelling has been seen for this author[/dim]"
                    )

            with Horizontal(id="row"):
                yield Label("name", id="label")
                yield Input(value=self.current, id="name")

            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Use this name", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
            yield Static(
                "[dim]a number picks a spelling, tab to type a different one, "
                "ctrl+s uses what is typed, escape cancels[/dim]",
                id="hint",
            )

    def on_mount(self) -> None:
        # The input is focused only when there is nothing to pick.  A focused
        # Input eats digits as text, which would make the numbered spellings
        # unreachable - and a digit is part of plenty of author names
        # ("30-Day Guides to Improve Your Life"), so the bindings must not
        # take priority over typing either.  Tab reaches the input.
        if not self.choices:
            self.query_one("#name", Input).focus()

    def action_choose(self, index: int) -> None:
        if 1 <= index <= len(self.choices):
            self.dismiss(self.choices[index - 1])

    def action_save(self) -> None:
        value = self.query_one("#name", Input).value.strip()
        if not value:
            # Refused in place rather than by closing: an author with no name
            # has no shelf to be filed on.
            self.query_one("#error", Static).update(
                "[bold]an author needs a name[/bold] — type one, or press escape"
            )
            return
        self.dismiss(None if value == self.current else value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#name")
    def _submitted(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class UnsavedChangesScreen(ModalScreen[str | None]):
    """What to do about unsaved edits when quitting.

    Three answers rather than two, so "I did not mean to press q" is not the
    same key as "throw my work away".  Dismisses with "save", "discard" or
    None for cancel.
    """

    SAVE = "save"
    DISCARD = "discard"

    CSS = """
    UnsavedChangesScreen { align: center middle; }
    #box {
        width: 76; max-width: 100%; height: auto;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    .choice { height: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("1", "choose('save')", "", show=False),
        Binding("2", "choose('discard')", "", show=False),
        Binding("3", "cancel", "", show=False),
    ]

    def __init__(self, pending: int) -> None:
        super().__init__()
        self.pending = pending

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(
                f"{self.pending} unsaved name change(s)", id="title"
            )
            yield Static("  [bold]1[/bold]  save them and quit", classes="choice")
            yield Static("  [bold]2[/bold]  quit without saving", classes="choice")
            yield Static("  [bold]3[/bold]  stay here", classes="choice")
            yield Static("press a number, or escape to stay", id="hint")

    def action_choose(self, answer: str) -> None:
        self.dismiss(answer)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AuthorSortScreen(ModalScreen[tuple[str, bool] | None]):
    """Choose how the author list is ordered.

    The same shape as triage's sort dialog - a number or the arrows picks the
    column, ``r`` reverses, ``enter`` applies - because they are the same
    gesture and should not be two different ones.
    """

    CSS = """
    AuthorSortScreen { align: center middle; }
    #box {
        width: 72; max-width: 100%; height: auto; max-height: 90%;
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
        *[Binding(str(index), f"pick({index})", "", show=False) for index in range(0, 10)],
    ]

    def __init__(
        self, orders: Sequence[tuple[str, str]], current: str = "", descending: bool = False
    ) -> None:
        super().__init__()
        self.orders = list(orders)
        self.chosen = next(
            (i for i, (name, _) in enumerate(self.orders) if name == current), 0
        )
        self.reversed_ = descending

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Sort the author list", id="title")
            yield Static("", id="direction")
            for index, _ in enumerate(self.orders):
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
        for index, (name, description) in enumerate(self.orders):
            marker = "▸" if index == self.chosen else " "
            style = "bold" if index == self.chosen else "default"
            self.query_one(f"#choice-{index}", Static).update(
                f"{marker} [bold]{index}[/bold]  [{style}]{name:<12}[/{style}]"
                f"[dim]{description}[/dim]"
            )

    def action_pick(self, index: int) -> None:
        if 0 <= index < len(self.orders):
            self.chosen = index
            self._repaint()

    def action_move(self, delta: int) -> None:
        self.chosen = (self.chosen + delta) % len(self.orders)
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
        self.dismiss((self.orders[self.chosen][0], self.reversed_))

    def action_cancel(self) -> None:
        self.dismiss(None)
