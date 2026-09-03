"""Model catalog for media generation.

Three modalities, three different discovery stories — this module exists to
hide that asymmetry behind one shape.

- **Image** and **video** have dedicated catalog endpoints
  (`/images/models`, `/videos/models`) that advertise, per model, exactly which
  resolutions / aspect ratios / durations that model accepts. We use them.
- **Audio (TTS)** has *no* discovery endpoint. `/api/v1/models` does not list
  the speech models at all — `minimax/speech-2.8-hd`, `hexgrad/kokoro-82m` and
  friends are absent from it — and voices are not exposed by any API. So the
  TTS catalog is a curated table here, and `TTS_MODELS` is the thing you edit
  when OpenRouter ships a new voice model.

The previous implementation read `output_modalities` off the top level of
`/api/v1/models`, where the field does not exist (it lives under
`architecture.output_modalities`). Every bucket came back empty, which is why
no model could be picked anywhere in the UI. Two guards now stand against a
silent repeat: `tests/test_catalog.py` asserts each bucket is non-empty
against a recorded fixture of the real payloads.

The opposite mistake was made later and is also fixed here: `normalize_*` used
to *invent* option lists for a model that advertised none. See the note above
`normalize_image_model` — a dial is offered only where the model claims it, and
only with the values it claims, because the API rejects anything else.
"""
from typing import Any, Dict, List, Optional

#: Shape returned to the frontend for every modality.
Capabilities = Dict[str, List[Dict[str, Any]]]

EMPTY_CAPABILITIES: Capabilities = {"image": [], "video": [], "audio": []}


# ── curated defaults ─────────────────────────────────────────────────────────

#: Models surfaced first in the picker. Everything else stays reachable via
#: search — this list only decides what a user sees before they type. Ordered:
#: the first entry present in the live catalog becomes the default selection.
RECOMMENDED: Dict[str, List[str]] = {
    "image": [
        "google/gemini-3.1-flash-image",
        "bytedance-seed/seedream-5-0-pro",
        "black-forest-labs/flux.2-pro",
        "openai/gpt-image-2",
        "google/gemini-3-pro-image",
        "qwen/qwen-image-3-pro",
    ],
    "video": [
        "google/veo-3.1-fast",
        "bytedance/seedance-2.5",
        "openai/sora-2-pro",
        "minimax/hailuo-3",
        "kwaivgi/kling-v3.0-pro",
    ],
    "audio": [
        "openai/gpt-4o-mini-tts",
        "minimax/speech-2.8-hd",
        "google/gemini-3.1-flash-tts-preview",
    ],
}

#: Curated TTS catalog — see module docstring for why this is hand-maintained.
#: `voices` is provider-specific and unobtainable from the API; an empty list
#: means "this model takes a free-form voice id", which the UI renders as a
#: text field rather than a chip row.
TTS_MODELS: List[Dict[str, Any]] = [
    {
        "id": "openai/gpt-4o-mini-tts",
        "name": "OpenAI: GPT-4o Mini TTS",
        "description": "Low-latency English-first speech with steerable delivery.",
        "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer", "coral", "sage"],
        "supports_speed": True,
        # "steerable delivery" is this: the OpenAI speech models take a free-text
        # `instructions` field ("speak in a warm, unhurried tone"). No other
        # family here accepts it, so it is declared rather than assumed.
        "supports_instructions": True,
    },
    {
        "id": "minimax/speech-2.8-hd",
        "name": "MiniMax: Speech 2.8 HD",
        "description": "High-fidelity multilingual speech, strong prosody on long text.",
        "voices": [],
        "supports_speed": True,
    },
    {
        "id": "minimax/speech-2.8-turbo",
        "name": "MiniMax: Speech 2.8 Turbo",
        "description": "Faster, cheaper sibling of Speech 2.8 HD.",
        "voices": [],
        "supports_speed": True,
    },
    {
        "id": "google/gemini-3.1-flash-tts-preview",
        "name": "Google: Gemini 3.1 Flash TTS",
        "description": "Multi-speaker capable speech from the Gemini Flash family.",
        "voices": ["Puck", "Charon", "Kore", "Fenrir", "Aoede"],
        "supports_speed": False,
    },
    {
        "id": "microsoft/mai-voice-2-flash",
        "name": "Microsoft: MAI-Voice-2 Flash",
        "description": "Expressive 24 kHz speech across 15 languages and 18 locales.",
        "voices": ["en-US-Harper:MAI-Voice-2", "en-US-Aria:MAI-Voice-2"],
        "supports_speed": True,
    },
    {
        "id": "mistralai/voxtral-mini-tts",
        "name": "Mistral: Voxtral Mini TTS",
        "description": "20+ languages with five built-in voices.",
        "voices": ["Eve", "Ara", "Rex", "Sal", "Leo"],
        "supports_speed": False,
    },
    {
        "id": "hexgrad/kokoro-82m",
        "name": "hexgrad: Kokoro 82M",
        "description": "Small open-weight TTS — cheapest option for bulk narration.",
        "voices": [],
        "supports_speed": True,
    },
    {
        "id": "x-ai/grok-voice-tts-1.0",
        "name": "xAI: Grok Voice TTS 1.0",
        "description": "Conversational delivery from xAI's voice stack.",
        "voices": [],
        "supports_speed": False,
    },
]


