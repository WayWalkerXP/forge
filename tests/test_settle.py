"""Waiting for files to stop changing before an unattended run touches them."""

from __future__ import annotations

import os
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from tests.helpers import TempCaseMixin

from forge.settle import describe_age, partition_settled


@dataclass
class FakeBook:
    path: Path
    files: list[Path] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.path.name


class Settling(TempCaseMixin):
    def make(self, name: str, *, age_seconds: float = 0.0) -> FakeBook:
        path = self.root / name
        path.write_bytes(b"content")
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(path, (past, past))
        return FakeBook(path=path, files=[path])

    def test_an_old_stable_file_is_ready(self) -> None:
        book = self.make("old.m4b", age_seconds=3600)
        ready, pending = partition_settled([book], min_age=120, settle_seconds=0)
        self.assertEqual(ready, [book])
        self.assertEqual(pending, [])

    def test_a_freshly_written_file_waits(self) -> None:
        # Catches a download that is still in flight.
        book = self.make("new.m4b")
        ready, pending = partition_settled([book], min_age=120, settle_seconds=0)
        self.assertEqual(ready, [])
        self.assertEqual(len(pending), 1)
        self.assertIn("waiting for it to settle", pending[0][1])

    def test_a_file_changing_during_the_window_waits(self) -> None:
        book = self.make("growing.m4b", age_seconds=3600)

        original_sleep = time.sleep

        def grow(_seconds: float) -> None:
            book.path.write_bytes(b"content and more")

        time.sleep = grow
        try:
            ready, pending = partition_settled([book], min_age=0, settle_seconds=0.01)
        finally:
            time.sleep = original_sleep

        self.assertEqual(ready, [])
        self.assertIn("still being written", pending[0][1])

    def test_a_vanished_file_waits(self) -> None:
        book = self.make("gone.m4b", age_seconds=3600)
        original_sleep = time.sleep

        def remove(_seconds: float) -> None:
            book.path.unlink()

        time.sleep = remove
        try:
            ready, pending = partition_settled([book], min_age=0, settle_seconds=0.01)
        finally:
            time.sleep = original_sleep

        self.assertEqual(ready, [])
        self.assertIn("disappeared", pending[0][1])

    def test_a_folder_book_waits_if_any_track_is_unsettled(self) -> None:
        # Converting three settled tracks and one half-written one would
        # silently produce a short book.
        folder = self.root / "Folder"
        folder.mkdir()
        settled = folder / "01.mp3"
        settled.write_bytes(b"x")
        fresh = folder / "02.mp3"
        fresh.write_bytes(b"x")
        past = time.time() - 3600
        os.utime(settled, (past, past))

        book = FakeBook(path=folder, files=[settled, fresh])
        ready, pending = partition_settled([book], min_age=120, settle_seconds=0)

        self.assertEqual(ready, [])
        self.assertIn("02.mp3", pending[0][1])

    def test_a_missing_file_is_reported_not_raised(self) -> None:
        book = FakeBook(path=self.root / "nope.m4b", files=[self.root / "nope.m4b"])
        ready, pending = partition_settled([book], min_age=0, settle_seconds=0)
        self.assertEqual(ready, [])
        self.assertEqual(len(pending), 1)

    def test_mixed_batch_is_partitioned(self) -> None:
        old = self.make("old.m4b", age_seconds=3600)
        new = self.make("new.m4b")
        ready, pending = partition_settled([old, new], min_age=120, settle_seconds=0)
        self.assertEqual(ready, [old])
        self.assertEqual([b for b, _ in pending], [new])

    def test_one_wait_covers_the_whole_batch(self) -> None:
        books = [self.make(f"b{i}.m4b", age_seconds=3600) for i in range(5)]
        calls = []
        original_sleep = time.sleep
        time.sleep = lambda seconds: calls.append(seconds)
        try:
            partition_settled(books, min_age=0, settle_seconds=0.01)
        finally:
            time.sleep = original_sleep
        self.assertEqual(len(calls), 1)


class AgeFormatting(unittest.TestCase):
    def test_readable_at_each_scale(self) -> None:
        self.assertEqual(describe_age(45), "45s")
        self.assertEqual(describe_age(300), "5m")
        self.assertEqual(describe_age(7200), "2.0h")


if __name__ == "__main__":
    unittest.main()
