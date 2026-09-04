"""LLM-driven intent classifier: NL message -> structured generation intent."""
import json
import logging
from typing import Any, Dict, List, Optional

from django.conf import settings

from ..services import catalog
from ..services.capabilities import capabilities_for
from ..services.openrouter import MissingOpenRouterCredentialError, OpenRouterService
from ..validation import constrain

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
    "aspect_ratio": "image/video — only a value the chosen model lists",
    "resolution": "image/video — only a value the chosen model lists",
    "size": "video — explicit WIDTHxHEIGHT, where the model lists sizes",
    "duration": "integer seconds (video only)",
    "quality": "image — only where the model lists qualities",
    "output_format": "image — png|jpeg|webp, only where the model lists them",
    "background": "image — auto|transparent|opaque, only where listed",
    "output_compression": "image — 0-100, jpeg/webp only",
    "batch_size": "image — how many images to return, within the model's range",
    "negative_prompt": "string (optional)",
    "voice": "audio — only a voice the chosen model lists",
    "speed": "audio — float 0.5-2.0",
    "instructions": "audio — tone direction, only where the model lists support"
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
    """Render the catalog for the router prompt.

    Capped per modality because the live catalog runs to 40+ image models and
    23 video models; the list is already ordered with the recommended ones
    first, so the cap trims the tail rather than the models worth choosing.

    Reduced to 12 per modality and stripped of display names / long extras to
    keep the router prompt under ~4k tokens. The previous shape sent 20 per
    modality with full names plus 6 aspect_ratios + 6 resolutions each — ~5k
    tokens before the user's message — which pushed the model past the 98s
    litellm timeout and produced a 49k truncated JSON that failed parsing.
    The router only needs the ids to choose; parameter validation happens in
    `_constrain_params` post-router.
    """
    lines: List[str] = []
    for kind in ("image", "video", "audio"):
        items = caps.get(kind) or []
        # 12 is enough to cover RECOMMENDED plus a few alternates; the router
        # can still fall back to defaults for anything not shown.
        shown = items[:12]
        lines.append(
            f"\n## {kind.upper()} models ({len(items)} available"
            + (f", showing first {len(shown)}" if len(items) > len(shown) else "")
            + ")"
        )
        for m in shown:
            # Only the id matters for the decision; extras are trimmed to one
            # token each to hint at capabilities without bloating the prompt.
            hint = ""
            if kind == "video" and m.get("durations"):
                hint = f" durations={','.join(map(str, m['durations'][:4]))}"
            elif kind == "audio" and m.get("voices"):
                hint = f" voices={len(m['voices'])}"
            lines.append(f"- {m['id']}{hint}")
    return "\n".join(lines)


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
    model = catalog.default_model_id(kind, pool)
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


#: The three things this app can generate. Named once because the router
#: validates against it, the pin lookup walks it, and the fallback re-checks it.
_MODALITIES = ("image", "video", "audio")

#: Turns the router returns are shaped by these; each key has a safe default so
#: a sparse reply from the model still produces a complete intent.
_INTENT_DEFAULTS = {
    "type": "image",
    "confidence": 0.5,
    "missing_required": [],
    "clarifying_question": None,
    "estimated_cost_usd": 0.05,
    "reasoning": "",
}


def _missing_credential_intent(message: str, reason: str) -> Dict[str, Any]:
    """The intent returned when the user has no OpenRouter credential.

    Shaped like any other intent so callers need no special case; the empty
    `model` and the `credential` entry in `missing_required` are what the UI
    reads to prompt for a key.
    """
    return {
        "type": "image",
        "model": None,
        "prompt": message,
        "params": {},
        "confidence": 0.0,
        "missing_required": ["credential"],
        "clarifying_question": reason,
        "estimated_cost_usd": 0.0,
        "reasoning": "no openrouter credential configured",
    }


