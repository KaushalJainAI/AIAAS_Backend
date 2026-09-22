# OpenCode Zen as a fifth LLM provider — plan

Status: implemented 2026-09-22, except live verification — no Zen key was
available, so chat-completions answers, streaming and `tool_calls` are still
unverified (see §8). Do not mark this fully implemented until §8 is filled
with a key.
Audience: the engineer (or model) implementing it. Every step names the exact
file and the exact change. Do the phases in order; each ends with a check.

---

## 1. The decision, in one paragraph

**Do not build an "OpenCode proxy". Add `opencode` as a normal provider, and
each user connects their own OpenCode Zen API key (bring-your-own-key).**
Zen already speaks the OpenAI chat-completions protocol over plain HTTPS
(`https://opencode.ai/zen/v1/chat/completions`, `Authorization: Bearer <key>`),
which is exactly what `llm/handlers/openai_compatible.py` already implements. A
proxy would add a process, a port and a failure point, and would translate a
protocol into the same protocol.

### Why not one shared key behind a proxy with per-user basic auth

That was the original idea, and it has three problems, the first of which is
disqualifying:

1. **It breaks OpenCode's Terms of Service.** The ToS says: *"You will only use
   the Services for your own internal use, and not on behalf of or for the
   benefit of any third party."* One account's key serving every platform user
   is serving third parties. The key gets banned, and every user loses the
   provider at once.
2. **One account's rate limits become everyone's rate limits.** Free models are
   limited per account; 50 users on one key share one bucket.
3. **Basic auth is not needed.** Our users already authenticate to us with
   JWT. The only credential Zen needs is Zen's own key, and the per-user vault
   (`credentials/`, AES-encrypted) already stores and injects one key per user
   per provider. "Authentication for each user" = each user's own Zen key in
   their own vault. That is the compliant version of the idea.

A proxy that runs `opencode serve` per user is worse still: it is a Bun/Node
process per user on a 913 MB box (see the MCP OOM of 2026-09-16 in `CLAUDE.md`),
and the image no longer installs Node.

### What users get

Free today (live `GET https://opencode.ai/zen/v1/models`, 2026-09-22):

| Zen model id | Notes from Zen's docs |
|---|---|
| `big-pickle` | Stealth model. Prompts may be used to improve the model. |
| `deepseek-v4-flash-free` | DeepSeek **V4** (not V4.1 — see CLAUDE.md "Model IDs"). |
| `mimo-v2.6-flash-free` | Limited-time free, feedback collection. |
| `mimo-v2.5-free` | Limited-time free, feedback collection. |
| `ling-3.0-flash-fin-free` | Limited-time free, feedback collection. |
| `nemotron-3-ultra-free` | NVIDIA trial. **"Do not submit personal or confidential data."** |
| `nemotron-3.5-lightning-free` | NVIDIA trial. Same warning. |
| `muse-spark-1.3-contributor-free` | Meta may train on prompts and completions. Reasoning model — never give a small `max_tokens`. **Not seeded**: docs map it to `/responses`, which this provider does not speak (§8). |

**Excluded on purpose:** `jev-1.13` / `jev-1.13-free` use a different endpoint
(`/zen/v1/systemone`), not chat-completions. Paid Zen models (Claude, GPT,
Gemini…) are out of scope — OpenRouter already reaches them, and
`llm/providers.py` says a new provider must answer a need the others cannot.
The need this one answers is: **free models, on the user's own account.**

**The honest caveat users must see:** every free Zen model retains or trains
on data. Free models must carry a visible "may be used for training — don't
send private data" note in the picker (Phase 3). That matters here more than
elsewhere, because an agent with the `mcp` grant could send the contents of a
user's Gmail to one of these models.

---

## 2. Phase 0 — verify before writing code (15 min)

A keyless request from the dev machine timed out on 2026-09-22, so nothing
below is verified against a live response yet. Get a Zen key
(https://opencode.ai/zen → sign in → API keys; free models need no balance —
confirm this) and run, from `Backend/`:

```bash
export ZEN=<key>
for m in big-pickle deepseek-v4-flash-free mimo-v2.6-flash-free mimo-v2.5-free \
         ling-3.0-flash-fin-free nemotron-3-ultra-free nemotron-3.5-lightning-free \
         muse-spark-1.3-contributor-free; do
  echo "== $m"
  curl -s -m 60 https://opencode.ai/zen/v1/chat/completions \
    -H "Authorization: Bearer $ZEN" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}],\"max_tokens\":800,\"stream\":false}" \
    | head -c 400; echo
done
```

Then repeat **one** model with `"stream": true` and check the body is
`data: {...choices[0].delta...}` lines ending in `data: [DONE]`.

Then one tool-calling request (agents depend on this):

