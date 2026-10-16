"""A record of every tag change forge makes, and the ability to undo it.

Backed by stdlib ``sqlite3``.  Three tables are not enough work to justify an
ORM, and avoiding one keeps forge installable from the distribution's own
packages with nothing extra.

Undo is forward replay, not a filesystem rollback: each change records the value
that was there before, and undoing writes those values back.  That means an undo
is itself a recorded change, so it can be undone in turn.

Cover images are never stored.  A change to artwork is recorded as a hash and a
size, which keeps the database small and means a cover change is deliberately
not undoable - the bytes are long gone.  A library's worth of embedded JPEGs in
a SQLite file is a problem that gets discovered far too late.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS change_group (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_key    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_group_book ON change_group (book_key);
CREATE INDEX IF NOT EXISTS idx_change_group_time ON change_group (created_at);

CREATE TABLE IF NOT EXISTS change (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES change_group (id) ON DELETE CASCADE,
    file_path TEXT    NOT NULL,
    field     TEXT    NOT NULL,
    old_value TEXT,
    new_value TEXT,
    status    TEXT    NOT NULL DEFAULT 'applied',
    error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_group ON change (group_id);
CREATE INDEX IF NOT EXISTS idx_change_file  ON change (file_path);

CREATE TABLE IF NOT EXISTS snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_key      TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    metadata_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_book ON snapshot (book_key);
"""

#: Fields whose values are image data rather than text, and are summarised
#: rather than stored.
_COVER_FIELDS = frozenset({"cover", "cover_path", "cover_url", "cover_data", "delete_cover"})

#: Cannot be restored: either derived from the audio, or image data we did not keep.
UNRESTORABLE_FIELDS = frozenset(_COVER_FIELDS | {"duration", "has_cover", "path"})


@dataclass(frozen=True)
class Change:
    """One field change on one file."""

    id: int
    group_id: int
    file_path: str
    field: str
    old_value: str | None
    new_value: str | None
    status: str = "applied"
    error: str | None = None

    @property
    def restorable(self) -> bool:
        return self.field not in UNRESTORABLE_FIELDS and self.status == "applied"


