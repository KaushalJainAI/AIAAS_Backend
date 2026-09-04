"""
Tests for `llm.context.ExecutionContext` — what a provider handler is handed
besides its config.

This file was `compiler/tests/test_schemas.py`. Before that it was
`test_compiler.py` and covered `validate_dag`, the config accessors,
topological sort and the `{{ $node[...] }}` expression resolver; all four went
with the DAG runtime. The tests for `variables` / `set_variable` /
`get_variable` went too — they were the only callers those methods had, which
is the whole reason the methods are gone.

What is pinned here is the credential lookup that
`llm/handlers/openai_compatible.py` awaits on every model call.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from llm.context import ExecutionContext


def _ctx(**overrides) -> ExecutionContext:
    base = dict(user_id=1)
    base.update(overrides)
    return ExecutionContext(**base)


class ConstructionTests(SimpleTestCase):
    def test_user_id_is_all_a_caller_passes(self):
        # llm/access.py and inference/engine.py both build it exactly so.
        self.assertEqual(_ctx().user_id, 1)

    def test_credentials_defaults_to_empty(self):
        self.assertEqual(_ctx().credentials, {})

    def test_none_is_coerced_rather_than_rejected(self):
        self.assertEqual(_ctx(credentials=None).credentials, {})


class GetCredentialTests(SimpleTestCase):
    def test_none_id_returns_none_without_a_lookup(self):
        self.assertIsNone(async_to_sync(_ctx().get_credential)(None))

    def test_already_resolved_credential_is_preferred(self):
        ctx = _ctx(credentials={"7": {"api_key": "cached"}})
        with patch("credentials.manager.get_credential_manager") as mgr:
            got = async_to_sync(ctx.get_credential)(7)
            mgr.assert_not_called()
        self.assertEqual(got, {"api_key": "cached"})

    def test_lookup_populates_the_cache(self):
        ctx = _ctx()
        manager = AsyncMock()
        manager.get_credential = AsyncMock(return_value={"api_key": "fetched"})
        with patch("credentials.manager.get_credential_manager", return_value=manager):
            got = async_to_sync(ctx.get_credential)(9)
        self.assertEqual(got, {"api_key": "fetched"})
        # Cached, so a second call on the same context does not re-fetch.
        self.assertEqual(ctx.credentials["9"], {"api_key": "fetched"})

    def test_lookup_failure_returns_none_rather_than_raising(self):
        # A missing credential is a common, recoverable user error; the caller
        # turns None into a "no verified credential" message.
        ctx = _ctx()
        with patch("credentials.manager.get_credential_manager",
                   side_effect=RuntimeError("vault down")):
            self.assertIsNone(async_to_sync(ctx.get_credential)(9))
