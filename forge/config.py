"""Configuration loading and validation.

forge reads an INI file rather than taking a dozen flags, because an unattended
run from cron should be reproducible from a file under version control rather
than from a crontab line.  Command-line flags override individual values.

Validation is deliberately strict and happens once at startup.  A misconfigured
directory layout is the one mistake that can quietly destroy a library: if the
output or archive directory sits inside the source tree, a later run discovers
its own outputs and converts them again.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import ConfigError

LOGGER = logging.getLogger(__name__)

APP_NAME = "forge"

DEFAULT_CONFIG_PATHS = (
    Path(os.environ.get("FORGE_CONFIG", "")) if os.environ.get("FORGE_CONFIG") else None,
    Path.home() / ".config" / APP_NAME / "config.ini",
    Path(__file__).resolve().parent.parent / "config.ini",
)


@dataclass(frozen=True)
class Config:
    """Validated configuration for one forge run."""

    source_dir: Path
    output_dir: Path
    archive_dir: Path

    #: Where ``forge shelve`` files finished books: the root Audiobookshelf
    #: watches.  Optional, like the credentials below - forge without it does
    #: everything except shelve.
    library_dir: Path | None = None
    #: Where a book whose ASIN is already shelved is set aside.  Defaults to
    #: ``duplicates`` inside whatever directory shelve is reading.
    duplicates_dir: Path | None = None

    max_bitrate_kbps: int = 64
    preferred_codec: str = "libfdk_aac"
    fallback_codec: str = "aac"

    log_file: Path | None = None
    state_dir: Path = Path.home() / ".local" / "share" / APP_NAME

    #: Copy-then-replace on every tag write.  Slower, but a failed in-place
    #: rewrite of an MP4 container leaves an unplayable file.
    safe_tag_writes: bool = True

    #: Unattended runs wait for a file to stop changing before touching it.
    settle_seconds: float = 5.0
    min_age_seconds: float = 120.0

    extract_covers: bool = False
    cover_dir: Path | None = None

    #: Audiobookshelf, used read-only to answer "is this book already shelved?".
    #: Optional: everything else works without it.
    abs_url: str | None = None
    abs_api_key: str | None = None
    abs_library_id: str | None = None

    #: Written onto outputs so they are never reprocessed.  Kept configurable
    #: only for testing; changing it in production breaks the loop guard.
    config_path: Path | None = None

    @property
    def can_shelve(self) -> bool:
        """True when forge knows where the library is."""

        return self.library_dir is not None

    def duplicates_for(self, source_dir: Path) -> Path:
        """Where duplicates go when shelving *source_dir*."""

        return self.duplicates_dir or source_dir / "duplicates"

    @property
    def has_library(self) -> bool:
        """True when forge knows how to ask Audiobookshelf anything."""

        return bool(self.abs_url and self.abs_api_key)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "history.sqlite3"

    @property
    def authors_db_path(self) -> Path:
        """The author identity registry.

        Its own file rather than a table in the history database: the two have
        separate schemas and separate lifetimes, and the change log is the one
        thing that must never be at risk from a migration of something else.
        """

        return self.state_dir / "authors.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "forge.lock"


def _parse_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return parser.getboolean(section, key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a boolean: {exc}") from exc


def _parse_int(parser: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return parser.getint(section, key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a whole number: {exc}") from exc


def _parse_float(parser: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        return parser.getfloat(section, key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a number: {exc}") from exc


def _required_path(parser: configparser.ConfigParser, section: str, key: str) -> Path:
    value = parser.get(section, key, fallback="").strip()
    if not value:
        raise ConfigError(f"missing required setting: [{section}] {key}")
    return Path(value).expanduser().resolve()


def _optional_path(parser: configparser.ConfigParser, section: str, key: str) -> Path | None:
    value = parser.get(section, key, fallback="").strip()
    return Path(value).expanduser().resolve() if value else None


#: Where credentials are looked for, in order.  Kept out of the INI by default
#: because the INI is the file worth committing and an API key is not.
def env_file_paths(config_path: Path | None) -> tuple[Path, ...]:
    # FORGE_ENV is authoritative when set: naming a file and then silently
    # reading a different one is how a test run ends up talking to the real
    # server.
    if os.environ.get("FORGE_ENV"):
        return (Path(os.environ["FORGE_ENV"]).expanduser(),)
    candidates = []
    if config_path is not None:
        candidates.append(config_path.parent / ".env")
    candidates.append(Path.home() / ".config" / APP_NAME / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    return tuple(candidates)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY = "value"`` file.  Unreadable or odd lines are ignored.

    Deliberately forgiving: this file holds credentials copied by hand from
    another tool, and refusing to start over a stray line in it would be a poor
    trade for a feature that is optional anyway.
    """

    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().upper()] = value
    return values