```bash
curl -s -m 60 https://opencode.ai/zen/v1/chat/completions \
  -H "Authorization: Bearer $ZEN" -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"What time is it in Tokyo? Use the tool."}],
       "tools":[{"type":"function","function":{"name":"get_time","description":"Current time","parameters":{"type":"object","properties":{"tz":{"type":"string"}},"required":["tz"]}}}],
       "max_tokens":800}'
```

Record, per model: answers? streams? returns `tool_calls`? accepts
`reasoning_effort` without a 400? Write the results into §8 of this file.
**Only seed models that answered.** A model that does not return `tool_calls`
is still seeded, but gets no `function_calling` capability (see Phase 1.6).

**Stop here and report back if** the endpoint needs anything beyond a Bearer
token, or free models refuse API keys used outside the OpenCode client. The
plan assumes neither.

---

## 3. Phase 1 — backend provider

### 1.1 `llm/providers.py`

- Append `'opencode'` to `SUPPORTED_PROVIDERS` — **last**, because the first
  entry is the default and the default must stay `openrouter`.
- Add `'opencode': 'OpenCode Zen (free models, your key)'` to `PROVIDER_LABELS`.
- In the module docstring's list of four, add a fifth line:
  `opencode    free models on the user's own OpenCode Zen account — bring your own key; never a platform key (ToS: own use only)`.
  Change "Four providers cover…" to "Five providers…".

### 1.2 `llm/handlers/llm_providers.py` — the handler

Add below `NvidiaNode`:

```python
class OpenCodeZenNode(OpenAICompatibleLLMNode):
    """OpenCode Zen — curated models on the user's own Zen account.

    Bring-your-own-key only. OpenCode's ToS limits the service to the key
    holder's own use, so there is deliberately no platform key for this
    provider (see `credentials.resolution.PLATFORM_ENV_KEYS`).
    """

    node_type = "opencode"
    name = "OpenCode Zen"
    description = "Free and paid models through your own OpenCode Zen key"

    provider_slug = "opencode"
    api_label = "OpenCode Zen"
    base_url = "https://opencode.ai/zen/v1"
    default_model = "opencode/big-pickle"
    image_endpoint = None

    #: Catalogue values are stored as `opencode/<zen id>` because
    #: `AIModel.value` is globally unique and bare Zen ids (`gpt-5`, …) would
    #: collide with other providers' rows. Zen wants the bare id on the wire.
    MODEL_PREFIX = "opencode/"

    def chat_payload(self, *, model, messages, config, stream):
        return super().chat_payload(
            model=model.removeprefix(self.MODEL_PREFIX),
            messages=messages, config=config, stream=stream,
        )
```

Before writing it, read `OpenAICompatibleLLMNode.chat_payload`
(`llm/handlers/openai_compatible.py` ~line 268) and confirm `model` is the
wire value there. If the wire model is set somewhere else too (grep the file
for `"model"`), strip the prefix in that place as well — the check in Phase
1.8 will catch a miss (Zen answers 404/400 for `opencode/big-pickle`).

`effort_field` stays the inherited `"reasoning_effort"`. Whether a row gets
effort levels is decided per model in the catalogue (1.6), never here.

### 1.3 `llm/handlers/registry.py`

Next to `registry.register(OllamaNode)` (~line 116) add
`registry.register(OpenCodeZenNode)` and import it with the others. Also add it
wherever `OpenRouterNode` is registered near line 31 **only if** that block is
the same "all providers" list (read it; if it is a special-purpose subset,
leave it alone). Update the docstring on line 10 that names the four.

### 1.4 `credentials/resolution.py` — do **not** add a platform key

Add nothing to `PLATFORM_ENV_KEYS`. Add a comment above it:

```python
# `opencode` is absent on purpose: Zen's ToS limits a key to its holder's own
# use, so a platform key would serve third parties. Users bring their own.
```

With no entry, `platform_api_key('opencode')` returns None and `llm/access.py`
already raises `CredentialUnavailable` → `LLMAccessDenied` at preflight, which
is the "fail before you look busy" behaviour we want: no key = an immediate
error naming the provider, never a spinner.

### 1.5 The credential type

a. `credentials/management/commands/seed_connector_credentials.py`, after the
   NVIDIA entry (~line 405):

```python
{
    "name": "OpenCode Zen", "slug": "opencode", "auth_method": "api_key",
    "description": "OpenCode Zen API key (opencode.ai/zen) — free models run on your own account",
    "icon": "Brain",
    "fields_schema": _api_key("API Key", "sk-..."),
},
```
   (Check the key prefix Zen actually issues in Phase 0 and use it as the
   placeholder.)

