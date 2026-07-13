import os
import django
from copy import deepcopy

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "workflow_backend.settings")
django.setup()

from django.db import transaction
from nodes.models import AIProvider, AIModel


CAPABILITY_FIELD_MAP = {
    "text_input": "supports_text_input",
    "text_generation": "supports_text_generation",
    "image_input": "supports_image_input",
    "image_generation": "supports_image_generation",
    "audio_input": "supports_audio_input",
    "audio_generation": "supports_audio_generation",
    "video_input": "supports_video_input",
    "video_generation": "supports_video_generation",
    "document_input": "supports_document_input",
    "document_generation": "supports_document_generation",
    "tabular_input": "supports_tabular_input",
    "tabular_generation": "supports_tabular_generation",
    "numeric_input": "supports_numeric_input",
    "numeric_generation": "supports_numeric_generation",
    "time_series_input": "supports_time_series_input",
    "time_series_generation": "supports_time_series_generation",
    "structured_output": "supports_structured_output",
    "tool_calling": "supports_tool_calling",
    "embedding_generation": "supports_embedding_generation",
}

DEFAULT_CAPS = {
    "text_input": True,
    "text_generation": True,
    "image_input": False,
    "image_generation": False,
    "audio_input": False,
    "audio_generation": False,
    "video_input": False,
    "video_generation": False,
    "document_input": False,
    "document_generation": False,
    "tabular_input": False,
    "tabular_generation": False,
    "numeric_input": False,
    "numeric_generation": False,
    "time_series_input": False,
    "time_series_generation": False,
    "structured_output": False,
    "tool_calling": False,
    "embedding_generation": False,
}

CHAT_CAPS = {"structured_output": True, "tool_calling": True}
VISION_CAPS = {**CHAT_CAPS, "image_input": True}
MULTIMODAL_CAPS = {
    **VISION_CAPS,
    "audio_input": True,
    "video_input": True,
    "document_input": True,
}
REASONING_CAPS = {**CHAT_CAPS, "numeric_input": True, "numeric_generation": True}


def m(name, value, is_free=False, caps=None):
    return {
        "name": name,
        "value": value,
        "is_free": is_free,
        "caps": caps or {},
    }


def build_model_defaults(item):
    caps = {**DEFAULT_CAPS, **item.get("caps", {})}

    defaults = {
        "provider": item["provider"],
        "name": item["name"],
        "is_free": item["is_free"],
    }

    for cap_key, field_name in CAPABILITY_FIELD_MAP.items():
        defaults[field_name] = caps[cap_key]

    return defaults