def _credentials(parser: configparser.ConfigParser, config_path: Path | None) -> dict[str, str | None]:
    """Audiobookshelf settings, from the INI, the environment, or a .env file.

    Precedence runs INI → environment → .env, so a value written deliberately
    into the configuration always wins over one inherited from a file that was
    copied in from somewhere else.
    """

    from_file: dict[str, str] = {}
    for candidate in env_file_paths(config_path):
        if candidate.is_file():
            from_file = read_env_file(candidate)
            if from_file:
                LOGGER.debug("read Audiobookshelf credentials from %s", candidate)
                break

    def pick(ini_key: str, env_key: str) -> str | None:
        value = parser.get("audiobookshelf", ini_key, fallback="").strip()
        if not value:
            value = (os.environ.get(env_key) or "").strip()
        if not value:
            value = (from_file.get(env_key) or "").strip()
        return value or None

    return {
        "abs_url": pick("url", "AUDIOBOOKSHELF_URL"),
        "abs_api_key": pick("api_key", "AUDIOBOOKSHELF_API_KEY"),
        "abs_library_id": pick("library_id", "AUDIOBOOKSHELF_LIBRARY_ID"),
    }


def find_config() -> Path | None:
    """Return the first configuration file that exists."""

    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate and candidate.is_file():
            return candidate
    return None


def load(path: Path | str | None = None, *, create_dirs: bool = True) -> Config:
    """Load and validate configuration from *path* or the default locations."""

    config_path = Path(path).expanduser() if path else find_config()
    if config_path is None:
        searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS if p)
        raise ConfigError(
            f"no configuration file found. Copy config.sample.ini to "
            f"{Path.home() / '.config' / APP_NAME / 'config.ini'} "
            f"(searched: {searched})"
        )
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")

    parser = configparser.ConfigParser()
    try:
        parser.read(config_path)
    except configparser.Error as exc:
        raise ConfigError(f"could not parse {config_path}: {exc}") from exc

    if "paths" not in parser:
        raise ConfigError(f"{config_path} is missing the required [paths] section")

    config = Config(
        source_dir=_required_path(parser, "paths", "source_dir"),
        output_dir=_required_path(parser, "paths", "output_dir"),
        archive_dir=_required_path(parser, "paths", "archive_dir"),
        library_dir=_optional_path(parser, "paths", "library_dir"),
        duplicates_dir=_optional_path(parser, "paths", "duplicates_dir"),
        max_bitrate_kbps=_parse_int(parser, "encoding", "max_bitrate", 64),
        preferred_codec=parser.get("encoding", "codec", fallback="libfdk_aac").strip(),
        fallback_codec=parser.get("encoding", "fallback_codec", fallback="aac").strip(),
        log_file=_optional_path(parser, "general", "log_file"),
        state_dir=(
            _optional_path(parser, "general", "state_dir")
            or Path.home() / ".local" / "share" / APP_NAME
        ),
        safe_tag_writes=_parse_bool(parser, "safety", "safe_tag_writes", True),
        settle_seconds=_parse_float(parser, "safety", "settle_seconds", 5.0),
        min_age_seconds=_parse_float(parser, "safety", "min_age_seconds", 120.0),
        extract_covers=_parse_bool(parser, "artwork", "extract_covers", False),
        cover_dir=_optional_path(parser, "artwork", "cover_dir"),
        config_path=config_path.resolve(),
        **_credentials(parser, config_path.resolve()),  # type: ignore[arg-type]
    )

    validate(config, create_dirs=create_dirs)
    return config


