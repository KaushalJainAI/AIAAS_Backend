"""
A sandbox timeout has to stop the code, not just stop waiting for it.

`Thread.join(timeout=...)` *returns*; it does not interrupt anything, and a
daemon thread only dies with the process. So an overrunning execution used to
be reported to the model as "Execution Timeout" and then keep running — a
`while True` burning a core and growing its allocations behind a turn that
looked like it had failed cleanly. That is the failure this file pins: it is
invisible from the outside, which is exactly why it needs a test.
"""
from __future__ import annotations

import threading
import time

from django.test import SimpleTestCase

from concurrent.futures import ThreadPoolExecutor
from sandbox.safe_execution import CodeSandbox, _stop_thread


def _sandbox_threads() -> int:
    return len([t for t in threading.enumerate()
                if type(t).__name__ == 'SandboxThread'])


class TimeoutKillsTests(SimpleTestCase):
    def setUp(self):
        self.before = _sandbox_threads()

    def test_a_runaway_loop_is_actually_stopped(self):
        sandbox = CodeSandbox()
        outcome = sandbox.execute('i = 0\nwhile True:\n    i += 1\n', timeout=1)

        self.assertFalse(outcome['success'])
        self.assertIn('Timeout', outcome['error'])
        # The point of the whole file: not merely that we stopped waiting.
        time.sleep(0.3)
        self.assertEqual(_sandbox_threads(), self.before)

    def test_the_timeout_message_does_not_claim_a_kill_that_failed(self):
        # A thread that ignores the interrupt is a leak, and the caller is told
        # so rather than being handed the same message as a clean stop. The
        # wording is what a reader of the transcript needs to know.
        sandbox = CodeSandbox()
        outcome = sandbox.execute('i = 0\nwhile True:\n    i += 1\n', timeout=1)
        self.assertNotIn('still running', outcome['error'])

    def test_normal_code_is_unaffected(self):
        sandbox = CodeSandbox()
        outcome = sandbox.execute('result = sum(range(100))', timeout=5)
        self.assertTrue(outcome['success'])
        self.assertEqual(outcome['result'], 4950)
        self.assertEqual(_sandbox_threads(), self.before)

    def test_repeated_timeouts_do_not_accumulate_threads(self):
        # The shape that actually kills a small box: one runaway is survivable,
        # a loop of them is not.
        sandbox = CodeSandbox()
        for _ in range(3):
            sandbox.execute('while True:\n    pass\n', timeout=1)
        time.sleep(0.3)
        self.assertEqual(_sandbox_threads(), self.before)


class StopThreadTests(SimpleTestCase):
    def test_reports_true_for_a_thread_that_has_already_finished(self):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        self.assertTrue(_stop_thread(thread))

    def test_reports_true_for_a_thread_that_never_started(self):
        self.assertTrue(_stop_thread(threading.Thread(target=lambda: None)))

    def test_interrupts_a_running_loop(self):
        stop_seen = threading.Event()

        def spin():
            try:
                while True:
                    pass
            except BaseException:      # noqa: BLE001 — SystemExit lands here
                # Swallowed rather than re-raised only to keep pytest's
                # unhandled-thread-exception warning out of the suite output.
                stop_seen.set()

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        time.sleep(0.05)               # let it get into the loop

        self.assertTrue(_stop_thread(thread))
        self.assertTrue(stop_seen.is_set())


class StopThreadTargetingTests(SimpleTestCase):
    """`_stop_thread` must never deliver `SystemExit` to a thread it does not own.

    A thread id names a slot, not a thread: CPython recycles ids once a thread
    finishes. Delivered into the main thread, `SystemExit` starts interpreter
    shutdown, `concurrent.futures` sets its global `_shutdown` flag, and every
    later `sync_to_async` call raises `RuntimeError: cannot schedule new futures
    after interpreter shutdown` -- a live server answering 500 to everything,
    health check included, with no traceback where the damage was done.
    """

    def test_refuses_to_target_the_main_thread(self):
        """A recycled id that happens to be the main thread's is never fired at."""
        main = threading.main_thread()

        class Pretender:
            """Alive, and claiming the main thread's id."""
            ident = main.ident

            @staticmethod
            def is_alive():
                return True

        self.assertFalse(_stop_thread(Pretender()))
        # And the interpreter is still able to schedule work.
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(lambda: 'alive').result(timeout=5), 'alive')

    def test_skips_an_id_that_now_belongs_to_another_thread(self):
        """If the id no longer maps to our thread object, do not raise into it."""
        other = threading.Thread(target=lambda: time.sleep(2), daemon=True)
        other.start()
        self.addCleanup(other.join, 3)

        class Impostor:
            """Claims a live id that belongs to `other`, not to itself."""
            ident = other.ident

            @staticmethod
            def is_alive():
                return True

        # Reports "not stopped" rather than killing `other`.
        self.assertFalse(_stop_thread(Impostor()))
        self.assertTrue(other.is_alive())

    def test_a_thread_that_finished_on_its_own_is_not_targeted(self):
        """Finished between the caller's check and here: nothing to kill."""
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        self.assertTrue(_stop_thread(done))