def _pinned_kind(caps: Dict[str, List[Dict[str, Any]]],
                 preferred_model: Optional[str]) -> Optional[str]:
    """Which modality the user's explicitly chosen model belongs to, if any."""
    if not preferred_model:
        return None
    return next(
        (kind for kind in _MODALITIES if catalog.find_model(caps, kind, preferred_model)),
        None,
    )


def _router_messages(message: str, caps: Dict[str, List[Dict[str, Any]]],
                     history: List[Dict[str, str]], preferred_model: Optional[str],
                     pinned_kind: Optional[str]) -> List[Dict[str, str]]:
    """The chat transcript sent to the router model."""
    user_block = f"User request: {message}\n\nAvailable models:{_capabilities_summary(caps)}"
    if pinned_kind:
        user_block += (
            f"\n\nThe user has explicitly selected the model '{preferred_model}' "
            f"({pinned_kind}). Use exactly that model and that type. Choose only "
            f"the prompt and the params."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[-6:],
        {"role": "user", "content": user_block},
    ]


def _extract_json(text: str) -> Dict[str, Any]:
    """Parse JSON from a model reply that may be wrapped in markdown fences.

    The router is instructed to return bare JSON, but litellm / OpenRouter
    models occasionally wrap it in ```json``` fences or prepend commentary
    when the prompt is long. We strip fences and fall back to extracting the
    first {...} block before giving up — the caller turns any failure into
    the heuristic fallback, so robustness here directly reduces `intent LLM
    call failed` log spam and the 98 s user wait observed in prod.
    """
    import re

    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty router response")

    # Strip common markdown fencing: ```json\n{...}\n``` or ```\n{...}\n```
    if raw.startswith("```"):
        # Remove opening fence line
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
        # Remove trailing fence
        raw = re.sub(r"\n?```\s*$", "", raw)

    # Direct parse first
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
        raise ValueError("intent JSON was not an object")
    except json.JSONDecodeError:
        pass

    # Fallback: extract first balanced-looking { ... } block (greedy last })
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict):
                return obj
            raise ValueError("intent JSON was not an object")
        except json.JSONDecodeError as e:
            # Include snippet for diagnostics before falling through
            logger.warning("Router JSON extract failed (len=%d, snippet=%.200s): %s", len(text), raw[:200], e)

    raise ValueError(f"could not parse router JSON (len={len(text)})")


def _ask_router(model_id: str, messages: List[Dict[str, str]],
                 api_key: Optional[str]) -> Dict[str, Any]:
    """Call the router model and return its parsed JSON object.

    Raises on anything that is not a JSON object -- the caller turns every
    failure into the same heuristic fallback, so there is nothing to gain from
    distinguishing them here.
    """
    import litellm

    kwargs: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "timeout": 30,
        # Bound the completion so a model that tries to echo the catalog is
        # cut short rather than streaming 49k of truncated JSON.
        "max_tokens": 800,
    }
    if api_key and model_id.startswith("openrouter/"):
        kwargs["api_key"] = api_key
    resp = litellm.completion(**kwargs)
    content = resp.choices[0].message.content or "{}"
    # Defensive: if the model returned >10k, it's not the shape we asked for;
    # log and let the extractor attempt a rescue rather than feeding a huge
    # truncated string straight to json.loads.
    if len(content) > 10000:
        logger.warning("Router returned large payload (%d chars, truncated to 500): %.500s", len(content), content[:500])
    return _extract_json(content)


def _with_defaults(intent: Dict[str, Any], message: str) -> Dict[str, Any]:
    """Fill in every key a caller is entitled to read. Mutates and returns."""
    for key, default in _INTENT_DEFAULTS.items():
        intent.setdefault(key, list(default) if isinstance(default, list) else default)
    intent.setdefault("prompt", message)
    intent["params"] = _coerce_params(intent.get("params"))
    return intent