# ── helpers ──────────────────────────────────────────────────────────────────

def provider_of(model_id: str) -> str:
    """`black-forest-labs/flux.2-pro` -> `black-forest-labs`.

    The frontend renders this as the secondary line under a model name. It used
    to read `m.provider`, which no serializer ever produced, so the line was
    always blank.
    """
    return (model_id or "").split("/")[0]


def _enum_values(spec: Any) -> List[Any]:
    """Pull the value list out of an `/images/models` parameter spec.

    Specs are typed objects, e.g. `{"type": "enum", "values": ["1K", "2K"]}` or
    `{"type": "range", "min": 1, "max": 4}`. Only enums carry a choice list;
    an absent or non-enum spec yields `[]`, which means *this model exposes no
    such control* and is never to be papered over — see `normalize_image_model`.
    """
    if isinstance(spec, dict) and spec.get("type") == "enum":
        values = spec.get("values")
        if isinstance(values, list):
            return values
    return []


def _range(spec: Any) -> Optional[Dict[str, int]]:
    """`{"type": "range", "min": 1, "max": 4}` -> `{"min": 1, "max": 4}`.

    None when the model does not advertise the parameter at all, which is the
    difference between "you may ask for 1 to 4" and "do not send this key".
    """
    if not isinstance(spec, dict) or spec.get("type") != "range":
        return None
    try:
        return {"min": int(spec.get("min", 0)), "max": int(spec.get("max", 0))}
    except (TypeError, ValueError):
        return None


# ── normalizers ──────────────────────────────────────────────────────────────

# There are deliberately no default option lists here any more.
#
# `_clean` used to substitute `["1K", "2K"]` (and five aspect ratios) whenever a
# model advertised none, so the panel would never render a dead control. The
# cost of that kindness was measured against the live API: a model advertising
# `["2K", "4K"]` answers a request for `1K` with
#
#   400 — resolution "512": not supported. Accepted: 2K, 4K
#
# and a model that advertises no `resolution` at all silently ignores the key,
# so the control moved nothing while looking like it did. Both failures come
# from the same place — offering a dial the model never claimed. An empty list
# now means exactly that, and the UI renders no control for it.
#
# The dials below are the complete set `POST /api/v1/images` and
# `POST /api/v1/videos` accept, as advertised per model by `/images/models` and
# `/videos/models`.


