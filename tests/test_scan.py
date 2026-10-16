"""Book discovery: what counts as a book, and what is skipped."""

from __future__ import annotations

import unittest

from tests import helpers
from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg

from forge import disposition as d, scan, tags
from forge.disposition import Disposition as D


@requires_ffmpeg
class Discovery(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.root / "src"
        self.src.mkdir()

    def test_loose_files_are_separate_books(self) -> None:
        copy_mp3(self.src / "a.mp3", seconds=1)
        copy_mp3(self.src / "b.mp3", seconds=1)
        self.assertEqual(len(scan.scan(self.src).books), 2)

    def test_a_directory_of_tracks_is_one_folder_book(self) -> None:
        folder = self.src / "Folder Book"
        for index in (1, 2, 3):
            copy_mp3(folder / f"0{index}.mp3", seconds=1)

        books = scan.scan(self.src).books
        self.assertEqual(len(books), 1)
        self.assertTrue(books[0].is_folder)
        self.assertEqual(len(books[0].files), 3)
        self.assertEqual(books[0].path, folder)

    def test_a_directory_holding_one_file_is_a_single_file_book(self) -> None:
        copy_mp3(self.src / "One Book" / "only.mp3", seconds=1)
        books = scan.scan(self.src).books
        self.assertEqual(len(books), 1)
        self.assertFalse(books[0].is_folder)

    def test_files_in_the_scan_root_are_never_grouped(self) -> None:
        # The root is a drop folder, not a book.
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            copy_mp3(self.src / name, seconds=1)
        self.assertTrue(all(not b.is_folder for b in scan.scan(self.src).books))

    def test_non_audio_files_are_ignored(self) -> None:
        copy_mp3(self.src / "a.mp3", seconds=1)
        (self.src / "notes.txt").write_text("hello")
        (self.src / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        self.assertEqual(len(scan.scan(self.src).books), 1)

    def test_in_progress_files_are_skipped(self) -> None:
        copy_mp3(self.src / "good.mp3", seconds=1)
        copy_mp3(self.src / "busy.tmp.mp3", seconds=1)
        copy_mp3(self.src / "partial.part.mp3", seconds=1)
        copy_mp3(self.src / ".hidden.mp3", seconds=1)

        result = scan.scan(self.src)
        self.assertEqual(len(result.books), 1)
        self.assertEqual(len(result.skipped_in_progress), 3)

    def test_a_missing_directory_is_an_error_not_a_crash(self) -> None:
        result = scan.scan(self.root / "nope")
        self.assertEqual(result.books, [])
        self.assertTrue(result.errors)

    def test_an_unreadable_file_is_recorded_and_the_scan_continues(self) -> None:
        copy_mp3(self.src / "good.mp3", seconds=1)
        (self.src / "broken.m4b").write_bytes(b"this is not audio")

        result = scan.scan(self.src)
        self.assertEqual(len(result.books), 2)
        broken = [b for b in result.books if b.display_name == "broken.m4b"][0]
        self.assertTrue(broken.errors)
        self.assertFalse(broken.is_ready)

    def test_progress_is_reported(self) -> None:
        for name in ("a.mp3", "b.mp3"):
            copy_mp3(self.src / name, seconds=1)
        calls = []
        scan.scan(self.src, progress=lambda done, total, path: calls.append((done, total)))
        self.assertEqual(calls[0], (0, 2))
        self.assertEqual(calls[-1], (2, 2))


@requires_ffmpeg
class Readiness(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.root / "src"
        self.src.mkdir()

    def book(self, **metadata):
        path = self.src / "book.mp3"
        copy_mp3(path, seconds=1)
        if metadata:
            tags.write_metadata(path, metadata)
        return scan.scan(self.src).books[0]

    def test_a_fully_tagged_book_is_ready(self) -> None:
        book = self.book(author="An Author", album="An Album")
        self.assertTrue(book.is_ready)
        self.assertEqual(book.blocking_reasons(), [])

    def test_a_missing_author_blocks(self) -> None:
        book = self.book(album="An Album")
        self.assertFalse(book.is_ready)
        self.assertIn("missing author", book.blocking_reasons())

    def test_a_missing_title_blocks(self) -> None:
        book = self.book(author="An Author")
        self.assertFalse(book.is_ready)
        self.assertIn("missing album or title", book.blocking_reasons())

    def test_albumartist_counts_as_an_author(self) -> None:
        book = self.book(albumartist="An Author", album="An Album")
        self.assertTrue(book.is_ready)

    def test_readiness_is_independent_of_disposition(self) -> None:
        # The dangerous state is a disposition set on a book that cannot be
        # converted, so the two have to be reported separately.
        path = self.src / "book.mp3"
        copy_mp3(path, seconds=1)
        d.write(path, D.DOWNCODE)

        book = scan.scan(self.src).books[0]
        self.assertIs(book.disposition, D.DOWNCODE)
        self.assertFalse(book.is_ready)

    def test_a_skipped_probe_is_not_reported_as_a_probe_failure(self) -> None:
        path = self.src / "book.mp3"
        copy_mp3(path, seconds=1)
        tags.write_metadata(path, {"author": "A", "album": "B"})

        book = scan.scan(self.src, with_probe=False).books[0]
        self.assertFalse(book.probed)
        self.assertEqual(book.blocking_reasons(), [])


@requires_ffmpeg
class Ordering(TempCaseMixin):
    def test_books_needing_a_decision_come_first(self) -> None:
        src = self.root / "src"
        src.mkdir()

        ready = src / "ready.mp3"
        copy_mp3(ready, seconds=1)
        tags.write_metadata(ready, {"author": "A", "album": "B"})
        d.write(ready, D.DOWNCODE)

        attention = src / "attention.mp3"
        copy_mp3(attention, seconds=1)
        tags.write_metadata(attention, {"author": "A", "album": "C"})
        d.write(attention, D.NEEDS_ATTENTION)

        blocked = src / "blocked.mp3"
        copy_mp3(blocked, seconds=1)

        names = [b.display_name for b in scan.scan(src).books]
        self.assertEqual(names[0], "attention.mp3")
        self.assertEqual(names[-1], "ready.mp3")


class Keys(unittest.TestCase):
    def test_keys_are_stable_and_path_specific(self) -> None:
        from pathlib import Path

        self.assertEqual(scan.book_key(Path("/a/b.m4b")), scan.book_key(Path("/a/b.m4b")))
        self.assertNotEqual(scan.book_key(Path("/a/b.m4b")), scan.book_key(Path("/a/c.m4b")))


if __name__ == "__main__":
    unittest.main()
