"""Asking Audiobookshelf whether a book is already shelved.

No test here touches a real server: a fake session answers the two endpoints
the client uses, which is enough to pin the behaviour that matters — an exact
ASIN match, and a failure that reports itself as a failure rather than as "not
a duplicate".
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.helpers import TempCaseMixin, write_config

import requests

from forge import config as config_mod
from forge import library
from forge.library import AudiobookshelfClient, LibraryError


LIBRARIES = {
    "libraries": [
        {"id": "lib-books", "mediaType": "book", "name": "Library"},
        {"id": "lib-pods", "mediaType": "podcast", "name": "Podcasts"},
    ]
}


def item(asin, title, author="An Author", series="", path="/audiobooks/x"):
    return {
        "id": f"id-{asin}",
        "path": path,
        "media": {
            "metadata": {
                "asin": asin,
                "title": title,
                "authorName": author,
                "seriesName": series,
            }
        },
    }


ITEMS = {
    "results": [
        item("B0892RSS31", "The Elementary Forms of the Religious Life", "Émile Durkheim"),
        item("B0CMYXVB65", "Ironclads", "Adrian Tchaikovsky"),
        item(None, "A book with no ASIN"),
    ]
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.url = "http://abs.invalid/api"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Answers the two endpoints the client uses, and counts the calls."""

    def __init__(self, *, items=ITEMS, status=200):
        self.items = items
        self.status = status
        self.calls: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)
        if self.status != 200:
            return FakeResponse({}, self.status)
        if url.endswith("/api/libraries"):
            return FakeResponse(LIBRARIES)
        if "/items" in url:
            return FakeResponse(self.items)
        return FakeResponse({}, 404)

    def close(self) -> None:
        pass


def client(session=None, **kwargs):
    return AudiobookshelfClient(
        "http://abs.invalid/", "secret", session=session or FakeSession(), **kwargs
    )


class AsinMatching(unittest.TestCase):
    def test_a_shelved_asin_is_a_duplicate(self) -> None:
        result = client().check_asin("B0CMYXVB65")
        self.assertTrue(result.is_duplicate)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].title, "Ironclads")
        self.assertEqual(result.matches[0].author, "Adrian Tchaikovsky")

    def test_an_unknown_asin_is_not_a_duplicate(self) -> None:
        result = client().check_asin("B000000000")
        self.assertEqual(result.status, library.UNIQUE)
        self.assertFalse(result.is_duplicate)
        self.assertTrue(result.checked)

    def test_the_comparison_ignores_case_and_space(self) -> None:
        self.assertTrue(client().check_asin("  b0cmyxvb65 ").is_duplicate)

    def test_a_missing_asin_is_reported_as_unchecked(self) -> None:
        # Not "unique": saying a book is new when it was never looked up is
        # the one wrong answer that could cost a library a book.
        for value in (None, "", "   "):
            result = client().check_asin(value)
            self.assertEqual(result.status, library.NO_ASIN)
            self.assertFalse(result.checked)
            self.assertFalse(result.is_duplicate)

    def test_a_partial_asin_does_not_match(self) -> None:
        self.assertEqual(client().check_asin("B0CMYX").status, library.UNIQUE)

    def test_items_without_an_asin_are_left_out_of_the_index(self) -> None:
        index = client().index()
        self.assertNotIn("", index)
        self.assertEqual(len(index), 2)


class Indexing(unittest.TestCase):
    def test_the_library_is_fetched_once_and_reused(self) -> None:
        session = FakeSession()
        abs_client = client(session)
        for asin in ("B0CMYXVB65", "B0892RSS31", "nope"):
            abs_client.check_asin(asin)
        item_calls = [url for url in session.calls if "/items" in url]
        self.assertEqual(len(item_calls), 1, session.calls)

    def test_a_refresh_fetches_again(self) -> None:
        session = FakeSession()
        abs_client = client(session)
        abs_client.index()
        abs_client.index(refresh=True)
        self.assertEqual(len([u for u in session.calls if "/items" in u]), 2)

    def test_only_book_libraries_are_indexed(self) -> None:
        session = FakeSession()
        client(session).index()
        self.assertIn("/api/libraries/lib-books/items", " ".join(session.calls))
        self.assertNotIn("lib-pods", " ".join(session.calls))

    def test_a_configured_library_id_skips_the_library_listing(self) -> None:
        session = FakeSession()
        client(session, library_id="lib-books").index()
        self.assertNotIn("http://abs.invalid/api/libraries", session.calls)

    def test_indexed_reports_whether_a_check_will_be_instant(self) -> None:
        abs_client = client()
        self.assertFalse(abs_client.indexed)
        abs_client.check_asin("B0CMYXVB65")
        self.assertTrue(abs_client.indexed)


