"""Scan, plan and execute, end to end.

These are the tests that matter most: they exercise the ordering that keeps the
source file safe, and the loop guard that stops an output being converted twice.
"""

from __future__ import annotations

import unittest

from tests import helpers
from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg, write_config

from forge import config as config_mod
from forge import convert, disposition as d, ffmpeg, plan as plan_mod, probe, scan, tags
from forge.convert import Outcome
from forge.disposition import Disposition as D
from forge.plan import PlanStatus


@requires_ffmpeg
class PipelineCase(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config_mod.load(write_config(self.root))
        self.codec = ffmpeg.select_codec(self.cfg.preferred_codec, self.cfg.fallback_codec)

    def add_book(self, name, *, disposition=None, metadata=None, mp3=False, **audio):
        path = self.cfg.source_dir / name
        if mp3:
            copy_mp3(path, **audio)
        else:
            helpers.copy_audio(path, **audio)
        if metadata:
            tags.write_metadata(path, metadata)
        if disposition is not None:
            d.write(path, disposition)
        return path

    def run_all(self, *, dry_run=False):
        books = scan.scan(self.cfg.source_dir).books
        plans = plan_mod.plan_many(books, self.cfg, codec=self.codec)
        return [convert.execute(p, self.cfg, dry_run=dry_run) for p in plans]

    def run_one(self, path, *, dry_run=False):
        book = scan._build_book(path, [path], is_folder=False, with_probe=True)
        item = plan_mod.plan(book, self.cfg, codec=self.codec)
        return item, convert.execute(item, self.cfg, dry_run=dry_run)


class UntriagedIsNeverTouched(PipelineCase):
    def test_nothing_happens_without_a_disposition(self) -> None:
        path = self.add_book("book.m4b", metadata={"author": "A", "album": "B"}, seconds=2)
        before = path.read_bytes()

        results = self.run_all()

        self.assertEqual([r.outcome for r in results], [Outcome.SKIPPED])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])

    def test_hold_is_skipped(self) -> None:
        self.add_book(
            "book.m4b", disposition=D.HOLD, metadata={"author": "A", "album": "B"}, seconds=2
        )
        self.assertEqual([r.outcome for r in self.run_all()], [Outcome.SKIPPED])

    def test_an_unknown_disposition_value_is_skipped(self) -> None:
        path = self.add_book("book.m4b", metadata={"author": "A", "album": "B"}, seconds=2)
        audio = tags.open_audio(path)
        tags.write_freeform(audio, d.TAG_NAME, "some-future-action")
        audio.save()

        self.assertEqual([r.outcome for r in self.run_all()], [Outcome.SKIPPED])
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])


class DryRun(PipelineCase):
    def test_dry_run_changes_nothing(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE, metadata={"author": "A", "album": "B"},
            mp3=True, seconds=2,
        )
        before = path.read_bytes()

        results = self.run_all(dry_run=True)

        self.assertEqual(results[0].outcome, Outcome.SKIPPED)
        self.assertIn("dry run", results[0].message)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])
        self.assertEqual(list(self.cfg.archive_dir.iterdir()), [])

    def test_dry_run_does_not_retag_an_invalid_book(self) -> None:
        path = self.add_book("book.mp3", disposition=D.DOWNCODE, mp3=True, seconds=2)
        item, result = self.run_one(path, dry_run=True)
        self.assertEqual(result.outcome, Outcome.NEEDS_ATTENTION)
        self.assertFalse(result.retagged)
        self.assertIs(d.read(path), D.DOWNCODE)


