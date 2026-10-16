"""The change log and undo."""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin

from forge.history import History, strip_cover_data, summarise_value


class Recording(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.store = History(self.root / "history.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_group_records_each_field(self) -> None:
        group_id = self.store.record(
            "key",
            {"author": (None, "An Author"), "album": ("Old", "New")},
            file_path="/books/a.m4b",
            source="manual",
            description="initial tagging",
        )
        changes = self.store.changes_in_group(group_id)
        self.assertEqual({c.field for c in changes}, {"author", "album"})
        self.assertEqual({c.old_value for c in changes}, {None, "Old"})

    def test_an_empty_change_set_creates_nothing(self) -> None:
        self.assertIsNone(self.store.record("key", {}, file_path="/x", source="manual"))
        self.assertEqual(self.store.stats()["change_group"], 0)

    def test_history_is_newest_first(self) -> None:
        for index in range(3):
            self.store.record(
                "key", {"author": (str(index), str(index + 1))},
                file_path="/books/a.m4b", source="manual",
            )
        groups = self.store.groups_for_file("/books/a.m4b")
        self.assertEqual([g.id for g in groups], sorted([g.id for g in groups], reverse=True))

    def test_groups_are_scoped_to_their_file(self) -> None:
        self.store.record("k", {"author": (None, "A")}, file_path="/books/a.m4b", source="m")
        self.store.record("k", {"author": (None, "B")}, file_path="/books/b.m4b", source="m")
        self.assertEqual(len(self.store.groups_for_file("/books/a.m4b")), 1)

    def test_recent_groups_spans_every_book(self) -> None:
        # Undo takes a group id, and until this existed the only way to see an
        # id was to already know which file to ask about.
        first = self.store.record("k1", {"author": (None, "A")}, file_path="/books/a.m4b",
                                  source="manual")
        second = self.store.record("k2", {"author": (None, "B")}, file_path="/books/b.m4b",
                                   source="audible")

        groups = self.store.recent_groups()
        self.assertEqual([g.id for g in groups], [second, first])
        self.assertEqual([g.change_count for g in groups], [1, 1])

    def test_recent_groups_respects_the_limit(self) -> None:
        for index in range(5):
            self.store.record("k", {"author": (None, str(index))},
                              file_path=f"/books/{index}.m4b", source="manual")
        self.assertEqual(len(self.store.recent_groups(limit=2)), 2)

    def test_recent_groups_is_empty_on_a_fresh_database(self) -> None:
        self.assertEqual(self.store.recent_groups(), [])

    def test_the_group_id_is_in_its_description(self) -> None:
        # It is what the user has to type into `forge undo`.
        group_id = self.store.record("k", {"author": (None, "A")},
                                     file_path="/books/a.m4b", source="manual")
        self.assertIn(f"[{group_id}]", self.store.group(group_id).describe())

    def test_files_in_group_lists_every_track(self) -> None:
        group_id = self.store.record(
            "k", {"author": (None, "A")}, file_path="/books/book/01.m4b", source="manual"
        )
        same = self.store.record(
            "k", {"author": (None, "A")}, file_path="/books/book/02.m4b", source="manual",
            group_id=group_id,
        )
        self.assertEqual(same, group_id, "appending must not open a new group")
        self.assertEqual(
            self.store.files_in_group(group_id),
            ["/books/book/01.m4b", "/books/book/02.m4b"],
        )
        self.assertEqual(len(self.store.recent_groups()), 1)

    def test_appending_to_an_unknown_group_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.record(
                "k", {"author": (None, "A")}, file_path="/books/a.m4b", source="manual",
                group_id=999,
            )

    def test_restoring_a_folder_group_covers_every_track(self) -> None:
        group_id = self.store.record(
            "k", {"author": ("Old", "New")}, file_path="/books/book/01.m4b", source="manual"
        )
        self.store.record(
            "k", {"author": ("Old", "New")}, file_path="/books/book/02.m4b", source="manual",
            group_id=group_id,
        )
        plan = self.store.restore_values(group_id)
        self.assertEqual(sorted(plan), ["/books/book/01.m4b", "/books/book/02.m4b"])
        self.assertEqual(plan["/books/book/02.m4b"], {"author": "Old"})

    def test_files_in_group_is_empty_for_an_unknown_group(self) -> None:
        self.assertEqual(self.store.files_in_group(999), [])

    def test_restore_values_returns_the_previous_state(self) -> None:
        group_id = self.store.record(
            "key",
            {"author": ("Wrong", "Right"), "album": ("Bad", "Good")},
            file_path="/books/a.m4b", source="audible",
        )
        plan = self.store.restore_values(group_id)
        self.assertEqual(plan["/books/a.m4b"], {"author": "Wrong", "album": "Bad"})

    def test_restore_can_be_limited_to_chosen_fields(self) -> None:
        group_id = self.store.record(
            "key",
            {"author": ("Wrong", "Right"), "album": ("Bad", "Good")},
            file_path="/books/a.m4b", source="audible",
        )
        plan = self.store.restore_values(group_id, fields={"author"})
        self.assertEqual(plan["/books/a.m4b"], {"author": "Wrong"})

    def test_a_cover_change_is_recorded_but_not_restorable(self) -> None:
        group_id = self.store.record(
            "key", {"cover_path": (None, "/tmp/cover.png")},
            file_path="/books/a.m4b", source="manual",
        )
        changes = self.store.changes_in_group(group_id)
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0].restorable)
        self.assertEqual(self.store.restore_values(group_id), {})

    def test_unknown_group_is_none(self) -> None:
        self.assertIsNone(self.store.group(999))


