"""The two naming templates: how they parse, and what they produce.

The parsing tests matter because a bad template is fatal by design - the user
gets an error and nothing moves, rather than a sanitised surprise and a library
to tidy by hand.  The rendering tests matter because the optional parts of a
name (series, part, year, ASIN) are missing far more often than they are there.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import TempCaseMixin, write_config

from forge import config as config_mod
from forge import naming
from forge.errors import ConfigError
from forge.tags import BookMetadata


def meta(**fields) -> BookMetadata:
    return BookMetadata(path="/nowhere.m4b", **fields)


def filename(template: str, **fields) -> str:
    parsed = naming.parse_template(template, setting="filename_template", allow_slash=False)
    return naming.render_filename(parsed, naming.tag_values(meta(**fields)))


def shelf(template: str, **fields) -> tuple[str, ...]:
    parsed = naming.parse_template(template, setting="shelf_template", allow_slash=True)
    return naming.render_shelf(parsed, naming.tag_values(meta(**fields)))


class Parsing(unittest.TestCase):
    def test_the_stock_templates_parse(self) -> None:
        templates = naming.default_templates()
        self.assertEqual(templates.filename.source, naming.DEFAULT_FILENAME_TEMPLATE)
        self.assertEqual(
            templates.shelf.tag_names(), {"author", "series", "series_part", "album"}
        )

    def test_an_unknown_tag_is_rejected_and_the_known_ones_listed(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            naming.parse_template("<albumname>", setting="filename_template", allow_slash=False)
        message = str(caught.exception)
        self.assertIn("<albumname>", message)
        self.assertIn("<album>", message)

    def test_an_illegal_character_in_the_literal_text_is_rejected(self) -> None:
        # Sanitising it instead would quietly file the book somewhere other
        # than where the template plainly says.
        for template in ("<album>: <subtitle>", '<album> "<narrator>"', "<album>|<asin>"):
            with self.subTest(template=template):
                with self.assertRaises(ConfigError):
                    naming.parse_template(
                        template, setting="filename_template", allow_slash=False
                    )

    def test_an_unclosed_or_stray_bracket_is_rejected(self) -> None:
        for template in ("<album", "album>", "<album>>"):
            with self.subTest(template=template):
                with self.assertRaises(ConfigError):
                    naming.parse_template(
                        template, setting="filename_template", allow_slash=False
                    )

    def test_a_template_with_no_tag_is_rejected(self) -> None:
        # Every book would be given the same name, and the second would
        # collide with the first for ever.
        with self.assertRaises(ConfigError):
            naming.parse_template("audiobook", setting="filename_template", allow_slash=False)

    def test_an_empty_template_is_rejected(self) -> None:
        for template in ("", "   "):
            with self.subTest(template=template):
                with self.assertRaises(ConfigError):
                    naming.parse_template(
                        template, setting="filename_template", allow_slash=False
                    )

    def test_a_filename_template_may_not_contain_a_separator(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            naming.parse_template(
                "<author>/<album>", setting="filename_template", allow_slash=False
            )
        self.assertIn("shelf_template", str(caught.exception))

    def test_an_empty_shelf_component_is_rejected(self) -> None:
        for template in ("<author>//<album>", "/<author>", "<author>/"):
            with self.subTest(template=template):
                with self.assertRaises(ConfigError):
                    naming.parse_template(template, setting="shelf_template", allow_slash=True)

    def test_the_setting_is_named_in_the_error(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            naming.parse_template("<nope>", setting="shelf_template", allow_slash=True)
        self.assertIn("shelf_template", str(caught.exception))


class RenderingFilenames(unittest.TestCase):
    def test_the_full_shape(self) -> None:
        self.assertEqual(
            filename(
                naming.DEFAULT_FILENAME_TEMPLATE,
                album="Pawn of Prophecy",
                published_year="1982",
                asin="B002V0QUOA",
            ),
            "Pawn of Prophecy (1982) [B002V0QUOA].m4b",
        )

    def test_an_empty_bracketed_run_is_dropped_with_its_brackets(self) -> None:
        self.assertEqual(
            filename(naming.DEFAULT_FILENAME_TEMPLATE, album="Ironclads"), "Ironclads.m4b"
        )

    def test_either_optional_part_may_be_missing_on_its_own(self) -> None:
        self.assertEqual(
            filename(naming.DEFAULT_FILENAME_TEMPLATE, album="B", published_year="1999"),
            "B (1999).m4b",
        )
        # The one that used to come out as "B  [XYZ]": the space left by the
        # dropped "(<year>)" must not survive beside the next one.
        self.assertEqual(
            filename(naming.DEFAULT_FILENAME_TEMPLATE, album="B", asin="XYZ"),
            "B [XYZ].m4b",
        )

    def test_glue_next_to_an_empty_tag_goes_with_it(self) -> None:
        self.assertEqual(filename("<album> - <narrator>", album="B"), "B.m4b")
        self.assertEqual(filename("<narrator> - <album>", album="B"), "B.m4b")
        self.assertEqual(
            filename("<album> - <narrator>", album="B", narrator="N"), "B - N.m4b"
        )

    def test_literal_text_with_no_tag_beside_it_survives(self) -> None:
        self.assertEqual(filename("audiobook <album>", album="B"), "audiobook B.m4b")

    def test_brackets_with_no_tag_inside_are_a_fixed_phrase(self) -> None:
        # They are not an optional run, so they stay whatever the tags say.
        self.assertEqual(filename("<album> (unabridged)", album="B"), "B (unabridged).m4b")

    def test_a_double_space_inside_a_tag_value_is_left_alone(self) -> None:
        # Only the seam left by a dropped run is collapsed: the live library
        # has folders with a double space in them, and collapsing one would
        # file the next book under a second name.
        self.assertEqual(
            filename(naming.DEFAULT_FILENAME_TEMPLATE, album="A  B", published_year="1999"),
            "A  B (1999).m4b",
        )

    def test_illegal_characters_in_a_tag_value_become_underscores(self) -> None:
        # Not an error: "Space: 1969" is a real album tag, and this is how the
        # library was already built.
        self.assertEqual(filename("<album>", album="Space: 1969"), "Space_ 1969.m4b")

    def test_the_suffix_is_kept_and_budgeted_for(self) -> None:
        name = filename("<album>", album="x" * 400)
        self.assertTrue(name.endswith(".m4b"))
        self.assertLessEqual(len(f"{Path(name).stem} (999).m4b".encode()), 255)

    def test_a_book_with_none_of_the_tags_the_template_asks_for(self) -> None:
        with self.assertRaises(naming.EmptyName) as caught:
            filename("<series> <series_part>", album="B")
        self.assertIn("<series>", str(caught.exception))


class RenderingShelves(unittest.TestCase):
    def test_the_full_shape(self) -> None:
        self.assertEqual(
            shelf(
                naming.DEFAULT_SHELF_TEMPLATE,
                author="Jonathan Brooks",
                album="The Affinity Collector 3",
                series="Octaeteris Cycle",
                series_sequence="3",
            ),
            ("Jonathan Brooks", "Octaeteris Cycle", "03 - The Affinity Collector 3"),
        )

    def test_a_component_that_renders_empty_is_dropped_not_underscored(self) -> None:
        # This is what gives a standalone book no series folder.
        self.assertEqual(
            shelf(naming.DEFAULT_SHELF_TEMPLATE, author="A", album="B"), ("A", "B")
        )

    def test_everything_coming_out_empty_is_an_error(self) -> None:
        # The book would otherwise be filed loose at the library root, where
        # Audiobookshelf cannot make sense of it.
        with self.assertRaises(naming.EmptyName):
            shelf("<series>/<narrator>", author="A", album="B")

    def test_the_series_part_is_zero_padded(self) -> None:
        self.assertEqual(naming.format_series_part("3"), "03")
        self.assertEqual(naming.format_series_part("1.5"), "1.5")
        self.assertEqual(naming.format_series_part(""), "")


class TheStartupCheck(TempCaseMixin):
    def test_a_bad_template_stops_the_config_from_loading(self) -> None:
        # The whole point: the error arrives before a single file has moved.
        path = write_config(self.root, filename_template="<album>: <subtitle>")
        with self.assertRaises(ConfigError) as caught:
            config_mod.load(path)
        self.assertIn("filename_template", str(caught.exception))

    def test_a_bad_shelf_template_stops_the_config_from_loading(self) -> None:
        path = write_config(self.root, shelf_template="<author>/<nope>")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_a_missing_section_falls_back_to_the_stock_templates(self) -> None:
        # An existing install has no [naming] section and must keep filing
        # books exactly where it already files them.
        cfg = config_mod.load(write_config(self.root))
        self.assertEqual(cfg.shelf_template, naming.DEFAULT_SHELF_TEMPLATE)
        self.assertEqual(cfg.filename_template, naming.DEFAULT_FILENAME_TEMPLATE)

    def test_require_returns_both_parsed_templates(self) -> None:
        cfg = config_mod.load(
            write_config(
                self.root,
                shelf_template="<author>",
                filename_template="<album> - <narrator>",
            )
        )
        templates = naming.require(cfg)
        self.assertEqual(templates.shelf.source, "<author>")
        self.assertEqual(templates.filename.source, "<album> - <narrator>")


class CustomTemplates(TempCaseMixin):
    def test_a_configured_template_decides_where_a_book_goes(self) -> None:
        (self.root / "lib").mkdir(parents=True, exist_ok=True)
        cfg = config_mod.load(
            write_config(
                self.root,
                library_dir=self.root / "lib",
                shelf_template="<narrator>/<album>",
                filename_template="<album> - <narrator>",
            ),
        )
        templates = naming.require(cfg)
        self.assertEqual(
            naming.shelf_path(
                Path("/library"),
                meta(author="A", album="B", narrator="Cameron Beierle"),
                shelf_template=templates.shelf,
                filename_template=templates.filename,
            ),
            Path("/library/Cameron Beierle/B/B - Cameron Beierle.m4b"),
        )


if __name__ == "__main__":
    unittest.main()
