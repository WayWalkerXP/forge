"""A single-instance lock.

Two forge runs over the same source directory would race for the same files and
output names.  Both source projects shipped a lock implementation and neither
ever instantiated it, so this one is wired into ``forge run``.

The lock records its owner's pid, which lets a later run distinguish a genuine
concurrent instance from a stale file left by a machine that lost power.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .errors import LockError

LOGGER = logging.getLogger(__name__)


class Lock:
    """An exclusive-create lock file with stale-owner detection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = self._read_owner()
            if owner is not None and _process_alive(owner):
                raise LockError(
                    f"another forge run is already in progress (pid {owner}); "
                    f"if that is wrong, remove {self.path}"
                )
            # The recorded owner is gone, so the lock is debris from a crash or
            # a kill -9.  Taking it over is safe and saves a confusing manual step.
            LOGGER.warning("removing a stale lock file left by pid %s", owner)
            try:
                self.path.unlink(missing_ok=True)
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except OSError as exc:
                raise LockError(f"could not take over the stale lock {self.path}: {exc}") from exc

        os.write(self._fd, f"pid={os.getpid()}\n".encode("utf-8"))
        try:
            os.fsync(self._fd)
        except OSError:
            pass

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("could not remove the lock file %s", self.path)

    def _read_owner(self) -> int | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("pid="):
                try:
                    return int(line.partition("=")[2].strip())
                except ValueError:
                    return None
        return None

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to someone else.
        return True
    except OSError:
        return True
    return True
