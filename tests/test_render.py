"""Terminal output.

The tests here are about one failure mode: rich treats square brackets as
markup, and an unknown tag is *removed* from the output rather than reported.
Tag values are full of brackets - "[Dramatized Adaptation]" is a real audiobook
subtitle, and every shelved filename ends in "[ASIN]" - so an unescaped value
loses part of itself with no error to go on.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from forge import render
from forge.shelve import ShelveOutcome, ShelvePlan, ShelveResult, ShelveStatus

try:
    from rich.console import Console

    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False

requires_rich = unittest.skipUnless(HAVE_RICH, "rich is required")


def ready_plan(name: str = "book.m4b", target: str = "/library/A/B/B (2026) [B0HM2FPY5F].m4b"):
    return ShelvePlan(source=Path("/out") / name, status=ShelveStatus.READY, target=Path(target))


def skipped_plan(reason: str):
    return ShelvePlan(
        source=Path("/out/book.m4b"), status=ShelveStatus.SKIPPED,
        reason=reason, category="missing author",
    )


class PlainOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = io.StringIO()
        self.renderer = render.PlainRenderer(stream=self.buffer)

    def test_the_plan_is_one_line_per_book(self) -> None:
        self.renderer.shelve_table([ready_plan(), skipped_plan("no author tag")])
        lines = [line for line in self.buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("B0HM2FPY5F", lines[0])
        self.assertIn("no author tag", lines[1])

    def test_a_result_names_the_destination(self) -> None:
        result = ShelveResult(
            plan=ready_plan(), outcome=ShelveOutcome.SHELVED,
            destination=Path("/library/A/B/B (2026) [B0HM2FPY5F].m4b"),
        )
        self.renderer.shelve_book(result, 1, 1)
        output = self.buffer.getvalue()
        self.assertIn("[1/1]", output)
        self.assertIn("shelved", output)
        self.assertIn("[B0HM2FPY5F]", output)

    def test_the_summary_counts_and_explains(self) -> None:
        results = [
            ShelveResult(plan=ready_plan(), outcome=ShelveOutcome.SHELVED),
            ShelveResult(plan=skipped_plan("no author"), outcome=ShelveOutcome.SKIPPED),
        ]
        self.renderer.shelve_summary(results, 12.0)
        output = self.buffer.getvalue()
        self.assertIn("shelved=1", output)
        self.assertIn("missing author", output)


@requires_rich
class RichOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = io.StringIO()
        self.renderer = render.RichRenderer()
        # A real Console, writing somewhere a test can read, with enough width
        # that nothing is dropped for layout reasons rather than markup ones.
        self.renderer.console = Console(
            file=self.buffer, width=200, no_color=True, highlight=False, soft_wrap=True
        )

    def output(self) -> str:
        return self.buffer.getvalue()

    def test_an_asin_in_a_filename_survives(self) -> None:
        self.renderer.shelve_table([ready_plan()])
        self.assertIn("[B0HM2FPY5F]", self.output())

    def test_a_bracketed_subtitle_survives(self) -> None:
        # Lowercase inside the brackets is what rich mistakes for a style tag,
        # and it removes it silently.
        plan = ready_plan(target="/library/A/B/B [dramatized adaptation].m4b")
        self.renderer.shelve_table([plan])
        self.assertIn("[dramatized adaptation]", self.output())

    def test_a_bracketed_skip_reason_survives(self) -> None:
        self.renderer.shelve_table([skipped_plan("no author tag [none at all]")])
        self.assertIn("[none at all]", self.output())

    def test_a_result_line_keeps_its_brackets(self) -> None:
        result = ShelveResult(
            plan=ready_plan(), outcome=ShelveOutcome.SHELVED,
            destination=Path("/library/A/B/B [dramatized adaptation].m4b"),
        )
        self.renderer.shelve_book(result, 2, 7)
        output = self.output()
        self.assertIn("[2/7]", output)
        self.assertIn("[dramatized adaptation]", output)

    def test_a_warning_keeps_its_brackets(self) -> None:
        result = ShelveResult(
            plan=ready_plan(), outcome=ShelveOutcome.SHELVED,
            destination=Path("/library/A/B/B.m4b"),
            warnings=("could not remove [the original]",),
        )
        self.renderer.shelve_book(result, 1, 1)
        self.assertIn("[the original]", self.output())

    def test_the_summary_lists_the_outcomes(self) -> None:
        results = [
            ShelveResult(plan=ready_plan(), outcome=ShelveOutcome.SHELVED),
            ShelveResult(plan=ready_plan(), outcome=ShelveOutcome.SHELVED),
            ShelveResult(plan=skipped_plan("no author [missing]"), outcome=ShelveOutcome.SKIPPED),
        ]
        self.renderer.shelve_summary(results, 61.0)
        output = self.output()
        self.assertIn("shelved", output)
        self.assertIn("1:01", output)
        # The breakdown is by category, which is what tells you what to fix.
        self.assertIn("missing author", output)

    def test_a_bracketed_book_title_survives_the_book_table(self) -> None:
        # The same hazard in the list `forge list` prints.
        from forge.scan import Book
        from forge.tags import BookMetadata

        book = Book(
            path=Path("/src/[2026] book.m4b"),
            is_folder=False,
            files=[Path("/src/[2026] book.m4b")],
            metadata=BookMetadata(
                path="/src/[2026] book.m4b",
                album="A Book [dramatized adaptation]",
                author="An Author",
                asin="B0HM2FPY5F",
            ),
            probed=False,
        )
        self.renderer.book_table([book])
        output = self.output()
        self.assertIn("[dramatized adaptation]", output)
        self.assertIn("B0HM2FPY5F", output)


if __name__ == "__main__":
    unittest.main()
