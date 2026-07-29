"""
Guest-mode helpers for anonymous AI chat.

The guest pipeline is intentionally narrow:
  - One shared Django user (settings.GUEST_USER_EMAIL) owns every guest
    ChatSession so existing FK constraints stay intact.
  - NVIDIA NIM API key is taken from settings.NVIDIA_API_KEY (env var),
    bypassing the per-user credential vault.
  - Streaming uses the same SSE format the frontend already handles for the
    authenticated send_message_stream view.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Whitelist of tools a guest is allowed to invoke from the agentic loop.
GUEST_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "search_web",
    "web_search",
    "execute_code",
    "run_code",
    "python",
})

_guest_user_cache: dict[str, object] = {}


def _estimate_tokens(text: str) -> int:
    """Rough char/3 heuristic — matches the budget the user specified."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def fits_token_budget(prompt_text: str, max_output_tokens: int) -> bool:
    """Return True if prompt + max output fits within GUEST_CHAT_MAX_TOKENS."""
    budget = getattr(settings, "GUEST_CHAT_MAX_TOKENS", 200_000)
    return _estimate_tokens(prompt_text) + max(0, int(max_output_tokens)) <= budget


@sync_to_async
def get_guest_user():
    """Get-or-create the shared guest user. Cached per-process."""
    cached = _guest_user_cache.get("user")
    if cached is not None:
        return cached

    User = get_user_model()
    email = getattr(settings, "GUEST_USER_EMAIL", "guest@aiaas.local")

    defaults = {"is_active": False}
    # Username field varies by project; handle both common cases.
    username_field = getattr(User, "USERNAME_FIELD", "username")
    lookup = {username_field: email} if username_field != "email" else {"email": email}
    if username_field != "email":
        defaults["email"] = email

    user, created = User.objects.get_or_create(defaults=defaults, **lookup)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        logger.info("Created guest chat user: %s", email)

    _guest_user_cache["user"] = user
    return user


def get_guest_user_sync():
    """Synchronous variant for use in management commands."""
    cached = _guest_user_cache.get("user")
    if cached is not None:
        return cached

    User = get_user_model()
    email = getattr(settings, "GUEST_USER_EMAIL", "guest@aiaas.local")

    username_field = getattr(User, "USERNAME_FIELD", "username")
    lookup = {username_field: email} if username_field != "email" else {"email": email}
    defaults = {"is_active": False}
    if username_field != "email":
        defaults["email"] = email

    user, created = User.objects.get_or_create(defaults=defaults, **lookup)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    _guest_user_cache["user"] = user
    return user


async def stream_nvidia_chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.6,
    max_tokens: int = 4096,
) -> AsyncIterator[dict]:
    """
    Stream a chat completion from NVIDIA NIM.

    Yields dicts of shape:
      {"type": "content", "content": str}
      {"type": "thinking", "content": str}
      {"type": "error", "message": str}
      {"type": "done"}

    Mirrors the SSE-emitting structure used by NvidiaNode.stream_execute in
    nodes/handlers/llm_nodes.py, but takes its key from settings instead of
    the credential vault.
    """
    api_key = getattr(settings, "NVIDIA_API_KEY", "") or ""
    if not api_key:
        yield {"type": "error", "message": "NVIDIA API key not configured on this server."}
        return

    model = model or getattr(settings, "NVIDIA_GUEST_MODEL", "nvidia/nemotron-3-super-120b-a12b")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{NVIDIA_BASE_URL}/chat/completions",
                headers=headers, json=payload,
            ) as response:
                if response.status_code == 429:
                    yield {"type": "error", "message": "NVIDIA API rate-limited the server. Try again shortly."}
                    return
                if response.status_code >= 500:
                    yield {"type": "error", "message": f"NVIDIA service error ({response.status_code})."}
                    return
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    yield {"type": "error", "message": f"NVIDIA API error {response.status_code}: {body}"}
                    return

                in_thinking = False
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    rc = delta.get("reasoning_content")
                    if rc:
                        yield {"type": "thinking", "content": rc}
                        continue

                    text = delta.get("content") or ""
                    if not text:
                        continue

                    # Inline <think>...</think> handling, same as NvidiaNode.
                    while text:
                        if not in_thinking and "<think>" in text:
                            pre, _, rest = text.partition("<think>")
                            if pre:
                                yield {"type": "content", "content": pre}
                            in_thinking = True
                            text = rest
                            continue
                        if in_thinking and "</think>" in text:
                            think, _, rest = text.partition("</think>")
                            if think:
                                yield {"type": "thinking", "content": think}
                            in_thinking = False
                            text = rest
                            continue
                        if in_thinking:
                            yield {"type": "thinking", "content": text}
                        else:
                            yield {"type": "content", "content": text}
                        break

        yield {"type": "done"}
    except httpx.TimeoutException:
        yield {"type": "error", "message": "NVIDIA request timed out."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Guest NVIDIA stream failed")
        yield {"type": "error", "message": f"NVIDIA request failed: {exc}"}
