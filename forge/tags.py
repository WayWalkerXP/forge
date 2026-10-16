"""Reading and writing audio tags for MP4/M4B and MP3/ID3 files.

Two layers live here:

*   Low-level primitives (``open_audio``, ``safe_save``, ``read_freeform``,
    ``write_freeform``) that hide the MP4-vs-ID3 split.  ``disposition`` builds
    directly on these.
*   The field mapping between forge's :class:`BookMetadata` and the concrete
    atoms and frames each container uses.

Every write goes through :func:`safe_save`, which never mutates the original
file in place.  mutagen rewrites an entire MP4 container when the tag block
grows past its padding, and a failure part-way through that rewrite leaves an
unplayable file.  Copying first costs I/O; losing an audiobook costs more.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

import mutagen
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TCOM,
    TCON,
    TCOP,
    TDRC,
    TIT2,
    TIT3,
    TLAN,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .errors import TagError

LOGGER = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".m4b", ".m4a", ".mp3"}

MP4_FREEFORM_PREFIX = "----:com.apple.iTunes:"

#: Fields that are derived from the audio stream and can never be written.
NON_WRITABLE_FIELDS = frozenset({"duration", "has_cover", "path"})


# --------------------------------------------------------------------------- #
# Low-level primitives
# --------------------------------------------------------------------------- #


def open_audio(path: Path | str) -> Any:
    """Open *path* with mutagen, raising :class:`TagError` on any failure."""

    path = Path(path)
    try:
        audio = mutagen.File(str(path), easy=False)
    except Exception as exc:  # mutagen raises a wide variety of types
        raise TagError(f"could not read tags from {path.name}: {exc}") from exc
    if audio is None:
        raise TagError(f"unrecognised audio format: {path.name}")
    return audio


def is_mp4(audio: Any) -> bool:
    """True when *audio* is an MP4/M4B container."""

    return isinstance(audio, MP4)


def ensure_tags(audio: Any) -> Any:
    """Return *audio*'s tag block, creating it when the file has none."""

    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as exc:
            raise TagError(f"could not create a tag block: {exc}") from exc
    return audio.tags


def safe_save(path: Path | str, mutate: Callable[[Any], None], *, in_place: bool = False) -> None:
    """Apply *mutate* to *path*'s tags without risking the original file.

    The file is copied to a sibling temporary name, mutated, re-opened to prove
    it still parses, and only then moved over the original with
    :func:`os.replace`.  The temporary lives in the same directory so that the
    final step is an atomic same-filesystem rename.

    Pass ``in_place=True`` to mutate the original directly.  That is faster and
    avoids copying a large container, but a failure mid-write is unrecoverable.
    """

    path = Path(path)
    if in_place:
        audio = open_audio(path)
        mutate(audio)
        try:
            audio.save()
        except Exception as exc:
            raise TagError(f"could not write tags to {path.name}: {exc}") from exc
        return

    temp_path = path.with_name(f".{path.name}.forge-tmp")
    try:
        shutil.copy2(path, temp_path)
    except OSError as exc:
        _unlink_quietly(temp_path)
        raise TagError(f"could not stage a copy of {path.name}: {exc}") from exc

    try:
        audio = open_audio(temp_path)
        mutate(audio)
        audio.save()
        # Prove the written container still parses before it replaces the
        # original.  A truncated or corrupted rewrite fails here, while the
        # original is still untouched.
        open_audio(temp_path)
    except Exception as exc:
        _unlink_quietly(temp_path)
        if isinstance(exc, TagError):
            raise
        raise TagError(f"could not write tags to {path.name}: {exc}") from exc

    try:
        shutil.copystat(path, temp_path)
        os.replace(temp_path, path)
    except OSError as exc:
        _unlink_quietly(temp_path)
        raise TagError(f"could not replace {path.name} with the tagged copy: {exc}") from exc


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("could not remove temporary file %s", path, exc_info=True)