class Downcode(PipelineCase):
    def test_stereo_mp3_becomes_mono_m4b(self) -> None:
        path = self.add_book(
            "book.mp3",
            disposition=D.DOWNCODE,
            metadata={"author": "Bob Kowalski", "album": "A History of Science"},
            mp3=True, seconds=3,
        )
        item, result = self.run_one(path)

        self.assertEqual(result.outcome, Outcome.CONVERTED)
        output = self.cfg.output_dir / "Bob Kowalski - A History of Science.m4b"
        self.assertTrue(output.exists())

        info = probe.probe(output)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.normalized_bitrate_kbps, 64)
        self.assertIn("mp4", info.format_name)

    def test_the_original_is_archived_not_deleted(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        _, result = self.run_one(path)

        self.assertFalse(path.exists())
        self.assertIsNotNone(result.archived_path)
        self.assertTrue(result.archived_path.exists())

    def test_duration_is_preserved(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=4,
        )
        source_duration = probe.probe(path).duration_seconds
        _, result = self.run_one(path)
        self.assertAlmostEqual(
            probe.probe(result.output_path).duration_seconds, source_duration, delta=1.0
        )

    def test_chapters_are_preserved(self) -> None:
        path = self.add_book(
            "book.m4b", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, seconds=6, chapters=5,
        )
        self.assertEqual(probe.probe(path).chapter_count, 5)
        _, result = self.run_one(path)
        self.assertEqual(result.outcome, Outcome.CONVERTED)
        self.assertEqual(probe.probe(result.output_path).chapter_count, 5)

    def test_cover_art_is_carried_over(self) -> None:
        path = self.add_book(
            "book.m4b", metadata={"author": "A", "album": "B"}, seconds=2,
        )
        png = self.root / "cover.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
        tags.write_metadata(path, {"cover_path": str(png)})
        d.write(path, D.DOWNCODE)

        _, result = self.run_one(path)
        self.assertTrue(tags.read_metadata(result.output_path).has_cover)


#: Everything a triaged book carries that ffmpeg's mp4 muxer will not write for
#: itself.  Each of these lived in a ``----:com.apple.iTunes:`` atom and used to
#: vanish during conversion even though ``-map_metadata`` was asked to copy it.
CUSTOM_TAGS = {
    "author": "Jonathan Brooks",
    "album": "The Affinity Collector 3",
    "subtitle": "A Fantasy Isekai LitRPG Series",
    "narrator": "Miles Meili",
    "series": "Octaeteris Cycle",
    "series_sequence": "3",
    "asin": "B0HM2FPY5F",
    "publisher": "Jonathan Brooks",
    "copyright": "\u00a92026 Jonathan Brooks",
    "language": "english",
    "explicit": False,
    "genres": ["Science Fiction & Fantasy", "Fantasy"],
}


class TagsSurviveConversion(PipelineCase):
    """A converted book must come out tagged the way it went in.

    ffmpeg writes only the atoms its mp4 muxer knows, so the custom ones are
    re-applied with mutagen after validation.  Without that, a book triaged in
    forge reached the library with no series, no series-part and no ASIN.
    """

    def converted(self, disposition=D.DOWNCODE, **audio):
        path = self.add_book(
            "book.m4b", disposition=disposition, metadata=dict(CUSTOM_TAGS),
            seconds=audio.pop("seconds", 2), **audio,
        )
        _, result = self.run_one(path)
        self.assertEqual(result.outcome, Outcome.CONVERTED, result.message)
        self.assertEqual(result.warnings, [])
        return result.output_path

    def test_every_custom_field_is_carried_over(self) -> None:
        meta = tags.read_metadata(self.converted())
        for name, expected in CUSTOM_TAGS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(meta, name), expected)

    def test_audiobookshelf_reads_the_series_from_the_output(self) -> None:
        # Audiobookshelf's prober looks for series / show / mvnm and
        # series-part / episode_id / mvin / part - never SERIES_SEQUENCE - so
        # the hyphenated mirror is the one that has to survive.
        output = self.converted()
        audio = tags.open_audio(output)
        self.assertEqual(tags.read_freeform(audio, "SERIES"), "Octaeteris Cycle")
        self.assertEqual(tags.read_freeform(audio, "series-part"), "3")
        self.assertEqual(tags.read_freeform(audio, "SERIES_SEQUENCE"), "3")

    def test_tags_survive_a_remux(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.REMUX, metadata=dict(CUSTOM_TAGS), mp3=True, seconds=3,
        )
        _, result = self.run_one(path)
        self.assertEqual(result.outcome, Outcome.CONVERTED, result.message)
        meta = tags.read_metadata(result.output_path)
        self.assertEqual(meta.series, "Octaeteris Cycle")
        self.assertEqual(meta.series_sequence, "3")
        self.assertEqual(meta.asin, "B0HM2FPY5F")

    def test_the_disposition_is_still_done_after_the_tags_are_copied(self) -> None:
        # carryover must not resurrect the source's disposition on the output.
        self.assertIs(d.read(self.converted()), D.DONE)

    def test_a_folder_book_does_not_inherit_track_one_s_numbering(self) -> None:
        folder = self.cfg.source_dir / "Folder Book"
        folder.mkdir()
        for index in range(2):
            track = helpers.copy_audio(folder / f"{index + 1:02d}.m4b", seconds=2)
            tags.write_metadata(
                track,
                dict(CUSTOM_TAGS, track=index + 1, disc=1, title=f"Track {index + 1}"),
            )
            d.write(track, D.DOWNCODE)

        book = scan.scan(self.cfg.source_dir).books[0]
        self.assertTrue(book.is_folder)
        item = plan_mod.plan(book, self.cfg, codec=self.codec)
        result = convert.execute(item, self.cfg)

        self.assertEqual(result.outcome, Outcome.CONVERTED, result.message)
        meta = tags.read_metadata(result.output_path)
        self.assertEqual(meta.series, "Octaeteris Cycle")
        self.assertIsNone(meta.track)
        # Not "Track 1": the combined file is the book, not its first track.
        self.assertEqual(meta.title, CUSTOM_TAGS["album"])

