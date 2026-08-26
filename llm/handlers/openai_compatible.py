"""
One implementation of the OpenAI chat-completions protocol.

Six providers once in `llm_nodes.py` — OpenAI, Perplexity, OpenRouter, HuggingFace,
xAI and Nvidia — speak the same wire format: `POST {base_url}/chat/completions`
with a Bearer token, optionally `POST {base_url}/images/generations` for image
models. Each had its own hand-written copy of that call, roughly 400 lines
apiece, and the copies had drifted:

  - the attachment→base64 block was pasted into seven handlers and every one of
    them logged "Blocked path traversal in Gemini attachment", so a blocked
    traversal in the xAI node named the wrong provider in the logs
  - only some copies validated the attachment path at all
  - `execute` and `stream_execute` disagreed within the same class about
    whether a missing key is an error string or an exception

This module holds that protocol once. A provider subclass declares where its
endpoint is and how it differs; it does not re-implement HTTP.

Deliberate deviation is expected and supported through overrides — Nvidia
injects extra headers, OpenRouter routes image models to a different endpoint,
Perplexity has no image endpoint at all. What a subclass must *not* do is grow
its own `httpx` client; if it needs to, this base is missing something and
should grow instead. (`RestConnectorNode` in `rest_base.py` held the same
contract for the REST connectors, and was deleted with them.)

Gemini and Ollama are intentionally NOT subclasses: they speak different wire
protocols (`/api/chat` and Google's schema respectively). They share the
attachment and credential helpers below but keep their own transport.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from typing import Any, AsyncIterator, TYPE_CHECKING

import httpx

from .base import BaseNodeHandler, NodeExecutionResult
from .llm_base import ChatChunkParser, iter_sse_chunks

if TYPE_CHECKING:
    from llm.context import ExecutionContext

logger = logging.getLogger(__name__)


# ── Shared helpers (also used by the non-OpenAI-shaped handlers) ─────────────

def validate_attachment_path(file_path: str) -> bool:
    """True if `file_path` resolves inside MEDIA_ROOT.

    Guards against traversal via a crafted attachment path. When MEDIA_ROOT is
    unset we fall back to rejecting anything that climbs out of the cwd.
    """
    import os
    try:
        from django.conf import settings
        media_root = getattr(settings, 'MEDIA_ROOT', None)
        if not media_root:
            return '..' not in os.path.relpath(os.path.abspath(file_path))
        return os.path.abspath(file_path).startswith(os.path.abspath(media_root))
    except Exception:
        return False


def image_mime_for(filename: str) -> str:
    """Mime type for an image attachment, defaulting to JPEG.

    Every attachment used to be labelled `image/jpeg` regardless of what it
    actually was. NIM tolerated PNG bytes under a JPEG label (verified), so this
    was latent rather than broken — but a stricter provider would reject it, and
    the vision witness now depends on this encoder for every look it takes.
    """
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed and guessed.startswith("image/"):
        return guessed
    ext = os.path.splitext(filename or "")[1].lower()
    return {
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def encode_image_attachments(
    attachments: list, *, provider: str, prompt: str,
) -> list[dict[str, Any]] | str:
    """Build OpenAI multimodal `content` from image attachments.

    Returns the plain prompt string when there is nothing to attach, so callers
    can pass the result straight through as the message content — the models
    reject a single-element content array in some versions.

    `provider` is only used for logging, but it is required rather than
    defaulted: the seven copies this replaces all logged the same hardcoded
    provider name, which made blocked-traversal warnings point at the wrong
    handler. Making it mandatory means a new subclass cannot inherit someone
    else's identity by omission.
    """
    if not attachments:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for att in attachments:
        if getattr(att, 'file_type', None) != 'image':
            logger.info(
                "Skipping unsupported attachment type %s for %s",
                getattr(att, 'file_type', '?'), provider,
            )
            continue
        try:
            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
            if not validate_attachment_path(file_path):
                logger.warning(
                    "Blocked path traversal in %s attachment: %s",
                    provider, getattr(att, 'filename', '?'),
                )
                continue
            with open(file_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode('utf-8')
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime_for(getattr(att, 'filename', '') or file_path)}"
                           f";base64,{b64}",
                },
            })
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            # One unreadable file must not fail the whole request, but it must
            # not vanish either: a silently dropped attachment looks to the
            # user like the model ignored what they sent.
            logger.warning(
                "Skipping unreadable attachment %s for %s: %s",
                getattr(att, 'filename', '?'), provider, exc,
            )

    return content if len(content) > 1 else prompt


async def resolve_node_api_key(
    config: dict[str, Any], context: 'ExecutionContext',
) -> str | None:
    """The API key for one node execution.

    `api_key_override` wins because it is how `chat.llm` passes a
    platform-managed key for a user with no credential of their own.
    """
    if override := config.get("api_key_override"):
        return override
    credential_id = config.get("credential")
    if not credential_id:
        return None
    creds = await context.get_credential(credential_id)
    if not creds:
        return None
    return creds.get("apiKey") or creds.get("api_key") or creds.get("token")


def extract_think_tags(content: str) -> tuple[str, str | None]:
    """Split `<think>…</think>` reasoning out of a completed response body."""
    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
    if not match:
        return content, None
    thinking = match.group(1).strip()
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return cleaned, thinking


def num(value: Any, fallback: float) -> float:
    """Parse a config number, falling back rather than raising.

    Node config arrives from JSON the user edited in a form, so a blank string
    or a stray unit suffix is routine. Every provider had its own try/except
    around `float()`; a couple of them omitted it and 500'd on empty input.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# ── The protocol ─────────────────────────────────────────────────────────────

