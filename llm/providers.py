"""
Which LLM providers this platform supports.

The list used to exist in six places — `chat.llm.PROVIDER_NODE_TYPES`, the
handler registry, `orchestrator.Workflow.llm_provider`, a slug map in the
since-deleted `canvas_agent` app, the seeded credential types, and a hardcoded
array in the frontend's `llm-config.ts`. They had drifted: `anthropic` and
`deepseek` were
routed and offered in the UI with no handler behind them, so choosing either
raised `LLMUnavailable` at request time, and `cohere`/`groq`/`mistral` had
credential types nobody could spend.

Four providers cover essentially every user, because they are chosen for
non-overlapping reasons rather than for breadth:

  openrouter  the default — one key reaching 400+ models across 70+ upstream
              providers, which is what makes Claude, Gemini, Grok, DeepSeek,
              Llama, Qwen and Mistral reachable without a handler each
  nvidia      the platform-managed free key, so the product works before the
              user has configured anything
  openai      the API key a user is most likely to already hold
  ollama      local inference, for offline and air-gapped use

Adding a fifth is deliberately a small change — a slug here, a subclass in
`llm_providers.py` — but it should answer a need the four above cannot, not
add another route to a model OpenRouter already serves.

Credential resolution for these slugs lives in `credentials.resolution`; this
module only answers *which* providers exist.
"""
from __future__ import annotations

#: Supported provider slugs, in the order the UI should present them.
#: Ordering is load-bearing: the first entry is the default offered to a user
#: who has expressed no preference.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    'openrouter',
    'nvidia',
    'openai',
    'ollama',
)

#: Providers that were offered previously, mapped to the OpenRouter namespace
#: prefix that now serves their models. Consumed by the data migration and by
#: `legacy_model_id` below; kept after the migration has run because workflow
#: JSON and API payloads can still carry the old slugs.
RETIRED_PROVIDERS: dict[str, str] = {
    'anthropic': 'anthropic/',
    'deepseek': 'deepseek/',
    'gemini': 'google/',
    'perplexity': 'perplexity/',
    'xai': 'x-ai/',
    'huggingface': '',   # ids are already `org/model`
}

#: The provider a retired slug resolves to.
REPLACEMENT_PROVIDER = 'openrouter'

#: Display names. Presentation only — never routing. The handler classes carry
#: their own `name`/`icon` for the node palette; these label the *provider*
#: choice in settings and agent config, where no handler is in scope.
PROVIDER_LABELS: dict[str, str] = {
    'openrouter': 'OpenRouter (400+ models)',
    'nvidia': 'NVIDIA NIM',
    'openai': 'OpenAI',
    'ollama': 'Ollama (Local)',
}


def provider_choices() -> list[tuple[str, str]]:
    """Django `choices` for a provider field, in presentation order."""
    return [(slug, PROVIDER_LABELS.get(slug, slug)) for slug in SUPPORTED_PROVIDERS]


def is_supported(provider: str) -> bool:
    """True if `provider` is a slug this platform still routes."""
    return provider in SUPPORTED_PROVIDERS


def legacy_model_id(provider: str, model: str) -> str:
    """
    Rewrite a retired provider's model id into its OpenRouter equivalent.

    Returns `model` unchanged when the provider was never retired, or when the
    id is already namespaced — some rows were written with OpenRouter-style ids
    while the provider column still said `gemini`, and prefixing those a second
    time would produce `google/google/gemini-...`.
    """
    prefix = RETIRED_PROVIDERS.get(provider)
    if prefix is None or not model or '/' in model:
        return model
    return f"{prefix}{model}"
