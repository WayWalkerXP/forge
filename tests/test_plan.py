"""The planner's own logic: chapters, track ordering, and warnings."""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg, write_config

from forge import config as config_mod
from forge import disposition as d, plan as plan_mod, scan, tags
from forge.disposition import Disposition as D
from forge.plan import PlanStatus


@requires_ffmpeg
class FolderChapters(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))

    def build(self, names, *, titles=None, tracks=None, disposition=D.DOWNCODE):
        folder = self.cfg.source_dir / "A Book"
        files = []
        for index, name in enumerate(names):
            path = folder / name
            copy_mp3(path, seconds=1)
            meta = {"author": "An Author", "album": "A Book"}
            if titles is not None:
                meta["title"] = titles[index]
            if tracks is not None and tracks[index] is not None:
                meta["track"] = tracks[index]
            tags.write_metadata(path, meta)
            files.append(path)
        d.write_folder(files, disposition)
        book = [b for b in scan.scan(self.cfg.source_dir).books if b.is_folder][0]
        return plan_mod.plan(book, self.cfg, codec="aac"), files

    def test_one_chapter_per_track(self) -> None:
        item, files = self.build(
            ["01.mp3", "02.mp3", "03.mp3"], tracks=[1, 2, 3], titles=["One", "Two", "Three"]
        )
        self.assertIs(item.status, PlanStatus.PLANNED)
        self.assertEqual(len(item.chapters), 3)
        self.assertEqual([c.title for c in item.chapters], ["One", "Two", "Three"])

    def test_chapter_start_times_are_cumulative(self) -> None:
        item, files = self.build(["01.mp3", "02.mp3", "03.mp3"], tracks=[1, 2, 3])
        starts = [c.start_seconds for c in item.chapters]
        self.assertEqual(starts[0], 0.0)
        for previous, current in zip(item.chapters, item.chapters[1:]):
            self.assertAlmostEqual(current.start_seconds, previous.end_seconds, places=5)

    def test_total_duration_is_the_sum_of_the_tracks(self) -> None:
        item, files = self.build(["01.mp3", "02.mp3"], tracks=[1, 2])
        self.assertAlmostEqual(
            item.total_duration, sum(c.duration_seconds for c in item.chapters), places=5
        )

    def test_chapter_titles_fall_back_to_the_filename(self) -> None:
        item, files = self.build(["First Part.mp3", "Second Part.mp3"], tracks=[1, 2])
        self.assertEqual([c.title for c in item.chapters], ["First Part", "Second Part"])

    def test_numbered_tracks_produce_no_ordering_warning(self) -> None:
        item, files = self.build(["01.mp3", "02.mp3", "03.mp3"], tracks=[1, 2, 3])
        self.assertFalse(any("order is uncertain" in w for w in item.warnings))

    def test_missing_track_numbers_warn_about_the_order(self) -> None:
        # Chapters could come out shuffled, so the planner says so rather than
        # quietly proceeding.
        item, files = self.build(["a.mp3", "b.mp3", "c.mp3"], tracks=[None, None, None])
        self.assertTrue(any("order is uncertain" in w for w in item.warnings))

    def test_duplicate_track_numbers_warn_about_the_order(self) -> None:
        item, files = self.build(["a.mp3", "b.mp3"], tracks=[1, 1])
        self.assertTrue(any("order is uncertain" in w for w in item.warnings))

    def test_an_ordering_warning_does_not_block_the_plan(self) -> None:
        item, files = self.build(["a.mp3", "b.mp3"], tracks=[None, None])
        self.assertIs(item.status, PlanStatus.PLANNED)