class OpenAICompatibleLLMNode(BaseNodeHandler):
    """Base for any provider exposing OpenAI-shaped chat completions.

    Subclasses declare the class attributes in the block below. Everything
    else — credential resolution, attachment encoding, payload assembly, the
    HTTP call, error shape, streaming and response parsing — is inherited.
    """

    # ── per-provider declaration ──
    #: Slug used for model lookup in the AIModel registry.
    provider_slug: str = ""
    #: Human-readable name, used verbatim in error messages and logs.
    api_label: str = ""
    #: Root of the API, no trailing slash. `/chat/completions` is appended.
    base_url: str = ""
    #: Model selected when config omits one.
    default_model: str = ""
    #: Endpoint for image-generation models, or None if unsupported.
    image_endpoint: str | None = None
    #: Sent on every request alongside auth.
    extra_headers: dict[str, str] = {}
    #: Seconds before a request is abandoned.
    timeout: float = 120.0
    #: Sampling temperature when config omits one.
    default_temperature: float = 0.7

    # ── overridable hooks ──

    def auth_headers(self, api_key: str) -> dict[str, str]:
        """Bearer by default. Override for providers that differ."""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

    def is_image_request(self, model: str) -> bool:
        """Whether this model should route to `image_endpoint`."""
        if not self.image_endpoint:
            return False
        from .llm_nodes import is_image_generation_model
        return is_image_generation_model(model)

    def chat_payload(
        self, *, model: str, messages: list[dict], config: dict[str, Any],
        stream: bool,
    ) -> dict[str, Any]:
        """Assemble the chat-completions body. Override to add provider extras."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": num(config.get("temperature"), self.default_temperature),
            "max_tokens": int(num(config.get("max_tokens"), 2048)),
        }
        if stream:
            payload["stream"] = True
        if tools := self._tools_for(config):
            payload["tools"] = tools
        if (fmt := config.get("response_format")) in ("json_object", "json_code"):
            payload["response_format"] = {"type": "json_object"}
        return payload

    def image_payload(self, *, model: str, prompt: str,
                      config: dict[str, Any]) -> dict[str, Any]:
        """Assemble the image-generation body."""
        return {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": config.get("size", "1024x1024"),
        }

    def parse_image_response(self, data: dict[str, Any]) -> tuple[str, str | None]:
        """(content, media_url) from an image-generation response."""
        entry = (data.get("data") or [{}])[0]
        media_url = entry.get("url")
        if not media_url and entry.get("b64_json"):
            media_url = f"data:image/png;base64,{entry['b64_json']}"
        return "Image generated successfully.", media_url

    # ── internals ──

    @staticmethod
    def _tools_for(config: dict[str, Any]) -> list | None:
        tools = config.get("tools")
        if not tools and config.get("enable_tools", False):
            import chat.tools as shared_tools
            tools = shared_tools.AVAILABLE_TOOLS
        return tools or None

    def effective_prompt(self, prompt: str, config: dict[str, Any]) -> str:
        """Append structured-output instructions the user configured.

        `customFieldDefs` drives a real JSON schema; `response_format` is the
        cruder "just answer in JSON" switch. Both shipped per-provider with
        subtly different wording, which meant the same workflow produced
        different shapes depending on which model it was pointed at.
        """
        from .base import build_json_schema_from_fields, format_schema_for_prompt

        if schema := build_json_schema_from_fields(config.get("customFieldDefs", [])):
            return prompt + format_schema_for_prompt(schema)

        fmt = config.get("response_format", "text")
        if fmt == "json_code":
            return prompt + (
                "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' "
                "(your reasoning), 'explanation' (brief summary), and 'code' (just "
                "the Python code string, no markdown fences)."
            )
        if fmt == "json_object":
            return prompt + (
                "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' "
                "(your reasoning) and 'content' (your actual answer)."
            )
        return prompt

    async def _system_message(self, config: dict[str, Any]) -> str:
        """System prompt with this call's configured skills appended."""
        from .llm_nodes import format_skills_as_context, resolve_node_skills

        system = config.get("system_message") or "You are a helpful assistant."
        try:
            if skills := await resolve_node_skills(config):
                system += format_skills_as_context(skills)
        except Exception:
            # Skills are additive context; failing to load them degrades the
            # answer but must not fail the call.
            logger.warning("Skill resolution failed for %s", self.api_label,
                           exc_info=True)
        return system

    async def _messages(
        self, prompt: str, config: dict[str, Any], context: 'ExecutionContext',
    ) -> list[dict]:
        content = encode_image_attachments(
            config.get("attachments", []),
            provider=self.api_label,
            prompt=self.effective_prompt(prompt, config),
        )
        messages: list[dict] = [
            {"role": "system", "content": await self._system_message(config)}
        ]
        messages += list(config.get("history") or [])
        messages.append({"role": "user", "content": content})
        return messages

    def _chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _missing_key_error(self) -> str:
        # Names the provider *and* the fix. "not configured" on its own left the
        # user to work out whether the node, the workflow or the platform was
        # broken, when the answer is always the same one thing: this provider
        # requires a credential and there is none to run it with.
        return (
            f"{self.api_label} requires a credential and none was found. "
            f"Add a {self.api_label} credential in Settings and select it on "
            f"this node."
        )

    # ── execution ──

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        model = config.get("model") or self.default_model
        prompt = config.get("prompt", "")
        if not prompt:
            return NodeExecutionResult(
                success=False, error="Prompt is required",
            )

        api_key = await resolve_node_api_key(config, context)
        if not api_key:
            return NodeExecutionResult(
                success=False, error=self._missing_key_error(),
            )

        is_image = self.is_image_request(model)
        if is_image:
            url = self.image_endpoint
            payload = self.image_payload(model=model, prompt=prompt, config=config)
        else:
            url = self._chat_url()
            payload = self.chat_payload(
                model=model, messages=await self._messages(prompt, config, context),
                config=config, stream=False,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url, headers=self.auth_headers(api_key), json=payload,
                )
                if response.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"{self.api_label} API error: {response.text}",
                        status_code=response.status_code,
                    )
                data = response.json()
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error=f"{self.api_label} API request timed out",
            )
        except Exception as exc:
            logger.exception("%s call failed", self.api_label)
            return NodeExecutionResult(
                success=False, error=f"{self.api_label} error: {exc}",
            )

        media_url = None
        if is_image:
            content, media_url = self.parse_image_response(data)
            result: dict[str, Any] = {}
        else:
            message = (data.get("choices") or [{}])[0].get("message") or {}
            content = message.get("content") or ""
            result = {}
            if tool_calls := message.get("tool_calls"):
                result["tool_calls"] = tool_calls

        if config.get("thinking"):
            content, thinking = extract_think_tags(content)
            if thinking:
                result["thinking"] = thinking

        result.update({
            "content": content,
            "model": model,
            "media_url": media_url,
            "usage": data.get("usage") or {},
        })
        return NodeExecutionResult(
            success=True, data=result,
        )

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> AsyncIterator[dict[str, Any]]:
        model = config.get("model") or self.default_model
        prompt = config.get("prompt", "")
        if not prompt:
            yield {"type": "error", "message": "Prompt is required"}
            return

        api_key = await resolve_node_api_key(config, context)
        if not api_key:
            yield {"type": "error", "message": self._missing_key_error()}
            return

        # Image models have no token stream to emit — run the non-streaming path
        # and surface its single result, rather than leaving the caller hanging.
        if self.is_image_request(model):
            result = await self.execute(input_data, config, context)
            if result.success:
                data = result.data
                yield {"type": "content", "content": data.get("content", "")}
                if data.get("media_url"):
                    yield {"type": "metadata", "media_url": data["media_url"]}
            else:
                yield {"type": "error", "message": result.error}
            return

        payload = self.chat_payload(
            model=model, messages=await self._messages(prompt, config, context),
            config=config, stream=True,
        )
        parser = ChatChunkParser(emit_thinking=bool(config.get("thinking", False)))

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST", self._chat_url(),
                    headers=self.auth_headers(api_key), json=payload,
                ) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")
                        # `status` rides along so the chat layer can tell "out of
                        # credit" from "provider hiccup" without parsing prose.
                        yield {
                            "type": "error",
                            "message": f"{self.api_label} API error: {body}",
                            "status": response.status_code,
                        }
                        return
                    async for chunk in iter_sse_chunks(response):
                        for event in parser.feed(chunk):
                            yield event
            for event in parser.flush():
                yield event
        except httpx.TimeoutException:
            yield {
                "type": "error",
                "message": f"{self.api_label} API request timed out",
            }
        except Exception as exc:
            logger.exception("%s stream failed", self.api_label)
            yield {"type": "error", "message": f"{self.api_label} error: {exc}"}