class DramaAndRemux(PipelineCase):

    def test_drama_keeps_stereo(self) -> None:
        path = self.add_book(
            "drama.mp3", disposition=D.DRAMA,
            metadata={"author": "A", "album": "DCC GraphicAudio"}, mp3=True, seconds=3,
        )
        _, result = self.run_one(path)
        self.assertEqual(result.outcome, Outcome.CONVERTED)
        self.assertEqual(probe.probe(result.output_path).channels, 2)

    def test_remux_preserves_the_audio_stream(self) -> None:
        path = self.add_book(
            "remux.mp3", disposition=D.REMUX,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=3,
        )
        source = probe.probe(path)
        _, result = self.run_one(path)

        self.assertEqual(result.outcome, Outcome.CONVERTED)
        output = probe.probe(result.output_path)
        self.assertEqual(output.codec, source.codec)
        self.assertEqual(output.channels, source.channels)
        self.assertAlmostEqual(output.duration_seconds, source.duration_seconds, delta=1.0)
        self.assertIn("mp4", output.format_name)


class AsIs(PipelineCase):
    def test_file_is_relocated_and_verified(self) -> None:
        path = self.add_book(
            "fine.m4b", disposition=D.AS_IS,
            metadata={"author": "J.R. Mathews", "album": "Portal to Nova Roma"}, seconds=2,
        )
        original = path.read_bytes()
        _, result = self.run_one(path)

        self.assertEqual(result.outcome, Outcome.RELOCATED)
        self.assertFalse(path.exists())
        self.assertEqual(result.output_path.read_bytes()[:64], original[:64])

    def test_nothing_is_archived_because_the_file_is_the_product(self) -> None:
        path = self.add_book(
            "fine.m4b", disposition=D.AS_IS,
            metadata={"author": "A", "album": "B"}, seconds=2,
        )
        _, result = self.run_one(path)
        self.assertIsNone(result.archived_path)
        self.assertEqual(list(self.cfg.archive_dir.iterdir()), [])

    def test_as_is_is_refused_for_a_folder_of_tracks(self) -> None:
        folder = self.cfg.source_dir / "A Folder"
        files = [folder / f"0{i} - Track.mp3" for i in (1, 2)]
        for path in files:
            copy_mp3(path, seconds=1)
            tags.write_metadata(path, {"author": "A", "album": "B"})
        d.write_folder(files, D.AS_IS)

        results = self.run_all()
        self.assertEqual(results[0].outcome, Outcome.NEEDS_ATTENTION)
        self.assertIn("folder of separate tracks", results[0].message)
        self.assertTrue(all(f.exists() for f in files))


