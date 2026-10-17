"""Author identity: who is this, really?"""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin

from forge.authors import (
    SCHEMA_VERSION,
    AuthorRegistry,
    Contributor,
    folder_order,
    join_authors,
    name_key,
    shelf_key,
    split_authors,
)

CHANEY = "B00W2ZAK7E"
MAGGERT = "B00EKN8RHG"
MORRIS = "B01N7UFRX4"


class Keys(unittest.TestCase):
    def test_punctuation_and_spacing_do_not_change_identity(self) -> None:
        # Every spelling of this author that Amazon has actually produced.
        spellings = ["J.N. Chaney", "J. N. Chaney", "JN Chaney", "J N Chaney", "j.n.chaney"]
        self.assertEqual(len({name_key(s) for s in spellings}), 1)

    def test_a_missing_initial_is_a_different_key(self) -> None:
        # The whole point: these may be two people, so nothing automatic may
        # join them.  Only an author ASIN or a human can.
        self.assertNotEqual(name_key("Brandon Morris"), name_key("Brandon Q. Morris"))

    def test_different_authors_stay_different(self) -> None:
        self.assertNotEqual(name_key("Terry Maggert"), name_key("Terry Mixon"))

    def test_an_author_tag_splits_on_commas_and_semicolons(self) -> None:
        self.assertEqual(
            split_authors("Tao Wong; Jason J. Willis"), ["Tao Wong", "Jason J. Willis"]
        )
        self.assertEqual(split_authors("A, B , C"), ["A", "B", "C"])
        self.assertEqual(split_authors(None), [])

    def test_joining_keeps_order_and_drops_repeats(self) -> None:
        self.assertEqual(join_authors(["B", "A", "B"]), "B, A")

    def test_a_name_suffix_is_not_a_second_author(self) -> None:
        # "L. E. Modesitt, Jr." is one man and 39 books here are his; reading
        # that comma as a separator gave Audiobookshelf an author called "Jr."
        self.assertEqual(split_authors("L. E. Modesitt, Jr."), ["L. E. Modesitt, Jr."])
        self.assertEqual(split_authors("L. E. Modesitt, Jr"), ["L. E. Modesitt, Jr"])

    def test_a_suffix_does_not_swallow_a_real_co_author(self) -> None:
        self.assertEqual(
            split_authors("Martin Luther King, Jr., Coretta Scott King"),
            ["Martin Luther King, Jr.", "Coretta Scott King"],
        )

    def test_a_suffix_first_is_still_a_name(self) -> None:
        # Nothing to rejoin to, so it stays as it is rather than vanishing.
        self.assertEqual(split_authors("Jr., Someone"), ["Jr.", "Someone"])

    def test_an_honorific_within_a_name_is_left_alone(self) -> None:
        self.assertEqual(split_authors("Someone III"), ["Someone III"])
        self.assertEqual(split_authors("Charles Dickens, BBC"), ["Charles Dickens", "BBC"])

    def test_a_folder_lists_collaborators_alphabetically(self) -> None:
        # By the whole name as written, first name first: no guessing at which
        # part is a surname.
        self.assertEqual(
            folder_order(["John Anderson", "Andrew Johnson"]),
            ["Andrew Johnson", "John Anderson"],
        )

    def test_folder_order_ignores_case(self) -> None:
        self.assertEqual(
            folder_order(["eden Hudson", "James Hunter"]),
            ["eden Hudson", "James Hunter"],
        )

    def test_folder_order_is_the_same_whichever_way_they_arrive(self) -> None:
        one = folder_order(split_authors("Terry Maggert, J.N. Chaney"))
        two = folder_order(split_authors("J.N. Chaney, Terry Maggert"))
        self.assertEqual(one, two)

    def test_a_collaboration_is_the_same_shelf_in_either_order(self) -> None:
        self.assertEqual(
            shelf_key("Terry Maggert, J. N. Chaney"),
            shelf_key("J.N. Chaney, Terry Maggert"),
        )

    def test_a_different_line_up_is_a_different_shelf(self) -> None:
        self.assertNotEqual(
            shelf_key("J. N. Chaney"), shelf_key("J. N. Chaney, Terry Maggert")
        )


