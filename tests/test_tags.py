"""Tag reading and writing for both container types."""

from __future__ import annotations

import unittest

from tests import helpers
from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg

from forge import tags
from forge.errors import TagError

FULL_METADATA = {
    "title": "Pawn of Prophecy",
    "subtitle": "The Belgariad, Book 1",
    "album": "Pawn of Prophecy",
    "author": "David Eddings",
    "albumartist": "David Eddings",
    "narrator": "Brian Wiggins",
    "series": "The Belgariad",
    "series_sequence": "1",
    "asin": "B0D8XMMWLF",
    "description": "A long description.\nWith a second line.",
    "publisher": "Tantor Media",
    "copyright": "\u00a92024 David Eddings (P)2024 Tantor Media",
    "published_date": "2024-07-16",
    "published_year": "2024",
    "language": "english",
    "genres": ["Science Fiction & Fantasy", "Fantasy", "Epic"],
    "explicit": False,
    "track": 3,
    "disc": 1,
}


class Normalisation(unittest.TestCase):
    def test_genres_from_a_list(self) -> None:
        self.assertEqual(tags.normalize_genres(["A", "B"]), ["A", "B"])

    def test_genres_from_a_delimited_string(self) -> None:
        self.assertEqual(tags.normalize_genres("A, B; C"), ["A", "B", "C"])

    def test_genres_from_a_repr_string(self) -> None:
        self.assertEqual(tags.normalize_genres("['A', 'B']"), ["A", "B"])

    def test_ampersands_and_slashes_are_not_separators(self) -> None:
        # "Science Fiction & Fantasy" is one genre, not two.
        self.assertEqual(
            tags.normalize_genres("Science Fiction & Fantasy"), ["Science Fiction & Fantasy"]
        )
        self.assertEqual(tags.normalize_genres("Rock/Pop"), ["Rock/Pop"])

    def test_genres_are_deduplicated_preserving_order(self) -> None:
        self.assertEqual(tags.normalize_genres("B, a, B, A"), ["B", "a"])

    def test_empty_genres(self) -> None:
        for value in (None, "", [], "  "):
            self.assertEqual(tags.normalize_genres(value), [])

    def test_bool_parsing(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", "checked"):
            self.assertIs(tags.parse_bool(value), True)
        for value in ("0", "false", "no", "off", "unchecked"):
            self.assertIs(tags.parse_bool(value), False)
        for value in (None, "maybe", ""):
            self.assertIsNone(tags.parse_bool(value))

    def test_int_parsing_tolerates_track_of_total(self) -> None:
        self.assertEqual(tags.parse_int("3/12"), 3)
        self.assertEqual(tags.parse_int(" 7 "), 7)
        self.assertEqual(tags.parse_int(5), 5)
        self.assertIsNone(tags.parse_int("none"))
        self.assertIsNone(tags.parse_int(None))


class _RoundTripContract:
    """Tag behaviour that must hold identically for MP4 and ID3.

    Subclassed once per container so the whole contract runs twice; this is not
    itself a TestCase, so unittest does not collect it on its own.
    """

    container = ".m4b"

    def make(self, name: str = "book"):
        path = self.root / f"{name}{self.container}"
        if self.container == ".mp3":
            copy_mp3(path, seconds=2)
        else:
            helpers.copy_audio(path, seconds=2)
        return path

    def test_the_fixture_covers_every_editable_field(self) -> None:
        # Guards the round-trip test above: a field added to the editor without
        # a mapping in both containers would otherwise go untested, which is how
        # subtitle and language came to be silently unwritable in the GUI.
        self.assertEqual(
            sorted(name for name, _ in tags.EDITABLE_FIELDS), sorted(FULL_METADATA)
        )

    def test_every_field_round_trips(self) -> None:
        path = self.make()
        tags.write_metadata(path, FULL_METADATA)
        meta = tags.read_metadata(path)

        for field, expected in FULL_METADATA.items():
            with self.subTest(field=field):
                actual = getattr(meta, field)
                if field == "genres":
                    self.assertEqual(actual, tags.normalize_genres(expected))
                elif field == "explicit":
                    self.assertIs(actual, expected)
                else:
                    self.assertEqual(str(actual), str(expected))

    def test_language_round_trips(self) -> None:
        # The implementation this replaces wrote language to one frame and read
        # it from another, so it silently never persisted.
        path = self.make()
        tags.write_metadata(path, {"language": "english"})
        self.assertEqual(tags.read_metadata(path).language, "english")

    def test_subtitle_round_trips(self) -> None:
        # Listed as editable in the GUI, but never actually read or written.
        path = self.make()
        tags.write_metadata(path, {"subtitle": "A Subtitle"})
        self.assertEqual(tags.read_metadata(path).subtitle, "A Subtitle")

    def test_series_sequence_is_mirrored_for_audiobookshelf(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"series_sequence": "4"})
        audio = tags.open_audio(path)
        mirror = "series-part" if tags.is_mp4(audio) else "SERIES-PART"
        self.assertEqual(tags.read_freeform(audio, mirror), "4")

    def test_series_aliases_other_taggers_write_are_read(self) -> None:
        # Other tools spell these differently; reading the alternatives means a
        # book tagged elsewhere arrives in triage with its series intact.
        path = self.make()
        audio = tags.open_audio(path)
        tags.ensure_tags(audio)
        tags.write_freeform(audio, "SHOW", "A Series")
        tags.write_freeform(audio, "PART", "7")
        audio.save()

        meta = tags.read_metadata(path)
        self.assertEqual(meta.series, "A Series")
        self.assertEqual(meta.series_sequence, "7")

    def test_carryover_updates_reproduce_the_tags_on_another_file(self) -> None:
        path = self.make()
        tags.write_metadata(path, FULL_METADATA)
        other = self.make("other")
        self.assertIsNone(tags.read_metadata(other).series)

        tags.write_metadata(other, tags.carryover_updates(tags.read_metadata(path)))

        before, after = tags.read_metadata(path), tags.read_metadata(other)
        for name, _ in tags.EDITABLE_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(getattr(after, name), getattr(before, name))

    def test_carryover_updates_skips_empty_fields(self) -> None:
        # An empty field must not be carried over as an explicit clear: whatever
        # the destination already holds is better than nothing.
        path = self.make()
        tags.write_metadata(path, {"album": "Only This"})
        updates = tags.carryover_updates(tags.read_metadata(path))
        self.assertEqual(updates, {"album": "Only This"})

    def test_carryover_updates_can_leave_track_numbering_behind(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"album": "A", "track": 2, "disc": 1})
        updates = tags.carryover_updates(tags.read_metadata(path), include_track=False)
        self.assertNotIn("track", updates)
        self.assertNotIn("disc", updates)

    def test_fields_can_be_cleared(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"author": "Someone", "series": "A Series"})
        tags.write_metadata(path, {"author": None, "series": None})
        meta = tags.read_metadata(path)
        self.assertIsNone(meta.author)
        self.assertIsNone(meta.series)

    def test_published_year_is_derived_from_the_date(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"published_date": "2024-07-16"})
        self.assertEqual(tags.read_metadata(path).published_year, "2024")

    def test_unknown_field_names_are_rejected(self) -> None:
        # The GUI let any typo'd name fall through into a junk TXXX frame.
        path = self.make()
        with self.assertRaises(TagError) as caught:
            tags.write_metadata(path, {"authr": "Typo"})
        self.assertIn("authr", str(caught.exception))

    def test_cover_is_not_loaded_unless_requested(self) -> None:
        path = self.make()
        png = self.root / "cover.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
        tags.write_metadata(path, {"cover_path": str(png)})

        without = tags.read_metadata(path)
        self.assertTrue(without.has_cover)
        self.assertIsNone(without.cover_data)

        with_cover = tags.read_metadata(path, with_cover=True)
        self.assertIsNotNone(with_cover.cover_data)

    def test_cover_can_be_deleted(self) -> None:
        path = self.make()
        png = self.root / "cover.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
        tags.write_metadata(path, {"cover_path": str(png)})
        self.assertTrue(tags.read_metadata(path).has_cover)
        tags.write_metadata(path, {"delete_cover": True})
        self.assertFalse(tags.read_metadata(path).has_cover)

    def test_cover_type_is_sniffed_not_trusted(self) -> None:
        path = self.make()
        liar = self.root / "notreally.png"
        liar.write_bytes(b"this is not an image at all")
        with self.assertRaises(TagError):
            tags.write_metadata(path, {"cover_path": str(liar)})

    def test_diff_reports_only_real_changes(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"author": "Someone", "album": "A Book"})
        meta = tags.read_metadata(path)

        unchanged = tags.diff_metadata(meta, {"author": "Someone"})
        self.assertEqual(unchanged, {})

        changed = tags.diff_metadata(meta, {"author": "Someone Else"})
        self.assertEqual(changed, {"author": ("Someone", "Someone Else")})

    def test_diff_normalises_genres_before_comparing(self) -> None:
        path = self.make()
        tags.write_metadata(path, {"genres": ["A", "B"]})
        meta = tags.read_metadata(path)
        self.assertEqual(tags.diff_metadata(meta, {"genres": "A, B"}), {})

    def test_unreadable_file_raises_a_clear_error(self) -> None:
        junk = self.root / "junk.m4b"
        junk.write_bytes(b"not audio")
        with self.assertRaises(TagError):
            tags.read_metadata(junk)


@requires_ffmpeg
class RoundTripMP4(_RoundTripContract, TempCaseMixin):
    container = ".m4b"


@requires_ffmpeg
class RoundTripID3(_RoundTripContract, TempCaseMixin):
    container = ".mp3"


if __name__ == "__main__":
    unittest.main()
