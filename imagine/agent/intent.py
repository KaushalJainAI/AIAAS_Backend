"""LLM-driven intent classifier: NL message -> structured generation intent."""
import json
import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.cache import cache

from ..services.openrouter import OpenRouterService

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an intent router for a media generation app. The user describes \
something they want to create (image, video, or audio). You must return a single JSON object \
that picks the right modality, model, and parameters.

You will be given a list of available models per modality. ONLY return a model id that appears \
in that list. If no model is suitable, set "model" to null and "missing_required" to ["model"].

Return JSON with this exact shape (no markdown, no commentary):
{
  "type": "image" | "video" | "audio",
  "model": "<model id from the available list> or null",
  "prompt": "<the prompt to send to the model — refine the user's request>",
  "params": {
    "aspect_ratio": "1:1|16:9|9:16|4:3|3:4 (image/video)",
    "resolution": "string (image/video)",
    "duration": "integer seconds (video only)",
    "negative_prompt": "string (optional)",
    "voice": "alloy|echo|fable|onyx|nova|shimmer (audio)",
    "speed": "float 0.25-4.0 (audio)"
  },
  "confidence": 0.0-1.0,
  "missing_required": ["list of fields the user did not specify that the model truly needs"],
  "clarifying_question": "string asking for the missing info, or null",
  "estimated_cost_usd": <best-effort number>,
  "reasoning": "one short sentence explaining the choice"
}

Cost guidance: typical image ~$0.02, audio TTS ~$0.015 per 1k chars, video 5-10s ~$0.30-1.00.
Keep "params" keys only if relevant to the chosen type.
Confidence should drop below 0.7 when the request is ambiguous (e.g., 'make me something cool')."""


def _capabilities_summary(caps: Dict[str, List[Dict[str, Any]]]) -> str:
    lines: List[str] = []
    for kind in ("image", "video", "audio"):
        items = caps.get(kind) or []
        lines.append(f"\n## {kind.upper()} models ({len(items)} available)")
        for m in items[:25]:
            extras = []
            if m.get("aspect_ratios"):
                extras.append(f"ar={','.join(m['aspect_ratios'][:5])}")
            if m.get("resolutions"):
                extras.append(f"res={','.join(map(str, m['resolutions'][:5]))}")
            if m.get("durations"):
                extras.append(f"dur={','.join(map(str, m['durations']))}")
            if m.get("voices"):
                extras.append(f"voices={','.join(m['voices'][:6])}")
            extra = " " + " ".join(extras) if extras else ""
            lines.append(f"- {m['id']}: {m.get('name', '')}{extra}")
    return "\n".join(lines)


def _get_capabilities() -> Dict[str, List[Dict[str, Any]]]:
    caps = cache.get("openrouter_capabilities")
    if not caps:
        caps = OpenRouterService.fetch_models()
        if any(caps.get(k) for k in ("image", "video", "audio")):
            cache.set("openrouter_capabilities", caps, 3600)
    return caps or {"image": [], "video": [], "audio": []}


def _fallback_intent(message: str, caps: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Heuristic fallback if the LLM call fails entirely."""
    lower = message.lower()
    if any(w in lower for w in ("video", "clip", "animation", "drone", "footage")):
        kind = "video"
    elif any(w in lower for w in ("audio", "voice", "speech", "narrate", "say ", "tts", "song", "music")):
        kind = "audio"
    else:
        kind = "image"
    pool = caps.get(kind) or []
    model = pool[0]["id"] if pool else None
    return {
        "type": kind,
        "model": model,
        "prompt": message,
        "params": {},
        "confidence": 0.4 if model else 0.0,
        "missing_required": [] if model else ["model"],
        "clarifying_question": None if model else f"No {kind} models are available right now.",
        "estimated_cost_usd": 0.05,
        "reasoning": "fallback heuristic (LLM unavailable)",
    }


def _coerce_params(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        out[k] = v
    return out


def classify(message: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Synchronous classifier; returns the intent dict."""
    caps = _get_capabilities()
    history = history or []

    try:
        import litellm
    except ImportError:
        logger.warning("litellm not installed; using heuristic fallback")
        return _fallback_intent(message, caps)

    model_id = getattr(settings, "IMAGINE_AGENT_MODEL", "openrouter/openai/gpt-4o-mini")
    api_key = getattr(settings, "OPEN_ROUTER_KEY", None)

    user_block = f"User request: {message}\n\nAvailable models:{_capabilities_summary(caps)}"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        msgs.append(h)
    msgs.append({"role": "user", "content": user_block})

    try:
        kwargs: Dict[str, Any] = {
            "model": model_id,
            "messages": msgs,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "timeout": 30,
        }
        if api_key and model_id.startswith("openrouter/"):
            kwargs["api_key"] = api_key
        resp = litellm.completion(**kwargs)
        content = resp.choices[0].message.content or "{}"
        intent = json.loads(content)
    except Exception as e:
        logger.exception(f"intent LLM call failed: {e}")
        return _fallback_intent(message, caps)

    intent.setdefault("type", "image")
    intent.setdefault("prompt", message)
    intent["params"] = _coerce_params(intent.get("params"))
    intent.setdefault("confidence", 0.5)
    intent.setdefault("missing_required", [])
    intent.setdefault("clarifying_question", None)
    intent.setdefault("estimated_cost_usd", 0.05)
    intent.setdefault("reasoning", "")

    valid_ids = {m["id"] for m in caps.get(intent["type"], [])}
    if intent.get("model") not in valid_ids:
        pool = caps.get(intent["type"]) or []
        if pool:
            intent["model"] = pool[0]["id"]
            intent["confidence"] = min(intent["confidence"], 0.6)
            intent["reasoning"] = (intent["reasoning"] + " | model id was invalid; auto-picked first available").strip(" |")
        else:
            intent["model"] = None
            if "model" not in intent["missing_required"]:
                intent["missing_required"].append("model")

    return intent
