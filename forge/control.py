"""Interrupting a run safely.

A conversion is a transaction: temporary file, validate, promote, archive.
Interrupting in the middle of one is fine (the temporary file is discarded and
the source is untouched), but interrupting *between* books is tidier, so a quit
request is honoured at the next boundary rather than immediately.

Two levels, following the convention a long-running CLI tool ought to have:

*   The first request asks to stop after the current book.
*   A second request gives up waiting, kills ffmpeg, and removes the temporary
    file it was writing.

When stdin is a terminal, ``q`` and ``p`` do the same thing without the
ceremony of a signal.  When it is not - a cron run - only signals apply, and
the raw-key reader is never started.
"""

from __future__ import annotations

import logging
import os
import select
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import ffmpeg
from .errors import ForcedTermination

LOGGER = logging.getLogger(__name__)


class RunController:
    """Tracks pause and quit requests, and cleans up on forced termination."""

    def __init__(self, *, allow_keys: bool = True) -> None:
        self.pause_requested = False
        self.quit_requested = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._suspend_keys = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_sigint: Any = None
        self._previous_sigterm: Any = None
        self._temp_files: set[Path] = set()
        self._allow_keys = allow_keys and sys.stdin.isatty()

    # ------------------------------------------------------------- lifecycle #

    def start(self) -> None:
        self._previous_sigint = signal.getsignal(signal.SIGINT)
        self._previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        if self._allow_keys:
            self._thread = threading.Thread(
                target=self._watch_keys, name="forge-keys", daemon=True
            )
            self._thread.start()
            LOGGER.info("keyboard controls active: q quits after this book, p pauses")
        else:
            LOGGER.debug("stdin is not a terminal; keyboard controls disabled")

    def stop(self) -> None:
        self._stop.set()
        if self._previous_sigint is not None:
            signal.signal(signal.SIGINT, self._previous_sigint)
        if self._previous_sigterm is not None:
            signal.signal(signal.SIGTERM, self._previous_sigterm)

    def __enter__(self) -> "RunController":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --------------------------------------------------------------- requests #

    def request_pause(self) -> None:
        with self._lock:
            self.pause_requested = True

    def request_quit(self, source: str) -> None:
        with self._lock:
            already_pending = self.quit_requested
            self.quit_requested = True

        if not already_pending:
            LOGGER.warning("stopping after the current book (requested by %s)", source)
            return

        # Asked twice: stop waiting for the current book to finish.
        LOGGER.critical("forced termination requested by %s", source)
        self.cleanup()
        raise ForcedTermination("forced termination requested")

    def clear_pause(self) -> None:
        with self._lock:
            self.pause_requested = False

    # --------------------------------------------------- temporary file registry #

    def register_temp(self, path: Path | None) -> None:
        if path is None:
            return
        with self._lock:
            self._temp_files.add(Path(path))

    def unregister_temp(self, path: Path | None) -> None:
        if path is None:
            return
        with self._lock:
            self._temp_files.discard(Path(path))

    def cleanup(self) -> None:
        """Kill any running ffmpeg and remove the files it was writing."""

        ffmpeg.terminate_all()
        with self._lock:
            pending = tuple(self._temp_files)
        for path in pending:
            try:
                path.unlink(missing_ok=True)
                LOGGER.info("removed the temporary file %s", path)
            except OSError:
                LOGGER.warning("could not remove the temporary file %s", path)

    # ----------------------------------------------------------- between books #

    def should_stop(self, *, prompt: Any = None) -> bool:
        """Called between books.  True means stop the run."""

        if self.pause_requested:
            self.clear_pause()
            if prompt is not None:
                prompt("paused — press Enter to continue")
                with self._quiet_keys():
                    try:
                        input()
                    except EOFError:
                        pass

        with self._lock:
            return self.quit_requested

    # ------------------------------------------------------------------ private #

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.request_quit(signal.Signals(signum).name)

    def _quiet_keys(self) -> "_QuietKeys":
        return _QuietKeys(self)

    def _watch_keys(self) -> None:
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        try:
            original = termios.tcgetattr(descriptor)
        except termios.error:
            LOGGER.debug("stdin does not support raw mode; keyboard controls disabled")
            return

        try:
            tty.setcbreak(descriptor)
            while not self._stop.is_set():
                if self._suspend_keys.is_set():
                    termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
                    while self._suspend_keys.is_set() and not self._stop.is_set():
                        time.sleep(0.1)
                    if not self._stop.is_set():
                        tty.setcbreak(descriptor)
                    continue

                readable, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not readable:
                    continue
                key = sys.stdin.read(1)
                if key.casefold() == "q":
                    self.request_quit("keyboard")
                elif key.casefold() == "p":
                    self.request_pause()
        except ForcedTermination:
            # Raised on the watcher thread; signal the main thread to unwind.
            os.kill(os.getpid(), signal.SIGINT)
        finally:
            try:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
            except termios.error:
                pass


class _QuietKeys:
    """Suspends raw key reading so a normal ``input()`` prompt works."""

    def __init__(self, controller: RunController) -> None:
        self.controller = controller

    def __enter__(self) -> None:
        self.controller._suspend_keys.set()
        time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        self.controller._suspend_keys.clear()
