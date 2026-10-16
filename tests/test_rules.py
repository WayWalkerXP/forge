"""The encoding rules, which decide what every conversion produces."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin  # noqa: F401  (keeps sys.path set up)

from forge import rules
from forge.disposition import Disposition
from forge.probe import AudioInfo


def info(bitrate_kbps: int, channels: int, *, name: str = "Book.m4b", metadata=None) -> AudioInfo:
    duration = 3600.0
    return AudioInfo(
        path=Path(name),
        bitrate_bps=bitrate_kbps * 1000,
        channels=channels,
        codec="aac",
        duration_seconds=duration,
        chapter_count=0,
        size_bytes=int(duration * bitrate_kbps * 125),
        metadata=metadata or {},
    )


class DowncodeRules(unittest.TestCase):
    def test_mono_below_floor_is_declined(self) -> None:
        # Values here are raw rates that bucket to 24k or less.
        for kbps in (8, 16, 24, 27):
            with self.subTest(kbps=kbps):
                target = rules.decide(info(kbps, 1), Disposition.DOWNCODE)
                self.assertTrue(target.declined)
                self.assertIn("below the 32k floor", target.rationale)

    def test_floor_applies_to_the_bucketed_rate_not_the_raw_one(self) -> None:
        # Encoders undershoot their nominal rate, so a 31k file is really a 32k
        # file.  Declining it while accepting an exactly-32k file would make the
        # rules depend on encoder noise.
        self.assertTrue(rules.decide(info(27, 1), Disposition.DOWNCODE).declined)
        self.assertFalse(rules.decide(info(28, 1), Disposition.DOWNCODE).declined)
        self.assertFalse(rules.decide(info(31, 1), Disposition.DOWNCODE).declined)

    def test_stereo_below_floor_is_declined(self) -> None:
        for kbps in (32, 48, 56, 59):
            with self.subTest(kbps=kbps):
                target = rules.decide(info(kbps, 2), Disposition.DOWNCODE)
                self.assertTrue(target.declined)
                self.assertIn("below the 64k floor", target.rationale)

    def test_mono_at_or_above_64_becomes_64_mono(self) -> None:
        for kbps in (64, 96, 128, 256):
            with self.subTest(kbps=kbps):
                target = rules.decide(info(kbps, 1), Disposition.DOWNCODE)
                self.assertEqual((target.bitrate_kbps, target.channels), (64, 1))

    def test_low_mono_keeps_roughly_its_own_rate(self) -> None:
        # Never upscale: that spends space without recovering quality.
        for source_kbps, expected in ((32, 32), (40, 32), (48, 48), (56, 48)):
            with self.subTest(kbps=source_kbps):
                target = rules.decide(info(source_kbps, 1), Disposition.DOWNCODE)
                self.assertEqual(target.bitrate_kbps, expected)
                self.assertEqual(target.channels, 1)
                self.assertLessEqual(target.bitrate_kbps, source_kbps)

    def test_high_stereo_becomes_64_mono(self) -> None:
        for kbps in (128, 192, 256, 320):
            with self.subTest(kbps=kbps):
                target = rules.decide(info(kbps, 2), Disposition.DOWNCODE)
                self.assertEqual((target.bitrate_kbps, target.channels), (64, 1))

    def test_mid_stereo_is_halved_and_downmixed(self) -> None:
        target = rules.decide(info(96, 2), Disposition.DOWNCODE)
        self.assertEqual((target.bitrate_kbps, target.channels), (48, 1))

    def test_max_bitrate_caps_the_result(self) -> None:
        for cap, expected in ((32, 32), (48, 48), (64, 64)):
            with self.subTest(cap=cap):
                target = rules.decide(info(256, 2), Disposition.DOWNCODE, max_bitrate_kbps=cap)
                self.assertEqual(target.bitrate_kbps, expected)

    def test_every_target_is_a_supported_bitrate(self) -> None:
        for kbps in range(8, 400, 1):
            for channels in (1, 2):
                target = rules.decide(info(kbps, channels), Disposition.DOWNCODE)
                if not target.declined:
                    self.assertIn(target.bitrate_kbps, rules.VALID_BITRATES)


class DramaRules(unittest.TestCase):
    def test_stereo_keeps_two_channels(self) -> None:
        for kbps in (64, 96, 128, 256):
            with self.subTest(kbps=kbps):
                target = rules.decide(info(kbps, 2), Disposition.DRAMA)
                self.assertEqual(target.channels, 2)

    def test_generous_stereo_keeps_128(self) -> None:
        self.assertEqual(rules.decide(info(128, 2), Disposition.DRAMA).bitrate_kbps, 128)
        self.assertEqual(rules.decide(info(256, 2), Disposition.DRAMA).bitrate_kbps, 128)

    def test_modest_stereo_drops_to_64(self) -> None:
        self.assertEqual(rules.decide(info(96, 2), Disposition.DRAMA).bitrate_kbps, 64)

    def test_max_bitrate_does_not_cap_drama(self) -> None:
        # The cap exists to shrink ordinary narration.  Applying it here would
        # defeat the purpose of marking a book as dramatised.
        target = rules.decide(info(256, 2), Disposition.DRAMA, max_bitrate_kbps=64)
        self.assertEqual((target.bitrate_kbps, target.channels), (128, 2))

    def test_mono_source_falls_back_to_standard_rules(self) -> None:
        target = rules.decide(info(64, 1), Disposition.DRAMA)
        self.assertEqual((target.bitrate_kbps, target.channels), (64, 1))
        self.assertIn("mono", target.rationale)

    def test_mono_below_floor_is_still_declined(self) -> None:
        self.assertTrue(rules.decide(info(16, 1), Disposition.DRAMA).declined)


class PassThroughDispositions(unittest.TestCase):
    def test_remux_reports_the_source_unchanged(self) -> None:
        target = rules.decide(info(128, 2), Disposition.REMUX)
        self.assertEqual((target.bitrate_kbps, target.channels), (128, 2))
        self.assertFalse(target.declined)

    def test_as_is_reports_the_source_unchanged(self) -> None:
        target = rules.decide(info(48, 1), Disposition.AS_IS)
        self.assertEqual((target.bitrate_kbps, target.channels), (48, 1))

    def test_remux_and_as_is_are_never_declined_on_low_bitrate(self) -> None:
        # Neither re-encodes, so the quality floor does not apply.
        for disposition in (Disposition.REMUX, Disposition.AS_IS):
            with self.subTest(disposition=disposition):
                self.assertFalse(rules.decide(info(16, 1), disposition).declined)

    def test_non_acting_dispositions_are_declined(self) -> None:
        for disposition in (Disposition.HOLD, Disposition.DONE, Disposition.UNTRIAGED):
            with self.subTest(disposition=disposition):
                self.assertTrue(rules.decide(info(128, 2), disposition).declined)


class BitrateNormalisation(unittest.TestCase):
    def test_bitrate_is_bucketed_to_8k(self) -> None:
        self.assertEqual(info(63, 1).normalized_bitrate_kbps, 64)
        self.assertEqual(info(65, 1).normalized_bitrate_kbps, 64)
        self.assertEqual(info(60, 1).normalized_bitrate_kbps, 64)

    def test_floor_to_valid_never_exceeds_input(self) -> None:
        for kbps in range(32, 400):
            self.assertLessEqual(rules._floor_to_valid(kbps), kbps)

    def test_floor_to_valid_has_a_lower_bound(self) -> None:
        self.assertEqual(rules._floor_to_valid(1), rules.VALID_BITRATES[0])


class DramaticDetection(unittest.TestCase):
    def test_marker_phrases_are_detected(self) -> None:
        for value in (
            "Dungeon Crawler Carl - GraphicAudio",
            "A Graphic Audio Production",
            "Full Cast Dramatisation",
            "Dramatized Adaptation",
            "An Audio Drama",
        ):
            with self.subTest(value=value):
                self.assertIsNotNone(rules.detect_dramatic(info(128, 2, metadata={"album": value})))

    def test_ordinary_titles_are_not_false_positives(self) -> None:
        # The looser implementation this replaces matched bare "graphic" and
        # "dramatic", which caught plenty of ordinary books.
        for value in (
            "The Graphic Design Handbook",
            "Dramatic Irony in Shakespeare",
            "A Graphic Novel Companion",
            "Ordinary Audiobook",
        ):
            with self.subTest(value=value):
                self.assertIsNone(rules.detect_dramatic(info(128, 2, metadata={"album": value})))

    def test_punctuation_does_not_hide_a_match(self) -> None:
        self.assertIsNotNone(
            rules.detect_dramatic(info(128, 2, metadata={"album": "Graphic-Audio!"}))
        )

    def test_filename_is_searched_too(self) -> None:
        match = rules.detect_dramatic(info(128, 2, name="Book - graphic audio.mp3"))
        self.assertIsNotNone(match)
        self.assertEqual(match.field, "filename")


class Suggestions(unittest.TestCase):
    def test_dramatic_audio_suggests_drama(self) -> None:
        suggestion, _ = rules.suggest_disposition(
            info(128, 2, metadata={"album": "DCC GraphicAudio"})
        )
        self.assertIs(suggestion, Disposition.DRAMA)

    def test_good_mp3_suggests_downcode(self) -> None:
        suggestion, _ = rules.suggest_disposition(info(128, 2, name="book.mp3"))
        self.assertIs(suggestion, Disposition.DOWNCODE)

    def test_low_bitrate_mp3_suggests_remux(self) -> None:
        suggestion, _ = rules.suggest_disposition(info(24, 1, name="book.mp3"))
        self.assertIs(suggestion, Disposition.REMUX)

    def test_low_bitrate_m4b_suggests_as_is(self) -> None:
        suggestion, _ = rules.suggest_disposition(info(16, 1, name="book.m4b"))
        self.assertIs(suggestion, Disposition.AS_IS)

    def test_already_small_m4b_suggests_as_is(self) -> None:
        suggestion, _ = rules.suggest_disposition(info(64, 1, name="book.m4b"))
        self.assertIs(suggestion, Disposition.AS_IS)

    def test_a_suggestion_always_comes_with_a_reason(self) -> None:
        for kbps, channels, name in ((16, 1, "a.m4b"), (128, 2, "b.mp3"), (64, 1, "c.m4b")):
            _, reason = rules.suggest_disposition(info(kbps, channels, name=name))
            self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