b. New data migration `credentials/migrations/0010_opencode_credential_type.py`
   for databases that already ran `0005`. Copy the shape of `0009`: a forward
   function that does `CredentialType.objects.update_or_create(slug='opencode',
   defaults={...})` using the **same dict** imported from the seed command (as
   `0005` does — import inside the function, never at module scope), and a
   reverse that deletes the row by slug. Dependency: `('credentials',
   '0009_slack_user_token')`. Docstring: one paragraph saying why (fresh DBs
   get it from 0005; this is for existing ones).

### 1.6 `populate_models.py` — the catalogue

Add a provider block after the NVIDIA block (~line 413):

```python
{
    "name": "OpenCode Zen",
    "slug": "opencode",
    "description": "Curated models on your own OpenCode Zen account. Free models may be used for training.",
    "icon": "OC",
    "models": [
        m("Big Pickle (Free)", "opencode/big-pickle", True, CHAT_CAPS, context=<from Zen docs>),
        ...
    ],
},
```

Rules for each row:
- `is_free=True`, prices `"0.0000"`.
- `caps`: `CHAT_CAPS` if Phase 0 showed `tool_calls`, otherwise a copy of
  `CHAT_CAPS` with function calling set False (read how `CHAT_CAPS` is defined
  at the top of the file and follow its keys exactly).
- `effort=()` unless Phase 0 showed `reasoning_effort` is accepted. An empty
  tuple is a claim that the model has no effort control, and that is the safe
  claim — sending the field to a model that rejects it is a hard 400.
- `context`: from the Zen docs page for that model; `0` if unknown.
- Only models that passed Phase 0.

Run `python populate_models.py` and check `AIProvider.objects.get(slug='opencode').models.count()`.

### 1.7 `llm/views.py` — the picker's availability rule (important)

Line ~105:

```python
'available': provider_available or (m.is_free and provider_slug != 'ollama'),
```

That rule means "a free cloud model is usable without a key because the
platform key pays for it". For `opencode` there is no platform key, so a free
Zen model would show as available and then fail at preflight — offered but
unrunnable, the exact bug CLAUDE.md warns about. Change it to:

```python
'available': provider_available or (
    m.is_free and platform_api_key(provider_slug) is not None
),
```

This is strictly more correct for every provider: Ollama has no platform key
(so the `!= 'ollama'` special case is subsumed), and any provider whose
platform key is unset stops advertising free models it cannot run. **Check
before changing**: run `pytest llm/tests/test_models_endpoint.py` first, then
after; if a test asserts a free model is available with no platform key set,
that test encoded the bug — set the platform key in that test instead of
weakening the rule.

Also check `llm/access.py` `payer` (~line 551-561): with a user key it
returns `own_key`, which is right. No change expected; read it to confirm.

### 1.8 Check for Phase 1

```bash
cd Backend
python manage.py migrate
python populate_models.py
pytest llm/ credentials/ -q
```
Then in the Django shell with a real key saved in your own vault as an
`opencode` credential, make one real call through the funnel (use the same
function the chat turn uses in `llm/access.py` — `complete()`), with
`provider='opencode'`, `model='opencode/big-pickle'`. It must return text.

---

## 4. Phase 2 — tests (write these, they are the spec)

Put them in `llm/tests/test_opencode_provider.py`. Use no network: patch the
HTTP call the same way `llm/tests/test_stream_retry.py` does (read it first
and copy its fixture).

1. `test_opencode_is_supported_and_not_default` — `'opencode' in
   SUPPORTED_PROVIDERS` and `SUPPORTED_PROVIDERS[0] == 'openrouter'`.
2. `test_registry_resolves_opencode` — `get_registry()` returns
   `OpenCodeZenNode` for `'opencode'`.
3. `test_prefix_is_stripped_on_the_wire` — build a payload for
   `opencode/big-pickle`; `payload["model"] == "big-pickle"`.
4. `test_no_platform_key_ever` — even with `OPENCODE_API_KEY` set in the
   environment (`patch.dict(os.environ, ...)`), `platform_api_key('opencode')
   is None`. This pins the ToS decision so nobody "helpfully" adds one.
5. `test_free_model_unavailable_without_user_key` — hit `/api/llm/models/` as
   a user with no opencode credential; every opencode model has
   `available: False`.
6. `test_free_model_available_with_user_key` — same, with a verified
   `opencode` credential; `available: True`.
7. `test_missing_key_fails_at_preflight` — a chat turn on an opencode model
   with no key raises the account error before any status event (copy the
   pattern from `chat/tests/test_account_errors.py`).
8. `test_credential_type_seeded_by_migrations` — add a case to
   `mcp_integration/tests/test_fresh_install.py` style: after migrations only,
   `CredentialType.objects.filter(slug='opencode').exists()`.

Also update the existing pinned tests if they enumerate providers:
`llm/tests/test_registry.py` (~line 63) and
`llm/tests/test_models_endpoint.py` (~line 90) iterate `SUPPORTED_PROVIDERS`,
so they should pass unchanged — if they fail, the handler or registry step is
incomplete, not the test.

