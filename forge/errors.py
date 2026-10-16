"""Exception hierarchy for forge.

Every error that a run can recover from at the per-book level derives from
``BookError``.  Errors that must abort the entire run (a full disk, a lost lock)
do not, so that the per-book handler cannot accidentally swallow them.
"""

from __future__ import annotations


class ForgeError(Exception):
    """Base class for every error raised by forge."""


class ConfigError(ForgeError):
    """The INI configuration is missing, unreadable, or invalid."""


class LockError(ForgeError):
    """Another forge instance already holds the lock file."""


class ExternalToolError(ForgeError):
    """A required external binary is missing or unusable."""


class BookError(ForgeError):
    """A per-book failure that must not stop the rest of the run."""


class ProbeError(BookError):
    """ffprobe could not read or describe a media file."""


class TagError(BookError):
    """A tag could not be read from or written to a file."""


class PlanError(BookError):
    """A conversion plan could not be built for a book."""


class DiskSpaceError(ForgeError):
    """Disk space was exhausted.  Always aborts the whole run."""


class ForcedTermination(ForgeError):
    """The user requested immediate termination after a graceful quit."""