def validate(config: Config, *, create_dirs: bool = True) -> None:
    """Check a :class:`Config` for the mistakes that matter."""

    if config.max_bitrate_kbps <= 0:
        raise ConfigError("[encoding] max_bitrate must be greater than zero")
    if not config.preferred_codec:
        raise ConfigError("[encoding] codec must not be empty")
    if config.settle_seconds < 0 or config.min_age_seconds < 0:
        raise ConfigError("[safety] settle_seconds and min_age_seconds must not be negative")
    if config.extract_covers and config.cover_dir is None:
        raise ConfigError("[artwork] cover_dir is required when extract_covers is true")

    directories = [
        ("source_dir", config.source_dir),
        ("output_dir", config.output_dir),
        ("archive_dir", config.archive_dir),
    ]
    if config.cover_dir is not None:
        directories.append(("cover_dir", config.cover_dir))

    for label, directory in directories:
        if create_dirs:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"could not create [paths] {label} at {directory}: {exc}") from exc
        if not directory.is_dir():
            raise ConfigError(f"[paths] {label} is not a directory: {directory}")

    # The loop guard, at the directory level.  An output or archive directory
    # inside the source tree means a later run rediscovers its own outputs and
    # converts them a second time.  The disposition tag guards against this too,
    # but defence in depth is cheap and the failure is expensive.
    source = config.source_dir
    for label, directory in (("output_dir", config.output_dir), ("archive_dir", config.archive_dir)):
        if directory == source:
            raise ConfigError(f"[paths] {label} must not be the same as source_dir")
        if source in directory.parents:
            raise ConfigError(
                f"[paths] {label} must not be inside source_dir "
                f"({directory} is under {source})"
            )

    if config.output_dir == config.archive_dir:
        raise ConfigError("[paths] output_dir and archive_dir must be different")

    _validate_library(config)

    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"could not create [general] state_dir at {config.state_dir}: {exc}") from exc

    if config.log_file is not None:
        try:
            config.log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"could not create the log directory for {config.log_file}: {exc}") from exc


def _validate_library(config: Config) -> None:
    """Check the directories ``forge shelve`` moves books between.

    The library root is never created.  It is an existing tree that
    Audiobookshelf is watching, so a typo in this setting must fail loudly
    rather than quietly produce a second, empty library beside the real one and
    start filling it.
    """

    library = config.library_dir
    if library is None:
        if config.duplicates_dir is not None:
            raise ConfigError("[paths] duplicates_dir is only used with library_dir")
        return

    if not library.is_dir():
        raise ConfigError(
            f"[paths] library_dir does not exist: {library}\n"
            f"  forge will not create the library root; check the path"
        )

    for label, directory in (
        ("source_dir", config.source_dir),
        ("output_dir", config.output_dir),
        ("archive_dir", config.archive_dir),
    ):
        if library == directory:
            raise ConfigError(f"[paths] library_dir must not be the same as {label}")

    # Shelving reads output_dir and writes library_dir.  Either one inside the
    # other means a shelved book is immediately a candidate for shelving again,
    # or a conversion writes its output into the live library.
    for label, directory in (("source_dir", config.source_dir), ("output_dir", config.output_dir)):
        if directory in library.parents:
            raise ConfigError(
                f"[paths] library_dir must not be inside {label} "
                f"({library} is under {directory})"
            )
        if library in directory.parents:
            raise ConfigError(
                f"[paths] {label} must not be inside library_dir "
                f"({directory} is under {library})"
            )

    duplicates = config.duplicates_dir
    if duplicates is not None and (duplicates == library or library in duplicates.parents):
        raise ConfigError(
            "[paths] duplicates_dir must not be inside library_dir: "
            "a duplicate set aside there would be picked up by Audiobookshelf"
        )


def with_overrides(config: Config, **overrides: object) -> Config:
    """Return a copy of *config* with command-line overrides applied."""

    clean = {key: value for key, value in overrides.items() if value is not None}
    if not clean:
        return config
    updated = replace(config, **clean)  # type: ignore[arg-type]
    validate(updated)
    return updated
