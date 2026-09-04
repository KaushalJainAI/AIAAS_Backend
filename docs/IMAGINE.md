# Imagine — Media Generation (Image / Video / Audio)

The `imagine` Django app routes user prompts to OpenRouter for image, video, and audio generation. It exposes both a form-based REST surface and a conversational agent flow with HITL approval.

---

## Architecture

```
┌────────────┐    POST /imagine/                  ┌──────────────────────┐
│ Frontend   │ ─────────────────────────────────► │ ImagineViewSet       │ (form mode)
│            │    POST /imagine/agent/chat/       │ ImagineAgentChatView │ (NL mode)
└────────────┘                                    └──────────┬───────────┘
                                                             │
                                              ┌──────────────▼────────────┐
                                              │ agent/graph.run_turn       │
                                              │  └ agent/intent.classify  │
                                              │  └ services/dispatcher    │
                                              └──────────────┬────────────┘
                                                             │
                                              ┌──────────────▼────────────┐
                                              │ OpenRouterService          │
                                              │  .for_user(user) ──────────┼─► credentials vault
                                              │  .generate_image / video   │
                                              │  .generate_audio           │
                                              │  .poll_video_status        │
                                              └──────────────┬────────────┘
                                                             │
                                                ┌────────────▼────────────┐
                                                │ openrouter.ai/api/v1     │
                                                └─────────────────────────┘
```

Video jobs (async) are tracked by `poll_video_generation` Celery task which polls `GET /videos/{id}` until terminal state.

Image generation is async only when `RUN_WORKFLOWS_ASYNC` is on: the row is
left `pending` and `generate_image_task` performs the call on the worker,
mirroring the video path. Local dev and tests run without Redis, so they stay
inline — the same split `inference.dispatch_extraction` uses. If the broker is
unreachable at enqueue time the call falls back to inline rather than leaving a
`pending` row that can never complete. Audio (TTS) is always inline.

Frontend status updates are pushed via Channels group `imagine_agent_{user_id}` with event types `generation.started | progress | completed | failed`. All broadcasts go through `services/events.py` — the single place the group name and frame shape live.

---

## Credentials

**OpenRouter API keys never live in `settings` or `.env`** — they're stored per-user in the encrypted `credentials/` vault (Fernet/AES at rest, same path as every other integration).

- CredentialType: `slug='openrouter'`, `name='OpenRouter API'`, `auth_method='api_key'`, field name `apiKey` (seeded by `populate_credentials.py`).
- Lookup: `OpenRouterService.for_user(user)` resolves the user's most-recently-updated active credential, decrypts it, and returns an instance bound to that key. Falls back to `api_key` / `token` field names for older entries.
- Missing-credential path: raises `MissingOpenRouterCredentialError`. Surfaces as:
  - **`GET /imagine/capabilities/`** → HTTP 400 with `detail` message and empty modality lists.
  - **`POST /imagine/`** → HTTP 400 with `detail` (preflighted in `perform_create`, so no row is created). The old behaviour returned a 201 with a row already marked `failed` — a configuration problem dressed as a generation.
  - **`run_generation`** (agent flow, which cannot preflight before it has classified) → `Generation.status='failed'`, `error_message` set, and a `generation.failed` WS event.
  - **`classify`** → intent with `confidence=0.0`, `missing_required=['credential']`, and the message as `clarifying_question`.
  - **`poll_video_generation`** → marks the in-flight job failed and broadcasts.

To onboard: in the app, **Credentials → New → "OpenRouter API" → paste key into `apiKey`**. No restart required; the dispatcher resolves it on the next request.

---

## OpenRouter API surface used

All calls go to base URL `https://openrouter.ai/api/v1` with headers `Authorization: Bearer <key>`, `HTTP-Referer: https://better-n8n.com`, `X-Title: Better n8n Imagine`.

### Image — `POST /images`

> **Migrated from `POST /chat/completions` + `modalities: ["image","text"]`.**
> That path still works but only reaches the ~10 image models that are *also*
> chat models (Gemini, GPT Image). FLUX, Seedream, Recraft, Krea, Qwen Image,
> MAI-Image and Riverflow are not chat models and are addressable **only**
> through `/images` — roughly two thirds of the catalog was unreachable. The
> unified endpoint also names its fields properly (`resolution`, not
> `image_config.image_size`).

