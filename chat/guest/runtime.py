"""
Guest-mode helpers for anonymous AI chat.

The guest pipeline is intentionally narrow:
  - One shared Django user (settings.GUEST_USER_EMAIL) owns every guest
    ChatSession so existing FK constraints stay intact.
  - One provider and one model, pinned in code (GUEST_PROVIDER / GUEST_MODEL):
    OpenRouter serving its free-models router. A guest has no picker and no
    credential of their own, so there is nothing for a second model to be
    chosen by — and an env knob that could point the demo somewhere else is a
    way for the pinning to stop being true without anyone noticing.
  - The key is the platform OpenRouter key, resolved through the same
    `credentials.resolution.platform_api_key` funnel every other platform-paid
    call uses, bypassing the per-user credential vault.
  - Streaming uses the same SSE format the frontend already handles for the
    authenticated send_message_stream view.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model

from llm.handlers.llm_base import ChatChunkParser, iter_sse_chunks

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# The whole of guest mode's model policy. `openrouter/free` is OpenRouter's
# free-models router: it picks a currently-available zero-cost model per
# request, so an anonymous demo costs the platform nothing and survives any one
# free model being withdrawn — the failure that a single pinned model name
# turns into a 410 and an apology nobody can act on.
# Pinned rather than read from settings: "guests only get this model" has to be
# a property of the code, not of whichever .env the box happens to have.
GUEST_PROVIDER = "openrouter"
GUEST_MODEL = "openrouter/free"

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


async def stream_guest_chat(
    messages: list[dict],
    *,
    temperature: float = 0.6,
    max_tokens: int = 4096,
) -> AsyncIterator[dict]:
    """
    Stream a chat completion from OpenRouter, always on GUEST_MODEL.

    There is deliberately no `model` argument: a caller able to name a model is
    a caller able to take guest mode off its one pinned model.

    Yields dicts of shape:
      {"type": "content", "content": str}
      {"type": "thinking", "content": str}
      {"type": "error", "message": str}
      {"type": "done"}

    Mirrors the SSE-emitting structure the OpenAI-compatible handlers use, but
    takes its key from the platform environment instead of the credential
    vault — a guest has no vault to take one from.
    """
    from credentials.resolution import platform_api_key

    api_key = platform_api_key(GUEST_PROVIDER)
    if not api_key:
        yield {
            "type": "error",
            "message": "Guest chat is not configured on this server.",
        }
        return

    payload = {
        "model": GUEST_MODEL,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter attributes usage to the calling app by these two headers. They
    # are optional to the API and deliberately best-effort here: a missing
    # PUBLIC_URL must not stop an anonymous visitor getting an answer.
    if referer := getattr(settings, "PUBLIC_URL", "") or "":
        headers["HTTP-Referer"] = referer
    headers["X-Title"] = "AIAAS guest chat"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers, json=payload,
            ) as response:
                if response.status_code == 429:
                    yield {"type": "error", "message": "The demo is rate-limited right now. Try again shortly."}
                    return
                if response.status_code >= 500:
                    yield {"type": "error", "message": f"Upstream service error ({response.status_code})."}
                    return
                if response.status_code != 200:
                    from llm.access import humanize_provider_body

                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    if response.status_code == 410:
                        # The *server's* model reached end of life, not one the
                        # guest chose — they have no picker and nothing to fix.
                        # So they get an apology and the operator gets the body.
                        logger.error(
                            "Guest chat model %s is retired: %s",
                            GUEST_MODEL,
                            humanize_provider_body(body),
                        )
                        yield {
                            "type": "error",
                            "message": (
                                "This demo's model is no longer available. Sign in "
                                "to pick your own model, or try again later."
                            ),
                        }
                        return
                    yield {
                        "type": "error",
                        "message": (
                            f"Upstream API error {response.status_code}: "
                            f"{humanize_provider_body(body)}"
                        ),
                    }
                    return

                # The provider quirks — `data:` framing, reasoning keys, tags
                # torn across chunks — are the shared parser's job. This used to
                # be a private copy of NvidiaNode's loop, which is precisely the
                # duplication that let the two drift apart.
                parser = ChatChunkParser(emit_tool_calls=False)
                async for chunk in iter_sse_chunks(response):
                    for event in parser.feed(chunk):
                        if event["type"] in ("content", "thinking"):
                            yield event
                for event in parser.flush():
                    yield event

        yield {"type": "done"}
    except httpx.TimeoutException:
        yield {"type": "error", "message": "The request timed out."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Guest chat stream failed")
        yield {"type": "error", "message": f"Request failed: {exc}"}
