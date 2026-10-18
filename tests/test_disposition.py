"""The disposition tag: parsing, round-tripping, and folder unanimity."""

from __future__ import annotations

import subprocess
import unittest

from tests.helpers import TempCaseMixin, copy_mp3, requires_ffmpeg

from forge import disposition as d
from forge import tags
from forge.disposition import Disposition as D
from tests import helpers


class Parsing(unittest.TestCase):
    def test_absent_is_untriaged(self) -> None:
        for value in (None, "", "   "):
            self.assertIs(d.parse(value), D.UNTRIAGED)

    def test_known_values_round_trip(self) -> None:
        for member in D:
            self.assertIs(d.parse(str(member)), member)

    def test_case_and_whitespace_are_forgiving(self) -> None:
        self.assertIs(d.parse("  DOWNCODE  "), D.DOWNCODE)
        self.assertIs(d.parse("As-Is"), D.AS_IS)

    def test_aliases_are_accepted(self) -> None:
        self.assertIs(d.parse("as_is"), D.AS_IS)
        self.assertIs(d.parse("asis"), D.AS_IS)
        self.assertIs(d.parse("dramatic"), D.DRAMA)
        self.assertIs(d.parse("needs_attention"), D.NEEDS_ATTENTION)

    def test_unknown_values_become_hold_never_an_action(self) -> None:
        # This is the forward-compatibility guarantee: a tag written by a future
        # version must never make this version do something destructive.
        for value in ("banana", "downcode_v2", "erase", "convert", "0", "yes"):
            with self.subTest(value=value):
                parsed = d.parse(value)
                self.assertIs(parsed, D.HOLD)
                self.assertFalse(parsed.is_actionable)


class Semantics(unittest.TestCase):
    def test_only_five_dispositions_act(self) -> None:
        acting = {m for m in D if m.is_actionable}
        self.assertEqual(acting, {D.DOWNCODE, D.DRAMA, D.REMUX, D.AS_IS, D.DELETE})

    def test_untriaged_never_acts(self) -> None:
        self.assertFalse(D.UNTRIAGED.is_actionable)

    def test_done_never_acts(self) -> None:
        # The loop guard depends on this.
        self.assertFalse(D.DONE.is_actionable)

    def test_needs_attention_never_acts(self) -> None:
        self.assertFalse(D.NEEDS_ATTENTION.is_actionable)

    def test_only_downcode_and_drama_re_encode(self) -> None:
        self.assertEqual({m for m in D if m.re_encodes}, {D.DOWNCODE, D.DRAMA})

    def test_remux_converts_without_re_encoding(self) -> None:
        self.assertTrue(D.REMUX.converts)
        self.assertFalse(D.REMUX.re_encodes)

    def test_as_is_does_not_convert(self) -> None:
        self.assertFalse(D.AS_IS.converts)

    def test_delete_is_read_back_whatever_the_case(self) -> None:
        # Written by triage in lower case, but a tag edited by hand in MP3Tag
        # is the usual way a disposition gets shouted.
        for value in ("delete", "DELETE", " Delete "):
            with self.subTest(value=value):
                self.assertIs(d.parse(value), D.DELETE)

    def test_delete_acts_but_neither_converts_nor_re_encodes(self) -> None:
        self.assertTrue(D.DELETE.is_actionable)
        self.assertFalse(D.DELETE.converts)
        self.assertFalse(D.DELETE.re_encodes)

    def test_only_delete_destroys(self) -> None:
        # The planner and the renderers branch on this rather than on the
        # member, so a second destructive value would have to opt in here.
        self.assertEqual({m for m in D if m.destroys}, {D.DELETE})

    def test_delete_is_off_the_numbered_menu_but_still_assignable(self) -> None:
        # The gap is the safety feature: a numbered choice that deletes books
        # sits one fingertip away from one that encodes them.
        self.assertNotIn(D.DELETE, d.ASSIGNABLE)
        self.assertIn(D.DELETE, d.GUARDED)
        self.assertIn(D.DELETE, d.SETTABLE)

    def test_assignable_excludes_machine_written_values(self) -> None:
        self.assertNotIn(D.NEEDS_ATTENTION, d.ASSIGNABLE)
        self.assertNotIn(D.DONE, d.ASSIGNABLE)
        self.assertNotIn(D.UNTRIAGED, d.ASSIGNABLE)
        for member in (D.NEEDS_ATTENTION, D.DONE, D.UNTRIAGED):
            self.assertNotIn(member, d.SETTABLE)


