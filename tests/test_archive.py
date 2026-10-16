"""File movement: promotion, archiving, and verified relocation."""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempCaseMixin

from forge import archive
from forge.archive import PromotionError


class Promotion(TempCaseMixin):
    def test_temp_becomes_final(self) -> None:
        temp = self.root / "x.tmp.m4b"
        temp.write_bytes(b"content")
        final = self.root / "x.m4b"

        archive.promote(temp, final)

        self.assertTrue(final.exists())
        self.assertFalse(temp.exists())
        self.assertEqual(final.read_bytes(), b"content")

    def test_an_existing_destination_is_never_overwritten(self) -> None:
        final = self.root / "x.m4b"
        final.write_bytes(b"precious")
        temp = self.root / "x.tmp.m4b"
        temp.write_bytes(b"replacement")

        with self.assertRaises(PromotionError):
            archive.promote(temp, final)

        self.assertEqual(final.read_bytes(), b"precious")
        self.assertTrue(temp.exists(), "the temporary file should survive for a retry")

    def test_parent_directories_are_created(self) -> None:
        temp = self.root / "x.tmp.m4b"
        temp.write_bytes(b"content")
        final = self.root / "a" / "b" / "x.m4b"

        archive.promote(temp, final)
        self.assertTrue(final.exists())


class Archiving(TempCaseMixin):
    def test_the_original_is_moved(self) -> None:
        source = self.root / "book.m4b"
        source.write_bytes(b"original")
        destination = archive.archive_original(source, self.root / "arch" / "book.m4b")

        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"original")

    def test_collisions_are_numbered_so_nothing_is_lost(self) -> None:
        # Two different books can legitimately share a filename, and the archive
        # holds the only remaining copy of each original.
        contents = [b"first", b"second", b"third"]
        for index, content in enumerate(contents):
            source = self.root / f"src{index}.m4b"
            source.write_bytes(content)
            archive.archive_original(source, self.root / "arch" / "book.m4b")

        archived = sorted((self.root / "arch").iterdir())
        self.assertEqual(len(archived), 3)
        self.assertEqual(sorted(p.read_bytes() for p in archived), sorted(contents))

    def test_a_directory_is_archived_whole(self) -> None:
        folder = self.root / "Folder Book"
        folder.mkdir()
        for index in (1, 2):
            (folder / f"0{index}.mp3").write_bytes(b"track")

        destination = archive.archive_original(folder, self.root / "arch" / "Folder Book")

        self.assertFalse(folder.exists())
        self.assertTrue(destination.is_dir())
        self.assertEqual(len(list(destination.iterdir())), 2)


class VerifiedRelocation(TempCaseMixin):
    def test_copy_is_verified_then_the_source_is_removed(self) -> None:
        source = self.root / "book.m4b"
        payload = os.urandom(50_000)
        source.write_bytes(payload)

        result = archive.safe_relocate(source, self.root / "out" / "book.m4b")

        self.assertTrue(result.verified)
        self.assertTrue(result.source_removed)
        self.assertFalse(source.exists())
        self.assertEqual(result.destination.read_bytes(), payload)

    def test_a_bad_copy_keeps_the_original(self) -> None:
        # as-is has no archived copy behind it, so a failed verification must
        # never be followed by deleting the source.
        source = self.root / "book.m4b"
        source.write_bytes(b"A" * 5000)

        original = archive.verify_copy
        archive.verify_copy = lambda *a, **k: ["contents differ from the source"]
        try:
            result = archive.safe_relocate(source, self.root / "out" / "book.m4b")
        finally:
            archive.verify_copy = original

        self.assertFalse(result.verified)
        self.assertFalse(result.source_removed)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "out" / "book.m4b").exists())

    def test_size_mismatch_is_detected(self) -> None:
        from forge import validate

        source = self.root / "a.bin"
        source.write_bytes(b"x" * 100)
        truncated = self.root / "b.bin"
        truncated.write_bytes(b"x" * 50)

        failures = validate.verify_copy(source, truncated)
        self.assertTrue(failures)
        self.assertIn("size differs", failures[0])

    def test_content_mismatch_is_detected_at_equal_size(self) -> None:
        from forge import validate

        source = self.root / "a.bin"
        source.write_bytes(b"x" * 100)
        corrupt = self.root / "b.bin"
        corrupt.write_bytes(b"y" * 100)

        failures = validate.verify_copy(source, corrupt)
        self.assertTrue(failures)
        self.assertIn("contents differ", failures[0])

    def test_an_identical_copy_passes(self) -> None:
        from forge import validate

        payload = os.urandom(10_000)
        source = self.root / "a.bin"
        source.write_bytes(payload)
        copy = self.root / "b.bin"
        copy.write_bytes(payload)

        self.assertEqual(validate.verify_copy(source, copy), [])


if __name__ == "__main__":
    unittest.main()