def read_freeform(audio: Any, name: str) -> str | None:
    """Read a custom tag *name* from either container type.

    MP4 uses ``----:com.apple.iTunes:NAME``; ID3 uses ``TXXX:NAME``.  Returns
    ``None`` when the tag is absent or empty.
    """

    tags = audio.tags
    if tags is None:
        return None

    if is_mp4(audio):
        for key in (f"{MP4_FREEFORM_PREFIX}{name}", f"{MP4_FREEFORM_PREFIX}{name.lower()}"):
            if key in tags:
                value = normalize_tag_value(tags[key])
                if value:
                    return value
        return None

    for frame in tags.getall("TXXX"):
        if (frame.desc or "").upper() == name.upper():
            value = normalize_tag_value(frame.text)
            if value:
                return value
    return None


def write_freeform(audio: Any, name: str, value: str | None) -> None:
    """Write (or delete, when *value* is ``None``) a custom tag *name*."""

    tags = ensure_tags(audio)

    if is_mp4(audio):
        key = f"{MP4_FREEFORM_PREFIX}{name}"
        if value is None:
            for candidate in (key, f"{MP4_FREEFORM_PREFIX}{name.lower()}"):
                tags.pop(candidate, None)
            return
        tags[key] = [MP4FreeForm(str(value).encode("utf-8"))]
        return

    existing = [f for f in tags.getall("TXXX") if (f.desc or "").upper() == name.upper()]
    for frame in existing:
        try:
            tags.delall(f"TXXX:{frame.desc}")
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("could not delete TXXX:%s", frame.desc, exc_info=True)
    if value is not None:
        tags.add(TXXX(encoding=3, desc=name, text=[str(value)]))


