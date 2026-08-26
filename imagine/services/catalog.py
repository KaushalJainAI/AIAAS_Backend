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
silent repeat: `normalize_*` never invents an empty option list, and
`tests/test_catalog.py` asserts each bucket is non-empty against a recorded
fixture of the real payloads.
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
    anything else yields none, and the caller falls back to a default.
    """
    if isinstance(spec, dict) and spec.get("type") == "enum":
        values = spec.get("values")
        if isinstance(values, list):
            return values
    return []


def _range_max(spec: Any, default: int = 1) -> int:
    if isinstance(spec, dict) and spec.get("type") == "range":
        try:
            return int(spec.get("max", default))
        except (TypeError, ValueError):
            return default
    return default


def _clean(values: List[Any], fallback: List[Any]) -> List[Any]:
    """Never hand the UI an empty option list — it renders as a dead control."""
    return list(values) if values else list(fallback)


# ── normalizers ──────────────────────────────────────────────────────────────

DEFAULT_IMAGE_RESOLUTIONS = ["1K", "2K"]
DEFAULT_IMAGE_ASPECT_RATIOS = ["1:1", "16:9", "9:16", "4:3", "3:4"]
DEFAULT_VIDEO_RESOLUTIONS = ["720p"]
DEFAULT_VIDEO_ASPECT_RATIOS = ["16:9", "9:16", "1:1"]
DEFAULT_VIDEO_DURATIONS = [5]


def normalize_image_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`/api/v1/images/models` entry -> our capability shape."""
    model_id = raw.get("id") or ""
    params = raw.get("supported_parameters") or {}
    architecture = raw.get("architecture") or {}
    input_modalities = architecture.get("input_modalities") or []

    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "resolutions": _clean(
            _enum_values(params.get("resolution")), DEFAULT_IMAGE_RESOLUTIONS
        ),
        "aspect_ratios": _clean(
            _enum_values(params.get("aspect_ratio")), DEFAULT_IMAGE_ASPECT_RATIOS
        ),
        "qualities": _enum_values(params.get("quality")),
        "max_batch": _range_max(params.get("n"), default=1),
        "supports_seed": bool(params.get("seed")),
        # Image-to-image: the model accepts reference images.
        "supports_references": _range_max(params.get("input_references"), default=0) > 0
        or "image" in input_modalities,
    }


def normalize_video_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`/api/v1/videos/models` entry -> our capability shape."""
    model_id = raw.get("id") or ""
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "resolutions": _clean(
            raw.get("supported_resolutions") or [], DEFAULT_VIDEO_RESOLUTIONS
        ),
        "aspect_ratios": _clean(
            raw.get("supported_aspect_ratios") or [], DEFAULT_VIDEO_ASPECT_RATIOS
        ),
        "durations": _clean(
            raw.get("supported_durations") or [], DEFAULT_VIDEO_DURATIONS
        ),
        "supports_audio": bool(raw.get("generate_audio")),
        "supports_seed": bool(raw.get("seed")),
    }


def normalize_audio_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    model_id = raw.get("id") or ""
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "provider": provider_of(model_id),
        "description": raw.get("description") or "",
        "voices": list(raw.get("voices") or []),
        "supports_speed": bool(raw.get("supports_speed")),
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
