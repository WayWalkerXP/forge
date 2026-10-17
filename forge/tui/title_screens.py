"""Modal screens for the title-phrase editor."""

from __future__ import annotations

from typing import Sequence

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..titles import strip_phrases
from .markup import literal as escape


class EditPhraseScreen(ModalScreen[str | None]):
    """Type a phrase, with a worked example of what it would do.

    The example is the point: a phrase is only as good as what it removes, and
    seeing "The Land: A LitRPG Adventure" become "The Land" as you type is
    quicker than adding it and testing afterwards.
    """

    CSS = """
    EditPhraseScreen { align: center middle; }
    #box {
        width: 88; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #row { height: 3; }
    #label { width: 10; padding-top: 1; color: $text-muted; }
    #preview { height: auto; margin-top: 1; }
    #error { color: $error; height: auto; }
    #buttons { height: auto; margin-top: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "use it"),
    ]

    #: A title to demonstrate against when the user has not given one.
    SAMPLE = "The Land: A LitRPG Adventure"

    def __init__(self, current: str = "", *, sample: str = "") -> None:
        super().__init__()
        self.current = current
        self.sample = sample or self.SAMPLE

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(
                "Edit phrase" if self.current else "Add a phrase", id="title"
            )
            with Horizontal(id="row"):
                yield Label("phrase", id="label")
                yield Input(value=self.current, id="phrase")
            yield Static("", id="preview")
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Use it", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
            yield Static(
                "[dim]ctrl+s or enter uses it, escape cancels[/dim]", id="hint"
            )

    def on_mount(self) -> None:
        self.query_one("#phrase", Input).focus()
        self._preview()

    @on(Input.Changed, "#phrase")
    def _changed(self) -> None:
        self._preview()

    def _preview(self) -> None:
        phrase = self.query_one("#phrase", Input).value.strip()
        panel = self.query_one("#preview", Static)
        if not phrase:
            panel.update("[dim]type a phrase to see what it would remove[/dim]")
            return
        cleaned = strip_phrases(self.sample, [phrase])
        if cleaned == self.sample:
            panel.update(
                f"[dim]{escape(self.sample)}[/dim]\n"
                f"[yellow]  no change — that phrase is not in the example[/yellow]"
            )
        else:
            panel.update(
                f"[dim]{escape(self.sample)}[/dim]\n"
                f"  → [bold]{escape(str(cleaned))}[/bold]"
            )

    def action_save(self) -> None:
        value = self.query_one("#phrase", Input).value.strip()
        if not value:
            self.query_one("#error", Static).update(
                "[bold]a phrase needs some text in it[/bold]"
            )
            return
        self.dismiss(None if value == self.current else value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#phrase")
    def _submitted(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class TestTitleScreen(ModalScreen[None]):
    """Try a real title against the list as it currently stands."""

    CSS = """
    TestTitleScreen { align: center middle; }
    #box {
        width: 92; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #row { height: 3; }
    #label { width: 8; padding-top: 1; color: $text-muted; }
    #result { height: auto; margin-top: 1; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(self, phrases: Sequence[str]) -> None:
        super().__init__()
        self.phrases = list(phrases)

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(
                f"Try a title against {len(self.phrases)} phrase(s)", id="title"
            )
            with Horizontal(id="row"):
                yield Label("title", id="label")
                yield Input(placeholder="paste a title as it is tagged", id="probe")
            yield Static("", id="result")
            yield Static("[dim]escape closes[/dim]", id="hint")

    def on_mount(self) -> None:
        self.query_one("#probe", Input).focus()

    @on(Input.Changed, "#probe")
    def _changed(self, event: Input.Changed) -> None:
        panel = self.query_one("#result", Static)
        value = event.value.strip()
        if not value:
            panel.update("")
            return
        cleaned = strip_phrases(value, self.phrases)
        if cleaned == value:
            panel.update("[yellow]unchanged — no phrase on the list appears in it[/yellow]")
        else:
            panel.update(f"  → [bold]{escape(str(cleaned))}[/bold]")

    def action_close(self) -> None:
        self.dismiss(None)
