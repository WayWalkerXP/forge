"""The lock and the run controller."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempCaseMixin

from forge.control import RunController
from forge.errors import ForcedTermination, LockError
from forge.lock import Lock


class Locking(TempCaseMixin):
    def test_acquire_and_release(self) -> None:
        path = self.root / "forge.lock"
        with Lock(path):
            self.assertTrue(path.exists())
            self.assertIn(f"pid={os.getpid()}", path.read_text())
        self.assertFalse(path.exists())

    def test_a_concurrent_run_is_refused(self) -> None:
        path = self.root / "forge.lock"
        with Lock(path):
            with self.assertRaises(LockError) as caught:
                Lock(path).acquire()
            self.assertIn(str(os.getpid()), str(caught.exception))

    def test_a_lock_from_a_dead_process_is_taken_over(self) -> None:
        # Otherwise a machine that lost power would need manual intervention
        # before the next nightly run could do anything.
        path = self.root / "forge.lock"
        path.write_text("pid=999999\n")
        with Lock(path):
            self.assertIn(f"pid={os.getpid()}", path.read_text())

    def test_a_malformed_lock_is_treated_as_stale(self) -> None:
        path = self.root / "forge.lock"
        path.write_text("nonsense\n")
        with Lock(path):
            self.assertTrue(path.exists())

    def test_the_lock_directory_is_created(self) -> None:
        path = self.root / "nested" / "deeper" / "forge.lock"
        with Lock(path):
            self.assertTrue(path.exists())


class Controller(TempCaseMixin):
    def make(self) -> RunController:
        # Keyboard watching is disabled: the test process has no usable tty and
        # signals are what an unattended run actually receives.
        return RunController(allow_keys=False)

    def test_a_fresh_controller_wants_to_continue(self) -> None:
        controller = self.make()
        self.assertFalse(controller.should_stop())

    def test_one_quit_request_stops_at_the_next_boundary(self) -> None:
        controller = self.make()
        controller.request_quit("test")
        self.assertTrue(controller.quit_requested)
        self.assertTrue(controller.should_stop())

    def test_a_second_quit_request_forces_termination(self) -> None:
        controller = self.make()
        controller.request_quit("test")
        with self.assertRaises(ForcedTermination):
            controller.request_quit("test")

    def test_forced_termination_removes_registered_temporary_files(self) -> None:
        controller = self.make()
        temp = self.root / "book.tmp.m4b"
        temp.write_bytes(b"partial output")
        controller.register_temp(temp)

        controller.request_quit("test")
        with self.assertRaises(ForcedTermination):
            controller.request_quit("test")

        self.assertFalse(temp.exists())

    def test_an_unregistered_temporary_file_is_left_alone(self) -> None:
        controller = self.make()
        temp = self.root / "book.tmp.m4b"
        temp.write_bytes(b"partial output")
        controller.register_temp(temp)
        controller.unregister_temp(temp)

        controller.cleanup()
        self.assertTrue(temp.exists())

    def test_cleanup_tolerates_an_already_deleted_file(self) -> None:
        controller = self.make()
        controller.register_temp(self.root / "gone.tmp.m4b")
        controller.cleanup()  # must not raise

    def test_registering_none_is_harmless(self) -> None:
        controller = self.make()
        controller.register_temp(None)
        controller.unregister_temp(None)
        controller.cleanup()

    def test_pause_is_cleared_once_honoured(self) -> None:
        controller = self.make()
        controller.request_pause()
        self.assertTrue(controller.pause_requested)
        controller.should_stop()  # no prompt, so it just clears
        self.assertFalse(controller.pause_requested)

    def test_signal_handlers_are_restored(self) -> None:
        import signal

        before = signal.getsignal(signal.SIGINT)
        with self.make():
            self.assertNotEqual(signal.getsignal(signal.SIGINT), before)
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_a_signal_is_treated_as_a_quit_request(self) -> None:
        import signal

        with self.make() as controller:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(controller.quit_requested)


class KeyboardGate(unittest.TestCase):
    """When the raw-key reader is allowed to start.

    Not merely a preference: starting it decides whether the terminal is put
    into cbreak at all, and `bin/forge-cron` run by hand has a terminal on
    stdin with its output redirected to a log.
    """

    def test_an_unattended_run_does_not_watch_keys(self) -> None:
        for stdin_tty, stdout_tty in ((False, False), (True, False), (False, True)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty):
                with mock.patch.object(sys, "stdin"), mock.patch.object(sys, "stdout"):
                    sys.stdin.isatty.return_value = stdin_tty
                    sys.stdout.isatty.return_value = stdout_tty
                    self.assertFalse(RunController(allow_keys=True)._allow_keys)

    def test_an_attended_run_watches_keys(self) -> None:
        with mock.patch.object(sys, "stdin"), mock.patch.object(sys, "stdout"):
            sys.stdin.isatty.return_value = True
            sys.stdout.isatty.return_value = True
            self.assertTrue(RunController(allow_keys=True)._allow_keys)

    def test_asking_for_no_keys_always_wins(self) -> None:
        with mock.patch.object(sys, "stdin"), mock.patch.object(sys, "stdout"):
            sys.stdin.isatty.return_value = True
            sys.stdout.isatty.return_value = True
            self.assertFalse(RunController(allow_keys=False)._allow_keys)


#: An attended run, start to finish, in a child process on a pty.  Printing
#: STARTED before the loop lets the test know cbreak is in force.
_ATTENDED_RUN = """
import sys, time
sys.path.insert(0, {root!r})
from forge.control import RunController