class FolderBooks(PipelineCase):
    def make_folder(self, disposition=D.DOWNCODE, tracks=3):
        folder = self.cfg.source_dir / "Odd Thomas"
        files = [folder / f"0{i} - Chapter {i}.mp3" for i in range(1, tracks + 1)]
        for index, path in enumerate(files, 1):
            copy_mp3(path, seconds=1)
            tags.write_metadata(
                path,
                {"author": "Dean Koontz", "album": "Odd Thomas",
                 "title": f"Chapter {index}", "track": index},
            )
        if disposition is not None:
            d.write_folder(files, disposition)
        return folder, files

    def test_tracks_are_combined_with_one_chapter_each(self) -> None:
        folder, files = self.make_folder(tracks=3)
        results = self.run_all()

        self.assertEqual(results[0].outcome, Outcome.CONVERTED)
        output = self.cfg.output_dir / "Dean Koontz - Odd Thomas.m4b"
        self.assertTrue(output.exists())
        self.assertEqual(probe.probe(output).chapter_count, 3)

    def test_duration_is_the_sum_of_the_tracks(self) -> None:
        folder, files = self.make_folder(tracks=3)
        total = sum(probe.probe(f).duration_seconds for f in files)
        results = self.run_all()
        self.assertAlmostEqual(
            probe.probe(results[0].output_path).duration_seconds, total, delta=1.5
        )

    def test_the_whole_directory_is_archived(self) -> None:
        # Archiving only the first track would strand the rest in the source.
        folder, files = self.make_folder(tracks=3)
        self.run_all()

        self.assertFalse(any(self.cfg.source_dir.rglob("*.mp3")))
        archived = sorted(p.name for p in (self.cfg.archive_dir / "Odd Thomas").iterdir())
        self.assertEqual(len(archived), 3)

    def test_a_conflicted_folder_is_never_converted(self) -> None:
        folder, files = self.make_folder(tracks=3)
        d.write(files[1], D.AS_IS)

        results = self.run_all()
        self.assertEqual(results[0].outcome, Outcome.NEEDS_ATTENTION)
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])
        self.assertTrue(all(f.exists() for f in files))


class InvalidBooksBecomeNeedsAttention(PipelineCase):
    def test_missing_author_and_album(self) -> None:
        path = self.add_book("untagged.mp3", disposition=D.DOWNCODE, mp3=True, seconds=2)
        item, result = self.run_one(path)

        self.assertIs(item.status, PlanStatus.INVALID)
        self.assertEqual(result.outcome, Outcome.NEEDS_ATTENTION)
        self.assertTrue(result.retagged)
        self.assertIs(d.read(path), D.NEEDS_ATTENTION)
        self.assertTrue(path.exists())

    def test_a_source_below_the_quality_floor(self) -> None:
        path = self.add_book(
            "lowrate.m4b", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"},
            seconds=3, channels=1, bitrate_kbps=16,
        )
        _, result = self.run_one(path)

        self.assertEqual(result.outcome, Outcome.NEEDS_ATTENTION)
        self.assertIn("below the 32k floor", result.message)
        self.assertIs(d.read(path), D.NEEDS_ATTENTION)

    def test_retagging_can_be_suppressed(self) -> None:
        path = self.add_book("untagged.mp3", disposition=D.DOWNCODE, mp3=True, seconds=2)
        book = scan._build_book(path, [path], is_folder=False, with_probe=True)
        item = plan_mod.plan(book, self.cfg, codec=self.codec)
        result = convert.execute(item, self.cfg, mark_needs_attention=False)

        self.assertEqual(result.outcome, Outcome.NEEDS_ATTENTION)
        self.assertFalse(result.retagged)
        self.assertIs(d.read(path), D.DOWNCODE)

    def test_a_needs_attention_book_is_not_retried(self) -> None:
        path = self.add_book("untagged.mp3", disposition=D.DOWNCODE, mp3=True, seconds=2)
        self.run_one(path)
        self.assertIs(d.read(path), D.NEEDS_ATTENTION)

        results = self.run_all()
        self.assertEqual([r.outcome for r in results], [Outcome.SKIPPED])


