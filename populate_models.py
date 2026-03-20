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
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI Gateway. Access 600+ models with a single API key.",
            "icon": "🌐",
            "models": [
                # --- Free Models ---
                {
                    "name": "🆓 Auto-Router (Best Free)",
                    "value": "openrouter/free",
                    "is_free": True,
                    "caps": {"structured_output": True, "tool_calling": True},
                },
                {
                    "name": "🆓 DeepSeek R1 0528 (Free)",
                    "value": "deepseek/deepseek-r1-0528:free",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                    },
                },
                {
                    "name": "🆓 Qwen3 235B A22B Thinking (Free)",
                    "value": "qwen/qwen3-235b-a22b-thinking-2507:free",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "structured_output": True,
                    },
                },
                {
                    "name": "🆓 Gemma 3 27B (Free)",
                    "value": "google/gemma-3-27b-it:free",
                    "is_free": True,
                    "caps": {"image_input": True},
                },
                {
                    "name": "🆓 Llama 4 Scout 17B (Free)",
                    "value": "meta-llama/llama-4-scout:free",
                    "is_free": True,
                    "caps": {"image_input": True},
                },
                {
                    "name": "🆓 Gemini 2.5 Flash (Free)",
                    "value": "google/gemini-2.5-flash:free",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "🆓 Gemini 3 Flash Preview (Free)",
                    "value": "google/gemini-3-flash-preview:free",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                # --- Paid Models ---
                {
                    "name": "💰 OpenAI GPT-5.4 (Latest Frontier)",
                    "value": "openai/gpt-5.4",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT-5.4 Thinking (Reasoning)",
                    "value": "openai/gpt-5.4-thinking",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Anthropic Claude Opus 4.6 (Best for Agents)",
                    "value": "anthropic/claude-opus-4.6",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Anthropic Claude Sonnet 4.6",
                    "value": "anthropic/claude-sonnet-4.6",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Anthropic Claude Haiku 4.5 (Fast)",
                    "value": "anthropic/claude-haiku-4.5",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Google Gemini 3.1 Pro",
                    "value": "google/gemini-3.1-pro",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Google Gemini 3.1 Flash-Lite",
                    "value": "google/gemini-3.1-flash-lite",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 DeepSeek Chat (V3)",
                    "value": "deepseek/deepseek-chat",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 DeepSeek Reasoner (R1)",
                    "value": "deepseek/deepseek-reasoner",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT-4o",
                    "value": "openai/gpt-4o",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT-4o Mini",
                    "value": "openai/gpt-4o-mini",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Perplexity Sonar",
                    "value": "perplexity/sonar",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Perplexity Sonar Pro (Search)",
                    "value": "perplexity/sonar-pro",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Perplexity Sonar Reasoning Pro",
                    "value": "perplexity/sonar-reasoning-pro",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Perplexity Sonar Deep Research",
                    "value": "perplexity/sonar-deep-research",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT-5-image",
                    "value": "openai/gpt-5-image",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT-5-image-mini",
                    "value": "openai/gpt-5-image-mini",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT Audio",
                    "value": "openai/gpt-audio",
                    "is_free": False,
                    "caps": {
                        "audio_input": True,
                        "audio_generation": True,
                    },
                },
                {
                    "name": "💰 OpenAI GPT Audio Mini",
                    "value": "openai/gpt-audio-mini",
                    "is_free": False,
                    "caps": {
                        "audio_input": True,
                        "audio_generation": True,
                    },
                },
                {
                    "name": "💰 Google Gemini 3.1 Flash Image Preview (Nano Banana 2)",
                    "value": "google/gemini-3.1-flash-image-preview",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 Black Forest Labs FLUX.2 Max",
                    "value": "black-forest-labs/flux.2-max",
                    "is_free": False,
                    "caps": {
                        "image_generation": True,
                    },
                },
                # --- Restored & Latest Models (March 2026) ---
                {
                    "name": "💰 MiniMax M2.5 (Latest Coding)",
                    "value": "minimax/minimax-m2.5",
                    "is_free": False,
                    "caps": {"tool_calling": True, "structured_output": True},
                },
                {
                    "name": "💰 MiniMax M2-her (Dialogue)",
                    "value": "minimax/minimax-m2-her",
                    "is_free": False,
                    "caps": {"text_generation": True},
                },
                {
                    "name": "💰 MiniMax M2.1 (Fast/Agentic)",
                    "value": "minimax/minimax-m2.1",
                    "is_free": False,
                    "caps": {"tool_calling": True, "structured_output": True},
                },
                {
                    "name": "💰 Kimi K2.5 (Latest Multimodal)",
                    "value": "moonshotai/kimi-k2.5",
                    "is_free": False,
                    "caps": {"image_input": True, "tool_calling": True, "structured_output": True},
                },
                {
                    "name": "💰 Kimi K2 Thinking (Reasoning)",
                    "value": "moonshotai/kimi-k2-thinking",
                    "is_free": False,
                    "caps": {"numeric_input": True, "structured_output": True},
                },
                {
                    "name": "🆓 Kimi VL A3B Thinking (Free)",
                    "value": "moonshotai/kimi-vl-a3b-thinking:free",
                    "is_free": True,
                    "caps": {"image_input": True},
                },
                {
                    "name": "💰 Qwen 3-Max Thinking",
                    "value": "qwen/qwen3-max-thinking",
                    "is_free": False,
                    "caps": {"numeric_input": True, "structured_output": True, "tool_calling": True},
                },
                {
                    "name": "💰 Qwen 3.5 Plus",
                    "value": "qwen/qwen3.5-plus",
                    "is_free": False,
                    "caps": {"image_input": True, "structured_output": True, "tool_calling": True},
                },
                {
                    "name": "💰 Qwen 3.5 Flash",
                    "value": "qwen/qwen3.5-flash",
                    "is_free": False,
                    "caps": {"image_input": True, "structured_output": True, "tool_calling": True},
                },
                {
                    "name": "💰 Qwen 3-Coder Next",
                    "value": "qwen/qwen3-coder-next",
                    "is_free": False,
                    "caps": {"tool_calling": True, "structured_output": True},
                },
                {
                    "name": "💰 Mistral Large 3 (2512)",
                    "value": "mistralai/mistral-large-3-2512",
                    "is_free": False,
                    "caps": {"tool_calling": True, "structured_output": True},
                },
                {
                    "name": "💰 Mistral Medium 3.1",
                    "value": "mistralai/mistral-medium-3.1",
                    "is_free": False,
                    "caps": {"tool_calling": True},
                },
                {
                    "name": "💰 Devstral 2 (Agentic Coding)",
                    "value": "mistralai/devstral-2",
                    "is_free": False,
                    "caps": {"tool_calling": True, "structured_output": True},
                },
                {
                    "name": "🆓 Llama 3.3 70B Instruct (Free)",
                    "value": "meta-llama/llama-3.3-70b-instruct:free",
                    "is_free": True,
                    "caps": {"structured_output": True, "tool_calling": True},
                },
            ],
        },
        {
            "name": "Ollama (Local)",
            "slug": "ollama",
            "description": "Run private, local AI models on your own hardware.",
            "icon": "🦙",
            "models": [
                {
                    "name": "🆓 DeepSeek R1 8B (Recommended)",
                    "value": "deepseek-r1:8b",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                    },
                },
                {
                    "name": "🆓 DeepSeek R1 32B",
                    "value": "deepseek-r1:32b",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                    },
                },
                {
                    "name": "🆓 DeepSeek R1 1.5B (Edge)",
                    "value": "deepseek-r1:1.5b",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                    },
                },
                {
                    "name": "🆓 DeepSeek V3",
                    "value": "deepseek-v3:latest",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                    },
                },
                {
                    "name": "🆓 Llama 4 Scout",
                    "value": "llama4:scout",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                    },
                },
                {
                    "name": "🆓 Llama 4 Maverick",
                    "value": "llama4:maverick",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                    },
                },
                {
                    "name": "🆓 Qwen 2.5 Coder 32B",
                    "value": "qwen2.5-coder:32b",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                        "structured_output": True,
                    },
                },
                {
                    "name": "🆓 Qwen 2.5 7B",
                    "value": "qwen2.5:latest",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                    },
                },
                {
                    "name": "🆓 Gemma 3 27B",
                    "value": "gemma3:27b",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                    },
                },
                {
                    "name": "🆓 Mistral Nemo 12B",
                    "value": "mistral-nemo:latest",
                    "is_free": True,
                    "caps": {},
                },
                {
                    "name": "🆓 Phi-4 (Microsoft)",
                    "value": "phi4:latest",
                    "is_free": True,
                    "caps": {
                        "numeric_input": True,
                    },
                },
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to OpenAI API.",
            "icon": "🤖",
            "models": [
                {
                    "name": "💰 GPT-5.4 (Latest Frontier)",
                    "value": "gpt-5.4",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 GPT-5.4 Thinking (Reasoning)",
                    "value": "gpt-5.4-thinking",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 GPT-5.4 Pro",
                    "value": "gpt-5.4-pro",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 o1",
                    "value": "o1",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 o3-mini",
                    "value": "o3-mini",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 gpt-4o",
                    "value": "gpt-4o",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 gpt-4o-mini",
                    "value": "gpt-4o-mini",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 gpt-image-1",
                    "value": "gpt-image-1",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 gpt-audio-1.5",
                    "value": "gpt-audio-1.5",
                    "is_free": False,
                    "caps": {
                        "audio_input": True,
                        "audio_generation": True,
                    },
                },
                {
                    "name": "💰 gpt-4-turbo (Legacy)",
                    "value": "gpt-4-turbo",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                    },
                },
                {
                    "name": "💰 GPT Image 1",
                    "value": "gpt-image-1",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 GPT Image 1 Mini",
                    "value": "gpt-image-1-mini",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 GPT Audio",
                    "value": "gpt-audio",
                    "is_free": False,
                    "caps": {
                        "audio_input": True,
                        "audio_generation": True,
                    },
                },
                {
                    "name": "💰 GPT Audio Mini",
                    "value": "gpt-audio-mini",
                    "is_free": False,
                    "caps": {
                        "audio_input": True,
                        "audio_generation": True,
                    },
                },
                {
                    "name": "💰 GPT-4o Mini TTS",
                    "value": "gpt-4o-mini-tts",
                    "is_free": False,
                    "caps": {
                        "audio_generation": True,
                    },
                },
            ],
        },
        {
            "name": "Google Gemini",
            "slug": "gemini",
            "description": "Google AI Studio models with massive context windows.",
            "icon": "✨",
            "models": [
                {
                    "name": "🆓 Gemini 3 Flash Preview (Free Tier)",
                    "value": "gemini-3-flash-preview",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "🆓 Gemini 2.5 Flash (Free Tier)",
                    "value": "gemini-2.5-flash",
                    "is_free": True,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 3.1 Pro (Latest, Best Reasoning)",
                    "value": "gemini-3.1-pro",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 3 Pro",
                    "value": "gemini-3-pro",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 3.1 Flash-Lite (Fastest)",
                    "value": "gemini-3.1-flash-lite",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 2.5 Flash-Lite",
                    "value": "gemini-2.5-flash-lite",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 2.5 Pro (Legacy)",
                    "value": "gemini-2.5-pro",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "audio_input": True,
                        "video_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Gemini 3.1 Flash Image (Nano Banana 2)",
                    "value": "gemini-3.1-flash-image",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 Veo 3.1 Generate Preview",
                    "value": "veo-3.1-generate-preview",
                    "is_free": False,
                    "caps": {
                        "video_generation": True,
                    },
                },
            ],
        },
        {
            "name": "Anthropic",
            "slug": "anthropic",
            "description": "Claude series by Anthropic. Best for coding agents and long tasks.",
            "icon": "🎭",
            "models": [
                {
                    "name": "💰 Claude Opus 4.6 (Best for Agents)",
                    "value": "claude-opus-4-6",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Claude Sonnet 4.6 (Latest, Best Coding)",
                    "value": "claude-sonnet-4-6",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Claude Haiku 4.5 (Fastest)",
                    "value": "claude-haiku-4-5",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Claude Sonnet 4.5 (Legacy)",
                    "value": "claude-sonnet-4-5",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "document_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Claude 3.5 Sonnet (Legacy)",
                    "value": "claude-3-5-sonnet-20241022",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
            ],
        },
        {
            "name": "Perplexity",
            "slug": "perplexity",
            "description": "Real-time web search and citation enabled AI.",
            "icon": "🔍",
            "models": [
                {
                    "name": "💰 Sonar (Lightweight Search)",
                    "value": "sonar",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Sonar Pro (Advanced Search)",
                    "value": "sonar-pro",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Sonar Reasoning Pro (CoT)",
                    "value": "sonar-reasoning-pro",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 Sonar Deep Research (Exhaustive)",
                    "value": "sonar-deep-research",
                    "is_free": False,
                    "caps": {
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
            ],
        },
        {
            "name": "DeepSeek",
            "slug": "deepseek",
            "description": "Official DeepSeek API. Best for reasoning and coding tasks.",
            "icon": "🧠",
            "models": [
                {
                    "name": "💰 DeepSeek Chat (V3)",
                    "value": "deepseek-chat",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
                {
                    "name": "💰 DeepSeek Reasoner (R1)",
                    "value": "deepseek-reasoner",
                    "is_free": False,
                    "caps": {
                        "numeric_input": True,
                        "numeric_generation": True,
                        "structured_output": True,
                        "tool_calling": True,
                    },
                },
            ],
        },
        {
            "name": "xAI",
            "slug": "xai",
            "description": "Explorative and witty models from xAI.",
            "icon": "𝕏",
            "models": [
                {
                    "name": "💰 Grok Imagine Image",
                    "value": "grok-imagine-image",
                    "is_free": False,
                    "caps": {
                        "image_generation": True,
                    },
                },
                {
                    "name": "💰 Grok Imagine Video",
                    "value": "grok-imagine-video",
                    "is_free": False,
                    "caps": {
                        "image_input": True,
                        "video_generation": True,
                    },
                },
            ],
        },
    ]


    providers = deepcopy(providers)
    
    print("\n" + "=" * 80)
    print("🚀 Synchronizing AI Models Database...")
    print("=" * 80 + "\n")
    
    synced_model_values = []
    
    with transaction.atomic():
        for provider_data in providers:
            model_data = provider_data.pop("models")
            
            provider, _ = AIProvider.objects.update_or_create(
                slug=provider_data["slug"],
                defaults=provider_data,
            )
            
            print(f"✅ Provider: {provider.name}")
            
            for item in model_data:
                item["provider"] = provider
                defaults = build_model_defaults(item)
                
                AIModel.objects.update_or_create(
                    value=item["value"],
                    defaults=defaults,
                )
                
                synced_model_values.append(item["value"])
                badge = "🆓" if item["is_free"] else "💰"
                print(f"   ├─ {badge} {item['name']}")
                
            print("   └─ Done.\n")
            
        AIModel.objects.exclude(value__in=synced_model_values).delete()
        
    print("=" * 80)
    print("✅ Successfully synchronized all models.")
    print("=" * 80)


if __name__ == "__main__":
    populate()