class FilenameTrackNumbers(unittest.TestCase):
    def test_common_numbering_styles(self) -> None:
        from pathlib import Path

        from forge.plan import _track_from_filename

        for name, expected in (
            ("01 - Title.mp3", 1),
            ("[03] Title.mp3", 3),
            ("(07) Title.mp3", 7),
            ("1-04 Title.mp3", 4),
            ("12_Title.mp3", 12),
            ("05.Title.mp3", 5),
        ):
            with self.subTest(name=name):
                self.assertEqual(_track_from_filename(Path(name)), expected)

    def test_unnumbered_names(self) -> None:
        from pathlib import Path

        from forge.plan import _track_from_filename

        for name in ("Introduction.mp3", "Chapter One.mp3"):
            with self.subTest(name=name):
                self.assertIsNone(_track_from_filename(Path(name)))


@requires_ffmpeg
class PlanShape(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))

    def single(self, *, disposition=D.DOWNCODE, metadata=None, **audio):
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, **(audio or {"seconds": 2}))
        if metadata:
            tags.write_metadata(path, metadata)
        d.write(path, disposition)
        book = scan.scan(self.cfg.source_dir).books[0]
        return plan_mod.plan(book, self.cfg, codec="aac")

    def test_paths_are_derived_from_the_tags(self) -> None:
        item = self.single(metadata={"author": "An Author", "album": "A Title"})
        self.assertEqual(item.output_path.name, "An Author - A Title.m4b")
        self.assertEqual(item.temp_path.name, "An Author - A Title.tmp.m4b")
        self.assertEqual(item.output_path.parent, self.cfg.output_dir)
        self.assertEqual(item.archive_path.parent, self.cfg.archive_dir)

    def test_as_is_has_no_archive_path(self) -> None:
        # The file is the product, so there is no original to file away.
        item = self.single(
            disposition=D.AS_IS, metadata={"author": "A", "album": "B"}
        )
        self.assertIsNone(item.archive_path)

    def test_a_codec_is_only_recorded_when_re_encoding(self) -> None:
        for disposition, expected in ((D.DOWNCODE, "aac"), (D.REMUX, None), (D.AS_IS, None)):
            with self.subTest(disposition=disposition):
                item = self.single(
                    disposition=disposition, metadata={"author": "A", "album": "B"}
                )
                self.assertEqual(item.codec, expected)

    def test_output_size_is_estimated_from_the_target_rate(self) -> None:
        item = self.single(metadata={"author": "A", "album": "B"}, seconds=10)
        expected = item.total_duration * item.target.bitrate_kbps * 1000 / 8
        self.assertAlmostEqual(item.estimated_output_bytes, expected, delta=expected * 0.02)

    def test_a_plan_describes_itself(self) -> None:
        item = self.single(metadata={"author": "A", "album": "B"})
        self.assertIn("downcode", item.describe())
        self.assertIn("A - B.m4b", item.describe())

    def test_a_skipped_plan_describes_the_reason_once(self) -> None:
        item = self.single(disposition=D.HOLD, metadata={"author": "A", "album": "B"})
        description = item.describe()
        self.assertIn("skipped", description)
        self.assertEqual(description.count("declined"), 0)

    def test_a_stale_temporary_file_is_only_a_warning(self) -> None:
        item = self.single(metadata={"author": "A", "album": "B"})
        item.temp_path.parent.mkdir(parents=True, exist_ok=True)
        item.temp_path.write_bytes(b"leftover")

        again = plan_mod.plan(scan.scan(self.cfg.source_dir).books[0], self.cfg, codec="aac")
        self.assertIs(again.status, PlanStatus.PLANNED)
        self.assertTrue(any("stale temporary file" in w for w in again.warnings))

    def test_no_codec_warns_but_still_plans(self) -> None:
        path = self.cfg.source_dir / "book.mp3"
        copy_mp3(path, seconds=2)
        tags.write_metadata(path, {"author": "A", "album": "B"})
        d.write(path, D.DOWNCODE)

        item = plan_mod.plan(scan.scan(self.cfg.source_dir).books[0], self.cfg, codec=None)
        self.assertIs(item.status, PlanStatus.PLANNED)
        self.assertTrue(any("no encoder" in w for w in item.warnings))


if __name__ == "__main__":
    unittest.main()
