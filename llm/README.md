# `llm/`: talking to AI models

Everything about AI model providers: which providers exist, which models they
offer, how a call is made, what it costs, and what happens when it fails.

**The one rule:** every model call in the codebase goes through
`llm/access.py`. It finds the right API key, falls back to the platform's key,
checks credits, trims the request to fit the model, and turns provider errors
into clear ones. Don't call a provider directly from anywhere else.

## Read in this order

1. `providers.py`: the list of supported providers.
2. `models.py`: `AIProvider` and `AIModel`, the model catalogue.
3. `access.py`: `preflight`, `complete`, `stream`. The funnel.
4. `handlers/openai_compatible.py`: how a request actually goes over the wire.

## Providers

`openrouter` (the default), `openai`, `nvidia`, `ollama` (local), and
`opencode` (OpenCode Zen, bring-your-own-key only). The default model is
OpenRouter's free router (`openrouter/free`), so the platform needs
`OPENROUTER_API_KEY` set.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `AIProvider` | A provider row. Table name `nodes_aiprovider` (historical, pinned) |
| `AIModel` | A model in the picker, with context size, price and supported effort levels. Table `nodes_aimodel` |
| `ModelFallback`, `ModelFallbackNotice` | What to use when a model is retired, and the notice shown when that happens |

## Files

| File | What it does |
|---|---|
| `access.py` | **The funnel** for every model call |
| `providers.py` | Supported providers and their labels |
| `budget.py` | How big a request is, and how to cut it down without breaking tool-call pairs |
| `effort.py` | "How hard should the model think" levels (`none` → `high`), mapped to each provider's own setting |
| `credits.py` | The allowance a user spends when using the *platform's* key |
| `pricing.py`, `usage.py` | What a call used and what it cost |
| `fallback.py` | What runs when the chosen model is retired or unknown |
| `catalog_refresh.py` | Refreshing the model list from providers |
| `context.py` | `ExecutionContext`, extra info handed to a provider handler |
| `views.py` | `/api/llm/models/`, what the model picker reads |
| `handlers/base.py` | The interface every provider handler follows |
| `handlers/registry.py` | Finds the handler for a provider name |
| `handlers/openai_compatible.py` | One implementation of the OpenAI chat protocol (used by most providers), including retries on connection failures |
| `handlers/llm_providers.py` | OpenRouter, OpenAI, NVIDIA and OpenCode Zen, declared as settings on top of that one implementation |
| `handlers/llm_nodes.py` | Ollama (local models) |
| `handlers/llm_base.py` | Shared streaming and reasoning-text plumbing |

(`handlers/` came from the deleted `nodes` app. Some names still say "node".)

## Errors

`access.classify_provider_error` sorts failures:

- 401/403 → `LLMAccessDenied` (bad key)
- 402 or "insufficient credit" → `LLMQuotaExhausted`
- 410 or "end of life" → `LLMModelUnavailable`

All three are `LLMUserActionable`: only the user can fix them, so the turn
fails straight away with a clear message instead of showing a spinner.
Anything else is treated as temporary.

## Model ids

Never type a model id from memory. The table of ids the project relies on is
in `CLAUDE.md` ("Model IDs"). Otherwise, look it up in the `AIModel` table and
prefer a row with `is_active=True`.

## Tests

`llm/tests/`: `test_effort.py`, `test_effort_funnel.py`, `test_stream_retry.py`
and others.
