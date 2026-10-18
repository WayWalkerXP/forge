"""Terminal output for ``forge run`` and the non-interactive subcommands.

Two renderers behind one interface.  :class:`RichRenderer` draws tables and a
live progress bar when stdout is a terminal; :class:`PlainRenderer` emits one
timestamped line per event for a log file.  ``forge run`` from cron therefore
produces something readable in ``cron.log`` rather than a screenful of escape
codes, which is why the choice is made from ``isatty`` rather than a flag.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Protocol

from .convert import ConversionResult, Outcome, StageEvent
from .disposition import Disposition
from .plan import Plan, PlanStatus
from .scan import Book

#: Outcome -> (symbol, rich style)
_OUTCOME_STYLE = {
    Outcome.CONVERTED: ("✓", "green"),
    Outcome.RELOCATED: ("→", "green"),
    Outcome.DELETED: ("×", "red"),
    Outcome.SKIPPED: ("·", "dim"),
    Outcome.NEEDS_ATTENTION: ("!", "yellow"),
    Outcome.FAILED: ("✗", "red"),
}

_DISPOSITION_STYLE = {
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


def _plain(value: object) -> str:
    """Text rich must print exactly as written.

    Anything that came from a tag can contain console markup: audiobook titles
    really do read "[Dramatized Adaptation]", and rich silently *removes* an
    unknown tag rather than failing, so an unescaped title loses a chunk of
    itself in the table with no error to go on.  Imported here rather than at
    module scope so that :func:`make_renderer` can still fall back to plain
    output when rich is missing.
    """

    from rich.markup import escape

    return escape(str(value))


def hms(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


class Renderer(Protocol):
    """What the CLI needs from an output layer."""

    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def book_table(self, books: list[Book]) -> None: ...
    def plan_table(self, plans: list[Plan]) -> None: ...
    def start_book(self, plan: Plan, index: int, total: int) -> None: ...
    def stage(self, event: StageEvent) -> None: ...
    def finish_book(self, result: ConversionResult) -> None: ...
    def summary(self, results: list[ConversionResult], elapsed: float) -> None: ...
    def shelve_table(self, plans: list[Any]) -> None: ...
    def shelve_book(self, result: Any, index: int, total: int) -> None: ...
    def shelve_summary(self, results: list[Any], elapsed: float) -> None: ...


def make_renderer(*, force_plain: bool = False, no_color: bool = False) -> Renderer:
    """Pick a renderer for the current environment."""

    interactive = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if force_plain or not interactive:
        return PlainRenderer()
    try:
        return RichRenderer(no_color=no_color)
    except ImportError:
        return PlainRenderer()


# --------------------------------------------------------------------------- #
# Plain text, for logs and pipes
# --------------------------------------------------------------------------- #


class PlainRenderer:
    """One line per event, timestamped, no escape codes."""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout

    def _write(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {level:<5} {message}", file=self.stream, flush=True)

    def info(self, message: str) -> None:
        self._write("info", message)

    def warn(self, message: str) -> None:
        self._write("warn", message)

    def error(self, message: str) -> None:
        print(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} error {message}",
            file=sys.stderr,
            flush=True,
        )

    def book_table(self, books: list[Book]) -> None:
        for book in books:
            bits = [
                f"disposition={book.disposition.value}",
                f"ready={'yes' if book.is_ready else 'no'}",
            ]
            if book.info:
                bits.append(f"{book.info.normalized_bitrate_kbps}k/{book.info.channels}ch")
            reasons = book.blocking_reasons()
            if reasons:
                bits.append(f"blocked={'; '.join(reasons)}")
            self._write("info", f"{book.display_name}: {' '.join(bits)}")

    def plan_table(self, plans: list[Plan]) -> None:
        for plan in plans:
            self._write("info", f"{plan.source_path.name}: {plan.describe()}")

    def start_book(self, plan: Plan, index: int, total: int) -> None:
        self._write(
            "info",
            f"[{index}/{total}] {plan.source_path.name}: {plan.disposition.value} "
            f"-> {plan.target_summary}",
        )

    def stage(self, event: StageEvent) -> None:
        # Progress samples would flood a log file, so only stage changes land.
        if event.progress is None and event.message:
            self._write("info", f"  {event.stage.value}: {event.message}")

    def finish_book(self, result: ConversionResult) -> None:
        level = "info" if result.ok else ("warn" if result.outcome is not Outcome.FAILED else "error")
        text = f"{result.source_name}: {result.outcome.value} - {result.message}"
        if level == "error":
            self.error(text)
        else:
            self._write(level, text)
        for warning in result.warnings:
            self._write("warn", f"  {result.source_name}: {warning}")

    def summary(self, results: list[ConversionResult], elapsed: float) -> None:
        counts = _count(results)
        parts = [f"{name}={count}" for name, count in counts.items() if count]
        self._write("info", f"finished in {hms(elapsed)}: {' '.join(parts) or 'nothing to do'}")

    # ------------------------------------------------------------- shelving #

    def shelve_table(self, plans: list[Any]) -> None:
        for plan in plans:
            self._write("info", f"{plan.name}: {plan.describe()}")

    def shelve_book(self, result: Any, index: int, total: int) -> None:
        from .shelve import ShelveOutcome

        level = {
            ShelveOutcome.SHELVED: "info",
            ShelveOutcome.SET_ASIDE: "warn",
            ShelveOutcome.SKIPPED: "warn",
            ShelveOutcome.FAILED: "error",
        }[result.outcome]
        where = f" -> {result.destination}" if result.destination else ""
        detail = f" ({result.message})" if result.message else ""
        text = f"[{index}/{total}] {result.name}: {result.outcome.value}{where}{detail}"
        if level == "error":
            self.error(text)
        else:
            self._write(level, text)
        for warning in result.warnings:
            self._write("warn", f"  {result.name}: {warning}")

    def shelve_summary(self, results: list[Any], elapsed: float) -> None:
        from .shelve import count_categories, count_outcomes

        parts = [f"{name}={count}" for name, count in count_outcomes(results).items() if count]
        self._write("info", f"shelving finished in {hms(elapsed)}: {' '.join(parts) or 'nothing to do'}")
        for category, count in count_categories(results).items():
            self._write("info", f"  {count} {category}")


# --------------------------------------------------------------------------- #
# Rich, for a terminal
# --------------------------------------------------------------------------- #


class RichRenderer:
    """Tables and a live progress bar."""

    def __init__(self, *, no_color: bool = False) -> None:
        from rich.console import Console

        self.console = Console(no_color=no_color, highlight=False, soft_wrap=False)
        self._progress: Any = None
        self._task: Any = None

    def info(self, message: str) -> None:
        self.console.print(message)

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]![/yellow] {message}")

    def error(self, message: str) -> None:
        self.console.print(f"[red]✗[/red] {message}", style="red")

    def book_table(self, books: list[Book]) -> None:
        from rich.table import Table

        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("disposition")
        table.add_column("ok", justify="center")
        table.add_column("title", overflow="ellipsis", max_width=34)
        table.add_column("author", overflow="ellipsis", max_width=20)
        table.add_column("series", overflow="ellipsis", max_width=20)
        table.add_column("asin")
        table.add_column("rate", justify="right")
        table.add_column("notes", overflow="fold", style="yellow")

        for index, book in enumerate(books, 1):
            style = _DISPOSITION_STYLE.get(book.disposition, "")
            label = book.disposition_label
            if book.disposition_conflict:
                style = "red"
            rate = (
                f"{book.info.normalized_bitrate_kbps}k/{book.info.channels}"
                if book.info
                else "—"
            )
            ready = "[green]ok[/green]" if book.is_ready else "[yellow]![/yellow]"
            if book.disposition is Disposition.UNTRIAGED and book.is_ready:
                ready = "[dim]·[/dim]"
            meta = book.metadata
            table.add_row(
                str(index),
                f"[{style}]{label}[/{style}]" if style else label,
                ready,
                _plain(book.title) if book.title else "[dim]—[/dim]",
                _plain(book.author) if book.author else "[dim]—[/dim]",
                _series_label(meta),
                (_plain(meta.asin) if meta and meta.asin else "[dim]—[/dim]"),
                rate,
                _plain("; ".join(book.blocking_reasons())),
            )

        self.console.print(table)

    def plan_table(self, plans: list[Plan]) -> None:
        from rich.table import Table

        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("source", overflow="ellipsis", max_width=38)
        table.add_column("action")
        table.add_column("target")
        table.add_column("output", overflow="ellipsis", max_width=40)
        table.add_column("detail", overflow="fold")

        for plan in plans:
            if plan.status is PlanStatus.PLANNED:
                style, detail = "green", "; ".join(plan.warnings)
            elif plan.status is PlanStatus.SKIPPED:
                style, detail = "dim", plan.target.rationale
            else:
                style, detail = "yellow", "; ".join(plan.errors)

            # A declined target has nothing to show, but a deletion's "target"
            # is the deletion itself, which is worth the column.
            if plan.disposition.destroys:
                target = plan.target_summary
            else:
                target = "" if plan.target.declined else str(plan.target)

            table.add_row(
                _plain(plan.source_path.name),
                f"[{style}]{plan.disposition.value}[/{style}]",
                target,
                _plain(plan.output_path.name) if plan.output_path else "[dim]—[/dim]",
                _plain(detail),
            )

        self.console.print(table)

    def start_book(self, plan: Plan, index: int, total: int) -> None:
        from rich.progress import (
            BarColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeRemainingColumn,
        )

        self.console.print(
            f"[bold]\\[{index}/{total}][/bold] {_plain(plan.source_path.name)} "
            f"[dim]{plan.disposition.value} → {plan.target_summary}[/dim]"
        )
        self._progress = Progress(
            TextColumn("  {task.description}"),
            BarColumn(bar_width=34),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=self.console,
            transient=True,
        )
        self._progress.start()
        self._task = self._progress.add_task("preparing", total=1000)

    def stage(self, event: StageEvent) -> None:
        if self._progress is None or self._task is None:
            return
        if event.progress is not None:
            self._progress.update(
                self._task,
                completed=int(event.progress.fraction * 1000),
                description=f"{event.stage.value}",
            )
        else:
            self._progress.update(self._task, description=event.stage.value)

    def finish_book(self, result: ConversionResult) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task = None

        symbol, style = _OUTCOME_STYLE.get(result.outcome, ("·", ""))
        self.console.print(f"  [{style}]{symbol}[/{style}] {_plain(result.message)}")
        for warning in result.warnings:
            self.console.print(f"    [yellow]![/yellow] {_plain(warning)}")

    def summary(self, results: list[ConversionResult], elapsed: float) -> None:
        from rich.table import Table

        counts = _count(results)
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column("", style="bold")
        table.add_column("", justify="right")
        for name, count in counts.items():
            if not count:
                continue
            style = _OUTCOME_STYLE.get(Outcome(name), ("", ""))[1] if name in Outcome.__members__.values() else ""
            table.add_row(f"[{style}]{name}[/{style}]" if style else name, str(count))
        table.add_row("elapsed", hms(elapsed))

        self.console.print()
        self.console.print(table)

    # ------------------------------------------------------------- shelving #

    def shelve_table(self, plans: list[Any]) -> None:
        from rich.table import Table

        from .shelve import ShelveStatus

        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("source", overflow="ellipsis", max_width=40)
        table.add_column("action")
        table.add_column("destination", overflow="fold")

        for plan in plans:
            if plan.status is ShelveStatus.READY:
                style, action = "green", "shelve"
                where = _plain(str(plan.target))
                notes = ", ".join(plan.fix_notes())
                if notes:
                    where += f"\n[dim]{_plain(notes)}[/dim]"
            elif plan.status is ShelveStatus.DUPLICATE:
                style, action, where = "yellow", "duplicate", _plain(plan.reason)
            else:
                style, action, where = "dim", plan.category or "skip", _plain(plan.reason)

            table.add_row(_plain(plan.name), f"[{style}]{action}[/{style}]", where)

        self.console.print(table)

    def shelve_book(self, result: Any, index: int, total: int) -> None:
        symbol, style = _shelve_style(result.outcome)
        self.console.print(
            f"[bold]\\[{index}/{total}][/bold] {_plain(result.name)}"
        )
        detail = str(result.destination) if result.destination else result.message
        self.console.print(
            f"  [{style}]{symbol}[/{style}] {result.outcome.value}  {_plain(detail)}"
        )
        if result.destination and result.message:
            self.console.print(f"    [dim]{_plain(result.message)}[/dim]")
        for warning in result.warnings:
            self.console.print(f"    [yellow]![/yellow] {_plain(warning)}")

    def shelve_summary(self, results: list[Any], elapsed: float) -> None:
        from rich.table import Table

        from .shelve import ShelveOutcome, count_categories, count_outcomes

        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column("", style="bold")
        table.add_column("", justify="right")
        counts = count_outcomes(results)
        for outcome in ShelveOutcome:
            count = counts.get(outcome.value, 0)
            if not count:
                continue
            style = _shelve_style(outcome)[1]
            table.add_row(f"[{style}]{outcome.value}[/{style}]", str(count))
        table.add_row("elapsed", hms(elapsed))

        self.console.print()
        self.console.print(table)

        # Why books were not shelved matters more than the raw count.
        categories = count_categories(results)
        if categories:
            self.console.print()
            for category, count in categories.items():
                self.console.print(f"  [dim]{count:>3}[/dim]  {_plain(category)}")


def _shelve_style(outcome: Any) -> tuple[str, str]:
    from .shelve import ShelveOutcome

    return {
        ShelveOutcome.SHELVED: ("✓", "green"),
        ShelveOutcome.SET_ASIDE: ("⊘", "yellow"),
        ShelveOutcome.SKIPPED: ("·", "dim"),
        ShelveOutcome.FAILED: ("✗", "red"),
    }.get(outcome, ("·", ""))


def _series_label(meta: Any) -> str:
    if meta is None or not meta.series:
        return "[dim]—[/dim]"
    if meta.series_sequence:
        return _plain(f"{meta.series} #{meta.series_sequence}")
    return _plain(str(meta.series))


def _count(results: list[ConversionResult]) -> dict[str, int]:
    counts = {outcome.value: 0 for outcome in Outcome}
    for result in results:
        counts[result.outcome.value] += 1
    return counts
