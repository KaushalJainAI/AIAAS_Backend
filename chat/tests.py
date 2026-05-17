"""
Tests for the guest (unauthenticated) chat pipeline.

Covers:
  - Guest user helper is idempotent.
  - Token-budget guard accepts small contexts and rejects oversized ones.
  - Throttle scopes are wired to the rates defined in settings.
  - Guest session create + fetch endpoints reject auth gating.
  - Guest fetch returns 404 for sessions owned by a real user.
  - The NVIDIA stream view emits a structured SSE error when the API key is unset.
  - The guest streaming view requires POST and rejects invalid JSON.

These tests run without hitting the real NVIDIA API.
"""
from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.test import AsyncClient
from rest_framework.test import APIClient

from chat import guest as guest_mod
from chat.guest import fits_token_budget, get_guest_user_sync
from chat.models import ChatSession


User = get_user_model()


class GuestUserHelperTests(TestCase):
    def setUp(self):
        guest_mod._guest_user_cache.clear()

    def test_get_guest_user_sync_is_idempotent(self):
        u1 = get_guest_user_sync()
        u2 = get_guest_user_sync()
        self.assertEqual(u1.pk, u2.pk)
        self.assertFalse(u1.is_active)
        self.assertFalse(u1.has_usable_password())

    @override_settings(GUEST_USER_EMAIL="custom_guest@example.com")
    def test_guest_user_email_from_settings(self):
        guest_mod._guest_user_cache.clear()
        user = get_guest_user_sync()
        self.assertEqual(user.email, "custom_guest@example.com")


class TokenBudgetTests(TestCase):
    @override_settings(GUEST_CHAT_MAX_TOKENS=200_000)
    def test_small_prompt_fits(self):
        self.assertTrue(fits_token_budget("x" * 1000, 4096))

    @override_settings(GUEST_CHAT_MAX_TOKENS=200_000)
    def test_oversized_prompt_rejected(self):
        # 700K chars / 3 ≈ 233K tokens, exceeds 200K.
        self.assertFalse(fits_token_budget("x" * 700_000, 4096))

    @override_settings(GUEST_CHAT_MAX_TOKENS=200_000)
    def test_output_budget_counted(self):
        # ~150K input tokens + 60K output > 200K.
        self.assertFalse(fits_token_budget("x" * 450_000, 60_000))


class GuestThrottleConfigTests(TestCase):
    def test_throttle_scopes_resolve(self):
        """Each guest throttle resolves its rate via the DRF rate registry."""
        from core.throttling import (
            GuestChatDayThrottle,
            GuestChatHourThrottle,
            GuestChatMinuteThrottle,
        )

        # The test settings inflate every existing rate to 100000/second,
        # so we just confirm the scope is wired and the throttle parses a rate.
        for cls in (GuestChatMinuteThrottle, GuestChatHourThrottle, GuestChatDayThrottle):
            inst = cls()
            self.assertIsNotNone(inst.rate, f"{cls.__name__} has no rate")
            num, duration = inst.parse_rate(inst.rate)
            self.assertGreater(num, 0)
            self.assertGreater(duration, 0)


