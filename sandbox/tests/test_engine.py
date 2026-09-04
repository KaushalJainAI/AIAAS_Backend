"""The one door (`sandbox.engine`) and the two engines behind it.

Three things are pinned here:

- **Engine selection is by config, and there is no silent fallback.** With
  `SANDBOX_ENGINE=service`, a run goes to the sidecar client even when the
  sidecar is unreachable — it must fail loudly, never quietly drop to the
  weaker in-process engine.
- **The service client normalizes whatever the sidecar returns** into the same
  envelope the in-process engine produces.
- **The in-process fallback's AST guard rejects the escapes it used to miss** —
  `type(()).mro()`, `vars`, `dir` — so the dev fallback is at least not
  trivially bypassable.
"""
from __future__ import annotations

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from sandbox.engine import arun_code
from sandbox.safe_execution import SafeCodeValidator


class EngineSelectionTests(SimpleTestCase):
    @override_settings(SANDBOX_ENGINE="inprocess")
    def test_inprocess_engine_runs_code(self):
        outcome = async_to_sync(arun_code)("result = 6 * 7")
        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["result"], 42)
        self.assertIn("timed_out", outcome)

    @override_settings(SANDBOX_ENGINE="service", SANDBOX_SERVICE_URL="http://sandbox:8100")
    def test_service_engine_calls_the_client(self):
        sentinel = {
            "success": True, "result": 42, "output": "",
            "stderr": "", "error": None, "timed_out": False,
        }
        with mock.patch(
            "sandbox.service_client.run_via_service",
            new=mock.AsyncMock(return_value=sentinel),
        ) as call:
            outcome = async_to_sync(arun_code)("result = 42")
        call.assert_awaited_once()
        self.assertEqual(outcome, sentinel)

    @override_settings(SANDBOX_ENGINE="service")
    def test_service_failure_does_not_fall_back_to_inprocess(self):
        # The sidecar is down: the client returns a failure envelope, and the
        # engine must surface it rather than running the code in-process.
        with mock.patch("sandbox.safe_execution.CodeSandbox.execute") as inproc:
            with mock.patch(
                "sandbox.service_client.run_via_service",
                new=mock.AsyncMock(return_value={
                    "success": False, "result": None, "output": "",
                    "stderr": "", "error": "The code sandbox is unavailable right now.",
                    "timed_out": False,
                }),
            ):
                outcome = async_to_sync(arun_code)("result = 1")
        inproc.assert_not_called()
        self.assertFalse(outcome["success"])
        self.assertIn("unavailable", outcome["error"])


class InProcessHardeningTests(SimpleTestCase):
    """The dev fallback's validator must reject the known escapes."""

    def _errors(self, code: str) -> list[str]:
        v = SafeCodeValidator()
        v.validate(code)
        return v.errors

    def test_mro_walk_to_object_is_blocked(self):
        # The escape that used to slip through: reach `object` with no dunder.
        self.assertTrue(self._errors("type(()).mro()"))

    def test_vars_and_dir_are_not_available(self):
        # Removed from SAFE_BUILTINS; they are introspection stepping stones.
        from sandbox.safe_execution import SAFE_BUILTINS
        self.assertNotIn("vars", SAFE_BUILTINS)
        self.assertNotIn("dir", SAFE_BUILTINS)

    def test_classic_subclasses_escape_still_blocked(self):
        self.assertTrue(self._errors("().__class__.__bases__[0].__subclasses__()"))

    def test_ordinary_code_still_passes(self):
        self.assertEqual(self._errors("result = sum(range(10))"), [])


class ServiceClientNormalizationTests(SimpleTestCase):
    @override_settings(SANDBOX_SERVICE_URL="http://sandbox:8100")
    def test_partial_envelope_is_backfilled(self):
        from sandbox import service_client

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True, "result": 5}  # missing the rest

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        with mock.patch.object(service_client.httpx, "AsyncClient", _Client):
            env = async_to_sync(service_client.run_via_service)("result = 5")
        self.assertTrue(env["success"])
        self.assertEqual(env["result"], 5)
        # Backfilled keys:
        for key in ("output", "stderr", "error", "timed_out"):
            self.assertIn(key, env)

    @override_settings(SANDBOX_SERVICE_URL="http://sandbox:8100")
    def test_transport_error_becomes_a_failure_envelope(self):
        from sandbox import service_client

        class _Boom:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise service_client.httpx.ConnectError("refused")

        with mock.patch.object(service_client.httpx, "AsyncClient", _Boom):
            env = async_to_sync(service_client.run_via_service)("result = 5")
        self.assertFalse(env["success"])
        self.assertTrue(env["error"])
