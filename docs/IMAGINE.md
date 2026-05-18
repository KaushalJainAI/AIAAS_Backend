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

Frontend status updates are pushed via Channels group `imagine_agent_{user_id}` with event types `generation.started | progress | completed | failed`.

---

## Credentials

**OpenRouter API keys never live in `settings` or `.env`** — they're stored per-user in the encrypted `credentials/` vault (Fernet/AES at rest, same path as every other integration).

- CredentialType: `slug='openrouter'`, `name='OpenRouter API'`, `auth_method='api_key'`, field name `apiKey` (seeded by `populate_credentials.py`).
- Lookup: `OpenRouterService.for_user(user)` resolves the user's most-recently-updated active credential, decrypts it, and returns an instance bound to that key. Falls back to `api_key` / `token` field names for older entries.
- Missing-credential path: raises `MissingOpenRouterCredentialError`. Surfaces as:
  - **`GET /imagine/capabilities/`** → HTTP 400 with `detail` message and empty modality lists.
  - **`run_generation`** → `Generation.status='failed'`, `error_message` set, and a `generation.failed` WS event.
  - **`classify`** → intent with `confidence=0.0`, `missing_required=['credential']`, and the message as `clarifying_question`.
  - **`poll_video_generation`** → marks the in-flight job failed and broadcasts.

To onboard: in the app, **Credentials → New → "OpenRouter API" → paste key into `apiKey`**. No restart required; the dispatcher resolves it on the next request.

---

## OpenRouter API surface used

All calls go to base URL `https://openrouter.ai/api/v1` with headers `Authorization: Bearer <key>`, `HTTP-Referer: https://better-n8n.com`, `X-Title: Better n8n Imagine`.

### Image — `POST /chat/completions`

Image generation rides on chat-completions with the `modalities` flag.

**Request**
```json
{
  "model": "google/gemini-3.1-flash-image-preview",
  "messages": [
    {"role": "system", "content": "Negative prompt: <optional>"},
    {"role": "user", "content": "<prompt>"}
  ],
  "modalities": ["image", "text"],
  "image_config": {
    "aspect_ratio": "1:1 | 16:9 | 9:16 | 4:3 | 3:4 | ...",
    "image_size":   "0.5K | 1K | 2K | 4K",
    "strength":     0.0-1.0     // optional, image-to-image only
  }
}
```

**Response** — `choices[0].message.images[0].image_url.url` contains a base64 **data URL** (`data:image/png;base64,…`), not a remote URL. The frontend can drop this straight into an `<img src>`.

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
  "voice": "alloy | echo | fable | onyx | nova | shimmer | <provider-specific>",
  "response_format": "mp3",   // we always set mp3; OpenRouter default is pcm
  "speed": 1.0                 // optional
}
```

**Response** — raw audio bytes (`Content-Type: audio/mpeg`). The service base64-encodes them into a data URL (`data:audio/mpeg;base64,…`) so the frontend can play them in a standard `<audio>` element without a second fetch.

### Model catalog — `GET /models` and `GET /videos/models`

`OpenRouterService.fetch_models()` fetches both and buckets entries by `output_modalities` containing `image`, `video`, or `audio`. Video metadata (`supported_resolutions`, `supported_aspect_ratios`, `supported_durations`) is merged in from the dedicated video catalog. Results are cached in Django cache under key `openrouter_capabilities` for 1 hour.

---

## Code references

- Service: [imagine/services/openrouter.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/openrouter.py)
- Dispatcher: [imagine/services/dispatcher.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/services/dispatcher.py)
- Agent flow: [imagine/agent/graph.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/graph.py), [imagine/agent/intent.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/intent.py)
- HITL: [imagine/agent/hitl.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/agent/hitl.py)
- Video poller: [imagine/tasks.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/tasks.py)
- WS consumer: [imagine/consumers.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/imagine/consumers.py)
- Credentials seed: [populate_credentials.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/populate_credentials.py) (`slug='openrouter'`)

## External references

- [OpenRouter image generation guide](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)
- [Create video request API ref](https://openrouter.ai/docs/api/api-reference/video-generation/create-videos)
- [Poll video status API ref](https://openrouter.ai/docs/api/api-reference/video-generation/get-videos)
- [TTS guide](https://openrouter.ai/docs/guides/overview/multimodal/tts)
