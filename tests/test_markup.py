"""Text that forge did not write, rendered as markup."""

from __future__ import annotations

import unittest

try:
    from textual.content import Content

    from forge.tui.markup import literal

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAVE_TEXTUAL = False


@unittest.skipUnless(HAVE_TEXTUAL, "textual is required")
class LiteralText(unittest.TestCase):
    def render(self, value: str) -> str:
        return Content.from_markup(f"[bold]{literal(value)}[/bold] tail").plain

    def test_a_year_in_brackets_survives(self) -> None:
        # textual's own escape() leaves this alone, and the parser then
        # rejects it: "[2026]" is not a style.
        self.assertEqual(self.render("[2026] Book"), "[2026] Book tail")

    def test_a_series_with_a_hash_survives(self) -> None:
        self.assertEqual(self.render("Octaeteris Cycle #3"), "Octaeteris Cycle #3 tail")

    def test_something_that_looks_like_a_tag_is_shown_not_applied(self) -> None:
        self.assertEqual(self.render("a [bold] b"), "a [bold] b tail")

    def test_an_interior_backslash_is_left_alone(self) -> None:
        self.assertEqual(self.render("back\\slash"), "back\\slash tail")

    def test_a_trailing_backslash_does_not_eat_the_closing_tag(self) -> None:
        self.assertEqual(self.render("ends with \\"), "ends with  tail")

    def test_ordinary_text_is_unchanged(self) -> None:
        self.assertEqual(literal("The Affinity Collector 3"), "The Affinity Collector 3")

    def test_non_strings_are_accepted(self) -> None:
        self.assertEqual(literal(3), "3")


if __name__ == "__main__":
    unittest.main()