class Snapshots(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.store = History(self.root / "history.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_snapshot_round_trips(self) -> None:
        self.store.snapshot("key", "/books/a.m4b", {"author": "A", "genres": ["X", "Y"]})
        self.assertEqual(
            self.store.latest_snapshot("key"), {"author": "A", "genres": ["X", "Y"]}
        )

    def test_the_latest_snapshot_wins(self) -> None:
        self.store.snapshot("key", "/books/a.m4b", {"author": "First"})
        self.store.snapshot("key", "/books/a.m4b", {"author": "Second"})
        self.assertEqual(self.store.latest_snapshot("key")["author"], "Second")

    def test_image_bytes_never_reach_the_database(self) -> None:
        # A library's worth of embedded JPEGs in a SQLite file is a problem
        # discovered far too late.
        cover = b"\x89PNG\r\n\x1a\n" + b"\xAB" * 400_000
        self.store.snapshot("key", "/books/a.m4b", {"author": "A", "cover_data": cover})

        stored = self.store.latest_snapshot("key")
        self.assertIsInstance(stored["cover_data"], str)
        self.assertIn("sha256=", stored["cover_data"])
        self.assertLess(self.store.size_bytes(), 100_000)


class Housekeeping(TempCaseMixin):
    def test_prune_removes_everything_older_than_the_cutoff(self) -> None:
        store = History(self.root / "history.sqlite3")
        self.addCleanup(store.close)
        store.record("k", {"author": (None, "A")}, file_path="/x", source="m")
        store.snapshot("k", "/x", {"author": "A"})

        counts = store.prune(days_to_keep=0)

        self.assertEqual(counts["groups"], 1)
        self.assertEqual(counts["snapshots"], 1)
        self.assertEqual(store.stats()["change"], 0)

    def test_prune_keeps_recent_records(self) -> None:
        store = History(self.root / "history.sqlite3")
        self.addCleanup(store.close)
        store.record("k", {"author": (None, "A")}, file_path="/x", source="m")

        store.prune(days_to_keep=30)
        self.assertEqual(store.stats()["change_group"], 1)

    def test_the_database_survives_being_reopened(self) -> None:
        path = self.root / "history.sqlite3"
        with History(path) as store:
            group_id = store.record("k", {"author": (None, "A")}, file_path="/x", source="m")
        with History(path) as store:
            self.assertIsNotNone(store.group(group_id))


class CoverSummarising(unittest.TestCase):
    def test_bytes_become_a_hash_and_a_size(self) -> None:
        summary = summarise_value("cover", b"\x89PNG" + b"\x00" * 1000)
        self.assertIn("1004 bytes", summary)
        self.assertIn("sha256=", summary)

    def test_a_data_uri_is_summarised(self) -> None:
        self.assertIn("sha256=", summarise_value("cover", "data:image/png;base64," + "A" * 600))

    def test_ordinary_text_is_untouched(self) -> None:
        self.assertEqual(summarise_value("author", "An Author"), "An Author")

    def test_lists_are_joined(self) -> None:
        self.assertEqual(summarise_value("genres", ["A", "B"]), "A, B")

    def test_none_stays_none(self) -> None:
        self.assertIsNone(summarise_value("author", None))

    def test_stripping_recurses(self) -> None:
        cleaned = strip_cover_data(
            {"a": {"cover_data": b"\x89PNG" + b"\x00" * 100}, "b": ["x", "y"]}
        )
        self.assertIsInstance(cleaned["a"]["cover_data"], str)
        self.assertEqual(cleaned["b"], ["x", "y"])


if __name__ == "__main__":
    unittest.main()


class FollowingAFileThatMoved(TempCaseMixin, unittest.TestCase):
    """`forge shelve` edits a book's tags and then moves it."""

    def setUp(self) -> None:
        super().setUp()
        self.store = History(self.root / "history.sqlite3")
        self.addCleanup(self.store.close)
        self.old = self.root / "out" / "book.m4b"
        self.new = self.root / "library" / "A" / "B" / "B.m4b"

    def test_changes_point_at_the_new_location(self) -> None:
        group = self.store.record(
            "key", {"author": ("A - Editor", "A")}, file_path=self.old, source="shelve"
        )
        moved = self.store.relocate(self.old, self.new, group_id=group)
        self.assertEqual(moved, 1)
        self.assertEqual(list(self.store.restore_values(group)), [str(self.new)])

    def test_an_undo_after_the_move_writes_to_the_file_that_is_there(self) -> None:
        group = self.store.record(
            "key", {"track": (3, None)}, file_path=self.old, source="shelve"
        )
        self.store.relocate(self.old, self.new, group_id=group)
        plan = self.store.restore_values(group)
        self.assertEqual(plan[str(self.new)], {"track": "3"})

    def test_another_group_is_left_alone(self) -> None:
        mine = self.store.record("key", {"author": ("x", "y")}, file_path=self.old, source="shelve")
        theirs = self.store.record(
            "key", {"narrator": ("p", "q")}, file_path=self.old, source="manual"
        )
        self.store.relocate(self.old, self.new, group_id=mine)
        self.assertEqual(list(self.store.restore_values(theirs)), [str(self.old)])

    def test_a_path_that_was_never_recorded_changes_nothing(self) -> None:
        self.assertEqual(self.store.relocate(self.old, self.new), 0)
