"""The character rules, the collision suffix, and the archive layout."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin

from forge import naming, paths, scan
from forge.tags import BookMetadata


class CharacterRules(unittest.TestCase):
    def test_every_separator_and_reserved_character_is_listed(self) -> None:
        # forge.naming holds every name it builds to this list, so a character
        # missing from it is a character that reaches the filesystem.
        for char in ('/', '\\', ':', '"', "|", "?", "*", "<", ">", "\0"):
            with self.subTest(char=char):
                self.assertIn(char, paths.INVALID_CHARS)

    def test_the_windows_device_names_are_listed(self) -> None:
        for value in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            with self.subTest(value=value):
                self.assertIn(value, paths.WINDOWS_RESERVED)


class TempNames(unittest.TestCase):
    def test_temp_sits_beside_the_final_output(self) -> None:
        final = Path("/out/Pawn of Prophecy (1982) [B002V0QUOA].m4b")
        self.assertEqual(paths.temp_output_path(final).parent, final.parent)

    def test_temp_is_recognised_as_in_progress(self) -> None:
        # Otherwise a concurrent scan would pick up a half-written conversion.
        final = Path("/out/Portal to Nova Roma (2021) [B08X1KQ2PL].m4b")
        self.assertTrue(scan.is_in_progress(paths.temp_output_path(final)))
        self.assertFalse(scan.is_in_progress(final))

    def test_dotted_names_do_not_confuse_detection(self) -> None:
        # "Space_ 1969 v1.5.tmp.m4b".suffixes is a mess, so detection looks at
        # the stem instead.
        self.assertTrue(scan.is_in_progress(Path("Space_ 1969 v1.5.tmp.m4b")))
        self.assertFalse(scan.is_in_progress(Path("Space_ 1969 v1.5.m4b")))

    def test_the_longest_name_a_template_can_build_still_fits_with_both(self) -> None:
        # The budget in forge.naming has to leave room for this suffix and a
        # collision marker, or the last step of a conversion fails on a name
        # too long to write.
        template = naming.parse_template(
            "<author> - <album>", setting="filename_template", allow_slash=False
        )
        name = naming.render_filename(
            template,
            naming.tag_values(BookMetadata(path="/x", author="A" * 300, album="B" * 300)),
        )
        temp = paths.temp_output_path(Path("/out") / name)
        self.assertLessEqual(len(temp.name.encode()), 255)
        self.assertLessEqual(len(f"{Path(name).stem} (999).m4b".encode()), 255)


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