class GuestSessionEndpointTests(TestCase):
    def setUp(self):
        guest_mod._guest_user_cache.clear()
        self.client = APIClient()

    def test_create_guest_session_no_auth_required(self):
        resp = self.client.post("/api/chat/guest/sessions/", {"title": "Hello"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["title"], "Hello")
        self.assertEqual(body["llm_provider"], "nvidia")
        # The session is bound to the shared guest user.
        session = ChatSession.objects.get(id=body["id"])
        self.assertEqual(session.user, get_guest_user_sync())

    def test_get_guest_session_returns_only_guest_owned(self):
        # A session owned by a real user must NOT leak through the guest endpoint.
        real_user = User.objects.create_user(username="alice", password="pw")
        real_session = ChatSession.objects.create(user=real_user, title="private")

        resp = self.client.get(f"/api/chat/guest/sessions/{real_session.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_get_guest_session_round_trip(self):
        create = self.client.post("/api/chat/guest/sessions/", {"title": "Hi"}, format="json")
        sid = create.json()["id"]
        fetch = self.client.get(f"/api/chat/guest/sessions/{sid}/")
        self.assertEqual(fetch.status_code, 200)
        self.assertEqual(fetch.json()["id"], sid)


class GuestStreamViewTests(TransactionTestCase):
    """
    Tests the streaming view via Django's AsyncClient.
    TransactionTestCase + AsyncClient avoids the SQLite "database table is locked"
    error that arises when the async event_stream and the wrapping sync test
    transaction share the same in-memory connection.
    """

    def setUp(self):
        guest_mod._guest_user_cache.clear()
        self.async_client = AsyncClient()
        # Create the session via the sync DRF client — the async view only needs it to exist.
        api = APIClient()
        resp = api.post("/api/chat/guest/sessions/", {"title": "T"}, format="json")
        self.session_id = resp.json()["id"]
        self.url = f"/api/chat/guest/sessions/{self.session_id}/message/stream/"

    async def _consume(self, resp) -> str:
        if not getattr(resp, "streaming", False):
            return resp.content.decode("utf-8")
        sc = resp.streaming_content
        chunks = []
        if hasattr(sc, "__aiter__"):
            async for c in sc:
                chunks.append(c if isinstance(c, (bytes, bytearray)) else c.encode("utf-8"))
        else:
            for c in sc:
                chunks.append(c if isinstance(c, (bytes, bytearray)) else c.encode("utf-8"))
        return b"".join(chunks).decode("utf-8")

    async def test_rejects_non_post(self):
        resp = await self.async_client.get(self.url)
        self.assertEqual(resp.status_code, 405)
        body = await self._consume(resp)
        self.assertIn("Method not allowed", body)

    async def test_rejects_invalid_json(self):
        resp = await self.async_client.post(self.url, data="not-json", content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        body = await self._consume(resp)
        self.assertIn("Invalid JSON", body)

    async def test_rejects_empty_content(self):
        resp = await self.async_client.post(self.url, data=json.dumps({"content": ""}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        body = await self._consume(resp)
        self.assertIn("Content is required", body)

    @override_settings(NVIDIA_API_KEY="")
    async def test_stream_reports_missing_api_key_via_sse(self):
        resp = await self.async_client.post(
            self.url,
            data=json.dumps({"content": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = await self._consume(resp)
        self.assertIn('"type": "error"', body)
        self.assertIn("NVIDIA API key not configured", body)

    @override_settings(NVIDIA_API_KEY="fake-test-key")
    async def test_stream_persists_messages_when_nvidia_returns_text(self):
        """The view persists user + assistant messages when NVIDIA streams content."""
        async def fake_stream(messages, *, model=None, temperature=0.6, max_tokens=4096):
            yield {"type": "content", "content": "Hello, "}
            yield {"type": "content", "content": "world!"}
            yield {"type": "done"}

        with mock.patch("chat.guest_views.stream_nvidia_chat", side_effect=fake_stream):
            resp = await self.async_client.post(
                self.url,
                data=json.dumps({"content": "hi"}),
                content_type="application/json",
            )
            body = await self._consume(resp)

        self.assertIn('"type": "content_chunk"', body)
        self.assertIn('"type": "done"', body)

        from asgiref.sync import sync_to_async

        @sync_to_async
        def fetch_msgs():
            session = ChatSession.objects.get(id=self.session_id)
            return list(session.messages.order_by("created_at"))

        msgs = await fetch_msgs()
        self.assertEqual([m.role for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0].content, "hi")
        self.assertEqual(msgs[1].content, "Hello, world!")
        self.assertTrue(msgs[1].metadata.get("guest"))

    @override_settings(GUEST_CHAT_MAX_TOKENS=10)
    async def test_stream_rejects_when_token_budget_exceeded(self):
        resp = await self.async_client.post(
            self.url,
            data=json.dumps({"content": "x" * 100}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = await self._consume(resp)
        self.assertIn("200K token guest limit", body)


class GuestUrlRoutingTests(TestCase):
    def test_guest_urls_are_resolvable(self):
        from django.urls import resolve
        for path in (
            "/api/chat/guest/sessions/",
            "/api/chat/guest/sessions/abc-123/",
            "/api/chat/guest/sessions/abc-123/message/stream/",
        ):
            match = resolve(path)
            self.assertTrue(match.func, f"{path} did not resolve to a view")
