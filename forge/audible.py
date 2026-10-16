"""Looking books up in Audible's public catalogue.

Two calls, because the API only returns the full picture for a known ASIN:

*   a keyword search, which returns candidates;
*   an ASIN lookup, which returns genres, large cover art and full details.

Hence the two-step flow in triage: search, pick, then fetch.

Unlike the client this replaces, requests here retry with backoff and honour
``Retry-After``.  Triage makes it easy to fire many lookups in a row, and the
catalogue endpoint does rate-limit.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

from .errors import ForgeError

LOGGER = logging.getLogger(__name__)

#: Audible marketplaces.  The GUI this replaces hardcoded the US domain.
REGIONS = {
    "us": "api.audible.com",
    "uk": "api.audible.co.uk",
    "de": "api.audible.de",
    "fr": "api.audible.fr",
    "ca": "api.audible.ca",
    "au": "api.audible.com.au",
    "jp": "api.audible.co.jp",
    "it": "api.audible.it",
    "es": "api.audible.es",
    "in": "api.audible.in",
}

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

_SEARCH_GROUPS = "contributors,media,product_desc,product_attrs,product_extended_attrs,series"
_ASIN_GROUPS = (
    "category_ladders,contributors,media,product_desc,product_attrs,"
    "product_extended_attrs,rating,series,product_details"
)

#: Trailing "- cd 3" / "part 2" / "disc 1" noise that ruins a keyword search.
_TRAILING_PART_RE = re.compile(
    r"[\s\-_,(\[]*\b(?:cd|disc|disk|part|pt|vol|volume|book|tape)\b[\s.\-_#]*\d+\s*[)\]]*\s*$",
    re.IGNORECASE,
)
_NARRATED_BY_RE = re.compile(r"\s*[\(\[]\s*narrated by[^)\]]*[\)\]]\s*$", re.IGNORECASE)
_UNABRIDGED_RE = re.compile(r"\s*[\(\[]\s*(?:un)?abridged\s*[\)\]]\s*$", re.IGNORECASE)


class AudibleError(ForgeError):
    """An Audible lookup failed."""


@dataclass
class AudibleMetadata:
    """Normalised metadata from an Audible product."""

    asin: str | None = None
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    narrator: str | None = None
    series: str | None = None
    series_sequence: str | None = None
    publisher: str | None = None
    copyright: str | None = None
    published_date: str | None = None
    published_year: str | None = None
    language: str | None = None
    description: str | None = None
    genres: list[str] = field(default_factory=list)
    cover_url: str | None = None
    #: Runtime in seconds.  Used for match confidence only, never written.
    duration: int | None = None
    explicit: bool | None = None

    def to_updates(self) -> dict[str, Any]:
        """The subset of fields that are written to tags.

        ``title`` maps to both ``title`` and ``album``: audiobook tooling reads
        the book name from the album tag, while ``title`` is the track name.
        """

        updates: dict[str, Any] = {}
        for name in (
            "subtitle",
            "author",
            "narrator",
            "series",
            "series_sequence",
            "asin",
            "description",
            "publisher",
            "copyright",
            "published_date",
            "published_year",
            "language",
            "genres",
            "explicit",
        ):
            value = getattr(self, name)
            if value not in (None, [], ""):
                updates[name] = value
        if self.title:
            updates["title"] = self.title
            updates["album"] = self.title
        if self.author:
            updates["albumartist"] = self.author
        return updates


@dataclass
class SearchResult:
    """One candidate from a keyword search."""

    asin: str
    title: str
    author: str
    narrator: str
    series: str
    runtime_minutes: int | None
    product: dict[str, Any]

    def runtime_delta_minutes(self, source_seconds: float | None) -> int | None:
        """How far this candidate's runtime is from the file's, in minutes."""

        if not source_seconds or self.runtime_minutes is None:
            return None
        return abs(round(source_seconds / 60) - self.runtime_minutes)

    def confidence(self, source_seconds: float | None) -> str:
        """``high`` / ``medium`` / ``low`` / ``unknown`` from runtime closeness.

        Runtime is the only reliable automatic signal: titles and authors are
        spelled inconsistently, but a book's length is a fact.  An exact runtime
        match is strong evidence; a 30-minute gap usually means a different
        edition or an abridgement.
        """

        delta = self.runtime_delta_minutes(source_seconds)
        if delta is None:
            return "unknown"
        if delta <= 5:
            return "high"
        if delta < 15:
            return "medium"
        return "low"


def normalize_asin(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().upper() or None


def validate_asin(value: str | None) -> str:
    asin = normalize_asin(value)
    if not asin or not ASIN_RE.match(asin):
        raise AudibleError("an ASIN must be exactly 10 letters or digits")
    return asin


def clean_search_title(title: str) -> str:
    """Strip the noise that stops a filename-derived title from matching."""

    text = str(title).strip()
    for pattern in (_NARRATED_BY_RE, _UNABRIDGED_RE, _TRAILING_PART_RE):
        text = pattern.sub("", text).strip()
    return text or str(title).strip()


class AudibleClient:
    """A thin, polite client for the public catalogue API."""

    def __init__(
        self,
        *,
        region: str = "us",
        timeout: float = 20.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        min_interval: float = 0.34,
    ) -> None:
        if region not in REGIONS:
            raise AudibleError(
                f"unknown region {region!r}; choose from {', '.join(sorted(REGIONS))}"
            )
        self.region = region
        self.host = REGIONS[region]
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "forge/0.1 (+audiobook tagger)")
        self._last_request = 0.0

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "AudibleClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- public #

    def search(
        self, *, title: str, author: str | None = None, limit: int = 25
    ) -> list[SearchResult]:
        """Search the catalogue by title, optionally narrowed by author."""

        cleaned = clean_search_title(title)
        if not cleaned:
            raise AudibleError("a title is required to search")

        keywords = f"{author} {cleaned}".strip() if author else cleaned
        payload = self._get(
            "/1.0/catalog/products",
            {
                "response_groups": _SEARCH_GROUPS,
                "image_sizes": "500",
                "num_results": max(1, min(limit, 50)),
                "products_sort_by": "Relevance",
                "keywords": keywords,
            },
        )

        results = [_search_result(product) for product in payload.get("products", []) or []]
        return [result for result in results if result is not None]  # type: ignore[misc]

    def lookup(self, asin: str) -> AudibleMetadata:
        """Fetch full details for a known ASIN."""

        checked = validate_asin(asin)
        payload = self._get(
            f"/1.0/catalog/products/{checked}",
            {"response_groups": _ASIN_GROUPS, "image_sizes": "1000,500"},
        )
        product = payload.get("product")
        if not isinstance(product, dict) or not product:
            raise AudibleError(f"Audible returned no product for {checked}")

        metadata = normalize_product(product)
        if not metadata.title:
            # A delisted ASIN still returns a product, but one carrying only an
            # asin and an empty rating block.  Treating that as a successful
            # lookup would write an ASIN and nothing else over good local tags.
            raise AudibleError(
                f"{checked} exists in the catalogue but has no title or author "
                f"details; it has probably been delisted, so try a title search"
            )
        return metadata

    def contributors(self, asin: str) -> tuple["Contributor", ...]:
        """The authors of a book, with the ASIN Audible identifies each one by.

        A separate call from :meth:`lookup` because it asks for one response
        group rather than nine, and because identity resolution wants it for
        books whose tags are otherwise fine and must not be overwritten.

        The ASIN is the point.  Audible's own display name for one author
        varies between products - the same B00W2ZAK7E comes back as both
        "J.N. Chaney" and "J. N. Chaney" - while the ASIN never does.
        """

        from .authors import Contributor

        checked = validate_asin(asin)
        payload = self._get(
            f"/1.0/catalog/products/{checked}", {"response_groups": "contributors"}
        )
        product = payload.get("product")
        if not isinstance(product, dict):
            raise AudibleError(f"Audible returned no product for {checked}")

        found: list[Contributor] = []
        for entry in product.get("authors") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            author_asin = str(entry.get("asin") or "").strip()
            # A name with no ASIN carries no identity, so it is no use here.
            if name and author_asin:
                found.append(Contributor(asin=author_asin, name=name))
        return tuple(found)

    def search_url(self, *, title: str, author: str | None = None) -> str:
        keywords = f"{author} {clean_search_title(title)}".strip() if author else clean_search_title(title)
        return f"https://{self.host}/1.0/catalog/products?{urlencode({'keywords': keywords})}"

    # --------------------------------------------------------------- private #

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://{self.host}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                self._sleep_before_retry(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = AudibleError(
                    f"Audible returned HTTP {response.status_code} for {path}"
                )
                if attempt == self.max_retries:
                    break
                self._sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code == 404:
                raise AudibleError(f"Audible has no record at {path}")
            if not response.ok:
                raise AudibleError(
                    f"Audible returned HTTP {response.status_code} for {path}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise AudibleError("Audible returned a response that was not JSON") from exc

        raise AudibleError(f"Audible lookup failed after {self.max_retries} attempts: {last_error}")

    def _throttle(self) -> None:
        """Keep a minimum gap between requests."""

        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def _sleep_before_retry(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(30.0, float(retry_after)))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))


# --------------------------------------------------------------------------- #
# Mapping Audible's payload onto forge's fields
# --------------------------------------------------------------------------- #


def normalize_product(product: dict[str, Any]) -> AudibleMetadata:
    """Convert one Audible product dict into :class:`AudibleMetadata`."""

    runtime_minutes = _as_int(product.get("runtime_length_min"))
    series, sequence = _series(product)

    return AudibleMetadata(
        asin=normalize_asin(product.get("asin")),
        title=_clean_title(product.get("title")),
        subtitle=_text(product.get("subtitle")),
        author=_names(product.get("authors")),
        narrator=_names(product.get("narrators")),
        series=series,
        series_sequence=sequence,
        publisher=_text(product.get("publisher_name")),
        copyright=_text(product.get("copyright")),
        published_date=_text(product.get("release_date")),
        published_year=(_text(product.get("release_date")) or "")[:4] or None,
        language=_text(product.get("language")),
        description=_description(product),
        genres=_genres(product),
        cover_url=_cover(product),
        duration=runtime_minutes * 60 if runtime_minutes else None,
        explicit=_as_bool(product.get("is_adult_product")),
    )


def _search_result(product: dict[str, Any]) -> SearchResult | None:
    asin = normalize_asin(product.get("asin"))
    if not asin:
        return None
    series, sequence = _series(product)
    return SearchResult(
        asin=asin,
        title=_clean_title(product.get("title")) or "(untitled)",
        author=_names(product.get("authors")) or "",
        narrator=_names(product.get("narrators")) or "",
        series=f"{series} #{sequence}" if series and sequence else (series or ""),
        runtime_minutes=_as_int(product.get("runtime_length_min")),
        product=product,
    )


def rank_by_runtime(results: list[SearchResult], source_seconds: float | None) -> list[SearchResult]:
    """Sort candidates by how closely their runtime matches the file.

    Candidates with no runtime go last: they cannot be assessed, so they should
    not displace one that can.
    """

    def key(item: SearchResult) -> tuple[int, int]:
        delta = item.runtime_delta_minutes(source_seconds)
        return (1, 0) if delta is None else (0, delta)

    return sorted(results, key=key)


def _names(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = [
        str(entry.get("name")).strip()
        for entry in value
        if isinstance(entry, dict) and entry.get("name")
    ]
    return ", ".join(dict.fromkeys(names)) or None


def _series(product: dict[str, Any]) -> tuple[str | None, str | None]:
    series = product.get("series")
    if not isinstance(series, list) or not series:
        return None, None
    first = series[0]
    if not isinstance(first, dict):
        return None, None
    sequence = first.get("sequence")
    return _text(first.get("title")), (str(sequence).strip() if sequence not in (None, "") else None)


def _genres(product: dict[str, Any]) -> list[str]:
    names: list[str] = []
    ladders = product.get("category_ladders")
    if isinstance(ladders, list):
        for entry in ladders:
            ladder = entry.get("ladder") if isinstance(entry, dict) else None
            if not isinstance(ladder, list):
                continue
            for rung in ladder:
                if isinstance(rung, dict) and rung.get("name"):
                    names.append(str(rung["name"]).strip())
    return list(dict.fromkeys(name for name in names if name))


def _cover(product: dict[str, Any]) -> str | None:
    images = product.get("product_images")
    if not isinstance(images, dict):
        return None
    for size in ("1000", "700", "500", "300", "100"):
        url = images.get(size) or images.get(int(size))  # type: ignore[arg-type]
        if url:
            return str(url)
    return None


def _description(product: dict[str, Any]) -> str | None:
    for key in ("publisher_summary", "product_description", "merchandising_summary"):
        raw = product.get(key)
        if raw:
            return _strip_html(str(raw))
    return None


def _strip_html(text: str) -> str:
    out = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    out = re.sub(r"<\s*/?\s*p\s*>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    out = html.unescape(out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _clean_title(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return _NARRATED_BY_RE.sub("", text).strip() or text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().casefold()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None