def populate():
    # Catalog reviewed on 2026-05-10. Rule: keep the latest per tier; legacy only if
    # widely deployed. Same-provider same-tier: newer wins, older removed.
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI gateway for routing across hosted model providers.",
            "icon": "OR",
            "models": [
                # --- OpenRouter routing ---
                m("Auto Router", "openrouter/auto", caps=CHAT_CAPS),
                m("Free Models Router", "openrouter/free", True, CHAT_CAPS),
                m("Pareto Code Router", "openrouter/pareto-code", caps=CHAT_CAPS),
                m("Kilo Auto Frontier", "kilo-auto/frontier", caps=CHAT_CAPS),
                m("Kilo Auto Balanced", "kilo-auto/balanced", caps=CHAT_CAPS),
                m("Kilo Auto Free", "kilo-auto/free", True, CHAT_CAPS),
                # --- OpenAI via OpenRouter ---
                m("OpenAI GPT-5.5", "openai/gpt-5.5", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.4 Mini", "openai/gpt-5.4-mini", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-4o", "openai/gpt-4o", caps=VISION_CAPS),
                m("OpenAI GPT-4o Mini", "openai/gpt-4o-mini", caps=VISION_CAPS),
                # --- Anthropic via OpenRouter (one model per tier) ---
                m("Anthropic Claude Opus 4.7", "anthropic/claude-opus-4.7", caps=VISION_CAPS),
                m("Anthropic Claude Sonnet 4.6", "anthropic/claude-sonnet-4.6", caps=VISION_CAPS),
                m("Anthropic Claude Haiku 4.5", "anthropic/claude-haiku-4.5", caps=VISION_CAPS),
                # --- Google via OpenRouter ---
                m("Google Gemini 3.1 Pro Preview", "google/gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Google Gemini 2.5 Flash", "google/gemini-2.5-flash", True, MULTIMODAL_CAPS),
                # --- DeepSeek via OpenRouter ---
                m("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", caps=REASONING_CAPS),
                m("DeepSeek R1", "deepseek/deepseek-r1", caps=REASONING_CAPS),
                m("DeepSeek V3 (Legacy)", "deepseek/deepseek-chat", caps=CHAT_CAPS),
                # --- xAI via OpenRouter ---
                m("xAI Grok 4.20", "x-ai/grok-4.20", caps={**VISION_CAPS, "document_input": True}),
                # --- Meta via OpenRouter ---
                m("Meta Llama 4 Maverick", "meta-llama/llama-4-maverick", caps=VISION_CAPS),
                m("Meta Llama 4 Scout", "meta-llama/llama-4-scout", caps=VISION_CAPS),
                # --- Qwen via OpenRouter (3.6 Plus flagship; keep 32B as mid-size) ---
                m("Qwen3.6 Plus", "qwen/qwen3.6-plus", caps={**VISION_CAPS, "video_input": True}),
                m("Qwen3 32B", "qwen/qwen3-32b", caps=CHAT_CAPS),
                m("Qwen3 Coder Next Free", "qwen/qwen3-coder-next:free", True, CHAT_CAPS),
                # --- Mistral via OpenRouter ---
                m("Mistral Small 4", "mistralai/mistral-small-2603", caps=VISION_CAPS),
                # --- Google Open via OpenRouter (Gemma 4 supersedes all Gemma 3) ---
                m("Google Gemma 4 31B Free", "google/gemma-4-31b-it:free", True, CHAT_CAPS),
                # --- NVIDIA via OpenRouter ---
                m("NVIDIA Nemotron 3 Super 120B Free", "nvidia/nemotron-3-super-120b-a12b:free", True, CHAT_CAPS),
                # --- Notable independents ---
                m("Moonshot Kimi K2.6", "moonshotai/kimi-k2.6", caps=VISION_CAPS),
                m("Inception Mercury 2", "inception/mercury-2", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "Ollama (Local)",
            "slug": "ollama",
            "description": "Run private local AI models on your own hardware.",
            "icon": "OL",
            "models": [
                # DeepSeek R1 in three sizes for different hardware tiers
                m("DeepSeek R1 1.5B", "deepseek-r1:1.5b", True, REASONING_CAPS),
                m("DeepSeek R1 8B", "deepseek-r1:8b", True, REASONING_CAPS),
                m("DeepSeek R1 32B", "deepseek-r1:32b", True, REASONING_CAPS),
                m("DeepSeek V3", "deepseek-v3:latest", True, CHAT_CAPS),
                # Llama 4 Scout only — Maverick requires 200GB+ RAM, impractical locally
                m("Llama 4 Scout", "llama4:scout", True, VISION_CAPS),
                # Qwen 3.6 flagship; 7B for low-RAM machines; Coder 32B best-in-class for code
                m("Qwen 3.6", "qwen3.6:latest", True, VISION_CAPS),
                m("Qwen 3 8B", "qwen3:8b", True, CHAT_CAPS),
                m("Qwen 2.5 Coder 32B", "qwen2.5-coder:32b", True, CHAT_CAPS),
                # Gemma 4 supersedes Gemma 3; 4B covers ultra-low-RAM use
                m("Gemma 4", "gemma4:latest", True, VISION_CAPS),
                m("Gemma 4 4B", "gemma4:4b", True, VISION_CAPS),
                m("Phi 4", "phi4:latest", True, REASONING_CAPS),
                # Mistral 7B — lean, fast, excellent for constrained hardware
                m("Mistral 7B", "mistral:7b", True, CHAT_CAPS),
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to the OpenAI API.",
            "icon": "OA",
            "models": [
                # Flagship + cheaper fast variant in the GPT-5 generation
                m("GPT-5.5", "gpt-5.5", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.5 Pro", "gpt-5.5-pro", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.4 Mini", "gpt-5.4-mini", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.4 Nano", "gpt-5.4-nano", caps={**VISION_CAPS, "document_input": True}),
                # Famous legacy — massively deployed
                m("GPT-4o", "gpt-4o", caps=VISION_CAPS),
                m("GPT-4o Mini", "gpt-4o-mini", caps=VISION_CAPS),
                # Reasoning — o3 flagship; o4-mini fast + cheap; o1 famous legacy
                m("o3", "o3", caps=REASONING_CAPS),
                m("o4-mini", "o4-mini", caps=REASONING_CAPS),
                m("o1 (Legacy)", "o1", caps=REASONING_CAPS),
                # Specialised modalities — latest only
                m("GPT Image 2", "gpt-image-2", caps={"image_input": True, "image_generation": True}),
                m("Sora 2 Pro", "sora-2-pro", caps={"video_generation": True}),
                m("GPT Realtime 1.5", "gpt-realtime-1.5", caps={"audio_input": True, "audio_generation": True, **CHAT_CAPS}),
                m("Text Embedding 3 Large", "text-embedding-3-large", caps={"embedding_generation": True}),
            ],
        },
        {
            "name": "Google Gemini",
            "slug": "gemini",
            "description": "Google Gemini API models with multimodal input and long context.",
            "icon": "GG",
            "models": [
                # Latest flagship — 3.1 Pro supersedes 3 Pro
                m("Gemini 3.1 Pro Preview", "gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Gemini 3.1 Flash Preview", "gemini-3.1-flash-preview", True, MULTIMODAL_CAPS),
                # Famous/widely-used — 2.5 Pro and Flash are the production workhorses
                m("Gemini 2.5 Pro", "gemini-2.5-pro", caps=MULTIMODAL_CAPS),
                m("Gemini 2.5 Flash", "gemini-2.5-flash", True, MULTIMODAL_CAPS),
                # Flash Lite — faster and cheaper than Flash for high-volume tasks
                m("Gemini 2.5 Flash Lite", "gemini-2.5-flash-lite", True, MULTIMODAL_CAPS),
                # Legacy kept — 2.0 Flash is ubiquitous in existing integrations
                m("Gemini 2.0 Flash (Legacy)", "gemini-2.0-flash", True, MULTIMODAL_CAPS),
                # Embeddings — 2 supersedes 001
                m("Gemini Embedding 2 Preview", "gemini-embedding-2-preview", caps={"embedding_generation": True}),
                # Image gen — 3.1 Flash Image supersedes 2.5 Flash Image; Imagen 4 Ultra supersedes Imagen 4
                m("Gemini 3.1 Flash Image Preview", "gemini-3.1-flash-image-preview", caps={"image_input": True, "image_generation": True}),
                m("Imagen 4 Ultra", "imagen-4.0-ultra-generate-001", caps={"image_generation": True}),
                # Video gen — Veo 3.1 supersedes Veo 3.1 Fast and Veo 3
                m("Veo 3.1", "veo-3.1-generate-preview", caps={"image_input": True, "video_generation": True}),
            ],
        },
        {
            "name": "Anthropic",
            "slug": "anthropic",
            "description": "Claude models by Anthropic for coding, writing, and long-running agents.",
            "icon": "AN",
            "models": [
                # Latest per tier (Opus 4.7 supersedes 4.6/4.6 Fast/Opus 4; Sonnet 4.6 supersedes 4.5/4)
                m("Claude Opus 4.7", "claude-opus-4-7", caps=VISION_CAPS),
                m("Claude Sonnet 4.6", "claude-sonnet-4-6", caps=VISION_CAPS),
                m("Claude Haiku 4.5", "claude-haiku-4-5", caps=VISION_CAPS),
                # Famous legacy — 3.7 is widely used for extended thinking; 3.5 Sonnet is iconic
                m("Claude 3.7 Sonnet (Legacy)", "claude-3-7-sonnet-20250219", caps=VISION_CAPS),
                m("Claude 3.5 Sonnet (Legacy)", "claude-3-5-sonnet-20241022", caps=VISION_CAPS),
            ],
        },
        {
            "name": "Perplexity",
            "slug": "perplexity",
            "description": "Search-augmented Sonar models with live web citations.",
            "icon": "PX",
            "models": [
                # Each model serves a genuinely different use case — all kept
                m("Sonar", "sonar", caps=CHAT_CAPS),
                m("Sonar Pro", "sonar-pro", caps=CHAT_CAPS),
                m("Sonar Reasoning", "sonar-reasoning", caps=REASONING_CAPS),
                m("Sonar Reasoning Pro", "sonar-reasoning-pro", caps=REASONING_CAPS),
                m("Sonar Deep Research", "sonar-deep-research", caps=REASONING_CAPS),
            ],
        },
        {
            "name": "DeepSeek",
            "slug": "deepseek",
            "description": "Official DeepSeek API for general chat, coding, and reasoning.",
            "icon": "DS",
            "models": [
                # V4 Pro supersedes V4 Flash (same tier, more capable)
                m("DeepSeek Chat V4 Pro", "deepseek-v4-pro", caps=REASONING_CAPS),
                # V3.1 kept as the production-stable chat model
                m("DeepSeek Chat V3.1", "deepseek-chat", caps=CHAT_CAPS),
                # R1 — iconic open reasoning model
                m("DeepSeek Reasoner R1", "deepseek-reasoner", caps=REASONING_CAPS),
            ],
        },
        {
            "name": "NVIDIA NIM",
            "slug": "nvidia",
            "description": "NVIDIA NIM API — optimized inference for NVIDIA and open-source models.",
            "icon": "NV",
            "models": [
                # NVIDIA flagship reasoning models (names/values corrected)
                m("Nemotron Ultra 253B", "nvidia/llama-3.1-nemotron-ultra-253b-v1", caps=REASONING_CAPS),
                m("Nemotron Super 49B", "nvidia/llama-3.3-nemotron-super-49b-v1", caps=CHAT_CAPS),
                # Top open-source models hosted on NIM
                m("Llama 3.1 405B Instruct", "meta/llama-3.1-405b-instruct", caps=CHAT_CAPS),
                m("Llama 3.3 70B Instruct", "meta/llama-3.3-70b-instruct", caps=CHAT_CAPS),
                m("DeepSeek R1 671B", "deepseek-ai/deepseek-r1", caps=REASONING_CAPS),
                m("Qwen3 235B A22B", "qwen/qwen3-235b-a22b", caps=CHAT_CAPS),
                # Mistral Large 2 supersedes Mixtral 8x22B
                m("Mistral Large 2", "mistralai/mistral-large-2-instruct", caps=CHAT_CAPS),
                # Efficient edge model
                m("Phi-4 Mini Instruct", "microsoft/phi-4-mini-instruct", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "xAI",
            "slug": "xai",
            "description": "Grok models from xAI.",
            "icon": "XA",
            "models": [
                # Grok 4.20 supersedes Grok 4; Multi-Agent is a distinct product
                m("Grok 4.20", "grok-4.20", caps={**VISION_CAPS, "document_input": True}),
                m("Grok 4.20 Multi-Agent", "grok-4.20-multi-agent", caps={**VISION_CAPS, "document_input": True}),
                m("Grok Code Fast 1", "grok-code-fast-1", caps=CHAT_CAPS),
                # Famous legacy
                m("Grok 3 (Legacy)", "grok-3", caps=CHAT_CAPS),
                m("Grok 3 Mini (Legacy)", "grok-3-mini", caps=REASONING_CAPS),
            ],
        },
    ]

    providers = deepcopy(providers)

    print("\n" + "=" * 80)
    print("Synchronizing AI Models Database...")
    print("=" * 80 + "\n")

    synced_model_values = []

    with transaction.atomic():
        for provider_data in providers:
            model_data = provider_data.pop("models")

            provider, _ = AIProvider.objects.update_or_create(
                slug=provider_data["slug"],
                defaults=provider_data,
            )

            print(f"Provider: {provider.name}")

            for item in model_data:
                item["provider"] = provider
                defaults = build_model_defaults(item)

                AIModel.objects.update_or_create(
                    value=item["value"],
                    defaults=defaults,
                )

                synced_model_values.append(item["value"])
                badge = "free" if item["is_free"] else "paid"
                print(f"   - [{badge}] {item['name']}")

            print("   Done.\n")

        # Keep manually-added and older rows. This seed script now only upserts.
        AIModel.objects.filter(value__in=synced_model_values).update(is_active=True)

    print("=" * 80)
    print("Successfully synchronized seeded models.")
    print("=" * 80)


if __name__ == "__main__":
    populate()
