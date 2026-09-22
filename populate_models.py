import os
import django
from copy import deepcopy
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "workflow_backend.settings.local")
django.setup()

from django.db import transaction
from django.utils import timezone
from llm.models import AIProvider, AIModel
from llm.providers import SUPPORTED_PROVIDERS
from llm.effort import (
    ALL as EFFORT_ALL,
    STANDARD as EFFORT_STANDARD,
    TOGGLEABLE as EFFORT_TOGGLEABLE,
    WITH_MINIMAL as EFFORT_WITH_MINIMAL,
)


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
    # Retired 2026-09-01. Each verified against NIM with the platform key:
    # 410 "reached its end of life" for the EOL block, 404 "not found for
    # account" for the unentitled pair. They were all still is_active=True, so
    # the model picker offered them and every pick failed at the first token.
    "nvidia/nv-embedqa-e5-v5",               # 410 — RAG embedder, EOL 2026-08-25
    "nvidia/nemotron-nano-12b-v2-vl",        # 410 — vision witness, EOL 2026-08-26
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",  # 410 — vision fallback, EOL 2026-08-26
    "deepseek-ai/deepseek-v4-pro",           # 410 — EOL on NIM
    "deepseek-ai/deepseek-v4-flash",         # 410 — EOL on NIM
    "mistralai/mistral-medium-3.5-128b",     # 410 — EOL on NIM
    "moonshotai/kimi-k2.6",                  # 404 — not entitled for this account
    # OpenAI direct — superseded by GPT-5.6 tiers
    "gpt-4o",                               # superseded by gpt-5.6-terra/sol
    "o3",                                   # superseded by o4-mini / gpt-5.6 reasoning
    # Ollama local — tiny or superseded
    "deepseek-r1:1.5b",                     # superseded by 8b/32b, too small for R1 quality
    "qwen2.5-coder:32b",                    # superseded by qwen3.6:latest,
    # Pruned 2026-09-02 -- not cost-efficient / fast / intelligent, superseded by 2026-08/09 open-source wave
    "openai/gpt-4o-mini",                   # superseded by gpt-5.6-luna ($0.20/$1.20, 1.5M ctx, far smarter)
    "gpt-4o-mini",                          # openai provider duplicate of above
    "google/gemini-3.1-pro-preview",        # superseded by gemini-3.7-flash ($0.75 vs $2/12, faster)
    "deepseek/deepseek-v4-pro",             # superseded by v4.1-flash (native multimodal, 7x cheaper; vendor routes pro->flash after Sep 14)
    "deepseek/deepseek-v4-flash",           # superseded by v4-flash-0731 ($0.07 vs $0.22)
    "deepseek/deepseek-v4-pro-0813",        # superseded by v4.1-flash ($0.15 vs $1.12, native multimodal; vendor routes pro->flash after Sep 14 noon BJT)
    # Gemma family -- small, not competitive vs Qwen/DeepSeek/NVIDIA (pruned per user 2026-09-02)
    "google/gemma-4-31b-it:free",
    "google/gemma-4-31b-it",
    "gemma4:latest",
    "gemma4:4b",
    # Pruned 2026-09-12 (verified against OpenRouter /v1/models live) — dead or
    # same-tier-older ids found by diffing the live catalogue against this seed.
    "qwen/qwen3.8-max",                     # 404 on OR — replaced by max-0902 snapshot
    "qwen/qwen3.8-2.4t-a95b",               # same $2/$6 tier older, superseded by max-0902
    "inception/mercury-2.5-preview",        # delisted from OR — superseded by mercury-2.5 GA
]


