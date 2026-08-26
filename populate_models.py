import os
import django
from copy import deepcopy
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "workflow_backend.settings.local")
django.setup()

from django.db import transaction
from llm.models import AIProvider, AIModel
from llm.providers import SUPPORTED_PROVIDERS


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


# Ids that 404 against their provider's live /models endpoint or are superseded
# by a newer tier. Checked 2026-08-24 (re-verified against OpenRouter + NIM live
# catalogs where reachable; OpenAI/Ollama ids are vendor-named and unverified).
# Deactivated rather than deleted — a saved run may still reference one.
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
    # Pruned 2026-08-24 — superseded by newer tier, not 404 but irrelevant.
    "google/gemini-3.6-flash",              # superseded by gemini-3.7-flash
    "google/gemini-3.5-flash-lite",         # superseded by gemini-3.7-flash
    "deepseek/deepseek-r1",                 # superseded by deepseek-v4-pro/flash
    "x-ai/grok-4.20-multi-agent",           # superseded by grok-4.6 / grok-4.5
    "qwen/qwen3.6-plus",                    # superseded by qwen3.8-max / 27b
    "qwen/qwen3-32b",                       # superseded by qwen3.8-27b dense
    "inception/mercury-2",                  # low usage, no price/perf edge vs kimi-k3
    # NIM — older Nemotron gens superseded by Nemotron 3 Nano/Super/Ultra
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "meta/llama-3.3-70b-instruct",           # superseded by llama-4 scout/maverick
    # OpenAI direct — superseded by GPT-5.6 tiers
    "gpt-4o",                               # superseded by gpt-5.6-terra/sol
    "o3",                                   # superseded by o4-mini / gpt-5.6 reasoning
    # Ollama local — tiny or superseded
    "deepseek-r1:1.5b",                     # superseded by 8b/32b, too small for R1 quality
    "qwen2.5-coder:32b",                    # superseded by qwen3.6:latest
]


def m(name, value, is_free=False, caps=None, input_price="0.0000", output_price="0.0000", cached_price=None, context=0):
    """
    Helper: caps is capability dict, pricing is USD per 1M tokens as strings
    (kept as string to avoid binary float). cached_price None means no cache tier.
    context is max input tokens (0 = unknown).
    Pricing source: official provider pages + OpenRouter listings, verified 2026-08-24.
    """
    return {
        "name": name,
        "value": value,
        "is_free": is_free,
        "caps": caps or {},
        "input_price_per_million": input_price,
        "output_price_per_million": output_price,
        "cached_input_price_per_million": cached_price,
        "context_window": context,
    }


def build_model_defaults(item):
    caps = {**DEFAULT_CAPS, **item.get("caps", {})}

    defaults = {
        "provider": item["provider"],
        "name": item["name"],
        "is_free": item["is_free"],
        "input_price_per_million": Decimal(str(item.get("input_price_per_million", "0.0000"))),
        "output_price_per_million": Decimal(str(item.get("output_price_per_million", "0.0000"))),
        "cached_input_price_per_million": (
            Decimal(str(item["cached_input_price_per_million"]))
            if item.get("cached_input_price_per_million") is not None else None
        ),
        "context_window": int(item.get("context_window", 0)),
    }

    for cap_key, field_name in CAPABILITY_FIELD_MAP.items():
        defaults[field_name] = caps[cap_key]

    return defaults


