"""Command-line parsing, and the global options that pick the library.

`-c` deciding which config.ini is read makes it the most consequential flag
forge has: get it wrong and a run operates on the wrong source directory
entirely.  It is worth its own tests.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.helpers import TempCaseMixin, requires_ffmpeg, write_config

from forge import config as config_mod
from forge import tags
from forge.cli import GLOBAL_DEFAULTS, build_parser, main
from forge.cli import _apply_global_defaults


def parse(*argv: str):
    return _apply_global_defaults(build_parser().parse_args(list(argv)))


class GlobalOptions(unittest.TestCase):
    """Each global option must work on either side of the subcommand.

    argparse shares these through `parents`, and the subparser parses second:
    with ordinary defaults it overwrote whatever the main parser had already
    stored, so `forge -c other.ini run` silently used the default config and
    ran against the wrong library.
    """

    def test_config_before_the_subcommand(self) -> None:
        self.assertEqual(parse("-c", "/tmp/a.ini", "run").config, Path("/tmp/a.ini"))

    def test_config_after_the_subcommand(self) -> None:
        self.assertEqual(parse("run", "-c", "/tmp/a.ini").config, Path("/tmp/a.ini"))

    def test_config_survives_every_subcommand(self) -> None:
        for command, extra in (
            ("triage", []),
            ("run", []),
            ("shelve", []),
            ("list", []),
            ("lookup", ["--asin", "B000000000"]),
            ("history", []),
            ("undo", ["1"]),
            ("disposition", ["/tmp/book.m4b"]),
            ("tags", ["show", "/tmp/book.m4b"]),
        ):
            with self.subTest(command=command):
                args = parse("-c", "/tmp/a.ini", command, *extra)
                self.assertEqual(args.config, Path("/tmp/a.ini"))

    def test_flags_before_the_subcommand(self) -> None:
        args = parse("-v", "-v", "--plain", "--no-color", "run")
        self.assertEqual((args.verbose, args.plain, args.no_color), (2, True, True))

    def test_flags_after_the_subcommand(self) -> None:
        args = parse("run", "-v", "--plain", "--no-color")
        self.assertEqual((args.verbose, args.plain, args.no_color), (1, True, True))

    def test_defaults_when_nothing_is_given(self) -> None:
        args = parse("run")
        for name, default in GLOBAL_DEFAULTS.items():
            with self.subTest(option=name):
                self.assertEqual(getattr(args, name), default)


@requires_ffmpeg
class TheConfigFlagChoosesTheLibrary(TempCaseMixin):
    """The behavioural half: the named config is the one actually loaded."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg_path = write_config(self.root)
        cfg = config_mod.load(self.cfg_path)
        helpers.copy_audio(cfg.source_dir / "only-here.m4b", seconds=1)

        # If the flag were ignored, forge would fall back to the real
        # ~/.config/forge/config.ini and list someone's actual library.  With
        # no default locations to search, that fallback fails loudly instead.
        patch = mock.patch.object(config_mod, "DEFAULT_CONFIG_PATHS", ())
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = main(list(argv))
        return status, buffer.getvalue()

    def test_list_reads_the_named_config(self) -> None:
        status, output = self.run_cli("-c", str(self.cfg_path), "list", "--plain")
        self.assertEqual(status, 0, output)
        self.assertIn("only-here", output)

    def test_the_flag_works_after_the_subcommand_too(self) -> None:
        status, output = self.run_cli("list", "-c", str(self.cfg_path), "--plain")
        self.assertEqual(status, 0, output)
        self.assertIn("only-here", output)

    def test_without_the_flag_there_is_no_library_to_read(self) -> None:
        # Proves the test above is not passing by accident: with no config
        # found, the command cannot have listed anything.
        self.assertEqual(self.run_cli("list", "--plain")[0], 3)