Full backend suite at the end: `pytest -q` (see memory note: `orchestrator/`
failures about missing whitenoise are environmental, not regressions).

---

## 5. Phase 3 — frontend (`better-n8n-frontend/`)

1. `src/components/mcp/MCPServerModal.tsx` line ~32: add `'opencode'` to
   `LLM_PROVIDER_SLUGS`, or the Zen key will show up as an MCP credential.
2. The model picker (`src/components/chat/StandaloneChat.tsx` ~line 3365 dims a
   provider with no credentials). For `opencode` with no credential, the dimmed
   row should say **"Add your OpenCode Zen key"** and link to the credentials
   page, pre-selecting the `opencode` type if that page supports it. Find how
   another provider's "connect" hint is rendered and reuse it — do not invent a
   new component.
3. Training-data notice: for any model whose provider is `opencode` and
   `is_free` is true, show a small muted line under the model name: *"Free on
   your OpenCode account — prompts may be used for training. Avoid private
   data."* Use the model's `description` field from the API as the source
   (set that text in `populate_models.py` per row, 1.6) rather than
   hard-coding provider logic in the component — CLAUDE.md: presentation
   comes from the backend.
4. Do **not** change `DEFAULT_PROVIDER` / `DEFAULT_MODEL` / `GUEST_PROVIDER`.
   A BYOK provider can never be a default, because a new user has no key.

Check: `npm run build`, `npm run lint` (baseline is zero problems — any new
one is a regression), `npx vitest run`, and `tsc -b --force` (plain
`tsc --noEmit` checks nothing here).

---

## 6. Phase 4 — docs

- `CLAUDE.md`, "AI provider layer" section: mention `opencode` in the
  handler list, and add one sentence: "`opencode` (OpenCode Zen) is
  bring-your-own-key only and has no platform key by design — Zen's ToS limits
  a key to its holder's own use." Add this file to "Internal Documentation".
- `Backend/docs/API.md`: no route changes, so no row changes. Only the
  `/api/llm/models/` row's notes if it describes the `available` rule (1.7).
- Set this file's status line to "implemented <date>" and fill §8.

---

## 7. Out of scope, and why

| Idea | Why not now |
|---|---|
| Shared platform Zen key | ToS (§1). Test 4 forbids it. |
| Self-hosted proxy / `opencode serve` | Same protocol in and out; a process per user on a 913 MB box. |
| Paid Zen models | OpenRouter already reaches them. Can be added later as catalogue rows only — no code — if a user asks. |
| `jev-*` models | Different endpoint (`/systemone`); needs its own transport. |
| Anthropic `/messages` or OpenAI `/responses` endpoints | Only needed for paid Claude/GPT rows; chat-completions covers every free model. |
| Making a Zen model the eval judge or benchmark model | Those ids are pinned in CLAUDE.md "Model IDs" and a test; changing them is the user's decision. |
| Guest (logged-out) chat on Zen | Guests have no vault, so this would need a platform key. See row 1. |

---

## 8. Phase 0 results (fill in)

Keyless `GET https://opencode.ai/zen/v1/models` answers (verified 2026-09-22,
no key needed): every id below is present, plus `muse-spark-1.2-contributor-free`
(not seeded — not in this table). The listing carries no context windows, so all
rows seed `context=0`. Everything else in this table still needs a Zen key.

| Zen id | Answers | Streams | tool_calls | reasoning_effort OK | Context | Seeded? |
|---|---|---|---|---|---|---|
| big-pickle | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| deepseek-v4-flash-free | | | | | 0 (unknown) | yes (endpoint unstated in docs; paid sibling is `chat/completions` — confirm first) |
| mimo-v2.6-flash-free | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| mimo-v2.5-free | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| ling-3.0-flash-fin-free | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| nemotron-3-ultra-free | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| nemotron-3.5-lightning-free | | | | | 0 (unknown) | yes (`chat/completions` per docs) |
| muse-spark-1.3-contributor-free | — | — | — | — | — | **no**: docs map it to `/responses` (Responses API), which this provider does not speak. Needs its own transport before it can be offered. |

---

## 9. Done means

- [x] A user with no Zen key sees Zen models greyed with "Add your OpenCode Zen key".
- [ ] After adding a key, a chat turn on `opencode/big-pickle` streams an answer. (needs key)
- [ ] An agent on a Zen model with `function_calling` completes one tool call. (needs key)
- [x] No key → immediate error naming OpenCode Zen, no spinner, no assistant-voice apology.
- [x] `pytest -q`, `npm run build`, `npm run lint`, `npx vitest run` all green.
- [x] `platform_api_key('opencode')` is None with `OPENCODE_API_KEY` set (test 4).