def normalize_image_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`/api/v1/images/models` entry -> our capability shape.

    Every key in `supported_parameters` is surfaced, because every one of them
    is a request parameter the user is entitled to set. The union across the
    live catalog is: aspect_ratio, background, input_references, n,
    output_compression, output_format, quality, resolution, seed — and they are
    genuinely per-model (only the OpenAI image models take `quality` and
    `background`; only the vector and FLUX families take `output_format`).
    """
    model_id = raw.get("id") or ""
    params = raw.get("supported_parameters") or {}
    architecture = raw.get("architecture") or {}

    references = _range(params.get("input_references"))
    batch = _range(params.get("n"))
    compression = _range(params.get("output_compression"))

    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "resolutions": _enum_values(params.get("resolution")),
        "aspect_ratios": _enum_values(params.get("aspect_ratio")),
        "qualities": _enum_values(params.get("quality")),
        "output_formats": _enum_values(params.get("output_format")),
        "backgrounds": _enum_values(params.get("background")),
        #: {min,max} or None. None means "do not send this key".
        "output_compression": compression,
        "batch": batch,
        # Retained as the flat number the picker already reads; `batch` is the
        # authority, and 0 means the model does not take `n` at all.
        "max_batch": (batch or {}).get("max", 0),
        "max_references": (references or {}).get("max", 0),
        "supports_seed": bool(params.get("seed")),
        "supports_streaming": bool(raw.get("supports_streaming")),
        # Kept for the existing picker badge. Note it is now strictly about
        # whether references may be *sent*, not about input modalities: a model
        # that lists `image` under inputs but advertises no `input_references`
        # range has nowhere to put one.
        "supports_references": (references or {}).get("max", 0) > 0,
        "input_modalities": list(architecture.get("input_modalities") or []),
    }


def normalize_video_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`/api/v1/videos/models` entry -> our capability shape.

    The video catalog advertises its dials as plain lists rather than typed
    specs. `supported_sizes` and `supported_frame_images` were being dropped:
    the first is the explicit `WIDTHxHEIGHT` alternative to a resolution tier,
    the second is what makes image-to-video possible at all — a model taking
    `first_frame` can be handed the picture a clip should start from.
    """
    model_id = raw.get("id") or ""
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "resolutions": list(raw.get("supported_resolutions") or []),
        "aspect_ratios": list(raw.get("supported_aspect_ratios") or []),
        "sizes": list(raw.get("supported_sizes") or []),
        "durations": list(raw.get("supported_durations") or []),
        #: `first_frame` / `last_frame` — which ends of the clip may be pinned.
        "frame_slots": list(raw.get("supported_frame_images") or []),
        "supports_audio": bool(raw.get("generate_audio")),
        "supports_seed": bool(raw.get("seed")),
    }


#: What `POST /api/v1/audio/speech` returns. `pcm` is the API default; we ask
#: for `mp3` unless told otherwise, because a `<audio>` element can play it.
AUDIO_RESPONSE_FORMATS = ["mp3", "pcm"]

#: The documented range. The slider used to run 0.25–4.0, which is the OpenAI
#: chat-TTS range, not this endpoint's — anything outside is a provider error.
AUDIO_SPEED_RANGE = {"min": 0.5, "max": 2.0}


def normalize_audio_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    model_id = raw.get("id") or ""
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "voices": list(raw.get("voices") or []),
        "supports_speed": bool(raw.get("supports_speed")),
        "speed_range": dict(AUDIO_SPEED_RANGE) if raw.get("supports_speed") else None,
        "response_formats": list(AUDIO_RESPONSE_FORMATS),
        #: Tone direction ("speak warmly"). An OpenAI-family extra, forwarded
        #: as a provider option — declared per model so the box is not offered
        #: where it would be dropped on the floor.
        "supports_instructions": bool(raw.get("supports_instructions")),
    }


def audio_catalog() -> List[Dict[str, Any]]:
    return [normalize_audio_model(m) for m in TTS_MODELS]


# ── ordering ─────────────────────────────────────────────────────────────────

def sort_by_recommendation(kind: str, models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recommended models first (in their curated order), then the rest.

    The rest keep the catalog's own order, which OpenRouter returns newest
    first — so an unlisted brand-new model still surfaces near the top.
    """
    ranking = {model_id: i for i, model_id in enumerate(RECOMMENDED.get(kind, []))}
    return sorted(models, key=lambda m: ranking.get(m["id"], len(ranking)))


def default_model_id(kind: str, models: List[Dict[str, Any]]) -> Optional[str]:
    """First recommended model that actually exists in the live catalog."""
    available = {m["id"] for m in models}
    for model_id in RECOMMENDED.get(kind, []):
        if model_id in available:
            return model_id
    return models[0]["id"] if models else None


def find_model(caps: Capabilities, kind: str, model_id: str) -> Optional[Dict[str, Any]]:
    for model in caps.get(kind) or []:
        if model["id"] == model_id:
            return model
    return None