@requires_ffmpeg
class RoundTrip(TempCaseMixin):
    def test_mp4_round_trip(self) -> None:
        path = self.root / "book.m4b"
        helpers.copy_audio(path, seconds=2)
        self._assert_round_trip(path)

    def test_id3_round_trip(self) -> None:
        path = self.root / "book.mp3"
        copy_mp3(path, seconds=2)
        self._assert_round_trip(path)

    def _assert_round_trip(self, path) -> None:
        self.assertIs(d.read(path), D.UNTRIAGED)
        for member in (D.DOWNCODE, D.DRAMA, D.REMUX, D.AS_IS, D.HOLD, D.NEEDS_ATTENTION, D.DONE):
            with self.subTest(member=member):
                d.write(path, member)
                self.assertIs(d.read(path), member)
        d.write(path, D.UNTRIAGED)
        self.assertIs(d.read(path), D.UNTRIAGED)

    def test_writing_disposition_preserves_other_tags(self) -> None:
        path = self.root / "book.m4b"
        helpers.copy_audio(path, seconds=2)
        tags.write_metadata(
            path, {"author": "An Author", "album": "An Album", "series": "A Series"}
        )
        d.write(path, D.DOWNCODE)
        meta = tags.read_metadata(path)
        self.assertEqual(meta.author, "An Author")
        self.assertEqual(meta.album, "An Album")
        self.assertEqual(meta.series, "A Series")

    def test_disposition_is_visible_to_external_tools(self) -> None:
        # Tagging has to be inspectable in MP3Tag and ffprobe, or the user
        # cannot audit or correct what forge decided.
        path = self.root / "book.m4b"
        helpers.copy_audio(path, seconds=2)
        d.write(path, D.DRAMA)
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("FORGE_DISPOSITION=drama", output)

    def test_no_temporary_file_is_left_behind(self) -> None:
        path = self.root / "book.m4b"
        helpers.copy_audio(path, seconds=2)
        d.write(path, D.DOWNCODE)
        leftovers = [p.name for p in self.root.iterdir() if "forge-tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_safe_write_leaves_the_original_intact_on_failure(self) -> None:
        path = self.root / "book.m4b"
        helpers.copy_audio(path, seconds=2)
        d.write(path, D.DOWNCODE)
        before = path.read_bytes()

        def explode(audio):
            raise RuntimeError("simulated mutagen failure")

        with self.assertRaises(Exception):
            tags.safe_save(path, explode)

        self.assertEqual(path.read_bytes(), before)
        self.assertIs(d.read(path), D.DOWNCODE)
        self.assertEqual([p.name for p in self.root.iterdir() if "forge-tmp" in p.name], [])


@requires_ffmpeg
class FolderBooks(TempCaseMixin):
    def setUp(self) -> None:
        super().setUp()
        self.files = [self.root / f"0{i} - Track.mp3" for i in (1, 2, 3)]
        for path in self.files:
            copy_mp3(path, seconds=1)

    def test_unanimous_folder_reports_its_value(self) -> None:
        d.write_folder(self.files, D.DOWNCODE)
        state = d.read_folder(self.files)
        self.assertIs(state.value, D.DOWNCODE)
        self.assertFalse(state.conflict)
        self.assertTrue(state.is_actionable)

    def test_disagreement_is_a_conflict_and_never_acts(self) -> None:
        d.write_folder(self.files, D.DOWNCODE)
        d.write(self.files[1], D.AS_IS)
        state = d.read_folder(self.files)
        self.assertTrue(state.conflict)
        self.assertFalse(state.is_actionable)
        self.assertEqual(state.label, "conflict")

    def test_a_partially_tagged_folder_is_a_conflict(self) -> None:
        # A half-written folder must not look like a decision.
        d.write(self.files[0], D.DOWNCODE)
        state = d.read_folder(self.files)
        self.assertTrue(state.conflict)
        self.assertFalse(state.is_actionable)

    def test_untagged_folder_is_untriaged(self) -> None:
        state = d.read_folder(self.files)
        self.assertIs(state.value, D.UNTRIAGED)
        self.assertFalse(state.conflict)

    def test_write_folder_reports_failures(self) -> None:
        missing = self.root / "gone.mp3"
        failures = d.write_folder([self.files[0], missing], D.DOWNCODE)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], missing)


if __name__ == "__main__":
    unittest.main()