def normalize_tag_value(value: Any) -> str | None:
    """Coerce any mutagen value into a plain string, or ``None`` when empty."""

    if value is None:
        return None
    if isinstance(value, (MP4FreeForm, bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace").strip()
        return text or None
    if isinstance(value, (list, tuple)):
        parts = [normalize_tag_value(item) for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    if hasattr(value, "text"):
        return normalize_tag_value(value.text)
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------- #
# Value normalisation
# --------------------------------------------------------------------------- #

BOOLEAN_FIELDS = frozenset({"explicit"})

_TRUE_TEXT = {"1", "true", "yes", "on", "y", "t", "checked"}
_FALSE_TEXT = {"0", "false", "no", "off", "n", "f", "unchecked"}

#: Genres arrive as real lists, as ``repr`` output from an earlier tool, or as a
#: delimited string.  Only ``;`` and ``,`` are treated as separators - ``&`` and
#: ``/`` appear inside legitimate genre names such as "Science Fiction & Fantasy".
_GENRE_SPLIT_RE = re.compile(r"\s*[;,]\s*")


def normalize_genres(value: Any) -> list[str]:
    """Coerce *value* into an ordered, de-duplicated list of genre names."""

    if value is None:
        return []

    items: list[str]
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = normalize_tag_value(value) or ""
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple)):
                items = [str(item) for item in parsed]
            else:
                items = _GENRE_SPLIT_RE.split(text)
        else:
            items = _GENRE_SPLIT_RE.split(text)

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip().strip("'\"")
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def format_genres(genres: Iterable[str] | None) -> str | None:
    """Render a genre list as the single delimited string tags store."""

    if not genres:
        return None
    joined = ", ".join(str(g).strip() for g in genres if str(g).strip())
    return joined or None


def parse_bool(value: Any) -> bool | None:
    """Parse a tag value into a tri-state boolean."""

    if value is None or isinstance(value, bool):
        return value
    text = (normalize_tag_value(value) or "").casefold()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return None


def parse_int(value: Any) -> int | None:
    """Parse the leading integer out of a tag value, tolerating ``3/12`` forms."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = normalize_tag_value(value)
    if not text:
        return None
    match = re.match(r"\s*(\d+)", text)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# Metadata model
# --------------------------------------------------------------------------- #


@dataclass
class BookMetadata:
    """Tag values forge reads and writes for one audio file."""

    path: str
    title: str | None = None
    subtitle: str | None = None
    album: str | None = None
    author: str | None = None
    albumartist: str | None = None
    narrator: str | None = None
    series: str | None = None
    series_sequence: str | None = None
    asin: str | None = None
    description: str | None = None
    publisher: str | None = None
    copyright: str | None = None
    published_date: str | None = None
    published_year: str | None = None
    language: str | None = None
    genres: list[str] = field(default_factory=list)
    track: int | None = None
    disc: int | None = None
    explicit: bool | None = None
    duration: int | None = None
    has_cover: bool = False
    cover_data: bytes | None = None
    cover_mime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("cover_data", None)
        return data

    @property
    def display_author(self) -> str | None:
        return self.author or self.albumartist

    @property
    def display_title(self) -> str | None:
        return self.album or self.title


#: Ordered (field, label) pairs offered by the metadata editor.
EDITABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Title"),
    ("subtitle", "Subtitle"),
    ("album", "Album"),
    ("author", "Author"),
    ("albumartist", "Album Artist"),
    ("narrator", "Narrator"),
    ("series", "Series"),
    ("series_sequence", "Series Part"),
    ("asin", "ASIN"),
    ("description", "Description"),
    ("publisher", "Publisher"),
    ("copyright", "Copyright"),
    ("published_date", "Published Date"),
    ("published_year", "Published Year"),
    ("language", "Language"),
    ("genres", "Genres"),
    ("explicit", "Explicit"),
    ("track", "Track"),
    ("disc", "Disc"),
)

#: Fields that belong to the book rather than an individual track, so that
#: editing a folder book applies them to every file.
BOOK_LEVEL_FIELDS = frozenset(
    {
        "subtitle",
        "album",
        "author",
        "albumartist",
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
    }
)

# MP4 atoms for fields with a dedicated atom.  Everything else goes to a
# ``----:com.apple.iTunes:`` freeform via the explicit list below, so a typo'd
# field name raises instead of silently creating a junk atom.
_MP4_ATOMS = {
    "title": "\xa9nam",
    "album": "\xa9alb",
    "author": "\xa9ART",
    "albumartist": "aART",
    "narrator": "\xa9wrt",
    "description": "desc",
    "copyright": "cprt",
    "published_date": "\xa9day",
    "genres": "\xa9gen",
}
_MP4_FREEFORMS = {
    "subtitle": "SUBTITLE",
    "series": "SERIES",
    "series_sequence": "SERIES_SEQUENCE",
    "asin": "ASIN",
    "publisher": "PUBLISHER",
    "language": "LANGUAGE",
    "explicit": "EXPLICIT",
}
#: Alternative spellings accepted when reading, in preference order.
_MP4_READ_FALLBACKS = {
    "narrator": ("NARRATOR", "\xa9nrt"),
    "series": ("series", "SHOW", "tvsh", "MOVEMENTNAME"),
    "series_sequence": (
        "series_sequence",
        "SERIES-PART",
        "series-part",
        "SERIES_PART",
        "PART",
        "MOVEMENT",
    ),
    "publisher": ("\xa9pub",),
    "description": ("\xa9cmt",),
}

_ID3_FRAMES = {
    "title": TIT2,
    "album": TALB,
    "author": TPE1,
    "albumartist": TPE2,
    "narrator": TCOM,
    "publisher": TPUB,
    "copyright": TCOP,
    "published_date": TDRC,
    "language": TLAN,
    "genres": TCON,
    "track": TRCK,
    "disc": TPOS,
}
_ID3_TXXX = {
    "series": "SERIES",
    "series_sequence": "SERIES_SEQUENCE",
    "asin": "ASIN",
    "explicit": "EXPLICIT",
}
_ID3_TXXX_READ_FALLBACKS = {
    "series": ("series", "SHOW", "MOVEMENTNAME"),
    "series_sequence": (
        "series_sequence",
        "SERIES-PART",
        "series-part",
        "SERIES_PART",
        "PART",
        "MOVEMENT",
    ),
    "narrator": ("NARRATOR",),
}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_metadata(path: Path | str, *, with_cover: bool = False) -> BookMetadata:
    """Read tags from *path*.

    Cover image bytes are only loaded when *with_cover* is true.  ``has_cover``
    is always populated, so a list view can show whether artwork exists without
    holding every cover in memory - the GUI this replaces loaded a base64 copy
    of every cover on every scan.
    """

    path = Path(path)
    audio = open_audio(path)
    meta = BookMetadata(path=str(path))

    length = getattr(getattr(audio, "info", None), "length", None)
    if length:
        meta.duration = int(length)

    if audio.tags is None:
        return meta

    reader = _read_mp4_field if is_mp4(audio) else _read_id3_field
    for name in (
        "title",
        "subtitle",
        "album",
        "author",
        "albumartist",
        "narrator",
        "series",
        "series_sequence",
        "asin",
        "description",
        "publisher",
        "copyright",
        "published_date",
        "language",
    ):
        setattr(meta, name, reader(audio, name))

    meta.genres = normalize_genres(reader(audio, "genres"))
    meta.explicit = parse_bool(reader(audio, "explicit"))
    meta.track = _read_number(audio, "track")
    meta.disc = _read_number(audio, "disc")

    if meta.published_date and not meta.published_year:
        match = re.search(r"(\d{4})", meta.published_date)
        if match:
            meta.published_year = match.group(1)

    meta.has_cover = _has_cover(audio)
    if with_cover and meta.has_cover:
        meta.cover_data, meta.cover_mime = _read_cover(audio)

    return meta


def _read_mp4_field(audio: Any, name: str) -> str | None:
    tags = audio.tags
    atom = _MP4_ATOMS.get(name)
    if atom and atom in tags:
        value = normalize_tag_value(tags[atom])
        if value:
            return value
    freeform = _MP4_FREEFORMS.get(name)
    if freeform:
        value = read_freeform(audio, freeform)
        if value:
            return value
    for fallback in _MP4_READ_FALLBACKS.get(name, ()):
        if fallback.startswith("\xa9") or len(fallback) == 4 and fallback.islower():
            if fallback in tags:
                value = normalize_tag_value(tags[fallback])
                if value:
                    return value
        else:
            value = read_freeform(audio, fallback)
            if value:
                return value
    return None


def _read_id3_field(audio: Any, name: str) -> str | None:
    tags = audio.tags
    frame = _ID3_FRAMES.get(name)
    if frame is not None:
        frames = tags.getall(frame.__name__)
        if frames:
            value = normalize_tag_value(frames[0])
            if value:
                return value
    if name == "subtitle":
        frames = tags.getall("TIT3")
        if frames:
            value = normalize_tag_value(frames[0])
            if value:
                return value
    if name == "description":
        for comm in tags.getall("COMM"):
            value = normalize_tag_value(comm)
            if value:
                return value
    txxx = _ID3_TXXX.get(name)
    if txxx:
        value = read_freeform(audio, txxx)
        if value:
            return value
    for fallback in _ID3_TXXX_READ_FALLBACKS.get(name, ()):
        value = read_freeform(audio, fallback)
        if value:
            return value
    return None


def _read_number(audio: Any, name: str) -> int | None:
    tags = audio.tags
    if is_mp4(audio):
        atom = "trkn" if name == "track" else "disk"
        if atom in tags:
            try:
                return int(tags[atom][0][0]) or None
            except (IndexError, TypeError, ValueError):
                return None
        return None
    return parse_int(_read_id3_field(audio, name))


def _has_cover(audio: Any) -> bool:
    tags = audio.tags
    if tags is None:
        return False
    if is_mp4(audio):
        return bool(tags.get("covr"))
    return any(key.startswith("APIC") for key in tags.keys())


def _read_cover(audio: Any) -> tuple[bytes | None, str | None]:
    tags = audio.tags
    if is_mp4(audio):
        covers = tags.get("covr") or []
        if not covers:
            return None, None
        cover = covers[0]
        fmt = getattr(cover, "imageformat", None)
        mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
        return bytes(cover), mime
    for key in tags.keys():
        if key.startswith("APIC"):
            frame = tags[key]
            return bytes(frame.data), frame.mime or "image/jpeg"
    return None, None


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

#: Pseudo-fields accepted by :func:`write_metadata` that are not tag fields.
_COVER_KEYS = frozenset({"cover_path", "cover_url", "delete_cover"})

_WRITABLE_FIELDS = frozenset(name for name, _ in EDITABLE_FIELDS)


def write_metadata(
    path: Path | str,
    updates: dict[str, Any],
    *,
    in_place: bool = False,
) -> None:
    """Write *updates* to *path*.

    Unknown field names raise :class:`TagError` rather than being written as
    junk custom tags - the GUI this replaces let any typo'd name fall through
    into a ``TXXX`` frame, which silently produced unreadable metadata.
    """

    path = Path(path)
    unknown = set(updates) - _WRITABLE_FIELDS - _COVER_KEYS
    if unknown:
        raise TagError(f"unknown metadata field(s): {', '.join(sorted(unknown))}")

    payload = {k: v for k, v in updates.items() if k not in NON_WRITABLE_FIELDS}

    # published_year has no tag of its own: it is read back as the first four
    # digits of published_date.  Keeping one source of truth avoids the two
    # drifting apart, so a bare year is written as the date, and a year supplied
    # alongside a full date is redundant and dropped.
    if "published_year" in payload:
        year = payload.pop("published_year")
        if "published_date" not in payload and year:
            payload["published_date"] = year

    if not payload:
        return

    safe_save(path, lambda audio: apply_updates(audio, payload), in_place=in_place)


def apply_updates(audio: Any, payload: dict[str, Any]) -> None:
    """Apply *payload* to an already-open *audio*, without saving it.

    Exposed so a caller that is already rewriting a container - copying tags
    onto a freshly converted file, say - can do the whole job in one save
    instead of one per concern.  ``write_metadata`` is the safe-save wrapper
    around this.
    """

    ensure_tags(audio)
    writer = _write_mp4_field if is_mp4(audio) else _write_id3_field
    for name, value in payload.items():
        if name in _COVER_KEYS:
            continue
        writer(audio, name, value)
        if name == "series_sequence":
            # Audiobookshelf reads only the hyphenated spelling (its prober
            # looks for series-part / episode_id / mvin / part, never
            # SERIES_SEQUENCE), while other tools read the underscored one.
            # Writing both is what makes a book readable either way.
            _write_series_part_mirror(audio, value)
    _apply_cover_updates(audio, payload)


def carryover_updates(meta: BookMetadata, *, include_track: bool = True) -> dict[str, Any]:
    """The updates needed to reproduce *meta*'s tags on another file.

    ffmpeg's mp4 muxer writes only the handful of atoms it knows about, so a
    converted output loses every ``----:com.apple.iTunes:`` freeform - series,
    series-part, ASIN, subtitle, publisher, language, explicit - even though
    ``-map_metadata`` was asked to copy them.  Re-applying them with mutagen
    afterwards is the only way they survive a conversion.

    Only fields that actually hold a value are returned, so this never clears a
    tag that ffmpeg did manage to carry across.
    """

    updates: dict[str, Any] = {}
    for name, _label in EDITABLE_FIELDS:
        if name in ("track", "disc") and not include_track:
            continue
        value = getattr(meta, name, None)
        if value in (None, "", []):
            continue
        updates[name] = value
    # published_year is derived from published_date on read; sending both would
    # be dropped as redundant anyway, and sending only the year would coarsen a
    # full date.
    if "published_date" in updates:
        updates.pop("published_year", None)
    return updates


def _write_mp4_field(audio: Any, name: str, value: Any) -> None:
    tags = audio.tags

    if name in ("track", "disc"):
        atom = "trkn" if name == "track" else "disk"
        number = parse_int(value)
        if number is None:
            tags.pop(atom, None)
        else:
            tags[atom] = [(number, 0)]
        return

    text = _render_value(name, value)
    atom = _MP4_ATOMS.get(name)
    if atom:
        if text is None:
            tags.pop(atom, None)
        elif name == "genres":
            tags[atom] = [text]
        else:
            tags[atom] = [text]
        return

    freeform = _MP4_FREEFORMS.get(name)
    if freeform:
        write_freeform(audio, freeform, text)
        return
    raise TagError(f"no MP4 mapping for metadata field {name!r}")


def _write_id3_field(audio: Any, name: str, value: Any) -> None:
    tags = audio.tags
    text = _render_value(name, value)

    if name == "subtitle":
        tags.delall("TIT3")
        if text is not None:
            tags.add(TIT3(encoding=3, text=[text]))
        return

    if name == "description":
        tags.delall("COMM")
        if text is not None:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[text]))
        return

    frame = _ID3_FRAMES.get(name)
    if frame is not None:
        tags.delall(frame.__name__)
        if text is not None:
            tags.add(frame(encoding=3, text=[text]))
        return

    txxx = _ID3_TXXX.get(name)
    if txxx:
        write_freeform(audio, txxx, text)
        return
    raise TagError(f"no ID3 mapping for metadata field {name!r}")


def _write_series_part_mirror(audio: Any, value: Any) -> None:
    text = _render_value("series_sequence", value)
    name = "series-part" if is_mp4(audio) else "SERIES-PART"
    write_freeform(audio, name, text)


def _render_value(name: str, value: Any) -> str | None:
    """Render *value* into the single string a tag stores, or ``None`` to clear."""

    if value is None:
        return None
    if name == "genres":
        return format_genres(normalize_genres(value))
    if name in BOOLEAN_FIELDS:
        parsed = parse_bool(value)
        return None if parsed is None else ("1" if parsed else "0")
    if name == "description":
        text = str(value).replace("\r\n", "\n").strip()
        return text or None
    text = str(value).strip()
    return text or None


def _apply_cover_updates(audio: Any, payload: dict[str, Any]) -> None:
    tags = audio.tags

    if payload.get("delete_cover"):
        if is_mp4(audio):
            tags.pop("covr", None)
        else:
            tags.delall("APIC")
        return

    source = payload.get("cover_path") or payload.get("cover_url")
    if not source:
        return

    data, mime = _load_cover_source(source)
    if is_mp4(audio):
        if mime == "image/png":
            fmt = MP4Cover.FORMAT_PNG
        elif mime == "image/jpeg":
            fmt = MP4Cover.FORMAT_JPEG
        else:
            raise TagError(f"MP4/M4B covers must be PNG or JPEG, not {mime}")
        tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    else:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))


def _load_cover_source(source: str) -> tuple[bytes, str]:
    text = str(source)
    if text.startswith(("http://", "https://")):
        import requests  # imported lazily so the engine needs no network stack

        response = requests.get(text, timeout=30)
        response.raise_for_status()
        data = response.content
    else:
        try:
            data = Path(text).read_bytes()
        except OSError as exc:
            raise TagError(f"could not read cover image {text}: {exc}") from exc
    return data, _sniff_image_mime(data)


def _sniff_image_mime(data: bytes) -> str:
    """Identify an image by its magic bytes rather than trusting its extension."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise TagError("cover image is not a recognised PNG, JPEG, or WebP file")


def diff_metadata(current: BookMetadata, updates: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Return ``{field: (old, new)}`` for the updates that actually change something."""

    changes: dict[str, tuple[Any, Any]] = {}
    for name, new_value in updates.items():
        if name in _COVER_KEYS or name in NON_WRITABLE_FIELDS:
            continue
        if not hasattr(current, name):
            continue
        old_rendered = _render_value(name, getattr(current, name))
        new_rendered = _render_value(name, new_value)
        if old_rendered != new_rendered:
            changes[name] = (old_rendered, new_rendered)
    return changes