class SchemaUpgrade(TempCaseMixin, unittest.TestCase):
    """A registry written by the first version still opens."""

    def v1_database(self):
        import sqlite3

        path = self.root / "authors.sqlite3"
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            CREATE TABLE author (
                author_asin    TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE author_alias (
                name_key    TEXT PRIMARY KEY,
                author_asin TEXT NOT NULL REFERENCES author(author_asin),
                spelling    TEXT NOT NULL,
                source      TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE author_unknown (
                name_key   TEXT PRIMARY KEY,
                spelling   TEXT NOT NULL,
                seen       INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            INSERT INTO schema_info VALUES (1);
            INSERT INTO author VALUES ('B00W2ZAK7E', 'J.N. Chaney', '2026-10-16T00:00:00+00:00');
            INSERT INTO author_alias VALUES
                ('jnchaney', 'B00W2ZAK7E', 'J.N. Chaney', 'audible', '2026-10-16T00:00:00+00:00');
            -- keyed the old way, with the accent deleted rather than folded
            INSERT INTO author VALUES ('B000000001', 'Mira González', '2026-10-16T00:00:00+00:00');
            INSERT INTO author_alias VALUES
                ('miragonzlez', 'B000000001', 'Mira González', 'audible', '2026-10-16T00:00:00+00:00');
            """
        )
        connection.close()
        return path

    def test_a_v1_registry_keeps_its_identities(self) -> None:
        path = self.v1_database()
        with AuthorRegistry(path) as registry:
            self.assertEqual(registry.identity("JN Chaney"), "B00W2ZAK7E")
            self.assertEqual(registry.canonical_name("J N Chaney"), "J.N. Chaney")

    def test_the_upgrade_recomputes_keys_built_the_old_way(self) -> None:
        path = self.v1_database()
        with AuthorRegistry(path) as registry:
            # "miragonzlez" was the old key; both spellings must resolve now.
            self.assertEqual(registry.identity("Mira González"), "B000000001")
            self.assertEqual(registry.identity("Mira Gonzalez"), "B000000001")

    def test_an_upgraded_registry_records_the_new_version(self) -> None:
        from forge.authors import SCHEMA_VERSION

        path = self.v1_database()
        with AuthorRegistry(path) as registry:
            row = registry._connection.execute("SELECT version FROM schema_info").fetchone()
            self.assertEqual(row["version"], SCHEMA_VERSION)

    def test_opening_it_twice_is_harmless(self) -> None:
        path = self.v1_database()
        with AuthorRegistry(path) as registry:
            registry.learn([Contributor(None, "Rhaegar")])
        with AuthorRegistry(path) as registry:
            self.assertEqual(registry.identity("Rhaegar"), "name:rhaegar")
            self.assertEqual(registry.identity("JN Chaney"), "B00W2ZAK7E")


class PinnedNames(TempCaseMixin, unittest.TestCase):
    """A spelling chosen by hand outranks any automatic vote."""

    CHANEY = "B00W2ZAK7E"

    def setUp(self) -> None:
        super().setUp()
        self.registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(self.registry.close)
        self.registry.learn([Contributor(self.CHANEY, "JN Chaney")])

    def test_an_automatic_choice_can_change_a_name(self) -> None:
        self.assertTrue(self.registry.set_canonical(self.CHANEY, "J. N. Chaney"))
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "J. N. Chaney")

    def test_renaming_by_hand_pins_the_name(self) -> None:
        self.registry.rename(self.CHANEY, "J. N. Chaney")
        self.assertTrue(self.registry.is_pinned(self.CHANEY))

    def test_an_automatic_choice_will_not_overrule_a_pin(self) -> None:
        self.registry.rename(self.CHANEY, "J. N. Chaney")
        self.assertFalse(self.registry.set_canonical(self.CHANEY, "JN Chaney"))
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "J. N. Chaney")

    def test_unpinning_lets_the_vote_decide_again(self) -> None:
        self.registry.rename(self.CHANEY, "J. N. Chaney")
        self.registry.unpin(self.CHANEY)
        self.assertTrue(self.registry.set_canonical(self.CHANEY, "JN Chaney"))

    def test_a_learned_name_is_not_pinned(self) -> None:
        self.assertFalse(self.registry.is_pinned(self.CHANEY))

    def test_a_v3_registry_gains_the_column(self) -> None:
        import sqlite3

        path = self.root / "v3.sqlite3"
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            CREATE TABLE author (
                author_id      TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE author_asin (
                asin TEXT PRIMARY KEY, author_id TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE author_alias (
                name_key TEXT PRIMARY KEY, author_id TEXT NOT NULL,
                spelling TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE author_unknown (
                name_key TEXT PRIMARY KEY, spelling TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
            );
            INSERT INTO schema_info VALUES (3);
            INSERT INTO author VALUES ('B00W2ZAK7E', 'J.N. Chaney', '2026-10-16T00:00:00+00:00');
            INSERT INTO author_alias VALUES
                ('jnchaney', 'B00W2ZAK7E', 'J.N. Chaney', 'audible', '2026-10-16T00:00:00+00:00');
            """
        )
        connection.close()

        with AuthorRegistry(path) as registry:
            self.assertFalse(registry.is_pinned("B00W2ZAK7E"))
            self.assertEqual(registry.canonical_name("JN Chaney"), "J.N. Chaney")


class SavingSeveralNames(TempCaseMixin, unittest.TestCase):
    """What the review interface needs: all or nothing, and never blank."""

    CHANEY = "B00W2ZAK7E"
    MAGGERT = "B00EKN8RHG"

    def setUp(self) -> None:
        super().setUp()
        self.registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(self.registry.close)
        self.registry.learn(
            [Contributor(self.CHANEY, "JN Chaney"), Contributor(self.MAGGERT, "Terry Maggert")]
        )

    def test_several_names_are_written_together(self) -> None:
        count = self.registry.rename_many(
            {self.CHANEY: "J.N. Chaney", self.MAGGERT: "T. Maggert"}
        )
        self.assertEqual(count, 2)
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "J.N. Chaney")
        self.assertEqual(self.registry.canonical_of(self.MAGGERT), "T. Maggert")

    def test_saving_a_name_pins_it(self) -> None:
        self.registry.rename_many({self.CHANEY: "J.N. Chaney"})
        self.assertTrue(self.registry.is_pinned(self.CHANEY))

    def test_a_blank_name_writes_nothing_at_all(self) -> None:
        # All or nothing: the good row must not land either, or a failed save
        # would leave half the screen applied.
        with self.assertRaises(ValueError):
            self.registry.rename_many({self.CHANEY: "J.N. Chaney", self.MAGGERT: "  "})
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "JN Chaney")

    def test_an_unknown_identity_writes_nothing_at_all(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.rename_many(
                {self.CHANEY: "J.N. Chaney", "B000000000": "Nobody"}
            )
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "JN Chaney")

    def test_names_are_stripped(self) -> None:
        self.registry.rename_many({self.CHANEY: "  J.N. Chaney  "})
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "J.N. Chaney")

    def test_saving_nothing_is_allowed(self) -> None:
        self.assertEqual(self.registry.rename_many({}), 0)

    def test_book_counts_are_stored_and_reported(self) -> None:
        self.registry.set_book_counts({self.CHANEY: 55, self.MAGGERT: 9})
        rows = {row.identity: row for row in self.registry.review_rows()}
        self.assertEqual(rows[self.CHANEY].books, 55)
        self.assertEqual(rows[self.MAGGERT].books, 9)

    def test_a_review_row_carries_what_the_interface_shows(self) -> None:
        self.registry.learn([Contributor(self.CHANEY, "Chaney, J.N.")])
        self.registry.rename(self.MAGGERT, "Terry Maggert")
        rows = {row.identity: row for row in self.registry.review_rows()}

        chaney = rows[self.CHANEY]
        self.assertEqual(sorted(chaney.spellings), ["Chaney, J.N.", "JN Chaney"])
        self.assertEqual(chaney.asins, 1)
        self.assertTrue(chaney.respelled)
        self.assertTrue(chaney.has_asin)
        self.assertFalse(chaney.pinned)
        self.assertTrue(rows[self.MAGGERT].pinned)

    def test_an_author_with_no_asin_reports_as_much(self) -> None:
        self.registry.learn([Contributor(None, "Rhaegar")])
        row = [r for r in self.registry.review_rows() if r.canonical == "Rhaegar"][0]
        self.assertFalse(row.has_asin)
        self.assertEqual(row.asins, 0)

    def test_a_name_holding_a_comma_survives_the_round_trip(self) -> None:
        # Spellings are joined for one query, so the separator must not be a
        # comma: "Chaney, J.N." contains one.
        self.registry.learn([Contributor(self.CHANEY, "Chaney, J.N.")])
        row = [r for r in self.registry.review_rows() if r.identity == self.CHANEY][0]
        self.assertIn("Chaney, J.N.", row.spellings)

    def test_a_v4_registry_gains_the_count_column(self) -> None:
        import sqlite3

        path = self.root / "v4.sqlite3"
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            CREATE TABLE author (
                author_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL,
                canonical_pinned INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE author_asin (
                asin TEXT PRIMARY KEY, author_id TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE author_alias (
                name_key TEXT PRIMARY KEY, author_id TEXT NOT NULL,
                spelling TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE author_unknown (
                name_key TEXT PRIMARY KEY, spelling TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
            );
            INSERT INTO schema_info VALUES (4);
            INSERT INTO author VALUES
                ('B00W2ZAK7E', 'J.N. Chaney', 1, '2026-10-16T00:00:00+00:00');
            INSERT INTO author_alias VALUES
                ('jnchaney', 'B00W2ZAK7E', 'J.N. Chaney', 'audible',
                 '2026-10-16T00:00:00+00:00');
            """
        )
        connection.close()

        with AuthorRegistry(path) as registry:
            row = registry.review_rows()[0]
            self.assertEqual(row.books, 0)
            self.assertTrue(row.pinned)
            registry.set_book_counts({"B00W2ZAK7E": 7})
            self.assertEqual(registry.review_rows()[0].books, 7)

    def test_a_failed_transaction_leaves_nothing_behind(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.registry.transaction() as connection:
                connection.execute(
                    "UPDATE author SET canonical_name = 'Wrong' WHERE author_id = ?",
                    (self.CHANEY,),
                )
                raise RuntimeError("something went wrong halfway")
        self.assertEqual(self.registry.canonical_of(self.CHANEY), "JN Chaney")


class FoldingDuplicateIdentities(TempCaseMixin, unittest.TestCase):
    """A v2 registry had one identity per ASIN; one author can have several."""

    def v2_database(self):
        import sqlite3

        path = self.root / "authors.sqlite3"
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            CREATE TABLE author (
                author_id      TEXT PRIMARY KEY,
                audible_asin   TEXT,
                canonical_name TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE author_alias (
                name_key    TEXT PRIMARY KEY,
                author_id   TEXT NOT NULL REFERENCES author(author_id),
                spelling    TEXT NOT NULL,
                source      TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE author_unknown (
                name_key   TEXT PRIMARY KEY,
                spelling   TEXT NOT NULL,
                seen       INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            INSERT INTO schema_info VALUES (2);
            -- the live registry's shape: three Baldaccis, one reachable
            INSERT INTO author VALUES
                ('B0H8QD5X57', 'B0H8QD5X57', 'David Baldacci', '2026-10-16T14:20:58+00:00'),
                ('B0H12WK1DB', 'B0H12WK1DB', 'David Baldacci', '2026-10-16T14:22:09+00:00'),
                ('B000AQ0STC', 'B000AQ0STC', 'David Baldacci', '2026-10-16T14:22:25+00:00'),
                ('name:rhaegar', NULL, 'Rhaegar', '2026-10-16T14:39:53+00:00');
            INSERT INTO author_alias VALUES
                ('davidbaldacci', 'B0H8QD5X57', 'David Baldacci', 'audible',
                 '2026-10-16T14:20:58+00:00'),
                ('rhaegar', 'name:rhaegar', 'Rhaegar', 'audible-no-asin',
                 '2026-10-16T14:39:53+00:00');
            """
        )
        connection.close()
        return path

    def test_the_duplicates_become_one_author_with_three_asins(self) -> None:
        with AuthorRegistry(self.v2_database()) as registry:
            self.assertEqual(
                [name for _, name, _ in registry.identities()],
                ["David Baldacci", "Rhaegar"],
            )
            self.assertEqual(
                registry.asins_of("B0H8QD5X57"),
                ["B000AQ0STC", "B0H12WK1DB", "B0H8QD5X57"],
            )

    def test_nothing_is_left_unreachable(self) -> None:
        with AuthorRegistry(self.v2_database()) as registry:
            orphans = registry._connection.execute(
                "SELECT author_id FROM author "
                "WHERE author_id NOT IN (SELECT author_id FROM author_alias)"
            ).fetchall()
            self.assertEqual(orphans, [])

    def test_resolution_is_unchanged_by_the_fold(self) -> None:
        with AuthorRegistry(self.v2_database()) as registry:
            self.assertEqual(registry.canonical_name("david baldacci"), "David Baldacci")
            self.assertEqual(registry.identity("Rhaegar"), "name:rhaegar")

    def test_a_folded_asin_still_identifies_its_author(self) -> None:
        # A later book crediting the duplicate id must land on the same author.
        with AuthorRegistry(self.v2_database()) as registry:
            self.assertEqual(registry.identity_for_asin("B000AQ0STC"), "B0H8QD5X57")
            registry.learn([Contributor("B000AQ0STC", "Baldacci, David")])
            self.assertEqual(registry.identity("Baldacci, David"), "B0H8QD5X57")
            self.assertEqual(registry.stats()["authors"], 2)

    def test_an_author_with_no_asin_survives_the_fold(self) -> None:
        with AuthorRegistry(self.v2_database()) as registry:
            self.assertEqual(registry.asins_of("name:rhaegar"), [])
            self.assertEqual(registry.canonical_name("rhaegar"), "Rhaegar")

    def test_a_v1_registry_upgrades_all_the_way(self) -> None:
        # Both steps in one open: v1 -> v2 -> v3.
        import sqlite3

        path = self.root / "chained.sqlite3"
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            """
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            CREATE TABLE author (
                author_asin    TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE author_alias (
                name_key    TEXT PRIMARY KEY,
                author_asin TEXT NOT NULL REFERENCES author(author_asin),
                spelling    TEXT NOT NULL,
                source      TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE author_unknown (
                name_key TEXT PRIMARY KEY, spelling TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
            );
            INSERT INTO schema_info VALUES (1);
            INSERT INTO author VALUES
                ('B00W2ZAK7E', 'J.N. Chaney', '2026-10-16T00:00:00+00:00');
            INSERT INTO author_alias VALUES
                ('jnchaney', 'B00W2ZAK7E', 'J.N. Chaney', 'audible',
                 '2026-10-16T00:00:00+00:00');
            """
        )
        connection.close()

        with AuthorRegistry(path) as registry:
            self.assertEqual(registry.identity("JN Chaney"), "B00W2ZAK7E")
            self.assertEqual(registry.asins_of("B00W2ZAK7E"), ["B00W2ZAK7E"])
            row = registry._connection.execute(
                "SELECT version FROM schema_info"
            ).fetchone()
            self.assertEqual(row["version"], SCHEMA_VERSION)


class Registry(TempCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = AuthorRegistry(self.root / "authors.sqlite3")
        self.addCleanup(self.registry.close)

    def test_an_unknown_author_resolves_to_nothing(self) -> None:
        self.assertIsNone(self.registry.identity("J.N. Chaney"))
        self.assertIsNone(self.registry.canonical_name("J.N. Chaney"))

    def test_learning_one_spelling_resolves_the_others(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        for spelling in ("J. N. Chaney", "JN Chaney", "J N Chaney"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.registry.identity(spelling), CHANEY)
                self.assertEqual(self.registry.canonical_name(spelling), "J.N. Chaney")

    def test_the_first_canonical_name_wins(self) -> None:
        # Audible returns different display names for one ASIN depending on
        # which product is asked about; letting the latest win would make the
        # library rename itself at random.
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.assertEqual(self.registry.canonical_name("JN Chaney"), "J.N. Chaney")

    def test_a_second_spelling_is_recorded_as_an_alias(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "Chaney, J.N.")])
        self.assertEqual(
            sorted(self.registry.aliases_of(CHANEY)), ["Chaney, J.N.", "J.N. Chaney"]
        )

    def test_an_author_amazon_has_no_id_for_still_gets_an_identity(self) -> None:
        # Amazon holds no contributor record for a great many self-published
        # authors: the catalogue returns {"name": "Rhaegar"} and nothing else.
        # Without an identity of their own, accepting a lookup for one of their
        # books recorded nothing, so triage went on flagging the author as new
        # however many times it was looked up.
        self.registry.learn([Contributor(None, "Rhaegar")])
        self.assertEqual(self.registry.identity("Rhaegar"), "name:rhaegar")
        self.assertEqual(self.registry.canonical_name("rhaegar"), "Rhaegar")

    def test_a_name_identity_is_marked_as_the_weaker_kind(self) -> None:
        from forge.authors import SOURCE_AUDIBLE_NO_ASIN

        self.registry.learn([Contributor(None, "X-RHODEN-X")])
        row = self.registry._connection.execute(
            "SELECT source FROM author_alias WHERE name_key = ?", ("xrhodenx",)
        ).fetchone()
        self.assertEqual(row["source"], SOURCE_AUDIBLE_NO_ASIN)
        # An author with no ASIN has no row in the ASIN table at all.
        self.assertEqual(self.registry.asins_of("name:xrhodenx"), [])

    def test_an_accented_name_keys_the_same_either_way(self) -> None:
        # Amazon writes this author's surname both ways; the tag on the file
        # has the accent and other places do not.
        self.registry.learn([Contributor(None, "Mira González")])
        self.assertEqual(
            self.registry.identity("Mira Gonzalez"), self.registry.identity("Mira González")
        )

    def test_a_contributor_with_no_name_at_all_is_ignored(self) -> None:
        self.registry.learn([Contributor(None, "   ")])
        self.assertEqual(self.registry.stats()["authors"], 0)

    def test_resolving_a_tag_keeps_the_billing_order(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )
        self.assertEqual(
            self.registry.resolve("Terry Maggert, J N Chaney").canonical,
            "Terry Maggert, J.N. Chaney",
        )

    def test_an_unknown_contributor_is_left_exactly_as_it_was(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        outcome = self.registry.resolve("JN Chaney, Some Newcomer")
        self.assertEqual(outcome.canonical, "J.N. Chaney, Some Newcomer")
        self.assertEqual(outcome.unknown, ("Some Newcomer",))
        self.assertFalse(outcome.complete)

    def test_a_tag_that_needs_nothing_reports_no_change(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.assertIsNone(self.registry.resolve("J.N. Chaney").canonical)

    def test_one_alias_row_covers_every_spelling_of_the_same_key(self) -> None:
        # "J. N. CHANEY" in a tag needs no second row: it reduces to the key
        # the contributor Audible named is already stored under.
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.assertEqual(self.registry.aliases_of(CHANEY), ["J.N. Chaney"])
        self.assertEqual(self.registry.canonical_name("J. N. CHANEY"), "J.N. Chaney")

    def test_linking_by_hand_settles_the_missing_initial_case(self) -> None:
        self.registry.learn([Contributor(MORRIS, "Brandon Q. Morris")])
        self.assertIsNone(self.registry.identity("Brandon Morris"))

        self.registry.link("Brandon Morris", MORRIS)
        self.assertEqual(self.registry.identity("Brandon Morris"), MORRIS)
        self.assertEqual(
            self.registry.canonical_name("Brandon Morris"), "Brandon Q. Morris"
        )

    def test_linking_can_create_an_identity_with_a_name(self) -> None:
        self.registry.link("Some Author", "B000000000", canonical_name="Some Author")
        self.assertEqual(self.registry.canonical_name("some author"), "Some Author")

    def test_linking_an_unknown_asin_without_a_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.link("Some Author", "B000000000")

    def test_a_manual_link_overrides_an_earlier_one(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MORRIS, "Brandon Q. Morris")]
        )
        self.registry.link("Mystery Name", CHANEY)
        self.registry.link("Mystery Name", MORRIS)
        self.assertEqual(self.registry.identity("Mystery Name"), MORRIS)

    def test_renaming_changes_every_spelling_at_once(self) -> None:
        self.registry.learn([Contributor(CHANEY, "JN Chaney")])
        self.registry.rename(CHANEY, "J. N. Chaney")
        self.assertEqual(self.registry.canonical_name("J.N. Chaney"), "J. N. Chaney")

    def test_unplaced_spellings_are_remembered_with_a_count(self) -> None:
        self.registry.note_unknown(["A Newcomer"])
        self.registry.note_unknown(["A Newcomer", "Another One"])
        self.assertEqual(
            self.registry.unknown_names(), [("A Newcomer", 2), ("Another One", 1)]
        )

    def test_an_identified_author_leaves_the_unplaced_list(self) -> None:
        self.registry.note_unknown(["J.N. Chaney"])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.assertEqual(self.registry.unknown_names(), [])

    def test_note_unknown_ignores_names_it_already_knows(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.note_unknown(["JN Chaney"])
        self.assertEqual(self.registry.unknown_names(), [])

    def test_the_registry_survives_being_reopened(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.close()
        with AuthorRegistry(self.root / "authors.sqlite3") as reopened:
            self.assertEqual(reopened.canonical_name("J N Chaney"), "J.N. Chaney")

    def test_stats_count_what_is_there(self) -> None:
        self.registry.learn(
            [Contributor(CHANEY, "J.N. Chaney"), Contributor(MAGGERT, "Terry Maggert")]
        )
        self.registry.learn([Contributor(CHANEY, "Chaney, J.N.")])
        self.registry.note_unknown(["A Newcomer"])
        self.assertEqual(
            self.registry.stats(),
            {"authors": 2, "aliases": 3, "asins": 2, "unknown": 1},
        )

    def test_a_second_asin_for_one_author_joins_the_identity_it_has(self) -> None:
        # Amazon keeps three contributor records for David Baldacci; three
        # lookups of his books used to make three identities, two of them with
        # nothing pointing at them.
        for asin in ("B0H8QD5X57", "B0H12WK1DB", "B000AQ0STC"):
            self.registry.learn([Contributor(asin, "David Baldacci")])

        self.assertEqual(self.registry.stats()["authors"], 1)
        self.assertEqual(self.registry.identity("David Baldacci"), "B0H8QD5X57")
        self.assertEqual(
            self.registry.asins_of("B0H8QD5X57"),
            ["B000AQ0STC", "B0H12WK1DB", "B0H8QD5X57"],
        )

    def test_every_identity_is_reachable_through_a_spelling(self) -> None:
        for asin in ("B0H8QD5X57", "B0H12WK1DB"):
            self.registry.learn([Contributor(asin, "David Baldacci")])
        orphans = self.registry._connection.execute(
            "SELECT author_id FROM author "
            "WHERE author_id NOT IN (SELECT author_id FROM author_alias)"
        ).fetchall()
        self.assertEqual(orphans, [])

    def test_an_asin_already_known_identifies_a_new_spelling(self) -> None:
        # The other direction: same person, same ASIN, a name that reduces to a
        # key the registry has not seen.
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "Chaney, J.N.")])

        self.assertEqual(self.registry.stats()["authors"], 1)
        self.assertEqual(self.registry.identity("Chaney, J.N."), CHANEY)
        # ...and it answers with the name it was first given.
        self.assertEqual(self.registry.canonical_name("Chaney, J.N."), "J.N. Chaney")

    def test_an_asin_belongs_to_one_identity_only(self) -> None:
        self.registry.learn([Contributor(MORRIS, "Brandon Q. Morris")])
        self.assertEqual(self.registry.identity_for_asin(MORRIS), MORRIS)
        self.assertIsNone(self.registry.identity_for_asin("B000000000"))

    def test_identities_report_the_most_respelled_author_first(self) -> None:
        self.registry.learn([Contributor(CHANEY, "J.N. Chaney")])
        self.registry.learn([Contributor(CHANEY, "J. N. Chaney")])
        self.registry.learn([Contributor(MAGGERT, "Terry Maggert")])
        self.assertEqual(self.registry.identities()[0][0], CHANEY)


if __name__ == "__main__":
    unittest.main()
