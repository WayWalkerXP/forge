"""Asking Audiobookshelf whether a book is already in the library.

The question triage needs answered is narrow: *is this exact edition already
shelved?*  Only the ASIN can answer that honestly — titles collide across
editions, abridgements and re-releases — so a book without an ASIN is reported
as unchecked rather than as unique.  Saying "not a duplicate" when the truth is
"could not tell" is the one answer that could cost a library a book.

The whole library index is fetched once per session and reused.  Audiobookshelf
also has a search endpoint, which is faster, but it is a fuzzy text search: a
book it fails to surface would look unique, and that is the wrong direction to
be wrong in.  One 5-second fetch buys an exact, local answer for every book
afterwards.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

import requests

from .errors import ForgeError

LOGGER = logging.getLogger(__name__)

#: How long a fetched index stays usable before it is fetched again.  Long
#: enough that a triage session fetches once; short enough that a book shelved
#: an hour ago is seen.
INDEX_TTL_SECONDS = 900.0

DEFAULT_TIMEOUT = 30.0


class LibraryError(ForgeError):
    """Audiobookshelf could not be reached, or refused the request."""


def normalize_asin(value: str | None) -> str:
    """ASINs are compared case-insensitively and without surrounding space."""

    return (value or "").strip().casefold()


@dataclass(frozen=True)
class LibraryItem:
    """One book already in the Audiobookshelf library."""

    id: str
    asin: str
    title: str
    author: str
    series: str
    path: str

    @property
    def summary(self) -> str:
        parts = [self.title or "(untitled)"]
        if self.author:
            parts.append(f"by {self.author}")
        if self.series:
            parts.append(f"[{self.series}]")
        return " ".join(parts)


# Result statuses.  Strings rather than an enum: they are shown to the user and
# written into log lines, and nothing branches on them outside this module and
# the interface.
DUPLICATE = "duplicate"
UNIQUE = "unique"
NO_ASIN = "no-asin"
ERROR = "error"


@dataclass(frozen=True)
class DuplicateCheck:
    """What the library had to say about one book."""

    status: str
    asin: str | None = None
    matches: tuple[LibraryItem, ...] = ()
    message: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.status == DUPLICATE

    @property
    def checked(self) -> bool:
        """True when the library actually answered."""

        return self.status in (DUPLICATE, UNIQUE)

    @property
    def label(self) -> str:
        """The short form shown in the book list."""

        return {
            DUPLICATE: "dup",
            UNIQUE: "new",
            NO_ASIN: "—",
            ERROR: "err",
        }.get(self.status, "?")

    def describe(self) -> str:
        if self.status == DUPLICATE:
            first = self.matches[0].summary if self.matches else ""
            extra = f" (+{len(self.matches) - 1} more)" if len(self.matches) > 1 else ""
            return f"already in the library: {first}{extra}"
        if self.status == UNIQUE:
            return f"not in the library (ASIN {self.asin})"
        if self.status == NO_ASIN:
            return "no ASIN, so the library cannot be checked"
        return self.message or "the library could not be checked"


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media")
    if isinstance(media, dict) and isinstance(media.get("metadata"), dict):
        return media["metadata"]
    return {}


def _author_of(meta: dict[str, Any]) -> str:
    name = meta.get("authorName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    authors = meta.get("authors")
    if isinstance(authors, list):
        names = [
            (entry.get("name") if isinstance(entry, dict) else entry) or ""
            for entry in authors
        ]
        return ", ".join(str(n).strip() for n in names if str(n).strip())
    return ""


def _as_item(raw: dict[str, Any]) -> LibraryItem | None:
    meta = _metadata(raw)
    asin = meta.get("asin") or raw.get("asin")
    if not isinstance(asin, str) or not asin.strip():
        return None
    return LibraryItem(
        id=str(raw.get("id") or ""),
        asin=asin.strip(),
        title=str(meta.get("title") or raw.get("title") or ""),
        author=_author_of(meta),
        series=str(meta.get("seriesName") or ""),
        path=str(raw.get("path") or raw.get("relPath") or ""),
    )


class AudiobookshelfClient:
    """A read-only view of an Audiobookshelf library.

    Read-only on purpose.  ``forge shelve`` moves files into the library's
    directory tree and lets Audiobookshelf notice them on its own schedule;
    nothing here writes to the server.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        library_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: Any | None = None,
    ) -> None:
        if not url or not api_key:
            raise LibraryError(
                "Audiobookshelf is not configured: set url and api_key in the "
                "[audiobookshelf] section, or AUDIOBOOKSHELF_URL and "
                "AUDIOBOOKSHELF_API_KEY in forge's .env file"
            )
        self.url = url.rstrip("/") + "/"
        self.api_key = api_key
        self.library_id = (library_id or "").strip() or None
        self.timeout = timeout
        self._session = session or requests.Session()
        self._owns_session = session is None
        # The index is shared between the interface thread and the workers
        # checking books, so building it is guarded.
        self._lock = threading.Lock()
        self._index: dict[str, list[LibraryItem]] | None = None
        self._fetched_at = 0.0

    # ------------------------------------------------------------- plumbing #

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "AudiobookshelfClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        # Bearer is what current Audiobookshelf wants; x-api-key is kept for
        # older servers, which ignore the header they do not know.
        return {"Authorization": f"Bearer {self.api_key}", "x-api-key": self.api_key}

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        target = urljoin(self.url, endpoint.lstrip("/"))
        try:
            response = self._session.get(
                target, headers=self._headers(), params=params or {}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LibraryError(f"could not reach Audiobookshelf at {self.url}: {exc}") from exc

        if response.status_code in (401, 403):
            raise LibraryError("Audiobookshelf rejected the API key")
        if response.status_code == 404:
            raise LibraryError(f"Audiobookshelf has no endpoint at {endpoint}")
        if response.status_code >= 500:
            raise LibraryError(
                f"Audiobookshelf returned {response.status_code}; the server may be busy"
            )
        try:
            response.raise_for_status()
            return response.json()
        except ValueError as exc:
            raise LibraryError(f"Audiobookshelf sent a reply forge could not read: {exc}") from exc
        except requests.RequestException as exc:
            raise LibraryError(f"Audiobookshelf request failed: {exc}") from exc

    # ---------------------------------------------------------------- index #

    def library_ids(self) -> list[str]:
        """The book libraries to search, or the one that was configured."""

        if self.library_id:
            return [self.library_id]
        payload = self._get("/api/libraries")
        libraries = payload.get("libraries") if isinstance(payload, dict) else payload
        if not isinstance(libraries, list):
            raise LibraryError("Audiobookshelf did not list any libraries")
        ids = [
            library["id"]
            for library in libraries
            if isinstance(library, dict)
            and isinstance(library.get("id"), str)
            and library.get("mediaType") in (None, "book")
        ]
        if not ids:
            raise LibraryError("Audiobookshelf has no book libraries")
        return ids

    def _fetch_items(self) -> list[LibraryItem]:
        items: list[LibraryItem] = []
        for library_id in self.library_ids():
            # limit=0 is "everything"; minified keeps the payload to the
            # metadata, which is all that is compared here.
            payload = self._get(
                f"/api/libraries/{library_id}/items",
                params={"limit": 0, "minified": 1},
            )
            results = payload.get("results") if isinstance(payload, dict) else payload
            if not isinstance(results, list):
                continue
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                item = _as_item(raw)
                if item is not None:
                    items.append(item)
        return items

    def index(self, *, refresh: bool = False) -> dict[str, list[LibraryItem]]:
        """ASIN → library items, fetched once and reused."""

        with self._lock:
            fresh = (
                self._index is not None
                and time.monotonic() - self._fetched_at < INDEX_TTL_SECONDS
            )
            if fresh and not refresh:
                return self._index  # type: ignore[return-value]

            index: dict[str, list[LibraryItem]] = {}
            for item in self._fetch_items():
                index.setdefault(normalize_asin(item.asin), []).append(item)
            self._index = index
            self._fetched_at = time.monotonic()
            LOGGER.info("indexed %d Audiobookshelf book(s) by ASIN", len(index))
            return index

    @property
    def indexed(self) -> bool:
        """True when a later check will answer without a network round trip."""

        return self._index is not None

    # --------------------------------------------------------------- checks #

    def find_by_asin(self, asin: str) -> list[LibraryItem]:
        normalized = normalize_asin(asin)
        if not normalized:
            return []
        return list(self.index().get(normalized, ()))

    def check_asin(self, asin: str | None) -> DuplicateCheck:
        """Answer for one ASIN, never raising: the status carries the failure."""

        if not normalize_asin(asin):
            return DuplicateCheck(status=NO_ASIN)
        try:
            matches = self.find_by_asin(asin or "")
        except LibraryError as exc:
            LOGGER.warning("duplicate check failed for %s: %s", asin, exc)
            return DuplicateCheck(status=ERROR, asin=asin, message=str(exc))
        if matches:
            return DuplicateCheck(status=DUPLICATE, asin=asin, matches=tuple(matches))
        return DuplicateCheck(status=UNIQUE, asin=asin)


def check_books(client: AudiobookshelfClient, books: Iterable[Any]) -> dict[str, DuplicateCheck]:
    """Check every book that has an ASIN, keyed by :attr:`Book.key`.

    Books without an ASIN are included with a ``no-asin`` status rather than
    left out, so a caller can tell "checked and clear" from "never asked".
    """

    results: dict[str, DuplicateCheck] = {}
    for book in books:
        meta = getattr(book, "metadata", None)
        asin = getattr(meta, "asin", None) if meta is not None else None
        results[book.key] = client.check_asin(asin)
    return results
