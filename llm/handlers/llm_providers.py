"""
The OpenAI-protocol providers, declared rather than implemented.

Each class below used to be 300–650 lines of hand-written HTTP in
`llm_nodes.py`. The protocol now lives once in `openai_compatible.py`; what
remains here is the part that genuinely differs between providers — where the
endpoint is, what the models are called, and the handful of real behavioural
deviations, each of which is an explicit override rather than an accident of
copy-paste.

Ollama is the one supported provider that does not belong here: it posts to
`/api/chat` and streams bare JSON rather than `choices[].delta`, so forcing it
onto this base would mean overriding every method the base defines — which is
the signal that a shared base is the wrong tool. It stays in `llm_nodes.py`.

The supported set itself is `llm.providers.SUPPORTED_PROVIDERS`; models from
providers no longer listed here are reached through OpenRouter.
"""
from __future__ import annotations

import logging

from .openai_compatible import OpenAICompatibleLLMNode

logger = logging.getLogger(__name__)


class OpenAINode(OpenAICompatibleLLMNode):
    """OpenAI GPT models."""

    node_type = "openai"
    name = "OpenAI"
    description = "Generate text using OpenAI GPT models"

    provider_slug = "openai"
    api_label = "OpenAI"
    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    image_endpoint = "https://api.openai.com/v1/images/generations"



class OpenRouterNode(OpenAICompatibleLLMNode):
    """OpenRouter — one key, many upstream models."""

    node_type = "openrouter"
    name = "OpenRouter"
    description = "Access many model providers through a single OpenRouter key"

    provider_slug = "openrouter"
    api_label = "OpenRouter"
    base_url = "https://openrouter.ai/api/v1"
    default_model = "nvidia/nemotron-3-super-120b-a12b:free"
    image_endpoint = "https://openrouter.ai/api/v1/images/generations"
    #: OpenRouter attributes traffic by referer/title for its dashboard and
    #: rate-limit tiers; requests without them are treated as anonymous.
    extra_headers = {
        "HTTP-Referer": "https://aiaas.local",
        "X-Title": "AIAAS Workflow",
    }
    default_temperature = 0.3

    #: Kept as a class attribute because the 404-retry path below and existing
    #: callers both reference it by name.
    FALLBACK_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

    def chat_payload(self, *, model, messages, config, stream):
        from .openai_compatible import num
        payload = super().chat_payload(
            model=model, messages=messages, config=config, stream=stream,
        )
        payload["top_p"] = num(config.get("top_p"), 1.0)
        # Asks OpenRouter to return what it actually charged, plus the cache
        # split, in its usage object. Without it the response carries token
        # counts only and every cost downstream is our own estimate against a
        # price table that goes stale the day a model is repriced upstream —
        # while OpenRouter, the party doing the billing, knew the real number
        # all along. `usage.reported_cost_usd` wins over the estimate in
        # `pricing.cost_for_usage` for exactly this reason.
        payload["usage"] = {"include": True}
        return payload


class NvidiaNode(OpenAICompatibleLLMNode):
    """NVIDIA NIM — optimised open models."""

    node_type = "nvidia"
    name = "NVIDIA NIM"
    description = "Generate text using NVIDIA NIM optimized models"

    provider_slug = "nvidia"
    api_label = "NVIDIA"
    base_url = "https://integrate.api.nvidia.com/v1"
    default_model = "nvidia/nemotron-3-super-120b-a12b"
    #: NIM serves text models only.
    image_endpoint = None

