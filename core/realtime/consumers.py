"""
The per-user WebSocket consumer pattern, written once.

Imagine, the canvas agent and the help panel each had their own copy of the same
consumer: read `scope["user"]`, reject if unauthenticated, join a group named
after the user, and discard that group on disconnect inside a try/finally. The
copies had already drifted on the details that matter during an incident —
whether the close carries a code, whether disconnect logs anything — without
any of those differences being a decision someone made.

Subclasses declare `group_prefix` and handle their own messages. The parts that
must not vary (authentication, group lifecycle, never raising out of
`disconnect`) are not overridable hooks; they are the base's `connect` and
`disconnect` themselves.

`streaming.consumers.ExecutionConsumer` deliberately does not inherit from
this: it joins several groups and verifies per-execution access, so folding it
in would mean bending the base into something that no longer states a simple
contract.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class UserGroupConsumer(AsyncWebsocketConsumer):
    """Authenticated consumer bound to a single per-user channel group."""

    #: Group name is f"{group_prefix}_{user_id}". Required.
    group_prefix: str = ""
    #: Close code used when the connection is not authenticated. 4001 is what
    #: the execution socket already sends, so clients have one code to match.
    reject_code: int = 4001
    #: Send a `{"type": "connected"}` frame once the group is joined. Off by
    #: default: only clients that gate their first send on it need it.
    send_connect_ack: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.user_id: int | None = None
        self.group_name: str | None = None

    # ── lifecycle ──

    async def connect(self):
        self.user = self.scope.get("user")
        if not (self.user and self.user.is_authenticated):
            await self.close(code=self.reject_code)
            return

        self.user_id = self.user.pk
        self.group_name = f"{self.group_prefix}_{self.user_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        if self.send_connect_ack:
            await self.send_json({"type": "connected", "user_id": self.user_id})

        logger.info("%s connected for user %s", type(self).__name__, self.user_id)
        await self.on_connect()

    async def disconnect(self, close_code):
        """Leave the group. Never raises — a failure here cannot be handled."""
        try:
            if self.group_name:
                await self.channel_layer.group_discard(
                    self.group_name, self.channel_name,
                )
        except Exception as exc:
            logger.warning(
                "%s disconnect cleanup error: %s", type(self).__name__, exc,
            )
        finally:
            logger.info(
                "%s disconnected for user %s (code=%s)",
                type(self).__name__, self.user_id or "?", close_code,
            )

    async def receive(self, text_data=None, bytes_data=None):
        """Decode one client frame and route it, converting failures to errors."""
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            await self.send_error("Malformed JSON frame")
            return

        try:
            await self.handle_message(data.get("type"), data)
        except Exception as exc:
            logger.exception("%s failed handling %r",
                             type(self).__name__, data.get("type"))
            await self.send_error(str(exc))

    # ── helpers for subclasses ──

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send a frame, tolerating a socket the client has already dropped."""
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception as exc:
            logger.warning("%s could not send to client: %s",
                           type(self).__name__, exc)

    async def send_error(self, message: str) -> None:
        await self.send_json({"type": "error", "message": message})

    async def cache_scoped(self, key: str, value: Any, timeout: int = 3600) -> None:
        """Write a per-user cache entry. Async, so it cannot block the loop."""
        from django.core.cache import cache
        await cache.aset(f"{key}_{self.user_id}", value, timeout=timeout)

    # ── overridable ──

    async def on_connect(self) -> None:
        """Run after the group is joined. Default: nothing."""

    async def handle_message(self, message_type: str | None,
                             data: dict[str, Any]) -> None:
        """Handle one decoded client frame. Default: ignore, answer pings."""
        if message_type == "ping":
            await self.send_json({"type": "pong"})