**Request** — only keys the user actually set are sent, so each model's own
defaults apply. This matters: models disagree about which resolutions they
accept (Seedream 5.0 Lite is 2K/4K-only), so there is no safe global default.

```json
{
  "model": "black-forest-labs/flux.2-pro",
  "prompt": "<prompt>",
  "resolution":    "512 | 1K | 2K | 4K",       // optional
  "aspect_ratio":  "1:1 | 16:9 | 9:16 | ...",  // optional
  "quality":       "auto | low | medium | high",
  "output_format": "png | jpeg | webp | svg",
  "seed":          12345
}
```

A negative prompt has no first-class field here; it is appended to the prompt
as `Avoid the following: …`, which is how the chat path expressed it too.

**Response** — `data[0].b64_json` + `data[0].media_type`, assembled into a data
URL (`data:image/png;base64,…`). A provider that returns `data[0].url` instead
is also accepted. `usage.cost` is recorded on `Generation.metadata.cost_usd`.

### Video — `POST /videos` + `GET /videos/{id}`

Video is async — submit returns a job, poll for completion.

**Submit request**
```json
{
  "model": "<video model id>",
  "prompt": "<prompt>",
  "resolution":   "480p | 720p | 1080p | 1K | 2K | 4K",
  "aspect_ratio": "16:9 | 9:16 | 1:1 | 4:3 | 3:4 | 3:2 | 2:3 | 21:9 | 9:21",
  "duration":     5,
  "seed":         12345         // optional
}
```

**Submit response** — `{ "id", "polling_url", "status": "pending|in_progress|..." }`. We persist `id → Generation.job_id` and `polling_url → Generation.polling_url`, then enqueue `poll_video_generation.delay(generation.id)`.

**Poll response status enum** (mapped by `OpenRouterService.poll_video_status`):

| OpenRouter status | Internal action |
|---|---|
| `completed` | Read `unsigned_urls[0]` → `Generation.output_url`. Broadcast `generation.completed`. |
| `failed`, `cancelled`, `expired` | Terminal failure. Set `Generation.error_message`. |
| `pending`, `in_progress` | Retry in 30s (Celery `self.retry(countdown=30)`, max 20 retries). |
| unknown / missing | Treated as pending — keep polling rather than mis-failing a live job. |

### Audio (TTS) — `POST /audio/speech`

**Request**
```json
{
  "model": "<tts model id>",
  "input": "<text>",
  "voice": "<provider-specific — omitted entirely when the user picks none>",
  "response_format": "mp3",   // we always set mp3; OpenRouter default is pcm
  "speed": 1.0                 // optional
}
```

**Response** — raw audio bytes (`Content-Type: audio/mpeg`). The service base64-encodes them into a data URL (`data:audio/mpeg;base64,…`) so the frontend can play them in a standard `<audio>` element without a second fetch.

Voice ids are **provider-specific**: `alloy` is an OpenAI name and is rejected
by MiniMax, Voxtral and Kokoro. The service sends `voice` only when the caller
chose one, and `catalog.TTS_MODELS` carries the per-model voice list the UI
renders. A model with an empty `voices` list gets a free-form text field.

### Model catalog — `GET /images/models`, `GET /videos/models`, curated TTS

Three modalities, three discovery stories:

| Modality | Source | Per-model metadata |
|---|---|---|
| Image | `GET /api/v1/images/models` (~43 models) | `supported_parameters` as typed specs — `resolution`/`aspect_ratio`/`quality` enums, `n` range, `seed` bool |
| Video | `GET /api/v1/videos/models` (~23 models) | `supported_resolutions`, `supported_aspect_ratios`, `supported_durations`, `generate_audio`, `seed` |
| Audio | **curated** in `services/catalog.TTS_MODELS` | voices, `supports_speed` |

