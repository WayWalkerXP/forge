"""Interrupting a run safely.

A conversion is a transaction: temporary file, validate, promote, archive.
Interrupting in the middle of one is fine (the temporary file is discarded and
the source is untouched), but interrupting *between* books is tidier, so a quit
request is honoured at the next boundary rather than immediately.

Two levels, following the convention a long-running CLI tool ought to have:

*   The first request asks to stop after the current book.
*   A second request gives up waiting, kills ffmpeg, and removes the temporary
    file it was writing.

When the run is attended - stdin *and* stdout both terminals - ``q`` and ``p``
do the same thing without the ceremony of a signal.  Otherwise only signals
apply and the raw-key reader is never started: ``bin/forge-cron`` run by hand
has a terminal on stdin but its output redirected to a log, so the "keyboard
controls active" notice would go into the log where nobody reads it, and a
stray ``q`` would silently stop the nightly pass.

**The terminal mode belongs to the controller, not to the reader thread.**  The
reader is a daemon thread, so the interpreter does not wait for it at exit: a
restore in its own ``finally`` loses the race with process exit every time, and
the run ends leaving the terminal in cbreak - accepting input without echoing
it, needing a ``reset``.  :meth:`RunController.start` enters cbreak on the
calling thread and :meth:`RunController.stop` restores it there, so the mode is
tied to the controller's lifetime and not to when a thread happens to notice.
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
        self._allow_keys = allow_keys and sys.stdin.isatty() and sys.stdout.isatty()
        #: The terminal settings to put back, and the lock that makes entering
        #: and leaving cbreak safe to call from either thread.
        self._tty_original: Any = None
        self._tty_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle #

    def start(self) -> None:
        self._previous_sigint = signal.getsignal(signal.SIGINT)
        self._previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        if self._allow_keys and self._enter_cbreak():
            self._thread = threading.Thread(
                target=self._watch_keys, name="forge-keys", daemon=True
            )
            self._thread.start()
            LOGGER.info("keyboard controls active: q quits after this book, p pauses")
        elif not self._allow_keys:
            LOGGER.debug("not an attended run; keyboard controls disabled")

    def stop(self) -> None:
        self._stop.set()

        # The terminal first, and on this thread: the reader is a daemon and
        # will be killed mid-select at interpreter exit, so anything it was
        # going to put back would never be put back.  Idempotent, because a
        # forced termination can reach here twice.
        self._leave_cbreak()
        if self._thread is not None:
            # One select timeout plus slack.  Only tidiness - the mode is
            # already restored - so a wedged reader must not hold up the exit.
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                LOGGER.debug("the key reader did not finish; leaving it to exit")
            self._thread = None

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

    def _enter_cbreak(self) -> bool:
        """Put the terminal into cbreak, remembering how to put it back.

        False when stdin cannot do it at all, which leaves the run on signals
        alone rather than failing.
        """

        import termios
        import tty

        with self._tty_lock:
            if self._tty_original is not None:
                return True
            descriptor = sys.stdin.fileno()
            try:
                self._tty_original = termios.tcgetattr(descriptor)
                tty.setcbreak(descriptor)
            except (termios.error, ValueError, OSError):
                self._tty_original = None
                LOGGER.debug("stdin does not support cbreak; keyboard controls disabled")
                return False
            return True

    def _leave_cbreak(self) -> None:
        """Put the terminal back the way it was found.  Safe to call twice."""

        import termios

        with self._tty_lock:
            original = self._tty_original
            self._tty_original = None
        if original is None:
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
        except (termios.error, ValueError, OSError):
            LOGGER.warning("could not restore the terminal mode; `reset` will fix it")

    def _watch_keys(self) -> None:
        """Read single keypresses until the run ends.

        Deliberately owns no terminal state: :meth:`stop` has already restored
        the mode by the time this thread is killed at exit.
        """

        try:
            while not self._stop.is_set():
                if self._suspend_keys.is_set():
                    time.sleep(0.1)
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
        except (OSError, ValueError):
            # stdin closed under us; signals still work.
            LOGGER.debug("the key reader lost stdin")


class _QuietKeys:
    """Suspends raw key reading so a normal ``input()`` prompt works.

    Both halves run on the calling thread: the prompt needs the terminal back
    in canonical mode to echo what is typed, and the reader has to be parked
    first or it eats the Enter that ends the prompt.
    """

    def __init__(self, controller: RunController) -> None:
        self.controller = controller

    def __enter__(self) -> None:
        self.controller._suspend_keys.set()
        time.sleep(0.05)
        self.controller._leave_cbreak()

    def __exit__(self, *exc_info: object) -> None:
        if self.controller._allow_keys and not self.controller._stop.is_set():
            self.controller._enter_cbreak()
        self.controller._suspend_keys.clear()
