"""Filing finished books into the library.

The naming tests are the important ones: the live library was built by absmove,
and a sanitiser that normalises differently would file the same author under a
second folder and split books Audiobookshelf currently shows as one.  Every
expected value here was taken from what is actually on disk.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempCaseMixin, copy_audio, requires_ffmpeg, write_config
from tests.test_library import FakeSession

from forge import config as config_mod
from forge import naming
from forge import shelve as shelve_mod
from forge import tags
from forge.errors import ConfigError
from forge.history import History
from forge.shelve import ShelveOutcome, ShelveStatus
from forge.tags import BookMetadata


def meta(**fields) -> BookMetadata:
    return BookMetadata(path="/nowhere.m4b", **fields)


def library_path(root: Path, metadata: BookMetadata, **kwargs):
    """``shelve.library_path`` with the stock templates, which these tests assert."""

    kwargs.setdefault("templates", naming.default_templates())
    return shelve_mod.library_path(root, metadata, **kwargs)


class LibraryComponentNames(unittest.TestCase):
    """What absmove put on disk, which forge must keep putting there."""

    def test_a_colon_becomes_an_underscore(self) -> None:
        # /media/audiobooks/Audiobooks/Bill Oakley/Space_ 1969 & 1972
        self.assertEqual(shelve_mod.library_component("Space: 1969 & 1972"), "Space_ 1969 & 1972")

    def test_quotes_become_underscores(self) -> None:
        # /media/audiobooks/Audiobooks/E.E. _Doc_ Smith, Reed McColm
        self.assertEqual(
            shelve_mod.library_component('E.E. "Doc" Smith, Reed McColm'),
            "E.E. _Doc_ Smith, Reed McColm",
        )

    def test_a_slash_becomes_an_underscore(self) -> None:
        self.assertEqual(shelve_mod.library_component("Star Trek / Voyager"), "Star Trek _ Voyager")

    def test_runs_of_spaces_are_not_collapsed(self) -> None:
        # paths.sanitize_filename collapses them; this must not, or a folder
        # created earlier stops matching.
        self.assertEqual(shelve_mod.library_component("A  B"), "A  B")

    def test_a_trailing_dot_is_dropped(self) -> None:
        self.assertEqual(shelve_mod.library_component("The End."), "The End")

    def test_a_reserved_name_is_prefixed(self) -> None:
        self.assertEqual(shelve_mod.library_component("NUL"), "_NUL")

    def test_an_empty_component_is_never_empty(self) -> None:
        for value in ("", "   ", None, ":::"):
            self.assertTrue(shelve_mod.library_component(value))

    def test_a_long_author_folder_already_in_the_library_is_preserved(self) -> None:
        # The library holds a 207-byte fourteen-author folder.  Capping at
        # forge's narrower output budget would file its next book under a new,
        # truncated name beside it.
        fourteen = ", ".join(f"Author Number {n:02d} Of Many" for n in range(1, 9))
        self.assertGreater(len(fourteen.encode()), 200)
        self.assertEqual(shelve_mod.library_component(fourteen), fourteen)

    def test_a_component_is_never_longer_than_a_filesystem_allows(self) -> None:
        capped = shelve_mod.library_component("x" * 400)
        self.assertEqual(len(capped.encode()), shelve_mod.MAX_LIBRARY_COMPONENT)

    def test_a_series_part_is_padded_to_two_digits(self) -> None:
        self.assertEqual(shelve_mod.format_series_part("3"), "03")
        self.assertEqual(shelve_mod.format_series_part(3), "03")
        self.assertEqual(shelve_mod.format_series_part("12"), "12")

    def test_a_non_numeric_part_is_left_alone(self) -> None:
        self.assertEqual(shelve_mod.format_series_part("1.5"), "1.5")
        self.assertEqual(shelve_mod.format_series_part("Prequel"), "Prequel")
        self.assertEqual(shelve_mod.format_series_part(""), "")


class LibraryPaths(unittest.TestCase):
    root = Path("/library")

    def test_the_full_shape(self) -> None:
        path = library_path(
            self.root,
            meta(
                author="Jonathan Brooks",
                album="The Affinity Collector 3",
                series="Octaeteris Cycle",
                series_sequence="3",
                published_year=2026,
                asin="B0HM2FPY5F",
            ),
        )
        self.assertEqual(
            path,
            self.root
            / "Jonathan Brooks"
            / "Octaeteris Cycle"
            / "03 - The Affinity Collector 3"
            / "The Affinity Collector 3 (2026) [B0HM2FPY5F].m4b",
        )

    def test_no_series_means_no_series_folder_and_no_part_prefix(self) -> None:
        path = library_path(
            self.root, meta(author="Adrian Tchaikovsky", album="Ironclads", published_year=2017)
        )
        self.assertEqual(
            path, self.root / "Adrian Tchaikovsky" / "Ironclads" / "Ironclads (2017).m4b"
        )

    def test_no_year_and_no_asin(self) -> None:
        path = library_path(self.root, meta(author="A", album="B"))
        self.assertEqual(path, self.root / "A" / "B" / "B.m4b")

    def test_the_year_falls_back_to_the_published_date(self) -> None:
        path = library_path(
            self.root, meta(author="A", album="B", published_date="2019-04-02")
        )
        self.assertEqual(path.name, "B (2019).m4b")

    def test_the_author_folder_is_the_cleaned_author(self) -> None:
        path = library_path(
            self.root, meta(author="Mary Smith - Translator, Bob Jones", album="B")
        )
        self.assertEqual(path.parts[2], "Bob Jones")

    def test_the_album_artist_is_used_when_there_is_no_author(self) -> None:
        path = library_path(self.root, meta(albumartist="A A", album="B"))
        self.assertEqual(path.parts[2], "A A")

    def test_the_suffix_is_kept(self) -> None:
        path = library_path(self.root, meta(author="A", album="B"), suffix=".m4a")
        self.assertEqual(path.suffix, ".m4a")

    def test_a_very_long_title_still_fits_a_filesystem_component(self) -> None:
        path = library_path(self.root, meta(author="A", album="x" * 300))
        for part in path.parts:
            self.assertLessEqual(len(part.encode("utf-8")), 255, part)

    def test_a_long_filename_leaves_room_for_a_collision_marker(self) -> None:
        # Otherwise resolving a collision would produce a name too long to
        # write, turning a shelving into a failure at the last step.
        path = library_path(self.root, meta(author="A", album="x" * 300))
        self.assertLessEqual(len(f"{path.stem} (999){path.suffix}".encode()), 255)


class CleaningAuthors(unittest.TestCase):
    def test_a_role_credit_is_dropped(self) -> None:
        for role in shelve_mod.AUTHOR_ROLE_SUFFIXES:
            self.assertEqual(
                shelve_mod.clean_author(f"Bob Jones, Mary Smith - {role.title()}"), "Bob Jones"
            )

    def test_stacked_roles_are_dropped(self) -> None:
        self.assertEqual(
            shelve_mod.clean_author("Bob Jones, Julia Fouche - Translator and Editor"),
            "Bob Jones",
        )
        self.assertEqual(
            shelve_mod.clean_author("Bob Jones, Julia Fouche - Translator/Editor"), "Bob Jones"
        )

    def test_a_leftover_bare_role_is_dropped(self) -> None:
        self.assertEqual(
            shelve_mod.clean_author("Bob Jones, Julia Fouche - Translator, Editor"), "Bob Jones"
        )

    def test_aka_names_both_count(self) -> None:
        self.assertEqual(
            shelve_mod.clean_author("Tom Clancy aka Jim Jones"), "Tom Clancy, Jim Jones"
        )

    def test_a_hyphenated_surname_is_not_touched(self) -> None:
        # The reason the roles are an explicit list rather than "drop anything
        # after a dash".
        self.assertEqual(shelve_mod.clean_author("Mary Smith - Jones"), "Mary Smith - Jones")

    def test_an_author_who_is_only_a_credit_is_kept(self) -> None:
        # Better a shelf named after the editor than no author at all.
        self.assertEqual(shelve_mod.clean_author("Mary Smith - Editor"), "Mary Smith - Editor")

    def test_an_ordinary_author_is_unchanged(self) -> None:
        self.assertEqual(shelve_mod.clean_author("Jonathan Brooks"), "Jonathan Brooks")

    def test_nothing_in_nothing_out(self) -> None:
        self.assertEqual(shelve_mod.clean_author(None), "")
        self.assertEqual(shelve_mod.clean_author(""), "")


class ShelveCase(TempCaseMixin, unittest.TestCase):
    """A config with a library, and books in the output directory."""

    SHELVED_ASIN = "B0CMYXVB65"

    def setUp(self) -> None:
        super().setUp()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        path = write_config(self.root, library_dir=self.library)
        path.write_text(
            path.read_text()
            + "\n[audiobookshelf]\nurl = http://abs.invalid\napi_key = secret\n"
        )
        self.cfg = config_mod.load(path)
        self.session = FakeSession()

    def client(self):
        from forge.library import AudiobookshelfClient

        return AudiobookshelfClient(
            "http://abs.invalid", "secret", session=self.session
        )

    def book(self, name: str, **fields) -> Path:
        path = self.cfg.output_dir / name
        copy_audio(path, seconds=1)
        if fields:
            tags.write_metadata(path, fields)
        return path

    def tree_hash(self, root: Path) -> list[tuple[str, str]]:
        """Every file under *root*, with its digest.  For "changed nothing"."""

        out = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out.append(
                    (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
                )
        return out


@requires_ffmpeg
class Planning(ShelveCase):
    def test_a_complete_book_is_ready(self) -> None:
        path = self.book("book.m4b", author="A Author", album="A Book", series="S", series_sequence="2")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.READY)
        self.assertEqual(
            plan.target, self.library / "A Author" / "S" / "02 - A Book" / "A Book.m4b"
        )

    def test_a_book_with_no_author_is_skipped(self) -> None:
        path = self.book("no-author.m4b", album="A Book")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.SKIPPED)
        self.assertEqual(plan.category, "missing author")

    def test_a_book_with_no_album_is_skipped(self) -> None:
        path = self.book("no-album.m4b", author="A Author")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.SKIPPED)
        self.assertEqual(plan.category, "missing album")

    def test_a_book_credited_only_to_an_editor_still_has_an_author(self) -> None:
        path = self.book("edited.m4b", author="Mary Smith - Editor", album="A Book")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.READY)

    def test_a_book_with_no_asin_is_shelved_without_asking_the_library(self) -> None:
        path = self.book("no-asin.m4b", author="A", album="B")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.READY)
        self.assertEqual(self.session.calls, [])

    def test_a_shelved_asin_is_a_duplicate(self) -> None:
        path = self.book("dupe.m4b", author="A", album="B", asin=self.SHELVED_ASIN)
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.DUPLICATE)
        self.assertEqual(plan.duplicate_of.title, "Ironclads")

    def test_a_library_that_cannot_be_reached_blocks_rather_than_shelves(self) -> None:
        # Without an answer there is no way to tell a new book from a second
        # copy, and guessing wrong puts a duplicate in the library.
        import requests

        self.session.get = mock.Mock(side_effect=requests.ConnectionError("no route"))
        path = self.book("unknown.m4b", author="A", album="B", asin="B000000000")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertIs(plan.status, ShelveStatus.SKIPPED)
        self.assertEqual(plan.category, "library error")

    def test_the_tag_fixes_are_planned_not_applied(self) -> None:
        path = self.book(
            "fixable.m4b", author="Bob Jones, Mary Smith - Translator", album="B", track=1
        )
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertEqual(plan.tag_fixes, {"author": "Bob Jones", "track": None})
        after = tags.read_metadata(path)
        self.assertEqual(after.author, "Bob Jones, Mary Smith - Translator")
        self.assertEqual(after.track, 1)

    def test_two_books_cannot_claim_the_same_destination(self) -> None:
        first = self.book("one.m4b", author="A", album="B")
        second = self.book("two.m4b", author="A", album="B")
        plans = shelve_mod.plan_many([first, second], self.cfg, client=self.client())
        self.assertNotEqual(plans[0].target, plans[1].target)
        self.assertEqual(plans[1].target.name, "B (2).m4b")

    def test_an_existing_library_file_is_not_a_destination(self) -> None:
        existing = self.library / "A" / "B" / "B.m4b"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"already here")
        path = self.book("book.m4b", author="A", album="B")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertEqual(plan.target.name, "B (2).m4b")


class CaseInsensitiveFolders(unittest.TestCase):
    """A folder differing only in case is reused, not duplicated.

    The library is read over SMB and from Windows, which cannot address two
    names that differ only in case: both are listed, both open the same one,
    and the other's contents are unreachable.  Four series in the live library
    were split across such a pair before this existed.
    """

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="forge-case-")
        self.root = Path(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def resolve(self, *parts: str) -> Path:
        return shelve_mod.match_existing_case(self.root, self.root.joinpath(*parts))

    def test_an_author_folder_is_reused_whatever_the_tag_says(self) -> None:
        (self.root / "FUNA" / "A Series").mkdir(parents=True)
        self.assertEqual(
            self.resolve("Funa", "A Series", "01 - Book", "Book.m4b"),
            self.root / "FUNA" / "A Series" / "01 - Book" / "Book.m4b",
        )

    def test_a_series_folder_is_reused_too(self) -> None:
        # The live library has one of these: Earth's First StarFighter against
        # Earth's First Starfighter, under one author.
        (self.root / "An Author" / "Earth's First StarFighter").mkdir(parents=True)
        self.assertEqual(
            self.resolve("An Author", "Earth's First Starfighter", "Book.m4b"),
            self.root / "An Author" / "Earth's First StarFighter" / "Book.m4b",
        )

    def test_two_case_variants_are_consolidated_not_kept_apart(self) -> None:
        # Deliberately *not* "the exact name wins": when both spellings exist
        # the books have to end up in one of them, or Windows can reach only
        # one and the other's contents are invisible.  The fuller shelf wins,
        # so the fewest books move.
        (self.root / "FUNA" / "Book One").mkdir(parents=True)
        (self.root / "FUNA" / "Book One" / "One.m4b").touch()
        (self.root / "Funa").mkdir()

        self.assertEqual(self.resolve("Funa", "Two.m4b"), self.root / "FUNA" / "Two.m4b")
        self.assertEqual(self.resolve("FUNA", "Two.m4b"), self.root / "FUNA" / "Two.m4b")

    def test_two_existing_variants_resolve_the_same_way_every_time(self) -> None:
        (self.root / "XKarnation").mkdir()
        (self.root / "Xkarnation").mkdir()
        first = self.resolve("xkarnation", "Book.m4b")
        self.assertEqual(first, self.resolve("XKARNATION", "Book.m4b"))
        self.assertEqual(first.parent.name, "XKarnation")

    def test_a_new_author_keeps_the_spelling_it_was_given(self) -> None:
        # No normalising: "eden Hudson" is how she spells it.
        self.assertEqual(
            self.resolve("eden Hudson", "Book.m4b"), self.root / "eden Hudson" / "Book.m4b"
        )

    def test_the_file_name_is_left_alone(self) -> None:
        # Files are the collision checker's business, not this function's.
        (self.root / "An Author").mkdir()
        (self.root / "An Author" / "BOOK.m4b").touch()
        self.assertEqual(
            self.resolve("An Author", "Book.m4b"), self.root / "An Author" / "Book.m4b"
        )

    def test_a_file_differing_only_in_case_counts_as_taken(self) -> None:
        from forge.paths import collision_safe_path

        (self.root / "BOOK.m4b").touch()
        self.assertEqual(
            collision_safe_path(self.root / "Book.m4b"), self.root / "Book (2).m4b"
        )

    def test_a_path_outside_the_library_is_returned_unchanged(self) -> None:
        elsewhere = Path("/tmp/not/under/the/library/Book.m4b")
        self.assertEqual(shelve_mod.match_existing_case(self.root, elsewhere), elsewhere)


@requires_ffmpeg
class PlanningAgainstExistingFolders(ShelveCase):
    def test_a_plan_targets_the_folder_that_is_already_there(self) -> None:
        (self.library / "FUNA" / "Average").mkdir(parents=True)
        path = self.book(
            "book.m4b", author="Funa", album="Volume 2", series="average",
            series_sequence="2",
        )
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertEqual(
            plan.target,
            self.library / "FUNA" / "Average" / "02 - Volume 2" / "Volume 2.m4b",
        )

    def test_shelving_puts_the_book_in_the_existing_folder(self) -> None:
        (self.library / "FUNA").mkdir(parents=True)
        path = self.book("book.m4b", author="Funa", album="A Book")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        result = shelve_mod.execute(plan, self.cfg)

        self.assertTrue(result.destination.exists())
        self.assertEqual(
            sorted(p.name for p in self.library.iterdir()), ["FUNA"]
        )


@requires_ffmpeg
class CanonicalAuthors(ShelveCase):
    """Shelving writes the registry's spelling, so ABS indexes one author."""

    CHANEY = "B00W2ZAK7E"
    MAGGERT = "B00EKN8RHG"

    def registry(self):
        from forge.authors import AuthorRegistry, Contributor

        registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(registry.close)
        registry.learn(
            [
                Contributor(self.CHANEY, "J.N. Chaney"),
                Contributor(self.MAGGERT, "Terry Maggert"),
            ]
        )
        return registry

    def test_the_tag_is_rewritten_to_the_canonical_spelling(self) -> None:
        path = self.book("book.m4b", author="JN Chaney", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(plan.tag_fixes.get("author"), "J.N. Chaney")
        self.assertEqual(plan.target.parent.parent.name, "J.N. Chaney")

    def test_shelving_writes_that_spelling_into_the_file(self) -> None:
        path = self.book("book.m4b", author="J N Chaney", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        result = shelve_mod.execute(plan, self.cfg)
        self.assertEqual(tags.read_metadata(result.destination).author, "J.N. Chaney")

    def test_role_credits_are_stripped_before_the_name_is_resolved(self) -> None:
        path = self.book(
            "book.m4b", author="JN Chaney, Some Translator - translator", album="A Book"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(plan.tag_fixes.get("author"), "J.N. Chaney")

    def test_an_unknown_author_is_left_alone(self) -> None:
        path = self.book("book.m4b", author="A Total Newcomer", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertNotIn("author", plan.tag_fixes)
        self.assertEqual(plan.target.parent.parent.name, "A Total Newcomer")

    def test_a_collaboration_keeps_its_billing_order(self) -> None:
        path = self.book("book.m4b", author="Terry Maggert, J N Chaney", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(plan.tag_fixes.get("author"), "Terry Maggert, J.N. Chaney")

    def test_a_collaboration_joins_the_shelf_it_already_has(self) -> None:
        # The live library has this both ways round: "J.N. Chaney, Terry
        # Maggert" with 55 books and "Terry Maggert, J. N. Chaney" with 4.
        (self.library / "J.N. Chaney, Terry Maggert").mkdir(parents=True)
        path = self.book("book.m4b", author="Terry Maggert, JN Chaney", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(
            plan.target.parent.parent.name, "J.N. Chaney, Terry Maggert"
        )

    def test_a_new_collaboration_is_filed_alphabetically(self) -> None:
        # Audible bills the same pair in whatever order it likes, so the folder
        # name is sorted to stop that making two shelves.
        path = self.book(
            "book.m4b", author="Terry Maggert, JN Chaney", album="A Book"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(
            plan.target.relative_to(self.library).parts[0],
            "J.N. Chaney, Terry Maggert",
        )

    def test_the_tag_keeps_the_order_it_was_billed_in(self) -> None:
        # Only the folder is sorted.  The billing order is information.
        path = self.book(
            "book.m4b", author="Terry Maggert, JN Chaney", album="A Book"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(plan.tag_fixes.get("author"), "Terry Maggert, J.N. Chaney")

        result = shelve_mod.execute(plan, self.cfg)
        self.assertEqual(
            tags.read_metadata(result.destination).author, "Terry Maggert, J.N. Chaney"
        )

    def test_an_existing_shelf_wins_whatever_order_it_used(self) -> None:
        # Nothing already shelved is renamed for the sake of alphabetising it.
        (self.library / "Terry Maggert, J.N. Chaney").mkdir(parents=True)
        path = self.book(
            "book.m4b", author="J.N. Chaney, Terry Maggert", album="A Book"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(
            plan.target.relative_to(self.library).parts[0],
            "Terry Maggert, J.N. Chaney",
        )

    def test_a_name_suffix_does_not_become_a_second_author(self) -> None:
        # One shelf, not "Jr., L. E. Modesitt".  The trailing full stop is
        # dropped by library_component, because a folder name may not end in
        # one on Windows - which is why the live folder is "…, Jr" already.
        path = self.book(
            "book.m4b", author="L. E. Modesitt, Jr.", album="A Book"
        )
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertEqual(
            plan.target.relative_to(self.library).parts[0], "L. E. Modesitt, Jr"
        )

    def test_a_different_line_up_gets_its_own_shelf(self) -> None:
        (self.library / "J.N. Chaney, Terry Maggert").mkdir(parents=True)
        path = self.book("book.m4b", author="JN Chaney", album="A Book")
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), registry=self.registry()
        )
        self.assertEqual(plan.target.parent.parent.name, "J.N. Chaney")

    def test_with_no_registry_nothing_changes(self) -> None:
        path = self.book("book.m4b", author="JN Chaney", album="A Book")
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertNotIn("author", plan.tag_fixes)
        self.assertEqual(plan.target.parent.parent.name, "JN Chaney")


@requires_ffmpeg
class CleaningTitles(ShelveCase):
    """Marketing out of the title, and out of the folder name with it."""

    PHRASES = ("A LitRPG Adventure",)

    def test_the_title_and_album_are_both_cleaned(self) -> None:
        path = self.book(
            "book.m4b",
            author="An Author",
            album="The Land: A LitRPG Adventure",
            title="The Land: A LitRPG Adventure",
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), phrases=self.PHRASES
        )
        self.assertEqual(plan.tag_fixes.get("album"), "The Land")
        self.assertEqual(plan.tag_fixes.get("title"), "The Land")

    def test_the_folder_follows_the_cleaned_title(self) -> None:
        path = self.book(
            "book.m4b", author="An Author", album="The Land (A LitRPG Adventure)"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), phrases=self.PHRASES
        )
        self.assertEqual(
            plan.target.relative_to(self.library).parts[1:],
            ("The Land", "The Land.m4b"),
        )

    def test_the_subtitle_is_left_alone(self) -> None:
        path = self.book(
            "book.m4b",
            author="An Author",
            album="The Land: A LitRPG Adventure",
            subtitle="A LitRPG Adventure",
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), phrases=self.PHRASES
        )
        self.assertNotIn("subtitle", plan.tag_fixes)

    def test_shelving_writes_the_cleaned_title(self) -> None:
        path = self.book(
            "book.m4b", author="An Author", album="The Land: A LitRPG Adventure"
        )
        plan = shelve_mod.plan_book(
            path, self.cfg, client=self.client(), phrases=self.PHRASES
        )
        result = shelve_mod.execute(plan, self.cfg)
        self.assertEqual(tags.read_metadata(result.destination).album, "The Land")

    def test_with_no_phrases_the_title_is_untouched(self) -> None:
        path = self.book(
            "book.m4b", author="An Author", album="The Land: A LitRPG Adventure"
        )
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        self.assertNotIn("album", plan.tag_fixes)
        self.assertEqual(
            plan.target.relative_to(self.library).parts[1],
            "The Land_ A LitRPG Adventure",
        )


@requires_ffmpeg
class LearningIdentities(ShelveCase):
    """The sweep that fills the registry before anything is planned."""

    CHANEY = "B00W2ZAK7E"

    class FakeAudible:
        def __init__(self, answers=None, fail=False):
            from forge.authors import Contributor

            self.answers = answers or {
                "B0DKPH5RT9": (Contributor("B00W2ZAK7E", "J.N. Chaney"),)
            }
            self.fail = fail
            self.calls: list[str] = []

        def contributors(self, asin):
            self.calls.append(asin)
            if self.fail:
                raise RuntimeError("Audible is having a day")
            return self.answers.get(asin, ())

    def registry(self):
        from forge.authors import AuthorRegistry

        registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(registry.close)
        return registry

    def test_a_lookup_teaches_the_registry(self) -> None:
        path = self.book("book.m4b", author="J N Chaney", album="A Book", asin="B0DKPH5RT9")
        registry = self.registry()
        outcome = shelve_mod.learn_identities([path], registry, self.FakeAudible())

        self.assertEqual(outcome.looked_up, 1)
        self.assertEqual(registry.canonical_name("JN Chaney"), "J.N. Chaney")

    def test_a_known_author_costs_no_lookup(self) -> None:
        from forge.authors import Contributor

        path = self.book("book.m4b", author="J N Chaney", album="A Book", asin="B0DKPH5RT9")
        registry = self.registry()
        registry.learn([Contributor(self.CHANEY, "J.N. Chaney")])

        audible = self.FakeAudible()
        outcome = shelve_mod.learn_identities([path], registry, audible)

        self.assertEqual(audible.calls, [])
        self.assertEqual(outcome.already_known, 1)

    def test_a_book_without_an_asin_is_recorded_as_a_candidate(self) -> None:
        path = self.book("book.m4b", author="A Newcomer", album="A Book")
        registry = self.registry()
        outcome = shelve_mod.learn_identities([path], registry, self.FakeAudible())

        self.assertEqual(outcome.no_asin, 1)
        self.assertEqual(registry.unknown_names(), [("A Newcomer", 1)])

    def test_a_failed_lookup_does_not_stop_the_sweep(self) -> None:
        path = self.book("book.m4b", author="A Newcomer", album="A Book", asin="B0DKPH5RT9")
        registry = self.registry()
        outcome = shelve_mod.learn_identities(
            [path], registry, self.FakeAudible(fail=True)
        )

        self.assertEqual(outcome.failed, 1)
        self.assertEqual(registry.unknown_names(), [("A Newcomer", 1)])

    def test_a_spelling_audible_does_not_match_becomes_a_candidate(self) -> None:
        # The Brandon Morris case: the book's tag is missing the middle
        # initial Audible's own name carries, so no key matches and nothing is
        # guessed at.
        from forge.authors import Contributor

        path = self.book("book.m4b", author="Brandon Morris", album="A Book", asin="B0DKPH5RT9")
        registry = self.registry()
        audible = self.FakeAudible(
            answers={"B0DKPH5RT9": (Contributor("B01N7UFRX4", "Brandon Q. Morris"),)}
        )
        shelve_mod.learn_identities([path], registry, audible)

        self.assertIsNone(registry.identity("Brandon Morris"))
        self.assertEqual(registry.canonical_name("Brandon Q. Morris"), "Brandon Q. Morris")
        self.assertEqual(registry.unknown_names(), [("Brandon Morris", 1)])

    def test_a_role_credited_contributor_is_not_learned(self) -> None:
        # Audible really does return these as authors, ASIN and all; shelving
        # strips the name before resolving, so an identity for it is useless.
        from forge.authors import Contributor

        path = self.book(
            "book.m4b", author="Shunryu Suzuki", album="A Book", asin="B0DKPH5RT9"
        )
        registry = self.registry()
        audible = self.FakeAudible(
            answers={
                "B0DKPH5RT9": (
                    Contributor("B000AP88YU", "Shunryu Suzuki"),
                    Contributor("B000APNOB2", "David Chadwick - editor"),
                )
            }
        )
        shelve_mod.learn_identities([path], registry, audible)

        self.assertEqual(
            [name for _, name, _ in registry.identities()], ["Shunryu Suzuki"]
        )

    def test_with_no_audible_client_nothing_is_invented(self) -> None:
        path = self.book("book.m4b", author="A Newcomer", album="A Book", asin="B0DKPH5RT9")
        registry = self.registry()
        outcome = shelve_mod.learn_identities([path], registry, None)

        self.assertEqual(outcome.no_asin, 1)
        self.assertEqual(registry.stats()["authors"], 0)


@requires_ffmpeg
class Executing(ShelveCase):
    def shelve(self, path: Path, **kwargs):
        plan = shelve_mod.plan_book(path, self.cfg, client=self.client())
        return shelve_mod.execute(plan, self.cfg, **kwargs)

    def test_a_book_is_moved_into_the_library(self) -> None:
        path = self.book("book.m4b", author="A Author", album="A Book")
        result = self.shelve(path)
        self.assertIs(result.outcome, ShelveOutcome.SHELVED)
        self.assertTrue(result.destination.is_file())
        self.assertFalse(path.exists(), "the original was left in the output directory")

    def test_the_move_is_recorded_in_history(self) -> None:
        path = self.book("book.m4b", author="A Author", album="A Book")
        result = self.shelve(path)
        with History(self.cfg.db_path) as store:
            group = store.group(result.group_id)
            changes = store.changes_in_group(result.group_id)
        self.assertEqual(group.source, "shelve")
        paths = {c.field: (c.old_value, c.new_value) for c in changes}
        self.assertIn("path", paths)
        self.assertEqual(paths["path"][1], str(result.destination))

    def test_the_recorded_move_is_not_something_undo_would_write(self) -> None:
        # "path" is in UNRESTORABLE_FIELDS, so the move shows in history while
        # undo leaves it alone instead of writing a tag called "path".
        path = self.book("book.m4b", author="A Author", album="A Book")
        result = self.shelve(path)
        with History(self.cfg.db_path) as store:
            restore = store.restore_values(result.group_id)
        for fields in restore.values():
            self.assertNotIn("path", fields)

    def test_the_tag_fixes_are_applied_and_undoable(self) -> None:
        path = self.book(
            "fixable.m4b", author="Bob Jones, Mary Smith - Translator", album="A Book", track=3
        )
        result = self.shelve(path)
        shelved = tags.read_metadata(result.destination)
        self.assertEqual(shelved.author, "Bob Jones")
        self.assertIsNone(shelved.track)

        # One edit, one group: the move and both fixes undo together.
        with History(self.cfg.db_path) as store:
            restore = store.restore_values(result.group_id)
        fields = next(iter(restore.values()))
        self.assertEqual(fields["author"], "Bob Jones, Mary Smith - Translator")
        self.assertEqual(fields["track"], "3")

    def test_the_change_log_follows_the_file_into_the_library(self) -> None:
        # Recorded against the output directory, which the book then leaves.
        # Asking about it where it now lives must show the tag edits, and an
        # undo must write to the file that is actually there.
        path = self.book(
            "fixable.m4b", author="Bob Jones, Mary Smith - Translator", album="A Book", track=3
        )
        result = self.shelve(path)
        with History(self.cfg.db_path) as store:
            groups = store.groups_for_file(result.destination)
            restore = store.restore_values(result.group_id)
        self.assertEqual([g.id for g in groups], [result.group_id])
        self.assertEqual(list(restore), [str(result.destination)])
        self.assertNotIn(str(path), restore)

    def test_one_group_covers_the_edit_and_the_move(self) -> None:
        path = self.book(
            "fixable.m4b", author="Bob Jones, Mary Smith - Translator", album="A Book", track=3
        )
        result = self.shelve(path)
        with History(self.cfg.db_path) as store:
            groups = store.groups_for_book(result.plan.book_key)
            fields = {c.field for c in store.changes_in_group(result.group_id)}
        self.assertEqual(len(groups), 1, "shelving one book recorded more than one group")
        self.assertEqual(groups[0].description, "shelved")
        self.assertEqual(fields, {"author", "track", "path"})

    def test_a_duplicate_is_set_aside_and_never_enters_the_library(self) -> None:
        path = self.book("dupe.m4b", author="A", album="B", asin=self.SHELVED_ASIN)
        result = self.shelve(path)
        self.assertIs(result.outcome, ShelveOutcome.SET_ASIDE)
        self.assertEqual(result.destination.parent, self.cfg.output_dir / "duplicates")
        self.assertTrue(result.destination.is_file())
        self.assertFalse(path.exists())
        self.assertEqual(list(self.library.rglob("*.m4b")), [])

    def test_setting_aside_is_recorded_with_the_asin_that_matched(self) -> None:
        path = self.book("dupe.m4b", author="A", album="B", asin=self.SHELVED_ASIN)
        result = self.shelve(path)
        with History(self.cfg.db_path) as store:
            group = store.group(result.group_id)
        self.assertIn(self.SHELVED_ASIN, group.description)

    def test_a_skipped_book_is_left_exactly_where_it_was(self) -> None:
        path = self.book("no-author.m4b", album="B")
        before = path.read_bytes()
        result = self.shelve(path)
        self.assertIs(result.outcome, ShelveOutcome.SKIPPED)
        self.assertEqual(path.read_bytes(), before)

    def test_nothing_is_overwritten(self) -> None:
        existing = self.library / "A" / "B" / "B.m4b"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"already here")
        path = self.book("book.m4b", author="A", album="B")
        result = self.shelve(path)
        self.assertEqual(existing.read_bytes(), b"already here")
        self.assertEqual(result.destination.name, "B (2).m4b")

    def test_a_dry_run_changes_nothing_at_all(self) -> None:
        self.book("book.m4b", author="A Author", album="A Book", track=2)
        self.book("dupe.m4b", author="A", album="B", asin=self.SHELVED_ASIN)
        before_out = self.tree_hash(self.cfg.output_dir)
        before_lib = self.tree_hash(self.library)

        paths, _ = shelve_mod.find_books(self.cfg.output_dir)
        plans = shelve_mod.plan_many(paths, self.cfg, client=self.client())
        results = [shelve_mod.execute(p, self.cfg, dry_run=True) for p in plans]

        self.assertEqual(self.tree_hash(self.cfg.output_dir), before_out)
        self.assertEqual(self.tree_hash(self.library), before_lib)
        # And it still says what it would have done.
        self.assertEqual(
            {r.outcome for r in results}, {ShelveOutcome.SHELVED, ShelveOutcome.SET_ASIDE}
        )

    def test_a_cross_device_move_falls_back_to_a_verified_copy(self) -> None:
        import errno

        from forge import archive

        real_link = archive.os.link
        calls: list[int] = []

        def fake_link(src, dst):
            calls.append(1)
            raise OSError(errno.EXDEV, "cross-device link")

        path = self.book("book.m4b", author="A Author", album="A Book")
        with mock.patch.object(archive.os, "link", fake_link):
            result = self.shelve(path)

        self.assertTrue(calls, "the fast path was never attempted")
        self.assertIs(result.outcome, ShelveOutcome.SHELVED)
        self.assertTrue(result.destination.is_file())
        self.assertFalse(path.exists())
        self.assertIsNotNone(real_link)

    def test_a_failed_move_reports_a_failure_and_keeps_the_book(self) -> None:
        from forge import shelve as module

        path = self.book("book.m4b", author="A Author", album="A Book")
        with mock.patch.object(
            module, "move_into_library", side_effect=OSError("disk fell over")
        ):
            result = self.shelve(path)
        self.assertIs(result.outcome, ShelveOutcome.FAILED)
        self.assertEqual(result.category, "move error")
        self.assertTrue(path.is_file())


@requires_ffmpeg
class FindingBooks(ShelveCase):
    def test_only_m4b_files_are_candidates(self) -> None:
        self.book("book.m4b", author="A", album="B")
        (self.cfg.output_dir / "cover.jpg").write_bytes(b"not audio")
        (self.cfg.output_dir / "notes.txt").write_text("hello")
        found, pending = shelve_mod.find_books(self.cfg.output_dir)
        self.assertEqual([p.name for p in found], ["book.m4b"])
        self.assertEqual(pending, [])

    def test_a_conversion_in_progress_is_left_alone(self) -> None:
        self.book("J.R. Mathews - Portal.tmp.m4b", author="A", album="B")
        found, pending = shelve_mod.find_books(self.cfg.output_dir)
        self.assertEqual(found, [])
        self.assertEqual(len(pending), 1)

    def test_the_duplicates_directory_is_not_rescanned(self) -> None:
        duplicates = self.cfg.output_dir / "duplicates"
        self.book("duplicates/set-aside.m4b", author="A", album="B")
        self.book("book.m4b", author="A", album="B")
        found, _ = shelve_mod.find_books(self.cfg.output_dir, exclude=[duplicates])
        self.assertEqual([p.name for p in found], ["book.m4b"])

    def test_nested_directories_are_searched(self) -> None:
        self.book("sub/deeper/book.m4b", author="A", album="B")
        found, _ = shelve_mod.find_books(self.cfg.output_dir)
        self.assertEqual([p.name for p in found], ["book.m4b"])


class LibraryConfiguration(TempCaseMixin, unittest.TestCase):
    def test_no_library_dir_is_allowed(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        self.assertFalse(cfg.can_shelve)
        self.assertIsNone(cfg.library_dir)

    def test_a_library_dir_that_does_not_exist_is_refused(self) -> None:
        # Never created: a typo must fail loudly rather than quietly start
        # filling a second, empty library beside the real one.
        path = write_config(self.root, library_dir=self.root / "typo")
        with self.assertRaises(ConfigError) as caught:
            config_mod.load(path)
        self.assertIn("library_dir does not exist", str(caught.exception))

    def test_a_library_inside_the_source_tree_is_refused(self) -> None:
        library = self.root / "src" / "library"
        library.mkdir(parents=True)
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(self.root, library_dir=library))

    def test_a_library_that_contains_the_output_directory_is_refused(self) -> None:
        library = self.root
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(self.root, library_dir=library))

    def test_duplicates_inside_the_library_are_refused(self) -> None:
        library = self.root / "library"
        library.mkdir()
        path = write_config(
            self.root, library_dir=library, duplicates_dir=library / "duplicates"
        )
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_duplicates_without_a_library_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(self.root, duplicates_dir=self.root / "dupes"))

    def test_the_default_duplicates_directory_sits_beside_the_books(self) -> None:
        library = self.root / "library"
        library.mkdir()
        cfg = config_mod.load(write_config(self.root, library_dir=library))
        self.assertEqual(
            cfg.duplicates_for(cfg.output_dir), cfg.output_dir / "duplicates"
        )


if __name__ == "__main__":
    unittest.main()