with RunController(allow_keys=True) as controller:
    print("STARTED", flush=True)
    for _ in range(40):
        if controller.quit_requested:
            break
        time.sleep(0.05)
print("ENDED", flush=True)
"""


#: A run that pauses.  `should_stop(prompt=...)` is how cli.py polls between
#: books, and the prompt it passes is the renderer's own info line.
_PAUSING_RUN = """
import sys, time
sys.path.insert(0, {root!r})
from forge.control import RunController

with RunController(allow_keys=True) as controller:
    print("STARTED", flush=True)
    for _ in range(100):
        if controller.should_stop(prompt=lambda text: print(text, flush=True)):
            break
        time.sleep(0.05)
print("ENDED", flush=True)
"""


class TerminalMode(unittest.TestCase):
    """The terminal must be handed back the way it was found.

    This is the bug the keyboard controls shipped with: the reader thread
    restored the mode in its own `finally`, but it is a daemon thread, so the
    interpreter killed it mid-select at exit and the restore never ran.  Every
    attended `forge run` and `forge shelve` ended with the terminal in cbreak -
    accepting input without echoing it - and needed a `reset`.  It reproduced
    five times out of five, and no test caught it because every other test here
    passes `allow_keys=False`.

    Driven through a real child process on a pty, because the mode is process
    state: asserting it in-process would leave *this* terminal in cbreak if the
    assertion failed.
    """

    #: Generous: it only has to cover process start-up on a loaded machine.
    TIMEOUT = 30.0

    def setUp(self) -> None:
        super().setUp()
        if not hasattr(os, "openpty"):  # pragma: no cover - Linux has it
            self.skipTest("no pty support")

    @staticmethod
    def echo_is_on(fd: int) -> bool:
        return bool(termios.tcgetattr(fd)[3] & termios.ECHO)

    def drain(self, fd: int, seconds: float) -> str:
        chunks: list[str] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.05)
            if not readable:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode(errors="replace"))
        return "".join(chunks)

    def test_an_attended_run_leaves_the_terminal_as_it_found_it(self) -> None:
        import pty

        root = str(Path(__file__).resolve().parent.parent)
        master, slave = pty.openpty()
        self.assertTrue(self.echo_is_on(slave), "the pty should start with echo on")

        proc = subprocess.Popen(
            [sys.executable, "-c", _ATTENDED_RUN.format(root=root)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        self.addCleanup(self._reap, proc, master)
        os.close(slave)

        output = ""
        deadline = time.monotonic() + self.TIMEOUT
        while "STARTED" not in output and proc.poll() is None:
            output += self.drain(master, 0.2)
            if time.monotonic() > deadline:
                self.fail(f"the child never started: {output!r}")

        # While the run is going the terminal is in cbreak, which is the whole
        # point of the feature - if this is already True the test proves nothing.
        self.assertFalse(
            self.echo_is_on(master), "the run should have put the terminal in cbreak"
        )

        os.write(master, b"q")
        while proc.poll() is None:
            output += self.drain(master, 0.2)
            if time.monotonic() > deadline:
                self.fail("the child did not stop when sent q")
        output += self.drain(master, 0.3)

        self.assertIn("ENDED", output)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(
            self.echo_is_on(master),
            "the run left the terminal without echo; a `reset` would be needed",
        )

    def test_the_pause_prompt_echoes_and_then_hands_the_mode_back(self) -> None:
        # The prompt uses input(), which needs canonical mode to show what is
        # typed; the reader has to be parked first or it eats the Enter.  Both
        # now happen on the calling thread, so this also checks the mode is
        # handed back afterwards rather than left canonical for the rest of the
        # run.
        import pty

        root = str(Path(__file__).resolve().parent.parent)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-c", _PAUSING_RUN.format(root=root)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        self.addCleanup(self._reap, proc, master)
        os.close(slave)

        output = ""
        deadline = time.monotonic() + self.TIMEOUT
        while "STARTED" not in output and proc.poll() is None:
            output += self.drain(master, 0.2)
            if time.monotonic() > deadline:
                self.fail(f"the child never started: {output!r}")

        os.write(master, b"p")
        while "paused" not in output and proc.poll() is None:
            output += self.drain(master, 0.2)
            if time.monotonic() > deadline:
                self.fail(f"no pause prompt: {output!r}")

        # Canonical mode while the prompt is up, or the user cannot see what
        # they type at it.
        self.assertTrue(self.echo_is_on(master), "the pause prompt should echo")

        os.write(master, b"\n")
        time.sleep(0.4)
        self.assertIsNone(proc.poll(), "Enter should resume the run, not end it")
        self.assertFalse(
            self.echo_is_on(master), "the run should be back in cbreak after the prompt"
        )

        os.write(master, b"q")
        while proc.poll() is None:
            output += self.drain(master, 0.2)
            if time.monotonic() > deadline:
                self.fail("the child did not stop when sent q")
        output += self.drain(master, 0.3)

        self.assertIn("ENDED", output)
        self.assertTrue(self.echo_is_on(master), "the run left the terminal without echo")

    def _reap(self, proc: subprocess.Popen, master: int) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        try:
            os.close(master)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