def _ensure_valid_model(intent: Dict[str, Any],
                        caps: Dict[str, List[Dict[str, Any]]]) -> None:
    """Repair a model id the router invented. Mutates `intent`.

    A hallucinated id is recoverable -- fall back to the modality's default and
    mark the answer less confident. Having no model at all is not, so that is
    reported through `missing_required` for the UI to ask about.
    """
    valid_ids = {m["id"] for m in caps.get(intent["type"], [])}
    if intent.get("model") in valid_ids:
        return
    fallback = catalog.default_model_id(intent["type"], caps.get(intent["type"]) or [])
    if fallback:
        intent["model"] = fallback
        intent["confidence"] = min(intent["confidence"], 0.6)
        intent["reasoning"] = (
            intent["reasoning"] + " | model id was invalid; fell back to the default"
        ).strip(" |")
    else:
        intent["model"] = None
        if "model" not in intent["missing_required"]:
            intent["missing_required"].append("model")


def classify(
    message: str,
    user,
    history: Optional[List[Dict[str, str]]] = None,
    preferred_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronous classifier; returns the intent dict.

    `user` is required so the OpenRouter API key can be pulled from the
    encrypted credentials vault rather than environment.

    `preferred_model` is the model the user picked in the UI. When it is a real
    catalog entry it wins outright -- the router still chooses the prompt and
    parameters, but an explicit choice is not something to second-guess, and
    the modality is taken from whichever bucket the model lives in.
    """
    try:
        service = OpenRouterService.for_user(user)
    except MissingOpenRouterCredentialError as e:
        return _missing_credential_intent(message, str(e))

    caps = capabilities_for(user)
    pinned_kind = _pinned_kind(caps, preferred_model)
    model_id = getattr(settings, "IMAGINE_AGENT_MODEL", "openrouter/openai/gpt-4o-mini")

    # Short-circuit the LLM entirely when the user already pinned a model:
    # the modality and model are decided, only the prompt needs refining and
    # params defaulting. This avoids a 30 s LLM call on the 50% of agent
    # requests where the user picked a model in the composer.
    if pinned_kind and preferred_model:
        return {
            "type": pinned_kind,
            "model": preferred_model,
            "prompt": message,
            "params": {},
            "confidence": 0.85,
            "missing_required": [],
            "clarifying_question": None,
            "estimated_cost_usd": 0.05,
            "reasoning": "pinned model short-circuit (no router LLM)",
        }

    try:
        intent = _ask_router(
            model_id,
            _router_messages(message, caps, history or [], preferred_model, pinned_kind),
            service.api_key,  # routed through the per-user credential
        )
    except ImportError:
        logger.warning("litellm not installed; using heuristic fallback")
        return _fallback_intent(message, caps)
    except Exception as e:
        logger.exception(f"intent LLM call failed: {e}")
        return _fallback_intent(message, caps)

    _with_defaults(intent, message)

    # An explicit UI selection overrides whatever the router decided, including
    # the modality -- picking a video model *is* the request for a video.
    if pinned_kind:
        intent["type"] = pinned_kind
        intent["model"] = preferred_model

    if intent["type"] not in _MODALITIES:
        intent["type"] = "image"

    _ensure_valid_model(intent, caps)
    intent["params"] = _constrain_params(intent, caps)
    return intent


def _constrain_params(intent: Dict[str, Any], caps: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Drop params the chosen model does not accept, snap the rest to its enums.

    The router is told each model's supported values but does not reliably
    respect them, and an out-of-range aspect ratio or duration is rejected by
    OpenRouter well after the user has approved the plan. Constraining here
    means the HITL card shows what will actually be sent.

    The rule itself lives in `imagine/validation.py`, shared with the form
    path — which *refuses* rather than drops, because there a human set the
    dial deliberately. This is the same table read with the other policy: two
    copies would have meant the conversational path quietly keeping whatever
    dial the last catalogue change added.
    """
    model = catalog.find_model(caps, intent["type"], intent.get("model") or "")
    params = dict(intent.get("params") or {})
    if not model:
        return params
    # `negative_prompt` is not a provider dial (it is folded into the prompt),
    # so it is carried through rather than validated against anything.
    constrained = constrain(intent["type"], model, params)
    if params.get("negative_prompt"):
        constrained["negative_prompt"] = params["negative_prompt"]
    return constrained