Audio is hand-maintained because OpenRouter exposes **no TTS discovery
endpoint** — `/audio/models`, `/speech/models` and `/audio/voices` all 404, and
the speech models (`minimax/speech-2.8-hd`, `hexgrad/kokoro-82m`,
`microsoft/mai-voice-2`, …) are absent from `/api/v1/models` entirely. Voices
are not exposed by any API either. `TTS_MODELS` is the file to edit when a new
voice model ships.

>  **Prior bug.** `fetch_models()` read `output_modalities` from the *top
> level* of `/api/v1/models`, where the key does not exist — it lives under
> `architecture.output_modalities`. All three buckets returned `[]` for every
> user, so no model could be selected anywhere in the UI and the agent always
> fell into a HITL clarifying question it could never satisfy. Guarded now by
> `tests_catalog.test_buckets_are_never_empty`.

`services/catalog.py` normalizes each source into one shape and derives
`provider` from the id prefix. `services/capabilities.capabilities_for(user)`
is the single cached accessor (key `openrouter_capabilities_v2`, 1 hour) shared
by the view, the intent classifier and the serializer validator — an **empty**
catalog is deliberately never cached, so an OpenRouter blip does not blank the
picker for an hour.

`RECOMMENDED` in the same module decides which models the picker pins above the
search results and which one a modality opens on; unlisted models keep the
catalog's own newest-first order, so a brand-new model still surfaces near the
top.

---

## Cost protection

