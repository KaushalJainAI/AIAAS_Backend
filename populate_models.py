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


# Ids that 404 against their provider's live /models endpoint, checked 2026-07-29.
# Deactivated rather than deleted — see the note where this is used.
RETIRED_MODEL_VALUES = [
    # OpenRouter — the whole kilo-auto namespace is gone
    "kilo-auto/frontier",
    "kilo-auto/balanced",
    "kilo-auto/free",
    "qwen/qwen3-coder-next:free",
    # NVIDIA NIM — delisted; NIM hosts no qwen/* at all now
    "meta/llama-3.1-405b-instruct",
    "deepseek-ai/deepseek-r1",
    "qwen/qwen3-235b-a22b",
    "microsoft/phi-4-mini-instruct",
]


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
    # Catalogue reviewed 2026-07-29. Rule: keep the latest per tier; legacy only
    # if still widely deployed. Same provider, same tier: newer wins, older goes.
    #
    # OpenRouter and NVIDIA NIM entries below were checked against those APIs'
    # live /models endpoints on that date — every id returned 200. The direct
    # vendor blocks (OpenAI, Anthropic, Gemini, DeepSeek, xAI, Perplexity,
    # Ollama) could not be probed from here, so their ids follow each vendor's
    # naming convention for models OpenRouter proves exist. Treat those as
    # unverified: a wrong id fails closed with a 404 at call time.
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI gateway for routing across hosted model providers.",
            "icon": "OR",
            "models": [
                # --- Routing ---
                m("Auto Router", "openrouter/auto", caps=CHAT_CAPS),
                m("Free Models Router", "openrouter/free", True, CHAT_CAPS),
                m("Pareto Code Router", "openrouter/pareto-code", caps=CHAT_CAPS),
                # kilo-auto/{frontier,balanced,free} removed — all three 404.
                # --- OpenAI via OpenRouter (GPT-5.6 tiers: Sol > Terra > Luna) ---
                m("OpenAI GPT-5.6 Sol", "openai/gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.6 Terra", "openai/gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.6 Luna", "openai/gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}),
                # Famous legacy — still the default in a lot of integrations
                m("OpenAI GPT-4o Mini", "openai/gpt-4o-mini", caps=VISION_CAPS),
                # --- Anthropic via OpenRouter ---
                m("Anthropic Claude Opus 5", "anthropic/claude-opus-5", caps=VISION_CAPS),
                m("Anthropic Claude Sonnet 5", "anthropic/claude-sonnet-5", caps=VISION_CAPS),
                m("Anthropic Claude Fable 5", "anthropic/claude-fable-5", caps=VISION_CAPS),
                m("Anthropic Claude Haiku 4.5", "anthropic/claude-haiku-4.5", caps=VISION_CAPS),
                # --- Google via OpenRouter ---
                m("Google Gemini 3.1 Pro Preview", "google/gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Google Gemini 3.6 Flash", "google/gemini-3.6-flash", caps=MULTIMODAL_CAPS),
                m("Google Gemini 3.5 Flash Lite", "google/gemini-3.5-flash-lite", caps=MULTIMODAL_CAPS),
                # --- DeepSeek via OpenRouter ---
                m("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", caps=REASONING_CAPS),
                m("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", caps=REASONING_CAPS),
                m("DeepSeek R1 (Legacy)", "deepseek/deepseek-r1", caps=REASONING_CAPS),
                # --- xAI via OpenRouter ---
                m("xAI Grok 4.5", "x-ai/grok-4.5", caps={**VISION_CAPS, "document_input": True}),
                m("xAI Grok 4.20 Multi-Agent", "x-ai/grok-4.20-multi-agent", caps={**VISION_CAPS, "document_input": True}),
                # --- Meta via OpenRouter ---
                m("Meta Llama 4 Maverick", "meta-llama/llama-4-maverick", caps=VISION_CAPS),
                m("Meta Llama 4 Scout", "meta-llama/llama-4-scout", caps=VISION_CAPS),
                # --- Qwen via OpenRouter ---
                m("Qwen3.7 Flash", "qwen/qwen3.7-flash", caps={**VISION_CAPS, "video_input": True}),
                m("Qwen3.6 Plus", "qwen/qwen3.6-plus", caps={**VISION_CAPS, "video_input": True}),
                m("Qwen3 32B", "qwen/qwen3-32b", caps=CHAT_CAPS),
                # qwen/qwen3-coder-next:free removed — 404.
                # --- Mistral via OpenRouter ---
                m("Mistral Small 4", "mistralai/mistral-small-2603", caps=VISION_CAPS),
                # --- Google Open via OpenRouter ---
                m("Google Gemma 4 31B Free", "google/gemma-4-31b-it:free", True, CHAT_CAPS),
                # --- NVIDIA via OpenRouter (the :free suffix is OpenRouter-only) ---
                m("NVIDIA Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS),
                m("NVIDIA Nemotron 3 Super 120B Free", "nvidia/nemotron-3-super-120b-a12b:free", True, CHAT_CAPS),
                # --- Notable independents ---
                m("Moonshot Kimi K3", "moonshotai/kimi-k3", caps=VISION_CAPS),
                m("Inception Mercury 2", "inception/mercury-2", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "NVIDIA NIM",
            "slug": "nvidia",
            "description": "NVIDIA NIM API — optimized inference for NVIDIA and open-source models.",
            "icon": "NV",
            "models": [
                # Nemotron 3 line. Note these ids carry no :free suffix — that
                # form exists only on OpenRouter and 404s against NIM.
                m("Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS),
                m("Nemotron 3 Super 120B", "nvidia/nemotron-3-super-120b-a12b", caps=CHAT_CAPS),
                m("Nemotron 3 Nano 30B", "nvidia/nemotron-3-nano-30b-a3b", caps=CHAT_CAPS),
                m("Nemotron Nano 12B VL", "nvidia/nemotron-nano-12b-v2-vl", caps=VISION_CAPS),
                # Point release supersedes the plain v1
                m("Nemotron Super 49B v1.5", "nvidia/llama-3.3-nemotron-super-49b-v1.5", caps=CHAT_CAPS),
                m("Nemotron Ultra 253B", "nvidia/llama-3.1-nemotron-ultra-253b-v1", caps=REASONING_CAPS),
                # Open-weight models hosted on NIM
                m("DeepSeek V4 Pro", "deepseek-ai/deepseek-v4-pro", caps=REASONING_CAPS),
                m("DeepSeek V4 Flash", "deepseek-ai/deepseek-v4-flash", caps=REASONING_CAPS),
                m("Llama 3.3 70B Instruct", "meta/llama-3.3-70b-instruct", caps=CHAT_CAPS),
                m("Moonshot Kimi K2.6", "moonshotai/kimi-k2.6", caps=VISION_CAPS),
                m("GPT-OSS 120B", "openai/gpt-oss-120b", caps=CHAT_CAPS),
                m("GPT-OSS 20B", "openai/gpt-oss-20b", caps=CHAT_CAPS),
                m("Mistral Medium 3.5", "mistralai/mistral-medium-3.5-128b", caps=CHAT_CAPS),
                m("Gemma 4 31B", "google/gemma-4-31b-it", caps=VISION_CAPS),
                # Embeddings — this is the model the RAG pipeline actually calls
                m("NV EmbedQA E5 v5", "nvidia/nv-embedqa-e5-v5", caps={"embedding_generation": True}),
                # Removed, all 404 on NIM: meta/llama-3.1-405b-instruct,
                # deepseek-ai/deepseek-r1, qwen/qwen3-235b-a22b,
                # microsoft/phi-4-mini-instruct. NIM hosts no qwen/* at all.
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to the OpenAI API.",
            "icon": "OA",
            "models": [
                m("GPT-5.6 Sol", "gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.6 Sol Pro", "gpt-5.6-sol-pro", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.6 Terra", "gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.6 Luna", "gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}),
                # Famous legacy — massively deployed
                m("GPT-4o", "gpt-4o", caps=VISION_CAPS),
                m("GPT-4o Mini", "gpt-4o-mini", caps=VISION_CAPS),
                # Reasoning
                m("o3", "o3", caps=REASONING_CAPS),
                m("o4-mini", "o4-mini", caps=REASONING_CAPS),
                # Specialised modalities — latest only
                m("GPT Image 2", "gpt-image-2", caps={"image_input": True, "image_generation": True}),
                m("Sora 2 Pro", "sora-2-pro", caps={"video_generation": True}),
                m("GPT Realtime 1.5", "gpt-realtime-1.5", caps={"audio_input": True, "audio_generation": True, **CHAT_CAPS}),
                m("Text Embedding 3 Large", "text-embedding-3-large", caps={"embedding_generation": True}),
            ],
        },
        {
            "name": "Anthropic",
            "slug": "anthropic",
            "description": "Claude models by Anthropic for coding, writing, and long-running agents.",
            "icon": "AN",
            "models": [
                # Opus 5 and Sonnet 5 supersede Opus 4.8/4.7 and Sonnet 4.6.
                # Fable 5 is a separate Mythos-class line, not a replacement.
                m("Claude Opus 5", "claude-opus-5", caps=VISION_CAPS),
                m("Claude Sonnet 5", "claude-sonnet-5", caps=VISION_CAPS),
                m("Claude Fable 5", "claude-fable-5", caps=VISION_CAPS),
                m("Claude Haiku 4.5", "claude-haiku-4-5", caps=VISION_CAPS),
                # Famous legacy — 3.7 is still widely used for extended thinking
                m("Claude 3.7 Sonnet (Legacy)", "claude-3-7-sonnet-20250219", caps=VISION_CAPS),
            ],
        },
        {
            "name": "Google Gemini",
            "slug": "gemini",
            "description": "Google Gemini API models with multimodal input and long context.",
            "icon": "GG",
            "models": [
                m("Gemini 3.1 Pro Preview", "gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Gemini 3.6 Flash", "gemini-3.6-flash", caps=MULTIMODAL_CAPS),
                m("Gemini 3.5 Flash Lite", "gemini-3.5-flash-lite", caps=MULTIMODAL_CAPS),
                # Production workhorses, still ubiquitous
                m("Gemini 2.5 Pro", "gemini-2.5-pro", caps=MULTIMODAL_CAPS),
                m("Gemini 2.5 Flash", "gemini-2.5-flash", caps=MULTIMODAL_CAPS),
                # Embeddings
                m("Gemini Embedding 2 Preview", "gemini-embedding-2-preview", caps={"embedding_generation": True}),
                # Image and video generation
                m("Gemini 3.1 Flash Image", "gemini-3.1-flash-image", caps={"image_input": True, "image_generation": True}),
                m("Imagen 4 Ultra", "imagen-4.0-ultra-generate-001", caps={"image_generation": True}),
                m("Veo 3.1", "veo-3.1-generate-preview", caps={"image_input": True, "video_generation": True}),
            ],
        },
        {
            "name": "Perplexity",
            "slug": "perplexity",
            "description": "Search-augmented Sonar models with live web citations.",
            "icon": "PX",
            "models": [
                # Each serves a genuinely different use case — all kept
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
                m("DeepSeek Chat V4 Pro", "deepseek-v4-pro", caps=REASONING_CAPS),
                m("DeepSeek Chat V4 Flash", "deepseek-v4-flash", caps=REASONING_CAPS),
                # Stable aliases the API keeps pointing at the current release
                m("DeepSeek Chat", "deepseek-chat", caps=CHAT_CAPS),
                m("DeepSeek Reasoner", "deepseek-reasoner", caps=REASONING_CAPS),
            ],
        },
        {
            "name": "xAI",
            "slug": "xai",
            "description": "Grok models from xAI.",
            "icon": "XA",
            "models": [
                m("Grok 4.5", "grok-4.5", caps={**VISION_CAPS, "document_input": True}),
                m("Grok 4.20 Multi-Agent", "grok-4.20-multi-agent", caps={**VISION_CAPS, "document_input": True}),
                m("Grok Code Fast 1", "grok-code-fast-1", caps=CHAT_CAPS),
                # Famous legacy
                m("Grok 3 (Legacy)", "grok-3", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "Ollama (Local)",
            "slug": "ollama",
            "description": "Run private local AI models on your own hardware.",
            "icon": "OL",
            "models": [
                # Tags are whatever the user has pulled locally, so these are
                # suggestions rather than a catalogue we can verify.
                m("DeepSeek R1 1.5B", "deepseek-r1:1.5b", True, REASONING_CAPS),
                m("DeepSeek R1 8B", "deepseek-r1:8b", True, REASONING_CAPS),
                m("DeepSeek R1 32B", "deepseek-r1:32b", True, REASONING_CAPS),
                # Llama 4 Scout only — Maverick needs 200GB+ RAM, impractical locally
                m("Llama 4 Scout", "llama4:scout", True, VISION_CAPS),
                m("Qwen 3.6", "qwen3.6:latest", True, VISION_CAPS),
                m("Qwen 3 8B", "qwen3:8b", True, CHAT_CAPS),
                m("Qwen 2.5 Coder 32B", "qwen2.5-coder:32b", True, CHAT_CAPS),
                m("Gemma 4", "gemma4:latest", True, VISION_CAPS),
                m("Gemma 4 4B", "gemma4:4b", True, VISION_CAPS),
                m("Phi 4", "phi4:latest", True, REASONING_CAPS),
                m("Mistral 7B", "mistral:7b", True, CHAT_CAPS),
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

        # Keep manually-added and older rows. This seed script only upserts.
        AIModel.objects.filter(value__in=synced_model_values).update(is_active=True)

        # ...except ids confirmed dead against the providers' live /models
        # endpoints. Retiring only this explicit list, rather than everything
        # absent from the catalogue above, so hand-added rows survive. They are
        # deactivated rather than deleted: a saved workflow may still reference
        # one, and a disabled row explains itself better than a missing one.
        retired = AIModel.objects.filter(value__in=RETIRED_MODEL_VALUES, is_active=True)
        if retired.exists():
            print("Retiring models that no longer exist upstream:")
            for value in sorted(retired.values_list("value", flat=True)):
                print(f"   - {value}")
            retired.update(is_active=False)
            print()

    print("=" * 80)
    print("Successfully synchronized seeded models.")
    print("=" * 80)


if __name__ == "__main__":
    populate()
