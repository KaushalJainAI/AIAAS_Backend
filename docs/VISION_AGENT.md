# Vision Agent — giving text-only models a witness

**Status: Phase 1 implemented (2026-08-15).** The witness, the tool, the
cross-check and both seams are live for chat; Phase 2 (rasterised documents) and
Phase 3 (workflow nodes) is not. The NVIDIA NIM findings in
[Verified behaviour](#verified-behaviour-nvidia-nim) were measured against the
live API on 2026-08-13 and *are* real. What shipped, and where:

| Piece | Lives in |
|---|---|
| Witness loop, budget, disagreement check | `chat/vision/agent.py` |
| Model + key resolution, the 404 fallthrough chain | `chat/vision/resolve.py` |
| `nemotron-parse` adapter (image-only content, tool-call reply) | `chat/vision/nim.py` |
| Anti-confabulation witness prompt | `chat/vision/prompts.py` |
| `ask_vision` spec, ownership check, dispatch | `chat/tools/vision.py` |
| Offered only when a witness resolves | `requires="vision"`, resolved in `chat/tools/__init__.py` |
| Pointer instead of a dead end | `chat/turn/history.py:describe_for_model`, `chat/turn/agent.py:_describe_attachment_for_text_model` |
| Main-agent discipline (rule 7, BORROWED SIGHT) | `chat/turn/prompts.py` |
| Per-user model choice | `UserProfile.vision_provider` / `.vision_model` |
| Transcript, audit trail | `chat.VisionExchange` |
| Tests | `chat/tests/test_vision.py` (39) |

Three decisions were made during implementation that the plan left open:

- **The per-turn cap needed a turn identity.** `session_id` spans the whole
  conversation and `thread_id` is invisible from inside a tool, so `TurnContext`
  now carries a generated `turn_id` and passes it in the tool context. Without it
  the six-question cap had nothing to count against.
- **`readings_diverge` ignores numbers glued to letters.** "Q3" contributed a `3`
  in the first cut, so a witness that named the quarter it was asked about
  disagreed with every correct parse — the uncertainty warning fired on right
  answers, which is how a signal gets learned-past and ignored.
- **The mime-type defect in "Adjacent defects" was fixed rather than noted**, as
  `encode_image_attachments` is now on the witness's own path. `image_mime_for`
  is shared by both callers so the two cannot label the same file differently.

---

## The problem

A text-only model cannot read an image, and the platform currently handles that
by giving up in two different places:

| Location | Current behaviour |
|---|---|
| `chat/turn/agent.py:207` `_describe_attachment_for_text_model` | Emits *"This model has no visual input, so you cannot see this file. Say so rather than guessing."* |
| `chat/turn/history.py:227` `partition_attachments` | Blocks the attachment and tells the user *"Switch to a multimodal model."* |

Both are honest, and both push the work onto the user. The upload is ignored by a
model the user already chose for good reasons — cost, latency, tool quality, or
which credential they happen to hold.

## The idea

Do not describe the image *at* the main agent. Give the main agent **a witness it
can interrogate.**

`ask_vision` is a tool whose implementation is itself an agent: a cheap
NVIDIA NIM vision model that holds the image in its own context and answers
questions about it in natural language. The main agent asks; the witness answers;
the main agent asks a follow-up when the answer is thin.

```
main agent (text-only, expensive, good at reasoning)
    |
    |  ask_vision(attachment_id, "what are the four bar values?")
    v
vision agent  —  nvidia/nemotron-nano-12b-v2-vl  (~2s, ~800 prompt tokens)
    |          + nvidia/nemotron-parse cross-check when glyphs matter
    |
    |  "Q1 4.2, Q2 5.1, Q3 4.8, Q4 6.3 — USD millions per the y-axis label."
    v
main agent reasons over the testimony
```

A caption is written before anyone knows what will be asked; a witness is queried
after. When the user's real question turns out to be "is the trend line above or
below the bars in Q3", a generic caption has already thrown that detail away
while the witness still has the pixels.

### Why it is an agent and not a function

The vision agent keeps a per-attachment transcript, so the second question
arrives with the first exchange already in context:

- follow-ups are cheap (the image is already in its window) and coherent
- it can refer back: *"the same bar I described as Q3"*
- the main agent can cross-examine

---

## Verified behaviour (NVIDIA NIM)

Measured directly against `https://integrate.api.nvidia.com/v1` with the
platform key on 2026-08-13. These are the facts the design rests on.

**NIM is already wired into this platform.** `nvidia` is in
`nodes/providers.py:SUPPORTED_PROVIDERS`, `NvidiaNode`
(`llm/handlers/llm_providers.py:109`) speaks the OpenAI protocol at the NIM
base URL, `settings.NVIDIA_API_KEY` already backs guest chat (`chat/guest/runtime.py`),
and `populate_models.py:186` already seeds a vision model. Almost nothing new is
needed to reach it.

### Candidate witnesses — all called with a real chart image

| Model | Read the chart | Latency (warm) | Prompt tokens | Notes |
|---|---|---|---|---|
| `nvidia/nemotron-nano-12b-v2-vl` | correct | ~1.8–2.0s | ~805 | **Recommended default.** Already seeded with `VISION_CAPS`. Natural, witness-like prose |
| `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` | correct | ~1.8s | ~810 | Cheapest tier. Extremely terse ("4.8, USD Millions") |
| `meta/llama-3.2-11b-vision-instruct` | correct | ~1.8s | ~1632 | 2× the image tokens for no accuracy gain here |
| `nvidia/nemotron-parse` | correct (small image) | ~1s | — | OCR/layout specialist. See below |
| `google/gemma-3-4b-it`, `microsoft/phi-3-vision-128k-instruct`, `nvidia/vila`, `nvidia/neva-22b`, `google/deplot` | — | — | — | **HTTP 404 for this account** |

Three findings that change the design:

**1. The `/v1/models` catalog is not an entitlement list.** Five of the models
listed for this key returned `404 Function ... not found for account`. Model
resolution must therefore treat 404 as "fall through to the next candidate", and
the settings UI must offer only models proven callable, not the raw catalog.

**2. OpenAI-style content parts work.** NIM accepted
`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`
directly, so `llm/handlers/openai_compatible.py:76`
`encode_image_attachments` works as-is with no NIM-specific message shape.

**3. No inline size ceiling was reached.** Base64 payloads up to ~6 MB were
accepted without a 413 or an asset-reference error, so v1 needs no NVCF asset
upload path.

### The accuracy failure that matters

On a chart whose labels were tiny relative to the canvas (11px text on a
2520×1320 image), `nemotron-nano-12b-v2-vl` read `4.8` as **`48`** — dropping the
decimal point. This deserves its own paragraph because of *how* it fails:

- **Silently.** No hedging, no uncertainty; the same confident sentence as a
  correct read.
- **Plausibly.** `48` is a number a reader accepts. It is not garbage output.
- **Deterministically.** Three trials at `temperature=0` returned the identical
  wrong answer, so **self-consistency retries cannot catch it.**
- **Not fixed by zooming.** Cropping to the region and upscaling 2× still
  returned `48`. Interpolation does not restore stroke detail already lost.
- **Not specific to the VLM.** `nemotron-parse` failed the same image
  differently, returning `Revenue (USD/MN) 8.2`.

On the realistically-proportioned version of the same chart, both models were
correct on every trial. The pathological case is a synthetic extreme, but
low-resolution screenshots and photographed documents are exactly where real
users live.

**The mitigation that follows from this** is not retrying and not zooming, but
**disagreement as the uncertainty signal**: ask the VLM *and* `nemotron-parse`,
and when their readings of a number or string diverge, say so to the main agent
instead of picking one. In the failing case the two disagreed (`48` vs `8.2`); in
the passing case they agreed (`4.8`, `4.8`). Two cheap models disagreeing is the
only doubt signal available, because neither model volunteers doubt on its own.

`nemotron-parse` returns its result as a **tool call**, not message content:
`message.content` is `null` and `message.tool_calls[0].function.arguments`
carries a `markdown_bbox` JSON array of `{bbox, text, type}` regions. It also
rejects plain-string content with *"The model does not support text input"* — it
takes an image-only content array. Both quirks need handling in its adapter.

---

## Design

### Placement

Scope for v1 is **chat only, images and documents**, so the code lives under
`chat/`, kept provider-agnostic enough to lift into a shared `perception/` app
when workflow nodes want it (Phase 3).

```
chat/vision/
    __init__.py
    agent.py      # the witness loop: ask(attachment, question) -> answer
    nim.py        # NIM adapter: witness call + nemotron-parse cross-check
    prompts.py    # witness system prompt — the anti-confabulation discipline
    resolve.py    # which model does the seeing, on whose key
```

### Model selection — NVIDIA NIM by default, configurable per user

Add two fields to `core.models.UserProfile`:

```python
vision_provider = models.CharField(max_length=50, blank=True, default="nvidia")
vision_model    = models.CharField(max_length=120, blank=True,
                                   default="nvidia/nemotron-nano-12b-v2-vl")
```

`UserProfile` is already served by `UserProfileView` (`core/views.py:280`, a
`RetrieveUpdateAPIView`) through `UserProfileSerializer`, so adding the fields to
the serializer gives the settings screen a working read/write endpoint with no
new route. Update the matching row in `docs/API.md`.

`chat/vision/resolve.py` resolves in this order:

1. the user's configured `vision_provider` / `vision_model`
2. **`nvidia/nemotron-nano-12b-v2-vl` on NIM** — the platform default
3. `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` — cheaper fallback, also a 404
   fallback given finding (1)
4. nothing callable → the tool is not offered, and today's "switch model"
   message stands

The key comes from `credentials.resolution.resolve_api_key` (user NVIDIA
credential first, `settings.NVIDIA_API_KEY` as the platform fallback) — the same
path guest chat already uses, so the feature works before a user has configured
anything.

Two NIM defaults need seeding in `populate_models.py`:
`nvidia/llama-3.1-nemotron-nano-vl-8b-v1` with `VISION_CAPS`, and
`nvidia/nemotron-parse` as a parse-only entry.

### The tool

Registered in `chat/tools/vision.py`, mirroring `read_attachment_text` in
`chat/tools/conversation.py` — including the session-ownership check, the only
thing standing between a crafted `attachment_id` and another user's files.

```python
{
    "type": "function",
    "function": {
        "name": "ask_vision",
        "description": (
            "Ask a vision-capable assistant a question about an image or "
            "visual document you cannot see yourself. It has the file open and "
            "remembers your earlier questions about it. Ask specific questions; "
            "ask follow-ups when an answer is vague or you need a detail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["attachment_id", "question"],
            "additionalProperties": False,
        },
    },
}
```

No dispatch entry to add: `@tool(...)` registers the schema and the function
together, so there is no second list that can fall out of step.

### The witness loop (`chat/vision/agent.py`)

```python
async def ask(attachment, question, *, session_id, user_id) -> str
```

1. resolve the vision model and key (`resolve.py`)
2. load this (session, attachment) transcript from `VisionExchange`
3. call `chat.llm.complete(provider="nvidia", model=..., prompt=question,
   system_message=WITNESS, history=prior, attachments=[attachment])` — no new
   provider plumbing
4. when the question asks for a number, a label, or verbatim text, also run
   `nemotron-parse` and **append a disagreement note if the readings diverge**
5. persist the exchange, return the answer

Bounds, all of which matter:

- **max ~6 questions per attachment per turn.** A main agent stuck on an
  ambiguous image will otherwise interrogate it forever at the user's expense.
- **transcript trimmed to the last few exchanges.**
- **timeout, and failures return an error string** — never raise into the main
  turn. A dead witness degrades to "I could not examine that file", not a 500.
- **retry with backoff on 4xx/5xx.** One probe run hit sustained HTTP 400s that
  did not reproduce afterwards, consistent with transient throttling under
  back-to-back calls. Log the response body so the real cause is visible next
  time.
- **no tools for the witness.** It looks and answers. Nothing else.

### The witness prompt (`chat/vision/prompts.py`)

The measured failure above is the thing this prompt exists to fight:

- answer only what is visible; "I cannot tell from this image" is a correct and
  expected answer
- separate what is read verbatim (labels, numbers, text) from what is inferred
- **flag small, blurred, or low-resolution text explicitly rather than reading
  through it** — this is where decimal points die
- name occlusions and ambiguity
- no speculation about intent or context outside the frame

### Persistence — `chat/models.py`

```python
class VisionExchange(models.Model):
    session    = FK(ChatSession)
    attachment = FK(ChatAttachment, related_name="vision_exchanges")
    question   = TextField()
    answer     = TextField()
    model      = CharField()
    created_at = DateTimeField(auto_now_add=True)
```

Three jobs at once: the witness's memory, an audit trail of what was asked on the
user's behalf, and the data behind a UI affordance — *"the assistant asked its
vision model 3 questions about this image"*, expandable.

### Telling the main agent the witness exists

`chat/turn/history.py:227` `partition_attachments` currently blocks non-ingestible
files. It changes to: still keep the image off the wire (a text-only model must
not receive it), but emit a pointer instead of a dead end —

```
[Image "q3-chart.png" (id 4f2a...) is attached. You cannot see it. Call
 ask_vision with that id to question an assistant that can.]
```

`chat/turn/agent.py:207` `_describe_attachment_for_text_model` becomes that pointer.
`describe_blocked` (`chat/turn/history.py:277`) keeps its user-facing message only for
files nothing can read.

`chat/turn/prompts.py` gains the reasoning discipline for the main agent: the answer
is **testimony from another model**, not firsthand sight. Do not tell the user
"I can see that...". Ask a follow-up rather than filling a gap by inference. Pass
on the witness's uncertainty instead of laundering it into a clean number.

### Documents

Text-bearing documents (pdf, docx, csv, json, html) already extract via
`inference/utils.py:54` and inline as text. That path does not change.

The gap is documents where extraction returns almost nothing: scanned PDFs and
chart/diagram pages. `nvidia/nemotron-parse` is the right tool there — it returns
markdown plus bounding boxes — but reaching it still requires rasterizing PDF
pages, and the current `pypdf` extracts text without rendering. **That dependency
decision (`pymupdf` or `pdf2image` + poppler) is Phase 2**, deliberately
separated so Phase 1 does not block on it. Image attachments need no rasterizer
and are fully covered in Phase 1.

### UI

Tool calls already stream to the frontend, so `ask_vision` renders as a tool call
for free — no new event type, no `lib/websocket.ts` work. Show the question and
answer rather than a spinner: watching the agent ask its own eyes *"what is the
value of the third bar?"* is legible in a way "calling ask_vision..." is not.

---

## Phases

**Phase 1 — images, chat, end to end, on NIM.** `UserProfile` fields +
serializer + `docs/API.md` row; seed the two extra NIM models; `VisionExchange`
model and migration; `chat/vision/` including the parse cross-check; the tool
spec and dispatch; the two seam changes in `history.py` and `agent.py`;
main-agent prompt discipline.

**Phase 2 — visual documents.** Rasterizer dependency; scanned-PDF and
chart-page routing into `nemotron-parse` plus the witness.

**Phase 3 — lift and reuse.** Move `chat/vision/` to a shared `perception/`
package; add a workflow node so text-only LLM nodes stop silently dropping media
(`nodes/handlers/`, registry, `lib/nodeConfigs.ts`).

---

## Known risks

1. **Silent misreads of fine detail.** Measured, deterministic, and plausible —
   see [Verified behaviour](#verified-behaviour-nvidia-nim). Mitigated by the
   VLM/parser disagreement check and by a witness prompt that must flag
   low-resolution text. Not fully solvable at this price point; the honest
   posture is to surface doubt, not to claim precision.
2. **The main agent claims it saw the image.** Users catch this and lose trust in
   every other answer. Fought in the answer framing and the main system prompt.
3. **Models without tool-calling get nothing.** `AIModel.supports_tool_calling`
   exists; when false the tool cannot be offered. Still open — those users keep
   today's "switch model" message, since an automatic description at attach time
   is the caption design this whole document argues against. Note `NvidiaNode` sets `include_tools=False`
   (`llm_providers.py:130`, *"NIM exposes no tool-calling surface"*) — so a user
   whose **main** agent is on NIM may not be able to call `ask_vision` at all.
   Worth re-testing against current NIM before Phase 1, since that comment
   predates the models measured above.
4. **Invisible cost.** A text-only model now silently spends NIM tokens
   (~800–1600 prompt tokens per look). The UI must attribute it; the per-turn
   question cap bounds it.
5. ~~**`chat/tests/test_rework.py:134-144` assert the current "Switch to a
   multimodal model" message.**~~ Resolved: the original assertion was kept for
   the no-witness case (still the right message when nothing can look) and a
   second test asserts the inversion when a witness exists.
6. **Path traversal.** The witness reads files by path; reuse
   `validate_attachment_path` (`llm/handlers/openai_compatible.py:60`).

## Adjacent defects found while probing

Not blockers, but they touch this code path:

- `llm/handlers/openai_compatible.py:114` hardcodes
  `data:image/jpeg;base64,...` for **every** attachment regardless of real type.
  NIM tolerated PNG bytes mislabelled as JPEG (verified), so this is latent
  rather than breaking — but the witness depends on this encoder and stricter
  providers will not be so forgiving.
- `llm/handlers/llm_providers.py:122` comments *"NIM serves text models only"*
  and sets `image_endpoint = None`. The first half is now false: NIM serves
  several VLMs, as measured above.
- A live `NVIDIA_API_KEY` is committed in `Backend/.env`, `.env.local`, and
  `.env.deployment`. `.env.deployment` in particular is the kind of file that
  travels. Worth rotating and moving to a secret store.
