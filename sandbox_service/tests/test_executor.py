"""Direct tests of the sidecar's subprocess executor.

These run the real subprocess (stdlib-only `runner.py`), so they work on any
platform: on POSIX they exercise the rlimit/killpg path, on Windows the plain
timeout-and-kill fallback. Plain `unittest` — the executor has no Django
dependency, and neither should its tests.
"""
from __future__ import annotations

import os
import sys
import unittest

# The sidecar is not importable as a package (the backend never imports it and
# the image copies loose files), so put its directory on the path for the test.
_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC_DIR not in sys.path:
    sys.path.insert(0, _SVC_DIR)

import executor  # noqa: E402

_POSIX = os.name == "posix"


class ExecutorTests(unittest.TestCase):
    def test_runs_and_returns_result_variable(self):
        env = executor.execute("result = 2 + 3")
        self.assertTrue(env["success"], env)
        self.assertEqual(env["result"], 5)
        self.assertFalse(env["timed_out"])

    def test_captures_stdout(self):
        env = executor.execute("print('hello sandbox')")
        self.assertTrue(env["success"], env)
        self.assertIn("hello sandbox", env["output"])

    def test_exception_is_reported_not_raised(self):
        env = executor.execute("raise ValueError('boom')")
        self.assertFalse(env["success"])
        self.assertIn("ValueError", env["error"])
        self.assertIn("boom", env["error"])

    def test_non_json_result_is_stringified(self):
        env = executor.execute("result = object()")
        self.assertTrue(env["success"], env)
        self.assertIsInstance(env["result"], str)

    def test_runaway_loop_is_killed(self):
        env = executor.execute("while True:\n    pass", wall_seconds=1, cpu_seconds=1)
        self.assertFalse(env["success"])
        self.assertTrue(env["timed_out"])

    @unittest.skipUnless(_POSIX, "rlimit memory cap is POSIX-only")
    def test_memory_limit_stops_a_huge_allocation(self):
        # 64 MB cap, then try to allocate far more. Either MemoryError bubbles
        # up (reported as a failure) or the kernel kills the child; both are a
        # non-success envelope, never a crash of the parent.
        env = executor.execute(
            "x = bytearray(1024 * 1024 * 1024)",
            mem_bytes=64 * 1024 * 1024,
            wall_seconds=5,
        )
        self.assertFalse(env["success"], env)


if __name__ == "__main__":
    unittest.main()