def m(name, value, is_free=False, caps=None, input_price="0.0000", output_price="0.0000",
      cached_price=None, context=0, cache_write_price=None, effort=(), default_effort="",
      description=""):
    """
    Helper: caps is capability dict, pricing is USD per 1M tokens as strings
    (kept as string to avoid binary float). cached_price None means no cache tier.
    context is max input tokens (0 = unknown).
    Pricing source: official provider pages + OpenRouter listings, verified 2026-08-24.

    `cache_write_price` is what the provider charges to *write* the cache, and
    None means it charges nothing — which is true of OpenAI and of every model
    served without prompt caching. Anthropic-family models routed through
    OpenRouter charge ~1.25x input to write, and folding that into the input
    rate understates exactly the long runs it is meant to bound. Note that
    `is_free=True` is load-bearing beyond display: `llm/pricing.py` reads it to
    tell a genuinely free model from one whose price nobody filled in, and only
    the latter is reported as `unpriced`.

    `effort` is which reasoning-effort rungs this model actually serves, from
    `llm.effort.LADDER`. The default is `()` — **no effort control** — and that
    is the safe default rather than a lazy one: a declared rung is a claim the
    runtime acts on by putting `reasoning_effort` on the wire, which OpenAI
    answers with a 400 for a model that does not take it. So an unverified
    model says nothing and behaves exactly as it did before the knob existed.
    `default_effort` is the rung to use when the caller names none; blank means
    the provider's own default, which is the right answer unless we have
    measured that a different rung is better for this model.
    """
    return {
        "name": name,
        "value": value,
        "is_free": is_free,
        "caps": caps or {},
        "description": description,
        "input_price_per_million": input_price,
        "output_price_per_million": output_price,
        "cached_input_price_per_million": cached_price,
        "cache_write_price_per_million": cache_write_price,
        "context_window": context,
        "effort_levels": list(effort),
        "default_effort": default_effort,
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
        "cache_write_price_per_million": (
            Decimal(str(item["cache_write_price_per_million"]))
            if item.get("cache_write_price_per_million") is not None else None
        ),
        "context_window": int(item.get("context_window", 0)),
        "effort_levels": list(item.get("effort_levels") or []),
        "default_effort": item.get("default_effort", "") or "",
    }

    for cap_key, field_name in CAPABILITY_FIELD_MAP.items():
        defaults[field_name] = caps[cap_key]

    # Only when the seed names one: an unconditional write would wipe a
    # description someone customised in admin on every boot (this script runs
    # at every backend boot).
    if item.get("description"):
        defaults["description"] = item["description"]

    return defaults