class Failures(unittest.TestCase):
    def test_a_rejected_key_is_an_error_not_a_clean_bill(self) -> None:
        result = client(FakeSession(status=401)).check_asin("B0CMYXVB65")
        self.assertEqual(result.status, library.ERROR)
        self.assertFalse(result.checked)
        self.assertIn("API key", result.message)

    def test_an_unreachable_server_is_an_error(self) -> None:
        session = FakeSession()
        session.get = mock.Mock(side_effect=requests.ConnectionError("no route"))
        result = client(session).check_asin("B0CMYXVB65")
        self.assertEqual(result.status, library.ERROR)
        self.assertIn("could not reach", result.message)

    def test_a_server_error_is_an_error(self) -> None:
        result = client(FakeSession(status=503)).check_asin("B0CMYXVB65")
        self.assertEqual(result.status, library.ERROR)

    def test_building_a_client_without_credentials_is_refused(self) -> None:
        with self.assertRaises(LibraryError):
            AudiobookshelfClient("", "", session=FakeSession())
        with self.assertRaises(LibraryError):
            AudiobookshelfClient("http://abs.invalid", "", session=FakeSession())

    def test_a_failed_index_is_not_cached_as_an_empty_library(self) -> None:
        # Otherwise one network blip would report every book as new for the
        # rest of the session.
        session = FakeSession(status=503)
        abs_client = client(session)
        self.assertEqual(abs_client.check_asin("B0CMYXVB65").status, library.ERROR)
        session.status = 200
        self.assertTrue(abs_client.check_asin("B0CMYXVB65").is_duplicate)


class CheckingManyBooks(unittest.TestCase):
    class Book:
        def __init__(self, key, asin):
            self.key = key
            self.metadata = mock.Mock(asin=asin)

    def test_every_book_gets_an_answer_including_the_ones_without_an_asin(self) -> None:
        books = [
            self.Book("k1", "B0CMYXVB65"),
            self.Book("k2", None),
            self.Book("k3", "B000000000"),
        ]
        results = library.check_books(client(), books)
        self.assertEqual(results["k1"].status, library.DUPLICATE)
        self.assertEqual(results["k2"].status, library.NO_ASIN)
        self.assertEqual(results["k3"].status, library.UNIQUE)


class CredentialsFromConfig(TempCaseMixin, unittest.TestCase):
    def test_an_env_file_beside_the_config_is_read(self) -> None:
        path = write_config(self.root)
        (self.root / "forge.env").write_text(
            'AUDIOBOOKSHELF_URL = "http://abs.invalid"\n'
            "AUDIOBOOKSHELF_API_KEY = secret-key\n"
            '# a comment\n'
            'AUDIOBOOKSHELF_LIBRARY_ID = "lib-books"\n'
        )
        with mock.patch.dict("os.environ", {"FORGE_ENV": str(self.root / "forge.env")}):
            config = config_mod.load(path)
        self.assertTrue(config.has_library)
        self.assertEqual(config.abs_url, "http://abs.invalid")
        self.assertEqual(config.abs_api_key, "secret-key")
        self.assertEqual(config.abs_library_id, "lib-books")

    def test_the_ini_wins_over_the_env_file(self) -> None:
        path = write_config(self.root)
        path.write_text(
            path.read_text()
            + "\n[audiobookshelf]\nurl = http://from-ini\napi_key = ini-key\n"
        )
        (self.root / "forge.env").write_text(
            'AUDIOBOOKSHELF_URL = "http://from-env"\nAUDIOBOOKSHELF_API_KEY = env-key\n'
        )
        with mock.patch.dict("os.environ", {"FORGE_ENV": str(self.root / "forge.env")}):
            config = config_mod.load(path)
        self.assertEqual(config.abs_url, "http://from-ini")
        self.assertEqual(config.abs_api_key, "ini-key")

    def test_no_credentials_anywhere_is_not_an_error(self) -> None:
        # Everything except the duplicate check works without a library.
        config = config_mod.load(write_config(self.root))
        self.assertFalse(config.has_library)
        self.assertIsNone(config.abs_url)

    def test_forge_env_is_authoritative_when_set(self) -> None:
        # A FORGE_ENV that names a file which does not exist must not fall
        # through to the credentials of whatever other .env is lying around.
        path = write_config(self.root)
        (self.root / ".env").write_text('AUDIOBOOKSHELF_URL = "http://nearby"\n')
        with mock.patch.dict("os.environ", {"FORGE_ENV": str(self.root / "nothing.env")}):
            config = config_mod.load(path)
        self.assertIsNone(config.abs_url)


if __name__ == "__main__":
    unittest.main()