class LoopGuard(PipelineCase):
    """An output must never be converted a second time."""

    def test_outputs_are_stamped_done(self) -> None:
        for name, disposition in (
            ("a.mp3", D.DOWNCODE), ("b.mp3", D.DRAMA), ("c.mp3", D.REMUX), ("d.m4b", D.AS_IS),
        ):
            path = self.add_book(
                name, disposition=disposition,
                metadata={"author": "Author", "album": f"Book {name}"},
                mp3=name.endswith(".mp3"), seconds=2,
            )
            _, result = self.run_one(path)
            with self.subTest(disposition=disposition):
                self.assertTrue(result.ok, result.message)
                self.assertIs(d.read(result.output_path), D.DONE)

    def test_the_input_disposition_does_not_leak_into_the_output(self) -> None:
        # -map_metadata copies the source's tags, so without the stamp the
        # output would still say "downcode".
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        _, result = self.run_one(path)
        self.assertIsNot(d.read(result.output_path), D.DOWNCODE)
        self.assertIs(d.read(result.output_path), D.DONE)

    def test_an_output_rescanned_as_a_source_is_ignored(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        _, result = self.run_one(path)

        import shutil
        second = self.root / "second"
        cfg2 = config_mod.load(write_config(second))
        shutil.copy2(result.output_path, cfg2.source_dir / result.output_path.name)

        books = scan.scan(cfg2.source_dir).books
        self.assertEqual(len(books), 1)
        self.assertIs(books[0].disposition, D.DONE)
        self.assertFalse(books[0].disposition.is_actionable)

        plans = plan_mod.plan_many(books, cfg2, codec=self.codec)
        outcomes = [convert.execute(p, cfg2).outcome for p in plans]
        self.assertEqual(outcomes, [Outcome.SKIPPED])
        self.assertEqual(list(cfg2.output_dir.iterdir()), [])


class NoOverwrite(PipelineCase):
    def test_an_existing_output_blocks_the_plan(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        existing = self.cfg.output_dir / "A - B.m4b"
        existing.write_bytes(b"precious")

        item, result = self.run_one(path)

        self.assertIs(item.status, PlanStatus.INVALID)
        self.assertIn("already exists", result.message)
        self.assertEqual(existing.read_bytes(), b"precious")
        self.assertTrue(path.exists())

    def test_two_books_producing_one_name_are_both_stopped(self) -> None:
        for name in ("one.mp3", "two.mp3"):
            self.add_book(
                name, disposition=D.DOWNCODE,
                metadata={"author": "Same", "album": "Name"}, mp3=True, seconds=2,
            )

        books = scan.scan(self.cfg.source_dir).books
        plans = plan_mod.plan_many(books, self.cfg, codec=self.codec)

        self.assertTrue(all(p.status is PlanStatus.INVALID for p in plans))
        for item in plans:
            self.assertTrue(any("would produce" in e for e in item.errors))


class FailuresLeaveTheSourceAlone(PipelineCase):
    def test_a_failed_conversion_keeps_the_source_and_cleans_up(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        book = scan._build_book(path, [path], is_folder=False, with_probe=True)
        item = plan_mod.plan(book, self.cfg, codec=self.codec)

        # Make ffmpeg fail by pointing the plan at a codec that does not exist.
        from dataclasses import replace
        broken = replace(item, codec="definitely_not_a_codec")
        result = convert.execute(broken, self.cfg)

        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertTrue(path.exists())
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])
        self.assertEqual(list(self.cfg.archive_dir.iterdir()), [])

    def test_a_failed_validation_discards_the_output(self) -> None:
        path = self.add_book(
            "book.mp3", disposition=D.DOWNCODE,
            metadata={"author": "A", "album": "B"}, mp3=True, seconds=2,
        )
        book = scan._build_book(path, [path], is_folder=False, with_probe=True)
        item = plan_mod.plan(book, self.cfg, codec=self.codec)

        # Claim the source is far longer than it is, so the duration check fails.
        from dataclasses import replace
        lying = replace(item, total_duration=99999.0)
        result = convert.execute(lying, self.cfg)

        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertIn("validation failed", result.message)
        self.assertTrue(path.exists())
        self.assertEqual(list(self.cfg.output_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