Every endpoint that spends money — `POST /imagine/` (create), `POST
`/imagine/agent/chat/`, `POST /imagine/agent/resume/` — carries
`ImagineGenerateThrottle` (scope `imagine_generate`, `30/hour` per user from
`settings/base.py`) in addition to the global user throttle. The global
`UserRateThrottle` (1000/hour) protects the API as a whole; it is not a cost
guard.

The HITL gate never trusts the router's `estimated_cost_usd` alone: each
modality has a floor (`image 0.02, audio 0.015, video 0.30` in
`agent/hitl.py::COST_FLOORS`) and the gate compares `max(estimate, floor)`.
The estimate is model-generated, and the request text shares the classifier's
context — a user can effectively set their own price, which made the old gate
tractable.

---

## Hardening notes

- **The classifier never 500s.** A syntactically valid but non-object response
  (`[]`, `"video"`, `123`) used to crash on `.setdefault` outside the try and
  take the endpoint down with it. It now routes through the same heuristic
  fallback as a dead LLM call (`agent/intent.py`).
- **Video polls stop, not spin.** A poll whose HTTP call failed used to match no
  status branch and strand the row in `pending` forever. `"error"` is now
  retried like `pending`/`in_progress`, and `MaxRetriesExceededError` (20 × 30 s
  budget) marks the row `failed` with a message that names the limit — a job
  still running past the budget is told apart from a live one (`tasks.py`).
- **`?refresh=1` cannot stampede.** Concurrent refreshes are serialised by a
  short lock (`capabilities.py`); the loser serves the cached copy instead of
  each firing its own OpenRouter call.
- **The conversation list is not N+1.** `last_message` is annotated via
  `Subquery` on the viewset's queryset instead of one query per row.
- **Quality is only sent when the model has it.** A model with no advertised
  `qualities` no longer forwards an unvalidated `quality` the provider would
  reject (`agent/intent.py`).

---

## Frontend

Two views over the same backend, toggled in the page header.

| | Agent | Studio |
|---|---|---|
| Entry | Natural language; the router picks modality, model and params | You pick everything |
| Model choice | Modality selector defaults to **Auto**; naming a modality reveals the picker and pins the model, which is sent as `model` on `/agent/chat/` | `ModelPicker` in both the composer and the options rail |
| Confirmation | HITL intent card (`IntentPreviewCard`), whose edit mode now uses the same picker instead of a raw model-id text field | None — submit generates |

Key modules:

- `api/imagine.ts` — typed client. `ModelCapability` is the contract; the page
  previously typed the capability response as `any`, which is how a backend
  returning three empty arrays went unnoticed.
- `components/imagine/ModelPicker.tsx` — searchable dialog, recommended models
  pinned. Replaces a flat sidebar list that was `hidden xl:flex`, i.e. absent
  below 1280px.
- `hooks/useImagineStudio.ts` — form state. Every control's options come from
  the selected model's advertised values and re-snap on model change.
- `components/imagine/GenerationControls.tsx` — renders only the controls the
  selected model actually supports.
- `components/imagine/AspectRatioSelector.tsx` — draws each ratio at its true
  proportion. A model can advertise 17 ratios; `9:19.5` as a text chip tells you
  nothing about the shape.
- `components/imagine/ModelRail.tsx` — the model tiles **on the page**. Mainstream
  generators keep the model visible and one click away (Leonardo: a model card on
  the canvas; Krea: a row of tiles above the prompt); ours was behind a dialog, so
  you had to know the picker existed before you could learn which models did. The
  rail shows the recommended set plus the active model, with a "Browse all" tile
  that opens `ModelPicker` headless (`openOnMount` + `hideTrigger`) for the full
  searchable catalog.
- `components/imagine/PillSelect.tsx` — inline dropdowns in the prompt bar for the
  two settings people change constantly (ratio, size/length/voice). Opens upward:
  the prompt bar sits low, so a downward menu is clipped by the results grid.
- `components/imagine/StyleGallery.tsx` + `lib/imagineStyles.ts` — style presets
  that **actually modify the request**. The old five cards were read once to
  highlight the active one and used nowhere else; these append a modifier to the
  prompt at generate time, and the card shows exactly what gets appended. Swatches
  are CSS gradients, not the remote Unsplash thumbnails the old cards loaded — a
  style picker that needs the network to draw its own controls renders blank on a
  slow connection.

The composed prompt (user text + style modifier) is what gets **stored** on the
`Generation` row, so a history entry is the exact string the model saw and can be
reproduced. Storing the bare prompt would lose half of it.

Studio's layout puts model choice **above** the prompt: it changes the result
more than any other control and was previously the hardest thing to find.

**Removed, not fixed:** style presets (five thumbnails whose selection was
never sent anywhere), the motion-intensity slider and FPS selector (OpenRouter's
video API accepts neither, and the page never sent them), and the Share button
(no share surface exists). Download and Delete were dead buttons and are now
wired.

**Completion arrives over the socket.** Studio subscribes to
`ws/imagine-agent/` via `useSocket` and refetches the row on
`generation.completed|failed`; a 15 s poll remains only as a backstop while the
socket is disconnected. The previous implementation ran a 5 s `setInterval` in
an effect that depended on `results` *and* called `setResults` inside itself,
rebuilding the timer on every tick, while ignoring the broadcast the backend
was already sending.

---

## Code references

- Catalog + curated TTS table: [imagine/services/catalog.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/catalog.py)
- Cached catalog accessor: [imagine/services/capabilities.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/capabilities.py)
- Tests: [imagine/tests/test_catalog.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/tests/test_catalog.py), [imagine/tests/test_api.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/tests/test_api.py)
- Service: [imagine/services/openrouter.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/openrouter.py)
- Dispatcher: [imagine/services/dispatcher.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/dispatcher.py) (image inline/async split via `RUN_WORKFLOWS_ASYNC`)
- WS events: [imagine/services/events.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/events.py)
- Agent flow: [imagine/agent/graph.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/graph.py), [imagine/agent/intent.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/intent.py)
- HITL: [imagine/agent/hitl.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/hitl.py)
- Workers: [imagine/tasks.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/tasks.py) (video poller + image task)
- Tests: [imagine/tests/test_dispatcher.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/tests/test_dispatcher.py) (async split + cost throttle)
- WS consumer: [imagine/consumers.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/consumers.py)
- Credentials seed: [populate_credentials.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/populate_credentials.py) (`slug='openrouter'`)

## External references

- [OpenRouter image generation guide](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)
- [Unified Image API announcement](https://openrouter.ai/blog/announcements/image-api/)
- [Image model collection](https://openrouter.ai/collections/image-models) · [TTS model collection](https://openrouter.ai/collections/text-to-speech-models)
- [Create video request API ref](https://openrouter.ai/docs/api/api-reference/video-generation/create-videos)
- [Poll video status API ref](https://openrouter.ai/docs/api/api-reference/video-generation/get-videos)
- [TTS guide](https://openrouter.ai/docs/guides/overview/multimodal/tts)
