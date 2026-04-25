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
    # Catalog reviewed on 2026-04-25 for models launched through 2026-04-24.
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI gateway for routing across hosted model providers.",
            "icon": "OR",
            "models": [
                m("Auto Router", "openrouter/auto", caps=CHAT_CAPS),
                m("Free Models Router", "openrouter/free", True, CHAT_CAPS),
                m("Pareto Code Router", "openrouter/pareto-code", caps=CHAT_CAPS),
                m("Kilo Auto Frontier", "kilo-auto/frontier", caps=CHAT_CAPS),
                m("Kilo Auto Balanced", "kilo-auto/balanced", caps=CHAT_CAPS),
                m("Kilo Auto Free", "kilo-auto/free", True, CHAT_CAPS),

                m("OpenAI GPT-5.5", "openai/gpt-5.5", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.5 Pro", "openai/gpt-5.5-pro", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.4", "openai/gpt-5.4", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.4 Mini", "openai/gpt-5.4-mini", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.4 Nano", "openai/gpt-5.4-nano", caps={**VISION_CAPS, "document_input": True}),
                m("OpenAI GPT-5.4 Image 2", "openai/gpt-5.4-image-2", caps={"image_input": True, "image_generation": True}),
                m("OpenAI GPT-5.3 Codex", "openai/gpt-5.3-codex", caps=CHAT_CAPS),
                m("OpenAI GPT-4o", "openai/gpt-4o", caps=VISION_CAPS),
                m("OpenAI GPT-4o Mini", "openai/gpt-4o-mini", caps=VISION_CAPS),
                m("OpenAI Sora 2 Pro", "openai/sora-2-pro", caps={"video_generation": True}),

                m("Anthropic Claude Opus 4.7", "anthropic/claude-opus-4.7", caps=VISION_CAPS),
                m("Anthropic Claude Opus 4.6", "anthropic/claude-opus-4.6", caps=VISION_CAPS),
                m("Anthropic Claude Opus 4.6 Fast", "anthropic/claude-opus-4.6-fast", caps=VISION_CAPS),
                m("Anthropic Claude Sonnet 4.6", "anthropic/claude-sonnet-4.6", caps=VISION_CAPS),
                m("Anthropic Claude Haiku 4.5", "anthropic/claude-haiku-4.5", caps=VISION_CAPS),

                m("Google Gemini 3.1 Pro Preview", "google/gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Google Gemini 3.1 Flash Preview", "google/gemini-3.1-flash-preview", caps=MULTIMODAL_CAPS),
                m("Google Gemini 3.1 Flash Lite Preview", "google/gemini-3.1-flash-lite-preview", caps=MULTIMODAL_CAPS),
                m("Google Gemini 2.5 Pro", "google/gemini-2.5-pro", caps=MULTIMODAL_CAPS),
                m("Google Gemini 2.5 Flash", "google/gemini-2.5-flash", True, MULTIMODAL_CAPS),
                m("Google Gemini 2.5 Flash Lite", "google/gemini-2.5-flash-lite", caps=MULTIMODAL_CAPS),
                m("Google Gemini Embedding 2 Preview", "google/gemini-embedding-2-preview", caps={"embedding_generation": True}),
                m("Google Veo 3.1", "google/veo-3.1", caps={"image_input": True, "video_generation": True}),
                m("Google Veo 3.1 Fast", "google/veo-3.1-fast", caps={"image_input": True, "video_generation": True}),
                m("Google Veo 3.1 Lite", "google/veo-3.1-lite", caps={"image_input": True, "video_generation": True}),

                m("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", caps=REASONING_CAPS),
                m("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", caps=CHAT_CAPS),
                m("DeepSeek Chat V3.1", "deepseek/deepseek-chat-v3.1", caps=CHAT_CAPS),
                m("DeepSeek R1", "deepseek/deepseek-r1", caps=REASONING_CAPS),
                m("DeepSeek V3", "deepseek/deepseek-chat", caps=CHAT_CAPS),

                m("xAI Grok 4.20", "x-ai/grok-4.20", caps={**VISION_CAPS, "document_input": True}),
                m("xAI Grok 4.20 Multi-Agent", "x-ai/grok-4.20-multi-agent", caps={**VISION_CAPS, "document_input": True}),
                m("xAI Grok Code Fast 1", "x-ai/grok-code-fast-1", caps=CHAT_CAPS),

                m("Qwen3.6 Plus", "qwen/qwen3.6-plus", caps={**VISION_CAPS, "video_input": True}),
                m("Qwen3.5 27B", "qwen/qwen3.5-27b", caps=CHAT_CAPS),
                m("Qwen3.5 9B", "qwen/qwen3.5-9b", caps=CHAT_CAPS),
                m("Qwen3 32B", "qwen/qwen3-32b", caps=CHAT_CAPS),
                m("Qwen3 14B", "qwen/qwen3-14b", caps=CHAT_CAPS),
                m("Qwen3 8B", "qwen/qwen3-8b", caps=CHAT_CAPS),
                m("Qwen3 Coder Next Free", "qwen/qwen3-coder-next:free", True, CHAT_CAPS),
                m("Qwen3 Coder 30B A3B", "qwen/qwen3-coder-30b-a3b-instruct", caps=CHAT_CAPS),
                m("Qwen3 VL 8B Instruct", "qwen/qwen3-vl-8b-instruct", caps=VISION_CAPS),
                m("Qwen3 VL 8B Thinking", "qwen/qwen3-vl-8b-thinking", caps=VISION_CAPS),
                m("Moonshot Kimi K2.6", "moonshotai/kimi-k2.6", caps=VISION_CAPS),
                m("MiniMax M2.7", "minimax/minimax-m2.7", caps=CHAT_CAPS),
                m("MiniMax M2.5 Free", "minimax/minimax-m2.5:free", True, CHAT_CAPS),
                m("Xiaomi MiMo V2.5 Pro", "xiaomi/mimo-v2.5-pro", caps=CHAT_CAPS),
                m("Xiaomi MiMo V2.5", "xiaomi/mimo-v2.5", caps=CHAT_CAPS),
                m("Xiaomi MiMo V2 Pro", "xiaomi/mimo-v2-pro", caps=CHAT_CAPS),
                m("Xiaomi MiMo V2 Pro Free", "xiaomi/mimo-v2-pro:free", True, CHAT_CAPS),
                m("Xiaomi MiMo V2 Omni", "xiaomi/mimo-v2-omni", caps=MULTIMODAL_CAPS),
                m("Z.ai GLM 5.1", "z-ai/glm-5.1", caps=CHAT_CAPS),
                m("Z.ai GLM 5", "z-ai/glm-5", caps=CHAT_CAPS),
                m("Z.ai GLM 4.7 Flash", "z-ai/glm-4.7-flash", caps=CHAT_CAPS),
                m("Mistral Small 4", "mistralai/mistral-small-2603", caps=VISION_CAPS),
                m("Mistral Devstral 2", "mistralai/devstral-2512", caps=CHAT_CAPS),
                m("Mistral Ministral 3 14B", "mistralai/ministral-14b-2512", caps=CHAT_CAPS),
                m("Mistral Ministral 3 8B", "mistralai/ministral-8b-2512", caps=CHAT_CAPS),
                m("Mistral Ministral 3 3B", "mistralai/ministral-3b-2512", caps=CHAT_CAPS),
                m("Google Gemma 4 31B", "google/gemma-4-31b-it", caps=CHAT_CAPS),
                m("Google Gemma 4 31B Free", "google/gemma-4-31b-it:free", True, CHAT_CAPS),
                m("Google Gemma 4 26B A4B", "google/gemma-4-26b-a4b-it", caps=CHAT_CAPS),
                m("Google Gemma 4 26B A4B Free", "google/gemma-4-26b-a4b-it:free", True, CHAT_CAPS),
                m("Google Gemma 3 27B", "google/gemma-3-27b-it", caps=VISION_CAPS),
                m("Google Gemma 3 27B Free", "google/gemma-3-27b-it:free", True, VISION_CAPS),
                m("Google Gemma 3 12B", "google/gemma-3-12b-it", caps=VISION_CAPS),
                m("Google Gemma 3 12B Free", "google/gemma-3-12b-it:free", True, VISION_CAPS),
                m("Google Gemma 3 4B", "google/gemma-3-4b-it", caps=VISION_CAPS),
                m("Google Gemma 3 4B Free", "google/gemma-3-4b-it:free", True, VISION_CAPS),
                m("Google Gemma 3n 4B Free", "google/gemma-3n-e4b-it:free", True, VISION_CAPS),
                m("Google Gemma 3n 2B Free", "google/gemma-3n-e2b-it:free", True, VISION_CAPS),
                m("Meta Llama 4 Scout", "meta-llama/llama-4-scout", caps=VISION_CAPS),
                m("Meta Llama 4 Maverick", "meta-llama/llama-4-maverick", caps=VISION_CAPS),
                m("Arcee Trinity Large Thinking", "arcee-ai/trinity-large-thinking", caps=REASONING_CAPS),
                m("Arcee Trinity Large Preview Free", "arcee-ai/trinity-large-preview:free", True, CHAT_CAPS),
                m("NVIDIA Nemotron 3 Super 120B Free", "nvidia/nemotron-3-super-120b-a12b:free", True, CHAT_CAPS),
                m("Inception Mercury 2", "inception/mercury-2", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "Ollama (Local)",
            "slug": "ollama",
            "description": "Run private local AI models on your own hardware.",
            "icon": "OL",
            "models": [
                m("DeepSeek R1 1.5B", "deepseek-r1:1.5b", True, REASONING_CAPS),
                m("DeepSeek R1 8B", "deepseek-r1:8b", True, REASONING_CAPS),
                m("DeepSeek R1 32B", "deepseek-r1:32b", True, REASONING_CAPS),
                m("DeepSeek V3", "deepseek-v3:latest", True, CHAT_CAPS),
                m("Llama 4 Scout", "llama4:scout", True, VISION_CAPS),
                m("Llama 4 Maverick", "llama4:maverick", True, VISION_CAPS),
                m("Qwen 3.6", "qwen3.6:latest", True, VISION_CAPS),
                m("Qwen 2.5 Coder 32B", "qwen2.5-coder:32b", True, CHAT_CAPS),
                m("Qwen 2.5 7B", "qwen2.5:latest", True, CHAT_CAPS),
                m("Gemma 4", "gemma4:latest", True, VISION_CAPS),
                m("Gemma 3 27B", "gemma3:27b", True, VISION_CAPS),
                m("Mistral Nemo 12B", "mistral-nemo:latest", True, CHAT_CAPS),
                m("Phi 4", "phi4:latest", True, REASONING_CAPS),
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to the OpenAI API.",
            "icon": "OA",
            "models": [
                m("GPT-5.5", "gpt-5.5", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.5 Pro", "gpt-5.5-pro", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.4", "gpt-5.4", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.4 Mini", "gpt-5.4-mini", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.4 Nano", "gpt-5.4-nano", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.3 Codex", "gpt-5.3-codex", caps=CHAT_CAPS),
                m("GPT-5.2", "gpt-5.2", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-5.2 Pro", "gpt-5.2-pro", caps={**VISION_CAPS, "document_input": True}),
                m("GPT-4o", "gpt-4o", caps=VISION_CAPS),
                m("GPT-4o Mini", "gpt-4o-mini", caps=VISION_CAPS),
                m("GPT-4 Turbo", "gpt-4-turbo", caps=VISION_CAPS),
                m("o4-mini", "o4-mini", caps=REASONING_CAPS),
                m("o3", "o3", caps=REASONING_CAPS),
                m("o3-mini", "o3-mini", caps=REASONING_CAPS),
                m("o1", "o1", caps=REASONING_CAPS),
                m("GPT Image 2", "gpt-image-2", caps={"image_input": True, "image_generation": True}),
                m("GPT Image 1", "gpt-image-1", caps={"image_input": True, "image_generation": True}),
                m("GPT Image 1 Mini", "gpt-image-1-mini", caps={"image_input": True, "image_generation": True}),
                m("Sora 2", "sora-2", caps={"video_generation": True}),
                m("Sora 2 Pro", "sora-2-pro", caps={"video_generation": True}),
                m("GPT Realtime 1.5", "gpt-realtime-1.5", caps={"audio_input": True, "audio_generation": True, **CHAT_CAPS}),
                m("GPT Realtime", "gpt-realtime", caps={"audio_input": True, "audio_generation": True, **CHAT_CAPS}),
                m("GPT Audio", "gpt-audio", caps={"audio_input": True, "audio_generation": True}),
                m("GPT Audio Mini", "gpt-audio-mini", caps={"audio_input": True, "audio_generation": True}),
                m("GPT-4o Mini TTS", "gpt-4o-mini-tts", caps={"audio_generation": True}),
                m("Text Embedding 3 Large", "text-embedding-3-large", caps={"embedding_generation": True}),
                m("Text Embedding 3 Small", "text-embedding-3-small", caps={"embedding_generation": True}),
            ],
        },
        {
            "name": "Google Gemini",
            "slug": "gemini",
            "description": "Google Gemini API models with multimodal input and long context.",
            "icon": "GG",
            "models": [
                m("Gemini 3.1 Pro Preview", "gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS),
                m("Gemini 3.1 Flash Preview", "gemini-3.1-flash-preview", True, MULTIMODAL_CAPS),
                m("Gemini 3.1 Flash Lite Preview", "gemini-3.1-flash-lite-preview", caps=MULTIMODAL_CAPS),
                m("Gemini 3 Pro Preview", "gemini-3-pro-preview", caps=MULTIMODAL_CAPS),
                m("Gemini 2.5 Pro", "gemini-2.5-pro", caps=MULTIMODAL_CAPS),
                m("Gemini 2.5 Flash", "gemini-2.5-flash", True, MULTIMODAL_CAPS),
                m("Gemini 2.5 Flash Lite", "gemini-2.5-flash-lite", caps=MULTIMODAL_CAPS),
                m("Gemini 2.0 Flash", "gemini-2.0-flash", True, MULTIMODAL_CAPS),
                m("Gemini 2.0 Flash Lite", "gemini-2.0-flash-lite", True, MULTIMODAL_CAPS),
                m("Gemini Embedding 2 Preview", "gemini-embedding-2-preview", caps={"embedding_generation": True}),
                m("Gemini Embedding 001", "gemini-embedding-001", caps={"embedding_generation": True}),
                m("Gemini 3.1 Flash Image Preview", "gemini-3.1-flash-image-preview", caps={"image_input": True, "image_generation": True}),
                m("Gemini 2.5 Flash Image Preview", "gemini-2.5-flash-image-preview", caps={"image_input": True, "image_generation": True}),
                m("Imagen 4", "imagen-4.0-generate-001", caps={"image_generation": True}),
                m("Imagen 4 Ultra", "imagen-4.0-ultra-generate-001", caps={"image_generation": True}),
                m("Veo 3.1", "veo-3.1-generate-preview", caps={"image_input": True, "video_generation": True}),
                m("Veo 3.1 Fast", "veo-3.1-fast-generate-preview", caps={"image_input": True, "video_generation": True}),
                m("Veo 3", "veo-3.0-generate-preview", caps={"image_input": True, "video_generation": True}),
            ],
        },
        {
            "name": "Anthropic",
            "slug": "anthropic",
            "description": "Claude models by Anthropic for coding, writing, and long-running agents.",
            "icon": "AN",
            "models": [
                m("Claude Opus 4.7", "claude-opus-4-7", caps=VISION_CAPS),
                m("Claude Opus 4.6", "claude-opus-4-6", caps=VISION_CAPS),
                m("Claude Opus 4.6 Fast", "claude-opus-4-6-fast", caps=VISION_CAPS),
                m("Claude Sonnet 4.6", "claude-sonnet-4-6", caps=VISION_CAPS),
                m("Claude Haiku 4.5", "claude-haiku-4-5", caps=VISION_CAPS),
                m("Claude Sonnet 4.5", "claude-sonnet-4-5", caps=VISION_CAPS),
                m("Claude Sonnet 4", "claude-sonnet-4-20250514", caps=VISION_CAPS),
                m("Claude Opus 4", "claude-opus-4-20250514", caps=VISION_CAPS),
                m("Claude 3.7 Sonnet", "claude-3-7-sonnet-20250219", caps=VISION_CAPS),
                m("Claude 3.5 Sonnet", "claude-3-5-sonnet-20241022", caps=VISION_CAPS),
                m("Claude 3.5 Haiku", "claude-3-5-haiku-20241022", caps=CHAT_CAPS),
            ],
        },
        {
            "name": "Perplexity",
            "slug": "perplexity",
            "description": "Search-augmented Sonar models with citations.",
            "icon": "PX",
            "models": [
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
                m("DeepSeek Chat V4 Flash", "deepseek-v4-flash", caps=CHAT_CAPS),
                m("DeepSeek Chat V3.1", "deepseek-chat", caps=CHAT_CAPS),
                m("DeepSeek Reasoner R1", "deepseek-reasoner", caps=REASONING_CAPS),
            ],
        },
        {
            "name": "xAI",
            "slug": "xai",
            "description": "Grok models from xAI.",
            "icon": "XA",
            "models": [
                m("Grok 4.20", "grok-4.20", caps={**VISION_CAPS, "document_input": True}),
                m("Grok 4.20 Multi-Agent", "grok-4.20-multi-agent", caps={**VISION_CAPS, "document_input": True}),
                m("Grok Code Fast 1", "grok-code-fast-1", caps=CHAT_CAPS),
                m("Grok 4", "grok-4", caps=VISION_CAPS),
                m("Grok 3", "grok-3", caps=CHAT_CAPS),
                m("Grok 3 Mini", "grok-3-mini", caps=REASONING_CAPS),
                m("Grok 2 Vision 1212", "grok-2-vision-1212", caps=VISION_CAPS),
                m("Grok Imagine Image", "grok-imagine-image", caps={"image_generation": True}),
                m("Grok Imagine Video", "grok-imagine-video", caps={"image_input": True, "video_generation": True}),
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
