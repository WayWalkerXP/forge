"""Filename construction and collision handling."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin

from forge import paths, scan


class Sanitising(unittest.TestCase):
    def test_path_separators_and_reserved_characters_are_removed(self) -> None:
        for value in ('A/B', 'A\\B', 'A:B', 'A"B', "A|B", "A?B", "A*B", "A<B>C"):
            with self.subTest(value=value):
                cleaned = paths.sanitize_filename(value)
                for char in paths.INVALID_CHARS:
                    self.assertNotIn(char, cleaned)

    def test_trailing_dots_and_spaces_are_stripped(self) -> None:
        # Windows cannot address such a name, and it is a nuisance on Linux.
        self.assertEqual(paths.sanitize_filename("Title..."), "Title")
        self.assertEqual(paths.sanitize_filename("Title   "), "Title")

    def test_inner_whitespace_is_collapsed(self) -> None:
        self.assertEqual(paths.sanitize_filename("A    B"), "A B")

    def test_empty_input_falls_back(self) -> None:
        for value in ("", "   ", "...", None):
            self.assertEqual(paths.sanitize_filename(value), "Untitled")

    def test_windows_reserved_names_are_suffixed(self) -> None:
        for value in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.m4b"):
            with self.subTest(value=value):
                self.assertTrue(paths.sanitize_filename(value).endswith("_"))

    def test_unicode_survives(self) -> None:
        self.assertEqual(paths.sanitize_filename("Ünïcödé"), "Ünïcödé")

    def test_long_values_are_capped(self) -> None:
        self.assertLessEqual(
            len(paths.sanitize_filename("x" * 500)), paths.MAX_COMPONENT_LENGTH
        )


class OutputNames(unittest.TestCase):
    def test_normal_case(self) -> None:
        self.assertEqual(
            paths.output_filename("David Eddings", "Pawn of Prophecy"),
            "David Eddings - Pawn of Prophecy.m4b",
        )

    def test_missing_values_get_placeholders(self) -> None:
        self.assertEqual(
            paths.output_filename(None, None), "Unknown Author - Unknown Title.m4b"
        )

    def test_combined_length_is_budgeted(self) -> None:
        # Capping each half separately produced names over the 255-byte limit
        # that ext4 imposes on a single component, which failed at write time.
        for author, title in (("A" * 300, "B" * 300), ("A" * 300, "x"), ("x", "B" * 300)):
            with self.subTest(author=len(author), title=len(title)):
                name = paths.output_filename(author, title)
                self.assertLessEqual(len(name), paths.MAX_COMPONENT_LENGTH)

    def test_a_long_name_still_fits_with_temp_and_collision_suffixes(self) -> None:
        name = paths.output_filename("A" * 300, "B" * 300)
        temp = paths.temp_output_path(Path("/out") / name)
        collision = f"{Path(name).stem} (12){Path(name).suffix}"
        self.assertLess(len(temp.name), 255)
        self.assertLess(len(collision), 255)


class TempNames(unittest.TestCase):
    def test_temp_sits_beside_the_final_output(self) -> None:
        final = Path("/out/Author - Title.m4b")
        self.assertEqual(paths.temp_output_path(final).parent, final.parent)

    def test_temp_is_recognised_as_in_progress(self) -> None:
        # Otherwise a concurrent scan would pick up a half-written conversion.
        final = Path("/out/J.R. Mathews - Portal to Nova Roma.m4b")
        self.assertTrue(scan.is_in_progress(paths.temp_output_path(final)))
        self.assertFalse(scan.is_in_progress(final))

    def test_dotted_author_names_do_not_confuse_detection(self) -> None:
        # "J.R. Mathews - Portal.tmp.m4b".suffixes is a mess, so detection
        # looks at the stem instead.
        self.assertTrue(scan.is_in_progress(Path("J.R. Mathews - Portal.tmp.m4b")))
        self.assertFalse(scan.is_in_progress(Path("J.R. Mathews - Portal.m4b")))


class Collisions(TempCaseMixin):
    def test_free_name_is_returned_unchanged(self) -> None:
        target = self.root / "book.m4b"
        self.assertEqual(paths.collision_safe_path(target), target)

    def test_taken_names_get_numbered(self) -> None:
        target = self.root / "book.m4b"
        target.write_bytes(b"x")
        self.assertEqual(paths.collision_safe_path(target).name, "book (2).m4b")
        (self.root / "book (2).m4b").write_bytes(b"x")
        self.assertEqual(paths.collision_safe_path(target).name, "book (3).m4b")

    def test_reserved_names_are_respected_for_dry_runs(self) -> None:
        target = self.root / "book.m4b"
        reserved = {target}
        self.assertEqual(paths.collision_safe_path(target, reserved=reserved).name, "book (2).m4b")

    def test_dotted_directory_names_are_not_split(self) -> None:
        # "Book 1.5" has a ".5" suffix as far as pathlib is concerned.
        folder = self.root / "Book 1.5"
        folder.mkdir()
        self.assertEqual(paths.collision_safe_path(folder).name, "Book 1.5 (2)")


class ArchiveLayout(unittest.TestCase):
    def test_tree_shape_is_mirrored(self) -> None:
        self.assertEqual(
            paths.relative_to_or_name(Path("/src/a/b/c.m4b"), Path("/src")),
            Path("a/b/c.m4b"),
        )

    def test_paths_outside_the_root_collapse_to_a_name(self) -> None:
        # Prevents a path outside the tree escaping into ../.. territory.
        self.assertEqual(
            paths.relative_to_or_name(Path("/elsewhere/c.m4b"), Path("/src")), Path("c.m4b")
        )


if __name__ == "__main__":
    unittest.main()