def populate():
    # Catalogue reviewed 2026-08-24 with live pricing. Rule: keep the latest
    # per tier; legacy only if still widely deployed. Same provider, same tier:
    # newer wins, older goes. Pruning pass removed 14 models that were superseded
    # by a strictly better tier. See RETIRED_MODEL_VALUES.
    #
    # Coverage target: every user can pick along three axes without duplicates:
    #   speed — Haiku 4.5 / Luna / V4 Flash / Qwen3.7 Flash / Scout (fastest)
    #           vs Sonnet 5 / Terra / Gemini 3.7 Flash (balanced)
    #           vs Fable 5 / Sol / Nemotron Ultra / Qwen3.8 Max (frontier)
    #   intelligence — Fable 5 / Sol / Ultra at top, Sonnet/Terra mid, Haiku/Luna low
    #   cost — $0 free → $0.03/$0.13 (Qwen3.7 Flash) → $10/$50 (Fable 5)
    #
    # Pricing is USD per 1M tokens, cached price is cache-hit input where the
    # provider bills it (Anthropic, OpenAI, DeepSeek, Qwen). Local Ollama is $0.
    # Sources checked 2026-08-24:
    #   Anthropic: platform.claude.com/docs/en/about-claude/pricing
    #   OpenAI: openai.com/index/gpt-5-6 + developers.openai.com pricing
    #   Google: blog.google Gemini 3.7 Flash post + Vertex pricing
    #   DeepSeek: api-docs.deepseek.com + mercatus Aug 16 peak/off-peak
    #   OpenRouter listings for Qwen, Llama, Grok, Mistral, Kimi, Nemotron
    #
    # OpenRouter and NVIDIA NIM entries were checked against live /models
    # endpoints where reachable — every id returned 200. OpenAI/Ollama ids are
    # vendor-named and unverified: a wrong id fails closed with a 404 at call time.
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI gateway for routing across hosted model providers.",
            "icon": "OR",
            "models": [
                # --- Routing (price varies by routed model; 0 here) ---
                m("Auto Router", "openrouter/auto", caps=CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0),
                m("Free Models Router", "openrouter/free", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0),
                m("Pareto Code Router", "openrouter/pareto-code", caps=CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0),
                # --- OpenAI via OpenRouter (GPT-5.6 tiers: Sol > Terra > Luna) ---
                # Pricing post July 30 cut: Sol $5/$30, Terra $2/$12 (was $2.50/$15), Luna $0.20/$1.20 (was $1/$6, 80% cut)
                # Cache: 90% off input → Sol $0.50, Terra $0.20, Luna $0.02
                m("OpenAI GPT-5.6 Sol", "openai/gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}, input_price="5.0000", output_price="30.0000", cached_price="0.5000", context=1500000),
                m("OpenAI GPT-5.6 Terra", "openai/gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="12.0000", cached_price="0.2000", context=1500000),
                m("OpenAI GPT-5.6 Luna", "openai/gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}, input_price="0.2000", output_price="1.2000", cached_price="0.0200", context=1500000),
                m("OpenAI GPT-4o Mini", "openai/gpt-4o-mini", caps=VISION_CAPS, input_price="0.1500", output_price="0.6000", cached_price="0.0750", context=128000),
                # --- Anthropic via OpenRouter (4 tiers) ---
                # Fable $10/$50 cache $1, Opus $5/$25 cache $0.50, Sonnet $2/$10 cache $0.20, Haiku $1/$5 cache $0.10
                # Context: Opus/Sonnet/Fable 1M, Haiku 200K per platform.claude.com
                m("Anthropic Claude Fable 5", "anthropic/claude-fable-5", caps=VISION_CAPS, input_price="10.0000", output_price="50.0000", cached_price="1.0000", context=1000000),
                m("Anthropic Claude Opus 5", "anthropic/claude-opus-5", caps=VISION_CAPS, input_price="5.0000", output_price="25.0000", cached_price="0.5000", context=1000000),
                m("Anthropic Claude Sonnet 5", "anthropic/claude-sonnet-5", caps=VISION_CAPS, input_price="2.0000", output_price="10.0000", cached_price="0.2000", context=1000000),
                m("Anthropic Claude Haiku 4.5", "anthropic/claude-haiku-4.5", caps=VISION_CAPS, input_price="1.0000", output_price="5.0000", cached_price="0.1000", context=200000),
                # --- Google via OpenRouter ---
                # Gemini 3.1 Pro Preview $2/$12 (≤200K) $4/$18 (>200K) — store base tier; Gemini 3.7 Flash $0.75/$3.75 intro (→ $1.50/$7.50 Jan 2027)
                m("Google Gemini 3.1 Pro Preview", "google/gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS, input_price="2.0000", output_price="12.0000", context=1000000),
                m("Google Gemini 3.7 Flash", "google/gemini-3.7-flash", caps=MULTIMODAL_CAPS, input_price="0.7500", output_price="3.7500", context=1000000),
                # --- DeepSeek via OpenRouter (MIT open-weights) ---
                # Aug 16 peak/off-peak: Pro $0.66/$1.98 off-peak $1.32/$3.96 peak, cached $0.022/$0.044; Flash $0.22/$0.66 cached $0.007/$0.014
                # Store off-peak as base — peak is 2x.
                m("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", caps=REASONING_CAPS, input_price="0.6600", output_price="1.9800", cached_price="0.0220", context=1000000),
                m("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", caps=REASONING_CAPS, input_price="0.2200", output_price="0.6600", cached_price="0.0070", context=1000000),
                # --- xAI via OpenRouter ---
                # Grok 4.6 $2/$6 500K, Grok 4.5 $2/$6 500K
                m("xAI Grok 4.6", "x-ai/grok-4.6", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="6.0000", context=500000),
                m("xAI Grok 4.5", "x-ai/grok-4.5", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="6.0000", context=500000),
                # --- Meta via OpenRouter ---
                # Llama 4 Scout $0.10/$0.30 1.31M, Maverick $0.20/$0.80 1.05M
                m("Meta Llama 4 Maverick", "meta-llama/llama-4-maverick", caps=VISION_CAPS, input_price="0.2000", output_price="0.8000", context=1050000),
                m("Meta Llama 4 Scout", "meta-llama/llama-4-scout", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=1310000),
                # --- Qwen via OpenRouter ---
                # Qwen3.8 Max $2/$6 1M cached $0.25, Qwen3.8 27B $0.35/$2.75 cached $0.035, Qwen3.7 Flash $0.03/$0.13 ultra-cheap
                m("Qwen3.8 Max", "qwen/qwen3.8-max", caps={**VISION_CAPS, "video_input": True}, input_price="2.0000", output_price="6.0000", cached_price="0.2500", context=1000000),
                m("Qwen3.8 27B", "qwen/qwen3.8-27b", caps={**VISION_CAPS, "video_input": True}, input_price="0.3500", output_price="2.7500", cached_price="0.0350", context=1000000),
                m("Qwen3.7 Flash", "qwen/qwen3.7-flash", caps={**VISION_CAPS, "video_input": True}, input_price="0.0300", output_price="0.1300", context=1000000),
                # --- Mistral via OpenRouter ---
                m("Mistral Small 4", "mistralai/mistral-small-2603", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=128000),
                # --- Google Open via OpenRouter ---
                m("Google Gemma 4 31B Free", "google/gemma-4-31b-it:free", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=128000),
                # --- NVIDIA via OpenRouter (the :free suffix is OpenRouter-only) ---
                m("NVIDIA Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS, input_price="0.5000", output_price="2.2000", context=1000000),
                m("NVIDIA Nemotron 3 Super 120B Free", "nvidia/nemotron-3-super-120b-a12b:free", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=1000000),
                # --- Notable independents ---
                m("Moonshot Kimi K3", "moonshotai/kimi-k3", caps=VISION_CAPS, input_price="3.0000", output_price="15.0000", context=1048576),
            ],
        },
        {
            "name": "NVIDIA NIM",
            "slug": "nvidia",
            "description": "NVIDIA NIM API — optimized inference for NVIDIA and open-source models.",
            "icon": "NV",
            "models": [
                # Nemotron 3 line — ids carry no :free suffix on NIM.
                # Lightning: 30B MoE 3B active, Super: 120B 12B active, Ultra: 550B 55B active, Nano: 30B
                # Pricing via NIM is lower than OpenRouter routed; use list $0.50/$2.20 for Ultra as reference, cheaper for smaller.
                m("Nemotron 3.5 Lightning 30B", "nvidia/nemotron-3.5-lightning-30b-a3b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=1000000),
                m("Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS, input_price="0.5000", output_price="2.2000", context=1000000),
                m("Nemotron 3 Super 120B", "nvidia/nemotron-3-super-120b-a12b", caps=CHAT_CAPS, input_price="0.3000", output_price="1.2000", context=1000000),
                m("Nemotron 3 Nano 30B", "nvidia/nemotron-3-nano-30b-a3b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=1000000),
                m("Nemotron Nano 12B VL", "nvidia/nemotron-nano-12b-v2-vl", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=128000),
                m("Nemotron Nano VL 8B", "nvidia/llama-3.1-nemotron-nano-vl-8b-v1", caps=VISION_CAPS, input_price="0.0800", output_price="0.2400", context=128000),
                m("Nemotron Parse", "nvidia/nemotron-parse", caps={"image_input": True, "text_input": False, "structured_output": True}, input_price="0.0500", output_price="0.0500", context=128000),
                # Open-weight models hosted on NIM (pruned older gens)
                m("DeepSeek V4 Pro", "deepseek-ai/deepseek-v4-pro", caps=REASONING_CAPS, input_price="0.6600", output_price="1.9800", cached_price="0.0220", context=1000000),
                m("DeepSeek V4 Flash", "deepseek-ai/deepseek-v4-flash", caps=REASONING_CAPS, input_price="0.2200", output_price="0.6600", cached_price="0.0070", context=1000000),
                m("Moonshot Kimi K2.6", "moonshotai/kimi-k2.6", caps=VISION_CAPS, input_price="0.6000", output_price="2.4000", context=262144),
                m("GPT-OSS 120B", "openai/gpt-oss-120b", caps=CHAT_CAPS, input_price="0.2000", output_price="0.8000", context=128000),
                m("GPT-OSS 20B", "openai/gpt-oss-20b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=128000),
                m("Mistral Medium 3.5", "mistralai/mistral-medium-3.5-128b", caps=CHAT_CAPS, input_price="0.2000", output_price="0.8000", context=128000),
                m("Gemma 4 31B", "google/gemma-4-31b-it", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=256000),
                # Embeddings — RAG pipeline model
                m("NV EmbedQA E5 v5", "nvidia/nv-embedqa-e5-v5", caps={"embedding_generation": True}, input_price="0.0200", output_price="0.0000", context=8192),
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to the OpenAI API.",
            "icon": "OA",
            "models": [
                m("GPT-5.6 Sol", "gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}, input_price="5.0000", output_price="30.0000", cached_price="0.5000", context=1500000),
                m("GPT-5.6 Sol Pro", "gpt-5.6-sol-pro", caps={**VISION_CAPS, "document_input": True}, input_price="5.0000", output_price="30.0000", cached_price="0.5000", context=1500000),
                m("GPT-5.6 Terra", "gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="12.0000", cached_price="0.2000", context=1500000),
                m("GPT-5.6 Luna", "gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}, input_price="0.2000", output_price="1.2000", cached_price="0.0200", context=1500000),
                m("GPT-4o Mini", "gpt-4o-mini", caps=VISION_CAPS, input_price="0.1500", output_price="0.6000", cached_price="0.0750", context=128000),
                m("o4-mini", "o4-mini", caps=REASONING_CAPS, input_price="1.1000", output_price="4.4000", cached_price="0.2750", context=200000),
                # Specialised modalities — latest only (pricing is per image/sec, not per token; 0 here)
                m("GPT Image 2", "gpt-image-2", caps={"image_input": True, "image_generation": True}, input_price="0.0000", output_price="0.0000", context=0),
                m("Sora 2 Pro", "sora-2-pro", caps={"video_generation": True}, input_price="0.0000", output_price="0.0000", context=0),
                m("GPT Realtime 1.5", "gpt-realtime-1.5", caps={"audio_input": True, "audio_generation": True, **CHAT_CAPS}, input_price="4.0000", output_price="16.0000", context=128000),
                m("Text Embedding 3 Large", "text-embedding-3-large", caps={"embedding_generation": True}, input_price="0.1300", output_price="0.0000", context=8191),
            ],
        },
        {
            "name": "Ollama (Local)",
            "slug": "ollama",
            "description": "Run private local AI models on your own hardware.",
            "icon": "OL",
            "models": [
                # Local models are $0 — no meter. Context is Ollama default.
                m("DeepSeek R1 8B", "deepseek-r1:8b", True, REASONING_CAPS, input_price="0.0000", output_price="0.0000", context=128000),
                m("DeepSeek R1 32B", "deepseek-r1:32b", True, REASONING_CAPS, input_price="0.0000", output_price="0.0000", context=128000),
                m("Llama 4 Scout", "llama4:scout", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=10000000),
                m("Qwen 3.6", "qwen3.6:latest", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=262144),
                m("Qwen 3 8B", "qwen3:8b", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=32768),
                m("Gemma 4", "gemma4:latest", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=262144),
                m("Gemma 4 4B", "gemma4:4b", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=128000),
                m("Phi 4", "phi4:latest", True, REASONING_CAPS, input_price="0.0000", output_price="0.0000", context=16384),
                m("Mistral 7B", "mistral:7b", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=32768),
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
                print(f"   - [{badge}] {item['name']}  ${item['input_price_per_million']}/${item['output_price_per_million']}  ctx {item['context_window']}")

            print("   Done.\n")

        # Providers dropped from the supported set. Deactivated rather than
        # deleted for the same reason as the models below: a saved workflow may
        # still point at one, and their models remain reachable through the
        # OpenRouter block above under `anthropic/…`, `google/…`, `x-ai/…`.
        stale = AIProvider.objects.filter(is_active=True).exclude(
            slug__in=SUPPORTED_PROVIDERS)
        if stale.exists():
            print("Deactivating providers no longer supported:")
            for name in sorted(stale.values_list("name", flat=True)):
                print(f"   - {name}")
            AIModel.objects.filter(provider__in=stale).update(is_active=False)
            stale.update(is_active=False)
            print()

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
    print("\nPricing is USD per 1M tokens. Use llm.pricing.estimate_cost_usd(")
    print("  input_tokens, output_tokens, input_price, output_price) to bill.")
    print("  Ollama local models are $0. Free cloud models are $0 on this meter.")


if __name__ == "__main__":
    populate()
