"""The lock and the run controller."""

from __future__ import annotations

import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