@dataclass(frozen=True)
class ChangeGroup:
    """A set of changes applied together."""

    id: int
    book_key: str
    created_at: datetime
    source: str
    description: str | None = None
    change_count: int = 0

    def describe(self) -> str:
        when = self.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
        label = self.description or self.source
        return f"[{self.id}] {when}  {label}  ({self.change_count} field(s))"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class History:
    """The change log for one forge installation."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        # The default rollback journal loses the last transaction if the machine
        # dies mid-write; WAL does not, and an unattended nightly run is exactly
        # when nobody is watching.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self._connection.executescript(_SCHEMA)
        row = self._connection.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            self._connection.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            LOGGER.warning(
                "history database is schema version %s, this build expects %s",
                row["version"],
                SCHEMA_VERSION,
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several writes atomically."""

        self._connection.execute("BEGIN")
        try:
            yield self._connection
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    # ----------------------------------------------------------------- write #

    def record(
        self,
        book_key: str,
        changes: dict[str, tuple[Any, Any]],
        *,
        file_path: Path | str,
        source: str,
        description: str | None = None,
        status: str = "applied",
        error: str | None = None,
        group_id: int | None = None,
    ) -> int | None:
        """Record a group of changes and return its id.

        Returns ``None`` when *changes* is empty, so callers need not guard.

        Pass *group_id* to add these changes to an existing group instead of
        opening a new one.  That is how every track of a folder book ends up in
        one group: undo restores a whole group at once, and splitting a single
        edit across twelve groups would mean undoing it twelve times.
        """

        if not changes:
            return None

        with self.transaction() as connection:
            if group_id is None:
                cursor = connection.execute(
                    "INSERT INTO change_group (book_key, created_at, source, description) "
                    "VALUES (?, ?, ?, ?)",
                    (book_key, _now(), source, description),
                )
                group_id = int(cursor.lastrowid)
            elif self.group(group_id) is None:
                raise ValueError(f"no change group with id {group_id}")
            connection.executemany(
                "INSERT INTO change (group_id, file_path, field, old_value, new_value, status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        group_id,
                        str(file_path),
                        field,
                        summarise_value(field, old),
                        summarise_value(field, new),
                        status,
                        error,
                    )
                    for field, (old, new) in sorted(changes.items())
                ],
            )
        return group_id

    def relocate(self, old_path: Path | str, new_path: Path | str, *, group_id: int | None = None) -> int:
        """Point recorded changes at a file's new location, and say how many moved.

        ``forge shelve`` edits a book's tags and then moves it into the
        library.  Without this, the edits stay recorded against a path that no
        longer exists: asking ``forge history`` about the book where it now
        lives would show only the move, and an undo would write the old tags to
        a file that is not there.  Restricted to one group when *group_id* is
        given, so an unrelated earlier change to a file of the same name is not
        swept along.
        """

        query = "UPDATE change SET file_path = ? WHERE file_path = ?"
        params: list[Any] = [str(new_path), str(old_path)]
        if group_id is not None:
            query += " AND group_id = ?"
            params.append(group_id)
        with self.transaction() as connection:
            cursor = connection.execute(query, params)
        return cursor.rowcount

    def snapshot(
        self, book_key: str, path: Path | str, metadata: dict[str, Any], *, source: str = "scan"
    ) -> int:
        """Store the full tag state of a book, for reference and diffing."""

        payload = json.dumps(strip_cover_data(metadata), ensure_ascii=False, sort_keys=True)
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO snapshot (book_key, path, created_at, source, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_key, str(path), _now(), source, payload),
            )
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------ read #

    def groups_for_book(self, book_key: str, *, limit: int = 50) -> list[ChangeGroup]:
        """Change groups for one book, newest first."""

        rows = self._connection.execute(
            "SELECT g.*, COUNT(c.id) AS change_count "
            "FROM change_group g LEFT JOIN change c ON c.group_id = g.id "
            "WHERE g.book_key = ? GROUP BY g.id ORDER BY g.id DESC LIMIT ?",
            (book_key, limit),
        ).fetchall()
        return [_group_from_row(row) for row in rows]

    def recent_groups(self, *, limit: int = 20) -> list[ChangeGroup]:
        """The most recent change groups across every book, newest first.

        Undo takes a group id, so there has to be a way to see the ids without
        already knowing which file to ask about.
        """

        rows = self._connection.execute(
            "SELECT g.*, COUNT(c.id) AS change_count "
            "FROM change_group g LEFT JOIN change c ON c.group_id = g.id "
            "GROUP BY g.id ORDER BY g.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_group_from_row(row) for row in rows]

    def files_in_group(self, group_id: int) -> list[str]:
        """The files a group touched, in the order they were recorded."""

        rows = self._connection.execute(
            "SELECT DISTINCT file_path FROM change WHERE group_id = ? ORDER BY id",
            (group_id,),
        ).fetchall()
        return [row["file_path"] for row in rows]

    def groups_for_file(self, file_path: Path | str, *, limit: int = 50) -> list[ChangeGroup]:
        """Change groups touching one file, newest first."""

        rows = self._connection.execute(
            "SELECT g.*, COUNT(c.id) AS change_count "
            "FROM change_group g JOIN change c ON c.group_id = g.id "
            "WHERE c.file_path = ? GROUP BY g.id ORDER BY g.id DESC LIMIT ?",
            (str(file_path), limit),
        ).fetchall()
        return [_group_from_row(row) for row in rows]

    def changes_in_group(self, group_id: int, *, file_path: Path | str | None = None) -> list[Change]:
        """The individual changes in one group."""

        if file_path is None:
            rows = self._connection.execute(
                "SELECT * FROM change WHERE group_id = ? ORDER BY field", (group_id,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM change WHERE group_id = ? AND file_path = ? ORDER BY field",
                (group_id, str(file_path)),
            ).fetchall()
        return [_change_from_row(row) for row in rows]

    def group(self, group_id: int) -> ChangeGroup | None:
        row = self._connection.execute(
            "SELECT g.*, COUNT(c.id) AS change_count "
            "FROM change_group g LEFT JOIN change c ON c.group_id = g.id "
            "WHERE g.id = ? GROUP BY g.id",
            (group_id,),
        ).fetchone()
        return _group_from_row(row) if row else None

    def latest_snapshot(self, book_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT metadata_json FROM snapshot WHERE book_key = ? ORDER BY id DESC LIMIT 1",
            (book_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            return None

    def restore_values(self, group_id: int, *, fields: set[str] | None = None) -> dict[str, dict[str, str | None]]:
        """The values an undo of *group_id* would write, keyed by file path."""

        plan: dict[str, dict[str, str | None]] = {}
        for change in self.changes_in_group(group_id):
            if not change.restorable:
                continue
            if fields is not None and change.field not in fields:
                continue
            plan.setdefault(change.file_path, {})[change.field] = change.old_value
        return plan

    # --------------------------------------------------------- housekeeping #

    def prune(self, *, days_to_keep: int = 90) -> dict[str, int]:
        """Delete records older than *days_to_keep* and reclaim the space."""

        cutoff = datetime.now(timezone.utc).timestamp() - days_to_keep * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()

        with self.transaction() as connection:
            snapshots = connection.execute(
                "DELETE FROM snapshot WHERE created_at < ?", (cutoff_iso,)
            ).rowcount
            changes = connection.execute(
                "DELETE FROM change WHERE group_id IN "
                "(SELECT id FROM change_group WHERE created_at < ?)",
                (cutoff_iso,),
            ).rowcount
            groups = connection.execute(
                "DELETE FROM change_group WHERE created_at < ?", (cutoff_iso,)
            ).rowcount

        self._connection.execute("VACUUM")
        return {"snapshots": snapshots, "changes": changes, "groups": groups}

    def size_bytes(self) -> int:
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0

    def stats(self) -> dict[str, int]:
        counts = {}
        for table in ("change_group", "change", "snapshot"):
            row = self._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
        counts["size_bytes"] = self.size_bytes()
        return counts


def _group_from_row(row: sqlite3.Row) -> ChangeGroup:
    return ChangeGroup(
        id=int(row["id"]),
        book_key=row["book_key"],
        created_at=_parse_time(row["created_at"]),
        source=row["source"],
        description=row["description"],
        change_count=int(row["change_count"]) if "change_count" in row.keys() else 0,
    )


def _change_from_row(row: sqlite3.Row) -> Change:
    return Change(
        id=int(row["id"]),
        group_id=int(row["group_id"]),
        file_path=row["file_path"],
        field=row["field"],
        old_value=row["old_value"],
        new_value=row["new_value"],
        status=row["status"],
        error=row["error"],
    )


# --------------------------------------------------------------------------- #
# Keeping image data out of the database
# --------------------------------------------------------------------------- #


def summarise_value(field: str, value: Any) -> str | None:
    """Render *value* for storage, replacing image data with a description."""

    if value is None:
        return None
    if field in _COVER_FIELDS:
        return _describe_cover(value)
    if isinstance(value, (bytes, bytearray)):
        return _describe_cover(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    text = str(value)
    if _looks_like_image_payload(text):
        return _describe_cover(text)
    return text


def _describe_cover(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        digest = hashlib.sha256(data).hexdigest()[:16]
        return f"cover present ({len(data)} bytes, sha256={digest})"
    text = str(value)
    if text.lower() in ("true", "1"):
        return "cover deleted"
    if _looks_like_image_payload(text):
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        return f"cover data (sha256={digest})"
    return f"cover from {text}"


def _looks_like_image_payload(text: str) -> bool:
    if text.startswith("data:image/"):
        return True
    # A long run of base64 with no spaces is almost certainly an embedded image.
    return len(text) > 512 and " " not in text[:512]


def strip_cover_data(value: Any) -> Any:
    """Recursively remove image payloads from a structure before storing it."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in _COVER_FIELDS or key in ("cover_data", "cover_data_uri"):
                if item:
                    cleaned[key] = _describe_cover(item)
                continue
            cleaned[key] = strip_cover_data(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [strip_cover_data(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _describe_cover(value)
    return value