def populate():
    # Catalogue reviewed 2026-08-24 with live pricing. Rule: keep the latest
    # per tier; legacy only if still widely deployed. Same provider, same tier:
    # newer wins, older goes. Pruning pass removed 14 models that were superseded
    # by a strictly better tier. See RETIRED_MODEL_VALUES.
    #
    # Addendum 2026-09-12 (second wave: DeepSeek V4.1 + surrounding updates, ids +
    # pricing verified against OpenRouter /v1/models live, modalities from the
    # list endpoint's architecture block): V4.1 Flash (official 09-10, first
    # native-multimodal DeepSeek), GPT-6 Astra Pro (reasoning.mode=pro twin),
    # Qwen3.8 Max 0902 (replaces dead `qwen3.8-max` id + older `2.4t-a95b` row),
    # Mercury 2.5 GA (preview id delisted). Deliberately NOT added: Sakana Fugu
    # Max / Ultra v2 (09-11, orchestration products over rented frontier pools,
    # not open weights — nothing to self-host or price predictably); Tencent
    # Hy4-preview / IBM Granite 4.2-8B / Ling-3.0-Flash family (preview- or
    # signal-free; no benchmark/usage case vs rows above); Kimi K2.6 (predates
    # the 09-02 wave, passed over for K2.7-Code then); DeepSeek V5 (rumor, no
    # release/model card/weights as of 09-11); all `:batch` / `:free` / `~alias`
    # variants (the seed carries none — batch is a billing mode, not a model).
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
    # Reasoning effort (added 2026-09-03). `effort=` declares which rungs a row
    # actually serves; omitting it means **no effort control**, which is the
    # default for every row above that does not name one. That asymmetry is
    # deliberate: a declared rung causes `reasoning_effort` (or OpenRouter's
    # `reasoning` object) to go on the wire, and OpenAI answers that with a 400
    # for a model that does not take it, so silence has to be the safe answer.
    #
    # Three families, by what the model can actually be asked:
    #   EFFORT_WITH_MINIMAL — the GPT-5.6 tiers, which add a rung below `low`.
    #   EFFORT_STANDARD     — low/medium/high, no way to switch thinking off.
    #   EFFORT_TOGGLEABLE   — hybrid checkpoints that serve a thinking and a
    #                         non-thinking mode, so `none` is a real request.
    #                         Only OpenRouter and Ollama can express it on the
    #                         wire; through NIM it degrades to the cheapest rung.
    #
    # Every row is left at `default_effort=""` — the provider's own default. A
    # default chosen here would silently change what an unconfigured call costs,
    # and nothing above has been measured well enough to justify that.
    providers = [
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "description": "Unified AI gateway for routing across hosted model providers.",
            "icon": "OR",
            "models": [
                # --- Routing (price varies by routed model; 0 here) ---
                # The routers carry `EFFORT_STANDARD` even though *which* model
                # answers is decided upstream per request. `reasoning` is an
                # OpenRouter-level abstraction: it accepts the field on a router
                # and maps it onto whatever it routes to, dropping it for a
                # model that has no such knob. So the rung is a request rather
                # than a guarantee here — which is the most a router can offer,
                # and strictly better than withholding the control on the model
                # a new chat starts on. No `none`: a router cannot promise the
                # model it picks is one whose thinking can be switched off.
                m("Auto Router", "openrouter/auto", caps=CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0, effort=EFFORT_STANDARD),
                m("Free Models Router", "openrouter/free", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0, effort=EFFORT_STANDARD),
                m("Pareto Code Router", "openrouter/pareto-code", caps=CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=0, effort=EFFORT_STANDARD),
                # --- OpenAI via OpenRouter (GPT-6 Astra > GPT-5.6 tiers: Sol > Terra > Luna) ---
                # GPT-6 Astra (GA 2026-09-03, verified 2026-09-12 against OpenRouter
                # /v1/models live: ctx 1050000, $10/$50 cached $1 write $12.50).
                # Direct OpenAI docs add: 128K max out, Apr 30 2026 cutoff,
                # reasoning.effort low..max (no none/minimal rung on this model,
                # so EFFORT_STANDARD — xhigh/max snap to high via nearest()).
                # Requests >272K input tokens bill 2x in / 1.5x out; stored base.
                m("OpenAI GPT-6 Astra", "openai/gpt-6-astra", caps={**VISION_CAPS, "document_input": True}, input_price="10.0000", output_price="50.0000", cached_price="1.0000", cache_write_price="12.5000", context=1050000, effort=EFFORT_STANDARD),
                # --- Sep 2026: GPT-6 Astra Pro (verified 2026-09-12 against OpenRouter
                # /v1/models live: ctx 1050000, same $10/$50 cached $1 write $12.50).
                # Same checkpoint as Astra, served with reasoning.mode=pro — a mode,
                # not an effort rung, so same EFFORT_STANDARD and no UI distinction
                # beyond the row itself.
                m("OpenAI GPT-6 Astra Pro", "openai/gpt-6-astra-pro", caps={**VISION_CAPS, "document_input": True}, input_price="10.0000", output_price="50.0000", cached_price="1.0000", cache_write_price="12.5000", context=1050000, effort=EFFORT_STANDARD),
                # Pricing post Aug 21 promo: Sol $4/$20 (was $5/$30) through 2026-11-21, Terra $2/$12 (was $2.50/$15), Luna $0.20/$1.20 (was $1/$6, 80% cut Jul 30)
                # Cache: 90% off input → Sol $0.40, Terra $0.20, Luna $0.02 — Sol promo verified 2026-08-21 against developers.openai.com
                m("OpenAI GPT-5.6 Sol", "openai/gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}, input_price="4.0000", output_price="20.0000", cached_price="0.4000", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("OpenAI GPT-5.6 Terra", "openai/gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="12.0000", cached_price="0.2000", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("OpenAI GPT-5.6 Luna", "openai/gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}, input_price="0.2000", output_price="1.2000", cached_price="0.0200", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("OpenAI GPT-4o Mini", "openai/gpt-4o-mini", caps=VISION_CAPS, input_price="0.1500", output_price="0.6000", cached_price="0.0750", context=128000),
                # --- Anthropic via OpenRouter (4 tiers) ---
                # Fable $10/$50 cache $1, Opus $5/$25 cache $0.50, Sonnet $2/$10 cache $0.20, Haiku $1/$5 cache $0.10
                # Context: Opus/Sonnet/Fable 1M, Haiku 200K per platform.claude.com
                m("Anthropic Claude Fable 5", "anthropic/claude-fable-5", caps=VISION_CAPS, input_price="10.0000", output_price="50.0000", cached_price="1.0000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Anthropic Claude Opus 5", "anthropic/claude-opus-5", caps=VISION_CAPS, input_price="5.0000", output_price="25.0000", cached_price="0.5000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Anthropic Claude Sonnet 5", "anthropic/claude-sonnet-5", caps=VISION_CAPS, input_price="2.0000", output_price="10.0000", cached_price="0.2000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Anthropic Claude Haiku 4.5", "anthropic/claude-haiku-4.5", caps=VISION_CAPS, input_price="1.0000", output_price="5.0000", cached_price="0.1000", context=200000, effort=EFFORT_TOGGLEABLE),
                # --- Sep 2026: Claude Fable 5.1 (GA 2026-09-01, verified 2026-09-12
                # against OpenRouter /v1/models live: ctx 1M, $10/$50) ---
                # Direct upgrade over Fable 5 (agentic coding, long-horizon
                # coherence); base rate held, cache-READ cut 75% $1.00 -> $0.25.
                # Text+image+file in, 128K out, adaptive reasoning. Fable 5 stays:
                # still widely deployed, so this is an addition, not a supersede.
                m("Anthropic Claude Fable 5.1", "anthropic/claude-fable-5.1", caps={**VISION_CAPS, "document_input": True}, input_price="10.0000", output_price="50.0000", cached_price="0.2500", cache_write_price="12.5000", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- Google via OpenRouter ---
                # Gemini 3.1 Pro Preview $2/$12 (≤200K) $4/$18 (>200K) — store base tier; Gemini 3.7 Flash $0.75/$3.75 intro (→ $1.50/$7.50 Jan 2027)
                m("Google Gemini 3.1 Pro Preview", "google/gemini-3.1-pro-preview", caps=MULTIMODAL_CAPS, input_price="2.0000", output_price="12.0000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Google Gemini 3.7 Flash", "google/gemini-3.7-flash", caps=MULTIMODAL_CAPS, input_price="0.7500", output_price="3.7500", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- Sep 2026: Gemini 3.8 Flash (GA 2026-09-02, verified 2026-09-12
                # against OpenRouter /v1/models live: ctx 1048576) ---
                # Intro pricing $0.75/$3.75 cached $0.075 through 2026-12-31, then
                # $1.50/$7.50 — stored rate goes stale Jan 2027, revisit then.
                # Thinking levels LOW/MEDIUM/HIGH (default MEDIUM); MINIMAL is a
                # native-API validation error, so EFFORT_STANDARD, not TOGGLEABLE.
                m("Google Gemini 3.8 Flash", "google/gemini-3.8-flash", caps=MULTIMODAL_CAPS, input_price="0.7500", output_price="3.7500", cached_price="0.0750", context=1048576, effort=EFFORT_STANDARD),
                # --- DeepSeek via OpenRouter (MIT open-weights) ---
                # Aug 16 peak/off-peak: Pro $0.66/$1.98 off-peak $1.32/$3.96 peak, cached $0.022/$0.044; Flash $0.22/$0.66 cached $0.007/$0.014
                # Store off-peak as base — peak is 2x.
                m("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", caps=REASONING_CAPS, input_price="0.6600", output_price="1.9800", cached_price="0.0220", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", caps=REASONING_CAPS, input_price="0.2200", output_price="0.6600", cached_price="0.0070", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- xAI via OpenRouter ---
                # Grok 4.6 $2/$6 500K cached $0.50, Grok 4.5 $2/$6 cached $0.30 — verified 2026-08-27 against docs.x.ai
                m("xAI Grok 4.6", "x-ai/grok-4.6", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="6.0000", cached_price="0.5000", context=500000, effort=EFFORT_STANDARD),
                m("xAI Grok 4.5", "x-ai/grok-4.5", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="6.0000", cached_price="0.3000", context=500000, effort=EFFORT_STANDARD),
                # --- Meta via OpenRouter ---
                # Llama 4 Scout $0.10/$0.30 1.31M, Maverick $0.20/$0.80 1.05M
                m("Meta Llama 4 Maverick", "meta-llama/llama-4-maverick", caps=VISION_CAPS, input_price="0.2000", output_price="0.8000", context=1050000),
                m("Meta Llama 4 Scout", "meta-llama/llama-4-scout", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=1310000),
                # --- Qwen via OpenRouter ---
                # Qwen3.8 Max $2/$6 1M cached $0.25, Qwen3.8 27B $0.35/$2.75 cached $0.035, Qwen3.7 Flash $0.03/$0.13 ultra-cheap
                # Qwen3.8 Max 0902: updated snapshot of Qwen3.8 Max (2.4T MoE),
                # $2/$6 cached $0.25 write $2.50, 1M ctx, text+image+video->text
                # (verified 2026-09-12 against OpenRouter /v1/models live). The
                # un-dated `qwen/qwen3.8-max` id is gone from OR (404) and the
                # weights-named `2.4t-a95b` id is the same tier older — both
                # retire below; one row per tier.
                m("Qwen3.8 Max 0902", "qwen/qwen3.8-max-0902", caps={**VISION_CAPS, "video_input": True}, input_price="2.0000", output_price="6.0000", cached_price="0.2500", cache_write_price="2.5000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Qwen3.8 27B", "qwen/qwen3.8-27b", caps={**VISION_CAPS, "video_input": True}, input_price="0.3500", output_price="2.7500", cached_price="0.0350", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Qwen3.7 Flash", "qwen/qwen3.7-flash", caps={**VISION_CAPS, "video_input": True}, input_price="0.0300", output_price="0.1300", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- Mistral via OpenRouter ---
                m("Mistral Small 4", "mistralai/mistral-small-2603", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=128000),
                # --- Google Open via OpenRouter ---
                # --- NVIDIA via OpenRouter (the :free suffix is OpenRouter-only) ---
                m("NVIDIA Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS, input_price="0.5000", output_price="2.2000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("NVIDIA Nemotron 3 Super 120B Free", "nvidia/nemotron-3-super-120b-a12b:free", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- Notable independents ---
                m("Moonshot Kimi K3", "moonshotai/kimi-k3", caps=VISION_CAPS, input_price="3.0000", output_price="15.0000", context=1048576, effort=EFFORT_STANDARD),
                # --- Aug 2026 additions (verified 2026-08-27) ---
                # Muse Spark 1.2: Meta 2026-08-05, $1.25/$4.25 cached $0.15 (contributor $0.10/$0.20), 1M ctx — docs: dev.meta.ai/docs/pricing-rate-limits
                # GLM-5.3: Z.ai 2026-08-14, $1.40/$4.40 cached $0.26, 1M ctx 128K out — docs: docs.z.ai/guides/overview/pricing
                m("Meta Muse Spark 1.2", "meta/muse-spark-1.2", caps=MULTIMODAL_CAPS, input_price="1.2500", output_price="4.2500", cached_price="0.1500", context=1048576),
                m("Z.ai GLM-5.3", "z-ai/glm-5.3", caps=REASONING_CAPS, input_price="1.4000", output_price="4.4000", cached_price="0.2600", context=1000000, effort=EFFORT_TOGGLEABLE),
                # --- Sep 2026: Muse Spark 1.3 family (verified 2026-09-03 against OpenRouter + Meta pricing) ---
                # Standard: meta/muse-spark-1.3, $1.25/$4.25 cached $0.15, 1M ctx, text+image+video in — private, not trained on.
                # Contributor: meta/muse-spark-1.3-contributor, $0.10/$0.20 cached $0.002, same caps/ctx — ~12x cheaper
                # in exchange for Meta training on prompts/completions. Same checkpoint, distinct billing endpoint.
                m("Meta Muse Spark 1.3", "meta/muse-spark-1.3", caps=MULTIMODAL_CAPS, input_price="1.2500", output_price="4.2500", cached_price="0.1500", context=1048576),
                m("Meta Muse Spark 1.3 Contributor", "meta/muse-spark-1.3-contributor", caps=MULTIMODAL_CAPS, input_price="0.1000", output_price="0.2000", cached_price="0.0020", context=1048576),
                # --- Sep 2026: DeepSeek V4 Flash Vision Exp (verified 2026-09-03) ---
                # deepseek/deepseek-v4-flash-vision-exp, 2026-08-21 exp, $0.22/$0.66 cached $0.007 on OpenRouter,
                # 1M ctx, text+image->text + reasoning/tools/structured/caching. Vision twin of the 0731 Flash row below.
                # --- Sep 2026 open-source wave (updated versions, verified 2026-09-02 against OpenRouter /v1/models) ---
                # Qwen3.8 Flash: open weights Qwen/Qwen3.8-Flash-Next, $0.15/$0.47 1M ctx, image+video->text -- updated, more intelligent than 3.7 Flash ($0.03) at still-cheap price
                m("Qwen3.8 Flash", "qwen/qwen3.8-flash", caps={**VISION_CAPS, "video_input": True}, input_price="0.1500", output_price="0.4700", context=1000000, effort=EFFORT_TOGGLEABLE),
                # DeepSeek V4 Flash 0731: open weights deepseek-ai/DeepSeek-V4-Flash-0731, $0.07/$0.18 1.3M -- cheapest capable tier, kept alongside V4.1 (multimodal costs 2x)
                m("DeepSeek V4 Flash 0731", "deepseek/deepseek-v4-flash-0731", caps=REASONING_CAPS, input_price="0.0700", output_price="0.1800", context=1310720, effort=EFFORT_TOGGLEABLE),
                m("DeepSeek V4 Flash Vision Exp", "deepseek/deepseek-v4-flash-vision-exp", caps={**VISION_CAPS, "numeric_input": True, "numeric_generation": True}, input_price="0.2200", output_price="0.6600", cached_price="0.0070", context=1048576, effort=EFFORT_TOGGLEABLE),
                # --- Sep 2026: DeepSeek V4.1 Flash (official 2026-09-10, verified
                # 2026-09-12 against OpenRouter /v1/models live: ctx 1048576) ---
                # First native-multimodal DeepSeek (552B CED MoE, 8B in / 16B out
                # active), open weights (MIT) + `deepseek-flash` API identity.
                # $0.15/$0.60 cached $0.003 — 7x under the 0813 pro tier it
                # replaces (DeepSeek routes deepseek-v4-pro -> Flash after Sep 14
                # noon BJT until V4.1 Pro, hence 0813 retires below). OR's served
                # modality is text+image->text, so VISION_CAPS without image out,
                # whatever the launch blogs claim about the architecture.
                m("DeepSeek V4.1 Flash", "deepseek/deepseek-v4.1-flash", caps={**VISION_CAPS, "numeric_input": True, "numeric_generation": True}, input_price="0.1500", output_price="0.6000", cached_price="0.0030", context=1048576, effort=EFFORT_TOGGLEABLE),
                # Z.ai GLM-5.3 Flash: open weights zai-org/GLM-5.3-Flash, $0.075/$0.25 cached $0.015, 1M ctx (providers vary 262K-1.31M) -- cost-efficient flash of 5.3 ($1.40)
                m("Z.ai GLM-5.3 Flash", "z-ai/glm-5.3-flash", caps={**VISION_CAPS, "video_input": True}, input_price="0.0750", output_price="0.2500", cached_price="0.0150", context=1048576, effort=EFFORT_TOGGLEABLE),
                # NVIDIA Nemotron 3.5 Lightning: open weights nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16, $0.08/$0.20 262k -- cheaper than 3.5-30b-a3b
                m("NVIDIA Nemotron 3.5 Lightning", "nvidia/nemotron-3.5-lightning", caps=CHAT_CAPS, input_price="0.0800", output_price="0.2000", context=262144, effort=EFFORT_TOGGLEABLE),
                # StepFun Step 3.7 Flash: open weights stepfun-ai/Step-3.7-Flash, 196B MoE 11B active, $0.20/$1.15 262k multimodal -- fastest cheap
                m("StepFun Step 3.7 Flash", "stepfun/step-3.7-flash", caps=MULTIMODAL_CAPS, input_price="0.2000", output_price="1.1500", context=262144, effort=EFFORT_STANDARD),
                # MiniMax M3: open weights MiniMaxAI/Minimax-M3, $0.30/$1.20 1M multimodal -- cost-efficient coding
                m("MiniMax M3", "minimax/minimax-m3", caps=MULTIMODAL_CAPS, input_price="0.3000", output_price="1.2000", context=1048576, effort=EFFORT_STANDARD),
                # Moonshot Kimi K2.7 Code: open weights moonshotai/Kimi-K2.7-Code, $0.66/$3.40 262k image -- updated cheaper than K3 ($3/$15)
                m("Moonshot Kimi K2.7 Code", "moonshotai/kimi-k2.7-code", caps=VISION_CAPS, input_price="0.6600", output_price="3.4000", context=262144, effort=EFFORT_STANDARD),
                # Meta Muse Glimmer 30B: open weights meta-models/Muse-Glimmer-30B, $0.30/$1.20 131k image -- new Meta open
                m("Meta Muse Glimmer 30B", "meta/muse-glimmer-30b", caps=VISION_CAPS, input_price="0.3000", output_price="1.2000", context=131072),
                # Inception Mercury 2.5: diffusion LM GA (verified 2026-09-12 against
                # OpenRouter /v1/models live: $0.04/$0.15 cached $0.004, 260k ctx,
                # text->text). The `-preview` id is delisted from OR — retires below.
                m("Inception Mercury 2.5", "inception/mercury-2.5", caps=CHAT_CAPS, input_price="0.0400", output_price="0.1500", cached_price="0.0040", context=260000),
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
                m("Nemotron 3.5 Lightning 30B", "nvidia/nemotron-3.5-lightning-30b-a3b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Nemotron 3 Ultra 550B", "nvidia/nemotron-3-ultra-550b-a55b", caps=REASONING_CAPS, input_price="0.5000", output_price="2.2000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Nemotron 3 Super 120B", "nvidia/nemotron-3-super-120b-a12b", caps=CHAT_CAPS, input_price="0.3000", output_price="1.2000", context=1000000, effort=EFFORT_TOGGLEABLE),
                m("Nemotron 3 Nano 30B", "nvidia/nemotron-3-nano-30b-a3b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=1000000, effort=EFFORT_TOGGLEABLE),
                # Vision — the witness chain in chat/vision/resolve.py. Both
                # re-verified 2026-09-01 by sending a real PNG and reading the
                # rendered number back; the previous two NIM VL models are EOL.
                m("Llama 3.2 11B Vision", "meta/llama-3.2-11b-vision-instruct", caps=VISION_CAPS, input_price="0.0600", output_price="0.0600", context=128000),
                m("Nemotron 3 Nano Omni 30B", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", caps=VISION_CAPS, input_price="0.1000", output_price="0.3000", context=128000, effort=EFFORT_TOGGLEABLE),
                m("Llama 3.2 90B Vision", "meta/llama-3.2-90b-vision-instruct", caps=VISION_CAPS, input_price="0.3500", output_price="0.4000", context=128000),
                m("Nemotron Parse", "nvidia/nemotron-parse", caps={"image_input": True, "text_input": False, "structured_output": True}, input_price="0.0500", output_price="0.0500", context=128000),
                # Open-weight models hosted on NIM (pruned older gens)
                m("GPT-OSS 120B", "openai/gpt-oss-120b", caps=CHAT_CAPS, input_price="0.2000", output_price="0.8000", context=128000, effort=EFFORT_STANDARD),
                m("GPT-OSS 20B", "openai/gpt-oss-20b", caps=CHAT_CAPS, input_price="0.1000", output_price="0.3000", context=128000, effort=EFFORT_STANDARD),
                # Embeddings — RAG pipeline model. 2048-dim; inference/engine.py
                # pins EMBEDDING_DIM to match, and EMBEDDER_VERSION carries the
                # pair so a swap re-indexes instead of mixing two vector spaces.
                m("Nemotron 3 Embed 1B", "nvidia/nemotron-3-embed-1b", caps={"embedding_generation": True}, input_price="0.0200", output_price="0.0000", context=8192),
            ],
        },
        {
            "name": "OpenCode Zen",
            "slug": "opencode",
            "description": "Free models on your own OpenCode Zen account (bring your own key). Free models may be used for training.",
            "icon": "OC",
            "models": [
                # Ids verified live 2026-09-22 against keyless
                # GET https://opencode.ai/zen/v1/models (all present), then
                # checked against the endpoint table in
                # https://opencode.ai/docs/zen (2026-09-22): six of these are
                # documented `chat/completions` models; `deepseek-v4-flash-free`
                # is absent from that table but its paid sibling
                # (`deepseek-v4-flash`) is chat/completions, so it stays
                # pending the first keyed call. `muse-spark-1.3-contributor-free`
                # is deliberately NOT seeded: the docs map it to the `/responses`
                # endpoint (Responses API, a different protocol), which this
                # provider does not speak — offering it would be offering a
                # model every call fails on.
                # NOT yet verified — needs a Zen key (see
                # docs/OPENCODE_ZEN_PLAN.md §8): chat-completions answers,
                # streaming, `tool_calls`, and `reasoning_effort` acceptance.
                # Until then: CHAT_CAPS claims tool calling because these are
                # agentic-coding models on a chat-completions endpoint (a wrong
                # claim degrades to the model ignoring tools, while a missing
                # one withholds the toolbox entirely); effort stays empty, the
                # safe default; context is 0 (the listing carries none).
                m("Big Pickle (Free)", "opencode/big-pickle", True, CHAT_CAPS,
                  description="Stealth model, free on your OpenCode Zen account — prompts may be used for training. Avoid private data."),
                m("DeepSeek V4 Flash (Free)", "opencode/deepseek-v4-flash-free", True, CHAT_CAPS,
                  description="DeepSeek V4 (not V4.1), free on your OpenCode Zen account — prompts may be used for training. Avoid private data."),
                m("Mimo v2.6 Flash (Free)", "opencode/mimo-v2.6-flash-free", True, CHAT_CAPS,
                  description="Limited-time free on your OpenCode Zen account — prompts may be used for training. Avoid private data."),
                m("Mimo v2.5 (Free)", "opencode/mimo-v2.5-free", True, CHAT_CAPS,
                  description="Limited-time free on your OpenCode Zen account — prompts may be used for training. Avoid private data."),
                m("Ling 3.0 Flash (Free)", "opencode/ling-3.0-flash-fin-free", True, CHAT_CAPS,
                  description="Limited-time free on your OpenCode Zen account — prompts may be used for training. Avoid private data."),
                m("Nemotron 3 Ultra (Free)", "opencode/nemotron-3-ultra-free", True, CHAT_CAPS,
                  description="NVIDIA trial — do not submit personal or confidential data."),
                m("Nemotron 3.5 Lightning (Free)", "opencode/nemotron-3.5-lightning-free", True, CHAT_CAPS,
                  description="NVIDIA trial — do not submit personal or confidential data."),
            ],
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "description": "Direct connection to the OpenAI API.",
            "icon": "OA",
            "models": [
                # GPT-6 Astra (GA 2026-09-03) — direct-API twin of the OpenRouter
                # row above; same $10/$50 cached $1 write $12.50, 1.05M ctx.
                m("GPT-6 Astra", "gpt-6-astra", caps={**VISION_CAPS, "document_input": True}, input_price="10.0000", output_price="50.0000", cached_price="1.0000", cache_write_price="12.5000", context=1050000, effort=EFFORT_STANDARD),
                m("GPT-6 Astra Pro", "gpt-6-astra-pro", caps={**VISION_CAPS, "document_input": True}, input_price="10.0000", output_price="50.0000", cached_price="1.0000", cache_write_price="12.5000", context=1050000, effort=EFFORT_STANDARD),
                # Promo 2026-08-21: Sol $4/$20 cached $0.40 through 2026-11-21 (was $5/$30/$0.50)
                m("GPT-5.6 Sol", "gpt-5.6-sol", caps={**VISION_CAPS, "document_input": True}, input_price="4.0000", output_price="20.0000", cached_price="0.4000", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("GPT-5.6 Sol Pro", "gpt-5.6-sol-pro", caps={**VISION_CAPS, "document_input": True}, input_price="4.0000", output_price="20.0000", cached_price="0.4000", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("GPT-5.6 Terra", "gpt-5.6-terra", caps={**VISION_CAPS, "document_input": True}, input_price="2.0000", output_price="12.0000", cached_price="0.2000", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("GPT-5.6 Luna", "gpt-5.6-luna", caps={**VISION_CAPS, "document_input": True}, input_price="0.2000", output_price="1.2000", cached_price="0.0200", context=1500000, effort=EFFORT_WITH_MINIMAL),
                m("GPT-4o Mini", "gpt-4o-mini", caps=VISION_CAPS, input_price="0.1500", output_price="0.6000", cached_price="0.0750", context=128000),
                m("o4-mini", "o4-mini", caps=REASONING_CAPS, input_price="1.1000", output_price="4.4000", cached_price="0.2750", context=200000, effort=EFFORT_STANDARD),
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
                m("DeepSeek R1 8B", "deepseek-r1:8b", True, REASONING_CAPS, input_price="0.0000", output_price="0.0000", context=128000, effort=EFFORT_TOGGLEABLE),
                m("DeepSeek R1 32B", "deepseek-r1:32b", True, REASONING_CAPS, input_price="0.0000", output_price="0.0000", context=128000, effort=EFFORT_TOGGLEABLE),
                m("Llama 4 Scout", "llama4:scout", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=10000000),
                m("Qwen 3.6", "qwen3.6:latest", True, VISION_CAPS, input_price="0.0000", output_price="0.0000", context=262144, effort=EFFORT_TOGGLEABLE),
                m("Qwen 3 8B", "qwen3:8b", True, CHAT_CAPS, input_price="0.0000", output_price="0.0000", context=32768, effort=EFFORT_TOGGLEABLE),
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
        # Rows deliberately retired (`retired_at` set — by a refresh holding
        # live evidence, or by the force list below) are NOT resurrected here:
        # without this guard every backend boot would undo the refresh. A row
        # that is genuinely back upstream is re-listed by the refresh itself
        # on sight, or by staff in admin.
        AIModel.objects.filter(
            value__in=synced_model_values, retired_at__isnull=True,
        ).update(is_active=True)

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
            retired.update(is_active=False, retired_at=timezone.now())
            print()

        # Prune Gemma and weaker irrelevant older models (2026-09-02)
        # Any active model not in the curated synced list is stale.
        # This removes Gemma family and $0 placeholder older models (qwen3-14b, gemini-2.5, gpt-5.2 etc.)
        # not cost-efficient / fast / intelligent vs current Qwen3.8/DeepSeek/NVIDIA wave.
        #
        # Only `curated` rows: `live` rows belong to the catalogue refresh
        # (`llm/catalog_refresh.py`) and `hand` rows to whoever added them in
        # admin. Pruning those here would undo a refresh (or a person) on
        # every backend boot, since this seed runs at every boot.
        stale = AIModel.objects.filter(is_active=True, source='curated').exclude(value__in=synced_model_values)
        if stale.exists():
            print(f"Pruning {stale.count()} stale/weak models not in curated list (Gemma + older):")
            for v in sorted(stale.values_list("value", flat=True)):
                print(f"   - {v}")
            stale.update(is_active=False)
            print()

    print("=" * 80)
    print("Successfully synchronized seeded models.")
    print("=" * 80)
    print("\nPricing is USD per 1M tokens. Use llm.pricing.estimate_cost_usd(")
    print("  input_tokens, output_tokens, input_price, output_price) to bill.")
    print("  Ollama local models are $0. Free cloud models are $0 on this meter.")


if __name__ == "__main__":
    populate()