@requires_ffmpeg
class FindingAChangeGroupId(TempCaseMixin):
    """`forge undo` takes a group id, so something has to show the ids.

    `forge history` with no arguments used to print database statistics, which
    left no way to discover an id without already knowing which file to ask
    about - and `forge undo` with no argument just said the argument was
    required.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg_path = write_config(self.root)
        self.cfg = config_mod.load(self.cfg_path)
        self.book = self.cfg.source_dir / "book.m4b"
        helpers.copy_audio(self.book, seconds=1)
        tags.write_metadata(self.book, {"author": "An Author", "album": "A Book"})

        patch = mock.patch.object(config_mod, "DEFAULT_CONFIG_PATHS", ())
        patch.start()
        self.addCleanup(patch.stop)

    def record_a_change(self, **changes):
        from forge.history import History
        from forge.scan import book_key

        before = tags.read_metadata(self.book)
        diff = tags.diff_metadata(before, changes)
        tags.write_metadata(self.book, changes)
        with History(self.cfg.db_path) as store:
            return store.record(
                book_key(self.book), diff, file_path=self.book,
                source="audible", description="ASIN B0TESTTEST",
            )

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = main(["-c", str(self.cfg_path), *argv])
        return status, buffer.getvalue()

    def test_history_lists_the_group_ids(self) -> None:
        group_id = self.record_a_change(narrator="A Narrator")
        status, output = self.run_cli("history")

        self.assertEqual(status, 0, output)
        self.assertIn(f"[{group_id}]", output)
        self.assertIn("book.m4b", output)
        self.assertIn("forge undo GROUP_ID", output)

    def test_history_names_the_file_each_group_touched(self) -> None:
        self.record_a_change(narrator="A Narrator")
        self.assertIn("book.m4b", self.run_cli("history")[1])

    def test_an_empty_history_says_so_rather_than_nothing(self) -> None:
        status, output = self.run_cli("history")
        self.assertEqual(status, 0, output)
        self.assertIn("no recorded tag changes", output)

    def test_the_database_summary_is_still_available(self) -> None:
        status, output = self.run_cli("history", "--stats")
        self.assertEqual(status, 0, output)
        self.assertIn("snapshots", output)

    def test_undo_without_an_id_lists_them_instead_of_failing_obscurely(self) -> None:
        group_id = self.record_a_change(narrator="A Narrator")
        status, output = self.run_cli("undo")

        self.assertEqual(status, 2, output)
        self.assertIn(f"[{group_id}]", output)
        self.assertIn("change group id", output)

    def test_undo_without_an_id_changes_nothing(self) -> None:
        self.record_a_change(narrator="A Narrator")
        self.run_cli("undo")
        self.assertEqual(tags.read_metadata(self.book).narrator, "A Narrator")

    def test_a_listed_id_can_actually_be_undone(self) -> None:
        # The whole point of printing an id is that it works when typed back in.
        group_id = self.record_a_change(narrator="A Narrator")
        listed, output = self.run_cli("history")
        self.assertIn(f"[{group_id}]", output)

        status, output = self.run_cli("undo", str(group_id), "-n")
        self.assertEqual(status, 0, output)
        self.assertIn("narrator", output)
        # -n previews only.
        self.assertEqual(tags.read_metadata(self.book).narrator, "A Narrator")

        with mock.patch("builtins.input", return_value="y"):
            status, output = self.run_cli("undo", str(group_id))
        self.assertEqual(status, 0, output)
        self.assertIsNone(tags.read_metadata(self.book).narrator)

    def test_an_absent_old_value_reads_as_a_dash(self) -> None:
        # repr(None) is the string "None", which is indistinguishable from a
        # tag whose value really was "None".
        self.record_a_change(narrator="A Narrator")
        output = self.run_cli("history", str(self.book))[1]
        self.assertIn("—", output)
        self.assertNotIn("'None'", output)


if __name__ == "__main__":
    unittest.main()


@requires_ffmpeg
class ShelveFromTheCommandLine(TempCaseMixin):
    """`forge shelve` end to end, with Audiobookshelf faked."""

    def setUp(self) -> None:
        super().setUp()
        self.library = self.root / "library"
        self.library.mkdir()
        self.cfg_path = write_config(self.root, library_dir=self.library)
        self.cfg = config_mod.load(self.cfg_path)

        self.book = self.cfg.output_dir / "finished.m4b"
        helpers.copy_audio(self.book, seconds=1)
        tags.write_metadata(
            self.book,
            {"author": "Adrian Tchaikovsky", "album": "Ironclads", "published_year": 2017},
        )

        patch = mock.patch.object(config_mod, "DEFAULT_CONFIG_PATHS", ())
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = main(list(argv))
        return status, buffer.getvalue()

    def shelved(self) -> Path:
        return (
            self.library / "Adrian Tchaikovsky" / "Ironclads" / "Ironclads (2017).m4b"
        )

    def test_a_dry_run_names_the_destination_and_moves_nothing(self) -> None:
        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "-n", "--no-duplicate-check"
        )
        self.assertEqual(status, 0, output)
        self.assertIn("Ironclads (2017).m4b", output)
        self.assertTrue(self.book.is_file())
        self.assertFalse(self.shelved().exists())

    def test_a_real_run_files_the_book(self) -> None:
        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "--no-settle", "--no-duplicate-check"
        )
        self.assertEqual(status, 0, output)
        self.assertTrue(self.shelved().is_file(), output)
        self.assertFalse(self.book.exists())

    def test_the_target_can_be_overridden(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "--no-settle",
            "--no-duplicate-check", "--target", str(other),
        )
        self.assertEqual(status, 0, output)
        self.assertTrue((other / "Adrian Tchaikovsky").is_dir())
        self.assertFalse((self.library / "Adrian Tchaikovsky").exists())

    def test_without_a_library_it_says_what_to_configure(self) -> None:
        bare = write_config(self.root / "bare")
        status, output = self.run_cli("-c", str(bare), "shelve", "--plain")
        self.assertEqual(status, 3, output)

    def test_the_csv_matches_what_was_planned(self) -> None:
        import csv

        target = self.root / "plan.csv"
        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "-n",
            "--no-duplicate-check", "--csv", str(target),
        )
        self.assertEqual(status, 0, output)
        with target.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], str(self.book))
        self.assertEqual(rows[0]["target"], str(self.shelved()))
        self.assertEqual(rows[0]["action"], "ready")

    def test_one_named_book_shelves_only_that_book(self) -> None:
        other = self.cfg.output_dir / "second.m4b"
        helpers.copy_audio(other, seconds=1)
        tags.write_metadata(other, {"author": "B Author", "album": "Second"})

        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "--no-settle",
            "--no-duplicate-check", str(self.book),
        )
        self.assertEqual(status, 0, output)
        self.assertTrue(self.shelved().is_file())
        self.assertTrue(other.is_file(), "a book that was not named got shelved")

    def test_the_limit_is_honoured(self) -> None:
        other = self.cfg.output_dir / "aaa-first.m4b"
        helpers.copy_audio(other, seconds=1)
        tags.write_metadata(other, {"author": "B Author", "album": "Second"})

        status, output = self.run_cli(
            "-c", str(self.cfg_path), "shelve", "--plain", "--no-settle",
            "--no-duplicate-check", "--limit", "1",
        )
        self.assertEqual(status, 0, output)
        shelved = list(self.library.rglob("*.m4b"))
        self.assertEqual(len(shelved), 1, shelved)
